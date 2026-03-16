"""
Local intent parser — drop-in replacement for intent_parser.py.

Uses a local Ollama model instead of the Claude API.
Identical function signature: parse(question) -> dict

If Ollama is unavailable or returns garbage, returns
{'intent': 'unknown', 'match_confidence': 'low'} which causes
_run_pipeline to fall through to Agent Mode (Claude still runs).
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

_OLLAMA_URL  = "http://localhost:11434/api/generate"
_MODEL       = "qwen2.5-coder:3b"
_TIMEOUT     = 10          # seconds — if Ollama is slow/down, fall back to Agent Mode

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?\s*```$", re.DOTALL)
_VALID_CONFIDENCE = {"high", "medium", "low"}

_FALLBACK = {"intent": "unknown", "match_confidence": "low"}


def parse(question: str) -> dict:
    """Parse a natural language question into an intent dict via Ollama.

    Returns the same shape as intent_parser.parse():
        {'intent': '...', 'match_confidence': 'high'|'medium'|'low', ...params}

    On any failure (Ollama down, timeout, bad JSON) returns:
        {'intent': 'unknown', 'match_confidence': 'low'}
    which triggers Agent Mode as a safe fallback.
    """
    if not question or not question.strip():
        return {**_FALLBACK, "error": "empty_question", "original": question}

    prompt = _SYSTEM_PROMPT + "\n\nQuestion: " + question

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
        logger.warning(f"Ollama timeout after {_TIMEOUT}s — falling back to Agent Mode")
        return {**_FALLBACK, "error": "ollama_timeout"}
    except Exception as e:
        logger.warning(f"Ollama unavailable: {e} — falling back to Agent Mode")
        return {**_FALLBACK, "error": "ollama_unavailable"}

    # Strip markdown code fences if present
    fence_match = _CODE_FENCE_RE.match(text)
    if fence_match:
        text = fence_match.group(1).strip()

    # Parse JSON
    try:
        result = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        logger.warning(f"Ollama returned non-JSON: {text!r}")
        return {**_FALLBACK, "error": "parse_failed"}

    if not isinstance(result, dict):
        logger.warning(f"Ollama returned non-dict JSON: {result!r}")
        return {**_FALLBACK, "error": "parse_failed"}

    # Normalise match_confidence
    if result.get("match_confidence") not in _VALID_CONFIDENCE:
        result["match_confidence"] = (
            "low" if result.get("intent") == "unknown" else "high"
        )

    return result
