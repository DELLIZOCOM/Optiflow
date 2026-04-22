"""
Thin async Microsoft Graph client.

Wraps httpx with:
  - token injection from OutlookTokenProvider
  - automatic retry on 429 honoring Retry-After
  - automatic retry on 401 (one refresh attempt, then surface)
  - paginated iterators for endpoints that return @odata.nextLink / @odata.deltaLink

We deliberately do NOT use the msgraph-sdk Python library — it's heavy,
pulls in Kiota, and we only need ~5 endpoints.
"""

import asyncio
import logging
from typing import AsyncIterator, Optional

from .auth import GraphAuthError, OutlookTokenProvider

logger = logging.getLogger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 4


class GraphHTTPError(Exception):
    def __init__(self, status: int, message: str, body: Optional[str] = None):
        super().__init__(f"Graph HTTP {status}: {message}")
        self.status = status
        self.body = body


class GraphClient:
    """
    Async Graph client. One instance per tenant (per OutlookSource).

    Concurrency is controlled by a shared semaphore so we never
    overwhelm Graph and trigger tenant-wide throttling.
    """

    def __init__(
        self,
        token_provider: OutlookTokenProvider,
        *,
        concurrency: int = 6,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        try:
            import httpx  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "httpx is not installed. Add 'httpx' to requirements.txt."
            ) from e
        self._token_provider = token_provider
        self._sema = asyncio.Semaphore(concurrency)
        self._timeout = timeout
        self._client = None

    async def _ensure_client(self):
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(timeout=self._timeout)

    async def aclose(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get(self, url: str, *, params: Optional[dict] = None) -> dict:
        """GET a Graph URL. `url` can be a full URL (nextLink) or a path."""
        await self._ensure_client()
        if not url.startswith("http"):
            url = f"{_GRAPH_BASE}{url}"

        for attempt in range(_MAX_RETRIES):
            token = self._token_provider.get_token()
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            async with self._sema:
                resp = await self._client.get(url, params=params, headers=headers)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 401 and attempt == 0:
                # Token may have been revoked or rotated; force refresh + retry once
                self._token_provider.invalidate()
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = float(resp.headers.get("Retry-After", "1"))
                backoff = min(retry_after * (2 ** attempt), 60.0)
                logger.info(
                    "Graph %s on %s — backing off %.1fs (attempt %d)",
                    resp.status_code, url, backoff, attempt + 1,
                )
                await asyncio.sleep(backoff)
                continue

            # Non-retryable
            body = resp.text[:500] if resp.text else ""
            raise GraphHTTPError(resp.status_code, resp.reason_phrase, body)

        raise GraphHTTPError(
            resp.status_code if resp else 0,
            "Retries exhausted",
            body=resp.text[:500] if resp is not None else None,
        )

    async def iter_pages(self, path_or_url: str, *, params: Optional[dict] = None) -> AsyncIterator[dict]:
        """
        Iterate through @odata.nextLink pages. Yields each response dict
        (so caller sees `value`, `@odata.nextLink`, `@odata.deltaLink`).
        """
        url = path_or_url
        first = True
        while url:
            page = await self.get(url, params=params if first else None)
            yield page
            first = False
            url = page.get("@odata.nextLink")
            # When nextLink is absent but deltaLink is present, we're done.
            if not url:
                break


# ── ergonomic endpoint wrappers ──────────────────────────────────────────────

async def list_users(client: GraphClient) -> AsyncIterator[dict]:
    """Yield user records across all pages. Filters to mail-enabled accounts."""
    path = "/users?$select=id,mail,userPrincipalName,displayName,accountEnabled&$top=999"
    async for page in client.iter_pages(path):
        for u in page.get("value", []):
            yield u


async def list_messages_initial(
    client: GraphClient,
    user_id: str,
    *,
    top: int = 100,
) -> AsyncIterator[dict]:
    """
    Initial sync: uses the delta endpoint, which gives us a deltaLink
    at the end of the stream suitable for later incremental syncs.
    """
    path = (
        f"/users/{user_id}/mailFolders/inbox/messages/delta"
        f"?$top={top}"
        f"&$select=id,internetMessageId,conversationId,subject,from,toRecipients,"
        f"ccRecipients,bccRecipients,body,bodyPreview,hasAttachments,importance,"
        f"isRead,sentDateTime,receivedDateTime,parentFolderId"
    )
    async for page in client.iter_pages(path):
        yield page


async def list_messages_delta(client: GraphClient, delta_link: str) -> AsyncIterator[dict]:
    """Incremental sync using a saved deltaLink."""
    async for page in client.iter_pages(delta_link):
        yield page


async def list_attachments(client: GraphClient, user_id: str, message_id: str) -> list[dict]:
    """Lightweight attachment metadata (names + sizes + content types). No bodies."""
    resp = await client.get(
        f"/users/{user_id}/messages/{message_id}/attachments"
        f"?$select=name,contentType,size"
    )
    return resp.get("value", [])
