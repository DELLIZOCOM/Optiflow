"""
Map a Microsoft Graph message JSON payload to a row dict suitable for
EmailStore.upsert_emails().

This module is pure and sync — no network, no SQL. Unit-test it against
recorded Graph fixtures.
"""

import hashlib
import html
import json
import re
import time
from datetime import datetime
from typing import Optional

_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")


def _parse_iso(dt: Optional[str]) -> float:
    """Graph ISO-8601 → epoch seconds. Returns 0.0 on failure."""
    if not dt:
        return 0.0
    try:
        # Graph uses `2026-04-22T10:31:00Z`
        return datetime.fromisoformat(dt.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _html_to_text(html_body: str) -> str:
    """
    Crude but dependency-free HTML → plain text.
    Good enough for FTS indexing; we're not rendering.
    """
    if not html_body:
        return ""
    # Drop <script> and <style> blocks entirely
    html_body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html_body, flags=re.I | re.S)
    # Replace breaks & paragraph ends with newlines before stripping tags
    html_body = re.sub(r"<(br|/p|/div|/li|/tr)[^>]*>", "\n", html_body, flags=re.I)
    txt = _TAG.sub(" ", html_body)
    txt = html.unescape(txt)
    txt = _WS.sub(" ", txt).strip()
    return txt


def _extract_body_text(body: Optional[dict], preview: Optional[str]) -> str:
    """Prefer plain-text body; fall back to HTML-stripped; fall back to preview."""
    if not body:
        return (preview or "").strip()
    content_type = (body.get("contentType") or "").lower()
    content = body.get("content") or ""
    if content_type == "text":
        return content.strip()
    if content_type == "html":
        return _html_to_text(content) or (preview or "").strip()
    return (content or preview or "").strip()


def _addrs(recipients: Optional[list[dict]]) -> list[str]:
    """Normalize a Graph recipients array to a list of email addresses."""
    if not recipients:
        return []
    out = []
    for r in recipients:
        addr = (r.get("emailAddress") or {}).get("address")
        if addr:
            out.append(addr.lower())
    return out


def _from_parts(msg: dict) -> tuple[Optional[str], Optional[str]]:
    f = msg.get("from") or msg.get("sender") or {}
    ea = f.get("emailAddress") or {}
    return ea.get("name"), (ea.get("address") or "").lower() or None


def _folder_from_parent(parent_folder_id: Optional[str]) -> str:
    # TODO: translate well-known folder ids to human names via /mailFolders.
    # For MVP we index everything into a coarse 'inbox' bucket unless the
    # caller tells us otherwise via the `folder_hint` argument.
    return "inbox"


def graph_to_row(
    msg: dict,
    *,
    mailbox_id: str,
    account_email: str,
    folder_hint: Optional[str] = None,
) -> dict:
    """
    Turn one Graph message payload into an EmailStore row.
    Safe on missing/partial fields — returns sensible defaults.
    """
    from_name, from_email = _from_parts(msg)
    to_list = _addrs(msg.get("toRecipients"))
    cc_list = _addrs(msg.get("ccRecipients"))
    bcc_list = _addrs(msg.get("bccRecipients"))

    body = msg.get("body") or {}
    body_text = _extract_body_text(body, msg.get("bodyPreview"))
    html_content = body.get("content") if (body.get("contentType") or "").lower() == "html" else None
    body_html_hash = hashlib.sha256(html_content.encode("utf-8")).hexdigest() if html_content else None

    has_attach = 1 if msg.get("hasAttachments") else 0
    attachment_names = msg.get("_attachment_names") or []   # caller may pre-populate

    sent_at = _parse_iso(msg.get("sentDateTime"))
    recv_at = _parse_iso(msg.get("receivedDateTime")) or sent_at

    return {
        "mailbox_id":        mailbox_id,
        "account_email":     account_email,
        "provider":          "outlook",
        "provider_msg_id":   msg.get("id"),
        "internet_msg_id":   msg.get("internetMessageId"),
        "conversation_id":   msg.get("conversationId"),
        "subject":           (msg.get("subject") or "").strip() or None,
        "from_name":         from_name,
        "from_email":        from_email,
        "to_emails":         json.dumps(to_list),
        "cc_emails":         json.dumps(cc_list),
        "bcc_emails":        json.dumps(bcc_list),
        "body_text":         body_text,
        "body_html_hash":    body_html_hash,
        "has_attachments":   has_attach,
        "attachment_names":  json.dumps(attachment_names),
        "folder":            folder_hint or _folder_from_parent(msg.get("parentFolderId")),
        "is_read":           1 if msg.get("isRead") else 0,
        "importance":        msg.get("importance") or "normal",
        "sent_at":           sent_at,
        "received_at":       recv_at,
        "ingested_at":       time.time(),
    }


def graph_user_to_mailbox(user: dict) -> Optional[dict]:
    """
    Convert a /users entry to a mailbox row. Returns None for non-mailbox
    accounts (mail == null, typical for guest users / service accounts).
    """
    mail = user.get("mail") or user.get("userPrincipalName")
    if not mail or not user.get("id"):
        return None
    status = "active" if user.get("accountEnabled", True) else "disabled"
    return {
        "id":            user["id"],
        "account_email": mail.lower(),
        "display_name":  user.get("displayName"),
        "status":        status,
        "discovered_at": time.time(),
    }
