"""
IMAP ingestion coordinator.

For each configured mailbox we run one async task that:
  1. Connects + selects INBOX
  2. On first run, fetches all UIDs SINCE (now - backfill_days) — paginated
     by chunks of FETCH_BATCH so we don't blow memory
  3. On every subsequent run, asks "UID > last_uid" and inserts the diff
  4. Sleeps POLL_INTERVAL_SECS and repeats

State per mailbox lives in EmailStore.sync_state:
  - last_uid (stored in delta_link as the literal string of the integer)
  - last_sync_at, initial_synced, backfill_done, last_error

This is conceptually identical to the Outlook delta loop, just driven by
IMAP UIDs instead of Microsoft Graph delta tokens.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Optional

from .client import IMAPClient, IMAPServer, IMAPAuthError
from .mapper import imap_message_to_row

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECS = 5 * 60         # 5 minutes between polls per mailbox
BATCH_INSERT_SIZE  = 50             # how many parsed rows to flush at once
FETCH_BATCH        = 25             # IMAP FETCH chunk size


@dataclass
class IMAPMailboxConfig:
    """One mailbox the user wants ingested."""
    account_email: str
    password: str                   # plaintext at runtime; encrypted at rest
    display_name: Optional[str] = None
    folder: str = "INBOX"           # IMAP folder to read; INBOX by default


class IMAPCoordinator:
    """
    Owns the lifecycle of one polling task per configured mailbox.

    Unlike the Outlook coordinator there's no tenant-wide discovery — the
    operator supplies the mailbox list. Mailboxes can be added or removed
    at runtime via add_mailbox / remove_mailbox, and a manual `sync_now`
    nudges one or all mailboxes to skip their wait and run immediately.

    Concurrency model: one asyncio.Task + one asyncio.Event per mailbox.
    The task awaits the event with a 5-min timeout — whichever fires
    first triggers the next sync. Setting the event = "sync now".
    """

    def __init__(
        self,
        store,
        *,
        server: IMAPServer,
        mailboxes: list[IMAPMailboxConfig],
        backfill_days: int = 365,
    ):
        self._store = store
        self._server = server
        self._mailboxes: dict[str, IMAPMailboxConfig] = {}   # mailbox_id -> config
        self._backfill_days = max(1, int(backfill_days))
        self._tasks: dict[str, asyncio.Task] = {}
        self._wake:  dict[str, asyncio.Event] = {}
        self._stopped = False

        for mb in mailboxes:
            mid = _stable_mailbox_id(mb.account_email)
            self._mailboxes[mid] = mb

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._stopped:
            raise RuntimeError("IMAPCoordinator cannot be restarted; create a new one")
        for mailbox_id, mb in self._mailboxes.items():
            self._store.upsert_mailbox({
                "id":            mailbox_id,
                "account_email": mb.account_email.lower(),
                "display_name":  mb.display_name or mb.account_email,
                "status":        "active",
                "discovered_at": time.time(),
            })
            self._spawn_task(mailbox_id, mb)
        logger.info("[IMAP] coordinator started for %d mailbox(es)", len(self._tasks))

    async def stop(self) -> None:
        self._stopped = True
        for t in self._tasks.values():
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        self._wake.clear()
        logger.info("[IMAP] coordinator stopped")

    # ── runtime mutation ────────────────────────────────────────────────────

    async def add_mailbox(self, mb: IMAPMailboxConfig) -> str:
        """
        Add a single mailbox at runtime and start its poll task immediately.
        Idempotent — calling with an already-configured email is a no-op.
        Returns the mailbox_id.
        """
        if self._stopped:
            raise RuntimeError("Coordinator stopped; cannot add mailbox")
        mailbox_id = _stable_mailbox_id(mb.account_email)
        if mailbox_id in self._mailboxes:
            return mailbox_id

        self._mailboxes[mailbox_id] = mb
        self._store.upsert_mailbox({
            "id":            mailbox_id,
            "account_email": mb.account_email.lower(),
            "display_name":  mb.display_name or mb.account_email,
            "status":        "active",
            "discovered_at": time.time(),
        })
        self._spawn_task(mailbox_id, mb)
        logger.info("[IMAP] mailbox added at runtime: %s", mb.account_email)
        return mailbox_id

    async def remove_mailbox(self, account_email: str, *, purge_cache: bool = False) -> bool:
        """
        Stop the poll task for a mailbox and mark it disabled in the store.
        With purge_cache=True the store also drops its messages and sync state.
        Returns True if a matching mailbox existed.
        """
        mailbox_id = _stable_mailbox_id(account_email)
        if mailbox_id not in self._mailboxes:
            return False

        task = self._tasks.pop(mailbox_id, None)
        self._wake.pop(mailbox_id, None)
        self._mailboxes.pop(mailbox_id, None)
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        if purge_cache:
            self._store.delete_mailbox(mailbox_id)
        else:
            self._store.set_mailbox_status(mailbox_id, "disabled")
        logger.info(
            "[IMAP] mailbox removed at runtime: %s (purge=%s)",
            account_email, purge_cache,
        )
        return True

    def sync_now(self, mailbox_id: Optional[str] = None) -> int:
        """
        Trigger an immediate sync for one mailbox (by mailbox_id) or all
        mailboxes if mailbox_id is None. Non-blocking — just fires the
        wake event(s); the existing task picks them up. Returns the
        number of wake events fired.
        """
        if mailbox_id:
            ev = self._wake.get(mailbox_id)
            if ev is not None and not ev.is_set():
                ev.set()
                return 1
            return 0
        fired = 0
        for ev in self._wake.values():
            if not ev.is_set():
                ev.set()
                fired += 1
        return fired

    # ── internals ───────────────────────────────────────────────────────────

    def _spawn_task(self, mailbox_id: str, mb: IMAPMailboxConfig) -> None:
        """Create the wake event + asyncio.Task for one mailbox."""
        self._wake[mailbox_id] = asyncio.Event()
        self._tasks[mailbox_id] = asyncio.create_task(
            self._mailbox_loop(mailbox_id, mb),
            name=f"imap-poll-{mb.account_email}",
        )

    # ── per-mailbox poll loop ────────────────────────────────────────────────

    async def _mailbox_loop(self, mailbox_id: str, mb: IMAPMailboxConfig) -> None:
        # Stagger startup so we don't hammer the IMAP server on boot
        await asyncio.sleep(0.2 + 0.5 * len(self._tasks))
        wake = self._wake[mailbox_id]
        while not self._stopped:
            try:
                await self._sync_once(mailbox_id, mb)
            except asyncio.CancelledError:
                raise
            except IMAPAuthError as e:
                msg = f"auth: {e}"
                logger.warning("[IMAP] %s — %s", mb.account_email, msg)
                self._store.update_sync_state(mailbox_id, last_error=msg, last_sync_at=time.time())
            except Exception:
                logger.exception("[IMAP] sync failed for %s", mb.account_email)
                self._store.update_sync_state(
                    mailbox_id,
                    last_error="unexpected error (see server logs)",
                    last_sync_at=time.time(),
                )
            # Wait for either the poll interval or a manual sync_now nudge,
            # whichever fires first. Clear the event afterwards so the next
            # cycle can be triggered again.
            try:
                await asyncio.wait_for(wake.wait(), timeout=POLL_INTERVAL_SECS)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            wake.clear()

    async def _sync_once(self, mailbox_id: str, mb: IMAPMailboxConfig) -> None:
        """Run one connect → select → fetch-new-UIDs cycle."""
        client = IMAPClient(self._server, mb.account_email, mb.password)
        try:
            await client.connect()
            await client.select_folder(mb.folder)

            state = self._store.get_sync_state(mailbox_id) or {}
            last_uid_raw = state.get("delta_link")  # we reuse this column for UID
            try:
                last_uid = int(last_uid_raw) if last_uid_raw else 0
            except (TypeError, ValueError):
                last_uid = 0
            initial_done = bool(state.get("initial_synced"))

            if initial_done and last_uid > 0:
                uids = await client.search_uids_above(last_uid)
            else:
                # First run for this mailbox: pull the backfill window in one go.
                since_epoch = time.time() - self._backfill_days * 86400
                uids = await client.search_uids_since(since_epoch)

            if not uids:
                self._store.update_sync_state(
                    mailbox_id,
                    last_sync_at=time.time(),
                    last_error=None,
                    initial_synced=1,
                    backfill_done=1,
                )
                return

            uids.sort()
            buffer: list[dict] = []
            highest = last_uid
            fetched = 0      # bodies actually pulled off the wire
            stored  = 0      # rows handed to the store
            failed  = 0      # mapper or insert errors

            logger.info(
                "[IMAP] %s — %d UID(s) to fetch (last_uid=%d, initial_done=%s)",
                mb.account_email, len(uids), last_uid, initial_done,
            )

            async for uid, raw in client.fetch_many(uids, batch=FETCH_BATCH):
                fetched += 1
                if uid > highest:
                    highest = uid
                try:
                    row = imap_message_to_row(
                        raw,
                        uid=uid,
                        mailbox_id=mailbox_id,
                        account_email=mb.account_email.lower(),
                        folder=mb.folder.lower() if mb.folder else "inbox",
                    )
                except Exception:
                    failed += 1
                    logger.exception(
                        "[IMAP] %s — mapper crashed on uid=%s; skipping",
                        mb.account_email, uid,
                    )
                    continue

                if row is None:
                    failed += 1
                    continue

                buffer.append(row)
                if len(buffer) >= BATCH_INSERT_SIZE:
                    try:
                        n = await self._store.upsert_emails(buffer)
                        stored += n
                    except Exception:
                        failed += len(buffer)
                        logger.exception(
                            "[IMAP] %s — upsert_emails failed for batch of %d",
                            mb.account_email, len(buffer),
                        )
                    buffer.clear()

            if buffer:
                try:
                    n = await self._store.upsert_emails(buffer)
                    stored += n
                except Exception:
                    failed += len(buffer)
                    logger.exception(
                        "[IMAP] %s — upsert_emails failed for final batch of %d",
                        mb.account_email, len(buffer),
                    )

            # Be honest about silent drops in the dashboard — a sync that
            # found UIDs but stored nothing is almost always a server-quirk
            # mismatch, and the operator deserves to see that.
            err_msg: Optional[str] = None
            if fetched == 0 and len(uids) > 0:
                err_msg = (
                    f"server matched {len(uids)} UID(s) but FETCH returned 0 bodies "
                    "(IMAP server compatibility issue — see logs)"
                )
            elif failed and stored == 0:
                err_msg = f"all {failed} message(s) failed to parse or insert (see logs)"
            elif failed:
                err_msg = f"{failed} message(s) failed to parse (rest stored OK)"

            self._store.update_sync_state(
                mailbox_id,
                delta_link=str(highest),
                last_sync_at=time.time(),
                last_error=err_msg,
                initial_synced=1,
                backfill_done=1,
            )
            logger.info(
                "[IMAP] %s — found=%d fetched=%d stored=%d failed=%d last_uid=%d",
                mb.account_email, len(uids), fetched, stored, failed, highest,
            )

            # ── Entity auto-discovery ──────────────────────────────────────
            # Cheap O(N) pass over messages received in the last 24h: extract
            # senders and upsert them as low-confidence entities. Idempotent —
            # existing entities just bump seen_count + last_seen.
            if stored > 0:
                try:
                    n = await self._store.auto_discover_entities_from_recent(
                        lookback_seconds=86400
                    )
                    if n:
                        logger.info("[IMAP] %s — discovered/refreshed %d entit(y/ies)",
                                    mb.account_email, n)
                except Exception:
                    logger.exception(
                        "[IMAP] %s — entity auto-discovery failed (sync still OK)",
                        mb.account_email,
                    )
        finally:
            await client.close()


def _stable_mailbox_id(account_email: str) -> str:
    """Deterministic id from the email address. Stable across restarts."""
    h = hashlib.sha1(account_email.strip().lower().encode("utf-8")).hexdigest()[:24]
    return f"imap-{h}"
