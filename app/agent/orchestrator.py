"""
Agent orchestrator — core autonomous ReAct loop.

AgentOrchestrator
    ask(question, session_id?)        → AgentResponse  (blocks until done)
    ask_stream(question, session_id?) → AsyncGenerator[dict, None]  (SSE-friendly)

The system prompt is built dynamically on each request from the live SourceRegistry,
so source changes (add/remove) take effect without restart.

Event types emitted by ask_stream:
    {"type": "status",      "message": "..."}
    {"type": "thinking",    "content": "..."}
    {"type": "tool_call",   "tool": "...", "input": {...}}
    {"type": "tool_result", "tool": "...", "result_summary": "...", "is_error": bool}
    {"type": "answer",      "content": "...", "session_id": "...",
                            "iterations": N, "tools_used": [...], "queries_executed": N}
    {"type": "error",       "message": "...", "retry_after"?: N}
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import AsyncGenerator, Optional
from weakref import WeakValueDictionary

logger = logging.getLogger(__name__)

_MAX_ITERATIONS = 15

_COMPANY_MD_CACHE: dict = {"mtime": 0.0, "content": ""}


class _SessionBusyError(RuntimeError):
    """Raised when a second request arrives for a session already in flight."""
    pass

_THINKING_RE = re.compile(r"<thinking>(.*?)</thinking>", re.DOTALL | re.IGNORECASE)
_CHART_BLOCK_RE = re.compile(r"<chart\b[^>]*>(.*?)</chart>", re.DOTALL | re.IGNORECASE)

_CHART_TYPES = ("bar", "line", "pie", "doughnut", "area", "table")
_MAX_CHARTS_PER_TURN = 4

_VISUALISE_INSTRUCTIONS = """\
## VISUALISE MODE — produce charts, not a long narrative

The user has asked for a **visual** answer. After running the SQL you need \
and gathering the data, your final assistant message MUST:

1. Start with a short text explanation (1–3 sentences) — the headline finding. \
No tables of numbers; the chart will show them.
2. Follow with one or more `<chart>` blocks, each containing a JSON spec. \
Example:

```
<chart data="last_query">
{
  "type": "bar",
  "title": "Revenue by month",
  "x": "month",
  "y": "revenue",
  "explanation": "Peak in March; dip in July matched the pricing change."
}
</chart>
```

Spec rules (strict — invalid charts are dropped):
- `type` must be one of: `bar`, `line`, `pie`, `doughnut`, `area`, `table`.
- `x` must be a column name from the most recent `execute_sql` result.
- `y` must be a numeric column name, or a JSON array of numeric column names \
(for multi-series charts). For `pie` / `doughnut`, `y` must be a single numeric column.
- `title` is required. `explanation` is optional but encouraged (1 sentence).
- `data` attribute must be exactly `"last_query"` — the server binds the \
rows from the last `execute_sql` automatically. Do NOT inline row data; \
do NOT restate numbers in prose. If you need rows from an earlier query, \
run that query again.
- Emit at most 4 `<chart>` blocks. Prefer one clear chart over many.

