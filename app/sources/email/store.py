"""
EmailStore — SQLite + FTS5 backend for indexed company email.

Shape:
  - mailboxes        one row per discovered mailbox in the tenant
  - emails           canonical message table
  - emails_fts       FTS5 virtual table over {subject, from_*, to_emails,
                     body_text, attachment_names}; BM25 ranked
  - sync_state       per-mailbox ingestion cursor (delta_link, backfill flags)

The store is intentionally synchronous sqlite3 wrapped in a short
asyncio.Lock for writer serialization. Readers (search, get_email)
hold the lock only briefly; ingestion writers batch their inserts.
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS mailboxes (
    id              TEXT PRIMARY KEY,
    account_email   TEXT NOT NULL UNIQUE,
    display_name    TEXT,
    status          TEXT NOT NULL DEFAULT 'active',
    last_sync_at    REAL,
    discovered_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mailboxes_status ON mailboxes(status);

CREATE TABLE IF NOT EXISTS emails (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    mailbox_id         TEXT    NOT NULL REFERENCES mailboxes(id),
    account_email      TEXT    NOT NULL,
    provider           TEXT    NOT NULL,
    provider_msg_id    TEXT    NOT NULL,
    internet_msg_id    TEXT,
    conversation_id    TEXT,
    subject            TEXT,
    from_name          TEXT,
    from_email         TEXT,
    to_emails          TEXT,
    cc_emails          TEXT,
    bcc_emails         TEXT,
    body_text          TEXT,
    body_html_hash     TEXT,
    has_attachments    INTEGER NOT NULL DEFAULT 0,
    attachment_names   TEXT,
    folder             TEXT,
    is_read            INTEGER NOT NULL DEFAULT 0,
    importance         TEXT,
    sent_at            REAL NOT NULL,
    received_at        REAL NOT NULL,
    ingested_at        REAL NOT NULL,
    embedding          BLOB,
    embedding_model    TEXT,
    UNIQUE(mailbox_id, provider_msg_id)
);
CREATE INDEX IF NOT EXISTS idx_emails_mailbox_recv ON emails(mailbox_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_emails_conversation ON emails(mailbox_id, conversation_id);
CREATE INDEX IF NOT EXISTS idx_emails_from         ON emails(from_email);
CREATE INDEX IF NOT EXISTS idx_emails_internet_id  ON emails(internet_msg_id);

CREATE VIRTUAL TABLE IF NOT EXISTS emails_fts USING fts5(
    subject, from_name, from_email, to_emails, body_text, attachment_names,
    content='emails', content_rowid='id',
    tokenize='porter unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS emails_ai AFTER INSERT ON emails BEGIN
    INSERT INTO emails_fts(rowid, subject, from_name, from_email, to_emails, body_text, attachment_names)
    VALUES (new.id, new.subject, new.from_name, new.from_email, new.to_emails, new.body_text, new.attachment_names);
END;

CREATE TRIGGER IF NOT EXISTS emails_ad AFTER DELETE ON emails BEGIN
    INSERT INTO emails_fts(emails_fts, rowid, subject, from_name, from_email, to_emails, body_text, attachment_names)
    VALUES ('delete', old.id, old.subject, old.from_name, old.from_email, old.to_emails, old.body_text, old.attachment_names);
END;

CREATE TRIGGER IF NOT EXISTS emails_au AFTER UPDATE ON emails BEGIN
    INSERT INTO emails_fts(emails_fts, rowid, subject, from_name, from_email, to_emails, body_text, attachment_names)
    VALUES ('delete', old.id, old.subject, old.from_name, old.from_email, old.to_emails, old.body_text, old.attachment_names);
    INSERT INTO emails_fts(rowid, subject, from_name, from_email, to_emails, body_text, attachment_names)
    VALUES (new.id, new.subject, new.from_name, new.from_email, new.to_emails, new.body_text, new.attachment_names);
END;

CREATE TABLE IF NOT EXISTS sync_state (
    mailbox_id      TEXT PRIMARY KEY REFERENCES mailboxes(id),
    delta_link      TEXT,
    initial_synced  INTEGER NOT NULL DEFAULT 0,
    backfill_cursor TEXT,
    backfill_done   INTEGER NOT NULL DEFAULT 0,
    last_sync_at    REAL,
    last_error      TEXT
);
"""


