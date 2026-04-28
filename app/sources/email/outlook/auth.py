"""
Microsoft Graph authentication — app-only (client credentials) flow.

One Azure AD app per tenant. The admin grants application-level
`Mail.Read` and `User.Read.All` with admin consent. OptiFlow exchanges
client_id + client_secret for an app-only access token via MSAL.

Tokens:
  - ~1h lifetime, no refresh token (re-acquire via client credentials)
  - cached in-process; we ask MSAL for a fresh one ~5 minutes before expiry
  - never persisted to disk
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]


@dataclass
class OutlookCredentials:
    """Admin-provided Azure AD app credentials. client_secret is plaintext here."""
    tenant_id:     str
    client_id:     str
    client_secret: str


class GraphAuthError(Exception):
    """Raised when MSAL can't get a token (bad creds, revoked consent, ...)."""


class OutlookTokenProvider:
    """
    Thin cache on top of MSAL. Call `get_token()` anywhere a Graph call
    needs an Authorization header.
    """

    def __init__(self, creds: OutlookCredentials):
        self._creds = creds
        self._app = None                  # msal.ConfidentialClientApplication
        self._token: Optional[str] = None
        self._expires_at: float = 0.0

    def _ensure_app(self):
        if self._app is not None:
            return
        try:
            import msal
        except ImportError as e:
            raise GraphAuthError(
                "msal is not installed. Add 'msal' to requirements.txt."
            ) from e
        self._app = msal.ConfidentialClientApplication(
            client_id=self._creds.client_id,
            client_credential=self._creds.client_secret,
            authority=f"https://login.microsoftonline.com/{self._creds.tenant_id}",
        )

    def get_token(self) -> str:
        """Return a live access token, refreshing if the cached one is near expiry."""
        now = time.time()
        if self._token and (self._expires_at - now) > 60:
            return self._token

        self._ensure_app()
        # acquire_token_for_client hits the cache internally; we add our
        # own layer because MSAL's in-memory cache doesn't expose expiry
        # as convenient as we'd like.
        result = self._app.acquire_token_for_client(scopes=_GRAPH_SCOPE)
        if "access_token" not in result:
            err = result.get("error_description") or result.get("error") or "unknown error"
            logger.warning("MSAL token acquisition failed: %s", err)
            raise GraphAuthError(f"Token acquisition failed: {err}")

        self._token = result["access_token"]
        self._expires_at = now + int(result.get("expires_in", 3600))
        logger.debug("Outlook token refreshed, expires in %ss", int(self._expires_at - now))
        return self._token

    def invalidate(self) -> None:
        """Drop cached token — next call re-acquires."""
        self._token = None
        self._expires_at = 0.0
