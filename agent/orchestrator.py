"""
Agent orchestrator — the core autonomous ReAct loop using Anthropic native tool use.

AIClient
    Thin async wrapper around anthropic.AsyncAnthropic.
    Reads provider / API key / model fresh on every call (no restart needed).
    Raises RateLimitExhausted on 429.

AgentOrchestrator
    ask(question, session_id?)         → AgentResponse  (collects all stream events)
    ask_stream(question, session_id?)  → AsyncGenerator[dict, None]  (SSE-friendly)

ReAct loop: the model writes <thinking>...</thinking> before each tool call.
The orchestrator strips those tags, emits them as "thinking" events, and sends
the remaining text as the final "answer" event when stop_reason == "end_turn".

Event types emitted:
    {"type": "status",      "message": "..."}
    {"type": "thinking",    "content": "..."}
    {"type": "tool_call",   "tool": "...", "input": {...}}
    {"type": "tool_result", "tool": "...", "result_summary": "...", "is_error": bool}
    {"type": "answer",      "content": "...", "session_id": "...",
                            "iterations": N, "tools_used": [...], "queries_executed": N}
    {"type": "error",       "message": "...", "retry_after"?: N}
"""

import json
import logging
import re
from typing import AsyncGenerator, Optional

from agent.models import AgentResponse, ToolResult
from agent.memory import SessionStore
from agent.prompts import build_system_prompt
from agent.tools.base import ToolRegistry

logger = logging.getLogger(__name__)

_MAX_ITERATIONS = 15

# Matches <thinking>...</thinking> (case-insensitive, dotall)
_THINKING_RE = re.compile(r"<thinking>(.*?)</thinking>", re.DOTALL | re.IGNORECASE)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_thinking(text: str) -> tuple[list[str], str]:
    """Pull <thinking> blocks out of text.

    Returns (list_of_thoughts, remaining_text_with_tags_removed).
    """
    thoughts  = [m.group(1).strip() for m in _THINKING_RE.finditer(text)]
    remaining = _THINKING_RE.sub("", text).strip()
    return thoughts, remaining


def _summarize_result(result: ToolResult) -> str:
    """One-line summary of a tool result for the trace UI."""
    if result.is_error:
        return result.content.split("\n")[0][:100]
    row_count = result.metadata.get("row_count")
    if row_count is not None:
        return f"{row_count} row{'s' if row_count != 1 else ''} returned"
    content = result.content.replace("\n", " ").strip()
    return (content[:80] + "…") if len(content) > 80 else content


def _content_to_list(content_blocks) -> list[dict]:
    """Convert Anthropic SDK content blocks to plain dicts for message history."""
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


# ── AI Client ─────────────────────────────────────────────────────────────────

class AIClient:
    """
    Async wrapper around Anthropic's messages API with tool support.

    Reads config fresh on every call so key/model changes take effect
    without restarting the server (same pattern as v1 get_completion).
    """

    async def complete(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict],
        max_tokens: int = 4096,
    ):
        """
        Call anthropic.AsyncAnthropic.messages.create with tool definitions.

        Returns the full Anthropic response object (content blocks + stop_reason).
        Raises RateLimitExhausted on 429, RuntimeError on other failures.
        """
        from backend.config.loader import load_ai_config
        from backend.ai.client import RateLimitExhausted

        cfg      = load_ai_config()
        api_key  = cfg.get("api_key", "")
        model    = cfg.get("model", "claude-sonnet-4-20250514")
        provider = cfg.get("provider", "anthropic")

        if not api_key:
            raise RuntimeError(
                "No API key configured. Complete Setup → AI Provider first."
            )

        if provider in ("openai", "custom"):
            raise NotImplementedError(
                "Agent mode requires an Anthropic model. "
                "Switch to Anthropic in Setup → AI Provider to use agent mode."
            )

        import anthropic

        client = anthropic.AsyncAnthropic(api_key=api_key)

        try:
            response = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                tools=tools,
            )
            return response

        except anthropic.RateLimitError as exc:
            retry_after = 60
            try:
                retry_after = int(
                    exc.response.headers.get("retry-after", "60")
                )
            except Exception:
                pass
            logger.warning(
                f"[AIClient] Anthropic rate limit — retry_after={retry_after}s"
            )
            raise RateLimitExhausted(retry_after=retry_after) from exc

        except Exception as exc:
            raise RuntimeError(f"AI API call failed: {exc}") from exc


