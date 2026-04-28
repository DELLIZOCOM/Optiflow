"""
OutlookSource — DataSource implementation for Microsoft 365 via admin consent.

Exposes the standard DataSource surface (name, source_type, description,
table index, system-prompt section, execute_query no-op) plus the
EmailSource extensions (provider, tenant_display_name, store, start/stop).

The "table index" concept is borrowed from DB sources — for email we
report a single synthetic `emails` row so the agent sees email exists
as part of its available context.
"""

import logging
from typing import Optional

from app.sources.email.store import EmailStore

from .auth import OutlookCredentials, OutlookTokenProvider
from .graph import GraphClient
from .ingest import IngestCoordinator

logger = logging.getLogger(__name__)


class OutlookSource:
    """
    One instance per connected tenant. Registered into SourceRegistry.
    """

    def __init__(
        self,
        *,
        name: str,
        tenant_display_name: str,
        credentials: OutlookCredentials,
        store: EmailStore,
        backfill_days: int = 365,
    ):
        self._name = name
        self._tenant_display_name = tenant_display_name
        self._creds = credentials
        self._store = store
        self._backfill_days = backfill_days

        self._token_provider = OutlookTokenProvider(credentials)
        self._graph = GraphClient(self._token_provider)
        self._ingest: Optional[IngestCoordinator] = None

    # ── DataSource protocol ──────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self._name

    @property
    def source_type(self) -> str:
        return "outlook"

    @property
    def description(self) -> str:
        total = self._store.mailbox_count(status="active")
        return (
            f"Company email for {self._tenant_display_name} (Microsoft 365, admin-consented). "
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
            "Indexed Microsoft 365 email messages.\n"
            "Access via email tools only (NOT execute_sql):\n"
            "  - search_emails(keywords, mailbox?, sender?, recipient?, date_range?, folder?, has_attachments?, limit?)\n"
            "  - get_email(email_id)\n"
            "  - get_email_thread(conversation_id)\n"
            "  - list_mailboxes()\n"
        )

    def get_database_name(self) -> str:
        return self._tenant_display_name

    def get_db_type(self) -> str:
        return "outlook"

    def get_system_prompt_section(self) -> str:
        n = self._store.mailbox_count(status="active")
        return (
            f"## Email source: {self._tenant_display_name} (Outlook, admin-consented, {n} mailboxes)\n"
            "You can read company email via search_emails, get_email, get_email_thread, list_mailboxes.\n\n"
            "When searching email:\n"
            "  - Generate 2-6 keyword variants, not just the literal user phrase.\n"
            "  - Use mailbox= to scope when the user names a specific person or role inbox.\n"
            "  - Use sender= when the user names an external party.\n"
            "  - Translate temporal words to date_range: last_7_days, last_30_days, YYYY-MM-DD..YYYY-MM-DD.\n"
            "  - Quote invoice numbers, PO numbers, and IDs exactly. Do NOT normalize.\n\n"
            "Do NOT fabricate email content. If search returns nothing, say so plainly and suggest alternative\n"
            "searches. Summarize findings with concrete details (sender, subject, date).\n"
        )

    async def execute_query(self, sql: str) -> list[dict]:
        raise NotImplementedError(
            "OutlookSource does not support execute_sql. Use the email tools "
            "(search_emails, get_email, get_email_thread, list_mailboxes)."
        )

    # ── EmailSource extensions ───────────────────────────────────────────────

    @property
    def provider(self) -> str:
        return "outlook"

    @property
    def tenant_display_name(self) -> str:
        return self._tenant_display_name

    @property
    def store(self) -> EmailStore:
        return self._store

    async def start(self) -> None:
        if self._ingest is not None:
            return
        self._ingest = IngestCoordinator(self._graph, self._store, backfill_days=self._backfill_days)
        await self._ingest.start()
        logger.info("[Outlook] ingestion started for %s", self._tenant_display_name)

    async def stop(self) -> None:
        if self._ingest is not None:
            await self._ingest.stop()
            self._ingest = None
        await self._graph.aclose()
        logger.info("[Outlook] ingestion stopped for %s", self._tenant_display_name)

    async def test_credentials(self) -> tuple[bool, Optional[str]]:
        """
        Verify the admin consent by acquiring a token and hitting /users?$top=1.
        Called from the setup wizard before persisting credentials.
        """
        try:
            # Force a fresh token acquisition
            self._token_provider.invalidate()
            _ = self._token_provider.get_token()
            resp = await self._graph.get("/users?$top=1&$select=id")
            if "value" not in resp:
                return False, "Graph response missing 'value' field"
            return True, None
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
