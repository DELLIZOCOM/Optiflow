"""
SessionStore — conversation history, persisted to SQLite.

Each session stores:
  * ``messages``     — full Anthropic message list used by the LLM (compressed
                      after each turn so it doesn't grow unboundedly).
  * ``display_log``  — a compact, UI-facing log of user + ai turns with
                      timestamps, meta badges, and any chart specs. This is
                      what the frontend replays when the user switches back
                      to a session; the LLM never sees it.
  * ``title``        — short label derived from the first user question,
                      used in the session sidebar. User-editable.
  * ``created_at``, ``updated_at``, ``last_accessed`` — lifecycle timestamps.

Design goals:
  * Durable. Sessions survive server restarts and are kept around long enough
    (30 days default) that users can navigate back to past conversations.
  * Reliable. All writes are serialized through a single thread lock; readers
    copy JSON before returning so the caller can't mutate the store.
  * Lean. The LLM message list is compressed per turn by the orchestrator;
    the display log is append-only but bounded per session.
"""

import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Keep sessions around for 30 days by default — long enough that users can
# navigate back to past conversations, short enough that the DB doesn't grow
# unbounded. LRU eviction kicks in once we hit the cap.
_DEFAULT_TTL      = 30 * 24 * 3600   # 30 days
_DEFAULT_MAX      = 1000             # max sessions before LRU eviction

# Per-session cap on display-log entries. One entry == one user or ai turn.
# At 400 turns a single session has burned way past any reasonable context
# budget anyway; at that point the user should start a new chat.
_DISPLAY_LOG_MAX  = 400

# Max length of the auto-derived session title, in characters.
_TITLE_MAX_LEN    = 80


def _db_path() -> Path:
    from app.config import DATA_DIR
    cache_dir = DATA_DIR / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "sessions.db"


def _derive_title(text: str) -> str:
    """
    Turn the first user question into a short sidebar label. Collapses
    whitespace, strips control chars, truncates with an ellipsis.
    """
    if not text:
        return "New chat"
    # Collapse all runs of whitespace (incl. newlines) to single spaces.
    cleaned = " ".join(str(text).split())
    if len(cleaned) > _TITLE_MAX_LEN:
        cleaned = cleaned[: _TITLE_MAX_LEN - 1].rstrip() + "\u2026"
    return cleaned or "New chat"


