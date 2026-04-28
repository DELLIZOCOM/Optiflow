"""
EmailSource protocol.

An EmailSource is a DataSource specialized for a mail provider
(Outlook, Gmail, ...). It exposes mailbox discovery + a uniform
search-backed view of indexed messages. The agent never talks to
the provider directly — it goes through EmailStore via the email
tools (app/tools/email.py).
"""

from typing import Optional, Protocol, runtime_checkable

from app.sources.base import DataSource


@runtime_checkable
class EmailSource(DataSource, Protocol):
    """
    A connected email source (one tenant, many mailboxes).

    In addition to the DataSource protocol, email sources expose:
      - provider identifier ('outlook' | 'gmail')
      - display name for the tenant (e.g. 'Contoso Corp')
      - access to the underlying EmailStore (for tools)
      - lifecycle methods for background ingestion
    """

    @property
    def provider(self) -> str:
        """'outlook' | 'gmail'."""
        ...

    @property
    def tenant_display_name(self) -> str:
        """Human-readable label for the tenant, shown in UI and prompts."""
        ...

    @property
    def store(self):
        """The EmailStore backing this source."""
        ...

    async def start(self) -> None:
        """Begin background ingestion (discovery + delta + backfill loops)."""
        ...

    async def stop(self) -> None:
        """Cancel all background tasks and close resources."""
        ...

    async def test_credentials(self) -> tuple[bool, Optional[str]]:
        """
        Attempt to authenticate and make a trivial API call.
        Returns (ok, error_message). Called from the setup wizard.
        """
        ...
