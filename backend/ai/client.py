"""
Unified AI client — single call site for all LLM completions.

Reads provider / API key / model from config/model_config.json on every call
so it always uses the latest saved configuration (no restart needed after setup).

Supported providers: anthropic | openai | custom
"""

import collections
import logging
import time

logger = logging.getLogger(__name__)

# ── Rate limiter ─────────────────────────────────────────────────────────────
# Track call timestamps to stay below 25 calls/minute.
_call_timestamps: collections.deque = collections.deque()
_RATE_LIMIT_WINDOW = 60   # seconds
_MAX_CALLS_PER_MIN = 25


def _record_call() -> None:
    """Record a call timestamp and sleep if we're approaching the rate limit."""
    now = time.time()
    # Evict timestamps outside the rolling window.
    while _call_timestamps and _call_timestamps[0] < now - _RATE_LIMIT_WINDOW:
        _call_timestamps.popleft()

    if len(_call_timestamps) >= _MAX_CALLS_PER_MIN:
        # Wait until the oldest call falls outside the window, capped at 5s
        # to avoid blocking request threads long enough to trigger HTTP timeouts.
        wait_secs = (_call_timestamps[0] + _RATE_LIMIT_WINDOW) - now + 0.1
        if 0 < wait_secs <= 5:
            logger.info(f"Rate limit approaching ({len(_call_timestamps)} calls/min): queuing for {wait_secs:.1f}s")
            time.sleep(wait_secs)
        elif wait_secs > 5:
            logger.warning(f"Rate limit queue would be {wait_secs:.0f}s — proceeding immediately")

    _call_timestamps.append(time.time())


class RateLimitExhausted(RuntimeError):
    """Raised when a 429 rate-limit error is received. Carries retry_after in seconds."""
    def __init__(self, message: str = "Rate limit hit", retry_after: int = 60):
        super().__init__(message)
        self.retry_after = retry_after


def get_completion(
    system: str,
    user: str,
    max_tokens: int = 2000,
    temperature: float = 0,
) -> str:
    """Make a text completion using the configured AI provider.

    Returns the response text.
    Raises RuntimeError on API failure.
    """
    from backend.config.loader import load_ai_config

    cfg = load_ai_config()
    provider  = cfg.get("provider", "anthropic")
    api_key   = cfg.get("api_key", "")
    model     = cfg.get("model", "claude-sonnet-4-20250514")

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


def _call_anthropic(api_key, model, system, user, max_tokens, temperature):
    import anthropic as _anthropic

    _record_call()

    try:
        client = _anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return response.content[0].text.strip()

    except _anthropic.RateLimitError as e:
        # Read retry-after header from the response if available.
        retry_after = 60
        try:
            retry_after = int(e.response.headers.get("retry-after", "60"))
        except Exception:
            pass
        logger.warning(f"Anthropic rate limit hit — signalling frontend to wait {retry_after}s")
        raise RateLimitExhausted(retry_after=retry_after) from e

    except Exception as e:
        raise RuntimeError(f"Anthropic API call failed: {e}") from e


def _call_openai(api_key, model, system, user, max_tokens, temperature):
    _record_call()
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        if type(e).__name__ == "RateLimitError":
            logger.warning("OpenAI rate limit hit — signalling frontend to wait")
            raise RateLimitExhausted(retry_after=60) from e
        raise RuntimeError(f"OpenAI API call failed: {e}") from e


def _call_openai_compat(api_key, model, system, user, max_tokens, temperature, base_url):
    """Call an OpenAI-compatible custom endpoint."""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url or None)
        response = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        raise RuntimeError(f"Custom endpoint call failed: {e}") from e


def test_connection(provider: str, api_key: str, model: str, custom_endpoint: str = "") -> dict:
    """Make a minimal API call to verify the key and model are valid.

    Returns {"success": True} or {"success": False, "error": "..."}.
    """
    try:
        if provider == "openai":
            from openai import OpenAI
            client = OpenAI(api_key=api_key)
            client.chat.completions.create(
                model=model,
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
            )
        elif provider == "custom":
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url=custom_endpoint or None)
            client.chat.completions.create(
                model=model,
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
            )
        else:  # anthropic
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            client.messages.create(
                model=model,
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
            )
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}
