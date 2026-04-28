"""
Unified AI client — single call site for all LLM completions.

Reads provider / API key / model from config on every call so changes
take effect without restarting the server.

Supported providers: anthropic | openai | custom
"""

import asyncio
import collections
import logging
import time
from typing import AsyncGenerator, Optional

logger = logging.getLogger(__name__)

# ── Rate limiter ──────────────────────────────────────────────────────────────
#
# Two layers of protection:
#   1. Sliding-window counter: track call timestamps over a 60s window.
#      If we're about to exceed _MAX_CALLS_PER_MIN, sleep until there's room.
#   2. Minimum gap: enforce at least _MIN_CALL_GAP_S seconds between consecutive
#      calls so the agent can't burst-fire 4 requests in < 1 second.
#
# Sync callers (setup wizard) use _record_call() + time.sleep.
# Async callers (agent loop) use _async_record_call() + asyncio.sleep.

_call_timestamps: collections.deque = collections.deque()   # monotonic timestamps
_last_call_time:  float             = 0.0                   # monotonic
_RATE_LIMIT_WINDOW  = 60     # seconds
_MAX_CALLS_PER_MIN  = 15     # conservative — Anthropic free tier is 5/min, paid is higher
_MIN_CALL_GAP_S     = 0.2    # minimum seconds between consecutive LLM calls

# Guards the globals above against concurrent async access. Without this,
# two requests arriving simultaneously can both read stale _last_call_time
# and both fire without the intended gap, causing avoidable 429s.
_rate_lock: asyncio.Lock | None = None

def _get_rate_lock() -> asyncio.Lock:
    global _rate_lock
    if _rate_lock is None:
        _rate_lock = asyncio.Lock()
    return _rate_lock


def _record_call() -> None:
    """Sync rate gate — used by get_completion() (setup wizard calls).

    Uses monotonic clock to share state consistently with `_async_record_call`.
    """
    global _last_call_time

    now = time.monotonic()

    # Enforce minimum gap
    gap = now - _last_call_time
    if _last_call_time > 0 and gap < _MIN_CALL_GAP_S:
        time.sleep(_MIN_CALL_GAP_S - gap)
        now = time.monotonic()

    # Sliding window check
    while _call_timestamps and _call_timestamps[0] < now - _RATE_LIMIT_WINDOW:
        _call_timestamps.popleft()

    if len(_call_timestamps) >= _MAX_CALLS_PER_MIN:
        wait_secs = (_call_timestamps[0] + _RATE_LIMIT_WINDOW) - now + 0.1
        if wait_secs > 0:
            logger.info(f"[RateLimit] Sync: window full, sleeping {wait_secs:.1f}s")
            time.sleep(wait_secs)
            now = time.monotonic()

    _call_timestamps.append(now)
    _last_call_time = now


async def _async_record_call() -> None:
    """Async rate gate — used by AIClient.complete() (agent loop calls).

    Serialized via `_rate_lock` so concurrent requests can't both observe
    stale `_last_call_time` / deque state and double-fire into a 429.
    All timestamps here are `time.monotonic()` — never mix with wall time.
    """
    global _last_call_time

    async with _get_rate_lock():
        now = time.monotonic()

        # Proactive throttle: if the last response said our bucket is nearly
        # empty, pace requests to avoid a 429 on the next call.
        rr = _rl_headers.get("requests_remaining")
        dynamic_gap = _MIN_CALL_GAP_S
        if isinstance(rr, int):
            if rr <= 1:
                dynamic_gap = max(dynamic_gap, 4.0)
            elif rr <= 3:
                dynamic_gap = max(dynamic_gap, 2.0)
            elif rr <= 6:
                dynamic_gap = max(dynamic_gap, 1.0)

        # Enforce minimum gap between consecutive calls
        gap = now - _last_call_time
        if _last_call_time > 0 and gap < dynamic_gap:
            sleep_for = dynamic_gap - gap
            logger.debug(f"[RateLimit] gap guard: sleeping {sleep_for:.2f}s (remaining={rr})")
            await asyncio.sleep(sleep_for)
            now = time.monotonic()

        # Sliding window check (monotonic clock for the deque)
        while _call_timestamps and _call_timestamps[0] < now - _RATE_LIMIT_WINDOW:
            _call_timestamps.popleft()

        if len(_call_timestamps) >= _MAX_CALLS_PER_MIN:
            wait_secs = (_call_timestamps[0] + _RATE_LIMIT_WINDOW) - now + 0.1
            if wait_secs > 0:
                logger.info(f"[RateLimit] Async: window full, sleeping {wait_secs:.1f}s")
                await asyncio.sleep(wait_secs)
                now = time.monotonic()

        _call_timestamps.append(now)
        _last_call_time = now


