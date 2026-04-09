"""
Unified AI client — single call site for all LLM completions.

Reads provider / API key / model from config on every call so changes
take effect without restarting the server.

Supported providers: anthropic | openai | custom
"""

import collections
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ── Rate limiter ──────────────────────────────────────────────────────────────

_call_timestamps: collections.deque = collections.deque()
_RATE_LIMIT_WINDOW = 60
_MAX_CALLS_PER_MIN = 25


def _record_call() -> None:
    now = time.time()
    while _call_timestamps and _call_timestamps[0] < now - _RATE_LIMIT_WINDOW:
        _call_timestamps.popleft()
    if len(_call_timestamps) >= _MAX_CALLS_PER_MIN:
        wait_secs = (_call_timestamps[0] + _RATE_LIMIT_WINDOW) - now + 0.1
        if 0 < wait_secs <= 5:
            logger.info(f"Rate limit approaching: queuing for {wait_secs:.1f}s")
            time.sleep(wait_secs)
        elif wait_secs > 5:
            logger.warning(f"Rate limit queue would be {wait_secs:.0f}s — proceeding immediately")
    _call_timestamps.append(time.time())


class RateLimitExhausted(RuntimeError):
    """Raised when a 429 rate-limit error is received."""
    def __init__(self, message: str = "Rate limit hit", retry_after: int = 60):
        super().__init__(message)
        self.retry_after = retry_after


# ── Sync completion (for setup wizard, company builder, etc.) ─────────────────

def get_completion(
    system: str,
    user: str,
    max_tokens: int = 2000,
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
        tools: list[dict],
        max_tokens: int = 4096,
    ):
        """Call Anthropic messages.create with tool definitions.

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

        client = anthropic.AsyncAnthropic(api_key=api_key)
        try:
            return await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                tools=tools,
            )
        except anthropic.RateLimitError as exc:
            retry_after = 60
            try:
                retry_after = int(exc.response.headers.get("retry-after", "60"))
            except Exception:
                pass
            logger.warning(f"[AIClient] Rate limit — retry_after={retry_after}s")
            raise RateLimitExhausted(retry_after=retry_after) from exc
        except Exception as exc:
            raise RuntimeError(f"AI API call failed: {exc}") from exc
