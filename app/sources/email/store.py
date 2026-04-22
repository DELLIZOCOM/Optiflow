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
        logger.info(f"EmailStore initialized at {self.db_path}")

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
    ) -> list[dict]:
        fts_query = _build_fts_query(keywords)
        date_from, date_to = _parse_date_range(date_range)
        sender_like = f"%{sender}%" if sender else None
        recipient_like = f"%{recipient}%" if recipient else None

        sql = """
            SELECT e.id, e.mailbox_id, e.account_email, e.subject,
                   e.from_name, e.from_email, e.to_emails, e.sent_at,
                   e.has_attachments, e.attachment_names, e.folder,
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
            int(limit),
        ]
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_email(self, email_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM emails WHERE id = ?", (int(email_id),)
        ).fetchone()
        return self._row_to_dict(row) if row else None

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

    # ── maintenance ─────────────────────────────────────────────────────────

    def purge_all(self) -> None:
        """
        Delete every mailbox, email, and sync_state row. Used on disconnect.

        Callers are expected to have already stopped the ingestion tasks;
        no async lock is taken here — this runs synchronously from the
        setup route handler while ingestion is idle.
        """
        self._conn.execute("DELETE FROM emails")
        self._conn.execute("DELETE FROM sync_state")
        self._conn.execute("DELETE FROM mailboxes")
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
