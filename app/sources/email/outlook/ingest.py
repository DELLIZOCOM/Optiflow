"""
Outlook ingestion pipelines.

Three coroutines run per tenant:
  - discovery_loop       enumerate /users periodically (hourly-ish),
                         upsert into mailboxes table
  - delta_loop_for(mb)   per-mailbox: initial 30-day sync (if not yet done),
                         then hit deltaLink every SYNC_INTERVAL seconds
  - backfill_loop        round-robins active mailboxes that aren't fully
                         backfilled, at 1 page/minute, until each hits
                         BACKFILL_HORIZON_DAYS

All three are cooperatively cancellable via asyncio.CancelledError and
tolerant of Graph throttling (handled inside GraphClient).
"""

import asyncio
import logging
import time
from typing import Optional

from .graph import GraphClient, GraphHTTPError, list_users
from .mapper import graph_to_row, graph_user_to_mailbox

logger = logging.getLogger(__name__)

DISCOVERY_INTERVAL_SECS = 60 * 60           # 1 hour
DELTA_INTERVAL_SECS     = 10 * 60           # 10 minutes
BACKFILL_PAGE_INTERVAL  = 60                # 1 minute between pages
BACKFILL_HORIZON_DAYS   = 365               # how far back we go, default
INITIAL_WINDOW_DAYS     = 30                # eager sync window


