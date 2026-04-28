"""
Map a raw RFC 822 message (as returned by IMAP FETCH BODY.PEEK[]) to a row
dict suitable for EmailStore.upsert_emails().

Pure and synchronous. Uses Python's stdlib `email` package — no third-party
MIME parsers, no extra deps.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import hashlib
import html
import json
import logging
import re
import time
from email.message import EmailMessage
from typing import Optional

logger = logging.getLogger(__name__)

_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")


def _html_to_text(html_body: str) -> str:
    """Crude HTML → plain text. Good enough for FTS indexing."""
    if not html_body:
        return ""
    html_body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html_body, flags=re.I | re.S)
    html_body = re.sub(r"<(br|/p|/div|/li|/tr)[^>]*>", "\n", html_body, flags=re.I)
    txt = _TAG.sub(" ", html_body)
    txt = html.unescape(txt)
    txt = _WS.sub(" ", txt).strip()
    return txt


def _parse_address(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Parse 'Name <email@host>' → (name, email_lowercased)."""
    if not value:
        return None, None
    try:
        name, addr = email.utils.parseaddr(value)
    except Exception:
        return None, None
    name = (name or "").strip() or None
    addr = (addr or "").strip().lower() or None
    return name, addr


def _parse_addresses(value: Optional[str]) -> list[str]:
    """Parse a header value with one or more addresses → list of email addresses."""
    if not value:
        return []
    try:
        pairs = email.utils.getaddresses([value])
    except Exception:
        return []
    out: list[str] = []
    for _, addr in pairs:
        a = (addr or "").strip().lower()
        if a:
            out.append(a)
    return out


def _parse_date(value: Optional[str]) -> float:
    """Parse an RFC 2822 Date header → epoch seconds. Returns 0.0 on failure."""
    if not value:
        return 0.0
    try:
        dt = email.utils.parsedate_to_datetime(value)
        if dt is None:
            return 0.0
        # parsedate_to_datetime returns naive for some inputs; treat naive as UTC.
        if dt.tzinfo is None:
            from datetime import timezone
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _extract_bodies(msg: EmailMessage) -> tuple[str, Optional[str]]:
    """
    Pull a plain-text body and (optionally) the raw HTML body out of the message.
    Returns (text_body, html_body_or_none).

    Handles multipart, charset detection, and missing parts.
    """
    text_parts: list[str] = []
    html_parts: list[str] = []

    for part in msg.walk():
        if part.is_multipart():
            continue
        if part.get_content_disposition() == "attachment":
            continue
        ctype = (part.get_content_type() or "").lower()
        if ctype == "text/plain":
            try:
                text_parts.append(part.get_content())
            except Exception:
                # Charset issues: fall back to raw decode
                payload = part.get_payload(decode=True) or b""
                text_parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
        elif ctype == "text/html":
            try:
                html_parts.append(part.get_content())
            except Exception:
                payload = part.get_payload(decode=True) or b""
                html_parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))

    text_body = "\n".join(p for p in text_parts if p).strip()
    html_body = "\n".join(p for p in html_parts if p).strip() or None

    if not text_body and html_body:
        text_body = _html_to_text(html_body)

    return text_body, html_body


def _extract_attachment_names(msg: EmailMessage) -> list[str]:
    names: list[str] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        if part.get_content_disposition() != "attachment":
            continue
        name = part.get_filename()
        if name:
            try:
                # Some clients put RFC 2047-encoded names; email.policy.default
                # generally decodes them, but be defensive.
                if isinstance(name, bytes):
                    name = name.decode("utf-8", errors="replace")
                names.append(str(name).strip())
            except Exception:
                continue
    return names


def _stable_msg_id(raw: bytes, header_msg_id: Optional[str], uid: int) -> str:
    """
    Provider-stable id for an IMAP message. Prefer the RFC 822 Message-ID
    when present (unique across servers). Fall back to a sha256 of the bytes.
    """
    if header_msg_id:
        return header_msg_id.strip().strip("<>")
    h = hashlib.sha256(raw).hexdigest()[:32]
    return f"imap-uid{uid}-{h}"


def imap_message_to_row(
    raw: bytes,
    *,
    uid: int,
    mailbox_id: str,
    account_email: str,
    folder: str = "inbox",
) -> Optional[dict]:
    """
    Parse an RFC 822 byte string into an EmailStore row.

    Returns None on completely-unparseable garbage so the caller can skip and
    continue processing other UIDs.
    """
    try:
        msg: EmailMessage = email.message_from_bytes(raw, policy=email.policy.default)  # type: ignore[assignment]
    except Exception as e:
        logger.warning("IMAP parse failed for uid=%s: %s", uid, e)
        return None

    from_name, from_email = _parse_address(_get_header(msg, "From"))
    to_list = _parse_addresses(_get_header(msg, "To"))
    cc_list = _parse_addresses(_get_header(msg, "Cc"))
    bcc_list = _parse_addresses(_get_header(msg, "Bcc"))

    subject = (_get_header(msg, "Subject") or "").strip() or None
    sent_at = _parse_date(_get_header(msg, "Date"))
    recv_at = sent_at  # IMAP doesn't reliably expose server-arrival; use Date

    body_text, html_body = _extract_bodies(msg)
    body_html_hash = (
        hashlib.sha256(html_body.encode("utf-8", errors="replace")).hexdigest()
        if html_body else None
    )

    attachment_names = _extract_attachment_names(msg)
    has_attach = 1 if attachment_names else 0

    internet_msg_id = _get_header(msg, "Message-ID")
    if internet_msg_id:
        internet_msg_id = internet_msg_id.strip().strip("<>")

    # IMAP has no native conversation thread id; In-Reply-To / References give
    # us a heuristic. Use the *root* of the References chain when present.
    refs = _get_header(msg, "References") or ""
    in_reply_to = _get_header(msg, "In-Reply-To") or ""
    conv_id: Optional[str] = None
    if refs.strip():
        first = refs.strip().split()[0].strip().strip("<>")
        if first:
            conv_id = first
    elif in_reply_to.strip():
        conv_id = in_reply_to.strip().strip("<>")
    if not conv_id:
        conv_id = internet_msg_id  # standalone message → its own thread

    return {
        "mailbox_id":        mailbox_id,
        "account_email":     account_email,
        "provider":          "imap",
        "provider_msg_id":   _stable_msg_id(raw, internet_msg_id, uid),
        "internet_msg_id":   internet_msg_id,
        "conversation_id":   conv_id,
        "subject":           subject,
        "from_name":         from_name,
        "from_email":        from_email,
        "to_emails":         json.dumps(to_list),
        "cc_emails":         json.dumps(cc_list),
        "bcc_emails":        json.dumps(bcc_list),
        "body_text":         body_text or "",
        "body_html_hash":    body_html_hash,
        "has_attachments":   has_attach,
        "attachment_names":  json.dumps(attachment_names),
        "folder":            folder,
        "is_read":           0,
        "importance":        "normal",
        "sent_at":           sent_at,
        "received_at":       recv_at,
        "ingested_at":       time.time(),
    }


def _get_header(msg: EmailMessage, name: str) -> Optional[str]:
    """Return header value as a plain string; tolerate decode errors."""
    try:
        v = msg.get(name)
        if v is None:
            return None
        return str(v)
    except Exception:
        return None
