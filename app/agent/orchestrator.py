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
from typing import AsyncGenerator, Optional

logger = logging.getLogger(__name__)

_MAX_ITERATIONS = 15

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
        from app.agent.prompts import SYSTEM_PROMPT
        from app.config import COMPANY_MD_PATH

        prompt = SYSTEM_PROMPT

        # Inject connected source names so the agent never has to guess
        sources = self._sources.get_all()
        if len(sources) == 1:
            s = sources[0]
            prompt += (
                f"\n\n## Connected Database\n\n"
                f"Source name: `{s.name}` | Type: {s.get_db_type().upper()} | "
                f"Database: {s.get_database_name()}\n"
                f"Use `{s.name}` as the `source` value in any tool call that asks for it."
            )
        elif len(sources) > 1:
            prompt += "\n\n## Connected Databases\n\n"
            for s in sources:
                prompt += f"- `{s.name}` ({s.get_db_type().upper()}, db: {s.get_database_name()})\n"
            prompt += "\nSpecify the correct `source` name in every tool call."

        # Append company knowledge — the agent's domain map
        try:
            if COMPANY_MD_PATH.exists():
                content = COMPANY_MD_PATH.read_text(encoding="utf-8").strip()
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
    ) -> AsyncGenerator[dict, None]:
        """Run the ReAct loop and yield SSE-friendly progress events."""
        from app.ai.client import RateLimitExhausted

        session_id = self._sessions.get_or_create(session_id)
        messages   = self._sessions.get_messages(session_id)
        messages.append({"role": "user", "content": question})

        system           = self._build_system_prompt()
        tools_used: list = []
        queries_executed = 0
        iteration        = 0
        _last_call_ts    = 0.0   # monotonic timestamp of last LLM call

        yield {"type": "status", "message": "Starting analysis…"}

        try:
            while iteration < self._max_iter:
                iteration += 1
                logger.info(f"[Agent] Iteration {iteration}")
                yield {"type": "status", "message": f"Thinking… (step {iteration})"}

                # ── 200 ms minimum gap between LLM calls ──────────────────────
                elapsed = time.monotonic() - _last_call_ts
                if elapsed < 0.2:
                    await asyncio.sleep(0.2 - elapsed)

                # ── On the penultimate iteration, remove tools to force answer ─
                force_final = (iteration >= self._max_iter - 1)
                call_tools  = None if force_final else self._registry.get_api_definitions()
                if force_final:
                    logger.info("[Agent] Forcing final answer — tools disabled")
                    yield {"type": "status", "message": "Composing final answer…"}

                # ── LLM call ──────────────────────────────────────────────────
                _last_call_ts = time.monotonic()
                try:
                    response = await self._ai.complete(
                        messages=messages,
                        system=system,
                        tools=call_tools,
                    )
                except RateLimitExhausted as rl:
                    self._sessions.set_messages(session_id, messages)
                    yield {
                        "type":        "error",
                        "message":     f"Rate limit hit. Retry after {rl.retry_after}s.",
                        "retry_after": rl.retry_after,
                    }
                    return
                except Exception as exc:
                    self._sessions.set_messages(session_id, messages)
                    yield {"type": "error", "message": str(exc)}
                    return

                # ── Parse content blocks ───────────────────────────────────────
                all_thoughts:   list[str] = []
                text_remainder: list[str] = []
                tool_blocks:    list      = []

                for block in response.content:
                    btype = getattr(block, "type", None)
                    if btype == "text":
                        thoughts, remaining = _extract_thinking(block.text)
                        for thought in thoughts:
                            if thought:
                                logger.info(f"[Agent] Thinking: {thought[:300]}")
                        all_thoughts.extend(t for t in thoughts if t)
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

                # ── Emit thinking events ───────────────────────────────────────
                for thought in all_thoughts:
                    yield {"type": "thinking", "content": thought}

                stop_reason = response.stop_reason

                # ── Done ──────────────────────────────────────────────────────
                if stop_reason == "end_turn":
                    answer = "\n\n".join(text_remainder)
                    logger.info(f"[Agent] Final answer: {answer[:300]}")
                    self._sessions.set_messages(session_id, messages)
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
                self._sessions.set_messages(session_id, messages)
                yield {
                    "type":    "error",
                    "message": f"Agent stopped unexpectedly (reason: {stop_reason}).",
                }
                return

            # Max iterations reached (force_final should have caught this, but just in case)
            self._sessions.set_messages(session_id, messages)
            yield {
                "type":    "error",
                "message": "Agent reached the step limit without a final answer. Try a more specific question.",
            }

        except Exception as exc:
            logger.exception("[AgentOrchestrator] Unhandled error in ask_stream")
            self._sessions.set_messages(session_id, messages)
            yield {"type": "error", "message": f"Agent error: {exc}"}