class IngestCoordinator:
    """
    Owns the lifecycle of discovery + per-mailbox delta + backfill tasks
    for a single Outlook tenant.
    """

    def __init__(self, graph: GraphClient, store, *, backfill_days: int = BACKFILL_HORIZON_DAYS):
        self._graph = graph
        self._store = store
        self._backfill_days = backfill_days

        self._discovery_task: Optional[asyncio.Task] = None
        self._delta_tasks: dict[str, asyncio.Task] = {}
        self._backfill_task: Optional[asyncio.Task] = None
        self._stopped = False

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start all background ingestion tasks."""
        if self._stopped:
            raise RuntimeError("IngestCoordinator cannot be restarted; create a new one")
        self._discovery_task = asyncio.create_task(self._discovery_loop(), name="outlook-discovery")
        self._backfill_task  = asyncio.create_task(self._backfill_loop(),  name="outlook-backfill")
        # Delta loops are spawned lazily after first discovery lands.

    async def stop(self) -> None:
        self._stopped = True
        tasks = [self._discovery_task, self._backfill_task, *self._delta_tasks.values()]
        for t in tasks:
            if t and not t.done():
                t.cancel()
        for t in tasks:
            if t is None:
                continue
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._delta_tasks.clear()

    # ── discovery ────────────────────────────────────────────────────────────

    async def _discovery_loop(self) -> None:
        while not self._stopped:
            try:
                await self._discover_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[Outlook] discovery pass failed")
            try:
                await asyncio.sleep(DISCOVERY_INTERVAL_SECS)
            except asyncio.CancelledError:
                raise

    async def _discover_once(self) -> None:
        seen_ids: set[str] = set()
        added = 0
        async for user in list_users(self._graph):
            mb = graph_user_to_mailbox(user)
            if not mb:
                continue
            seen_ids.add(mb["id"])
            self._store.upsert_mailbox(mb)
            added += 1
            # Spawn delta task for new active mailboxes
            if mb["status"] == "active" and mb["id"] not in self._delta_tasks:
                self._spawn_delta_task(mb["id"], mb["account_email"])
        logger.info("[Outlook] discovery pass: %d mailboxes upserted", added)
        # Mark mailboxes no longer in /users as disabled (off-boarded employees).
        current = {m["id"] for m in self._store.list_mailboxes(active_only=False)}
        for gone_id in current - seen_ids:
            self._store.upsert_mailbox({
                "id": gone_id,
                "account_email": "",  # will be ignored by UPDATE branch
                "status": "disabled",
            })
            t = self._delta_tasks.pop(gone_id, None)
            if t and not t.done():
                t.cancel()

    # ── delta (per-mailbox) ──────────────────────────────────────────────────

    def _spawn_delta_task(self, mailbox_id: str, account_email: str) -> None:
        task = asyncio.create_task(
            self._delta_loop_for(mailbox_id, account_email),
            name=f"outlook-delta-{account_email}",
        )
        self._delta_tasks[mailbox_id] = task

    async def _delta_loop_for(self, mailbox_id: str, account_email: str) -> None:
        while not self._stopped:
            try:
                await self._delta_once(mailbox_id, account_email)
            except asyncio.CancelledError:
                raise
            except GraphHTTPError as e:
                logger.warning("[Outlook] delta %s HTTP %s", account_email, e.status)
                self._store.update_sync_state(
                    mailbox_id,
                    last_error=f"HTTP {e.status}",
                    last_sync_at=time.time(),
                )
            except Exception as e:
                logger.exception("[Outlook] delta %s failed", account_email)
                self._store.update_sync_state(
                    mailbox_id,
                    last_error=type(e).__name__,
                    last_sync_at=time.time(),
                )
            try:
                await asyncio.sleep(DELTA_INTERVAL_SECS)
            except asyncio.CancelledError:
                raise

    async def _delta_once(self, mailbox_id: str, account_email: str) -> None:
        state = self._store.get_sync_state(mailbox_id)
        delta_link = state.get("delta_link")
        initial_done = bool(state.get("initial_synced"))

        if not initial_done or not delta_link:
            # Initial sync via the delta endpoint; last page yields deltaLink
            start_path = self._initial_delta_path(mailbox_id)
            delta_link = await self._consume_delta_stream(
                mailbox_id, account_email, start_path, initial=True
            )
            self._store.update_sync_state(
                mailbox_id,
                delta_link=delta_link,
                initial_synced=1,
                last_sync_at=time.time(),
                last_error=None,
            )
            return

        new_link = await self._consume_delta_stream(
            mailbox_id, account_email, delta_link, initial=False
        )
        self._store.update_sync_state(
            mailbox_id,
            delta_link=new_link or delta_link,
            last_sync_at=time.time(),
            last_error=None,
        )

    def _initial_delta_path(self, mailbox_id: str) -> str:
        cutoff = time.time() - INITIAL_WINDOW_DAYS * 86400
        # Graph $delta doesn't accept $filter on receivedDateTime directly —
        # we filter client-side during consumption. This keeps the cursor
        # valid for subsequent incremental runs.
        return (
            f"/users/{mailbox_id}/mailFolders/inbox/messages/delta?$top=100"
            f"&$select=id,internetMessageId,conversationId,subject,from,toRecipients,"
            f"ccRecipients,bccRecipients,body,bodyPreview,hasAttachments,importance,"
            f"isRead,sentDateTime,receivedDateTime,parentFolderId"
        )

    async def _consume_delta_stream(
        self,
        mailbox_id: str,
        account_email: str,
        start_url: str,
        *,
        initial: bool,
    ) -> Optional[str]:
        cutoff = (time.time() - INITIAL_WINDOW_DAYS * 86400) if initial else 0.0
        rows_buf: list[dict] = []
        deletes_buf: list[str] = []
        delta_link: Optional[str] = None
        total_new = 0
        total_del = 0

        async for page in self._graph.iter_pages(start_url):
            for item in page.get("value", []):
                # Deletions in delta stream are marked with @removed
                if item.get("@removed"):
                    if item.get("id"):
                        deletes_buf.append(item["id"])
                    continue
                # Client-side filter during initial sync
                if initial:
                    recv = item.get("receivedDateTime")
                    from .mapper import _parse_iso  # local import: sibling module
                    if _parse_iso(recv) < cutoff:
                        continue
                row = graph_to_row(item, mailbox_id=mailbox_id, account_email=account_email)
                if row.get("provider_msg_id"):
                    rows_buf.append(row)

            # Flush in batches of ~200 to keep memory bounded
            if len(rows_buf) >= 200:
                total_new += await self._store.upsert_emails(rows_buf)
                rows_buf.clear()
            if len(deletes_buf) >= 200:
                total_del += await self._store.delete_emails(mailbox_id, deletes_buf)
                deletes_buf.clear()

            delta_link = page.get("@odata.deltaLink") or delta_link

        if rows_buf:
            total_new += await self._store.upsert_emails(rows_buf)
        if deletes_buf:
            total_del += await self._store.delete_emails(mailbox_id, deletes_buf)

        logger.info(
            "[Outlook] %s %s +%d -%d",
            "initial" if initial else "delta",
            account_email,
            total_new,
            total_del,
        )
        return delta_link

    # ── backfill ─────────────────────────────────────────────────────────────

    async def _backfill_loop(self) -> None:
        """
        Round-robins mailboxes that have initial sync done but backfill
        pending. One page per BACKFILL_PAGE_INTERVAL to stay polite.
        """
        while not self._stopped:
            mailboxes = [
                m for m in self._store.list_mailboxes(active_only=True)
                if m.get("initial_synced") and not m.get("backfill_done")
            ]
            if not mailboxes:
                try:
                    await asyncio.sleep(BACKFILL_PAGE_INTERVAL * 5)
                    continue
                except asyncio.CancelledError:
                    raise
            for mb in mailboxes:
                if self._stopped:
                    return
                try:
                    await self._backfill_one_page(mb)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("[Outlook] backfill page %s failed", mb["account_email"])
                try:
                    await asyncio.sleep(BACKFILL_PAGE_INTERVAL)
                except asyncio.CancelledError:
                    raise

    async def _backfill_one_page(self, mb: dict) -> None:
        """
        Pull ONE page of messages older than INITIAL_WINDOW_DAYS, moving
        backward through time using a cursor stored in sync_state.
        """
        state = self._store.get_sync_state(mb["id"])
        cursor = state.get("backfill_cursor")

        if cursor:
            url = cursor
        else:
            cutoff = time.time() - INITIAL_WINDOW_DAYS * 86400
            cutoff_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cutoff))
            horizon = time.time() - self._backfill_days * 86400
            url = (
                f"/users/{mb['id']}/mailFolders/inbox/messages"
                f"?$top=100&$orderby=receivedDateTime desc"
                f"&$filter=receivedDateTime lt {cutoff_iso}"
                f"&$select=id,internetMessageId,conversationId,subject,from,toRecipients,"
                f"ccRecipients,bccRecipients,body,bodyPreview,hasAttachments,importance,"
                f"isRead,sentDateTime,receivedDateTime,parentFolderId"
            )

        page = await self._graph.get(url)
        rows = []
        oldest_recv = None
        from .mapper import _parse_iso
        horizon_ts = time.time() - self._backfill_days * 86400
        for item in page.get("value", []):
            recv_ts = _parse_iso(item.get("receivedDateTime"))
            if recv_ts and recv_ts < horizon_ts:
                # Crossed the horizon — stop.
                self._store.update_sync_state(mb["id"], backfill_done=1, backfill_cursor=None)
                return
            row = graph_to_row(item, mailbox_id=mb["id"], account_email=mb["account_email"])
            if row.get("provider_msg_id"):
                rows.append(row)
            if oldest_recv is None or recv_ts < oldest_recv:
                oldest_recv = recv_ts

        inserted = await self._store.upsert_emails(rows) if rows else 0
        next_link = page.get("@odata.nextLink")
        if not next_link:
            self._store.update_sync_state(mb["id"], backfill_done=1, backfill_cursor=None)
        else:
            self._store.update_sync_state(mb["id"], backfill_cursor=next_link)

        logger.info(
            "[Outlook] backfill %s +%d (oldest=%s)",
            mb["account_email"], inserted,
            time.strftime("%Y-%m-%d", time.gmtime(oldest_recv)) if oldest_recv else "n/a",
        )