If the data can't be charted (single scalar, non-numeric result), return a \
normal text answer WITHOUT any `<chart>` block and briefly say why.
"""


def _extract_chart_blocks(text: str) -> tuple[str, list[dict]]:
    """Pull `<chart>...</chart>` JSON specs out of the final answer.

    Returns (cleaned_text, specs). Invalid JSON blocks are dropped silently
    (they'll also be stripped from the returned text so the user doesn't see
    raw JSON).
    """
    specs: list[dict] = []

    def _parse(match):
        raw = match.group(1).strip()
        # Strip optional ```json fences the model sometimes adds.
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:].lstrip()
        try:
            spec = json.loads(raw)
            if isinstance(spec, dict):
                specs.append(spec)
        except Exception as exc:
            logger.warning(f"[Chart] Failed to parse chart spec: {exc}")
        return ""  # remove the block from the visible text

    cleaned = _CHART_BLOCK_RE.sub(_parse, text)
    # Collapse any blank-line runs left behind by removed blocks.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, specs


def _validate_chart(spec: dict, rows: list[dict]) -> dict | None:
    """Validate a chart spec against actual query rows.

    Returns a normalised spec dict ready for the frontend, or None if the
    spec can't safely be plotted. We intentionally refuse to fall back to
    guessing columns — the chart is only emitted if the spec and the data
    actually match.
    """
    if not isinstance(spec, dict) or not rows:
        return None

    ctype = str(spec.get("type", "")).lower().strip()
    if ctype not in _CHART_TYPES:
        return None

    title = str(spec.get("title", "")).strip()
    if not title:
        return None

    columns = list(rows[0].keys()) if rows else []
    columns_lower = {c.lower(): c for c in columns}

    def _resolve(col: str) -> str | None:
        if not isinstance(col, str):
            return None
        if col in columns:
            return col
        return columns_lower.get(col.lower())

    if ctype == "table":
        # Table: no axis validation. Frontend renders all columns.
        return {
            "type": "table",
            "title": title,
            "explanation": str(spec.get("explanation", "")).strip(),
            "columns": columns,
            "rows": rows,
        }

    x_col = _resolve(spec.get("x"))
    if x_col is None:
        return None

    y_spec = spec.get("y")
    if isinstance(y_spec, str):
        y_cols_raw = [y_spec]
    elif isinstance(y_spec, list):
        y_cols_raw = [c for c in y_spec if isinstance(c, str)]
    else:
        return None

    y_cols: list[str] = []
    for y in y_cols_raw:
        resolved = _resolve(y)
        if resolved and resolved != x_col:
            y_cols.append(resolved)
    if not y_cols:
        return None

    # Pie / doughnut take a single series only.
    if ctype in ("pie", "doughnut"):
        y_cols = y_cols[:1]

    # Numeric check: at least one sampled row per y-column must coerce to
    # a number. If a column is entirely non-numeric, drop the chart rather
    # than plot nonsense.
    def _is_numeric(v) -> bool:
        if isinstance(v, bool):
            return False
        if isinstance(v, (int, float)):
            return True
        if isinstance(v, str):
            try:
                float(v.replace(",", ""))
                return True
            except Exception:
                return False
        return False

    for yc in y_cols:
        if not any(_is_numeric(r.get(yc)) for r in rows):
            return None

    # X-axis must have ≥ 2 distinct values for a meaningful chart, except
    # for a 1-row pie/doughnut (e.g. single category breakdown).
    distinct_x = len({r.get(x_col) for r in rows})
    if distinct_x < 2 and ctype not in ("pie", "doughnut"):
        return None

    return {
        "type":        ctype,
        "title":       title,
        "explanation": str(spec.get("explanation", "")).strip(),
        "x":           x_col,
        "y":           y_cols,
        "rows":        rows,
    }


# ── Response model ─────────────────────────────────────────────────────────────

@dataclass
class AgentResponse:
    status: str          # "complete" | "error"
    session_id: str
    answer: Optional[str] = None
    error: Optional[str] = None
    iterations: int = 0
    tools_used: list = field(default_factory=list)
    queries_executed: int = 0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_thinking(text: str) -> tuple[list[str], str]:
    thoughts  = [m.group(1).strip() for m in _THINKING_RE.finditer(text)]
    remaining = _THINKING_RE.sub("", text).strip()
    return thoughts, remaining


def _summarize_result(result) -> str:
    if result.is_error:
        return result.content.split("\n")[0][:100]
    row_count = result.metadata.get("row_count")
    if row_count is not None:
        return f"{row_count} row{'s' if row_count != 1 else ''} returned"
    content = result.content.replace("\n", " ").strip()
    return (content[:80] + "…") if len(content) > 80 else content


def _compress_turn(prior_messages: list, new_messages: list, final_answer: str) -> list:
    """
    Replace the just-completed turn (new_messages, i.e. the messages added this
    question) with a compact 2-message summary before storing in session history.

    Compresses to:
      user:      original question text (stripped of any [System note:...] we injected)
      assistant: [Previous answer] <answer>
                 [Context] Tables: X, Y | SQL: <sql1> ; <sql2>

    prior_messages  = messages that were in the session BEFORE this question
    new_messages    = all messages from this turn (user question + assistant + tool results)
    final_answer    = the text we emitted as the 'answer' event
    """
    if not new_messages:
        return prior_messages

    # ── Extract original user question (strip system note suffix) ────────────
    first_user = new_messages[0]
    raw_question: str = ""
    if isinstance(first_user.get("content"), str):
        raw_question = first_user["content"]
    # Strip injected system note
    for marker in ("\n\n[System note:", "\n\n[System Note:"):
        idx = raw_question.find(marker)
        if idx != -1:
            raw_question = raw_question[:idx]
    raw_question = raw_question.strip()

    # ── Collect SQL statements and table names from this turn ─────────────────
    sql_statements: list[str] = []
    tables_used: list[str]    = []

    for m in new_messages:
        if m.get("role") == "assistant":
            content = m.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    tool_name  = block.get("name", "")
                    tool_input = block.get("input", {}) or {}
                    if tool_name == "execute_sql" and tool_input.get("sql"):
                        sql_statements.append(tool_input["sql"].strip())
                    if tool_name == "get_table_schema":
                        tbls = tool_input.get("tables", [])
                        if isinstance(tbls, list):
                            tables_used.extend(tbls)

    # Deduplicate while preserving order
    seen: set = set()
    tables_deduped: list[str] = []
    for t in tables_used:
        if t not in seen:
            seen.add(t)
            tables_deduped.append(t)

    # ── Build compressed context line ─────────────────────────────────────────
    context_parts: list[str] = []
    if tables_deduped:
        context_parts.append("Tables: " + ", ".join(tables_deduped))
    if sql_statements:
        sql_summary = " ; ".join(s.replace("\n", " ")[:200] for s in sql_statements)
        context_parts.append("SQL: " + sql_summary)
    context_line = " | ".join(context_parts) if context_parts else "No SQL executed"

    # Keep context as a plain annotation — do NOT wrap in <thinking>, because
    # persisted <thinking> blocks get replayed to the model and pollute its
    # context with reasoning it didn't actually do.
    compressed_answer = final_answer.strip()
    if context_line != "No SQL executed":
        compressed_answer = f"[Context from earlier turn — {context_line}]\n\n{compressed_answer}"

    compressed: list = [
        {"role": "user",      "content": raw_question},
        {"role": "assistant", "content": compressed_answer},
    ]
    return prior_messages + compressed


class _ThinkingStripper:
    """Remove literal <thinking>/</thinking> tags from a chunked text stream.

    Whole-tag occurrences are stripped. A trailing partial prefix (e.g. "<th"
    at chunk boundary) is held back until the next chunk so we never emit
    half a tag to the client.
    """
    _TAGS = ("<thinking>", "</thinking>")
    _MAX  = max(len(t) for t in _TAGS)

    def __init__(self):
        self._buf = ""

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        for tag in self._TAGS:
            while tag in self._buf:
                self._buf = self._buf.replace(tag, "", 1)
        hold = 0
        for n in range(1, min(self._MAX, len(self._buf)) + 1):
            suffix = self._buf[-n:]
            if any(t.startswith(suffix) and t != suffix for t in self._TAGS):
                hold = n
        if hold:
            out, self._buf = self._buf[:-hold], self._buf[-hold:]
        else:
            out, self._buf = self._buf, ""
        return out

    def flush(self) -> str:
        out, self._buf = self._buf, ""
        return out


def _strip_tool_blocks(messages: list) -> list:
    """
    Return a copy of `messages` with tool_use and tool_result blocks removed.

    Keeps only:
      - user messages with plain string content (the original questions)
      - assistant messages flattened to their concatenated text blocks

    Tool-result "user" messages (whose content is a list of tool_result blocks)
    are dropped entirely — they carry no user intent.

    This is the authoritative history-trimming pass. All persistence paths
    (happy, error, rate-limit, force-final) route through this so prior turns
    never ship their tool payloads back to the LLM on the next request.
    """
    pruned: list = []
    for m in messages:
        role    = m.get("role")
        content = m.get("content")

        if role == "user":
            # Plain string → original user question. Keep.
            if isinstance(content, str):
                if content.strip():
                    pruned.append({"role": "user", "content": content})
            # List → this is a tool-result batch. Drop entirely.
            continue

        if role == "assistant":
            # Flatten to text
            if isinstance(content, str):
                if content.strip():
                    pruned.append({"role": "assistant", "content": content})
                continue
            if isinstance(content, list):
                text_parts = [
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                joined = "\n\n".join(p for p in text_parts if p).strip()
                if joined:
                    pruned.append({"role": "assistant", "content": joined})
    return pruned


def _tool_id_is_list_tables(tool_use_id: str, messages: list) -> bool:
    """Return True if the given tool_use_id corresponds to a list_tables call."""
    for m in messages:
        if m.get("role") != "assistant":
            continue
        content = m.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if (isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("id") == tool_use_id
                    and block.get("name") == "list_tables"):
                return True
    return False


def _content_to_list(content_blocks) -> list[dict]:
    result = []
    for block in content_blocks:
        btype = getattr(block, "type", None)
        if btype == "text":
            result.append({"type": "text", "text": block.text})
        elif btype == "tool_use":
            result.append({
                "type":  "tool_use",
                "id":    block.id,
                "name":  block.name,
                "input": dict(block.input),
            })
    return result


# ── Orchestrator ──────────────────────────────────────────────────────────────

class AgentOrchestrator:
    """
    Runs the autonomous ReAct agent loop.

    Each iteration:
      1. Build system prompt dynamically from live SourceRegistry
      2. Call LLM with tool definitions + full message history
      3. Parse text blocks → extract <thinking> tags → emit as "thinking" events
      4. If stop_reason == "tool_use":
           emit tool_call → execute → emit tool_result → loop
      5. If stop_reason == "end_turn":
           emit "answer" event → done
    """

    def __init__(
        self,
        ai_client,
        tool_registry,
        source_registry,
        sessions,
        max_iterations: int = _MAX_ITERATIONS,
    ):
        self._ai       = ai_client
        self._registry = tool_registry
        self._sources  = source_registry
        self._sessions = sessions
        self._max_iter = max_iterations
        # Per-session locks so two concurrent requests on the same session_id
        # don't race and corrupt history. WeakValueDictionary lets unused
        # locks get garbage-collected once no coroutine holds a reference.
        self._session_locks: WeakValueDictionary = WeakValueDictionary()
        self._locks_guard: asyncio.Lock = asyncio.Lock()

    async def _try_acquire_session_lock(self, session_id: str) -> asyncio.Lock | None:
        """Atomically look up or create the session lock and acquire it.

        Returns the held lock, or None if another request is already holding
        it. The check-and-acquire happens under `_locks_guard` so two
        requests for the same session can't both pass the `locked()` check.
        """
        async with self._locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._session_locks[session_id] = lock
            if lock.locked():
                return None
            # Lock is free; acquire it while we still hold the guard so no
            # other coroutine can sneak in. acquire() on a free lock returns
            # immediately without yielding.
            await lock.acquire()
            return lock

    def _build_system_prompt(self) -> str:
        from app.agent.prompts import SYSTEM_PROMPT
        from app.config import COMPANY_MD_PATH

        prompt = SYSTEM_PROMPT
        now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
        prompt += (
            "\n\n## Runtime Context\n\n"
            f"Today is `{now_ist.strftime('%Y-%m-%d')}`.\n"
            f"Current local datetime is `{now_ist.strftime('%Y-%m-%d %H:%M:%S %Z')}`.\n"
            "Interpret relative dates such as today, yesterday, this month, last 7 days, "
            "and last 10 days using this date and timezone unless the user specifies otherwise."
        )

        # Tell the agent how many sources are connected (names come from list_tables)
        sources = self._sources.get_all()
        if len(sources) == 1:
            s = sources[0]
            prompt += (
                f"\n\n## Connected Database\n\n"
                f"Source: `{s.name}` | Type: {s.get_db_type().upper()} | "
                f"Database: {s.get_database_name()}\n"
                "Call `list_tables` to get the full schema directory, relationships, and dialect rules."
            )
        elif len(sources) > 1:
            prompt += "\n\n## Connected Databases\n\n"
            for s in sources:
                prompt += f"- `{s.name}` ({s.get_db_type().upper()}, db: {s.get_database_name()})\n"
            prompt += "\nCall `list_tables(source=<name>)` for each source you need to query."

        # Append company knowledge — the agent's domain map.
        # Cached in memory with mtime invalidation so we don't re-read + re-parse
        # the file on every question.
        try:
            if COMPANY_MD_PATH.exists():
                mtime = COMPANY_MD_PATH.stat().st_mtime
                if _COMPANY_MD_CACHE["mtime"] != mtime:
                    _COMPANY_MD_CACHE["mtime"]   = mtime
                    _COMPANY_MD_CACHE["content"] = COMPANY_MD_PATH.read_text(encoding="utf-8").strip()
                content = _COMPANY_MD_CACHE["content"]
                if content:
                    prompt += f"\n\n## Business Context\n\n{content}"
        except Exception:
            pass

        return prompt

    # ── Non-streaming ─────────────────────────────────────────────────────────

    async def ask(
        self,
        question: str,
        session_id: Optional[str] = None,
    ) -> AgentResponse:
        """Run the agent loop to completion and return an AgentResponse."""
        sid = self._sessions.get_or_create(session_id)

        events: list[dict] = []
        async for event in self.ask_stream(question, sid):
            events.append(event)

        for event in reversed(events):
            etype = event.get("type")
            if etype == "answer":
                return AgentResponse(
                    status="complete",
                    session_id=sid,
                    answer=event.get("content"),
                    iterations=event.get("iterations", 0),
                    tools_used=event.get("tools_used", []),
                    queries_executed=event.get("queries_executed", 0),
                )
            if etype == "error":
                return AgentResponse(
                    status="error",
                    session_id=sid,
                    error=event.get("message", "Unknown error"),
                )

        return AgentResponse(status="error", session_id=sid, error="Agent produced no response.")

    # ── Streaming ─────────────────────────────────────────────────────────────

    async def ask_stream(
        self,
        question: str,
        session_id: Optional[str] = None,
        visualise: bool = False,
    ) -> AsyncGenerator[dict, None]:
        """Run the ReAct loop and yield SSE-friendly progress events.

        When ``visualise`` is True, the agent is prompted to emit one or more
        ``<chart>`` JSON blocks inside its final answer. The orchestrator
        extracts those blocks, validates them, binds them to the actual rows
        returned by ``execute_sql`` (so numbers can't be hallucinated), and
        yields a ``chart`` SSE event per valid spec.
        """
        from app.ai.client import RateLimitExhausted

        session_id = self._sessions.get_or_create(session_id)

        # ── Per-session lock ──────────────────────────────────────────────────
        # Two concurrent requests on the same session_id would race to mutate
        # and persist history. Reject the second one with a clear error.
        lock = await self._try_acquire_session_lock(session_id)
        if lock is None:
            yield {
                "type":    "error",
                "message": (
                    "Another request is already in progress for this session. "
                    "Wait for it to finish or start a new session."
                ),
            }
            return

        try:
            async for event in self._ask_stream_locked(
                question, session_id, visualise=visualise
            ):
                yield event
        finally:
            if lock.locked():
                lock.release()

    async def _ask_stream_locked(
        self,
        question: str,
        session_id: str,
        visualise: bool = False,
    ) -> AsyncGenerator[dict, None]:
        from app.ai.client import RateLimitExhausted

        # Always strip on load — legacy sessions may contain tool_use/tool_result
        # blocks from before the pruning pass was added. Trim now so they never
        # bloat the next LLM request.
        prior_messages = _strip_tool_blocks(self._sessions.get_messages(session_id))
        messages       = list(prior_messages)                       # working copy

        # ── Follow-up hint: only needed on the first follow-up ────────────────
        # After one reminder the model carries the behavior forward from context,
        # so we inject the hint only on turn 2 (exactly one prior turn). Always
        # restate the <thinking> requirement on every follow-up because the UI
        # streaming depends on it.
        prior_turn_count  = sum(1 for m in messages if m.get("role") == "user")
        user_content: str = question
        if prior_turn_count == 1:
            user_content = (
                question
                + "\n\n[Follow-up: skip list_tables (already done earlier). "
                "Begin with a <thinking> block, then call get_table_schema for "
                "the specific tables you need.]"
            )
        elif prior_turn_count >= 2:
            user_content = question + "\n\n[Begin with a <thinking> block.]"

        # ── Visualise nudge: attach on every turn where chart mode is on ────
        # The system prompt carries the full spec rules, but the model tends
        # to pattern-match to the *conversation* — if prior turns were text,
        # it often keeps answering in text. A short inline reminder on the
        # current user turn is the most reliable way to get a chart block.
        if visualise:
            user_content = (
                user_content
                + "\n\n[VISUALISE MODE: the user toggled Chart format for this "
                "question. Your final message MUST include at least one "
                "<chart data=\"last_query\"> JSON block (see system instructions "
                "for the full spec), unless the result is a single scalar or "
                "truly non-chartable — in which case briefly explain why in "
                "text. Do not restate the numbers in prose; let the chart show "
                "them.]"
            )

        messages.append({"role": "user", "content": user_content})

        system           = self._build_system_prompt()
        if visualise:
            system = system + "\n\n" + _VISUALISE_INSTRUCTIONS
        tools_used: list = []
        queries_executed = 0
        iteration        = 0

        # Chart-mode bookkeeping. When visualise is on we keep the rows returned
        # by every execute_sql call during this turn (keyed by tool_use_id),
        # plus a pointer to the last SQL call. Chart specs bind to rows here
        # — the model never re-serializes the numbers, so they can't drift.
        sql_rows_by_id: dict[str, list[dict]] = {}
        last_sql_tool_id: str | None          = None

        yield {"type": "status", "message": "Starting analysis…"}

        try:
            while iteration < self._max_iter:
                iteration += 1
                logger.info(f"[Agent] Iteration {iteration}")
                yield {"type": "status", "message": f"Thinking… (step {iteration})"}

                # ── Two iterations before limit, remove tools to force answer ─
                force_final = (iteration >= self._max_iter - 2)
                call_tools  = None if force_final else self._registry.get_api_definitions()

                # On force_final, override the system prompt's "always write a
                # <thinking> block" rule. Otherwise the model sometimes wraps
                # its entire response inside <thinking>, the stripper removes
                # it, and the user sees an empty answer.
                call_system = system
                if force_final:
                    logger.info("[Agent] Forcing final answer — tools disabled")
                    yield {"type": "status", "message": "Composing final answer…"}
                    call_system = (
                        system
                        + "\n\n## FINAL ANSWER MODE\n\n"
                        "Write the final answer to the user NOW using the data "
                        "already gathered.\n"
                        "- DO NOT write a <thinking> block. Write the answer directly.\n"
                        "- DO NOT call any tools.\n"
                        "- Lead with the key number or finding. Be concrete."
                    )

                # ── LLM call (streaming) ──────────────────────────────────────
                response    = None
                stripper    = _ThinkingStripper()
                thinking_open = False

                try:
                    async for evt in self._ai.complete_stream(
                        messages=messages,
                        system=call_system,
                        tools=call_tools,
                    ):
                        et = evt.get("type")
                        if et == "text_delta":
                            clean = stripper.feed(evt["text"])
                            if clean:
                                if not thinking_open:
                                    yield {"type": "thinking_start"}
                                    thinking_open = True
                                yield {"type": "thinking_delta", "delta": clean}
                        elif et == "tool_use_start":
                            # Close any open streaming thinking block before the tool runs
                            if thinking_open:
                                trailing = stripper.flush()
                                if trailing:
                                    yield {"type": "thinking_delta", "delta": trailing}
                                yield {"type": "thinking_end"}
                                thinking_open = False
                        elif et == "rate_limit_wait":
                            # Close any open thinking block so the UI can swap to
                            # the rate-limit notice without a stale cursor.
                            if thinking_open:
                                trailing = stripper.flush()
                                if trailing:
                                    yield {"type": "thinking_delta", "delta": trailing}
                                yield {"type": "thinking_end"}
                                thinking_open = False
                            yield {
                                "type":         "rate_limit_wait",
                                "wait_seconds": evt.get("wait_seconds", 30),
                                "attempt":      evt.get("attempt", 1),
                                "max_attempts": evt.get("max_attempts", 3),
                            }
                        elif et == "rate_limit_tick":
                            yield {
                                "type":      "rate_limit_tick",
                                "remaining": evt.get("remaining", 0),
                            }
                        elif et == "rate_limit_resume":
                            yield {"type": "rate_limit_resume"}
                            # Fresh stripper for the retried stream so a half-tag
                            # from the aborted attempt doesn't leak into output.
                            stripper = _ThinkingStripper()
                        elif et == "final_message":
                            response = evt["message"]
                except RateLimitExhausted as rl:
                    if thinking_open:
                        yield {"type": "thinking_end"}
                    self._sessions.set_messages(session_id, _strip_tool_blocks(prior_messages))
                    yield {
                        "type":        "error",
                        "message":     f"Rate limit hit. Retry after {rl.retry_after}s.",
                        "retry_after": rl.retry_after,
                    }
                    return
                except Exception as exc:
                    if thinking_open:
                        yield {"type": "thinking_end"}
                    self._sessions.set_messages(session_id, _strip_tool_blocks(prior_messages))
                    logger.exception("[Agent] LLM stream failed")
                    yield {
                        "type":    "error",
                        "message": "The AI provider returned an error. Please try again.",
                        "detail":  type(exc).__name__,
                    }
                    return

                # Flush any trailing buffered chars (e.g. partial tag suffix left over)
                if thinking_open:
                    trailing = stripper.flush()
                    if trailing:
                        yield {"type": "thinking_delta", "delta": trailing}
                    yield {"type": "thinking_end"}
                    thinking_open = False

                if response is None:
                    self._sessions.set_messages(session_id, _strip_tool_blocks(prior_messages))
                    yield {"type": "error", "message": "Stream ended without a final message."}
                    return

                # ── Parse content blocks ───────────────────────────────────────
                text_remainder: list[str] = []
                tool_blocks:    list      = []

                for block in response.content:
                    btype = getattr(block, "type", None)
                    if btype == "text":
                        _thoughts, remaining = _extract_thinking(block.text)
                        for thought in _thoughts:
                            if thought:
                                logger.info(f"[Agent] Thinking: {thought[:300]}")
                        if remaining:
                            text_remainder.append(remaining)
                    elif btype == "tool_use":
                        logger.info(
                            f"[Agent] Tool call: {block.name} -> "
                            f"{json.dumps(dict(block.input))[:300]}"
                        )
                        tool_blocks.append(block)

                # ── Append assistant turn to history ───────────────────────────
                messages.append({"role": "assistant", "content": _content_to_list(response.content)})

                stop_reason = response.stop_reason

                # ── Done ──────────────────────────────────────────────────────
                if stop_reason == "end_turn":
                    answer = "\n\n".join(text_remainder).strip()

                    # Visualise mode: strip out any <chart> blocks from the
                    # visible answer and emit them as separate chart events
                    # bound to the actual SQL rows.
                    chart_events: list[dict] = []
                    if visualise:
                        answer, chart_specs = _extract_chart_blocks(answer)
                        if chart_specs and not sql_rows_by_id:
                            logger.warning(
                                "[Chart] Model emitted chart specs but no "
                                "execute_sql result was captured this turn."
                            )
                        for spec in chart_specs[:_MAX_CHARTS_PER_TURN]:
                            # Resolve which query's rows to use. Default to
                            # last_query; allow an explicit tool_use_id only
                            # if we actually have it.
                            data_ref = str(spec.get("data", "last_query")).strip()
                            rows: list[dict] = []
                            if data_ref == "last_query" and last_sql_tool_id:
                                rows = sql_rows_by_id.get(last_sql_tool_id, [])
                            elif data_ref in sql_rows_by_id:
                                rows = sql_rows_by_id[data_ref]
                            elif last_sql_tool_id:
                                rows = sql_rows_by_id.get(last_sql_tool_id, [])

                            validated = _validate_chart(spec, rows)
                            if validated:
                                chart_events.append(validated)
                            else:
                                logger.info(
                                    f"[Chart] Dropped invalid spec: "
                                    f"type={spec.get('type')!r} x={spec.get('x')!r} "
                                    f"y={spec.get('y')!r}"
                                )

                    logger.info(
                        f"[Agent] Final answer: {answer[:300]} "
                        f"({len(chart_events)} chart(s))"
                    )

                    # Fail loud on empty answer. Correct answer or explicit
                    # error — never a silent empty bubble. This happens when
                    # the model wraps its whole response in <thinking> and
                    # the stripper removes everything.
                    # In visualise mode, a chart alone is a valid answer, so
                    # only bail if there's no text AND no charts.
                    if not answer and not chart_events:
                        logger.warning(
                            "[Agent] end_turn with empty answer — rolling back turn"
                        )
                        self._sessions.set_messages(
                            session_id, _strip_tool_blocks(prior_messages)
                        )
                        yield {
                            "type":    "error",
                            "message": (
                                "The agent finished without producing an answer. "
                                "This usually clears up on retry."
                            ),
                        }
                        return

                    # Compress this turn: replace full tool call/result chains
                    # with a compact 2-message summary to keep context lean.
                    # Note: persist only the text answer — charts are a
                    # UI-only artifact and should not be replayed to the LLM.
                    new_messages = messages[len(prior_messages):]
                    answer_for_history = answer or "(chart-only answer)"
                    compressed   = _compress_turn(
                        prior_messages, new_messages, answer_for_history
                    )
                    # Defense-in-depth: strip any stray tool_use/tool_result blocks
                    # before persisting. prior_messages is already clean from the
                    # previous turn, but compressed may contain inline tool blocks
                    # if _compress_turn ever changes.
                    self._sessions.set_messages(session_id, _strip_tool_blocks(compressed))

                    # Emit chart events before the answer so the UI can render
                    # them in order and the final `answer` event signals "done".
                    # Note: the chart dict has its own `type` field (bar/line/…),
                    # so nest it under `spec` to keep the SSE event's own
                    # `type: "chart"` discriminator intact.
                    for ch in chart_events:
                        yield {"type": "chart", "spec": ch}

                    # ── Persist the UI-facing transcript ──────────────────────
                    # Store a compact, UI-shaped record of this turn so the
                    # sidebar and "resume conversation" features can replay
                    # it exactly as it originally rendered. The LLM never
                    # sees this log — it's purely for the frontend.
                    now_ts = time.time()
                    badges = [
                        f"{queries_executed} quer"
                        f"{'y' if queries_executed == 1 else 'ies'}",
                        f"{iteration} step"
                        f"{'' if iteration == 1 else 's'}",
                    ]
                    if chart_events:
                        badges.append(
                            f"{len(chart_events)} chart"
                            f"{'' if len(chart_events) == 1 else 's'}"
                        )
                    user_entry = {
                        "role": "user",
                        "text": question,
                        "ts":   now_ts,
                    }
                    ai_entry: dict = {
                        "role":   "ai",
                        "text":   answer,
                        "ts":     now_ts,
                        "badges": badges,
                    }
                    if chart_events:
                        ai_entry["charts"] = chart_events
                    try:
                        self._sessions.append_display_entries(
                            session_id,
                            [user_entry, ai_entry],
                            first_user_text_if_empty=question,
                        )
                    except Exception:
                        # The display log is best-effort — never fail a turn
                        # because we couldn't persist the UI transcript.
                        logger.exception(
                            "[Agent] Failed to append display log"
                        )

                    yield {
                        "type":             "answer",
                        "content":          answer,
                        "session_id":       session_id,
                        "iterations":       iteration,
                        "tools_used":       tools_used,
                        "queries_executed": queries_executed,
                        "chart_count":      len(chart_events),
                    }
                    return

                # ── Tool calls ────────────────────────────────────────────────
                if stop_reason == "tool_use":
                    tool_results: list[dict] = []

                    for block in tool_blocks:
                        tool_name  = block.name
                        tool_id    = block.id
                        tool_input = dict(block.input)

                        tools_used.append(tool_name)
                        yield {"type": "tool_call", "tool": tool_name, "input": tool_input}

                        result = await self._registry.execute(tool_name, tool_id, tool_input)

                        if tool_name == "execute_sql":
                            queries_executed += 1
                            # Capture the rows so visualise mode can bind
                            # charts to real data (never re-serialised by
                            # the LLM). Only tracked when we're actually
                            # going to use them, to save memory.
                            if visualise and not result.is_error:
                                rows = (result.metadata or {}).get("rows_for_chart") or []
                                if rows:
                                    sql_rows_by_id[tool_id] = rows
                                    last_sql_tool_id = tool_id

                        logger.info(
                            f"[Agent] Tool result ({tool_name}): {result.content[:300]}"
                        )

                        yield {
                            "type":           "tool_result",
                            "tool":           tool_name,
                            "result_summary": _summarize_result(result),
                            "is_error":       result.is_error,
                        }

                        tr: dict = {
                            "type":        "tool_result",
                            "tool_use_id": tool_id,
                            "content":     result.content,
                        }
                        if result.is_error:
                            tr["is_error"] = True
                        tool_results.append(tr)

                    messages.append({"role": "user", "content": tool_results})
                    continue

                # ── Unexpected stop reason ────────────────────────────────────
                logger.warning(f"[AgentOrchestrator] Unexpected stop_reason={stop_reason!r}")
                self._sessions.set_messages(session_id, _strip_tool_blocks(prior_messages))
                yield {
                    "type":    "error",
                    "message": f"Agent stopped unexpectedly (reason: {stop_reason}).",
                }
                return

            # Max iterations reached (force_final should have caught this, but just in case)
            self._sessions.set_messages(session_id, _strip_tool_blocks(prior_messages))
            yield {
                "type":    "error",
                "message": "Agent reached the step limit without a final answer. Try a more specific question.",
            }

        except (asyncio.CancelledError, GeneratorExit):
            # Client disconnected mid-stream (Stop button, closed tab, dropped
            # connection). Roll the session back to its pre-question state so
            # the next request doesn't see a dangling assistant tool_use block
            # with no matching tool_result.
            logger.info(
                f"[Agent] Stream cancelled for session {session_id} "
                f"(iteration {iteration}). Rolling back turn."
            )
            try:
                self._sessions.set_messages(
                    session_id, _strip_tool_blocks(prior_messages)
                )
            except Exception:
                logger.exception("[Agent] Failed to roll back session on cancel")
            raise
        except Exception as exc:
            logger.exception("[AgentOrchestrator] Unhandled error in ask_stream")
            self._sessions.set_messages(session_id, _strip_tool_blocks(prior_messages))
            # Don't echo raw exception text to clients — could leak internals.
            # The full exception is already in the server log.
            yield {
                "type":    "error",
                "message": "The agent encountered an unexpected error. Please try again.",
            }
