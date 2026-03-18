"""
Local intent parser — drop-in replacement for intent_parser.py.

Uses a local Ollama model instead of the Claude API.
Identical function signature: parse(question) -> dict

Fallback chain:
  1. Try local Ollama (5s timeout)
  2. On timeout or bad response → try Claude API (intent_parser.py)
  3. If Claude also fails → return {'intent': 'unknown', 'match_confidence': 'low'}
"""

import json
import logging
import os
import re

import requests

from config.loader import load_model_config

logger = logging.getLogger(__name__)

_PROMPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "prompts",
    "system_prompt.txt",
)
with open(_PROMPT_PATH) as _f:
    _SYSTEM_PROMPT = _f.read()

# Load model settings from config/model_config.json (falls back to defaults if missing)
_ip         = load_model_config().get("intent_parser", {})
_OLLAMA_URL = _ip.get("endpoint", "http://localhost:11434/api/generate")
_MODEL      = _ip.get("model", "qwen2.5-coder:3b")
_TIMEOUT    = int(_ip.get("timeout_seconds", 5))

_CODE_FENCE_RE    = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?\s*```$", re.DOTALL)
_VALID_CONFIDENCE = {"high", "medium", "low"}

_FALLBACK  = {"intent": "unknown", "match_confidence": "low"}
_BASE_PROMPT = _SYSTEM_PROMPT


def _claude_fallback(question: str) -> dict:
    """Fall back to the Claude API parser on Ollama failure."""
    from core.intent_parser import parse as cloud_parse
    logger.warning("Ollama failed — falling back to Claude API")
    return cloud_parse(question)


def parse(question: str) -> dict:
    """Parse a natural language question into an intent dict via Ollama.

    Returns the same shape as intent_parser.parse():
        {'intent': 'business_health'|'deep_dive'|'agent'|'unknown',
         'match_confidence': 'high'|'medium'|'low', ...}

    Fallback chain:
      1. Try local Ollama (5s timeout)
      2. On timeout or parse failure → try Claude API
      3. If Claude also fails → return {'intent': 'unknown', 'match_confidence': 'low'}
    """
    logger.info("PARSER: Local Ollama")
    if not question or not question.strip():
        return {**_FALLBACK, "error": "empty_question", "original": question}

    prompt = _BASE_PROMPT + "\n\nQuestion: " + question

    try:
        resp = requests.post(
            _OLLAMA_URL,
            json={
                "model": _MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0},
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        text = resp.json()["response"].strip()
    except requests.exceptions.Timeout:
        logger.warning(f"Ollama timeout after {_TIMEOUT}s — falling back to Claude API")
        return _claude_fallback(question)
    except Exception as e:
        logger.warning(f"Ollama unavailable: {e} — falling back to Claude API")
        return _claude_fallback(question)

    # Strip markdown code fences if present
    fence_match = _CODE_FENCE_RE.match(text)
    if fence_match:
        text = fence_match.group(1).strip()

    # Parse JSON
    try:
        result = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        logger.warning(f"Ollama returned non-JSON: {text!r} — falling back to Claude API")
        return _claude_fallback(question)

    if not isinstance(result, dict):
        logger.warning(f"Ollama returned non-dict JSON: {result!r} — falling back to Claude API")
        return _claude_fallback(question)

    # Normalise match_confidence
    if result.get("match_confidence") not in _VALID_CONFIDENCE:
        result["match_confidence"] = (
            "low" if result.get("intent") == "unknown" else "high"
        )

    return result
