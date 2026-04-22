"""
Agent tools for the Outlook email source.

All four tools read from the EmailStore backing the registered OutlookSource.
They are registered into the ToolRegistry when at least one email source is
connected at startup.
"""

import json
import logging
from typing import Optional

from app.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


def _fmt_ts(ts: Optional[float]) -> str:
    if not ts:
        return ""
    import time
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts))


def _summarize_row(r: dict) -> dict:
    """Trim an email row for compact tool output."""
    return {
        "id":               r.get("id"),
        "mailbox":          r.get("account_email"),
        "subject":          r.get("subject") or "(no subject)",
        "from":             f"{r.get('from_name') or ''} <{r.get('from_email') or ''}>".strip(),
        "to":               r.get("to_emails") or [],
        "sent_at":          _fmt_ts(r.get("sent_at")),
        "folder":           r.get("folder"),
        "has_attachments":  bool(r.get("has_attachments")),
        "attachments":      r.get("attachment_names") or [],
        "conversation_id":  r.get("conversation_id"),
        "preview":          r.get("preview"),
    }


class ListMailboxesTool(BaseTool):
    name = "list_mailboxes"
    description = (
        "List the email mailboxes OptiFlow has indexed for the company. "
        "Returns one entry per active mailbox with message count and last sync time. "
        "Use this when the user asks who is covered, or to validate a mailbox= filter "
        "before calling search_emails."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(self, store):
        self._store = store

    async def execute(self, input: dict) -> ToolResult:
        rows = self._store.list_mailboxes(active_only=True)
        compact = [
            {
                "mailbox":        r["account_email"],
                "display_name":   r.get("display_name"),
                "message_count":  r.get("message_count", 0),
                "last_sync":      _fmt_ts(r.get("last_sync_at")),
                "initial_synced": bool(r.get("initial_synced")),
                "backfill_done":  bool(r.get("backfill_done")),
            }
            for r in rows
        ]
        text = json.dumps({"mailboxes": compact, "count": len(compact)}, indent=2)
        return ToolResult(
            tool_call_id="",
            content=text,
            metadata={"mailbox_count": len(compact)},
        )


class SearchEmailsTool(BaseTool):
    name = "search_emails"
    description = (
        "Search indexed company email using BM25 full-text ranking. "
        "Provide 2-6 keyword variants (synonyms, abbreviations, exact IDs). "
        "Optional filters: mailbox (exact address), sender (name or address substring), "
        "recipient (substring), date_range ('last_7_days' | 'last_30_days' | "
        "'YYYY-MM-DD..YYYY-MM-DD'), folder, has_attachments. "
        "Returns a ranked list with preview snippets. Do NOT invent email ids."
    )
    parameters = {
        "type": "object",
        "properties": {
            "keywords":         {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "mailbox":          {"type": "string"},
            "sender":           {"type": "string"},
            "recipient":        {"type": "string"},
            "date_range":       {"type": "string"},
            "folder":           {"type": "string"},
            "has_attachments":  {"type": "boolean"},
            "limit":            {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
        },
        "required": ["keywords"],
        "additionalProperties": False,
    }

    def __init__(self, store):
        self._store = store

    async def execute(self, input: dict) -> ToolResult:
        kws = input.get("keywords") or []
        if not isinstance(kws, list) or not kws:
            return ToolResult(
                tool_call_id="",
                content="search_emails requires a non-empty 'keywords' array.",
                is_error=True,
            )
        try:
            rows = self._store.search(
                keywords=[str(k) for k in kws],
                mailbox=input.get("mailbox"),
                sender=input.get("sender"),
                recipient=input.get("recipient"),
                date_range=input.get("date_range"),
                folder=input.get("folder"),
                has_attachments=input.get("has_attachments"),
                limit=int(input.get("limit") or 10),
            )
        except Exception as e:
            logger.exception("search_emails failed")
            return ToolResult(
                tool_call_id="",
                content=f"Search failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        results = [_summarize_row(r) for r in rows]
        text = json.dumps({"results": results, "count": len(results)}, indent=2)
        return ToolResult(
            tool_call_id="",
            content=text,
            metadata={"result_count": len(results)},
        )


class GetEmailTool(BaseTool):
    name = "get_email"
    description = (
        "Fetch the full plain-text body and metadata of a single email by its "
        "internal id (returned by search_emails). Use when you need the complete "
        "message to answer — do not paraphrase from the preview snippet alone."
    )
    parameters = {
        "type": "object",
        "properties": {"email_id": {"type": "integer"}},
        "required": ["email_id"],
        "additionalProperties": False,
    }

    def __init__(self, store):
        self._store = store

    async def execute(self, input: dict) -> ToolResult:
        eid = input.get("email_id")
        try:
            eid_int = int(eid)
        except (TypeError, ValueError):
            return ToolResult(tool_call_id="", content="email_id must be an integer.", is_error=True)
        row = self._store.get_email(eid_int)
        if not row:
            return ToolResult(
                tool_call_id="",
                content=f"No email found with id={eid_int}.",
                is_error=True,
            )
        payload = {
            "id":               row.get("id"),
            "mailbox":          row.get("account_email"),
            "subject":          row.get("subject"),
            "from":             f"{row.get('from_name') or ''} <{row.get('from_email') or ''}>".strip(),
            "to":               row.get("to_emails") or [],
            "cc":               row.get("cc_emails") or [],
            "sent_at":          _fmt_ts(row.get("sent_at")),
            "received_at":      _fmt_ts(row.get("received_at")),
            "folder":           row.get("folder"),
            "importance":       row.get("importance"),
            "has_attachments":  bool(row.get("has_attachments")),
            "attachments":      row.get("attachment_names") or [],
            "conversation_id":  row.get("conversation_id"),
            "body_text":        row.get("body_text") or "",
        }
        return ToolResult(tool_call_id="", content=json.dumps(payload, indent=2))


class GetEmailThreadTool(BaseTool):
    name = "get_email_thread"
    description = (
        "Fetch every message in a conversation/thread, oldest to newest, for a "
        "given conversation_id (returned by search_emails or get_email)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "conversation_id": {"type": "string"},
            "mailbox":         {"type": "string", "description": "Optional: limit to one mailbox's copy of the thread."},
        },
        "required": ["conversation_id"],
        "additionalProperties": False,
    }

    def __init__(self, store):
        self._store = store

    async def execute(self, input: dict) -> ToolResult:
        conv_id = input.get("conversation_id")
        if not conv_id:
            return ToolResult(tool_call_id="", content="conversation_id required.", is_error=True)
        mailbox_filter = input.get("mailbox")
        # Thread query wants mailbox_id; we have account_email — resolve via store.
        mailbox_id = None
        if mailbox_filter:
            for mb in self._store.list_mailboxes(active_only=False):
                if mb.get("account_email", "").lower() == mailbox_filter.lower():
                    mailbox_id = mb["id"]
                    break
        rows = self._store.get_thread(conv_id, mailbox_id=mailbox_id)
        messages = []
        for r in rows:
            messages.append({
                "id":          r.get("id"),
                "mailbox":     r.get("account_email"),
                "subject":     r.get("subject"),
                "from":        f"{r.get('from_name') or ''} <{r.get('from_email') or ''}>".strip(),
                "sent_at":     _fmt_ts(r.get("sent_at")),
                "body_text":   r.get("body_text") or "",
            })
        text = json.dumps({"conversation_id": conv_id, "messages": messages, "count": len(messages)}, indent=2)
        return ToolResult(tool_call_id="", content=text)


def register_email_tools(registry, store) -> None:
    """
    Register all four email tools into a ToolRegistry, bound to the given EmailStore.

    Call this from startup after the OutlookSource is initialized.
    """
    registry.register(ListMailboxesTool(store))
    registry.register(SearchEmailsTool(store))
    registry.register(GetEmailTool(store))
    registry.register(GetEmailThreadTool(store))
    logger.info("Registered 4 email tools (list_mailboxes, search_emails, get_email, get_email_thread)")