# ── Entity-resolution schema (added in user_version=2) ───────────────────────
#
# `entities`   — one row per known person/org. Uniqueness on (kind, canonical_email).
# `entity_emails` — many email addresses can map to one entity (aliases). PK on
#                   (entity_id, email_address) so a re-discovery is idempotent.
#
# Index choices keep both lookup paths sub-millisecond:
#   - by email address (most common: "who emailed us from X")
#   - by display name (for fuzzy lookup: "who is Acme Corp")
#
# ON DELETE CASCADE keeps the join table consistent automatically: deleting an
# entity sweeps its addresses with no orphans.

_ENTITIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    entity_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind             TEXT NOT NULL DEFAULT 'unknown',
    display_name     TEXT,
    canonical_email  TEXT NOT NULL,
    company          TEXT,
    notes            TEXT,
    source           TEXT NOT NULL,                  -- 'manual' | 'email' | 'db:<table>'
    source_pk        TEXT,                           -- foreign key in source DB, if linked
    confidence       REAL NOT NULL DEFAULT 1.0,      -- 0.5 = auto-discovered, 1.0 = confirmed
    first_seen       REAL NOT NULL,
    last_seen        REAL NOT NULL,
    UNIQUE(kind, canonical_email)
);
CREATE INDEX IF NOT EXISTS idx_entities_email ON entities(canonical_email);
CREATE INDEX IF NOT EXISTS idx_entities_name  ON entities(display_name);