# ── Orchestrator ──────────────────────────────────────────────────────────────

class AgentOrchestrator:
    """
    Runs the autonomous ReAct agent loop.

    Each iteration:
      1. Call LLM with tool definitions + full message history
      2. Parse text blocks → extract <thinking> tags → emit as "thinking" events
      3. If stop_reason == "tool_use":
           emit tool_call events → execute → emit tool_result events → loop
      4. If stop_reason == "end_turn":
           emit "answer" event with remaining text → done
    """

    def __init__(
        self,
        ai_client: AIClient,
        registry: ToolRegistry,
        sessions: SessionStore,
        db_type: str = "mssql",
        max_iterations: int = _MAX_ITERATIONS,
    ):
        self._ai       = ai_client
        self._registry = registry
        self._sessions = sessions
        self._system   = build_system_prompt(db_type)
        self._max_iter = max_iterations

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

        return AgentResponse(
            status="error",
            session_id=sid,
            error="Agent produced no response.",
        )

    # ── Streaming ─────────────────────────────────────────────────────────────

    async def ask_stream(
        self,
        question: str,
        session_id: Optional[str] = None,
    ) -> AsyncGenerator[dict, None]:
        """
        Run the ReAct loop and yield SSE-friendly progress events.
        """
        from backend.ai.client import RateLimitExhausted

        session_id = self._sessions.get_or_create(session_id)
        messages   = self._sessions.get_messages(session_id)
        messages.append({"role": "user", "content": question})

        tools_used: list[str] = []
        queries_executed = 0
        iteration = 0

        yield {"type": "status", "message": "Starting analysis…"}

        try:
            while iteration < self._max_iter:
                iteration += 1
                logger.info(f"[Agent] Iteration {iteration}")
                yield {
                    "type":    "status",
                    "message": f"Thinking… (step {iteration})",
                }

                # ── LLM call ──────────────────────────────────────────────────
                try:
                    response = await self._ai.complete(
                        messages=messages,
                        system=self._system,
                        tools=self._registry.get_api_definitions(),
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
                all_thoughts:   list[str]  = []
                text_remainder: list[str]  = []
                tool_blocks:    list       = []

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
                content_list = _content_to_list(response.content)
                messages.append({"role": "assistant", "content": content_list})

                # ── Emit thinking events ───────────────────────────────────────
                for thought in all_thoughts:
                    yield {"type": "thinking", "content": thought}

                stop_reason = response.stop_reason

                # ── Done (no more tool calls) ──────────────────────────────────
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
                        yield {
                            "type":  "tool_call",
                            "tool":  tool_name,
                            "input": tool_input,
                        }

                        result = await self._registry.execute(
                            tool_name, tool_id, tool_input
                        )

                        if tool_name == "execute_sql":
                            queries_executed += 1

                        logger.info(
                            f"[Agent] Tool result ({tool_name}): "
                            f"{result.content[:300]}"
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
                    continue  # next iteration

                # ── Unexpected stop reason ────────────────────────────────────
                logger.warning(
                    f"[AgentOrchestrator] Unexpected stop_reason={stop_reason!r} "
                    f"on iteration {iteration}"
                )
                self._sessions.set_messages(session_id, messages)
                yield {
                    "type":    "error",
                    "message": f"Agent stopped unexpectedly (reason: {stop_reason}).",
                }
                return

            # Max iterations reached
            self._sessions.set_messages(session_id, messages)
            yield {
                "type":    "error",
                "message": (
                    f"Agent reached the maximum of {self._max_iter} steps without "
                    "a final answer. Try a more specific question."
                ),
            }

        except Exception as exc:
            logger.exception("[AgentOrchestrator] Unhandled error in ask_stream")
            self._sessions.set_messages(session_id, messages)
            yield {"type": "error", "message": f"Agent error: {exc}"}
