"""
Intent parser — calls Claude API to extract intent from natural language.

Takes a user question string, sends it to Claude with the OptiFlow system
prompt, and returns a parsed intent dict like {'intent': 'amc_expiry', 'days': 60}.
This module ONLY talks to the Claude API. It does not touch the database,
query engine, or response formatter.
"""

import json
import logging
import os
import re

import anthropic

from config.settings import ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

# Load system prompt once at module level.
_PROMPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "prompts",
    "system_prompt.txt",
)
with open(_PROMPT_PATH) as _f:
    _SYSTEM_PROMPT = _f.read()

# Regex to strip ```json ... ``` wrappers Claude sometimes adds.
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?\s*```$", re.DOTALL)

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def parse(question: str) -> dict:
    """Parse a natural language question into an intent dict.

    Args:
        question: The user's question, e.g. "Which AMC contracts expire soon?"

    Returns:
        Parsed intent dict, e.g. {'intent': 'amc_expiry', 'days': 60}.
        On failure returns {'intent': 'unknown', 'error': '...', 'original': question}.
    """
    if not question or not question.strip():
        return {"intent": "unknown", "error": "empty_question", "original": question}

    # Call Claude API
    try:
        response = _client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=200,
            temperature=0,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": question}],
        )
        text = response.content[0].text.strip()
    except Exception as e:
        logger.error(f"Claude API call failed: {e}")
        return {"intent": "unknown", "error": "api_failed", "original": question}

    # Strip markdown code fences if present
    fence_match = _CODE_FENCE_RE.match(text)
    if fence_match:
        text = fence_match.group(1).strip()

    # Parse JSON
    try:
        result = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"Failed to parse Claude response as JSON: {text!r}")
        return {"intent": "unknown", "error": "parse_failed", "original": question}

    if not isinstance(result, dict):
        logger.warning(f"Claude returned non-dict JSON: {result!r}")
        return {"intent": "unknown", "error": "parse_failed", "original": question}

    return result
