"""
Unified AI client — single call site for all LLM completions.

Reads provider / API key / model from config/model_config.json on every call
so it always uses the latest saved configuration (no restart needed after setup).

Supported providers: anthropic | openai | custom
"""

import logging

logger = logging.getLogger(__name__)


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
    from config.loader import load_ai_config

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
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        raise RuntimeError(f"Anthropic API call failed: {e}") from e


def _call_openai(api_key, model, system, user, max_tokens, temperature):
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
