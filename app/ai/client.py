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
from typing import Optional

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

_call_timestamps: collections.deque = collections.deque()
_last_call_time:  float             = 0.0
_RATE_LIMIT_WINDOW  = 60     # seconds
_MAX_CALLS_PER_MIN  = 15     # conservative — Anthropic free tier is 5/min, paid is higher
_MIN_CALL_GAP_S     = 0.5    # minimum seconds between consecutive LLM calls


def _record_call() -> None:
    """Sync rate gate — used by get_completion() (setup wizard calls)."""
    global _last_call_time

    now = time.time()

    # Enforce minimum gap
    gap = now - _last_call_time
    if _last_call_time > 0 and gap < _MIN_CALL_GAP_S:
        time.sleep(_MIN_CALL_GAP_S - gap)
        now = time.time()

    # Sliding window check
    while _call_timestamps and _call_timestamps[0] < now - _RATE_LIMIT_WINDOW:
        _call_timestamps.popleft()

    if len(_call_timestamps) >= _MAX_CALLS_PER_MIN:
        wait_secs = (_call_timestamps[0] + _RATE_LIMIT_WINDOW) - now + 0.1
        if wait_secs > 0:
            logger.info(f"[RateLimit] Sync: window full, sleeping {wait_secs:.1f}s")
            time.sleep(wait_secs)
            now = time.time()

    _call_timestamps.append(time.time())
    _last_call_time = time.time()


async def _async_record_call() -> None:
    """Async rate gate — used by AIClient.complete() (agent loop calls)."""
    global _last_call_time

    now = time.monotonic()

    # Enforce minimum gap between consecutive calls
    gap = now - _last_call_time
    if _last_call_time > 0 and gap < _MIN_CALL_GAP_S:
        sleep_for = _MIN_CALL_GAP_S - gap
        logger.debug(f"[RateLimit] Min gap: sleeping {sleep_for:.2f}s")
        await asyncio.sleep(sleep_for)

    # Sliding window check (uses wall time for the deque)
    wall = time.time()
    while _call_timestamps and _call_timestamps[0] < wall - _RATE_LIMIT_WINDOW:
        _call_timestamps.popleft()

    if len(_call_timestamps) >= _MAX_CALLS_PER_MIN:
        wait_secs = (_call_timestamps[0] + _RATE_LIMIT_WINDOW) - wall + 0.1
        if wait_secs > 0:
            logger.info(f"[RateLimit] Async: window full, sleeping {wait_secs:.1f}s")
            await asyncio.sleep(wait_secs)
            wall = time.time()

    _call_timestamps.append(time.time())
    _last_call_time = time.monotonic()


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


# ── Async client (for agent orchestrator) ────────────────────────────────────

class AIClient:
    """
    Async wrapper around Anthropic's messages API with tool support.
    Reads config fresh on every call — no restart needed after setup changes.
    """

    async def complete(
        self,
        messages: list[dict],
        system: str,
        tools: Optional[list[dict]] = None,
        max_tokens: int = 16000,
    ):
        """Call Anthropic messages.create with optional tool definitions.

        Pass tools=None or tools=[] to force a plain text response (no tool calls).
        Returns the full Anthropic response object.
        Raises RateLimitExhausted on 429, RuntimeError on other failures.
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

        await _async_record_call()

        client = anthropic.AsyncAnthropic(api_key=api_key)
        kwargs: dict = dict(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        if tools:
            kwargs["tools"] = tools

        # One automatic retry on 429: wait the retry-after duration, then try once more.
        # If it fails again, raise RateLimitExhausted so the orchestrator can surface
        # the countdown to the user.
        for attempt in range(2):
            try:
                return await client.messages.create(**kwargs)
            except anthropic.RateLimitError as exc:
                retry_after = 60
                try:
                    retry_after = int(exc.response.headers.get("retry-after", "60"))
                except Exception:
                    pass
                logger.warning(
                    f"[AIClient] 429 received (attempt {attempt + 1}/2) — "
                    f"retry_after={retry_after}s"
                )
                if attempt == 0 and retry_after <= 65:
                    # Auto-wait and retry once for short waits (≤ 65s)
                    logger.info(f"[AIClient] Auto-waiting {retry_after}s before retry…")
                    await asyncio.sleep(retry_after)
                    await _async_record_call()   # re-check rate gate after sleep
                    continue
                raise RateLimitExhausted(retry_after=retry_after) from exc
            except Exception as exc:
                raise RuntimeError(f"AI API call failed: {exc}") from exc
