"""
Async wrapper over Python's stdlib imaplib.

stdlib imaplib is synchronous; we offload each call into a thread executor so
the rest of the event loop stays responsive. One IMAPClient instance owns one
TCP/SSL connection; do not share across coroutines.

Why imaplib (not aioimaplib)?
  - Zero new dependencies — imaplib + ssl ship with Python.
  - The volume of IMAP traffic per mailbox is modest (poll every 5 min);
    sync-in-thread is fine.
  - aioimaplib's API is less mature; harder to debug.

Provider presets:
  - GoDaddy Workspace Email:   imap.secureserver.net : 993 (SSL)
  - Generic IMAP:              host/port/use_ssl supplied by the user
"""

from __future__ import annotations

import asyncio
import imaplib
import logging
import re
import socket
import ssl
from dataclasses import dataclass
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Matches "UID 1234" anywhere in a FETCH preamble or trailer.
# Bytes-mode so we don't depend on any one charset surviving a decode.
_UID_RE = re.compile(rb"\bUID\s+(\d+)", re.IGNORECASE)

# Network timeout in seconds. imaplib's default is system-wide; we want fast
# failure on misconfigured hosts so the setup wizard doesn't hang.
_DEFAULT_TIMEOUT = 25

# Provider presets — a thin map from the UI's "provider" picker to defaults.
PROVIDER_PRESETS: dict[str, dict] = {
    "godaddy": {
        "label":   "GoDaddy Workspace Email",
        "host":    "imap.secureserver.net",
        "port":    993,
        "use_ssl": True,
    },
    "generic": {
        "label":   "Generic IMAP",
        "host":    "",
        "port":    993,
        "use_ssl": True,
    },
}


class IMAPAuthError(Exception):
    """Raised on login / connection failure with a human-readable message."""


@dataclass
class IMAPServer:
    host: str
    port: int = 993
    use_ssl: bool = True

    def __post_init__(self):
        if not self.host:
            raise ValueError("IMAP host is required")
        if self.port <= 0 or self.port > 65535:
            raise ValueError("IMAP port out of range")


