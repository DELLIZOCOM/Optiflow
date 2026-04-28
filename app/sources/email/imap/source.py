"""
IMAPSource — EmailSource implementation for any RFC 3501 IMAP server.

Used for GoDaddy Workspace Email, Zoho, FastMail, Hostinger / cPanel hosts,
on-prem Postfix/Dovecot, and anything else that speaks plain IMAP.

Not used for Microsoft 365 — that goes through OutlookSource (Microsoft
Graph). On M365 tenants Microsoft disabled Basic Auth IMAP in Sept 2024.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.sources.email.store import EmailStore

from .client import IMAPClient, IMAPServer, IMAPAuthError
from .ingest import IMAPCoordinator, IMAPMailboxConfig  # re-exported via type hint

logger = logging.getLogger(__name__)


class IMAPSource:
    """One instance per connected IMAP "tenant" (a host + a list of mailboxes)."""

    def __init__(
        self,
        *,
        name: str,
        tenant_display_name: str,
        server: IMAPServer,
        mailboxes: list[IMAPMailboxConfig],
        store: EmailStore,
        provider_label: str = "imap",
        backfill_days: int = 365,
    ):
        self._name = name
        self._tenant_display_name = tenant_display_name
        self._server = server
        self._mailboxes = list(mailboxes)
        self._store = store
        self._provider_label = provider_label  # "godaddy" | "imap" | etc — for display only
        self._backfill_days = backfill_days
        self._ingest: Optional[IMAPCoordinator] = None

    # ── DataSource protocol ──────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self._name

    @property
    def source_type(self) -> str:
        return "imap"

    @property
    def description(self) -> str:
        total = self._store.mailbox_count(status="active")
        return (
            f"Company email for {self._tenant_display_name} (IMAP). "
            f"{total} mailboxes indexed. Read-only."
        )

    def get_table_index(self) -> str:
        return "- emails  (search via search_emails / get_email / get_email_thread; list_mailboxes shows accounts)"

    def get_compact_index(self) -> str:
        return self.get_table_index()

    def get_table_detail(self, table_name: str) -> Optional[str]:
        if table_name.lower() != "emails":
            return None
        return (
            "Indexed IMAP email messages.\n"
            "Access via email tools only (NOT execute_sql):\n"
            "  - search_emails(keywords, mailbox?, sender?, recipient?, date_range?, folder?, has_attachments?, limit?)\n"
            "  - get_email(email_id)\n"
            "  - get_email_thread(conversation_id)\n"
            "  - list_mailboxes()\n"
        )

    def get_database_name(self) -> str:
        return self._tenant_display_name

    def get_db_type(self) -> str:
        return "imap"

    def get_system_prompt_section(self) -> str:
        n = self._store.mailbox_count(status="active")
        return (
            f"## Email source: {self._tenant_display_name} (IMAP, {n} mailboxes)\n"
            "You can read company email via search_emails, get_email, get_email_thread, list_mailboxes.\n\n"
            "When searching email:\n"
            "  - Generate 2-6 keyword variants, not just the literal user phrase.\n"
            "  - Use mailbox= to scope when the user names a specific person or role inbox.\n"
            "  - Use sender= when the user names an external party.\n"
            "  - Translate temporal words to date_range: last_7_days, last_30_days, YYYY-MM-DD..YYYY-MM-DD.\n"
            "  - Quote invoice numbers, PO numbers, and IDs exactly. Do NOT normalize.\n\n"
            "Note: IMAP threading uses Message-ID headers; conversation grouping may be looser than Outlook.\n"
            "Do NOT fabricate email content. If search returns nothing, say so plainly and suggest alternative\n"
            "searches. Summarize findings with concrete details (sender, subject, date).\n"
        )

    async def execute_query(self, sql: str) -> list[dict]:
        raise NotImplementedError(
            "IMAPSource does not support execute_sql. Use the email tools "
            "(search_emails, get_email, get_email_thread, list_mailboxes)."
        )

    # ── EmailSource extensions ───────────────────────────────────────────────

    @property
    def provider(self) -> str:
        return self._provider_label

    @property
    def tenant_display_name(self) -> str:
        return self._tenant_display_name

    @property
    def store(self) -> EmailStore:
        return self._store

    async def start(self) -> None:
        if self._ingest is not None:
            return
        self._ingest = IMAPCoordinator(
            self._store,
            server=self._server,
            mailboxes=self._mailboxes,
            backfill_days=self._backfill_days,
        )
        await self._ingest.start()
        logger.info("[IMAP] ingestion started for %s", self._tenant_display_name)

    async def stop(self) -> None:
        if self._ingest is not None:
            await self._ingest.stop()
            self._ingest = None
        logger.info("[IMAP] ingestion stopped for %s", self._tenant_display_name)

    # ── runtime mutation (for the management UI) ─────────────────────────────

    def sync_now(self, mailbox_id: Optional[str] = None) -> int:
        """Trigger an immediate poll for one mailbox or all of them.

        Returns the number of mailboxes that were nudged. Non-blocking.
        """
        if self._ingest is None:
            return 0
        return self._ingest.sync_now(mailbox_id)

    async def add_mailbox(self, mailbox: "IMAPMailboxConfig") -> str:
        """Add a mailbox at runtime; returns its mailbox_id."""
        if self._ingest is None:
            raise RuntimeError("IMAP source is not running")
        # Keep the configured list in sync with the live coordinator so a
        # subsequent restart picks it up.
        self._mailboxes.append(mailbox)
        return await self._ingest.add_mailbox(mailbox)

    async def remove_mailbox(self, account_email: str, *, purge_cache: bool = False) -> bool:
        """Stop polling a mailbox and mark it disabled (or hard-delete)."""
        if self._ingest is None:
            return False
        ok = await self._ingest.remove_mailbox(account_email, purge_cache=purge_cache)
        if ok:
            self._mailboxes = [
                m for m in self._mailboxes
                if (m.account_email or "").lower() != account_email.lower()
            ]
        return ok

    async def test_credentials(self) -> tuple[bool, Optional[str]]:
        """
        Verify that *all* configured mailboxes can log in and SELECT INBOX.
        Returns (False, "<email>: <error>") on the first failure so the UI
        can show the user exactly which one is misconfigured.
        """
        if not self._mailboxes:
            return False, "no mailboxes configured"
        for mb in self._mailboxes:
            client = IMAPClient(self._server, mb.account_email, mb.password)
            try:
                await client.connect()
                await client.select_folder(mb.folder or "INBOX")
            except IMAPAuthError as e:
                return False, f"{mb.account_email}: {e}"
            except Exception as e:
                return False, f"{mb.account_email}: {type(e).__name__}: {e}"
            finally:
                await client.close()
        return True, None