class RateLimitExhausted(RuntimeError):
    """Raised when a 429 rate-limit error is received from the API."""
    def __init__(self, message: str = "Rate limit hit", retry_after: int = 60):
        super().__init__(message)
        self.retry_after = retry_after


# ── Sync completion (for setup wizard, company builder, etc.) ─────────────────

def get_completion(
    system: str,
    user: str,
    max_tokens: int = 8000,
    temperature: float = 0,
) -> str:
    """Make a text completion using the configured AI provider. Returns response text."""
    from app.config import load_ai_config

    cfg      = load_ai_config()
    provider = cfg.get("provider", "anthropic")
    api_key  = cfg.get("api_key", "")
    model    = cfg.get("model", "claude-sonnet-4-20250514")

    if not api_key:
        raise RuntimeError(
            "No API key configured. Complete the AI Provider step in the setup wizard."
        )

    if provider == "openai":
        return _call_openai(api_key, model, system, user, max_tokens, temperature)
    elif provider == "custom":
        endpoint = cfg.get("custom_endpoint", "")
        return _call_openai_compat(api_key, model, system, user, max_tokens, temperature, endpoint)
    else:
        return _call_anthropic(api_key, model, system, user, max_tokens, temperature)


def test_connection(provider: str, api_key: str, model: str, custom_endpoint: str = "") -> dict:
    """Make a minimal API call to verify the key and model are valid."""
    try:
        if provider == "openai":
            from openai import OpenAI
            OpenAI(api_key=api_key).chat.completions.create(
                model=model, max_tokens=1, messages=[{"role": "user", "content": "hi"}]
            )
        elif provider == "custom":
            from openai import OpenAI
            OpenAI(api_key=api_key, base_url=custom_endpoint or None).chat.completions.create(
                model=model, max_tokens=1, messages=[{"role": "user", "content": "hi"}]
            )
        else:
            import anthropic
            anthropic.Anthropic(api_key=api_key).messages.create(
                model=model, max_tokens=1, messages=[{"role": "user", "content": "hi"}]
            )
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _call_anthropic(api_key, model, system, user, max_tokens, temperature):
    import anthropic as _anthropic
    _record_call()
    try:
        client = _anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model, max_tokens=max_tokens, temperature=temperature,
            system=system, messages=[{"role": "user", "content": user}],
        )
        return response.content[0].text.strip()
    except _anthropic.RateLimitError as e:
        retry_after = 60
        try:
            retry_after = int(e.response.headers.get("retry-after", "60"))
        except Exception:
            pass
        raise RateLimitExhausted(retry_after=retry_after) from e
    except Exception as e:
        raise RuntimeError(f"Anthropic API call failed: {e}") from e