class SessionStore:
    """
    Thread-safe session store backed by SQLite.
    """

    def __init__(
        self,
        ttl: int = _DEFAULT_TTL,
        max_sessions: int = _DEFAULT_MAX,
        db_path: Optional[Path] = None,
    ):
        self._ttl  = ttl
        self._max  = max_sessions
        self._path = str(db_path or _db_path())
        self._lock = threading.Lock()
        self._init_db()

    # ── DB setup ──────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_id    TEXT PRIMARY KEY,
                        messages      TEXT    NOT NULL DEFAULT '[]',
                        display_log   TEXT    NOT NULL DEFAULT '[]',
                        title         TEXT    NOT NULL DEFAULT '',
                        created_at    REAL    NOT NULL,
                        updated_at    REAL    NOT NULL DEFAULT 0,
                        last_accessed REAL    NOT NULL
                    )
                """)

                # Forward-compat migration: older deployments had only
                # (session_id, messages, created_at, last_accessed). Add the
                # new columns in place so existing chats aren't wiped.
                cols = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
                }
                if "display_log" not in cols:
                    conn.execute(
                        "ALTER TABLE sessions ADD COLUMN display_log TEXT "
                        "NOT NULL DEFAULT '[]'"
                    )
                if "title" not in cols:
                    conn.execute(
                        "ALTER TABLE sessions ADD COLUMN title TEXT "
                        "NOT NULL DEFAULT ''"
                    )
                if "updated_at" not in cols:
                    conn.execute(
                        "ALTER TABLE sessions ADD COLUMN updated_at REAL "
                        "NOT NULL DEFAULT 0"
                    )
                    # Backfill updated_at with last_accessed so the sidebar
                    # has a sensible sort key for pre-migration rows.
                    conn.execute(
                        "UPDATE sessions SET updated_at = last_accessed "
                        "WHERE updated_at = 0"
                    )

                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_last_accessed "
                    "ON sessions (last_accessed)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_updated_at "
                    "ON sessions (updated_at DESC)"
                )
                conn.commit()
            finally:
                conn.close()
        logger.info(f"SessionStore (SQLite): initialized at {self._path}")

    # ── Internals ─────────────────────────────────────────────────────────────

    def _evict_expired(self, conn: sqlite3.Connection) -> None:
        cutoff = time.time() - self._ttl
        conn.execute("DELETE FROM sessions WHERE last_accessed < ?", (cutoff,))

    def _evict_lru_if_needed(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
        count = row[0] if row else 0
        if count >= self._max:
            overage = count - self._max + 1
            conn.execute("""
                DELETE FROM sessions WHERE session_id IN (
                    SELECT session_id FROM sessions
                    ORDER BY last_accessed ASC
                    LIMIT ?
                )
            """, (overage,))

    def _load_display_log(self, conn: sqlite3.Connection, session_id: str) -> list:
        row = conn.execute(
            "SELECT display_log FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return []
        try:
            log = json.loads(row["display_log"])
            return log if isinstance(log, list) else []
        except Exception:
            return []

    # ── Public API ────────────────────────────────────────────────────────────

    def create_session(self) -> str:
        """Create a new session and return its ID."""
        session_id = uuid.uuid4().hex[:16]
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                self._evict_expired(conn)
                self._evict_lru_if_needed(conn)
                conn.execute(
                    "INSERT INTO sessions "
                    "(session_id, messages, display_log, title, created_at, "
                    " updated_at, last_accessed) "
                    "VALUES (?, '[]', '[]', '', ?, ?, ?)",
                    (session_id, now, now, now),
                )
                conn.commit()
            finally:
                conn.close()
        logger.info(f"SessionStore: created session {session_id}")
        return session_id

    def get_or_create(self, session_id: Optional[str]) -> str:
        """Return session_id if it exists and is fresh; otherwise create."""
        if session_id:
            with self._lock:
                conn = self._connect()
                try:
                    row = conn.execute(
                        "SELECT last_accessed FROM sessions WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()
                    if row and (time.time() - row["last_accessed"]) <= self._ttl:
                        conn.execute(
                            "UPDATE sessions SET last_accessed = ? "
                            "WHERE session_id = ?",
                            (time.time(), session_id),
                        )
                        conn.commit()
                        return session_id
                finally:
                    conn.close()
        return self.create_session()

    def get_messages(self, session_id: str) -> list:
        """Return a copy of the LLM message list for this session."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT messages FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row:
                    try:
                        return json.loads(row["messages"])
                    except Exception:
                        return []
                return []
            finally:
                conn.close()

    def set_messages(self, session_id: str, messages: list) -> None:
        """Replace the stored LLM message list for this session."""
        now = time.time()
        try:
            encoded = json.dumps(messages, ensure_ascii=False, default=str)
        except Exception as exc:
            logger.warning(
                f"SessionStore: failed to serialize messages for {session_id}: {exc}"
            )
            return
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE sessions SET messages = ?, last_accessed = ? "
                    "WHERE session_id = ?",
                    (encoded, now, session_id),
                )
                conn.commit()
            finally:
                conn.close()

    # ── Display log (UI-facing transcript) ────────────────────────────────────

    def get_display_log(self, session_id: str) -> list:
        """Return the UI-facing transcript for this session."""
        with self._lock:
            conn = self._connect()
            try:
                return self._load_display_log(conn, session_id)
            finally:
                conn.close()

    def append_display_entries(
        self,
        session_id: str,
        entries: list,
        *,
        first_user_text_if_empty: Optional[str] = None,
    ) -> None:
        """
        Append one or more transcript entries to the display log atomically.

        Each entry is a small JSON-safe dict, e.g.:
            {"role": "user", "text": "...", "ts": 1735000000.0}
            {"role": "ai",   "text": "...", "ts": ..., "badges": [...],
             "charts": [...]}

        If ``first_user_text_if_empty`` is provided and the session's title
        is still empty, it will be derived from that text in the same
        transaction — so the first user turn immediately becomes the title
        without an extra round-trip.
        """
        if not entries:
            return
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT display_log, title FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if not row:
                    logger.warning(
                        f"SessionStore: append_display_entries for unknown session "
                        f"{session_id}"
                    )
                    return

                try:
                    current = json.loads(row["display_log"])
                    if not isinstance(current, list):
                        current = []
                except Exception:
                    current = []

                current.extend(entries)
                if len(current) > _DISPLAY_LOG_MAX:
                    # Trim from the head — newest turns always win.
                    current = current[-_DISPLAY_LOG_MAX:]

                try:
                    encoded = json.dumps(current, ensure_ascii=False, default=str)
                except Exception as exc:
                    logger.warning(
                        f"SessionStore: failed to serialize display_log for "
                        f"{session_id}: {exc}"
                    )
                    return

                title = row["title"] or ""
                if not title and first_user_text_if_empty:
                    title = _derive_title(first_user_text_if_empty)

                conn.execute(
                    "UPDATE sessions "
                    "SET display_log = ?, title = ?, "
                    "    updated_at = ?, last_accessed = ? "
                    "WHERE session_id = ?",
                    (encoded, title, now, now, session_id),
                )
                conn.commit()
            finally:
                conn.close()

    # ── Title management ──────────────────────────────────────────────────────

    def get_title(self, session_id: str) -> str:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT title FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                return (row["title"] if row else "") or ""
            finally:
                conn.close()

    def rename(self, session_id: str, title: str) -> bool:
        """Set a user-chosen title. Returns True if the session existed."""
        cleaned = _derive_title(title)
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "UPDATE sessions SET title = ?, last_accessed = ? "
                    "WHERE session_id = ?",
                    (cleaned, time.time(), session_id),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    # ── Enumeration ───────────────────────────────────────────────────────────

    def list_sessions(self, limit: int = 200) -> list[dict[str, Any]]:
        """
        List sessions newest-first for the sidebar. Returns a lightweight
        shape per row — no full messages, no full display log.
        """
        with self._lock:
            conn = self._connect()
            try:
                self._evict_expired(conn)
                rows = conn.execute(
                    "SELECT session_id, title, display_log, created_at, "
                    "       updated_at, last_accessed "
                    "FROM sessions "
                    "ORDER BY updated_at DESC, last_accessed DESC "
                    "LIMIT ?",
                    (int(limit),),
                ).fetchall()
                conn.commit()
            finally:
                conn.close()

        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                log = json.loads(r["display_log"]) if r["display_log"] else []
                if not isinstance(log, list):
                    log = []
            except Exception:
                log = []

            # Preview: first user message if present, else the title.
            preview = ""
            for entry in log:
                if isinstance(entry, dict) and entry.get("role") == "user":
                    preview = str(entry.get("text", ""))[:140]
                    break

            # Count user turns — a cheap "how many questions" signal.
            turn_count = sum(
                1 for e in log
                if isinstance(e, dict) and e.get("role") == "user"
            )

            out.append({
                "session_id":    r["session_id"],
                "title":         r["title"] or _derive_title(preview) or "New chat",
                "preview":       preview,
                "turn_count":    turn_count,
                "created_at":    r["created_at"],
                "updated_at":    r["updated_at"] or r["last_accessed"],
                "last_accessed": r["last_accessed"],
            })
        return out

    # ── Existence & lifecycle ────────────────────────────────────────────────

    def exists(self, session_id: str) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT last_accessed FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                return bool(
                    row and (time.time() - row["last_accessed"]) <= self._ttl
                )
            finally:
                conn.close()

    def destroy(self, session_id: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "DELETE FROM sessions WHERE session_id = ?", (session_id,)
                )
                conn.commit()
            finally:
                conn.close()
        logger.info(f"SessionStore: destroyed session {session_id}")

    def clear_messages(self, session_id: str) -> None:
        """
        Wipe the LLM message list and display log for this session, but keep
        the row (and title) so the session still shows up in the sidebar.
        Useful for "start this chat over" without losing the slot.
        """
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE sessions "
                    "SET messages = '[]', display_log = '[]', "
                    "    updated_at = ?, last_accessed = ? "
                    "WHERE session_id = ?",
                    (now, now, session_id),
                )
                conn.commit()
            finally:
                conn.close()

    def clear_all(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
                count = row[0] if row else 0
                conn.execute("DELETE FROM sessions")
                conn.commit()
            finally:
                conn.close()
        logger.info(f"SessionStore: cleared {count} session(s)")

    def session_count(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                cutoff = time.time() - self._ttl
                row = conn.execute(
                    "SELECT COUNT(*) FROM sessions WHERE last_accessed >= ?",
                    (cutoff,),
                ).fetchone()
                return row[0] if row else 0
            finally:
                conn.close()