class IMAPClient:
    """
    One IMAP TCP/SSL connection bound to one mailbox login.

    Public surface is async; internals run imaplib in threads.
    Connect / login / select happen lazily on the first call so callers
    can construct the object without doing I/O.
    """

    def __init__(self, server: IMAPServer, account_email: str, password: str):
        self._server = server
        self._email = account_email
        self._password = password
        self._imap: Optional[imaplib.IMAP4] = None
        self._lock = asyncio.Lock()  # serialize writes against this socket

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        if self._imap is not None:
            return

        def _do() -> imaplib.IMAP4:
            try:
                if self._server.use_ssl:
                    ctx = ssl.create_default_context()
                    imap = imaplib.IMAP4_SSL(
                        self._server.host, self._server.port,
                        ssl_context=ctx, timeout=_DEFAULT_TIMEOUT,
                    )
                else:
                    imap = imaplib.IMAP4(
                        self._server.host, self._server.port,
                        timeout=_DEFAULT_TIMEOUT,
                    )
                imap.login(self._email, self._password)
                return imap
            except imaplib.IMAP4.error as e:
                # Wrong username / password / disabled account → bytes message
                raise IMAPAuthError(_decode_imap_err(e)) from e
            except (socket.gaierror, socket.timeout, OSError) as e:
                raise IMAPAuthError(f"Cannot reach {self._server.host}:{self._server.port} — {e}") from e
            except ssl.SSLError as e:
                raise IMAPAuthError(f"TLS handshake failed: {e}") from e

        self._imap = await asyncio.to_thread(_do)
        logger.debug("IMAP connected: %s @ %s", self._email, self._server.host)

    async def close(self) -> None:
        if self._imap is None:
            return
        imap = self._imap
        self._imap = None

        def _do() -> None:
            try:
                try:
                    imap.close()
                except Exception:
                    pass
                imap.logout()
            except Exception:
                pass

        await asyncio.to_thread(_do)

    # ── operations ───────────────────────────────────────────────────────────

    async def select_folder(self, folder: str = "INBOX") -> int:
        """
        SELECT a folder read-only. Returns the message count reported by the
        server. Raises IMAPAuthError if the folder doesn't exist.
        """
        await self.connect()
        async with self._lock:
            def _do() -> int:
                typ, data = self._imap.select(_quote(folder), readonly=True)
                if typ != "OK":
                    raise IMAPAuthError(f"SELECT {folder} failed: {_safe_decode(data)}")
                try:
                    return int((data[0] or b"0").decode())
                except Exception:
                    return 0
            return await asyncio.to_thread(_do)

    async def list_folders(self) -> list[str]:
        """Return all top-level folder names visible to this login."""
        await self.connect()
        async with self._lock:
            def _do() -> list[str]:
                typ, data = self._imap.list()
                if typ != "OK":
                    return []
                out: list[str] = []
                for raw in data or []:
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="replace")
                    # Format: (\HasNoChildren) "/" "INBOX"
                    if '"' in line:
                        out.append(line.rsplit('"', 2)[1])
                return out
            return await asyncio.to_thread(_do)

    async def search_uids_since(self, since_epoch: Optional[float]) -> list[int]:
        """
        Return UIDs in the currently-selected folder. If `since_epoch` is set,
        scope to messages received on or after that day (IMAP date granularity
        is one day).
        """
        await self.connect()
        async with self._lock:
            def _do() -> list[int]:
                if since_epoch is None or since_epoch <= 0:
                    typ, data = self._imap.uid("SEARCH", None, "ALL")
                else:
                    from datetime import datetime, timezone
                    dt = datetime.fromtimestamp(since_epoch, tz=timezone.utc)
                    # IMAP date format: 01-Jan-2025
                    date_str = dt.strftime("%d-%b-%Y")
                    typ, data = self._imap.uid("SEARCH", None, "SINCE", date_str)
                if typ != "OK" or not data:
                    return []
                raw = data[0] or b""
                return [int(x) for x in raw.split() if x.isdigit()]
            return await asyncio.to_thread(_do)

    async def search_uids_above(self, last_uid: int) -> list[int]:
        """
        Return UIDs in the currently-selected folder strictly greater than
        `last_uid`. Used for the incremental polling loop.
        """
        await self.connect()
        async with self._lock:
            def _do() -> list[int]:
                criterion = f"{last_uid + 1}:*"
                typ, data = self._imap.uid("SEARCH", None, "UID", criterion)
                if typ != "OK" or not data:
                    return []
                raw = data[0] or b""
                # The "*:N" form can return the highest UID even when nothing's
                # new — defensive filter.
                return [int(x) for x in raw.split() if x.isdigit() and int(x) > last_uid]
            return await asyncio.to_thread(_do)

    async def fetch_raw(self, uid: int) -> Optional[bytes]:
        """Fetch one message by UID, returning the raw RFC 822 bytes."""
        await self.connect()
        async with self._lock:
            def _do() -> Optional[bytes]:
                # Asking for UID explicitly is harmless (a UID FETCH already
                # implies it server-side per RFC 3501 §6.4.8) and makes the
                # response shape deterministic across servers.
                typ, data = self._imap.uid("FETCH", str(uid), "(UID BODY.PEEK[])")
                if typ != "OK" or not data:
                    return None
                # data is a list of tuples-or-bytes; the payload comes back as
                # (b'1 (UID 4 BODY[] {12345}', <bytes>)
                for item in data:
                    if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
                        return bytes(item[1])
                return None
            return await asyncio.to_thread(_do)

    async def fetch_many(self, uids: Iterable[int], batch: int = 25):
        """Yield (uid, raw_bytes) tuples, fetching in batches."""
        chunk: list[int] = []
        for uid in uids:
            chunk.append(int(uid))
            if len(chunk) >= batch:
                async for pair in self._fetch_chunk(chunk):
                    yield pair
                chunk = []
        if chunk:
            async for pair in self._fetch_chunk(chunk):
                yield pair

    async def _fetch_chunk(self, uids: list[int]):
        """
        FETCH a batch of UIDs and yield (uid, body_bytes) pairs.

        Server response shape varies by IMAP implementation. imaplib hands us
        a list mixing tuples (preamble + body) and trailing bytes literals
        (`b')'` or `b' UID 4)'`). Different servers put the UID in different
        places:

          * Most:   ``(b'1 (UID 4 BODY[] {1234}', <body>)``  — UID in preamble
          * Some:   ``(b'1 (BODY[] {1234}', <body>), b' UID 4)'`` — in trailer

        We extract UID via a regex that scans both the tuple's preamble *and*
        the bytes literal that follows the body (whichever has it). If
        neither contains a UID we fall back to positional matching against
        the request order — better than silently dropping the message.
        """
        if not uids:
            return
        requested_order = list(uids)
        await self.connect()
        async with self._lock:
            def _do() -> dict[int, bytes]:
                seq = ",".join(str(u) for u in requested_order)
                typ, data = self._imap.uid("FETCH", seq, "(UID BODY.PEEK[])")
                if typ != "OK" or not data:
                    logger.warning(
                        "[IMAP] FETCH non-OK or empty response for %d uid(s) (%s)",
                        len(requested_order), typ,
                    )
                    return {}
                out: dict[int, bytes] = {}
                pending_body: Optional[bytes] = None
                pending_uid:  Optional[int]   = None
                positional_idx = 0

                def _uid_from_bytes(b) -> Optional[int]:
                    if not isinstance(b, (bytes, bytearray)):
                        return None
                    m = _UID_RE.search(b)
                    if not m:
                        return None
                    try:
                        return int(m.group(1))
                    except ValueError:
                        return None

                def _commit():
                    nonlocal pending_body, pending_uid, positional_idx
                    if pending_body is None:
                        return
                    uid = pending_uid
                    if uid is None and positional_idx < len(requested_order):
                        uid = requested_order[positional_idx]
                        logger.debug(
                            "[IMAP] FETCH had no UID marker, using positional uid=%s", uid,
                        )
                    if uid is not None:
                        out[uid] = bytes(pending_body)
                        positional_idx += 1
                    else:
                        logger.warning("[IMAP] dropping message body — no UID could be inferred")
                    pending_body = None
                    pending_uid  = None

                for item in data:
                    if isinstance(item, tuple) and len(item) >= 2:
                        # New message tuple — flush whatever was pending.
                        _commit()
                        header, body = item[0], item[1]
                        pending_uid  = _uid_from_bytes(header)
                        pending_body = bytes(body) if isinstance(body, (bytes, bytearray)) else None
                    elif isinstance(item, (bytes, bytearray)):
                        # Trailing literal — may contain the UID for the body
                        # we just collected (some servers do this).
                        if pending_uid is None:
                            uid = _uid_from_bytes(item)
                            if uid is not None:
                                pending_uid = uid

                _commit()

                if not out:
                    logger.warning(
                        "[IMAP] FETCH yielded 0 messages for %d requested uid(s); "
                        "raw response items=%d",
                        len(requested_order), len(data),
                    )
                return out
            uid_to_raw = await asyncio.to_thread(_do)
        for uid, raw in uid_to_raw.items():
            yield uid, raw


# ── helpers ───────────────────────────────────────────────────────────────────

def _quote(folder: str) -> str:
    """
    IMAP folder names containing spaces or non-ASCII need quoting; imaplib
    accepts a string, but quoting defensively avoids surprises.
    """
    f = folder.strip()
    if not f:
        return '"INBOX"'
    if '"' not in f:
        return f'"{f}"'
    return f


def _decode_imap_err(e: imaplib.IMAP4.error) -> str:
    args = getattr(e, "args", None) or ()
    out = []
    for a in args:
        if isinstance(a, (bytes, bytearray)):
            out.append(a.decode("utf-8", errors="replace"))
        else:
            out.append(str(a))
    return " ".join(out).strip() or "IMAP error"


def _safe_decode(data) -> str:
    if not data:
        return ""
    try:
        if isinstance(data, list):
            data = data[0]
        if isinstance(data, (bytes, bytearray)):
            return data.decode("utf-8", errors="replace")
        return str(data)
    except Exception:
        return ""