def _call_openai(api_key, model, system, user, max_tokens, temperature):
    _record_call()
    try:
        from openai import OpenAI
        response = OpenAI(api_key=api_key).chat.completions.create(
            model=model, max_tokens=max_tokens, temperature=temperature,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        if type(e).__name__ == "RateLimitError":
            raise RateLimitExhausted(retry_after=60) from e
        raise RuntimeError(f"OpenAI API call failed: {e}") from e


def _call_openai_compat(api_key, model, system, user, max_tokens, temperature, base_url):
    try:
        from openai import OpenAI
        response = OpenAI(api_key=api_key, base_url=base_url or None).chat.completions.create(
            model=model, max_tokens=max_tokens, temperature=temperature,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        raise RuntimeError(f"Custom endpoint call failed: {e}") from e


# ── Prompt-cache helpers ─────────────────────────────────────────────────────
#
# Anthropic's prompt caching (https://docs.anthropic.com/en/docs/build-with-
# claude/prompt-caching) lets us mark large, stable prefixes as cacheable.
# Subsequent requests within ~5 min that share that prefix pay ~10% of the
# input cost on the cached portion (the SDK reports this in usage as
# `cache_read_input_tokens`).
#
# We tag two surfaces:
#   1. The system prompt (large, content-stable across a session).
#   2. The last tool definition (caches the entire tools array).
#
# Quality impact: zero — the bytes the model sees are unchanged. Only
# billing changes.

def _with_system_cache(system: str | list) -> list:
    """
    Convert a system-prompt string into the structured list form that
    accepts cache_control. If `system` is already a list, attach
    cache_control to the last block (idempotent).
    """
    if isinstance(system, list):
        if not system:
            return system
        # Attach cache_control to the last block, leave earlier blocks alone.
        # Mutating in place is fine — these dicts are constructed per-request.
        last = dict(system[-1])
        last["cache_control"] = {"type": "ephemeral"}
        return list(system[:-1]) + [last]
    return [{"type": "text", "text": system or "", "cache_control": {"type": "ephemeral"}}]


def _with_tool_cache(tools: list[dict]) -> list[dict]:
    """
    Tag the LAST tool with cache_control. Per Anthropic's caching rules,
    a cache_control marker covers everything up to and including itself
    in the tools array — so marking the last tool caches the entire tools
    block in one stroke.
    """
    if not tools:
        return tools
    out = [dict(t) for t in tools]
    out[-1] = dict(out[-1])
    out[-1]["cache_control"] = {"type": "ephemeral"}
    return out


def _log_cache_usage(usage) -> None:
    """Log cache hits/writes from the Anthropic usage object."""
    if usage is None:
        return
    try:
        write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        read  = getattr(usage, "cache_read_input_tokens", 0) or 0
        normal = getattr(usage, "input_tokens", 0) or 0
        out    = getattr(usage, "output_tokens", 0) or 0
        if write or read:
            logger.info(
                "[AIClient] tokens: in=%d cache_read=%d cache_write=%d out=%d "
                "(saved ~%.0f%% on cached portion)",
                normal, read, write, out,
                (read / max(read + normal, 1)) * 100 if (read or normal) else 0,
            )
    except Exception:
        pass


# ── Async client (for agent orchestrator) ────────────────────────────────────

class AIClient:
    """
    Async wrapper around Anthropic's messages API with tool support.
    Reads config fresh on every call — no restart needed after setup changes.
    """

    async def complete_stream(
        self,
        messages: list[dict],
        system: str,
        tools: Optional[list[dict]] = None,
        max_tokens: int = 16000,
    ) -> AsyncGenerator[dict, None]:
        """Stream a completion from Anthropic, yielding incremental events.

        Yielded event shapes:
            {"type": "text_delta",        "text": "<chunk>"}
            {"type": "tool_use_start",    "name": "<tool>", "id": "<id>"}
            {"type": "rate_limit_wait",   "wait_seconds": N, "attempt": k, "max_attempts": K}
            {"type": "rate_limit_tick",   "remaining": N}
            {"type": "rate_limit_resume"}
            {"type": "final_message",     "message": <anthropic.types.Message>}

        The final event is always `final_message`, carrying the fully-assembled
        response object. On 429 we handle retry ourselves (SDK retries disabled
        via max_retries=0) so the orchestrator can stream wait progress to the UI.

        Raises RateLimitExhausted if all retries are exhausted, RuntimeError on
        other transport errors.
        """
        from app.config import load_ai_config

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

        # max_retries=0 disables the SDK's silent 2-attempt retry on 429.
        # We handle retries ourselves so the UI can see the wait in real time.
        client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=0)

        # ── Prompt caching ────────────────────────────────────────────────
        # The system prompt + tool definitions are large and identical across
        # ReAct iterations within a turn (3-10 LLM calls per question), and
        # across turns within a 5-minute window. Tagging them as cacheable
        # means subsequent calls pay 10% of input cost on those tokens
        # instead of 100% — typical savings on this app are 60-80% per turn
        # with zero quality impact (the prompt content is identical).
        #
        # Min cache size: 1024 tokens (Haiku) / 2048 tokens (Sonnet/Opus).
        # Tagging a too-small block is a silent no-op, so always-on is safe.
        cached_system = _with_system_cache(system)
        cached_tools  = _with_tool_cache(tools) if tools else None

        kwargs: dict = dict(
            model=model,
            max_tokens=max_tokens,
            system=cached_system,
            messages=messages,
        )
        if cached_tools:
            kwargs["tools"] = cached_tools

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            await _async_record_call()
            try:
                async with client.messages.stream(**kwargs) as stream:
                    async for event in stream:
                        etype = getattr(event, "type", None)
                        if etype == "content_block_start":
                            block = getattr(event, "content_block", None)
                            btype = getattr(block, "type", None)
                            if btype == "tool_use":
                                yield {
                                    "type": "tool_use_start",
                                    "name": getattr(block, "name", ""),
                                    "id":   getattr(block, "id", ""),
                                }
                        elif etype == "content_block_delta":
                            delta = getattr(event, "delta", None)
                            dtype = getattr(delta, "type", None)
                            if dtype == "text_delta":
                                text = getattr(delta, "text", "")
                                if text:
                                    yield {"type": "text_delta", "text": text}
                    final = await stream.get_final_message()

                    # ── Proactive rate-limit awareness ───────────────────────
                    # Read reply headers and, if the bucket is nearly empty,
                    # slow down the next request so we don't trip a 429.
                    _maybe_record_headers(final)

                    # Visibility into prompt-cache hit/miss for cost auditing.
                    _log_cache_usage(getattr(final, "usage", None))

                    yield {"type": "final_message", "message": final}
                    return

            except anthropic.RateLimitError as exc:
                retry_after = _parse_retry_after(exc)
                logger.warning(
                    f"[AIClient] 429 on attempt {attempt}/{max_attempts} — "
                    f"wait {retry_after}s"
                )

                # Surface the wait to the caller. Cap at 90s; beyond that
                # raise RateLimitExhausted so the user can manually retry.
                if attempt >= max_attempts or retry_after > 90:
                    raise RateLimitExhausted(retry_after=retry_after) from exc

                yield {
                    "type":         "rate_limit_wait",
                    "wait_seconds": retry_after,
                    "attempt":      attempt,
                    "max_attempts": max_attempts,
                }

                # Stream a 1-Hz countdown so the UI can update a live clock
                remaining = retry_after
                while remaining > 0:
                    yield {"type": "rate_limit_tick", "remaining": remaining}
                    await asyncio.sleep(1)
                    remaining -= 1

                yield {"type": "rate_limit_resume"}
                continue

            except anthropic.APIStatusError as exc:
                # 5xx from Anthropic — one retry with short backoff
                if 500 <= exc.status_code < 600 and attempt < max_attempts:
                    backoff = min(2 ** attempt, 8)
                    logger.warning(
                        f"[AIClient] {exc.status_code} on attempt {attempt} — "
                        f"retrying in {backoff}s"
                    )
                    await asyncio.sleep(backoff)
                    continue
                raise RuntimeError(f"AI stream call failed: {exc}") from exc

            except Exception as exc:
                raise RuntimeError(f"AI stream call failed: {exc}") from exc


# ── Rate-limit helpers ────────────────────────────────────────────────────────

# Last-observed bucket headers from Anthropic (updated on every successful call).
# When `remaining` drops low we proactively space out subsequent calls.
_rl_headers: dict = {
    "requests_remaining": None,   # int
    "tokens_remaining":   None,   # int
    "requests_reset":     None,   # ISO8601 timestamp
}


def _parse_retry_after(exc) -> int:
    """Extract retry-after seconds from an Anthropic RateLimitError response."""
    try:
        raw = exc.response.headers.get("retry-after")
        if raw is None:
            return 30
        return max(1, int(float(raw)))
    except Exception:
        return 30


def _maybe_record_headers(message) -> None:
    """Inspect response headers on a successful stream and remember rate-limit state."""
    try:
        hdrs = getattr(getattr(message, "_raw_response", None), "headers", None)
        if hdrs is None:
            # Not always available on streamed messages; skip silently.
            return
        rr = hdrs.get("anthropic-ratelimit-requests-remaining")
        tr = hdrs.get("anthropic-ratelimit-tokens-remaining")
        rs = hdrs.get("anthropic-ratelimit-requests-reset")
        if rr is not None:
            _rl_headers["requests_remaining"] = int(rr)
        if tr is not None:
            _rl_headers["tokens_remaining"] = int(tr)
        if rs is not None:
            _rl_headers["requests_reset"] = rs
    except Exception:
        pass
