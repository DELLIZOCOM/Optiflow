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
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import AsyncGenerator, Optional

logger = logging.getLogger(__name__)

_MAX_ITERATIONS = 15

_COMPANY_MD_CACHE: dict = {"mtime": 0.0, "content": ""}

_THINKING_RE = re.compile(r"<thinking>(.*?)</thinking>", re.DOTALL | re.IGNORECASE)


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

    # Wrap in <thinking> so the model sees the pattern and continues writing
    # thinking blocks for follow-up questions.
    compressed_answer = (
        f"<thinking>\n"
        f"{context_line}\n"
        f"</thinking>\n\n"
        f"{final_answer.strip()}"
    )

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

    def _build_system_prompt(self) -> str:
        """
        Compose the system prompt for this request.

        Provider-agnostic. The static base in prompts.py knows nothing about
        specific vendors. We assemble per-source sections by calling each
        registered source's own `get_system_prompt_section()` — that way,
        adding a new source type (Gmail, Oracle, SQLite, …) only requires
        the source class to implement that method. The orchestrator never
        special-cases by db_type or provider.
        """
        from app.agent.prompts import SYSTEM_PROMPT
        from app.config import COMPANY_MD_PATH

        parts: list[str] = [SYSTEM_PROMPT]

        # ── Connected sources (one-line summary, sorted db before email) ──
        sources = list(self._sources.get_all())
        if sources:
            # Stable order: databases first (so the LLM sees structured data
            # tools first), then email. Inside each group, alphabetic by name.
            def _kind(s) -> int:
                t = (s.get_db_type() or "").lower()
                return 0 if t in ("mssql", "postgresql", "mysql", "sqlite", "oracle") else 1
            sources.sort(key=lambda s: (_kind(s), s.name))

            summary_lines = []
            for s in sources:
                try:
                    desc = (s.description or "").strip().splitlines()[0]
                except Exception:
                    desc = ""
                summary_lines.append(
                    f"- `{s.name}` — {s.source_type.upper()} "
                    f"({s.get_database_name()}). {desc}"
                )
            parts.append("\n\n## Connected sources\n\n" + "\n".join(summary_lines))

            # ── Per-source guidance — each source contributes its own block ──
            # Sources implement get_system_prompt_section() to surface their
            # own tools, dialect rules, and routing tips. The orchestrator
            # just stitches them together; it doesn't know what's inside.
            sections: list[str] = []
            for s in sources:
                try:
                    sec = s.get_system_prompt_section()
                except Exception:
                    sec = ""
                sec = (sec or "").strip()
                if sec:
                    sections.append(sec)
            if sections:
                parts.append("\n\n## Source-specific guidance\n\n" + "\n\n".join(sections))
        else:
            parts.append(
                "\n\n## Connected sources\n\n"
                "_No sources are currently connected. Tell the user to finish "
                "the setup wizard at `/setup` to connect a database or email._"
            )

        # ── Runtime context (date/time) ─────────────────────────────────
        now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
        parts.append(
            "\n\n## Runtime Context\n\n"
            f"Today is `{now_ist.strftime('%Y-%m-%d')}`.\n"
            f"Current local datetime is `{now_ist.strftime('%Y-%m-%d %H:%M:%S %Z')}`.\n"
            "Interpret relative dates such as today, yesterday, this month, "
            "last 7 days, and last 10 days using this date and timezone unless "
            "the user specifies otherwise."
        )

        # ── Business context (cached on mtime) ──────────────────────────
        try:
            if COMPANY_MD_PATH.exists():
                mtime = COMPANY_MD_PATH.stat().st_mtime
                if _COMPANY_MD_CACHE["mtime"] != mtime:
                    _COMPANY_MD_CACHE["mtime"]   = mtime
                    _COMPANY_MD_CACHE["content"] = COMPANY_MD_PATH.read_text(encoding="utf-8").strip()
                content = _COMPANY_MD_CACHE["content"]
                if content:
                    parts.append(f"\n\n## Business Context\n\n{content}")
        except Exception:
            pass

        return "".join(parts)

    # ── Non-streaming ─────────────────────────────────────────────────────────

    async def ask(
        self,
        question: str,
        session_id: Optional[str] = None,
        *,
        visualise: bool = False,
    ) -> AgentResponse:
        """Run the agent loop to completion and return an AgentResponse.

        `visualise` is accepted for forward compatibility with the chart-spec
        feature; today it's surfaced via the route but the loop ignores it.
        """
        sid = self._sessions.get_or_create(session_id)

        events: list[dict] = []
        async for event in self.ask_stream(question, sid, visualise=visualise):
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
        *,
        visualise: bool = False,
    ) -> AsyncGenerator[dict, None]:
        """Run the ReAct loop and yield SSE-friendly progress events.

        When `visualise=True` the `render_chart` tool is included in the
        tool list and the system prompt nudges the model to call it once it
        has the data. When False (default), the chart tool is hidden so the
        model isn't tempted into spurious chart calls on plain text Qs.
        """
        from app.ai.client import RateLimitExhausted

        session_id     = self._sessions.get_or_create(session_id)
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
        #
        # Provider-agnostic: the hint doesn't name any specific tool. The
        # rule "if the user pivots to a different source, you may need to
        # orient again" works for both email-first and database-first sessions.
        prior_turn_count  = sum(1 for m in messages if m.get("role") == "user")
        user_content: str = question
        if prior_turn_count == 1:
            user_content = (
                question
                + "\n\n[Follow-up turn: begin with a <thinking> block. "
                "If this question targets the same source you used last turn, "
                "you can skip the orientation call (e.g. list_tables) and go "
                "straight to schema lookup or query. If it targets a different "
                "source, orient yourself there first.]"
            )
        elif prior_turn_count >= 2:
            user_content = question + "\n\n[Begin with a <thinking> block.]"

        messages.append({"role": "user", "content": user_content})

        system           = self._build_system_prompt()
        if visualise:
            system += (
                "\n\n## Visualisation Mode\n\n"
                "The user asked for a chart. Plan:\n"
                "  1. Query the data with the appropriate tool (execute_sql, "
                "     search_emails, etc.) and inspect the rows.\n"
                "  2. Call `render_chart` exactly once. Pass the rows you "
                "     already retrieved (do NOT re-query). Pick the chart "
                "     `type` that fits the data:\n"
                "       - `bar` for category comparisons,\n"
                "       - `line` or `area` for time series,\n"
                "       - `pie`/`doughnut` for parts of a whole (≤8 slices, single y),\n"
                "       - `table` if no chart shape fits.\n"
                "     Cap rows at 200; aggregate or top-N if the result is larger.\n"
                "  3. After render_chart succeeds, give the user a brief "
                "     1-3 sentence text answer describing the result. Do NOT "
                "     restate the data — the chart already shows it.\n"
                "If the question genuinely doesn't have a numerical answer "
                "to chart, skip render_chart and answer in text.\n"
            )
        tools_used: list = []
        queries_executed = 0
        iteration        = 0

        yield {"type": "status", "message": "Starting analysis…"}

        try:
            while iteration < self._max_iter:
                iteration += 1
                logger.info(f"[Agent] Iteration {iteration}")
                yield {"type": "status", "message": f"Thinking… (step {iteration})"}

                # ── Two iterations before limit, remove tools to force answer ─
                force_final = (iteration >= self._max_iter - 2)
                if force_final:
                    call_tools = None
                    logger.info("[Agent] Forcing final answer — tools disabled")
                    yield {"type": "status", "message": "Composing final answer…"}
                else:
                    call_tools = self._registry.get_api_definitions()
                    if not visualise:
                        # Hide render_chart from the LLM in text mode so it
                        # doesn't get tempted into spurious chart calls.
                        call_tools = [t for t in call_tools if t.get("name") != "render_chart"]

                # ── LLM call (streaming) ──────────────────────────────────────
                response    = None
                stripper    = _ThinkingStripper()
                thinking_open = False

                try:
                    async for evt in self._ai.complete_stream(
                        messages=messages,
                        system=system,
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
                    yield {"type": "error", "message": str(exc)}
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
                    answer = "\n\n".join(text_remainder)
                    logger.info(f"[Agent] Final answer: {answer[:300]}")

                    # Compress this turn: replace full tool call/result chains
                    # with a compact 2-message summary to keep context lean.
                    new_messages = messages[len(prior_messages):]
                    compressed   = _compress_turn(prior_messages, new_messages, answer)
                    # Defense-in-depth: strip any stray tool_use/tool_result blocks
                    # before persisting. prior_messages is already clean from the
                    # previous turn, but compressed may contain inline tool blocks
                    # if _compress_turn ever changes.
                    self._sessions.set_messages(session_id, _strip_tool_blocks(compressed))

                    yield {
                        "type":             "answer",
                        "content":          answer,
                        "session_id":       session_id,
                        "iterations":       iteration,
                        "tools_used":       tools_used,
                        "queries_executed": queries_executed,
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

                        # Chart-tool side effect: surface the validated spec
                        # to the frontend via SSE so it renders inside the
                        # AI message card. Only emit on success — on a bad
                        # spec the tool returns is_error=True, the LLM gets
                        # the validation message back, and can correct + retry.
                        if (
                            tool_name == "render_chart"
                            and not result.is_error
                            and isinstance(result.metadata, dict)
                            and "chart_spec" in result.metadata
                        ):
                            yield {
                                "type": "chart",
                                "spec": result.metadata["chart_spec"],
                            }

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

        except Exception as exc:
            logger.exception("[AgentOrchestrator] Unhandled error in ask_stream")
            self._sessions.set_messages(session_id, _strip_tool_blocks(prior_messages))
            yield {"type": "error", "message": f"Agent error: {exc}"}