CREATE TABLE IF NOT EXISTS entity_emails (
    entity_id      INTEGER NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    email_address  TEXT NOT NULL,
    is_canonical   INTEGER NOT NULL DEFAULT 0,
    seen_count     INTEGER NOT NULL DEFAULT 1,
    last_seen      REAL NOT NULL,
    PRIMARY KEY (entity_id, email_address)
);
CREATE INDEX IF NOT EXISTS idx_entity_emails_addr ON entity_emails(email_address);
"""


def _build_fts_query(keywords: Iterable[str]) -> str:
    """
    Build an FTS5 MATCH expression from a list of keyword phrases.

    We always quote each phrase so that:
      - multi-word phrases stay phrases,
      - FTS5 syntax characters in user input are neutralized,
      - the model cannot inject raw MATCH operators.
    """
    phrases = []
    for kw in keywords:
        s = (kw or "").strip()
        if not s:
            continue
        # Escape embedded double quotes per FTS5 spec (double them).
        s = s.replace('"', '""')
        phrases.append(f'"{s}"')
    if not phrases:
        # Force zero rows; MATCH '' errors, this matches nothing cheaply.
        return '"__no_keywords_provided__"'
    return " OR ".join(phrases)


def _parse_date_range(expr: Optional[str]) -> tuple[Optional[float], Optional[float]]:
    """'last_7_days' | 'last_30_days' | 'YYYY-MM-DD..YYYY-MM-DD' → (from, to) epoch."""
    if not expr:
        return None, None
    expr = expr.strip().lower()
    now = time.time()
    if expr == "last_7_days":
        return now - 7 * 86400, None
    if expr == "last_30_days":
        return now - 30 * 86400, None
    if ".." in expr:
        a, b = expr.split("..", 1)
        from datetime import datetime, timezone
        def _p(s):
            try:
                return datetime.strptime(s.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
            except Exception:
                return None
        return _p(a), _p(b)
    return None, None


class EmailStore:
    """SQLite + FTS5 wrapper. One instance per process."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._write_lock = asyncio.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        logger.info(f"EmailStore initialized at {self.db_path}")

    # ── schema migrations ──────────────────────────────────────────────────
    #
    # PRAGMA user_version is a 32-bit int the application owns. We use it as
    # a monotonic schema version so old DBs auto-upgrade in-place on next
    # boot. Each migration is idempotent (CREATE IF NOT EXISTS / ALTER guards)
    # so re-running a migration is safe.

    _SCHEMA_VERSION = 2

    def _migrate(self) -> None:
        cur_version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if cur_version >= self._SCHEMA_VERSION:
            return
        try:
            if cur_version < 2:
                # v2: entity-resolution tables
                self._conn.executescript(_ENTITIES_SCHEMA)
                logger.info("EmailStore migrated to schema v2 (entities)")
            self._conn.execute(f"PRAGMA user_version = {self._SCHEMA_VERSION}")
        except Exception:
            logger.exception("EmailStore migration failed; leaving schema at v%d", cur_version)

    # ── mailbox CRUD ─────────────────────────────────────────────────────────

    def upsert_mailbox(self, mb: dict) -> None:
        """Insert or update a mailbox row by id."""
        self._conn.execute(
            """
            INSERT INTO mailboxes (id, account_email, display_name, status, discovered_at)
            VALUES (:id, :account_email, :display_name, :status, :discovered_at)
            ON CONFLICT(id) DO UPDATE SET
                account_email = excluded.account_email,
                display_name  = excluded.display_name,
                status        = excluded.status
            """,
            {
                "id":            mb["id"],
                "account_email": mb["account_email"],
                "display_name":  mb.get("display_name"),
                "status":        mb.get("status", "active"),
                "discovered_at": mb.get("discovered_at", time.time()),
            },
        )
        # Ensure a sync_state row exists
        self._conn.execute(
            "INSERT OR IGNORE INTO sync_state (mailbox_id) VALUES (?)",
            (mb["id"],),
        )

    def list_mailboxes(self, active_only: bool = True) -> list[dict]:
        rows = self._conn.execute(
            f"SELECT * FROM mailboxes{' WHERE status = \"active\"' if active_only else ''} ORDER BY account_email"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            # Attach message_count + sync info for the tool output
            d["message_count"] = self._conn.execute(
                "SELECT COUNT(*) FROM emails WHERE mailbox_id = ?", (r["id"],)
            ).fetchone()[0]
            ss = self._conn.execute(
                "SELECT last_sync_at, initial_synced, backfill_done, last_error FROM sync_state WHERE mailbox_id = ?",
                (r["id"],),
            ).fetchone()
            if ss:
                d.update(dict(ss))
            out.append(d)
        return out

    def mailbox_count(self, status: Optional[str] = None) -> int:
        if status:
            return self._conn.execute(
                "SELECT COUNT(*) FROM mailboxes WHERE status = ?", (status,)
            ).fetchone()[0]
        return self._conn.execute("SELECT COUNT(*) FROM mailboxes").fetchone()[0]

    # ── email write path ─────────────────────────────────────────────────────

    async def upsert_emails(self, rows: list[dict]) -> int:
        """
        Insert-or-replace a batch of emails. Returns count inserted/updated.

        Caller is responsible for passing fully-mapped row dicts; see
        app.sources.email.outlook.mapper.graph_to_row.
        """
        if not rows:
            return 0
        async with self._write_lock:
            now = time.time()
            for r in rows:
                r.setdefault("ingested_at", now)
                r.setdefault("provider", "outlook")
            self._conn.executemany(
                """
                INSERT INTO emails (
                    mailbox_id, account_email, provider, provider_msg_id,
                    internet_msg_id, conversation_id, subject, from_name, from_email,
                    to_emails, cc_emails, bcc_emails, body_text, body_html_hash,
                    has_attachments, attachment_names, folder, is_read, importance,
                    sent_at, received_at, ingested_at
                ) VALUES (
                    :mailbox_id, :account_email, :provider, :provider_msg_id,
                    :internet_msg_id, :conversation_id, :subject, :from_name, :from_email,
                    :to_emails, :cc_emails, :bcc_emails, :body_text, :body_html_hash,
                    :has_attachments, :attachment_names, :folder, :is_read, :importance,
                    :sent_at, :received_at, :ingested_at
                )
                ON CONFLICT(mailbox_id, provider_msg_id) DO UPDATE SET
                    subject          = excluded.subject,
                    body_text        = excluded.body_text,
                    body_html_hash   = excluded.body_html_hash,
                    has_attachments  = excluded.has_attachments,
                    attachment_names = excluded.attachment_names,
                    folder           = excluded.folder,
                    is_read          = excluded.is_read,
                    importance       = excluded.importance,
                    received_at      = excluded.received_at
                """,
                rows,
            )
            return len(rows)

    async def delete_emails(self, mailbox_id: str, provider_msg_ids: list[str]) -> int:
        if not provider_msg_ids:
            return 0
        async with self._write_lock:
            placeholders = ",".join("?" * len(provider_msg_ids))
            cur = self._conn.execute(
                f"DELETE FROM emails WHERE mailbox_id = ? AND provider_msg_id IN ({placeholders})",
                [mailbox_id, *provider_msg_ids],
            )
            return cur.rowcount or 0

    # ── sync_state ──────────────────────────────────────────────────────────

    def get_sync_state(self, mailbox_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM sync_state WHERE mailbox_id = ?", (mailbox_id,)
        ).fetchone()
        return dict(row) if row else {}

    def update_sync_state(self, mailbox_id: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [mailbox_id]
        self._conn.execute(
            f"UPDATE sync_state SET {cols} WHERE mailbox_id = ?", values
        )

    # ── search / fetch ──────────────────────────────────────────────────────

    # Half-life for time decay (seconds). 30 days = a month-old email gets
    # half the rank weight of a brand-new one. 90-day-old gets 1/8. The decay
    # only nudges ordering — it never excludes a result, so users searching
    # for "the contract from 2023" still find it.
    _TIME_DECAY_HALF_LIFE = 30 * 86400.0

    def search(
        self,
        keywords: list[str],
        *,
        mailbox: Optional[str] = None,
        sender: Optional[str] = None,
        recipient: Optional[str] = None,
        date_range: Optional[str] = None,
        folder: Optional[str] = None,
        has_attachments: Optional[bool] = None,
        limit: int = 10,
        group_by_conversation: bool = True,
    ) -> list[dict]:
        """
        Two-phase ranked search:

          1. **Candidate pool** — pull the top ~5×limit BM25 hits matching
             all filters (mailbox, sender, date_range, etc).
          2. **Re-rank in Python** — apply a time-decay multiplier so recent
             messages outrank old ones with similar BM25, optionally collapse
             results by `conversation_id` so one thread = one hit, and trim
             to `limit`.

        Why re-rank in Python: SQLite FTS5's `bm25()` returns a negative score
        whose absolute scale varies by index size — composing it with a decay
        factor in pure SQL is brittle. Doing it in Python is a few µs and
        gives us full control.

        `group_by_conversation=True` is the new default: the result is one
        entry per thread with the best matching message, plus
        `thread_message_count` and `thread_last_received` so the agent can
        decide whether to call `get_email_thread` for the full conversation.
        """
        fts_query = _build_fts_query(keywords)
        date_from, date_to = _parse_date_range(date_range)
        sender_like = f"%{sender}%" if sender else None
        recipient_like = f"%{recipient}%" if recipient else None

        # Pull a wider candidate pool than `limit` so re-ranking has room to
        # surface items BM25 alone wouldn't have put in the top-N.
        pool_size = max(int(limit) * 5, 25)

        sql = """
            SELECT e.id, e.mailbox_id, e.account_email, e.subject,
                   e.from_name, e.from_email, e.to_emails, e.sent_at,
                   e.received_at, e.has_attachments, e.attachment_names, e.folder,
                   e.conversation_id,
                   snippet(emails_fts, 4, '<mark>', '</mark>', '…', 12) AS preview,
                   bm25(emails_fts) AS score
            FROM   emails_fts
            JOIN   emails e ON e.id = emails_fts.rowid
            WHERE  emails_fts MATCH ?
              AND  (? IS NULL OR e.account_email = ?)
              AND  (? IS NULL OR e.from_email LIKE ? OR e.from_name LIKE ?)
              AND  (? IS NULL OR e.to_emails   LIKE ?)
              AND  (? IS NULL OR e.sent_at >= ?)
              AND  (? IS NULL OR e.sent_at <  ?)
              AND  (? IS NULL OR e.folder = ?)
              AND  (? IS NULL OR e.has_attachments = ?)
            ORDER BY bm25(emails_fts)
            LIMIT ?
        """
        has_att_int = None if has_attachments is None else (1 if has_attachments else 0)
        params = [
            fts_query,
            mailbox, mailbox,
            sender_like, sender_like, sender_like,
            recipient_like, recipient_like,
            date_from, date_from,
            date_to, date_to,
            folder, folder,
            has_att_int, has_att_int,
            int(pool_size),
        ]
        rows = self._conn.execute(sql, params).fetchall()
        if not rows:
            return []

        # ── Phase 2: re-rank with time decay ───────────────────────────────
        # FTS5 bm25 is negative (more negative = better). Translate to a
        # positive relevance, then divide by the time-decay factor so newer
        # rows boost. Operates on a small in-memory pool — cheap.
        now = time.time()
        scored: list[tuple[float, dict]] = []
        for r in rows:
            d = self._row_to_dict(r)
            bm = float(d.get("score") or 0.0)         # negative number
            relevance = -bm                            # → positive ("higher is better")
            ts = float(d.get("received_at") or d.get("sent_at") or 0.0)
            age = max(0.0, now - ts) if ts else self._TIME_DECAY_HALF_LIFE * 4
            # exp decay: half_life seconds → ×0.5 weight
            import math
            decay_factor = math.exp(-(age / self._TIME_DECAY_HALF_LIFE) * math.log(2))
            final = relevance * (0.4 + 0.6 * decay_factor)   # never less than 40% of raw BM25
            d["_final_score"] = final
            scored.append((final, d))

        scored.sort(key=lambda x: x[0], reverse=True)

        if not group_by_conversation:
            out = [d for _, d in scored[:int(limit)]]
            for d in out:
                d.pop("_final_score", None)
            return out

        # Collapse by conversation_id → keep the best-scoring message per
        # thread. Threads without an id (rare; fallback to message id) keep
        # their own row. Carry thread metadata so the UI/agent can show
        # "3 messages in this thread, last 2026-04-15" without another query.
        per_thread: dict[str, dict] = {}
        thread_keys: list[str] = []
        for _, d in scored:
            key = d.get("conversation_id") or f"_msg:{d.get('id')}"
            if key not in per_thread:
                per_thread[key] = d
                thread_keys.append(key)

        # Pull thread sizes/recency in ONE query, not N. Bound to the
        # conversation IDs we actually care about.
        ids = [k for k in thread_keys if not k.startswith("_msg:")]
        meta: dict[str, dict] = {}
        if ids:
            placeholders = ",".join("?" * len(ids))
            meta_rows = self._conn.execute(
                f"""
                SELECT conversation_id,
                       COUNT(*)         AS thread_message_count,
                       MAX(received_at) AS thread_last_received
                  FROM emails
                 WHERE conversation_id IN ({placeholders})
                 GROUP BY conversation_id
                """,
                ids,
            ).fetchall()
            meta = {r["conversation_id"]: dict(r) for r in meta_rows}

        out: list[dict] = []
        for key in thread_keys:
            d = per_thread[key]
            d.pop("_final_score", None)
            if not key.startswith("_msg:"):
                m = meta.get(key, {})
                d["thread_message_count"] = int(m.get("thread_message_count") or 1)
                d["thread_last_received"] = m.get("thread_last_received") or d.get("received_at")
            else:
                d["thread_message_count"] = 1
                d["thread_last_received"] = d.get("received_at")
            out.append(d)
            if len(out) >= int(limit):
                break
        return out

    def get_email(self, email_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM emails WHERE id = ?", (int(email_id),)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def recent_emails(
        self,
        *,
        limit: int = 20,
        mailbox_id: Optional[str] = None,
    ) -> list[dict]:
        """
        Return the most recently received messages across all mailboxes
        (or one mailbox), newest first. Used by the management UI's
        activity feed — distinct from agent search.

        Returns lightweight rows: id, mailbox_id, account_email, from_name,
        from_email, subject, received_at, has_attachments, folder, conversation_id,
        and a 240-char preview of body_text (first lines, whitespace-collapsed).
        """
        limit = max(1, min(int(limit), 200))
        if mailbox_id:
            rows = self._conn.execute(
                """
                SELECT id, mailbox_id, account_email, from_name, from_email,
                       subject, received_at, has_attachments, folder,
                       conversation_id, substr(body_text, 1, 360) AS body_snip
                FROM   emails
                WHERE  mailbox_id = ?
                ORDER BY received_at DESC
                LIMIT  ?
                """,
                (mailbox_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT id, mailbox_id, account_email, from_name, from_email,
                       subject, received_at, has_attachments, folder,
                       conversation_id, substr(body_text, 1, 360) AS body_snip
                FROM   emails
                ORDER BY received_at DESC
                LIMIT  ?
                """,
                (limit,),
            ).fetchall()

        out: list[dict] = []
        for r in rows:
            d = dict(r)
            snip = (d.pop("body_snip", "") or "").strip()
            # Collapse whitespace and trim to ~200 chars for a one-line preview
            if snip:
                snip = " ".join(snip.split())
                if len(snip) > 220:
                    snip = snip[:217] + "…"
            d["preview"] = snip
            out.append(d)
        return out

    def set_mailbox_status(self, mailbox_id: str, status: str) -> None:
        """Mark a mailbox active/disabled. Does not touch its emails."""
        self._conn.execute(
            "UPDATE mailboxes SET status = ? WHERE id = ?",
            (status, mailbox_id),
        )

    def delete_mailbox(self, mailbox_id: str) -> int:
        """
        Hard-delete a mailbox row + all its emails + sync_state. Used when
        the operator removes a mailbox from the IMAP config and asks for
        its cache to be cleaned up.
        """
        self._conn.execute("DELETE FROM emails      WHERE mailbox_id = ?", (mailbox_id,))
        self._conn.execute("DELETE FROM sync_state  WHERE mailbox_id = ?", (mailbox_id,))
        cur = self._conn.execute("DELETE FROM mailboxes WHERE id = ?", (mailbox_id,))
        # Rebuild the FTS index because we removed rows underneath it
        self._conn.execute("INSERT INTO emails_fts(emails_fts) VALUES('rebuild')")
        return cur.rowcount or 0

    def get_thread(self, conversation_id: str, mailbox_id: Optional[str] = None) -> list[dict]:
        if mailbox_id:
            rows = self._conn.execute(
                "SELECT * FROM emails WHERE conversation_id = ? AND mailbox_id = ? ORDER BY sent_at ASC",
                (conversation_id, mailbox_id),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM emails WHERE conversation_id = ? ORDER BY sent_at ASC",
                (conversation_id,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ── entity resolution ──────────────────────────────────────────────────
    #
    # The entities table is a thin canonical-name layer on top of email
    # addresses. The agent uses it to answer questions like "did Acme email us"
    # without having to guess address variants. Storage is structured + indexed,
    # so lookups are sub-ms even at 100k+ entities. The table is decoupled from
    # any specific provider — Outlook / IMAP / a future Gmail connector all
    # feed it through the same upsert path.

    @staticmethod
    def _norm_email(addr: Optional[str]) -> str:
        """Lowercase + strip + drop angle brackets so equality compares cleanly."""
        if not addr:
            return ""
        s = str(addr).strip().strip("<>").strip()
        return s.lower()

    @staticmethod
    def _norm_name(name: Optional[str]) -> str:
        if not name:
            return ""
        return " ".join(str(name).split()).strip()

    def upsert_entity(
        self,
        *,
        kind: str = "unknown",
        display_name: Optional[str] = None,
        emails: Optional[Iterable[str]] = None,
        company: Optional[str] = None,
        notes: Optional[str] = None,
        source: str = "manual",
        source_pk: Optional[str] = None,
        confidence: float = 1.0,
    ) -> Optional[int]:
        """
        Idempotently insert-or-update an entity, plus associated addresses.

        Returns the entity_id, or None if no usable email was provided. Safe to
        call repeatedly — upserts on (kind, canonical_email). The first email
        in `emails` becomes canonical; later calls can add more aliases without
        touching the canonical address. `confidence` is monotonically maxed:
        confirming an auto-discovered entity bumps its confidence but a
        re-discovery cannot demote a confirmed one.
        """
        addrs: list[str] = []
        for a in (emails or []):
            n = self._norm_email(a)
            if n and n not in addrs:
                addrs.append(n)
        if not addrs:
            return None

        canonical = addrs[0]
        kind = (kind or "unknown").strip().lower()
        now  = time.time()
        name = self._norm_name(display_name)

        cur = self._conn.execute(
            "SELECT entity_id, confidence FROM entities WHERE kind = ? AND canonical_email = ?",
            (kind, canonical),
        ).fetchone()

        if cur:
            entity_id  = int(cur["entity_id"])
            new_conf   = max(float(cur["confidence"] or 0.0), float(confidence))
            self._conn.execute(
                """
                UPDATE entities
                   SET display_name = COALESCE(NULLIF(?, ''), display_name),
                       company      = COALESCE(NULLIF(?, ''), company),
                       notes        = COALESCE(NULLIF(?, ''), notes),
                       source_pk    = COALESCE(NULLIF(?, ''), source_pk),
                       confidence   = ?,
                       last_seen    = ?
                 WHERE entity_id = ?
                """,
                (name, company or "", notes or "", source_pk or "", new_conf, now, entity_id),
            )
        else:
            row = self._conn.execute(
                """
                INSERT INTO entities
                    (kind, display_name, canonical_email, company, notes,
                     source, source_pk, confidence, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING entity_id
                """,
                (kind, name or None, canonical, company or None, notes or None,
                 source, source_pk, confidence, now, now),
            ).fetchone()
            entity_id = int(row["entity_id"])

        # Attach all addresses, marking the first as canonical. Idempotent on
        # (entity_id, email_address); subsequent re-discoveries bump seen_count.
        for i, addr in enumerate(addrs):
            self._conn.execute(
                """
                INSERT INTO entity_emails (entity_id, email_address, is_canonical, seen_count, last_seen)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(entity_id, email_address) DO UPDATE SET
                    seen_count = entity_emails.seen_count + 1,
                    last_seen  = excluded.last_seen
                """,
                (entity_id, addr, 1 if i == 0 else 0, now),
            )
        return entity_id

    def find_entity_by_email(self, email: str) -> Optional[dict]:
        """Exact-match lookup by any aliased address. Returns the entity row
        with all known addresses attached, or None."""
        addr = self._norm_email(email)
        if not addr:
            return None
        row = self._conn.execute(
            """
            SELECT e.*
              FROM entities e
              JOIN entity_emails ea ON ea.entity_id = e.entity_id
             WHERE ea.email_address = ?
             LIMIT 1
            """,
            (addr,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["emails"] = self._entity_addresses(d["entity_id"])
        return d

    def find_entities_by_name(self, query: str, *, limit: int = 10) -> list[dict]:
        """
        Substring + token-overlap lookup on display_name and company. Case-
        insensitive. Returns up to `limit` rows, ordered by exact-prefix first,
        then by recency. Cheap (indexed prefix scan + small linear filter).
        """
        q = self._norm_name(query)
        if not q:
            return []
        like = f"%{q.lower()}%"
        rows = self._conn.execute(
            """
            SELECT *
              FROM entities
             WHERE LOWER(display_name) LIKE ?
                OR LOWER(company)      LIKE ?
             ORDER BY
                CASE
                  WHEN LOWER(display_name) = ?      THEN 0
                  WHEN LOWER(display_name) LIKE ?   THEN 1
                  ELSE 2
                END,
                last_seen DESC
             LIMIT ?
            """,
            (like, like, q.lower(), f"{q.lower()}%", int(limit)),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["emails"] = self._entity_addresses(d["entity_id"])
            out.append(d)
        return out

    def get_entity(self, entity_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM entities WHERE entity_id = ?", (int(entity_id),)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["emails"] = self._entity_addresses(d["entity_id"])
        return d

    def list_entities(
        self,
        *,
        kind: Optional[str] = None,
        min_confidence: float = 0.0,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Paginated list, newest-seen first. For the management UI."""
        sql = "SELECT * FROM entities WHERE confidence >= ?"
        params: list[Any] = [float(min_confidence)]
        if kind:
            sql += " AND kind = ?"
            params.append(kind.lower())
        sql += " ORDER BY last_seen DESC LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
        rows = self._conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["emails"] = self._entity_addresses(d["entity_id"])
            out.append(d)
        return out

    def count_entities(self, *, kind: Optional[str] = None, min_confidence: float = 0.0) -> int:
        sql = "SELECT COUNT(*) FROM entities WHERE confidence >= ?"
        params: list[Any] = [float(min_confidence)]
        if kind:
            sql += " AND kind = ?"
            params.append(kind.lower())
        return int(self._conn.execute(sql, params).fetchone()[0])

    def update_entity(
        self,
        entity_id: int,
        *,
        kind: Optional[str] = None,
        display_name: Optional[str] = None,
        company: Optional[str] = None,
        notes: Optional[str] = None,
        source_pk: Optional[str] = None,
        confidence: Optional[float] = None,
    ) -> bool:
        """Partial update — pass only fields you want changed. Returns True on hit."""
        sets: list[str] = []
        params: list[Any] = []
        if kind is not None:
            sets.append("kind = ?");          params.append(kind.strip().lower())
        if display_name is not None:
            sets.append("display_name = ?");  params.append(self._norm_name(display_name) or None)
        if company is not None:
            sets.append("company = ?");       params.append(company.strip() or None)
        if notes is not None:
            sets.append("notes = ?");         params.append(notes.strip() or None)
        if source_pk is not None:
            sets.append("source_pk = ?");     params.append(source_pk.strip() or None)
        if confidence is not None:
            sets.append("confidence = ?");    params.append(float(confidence))
        if not sets:
            return False
        sets.append("last_seen = ?")
        params.append(time.time())
        params.append(int(entity_id))
        cur = self._conn.execute(
            f"UPDATE entities SET {', '.join(sets)} WHERE entity_id = ?", params,
        )
        return (cur.rowcount or 0) > 0

    def delete_entity(self, entity_id: int) -> bool:
        cur = self._conn.execute("DELETE FROM entities WHERE entity_id = ?", (int(entity_id),))
        return (cur.rowcount or 0) > 0

    def _entity_addresses(self, entity_id: int) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT email_address, is_canonical, seen_count, last_seen
              FROM entity_emails
             WHERE entity_id = ?
             ORDER BY is_canonical DESC, seen_count DESC, last_seen DESC
            """,
            (int(entity_id),),
        ).fetchall()
        return [dict(r) for r in rows]

    async def auto_discover_entities_from_recent(self, *, lookback_seconds: float = 86400) -> int:
        """
        Scan recent emails and upsert their senders as low-confidence entities.

        Idempotent — re-running on the same window just bumps `seen_count` and
        `last_seen` on existing rows. Cheap: O(N) over recent rows where N is
        small (one batch of newly-ingested emails). Called from the IMAP
        ingestion loop after every successful sync.

        Returns the number of (entity, address) upserts performed.
        """
        cutoff = time.time() - max(60.0, float(lookback_seconds))
        rows = self._conn.execute(
            """
            SELECT DISTINCT from_email, from_name
              FROM emails
             WHERE from_email IS NOT NULL
               AND from_email <> ''
               AND received_at >= ?
            """,
            (cutoff,),
        ).fetchall()
        if not rows:
            return 0

        n = 0
        async with self._write_lock:
            for r in rows:
                addr = self._norm_email(r["from_email"])
                if not addr:
                    continue
                # Skip our own mailboxes — they're "us," not contacts.
                if self._is_own_mailbox(addr):
                    continue
                self.upsert_entity(
                    kind="unknown",
                    display_name=r["from_name"],
                    emails=[addr],
                    source="email",
                    confidence=0.5,
                )
                n += 1
        return n

    def _is_own_mailbox(self, addr: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM mailboxes WHERE LOWER(account_email) = ? LIMIT 1",
            (addr,),
        ).fetchone()
        return row is not None

    # ── maintenance ─────────────────────────────────────────────────────────

    def purge_all(self) -> None:
        """
        Delete every mailbox, email, sync_state, and entity row. Used on disconnect.

        Callers are expected to have already stopped the ingestion tasks;
        no async lock is taken here — this runs synchronously from the
        setup route handler while ingestion is idle.
        """
        self._conn.execute("DELETE FROM emails")
        self._conn.execute("DELETE FROM sync_state")
        self._conn.execute("DELETE FROM mailboxes")
        self._conn.execute("DELETE FROM entity_emails")
        self._conn.execute("DELETE FROM entities")
        self._conn.execute("INSERT INTO emails_fts(emails_fts) VALUES('rebuild')")
        self._conn.execute("VACUUM")

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # ── helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row) -> dict:
        if row is None:
            return {}
        d = dict(row)
        for f in ("to_emails", "cc_emails", "bcc_emails", "attachment_names"):
            v = d.get(f)
            if isinstance(v, str) and v:
                try:
                    d[f] = json.loads(v)
                except Exception:
                    pass
        return d
