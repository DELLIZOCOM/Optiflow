"""
Agent tools for any email source.

Provider-agnostic: all four tools read from an `EmailStore` instance, which
is the same shape regardless of which ingestor (Outlook / IMAP / a future
Gmail connector) populated it. The store gets handed to `register_email_tools`
when the source comes up; adding a new email provider only needs a new
`EmailSource` subclass that writes into `EmailStore` — these tools don't change.
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
    """
    Trim an email row for compact tool output. Includes:
      * `preview`          — BM25 snippet (highlighted), good for "what matched"
      * `body_head`        — first 1500 chars of body, whitespace-normalized
      * `body_truncated`   — True if the full body is longer than what we returned
      * `body_full_length` — total body length so the LLM can decide whether
                             to call get_email for the rest (snippet-trap
                             prevention: the model sees that there's more)
    """
    body_head = (r.get("body_head") or "")
    if body_head:
        body_head = " ".join(body_head.split())
    body_full_len = int(r.get("body_full_length") or len(body_head) or 0)

    out = {
        "id":                r.get("id"),
        "mailbox":           r.get("account_email"),
        "subject":           r.get("subject") or "(no subject)",
        "from":              f"{r.get('from_name') or ''} <{r.get('from_email') or ''}>".strip(),
        "to":                r.get("to_emails") or [],
        "sent_at":           _fmt_ts(r.get("sent_at")),
        "folder":            r.get("folder"),
        "has_attachments":   bool(r.get("has_attachments")),
        "attachments":       r.get("attachment_names") or [],
        "conversation_id":   r.get("conversation_id"),
        "preview":           r.get("preview"),
        "body_head":         body_head,
        "body_full_length":  body_full_len,
        "body_truncated":    body_full_len > len(body_head),
    }
    # When search() runs in conversation-grouped mode (the default), each row
    # represents a whole thread. Surface thread size + last-received so the
    # agent can decide whether to call get_email_thread for the full chain.
    if "thread_message_count" in r:
        out["thread_message_count"] = r.get("thread_message_count")
        out["thread_last_received"] = _fmt_ts(r.get("thread_last_received"))
    return out


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
        "Search indexed company email using BM25 full-text ranking with time-decay "
        "boosting (recent messages outrank old ones at similar relevance). Results "
        "are grouped by conversation by default — one row per thread, with "
        "thread_message_count showing how many messages are in the thread. "
        "Each result includes `preview` (BM25 highlighted snippet, ~12 tokens), "
        "`body_head` (first 1500 chars of the body), `body_full_length`, and "
        "`body_truncated`. **If `body_truncated` is true and you need to extract "
        "specific values (IDs, codes, error variables, line items) — call "
        "`get_email(email_id)` to read the full body before answering.** Snippets "
        "and previews are for relevance, not for verbatim extraction. "
        "Provide 2-6 keyword variants (synonyms, abbreviations, exact IDs). "
        "Optional filters: mailbox (exact address), sender (name or address substring), "
        "recipient (substring), date_range ('last_7_days' | 'last_30_days' | "
        "'YYYY-MM-DD..YYYY-MM-DD'), folder, has_attachments. Set "
        "group_by_conversation=false if you specifically need every matching "
        "message rather than one-per-thread. Do NOT invent email ids."
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
            "group_by_conversation": {
                "type": "boolean",
                "description": "Default true. Set false to get every matching message instead of one row per thread.",
            },
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
                group_by_conversation=bool(input.get("group_by_conversation", True)),
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


class LookupEntityTool(BaseTool):
    """
    Resolve a person/organization name or email address to a canonical
    entity record with all known aliases. The agent should call this BEFORE
    `search_emails(sender=...)` whenever the user names a contact by their
    real-world name (e.g. "Acme Corp", "John Smith") so it gets every email
    address that person uses, not just the most recent one.

    Returns a small JSON record:
        {
          "found": true,
          "entity": {
            "entity_id": 42,
            "kind": "customer" | "vendor" | "employee" | "unknown",
            "display_name": "Acme Corp",
            "company": "Acme Corp Ltd.",
            "confidence": 1.0,
            "emails": ["a@acme.io", "billing@acme.io"]
          },
          "candidates": [...]    # if multiple weak matches
        }
    """

    name = "lookup_entity"
    description = (
        "Resolve a person's name, organization, or email address to a "
        "canonical entity with all known email addresses. Call this when "
        "the user names a contact ('Acme', 'John Smith', 'the supplier') "
        "before searching email — you'll get every alias they've ever used "
        "instead of guessing one address. Cheap (sub-millisecond), safe to "
        "call early in the plan."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Either an email address (exact match) or a display "
                    "name / company name (substring + token match)."
                ),
            },
            "kind": {
                "type": "string",
                "enum": ["customer", "vendor", "employee", "unknown"],
                "description": (
                    "Optional filter — restrict to entities of this kind. "
                    "Omit to search across all kinds."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Max name-match candidates to return (default 5).",
            },
        },
        "required": ["query"],
    }

    def __init__(self, store):
        self._store = store

    async def execute(self, input: dict) -> ToolResult:
        q     = (input.get("query") or "").strip()
        kind  = (input.get("kind") or "").strip().lower() or None
        limit = int(input.get("limit") or 5)
        if not q:
            return ToolResult(
                tool_call_id="",
                content="lookup_entity: query is required.",
                is_error=True,
            )

        store = self._store

        # Email address path → exact match wins outright
        if "@" in q:
            ent = store.find_entity_by_email(q)
            if ent and (kind is None or ent.get("kind") == kind):
                return ToolResult(
                    tool_call_id="",
                    content=json.dumps({"found": True, "match_type": "email", "entity": _strip_entity(ent)}, indent=2),
                )
            # Even if not in entities table, the address itself is a usable answer.
            return ToolResult(
                tool_call_id="",
                content=json.dumps({
                    "found": False,
                    "match_type": "email",
                    "note": "Address not yet linked to an entity; you can still pass it to search_emails(sender=...).",
                    "address": store._norm_email(q),
                }, indent=2),
            )

        # Name path → ranked candidates
        candidates = store.find_entities_by_name(q, limit=max(1, limit))
        if kind:
            candidates = [c for c in candidates if c.get("kind") == kind]

        if not candidates:
            return ToolResult(
                tool_call_id="",
                content=json.dumps({
                    "found": False,
                    "match_type": "name",
                    "query": q,
                    "note": (
                        "No matching entity. Either the contact has not been "
                        "discovered yet (auto-discovery runs on each email "
                        "sync) or the spelling differs. Try search_emails "
                        "with the name as a keyword instead."
                    ),
                }, indent=2),
            )

        primary = candidates[0]
        rest    = [_strip_entity(c) for c in candidates[1:]]
        return ToolResult(
            tool_call_id="",
            content=json.dumps({
                "found":      True,
                "match_type": "name",
                "entity":     _strip_entity(primary),
                "candidates": rest,
            }, indent=2),
        )


def _strip_entity(d: dict) -> dict:
    """Compact entity record for tool output — drops noisy timestamps."""
    return {
        "entity_id":      d.get("entity_id"),
        "kind":           d.get("kind"),
        "display_name":   d.get("display_name"),
        "company":        d.get("company"),
        "notes":          d.get("notes"),
        "confidence":     d.get("confidence"),
        "emails":         [e.get("email_address") for e in (d.get("emails") or [])],
    }


def register_email_tools(registry, store) -> None:
    """
    Register all five email-stack tools into a ToolRegistry, bound to the
    given EmailStore. Provider-agnostic — call from any EmailSource's install
    path (Outlook, IMAP, or any future provider).

    The five tools:
      list_mailboxes, search_emails, get_email, get_email_thread, lookup_entity
    """
    registry.register(ListMailboxesTool(store))
    registry.register(SearchEmailsTool(store))
    registry.register(GetEmailTool(store))
    registry.register(GetEmailThreadTool(store))
    registry.register(LookupEntityTool(store))
    logger.info("Registered 5 email tools "
                "(list_mailboxes, search_emails, get_email, get_email_thread, lookup_entity)")
