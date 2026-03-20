"""
Local intent parser — drop-in replacement for intent_parser.py.

Uses a local Ollama model instead of the cloud AI provider.
Identical function signature: parse(question) -> dict

Fallback chain:
  1. Try local Ollama (5s timeout)
  2. On timeout or bad response → try cloud AI (intent_parser.py)
  3. If cloud also fails → return {'intent': 'unknown', 'match_confidence': 'low'}

Config is read fresh on each parse() call from load_ai_config() so the
endpoint/model picked up immediately after the setup wizard saves them.
"""

import json
import logging
import os
import re

import requests

logger = logging.getLogger(__name__)

_PROMPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "prompts",
    "system_prompt.txt",
)
with open(_PROMPT_PATH) as _f:
    _SYSTEM_PROMPT = _f.read()

_CODE_FENCE_RE    = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?\s*```$", re.DOTALL)
_VALID_CONFIDENCE = {"high", "medium", "low"}

_FALLBACK  = {"intent": "unknown", "match_confidence": "low"}


def _get_ollama_cfg() -> tuple[str, str, int]:
    """Return (url, model, timeout) from current ai config."""
    from config.loader import load_ai_config
    ai = load_ai_config()
    endpoint = ai.get("local_endpoint", "http://localhost:11434")
    endpoint = endpoint.rstrip("/")
    url = endpoint + "/api/generate"
    model   = ai.get("local_model", "qwen3:8b")
    timeout = 5
    return url, model, timeout


def _cloud_fallback(question: str) -> dict:
    """Fall back to the cloud AI parser on Ollama failure."""
    from core.intent_parser import parse as cloud_parse
    logger.warning("Ollama failed — falling back to cloud AI")
    return cloud_parse(question)


def parse(question: str) -> dict:
    """Parse a natural language question into an intent dict via Ollama.

    Returns the same shape as intent_parser.parse():
        {'intent': 'business_health'|'deep_dive'|'agent'|'unknown',
         'match_confidence': 'high'|'medium'|'low', ...}

    Fallback chain:
      1. Try local Ollama (5s timeout)
      2. On timeout or parse failure → try cloud AI
      3. If cloud also fails → return {'intent': 'unknown', 'match_confidence': 'low'}
    """
    logger.info("PARSER: Local Ollama")
    if not question or not question.strip():
        return {**_FALLBACK, "error": "empty_question", "original": question}

    url, model, timeout = _get_ollama_cfg()
    prompt = _SYSTEM_PROMPT + "\n\nQuestion: " + question

    try:
        resp = requests.post(
            url,
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0},
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        text = resp.json()["response"].strip()
    except requests.exceptions.Timeout:
        logger.warning(f"Ollama timeout after {timeout}s — falling back to cloud AI")
        return _cloud_fallback(question)
    except Exception as e:
        logger.warning(f"Ollama unavailable: {e} — falling back to cloud AI")
        return _cloud_fallback(question)

    # Strip markdown code fences if present
    fence_match = _CODE_FENCE_RE.match(text)
    if fence_match:
        text = fence_match.group(1).strip()

    # Parse JSON
    try:
        result = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        logger.warning(f"Ollama returned non-JSON: {text!r} — falling back to cloud AI")
        return _cloud_fallback(question)

    if not isinstance(result, dict):
        logger.warning(f"Ollama returned non-dict JSON: {result!r} — falling back to cloud AI")
        return _cloud_fallback(question)

    # Normalise match_confidence
    if result.get("match_confidence") not in _VALID_CONFIDENCE:
        result["match_confidence"] = (
            "low" if result.get("intent") == "unknown" else "high"
        )

    return result
