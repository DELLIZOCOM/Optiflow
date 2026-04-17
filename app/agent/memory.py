"""
SessionStore — conversation history, persisted to SQLite.

Each session stores the full Anthropic messages list (user + assistant turns,
including all tool call/result blocks). This allows multi-turn follow-up questions
within the same session AND survives server restarts.

Schema:
    sessions (
        session_id   TEXT PRIMARY KEY,
        messages     TEXT,          -- JSON array
        created_at   REAL,          -- time.time()
        last_accessed REAL
    )

TTL and LRU eviction are handled on every read/write to keep the DB lean.
"""

import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_TTL      = 3600   # 1 hour
_DEFAULT_MAX      = 100    # max sessions before LRU eviction


def _db_path() -> Path:
    from app.config import DATA_DIR
    cache_dir = DATA_DIR / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "sessions.db"


class SessionStore:
    """
    Thread-safe session store backed by SQLite.

    Public interface is identical to the old in-memory version — drop-in replacement.
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
                        created_at    REAL    NOT NULL,
                        last_accessed REAL    NOT NULL
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_last_accessed "
                    "ON sessions (last_accessed)"
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
            # Delete the oldest session(s) to stay under the cap
            overage = count - self._max + 1
            conn.execute("""
                DELETE FROM sessions WHERE session_id IN (
                    SELECT session_id FROM sessions
                    ORDER BY last_accessed ASC
                    LIMIT ?
                )
            """, (overage,))

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
                    "INSERT INTO sessions (session_id, messages, created_at, last_accessed) "
                    "VALUES (?, '[]', ?, ?)",
                    (session_id, now, now),
                )
                conn.commit()
            finally:
                conn.close()
        logger.info(f"SessionStore: created session {session_id}")
        return session_id

    def get_or_create(self, session_id: Optional[str]) -> str:
        """Return session_id if it exists and is not expired; otherwise create a new one."""
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
                            "UPDATE sessions SET last_accessed = ? WHERE session_id = ?",
                            (time.time(), session_id),
                        )
                        conn.commit()
                        return session_id
                finally:
                    conn.close()
        return self.create_session()

    def get_messages(self, session_id: str) -> list:
        """Return a copy of the message list for this session (empty list if not found)."""
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
        """Replace the stored message list for this session."""
        now = time.time()
        try:
            encoded = json.dumps(messages, ensure_ascii=False, default=str)
        except Exception as exc:
            logger.warning(f"SessionStore: failed to serialize messages for {session_id}: {exc}")
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

    def exists(self, session_id: str) -> bool:
        """Return True if the session exists and has not expired."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT last_accessed FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                return bool(row and (time.time() - row["last_accessed"]) <= self._ttl)
            finally:
                conn.close()

    def destroy(self, session_id: str) -> None:
        """Delete a session immediately."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
                conn.commit()
            finally:
                conn.close()
        logger.info(f"SessionStore: destroyed session {session_id}")

    def clear_all(self) -> None:
        """Destroy all sessions immediately."""
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
        """Return the current number of live (non-expired) sessions."""
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
