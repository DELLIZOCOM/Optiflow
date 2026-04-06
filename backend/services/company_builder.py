"""
Company builder — generates and updates company.md knowledge document.

Extracted from app.py.
"""

import json
import logging
import re

from backend.ai.client import get_completion, RateLimitExhausted
from backend.ai.prompts import COMPANY_DRAFT_SYSTEM, COMPANY_FOLLOWUP_SYSTEM
from backend.config.paths import SCHEMA_CONTEXT_PATH, COMPANY_MD_PATH

logger = logging.getLogger(__name__)


def generate_company_draft(db_name: str) -> dict:
    """Generate a rich company.md draft by sending schema_context.txt to the AI.

    Returns {"success": True, "content": "..."} or {"success": False, "error": "..."}.
    """
    if not SCHEMA_CONTEXT_PATH.exists():
        return {"success": False, "error": "Schema not discovered yet — complete Step 4 first."}

    try:
        with open(SCHEMA_CONTEXT_PATH, encoding="utf-8") as f:
            schema_content = f.read()
    except Exception as e:
        return {"success": False, "error": f"Could not read schema: {e}"}

    try:
        content = get_completion(
            system=COMPANY_DRAFT_SYSTEM,
            user=f"Database name: {db_name}\n\n{schema_content}",
            max_tokens=4000,
            temperature=0,
        )
        return {"success": True, "content": content}
    except RateLimitExhausted as rl:
        return {"success": False, "error": "Rate limited", "retry_after": rl.retry_after}
    except Exception as e:
        logger.error(f"generate-company-draft error: {e}")
        return {"success": False, "error": str(e)}


def generate_company_followup(draft: str) -> dict:
    """Generate targeted follow-up questions from the AI-generated company.md draft.

    Returns {"success": True, "questions": [...]} or {"success": True, "questions": []}.
    """
    if not draft:
        return {"success": True, "questions": []}

    try:
        text = get_completion(
            system=COMPANY_FOLLOWUP_SYSTEM,
            user=f"Here is the company knowledge document I generated:\n\n{draft[:3000]}\n\nGenerate follow-up questions.",
            max_tokens=600,
            temperature=0,
        )
        fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if fence:
            text = fence.group(1)
        questions = json.loads(text)
        if not isinstance(questions, list):
            questions = []
        return {"success": True, "questions": questions[:5]}
    except Exception as e:
        logger.warning(f"company-followup non-fatal: {e}")
        return {"success": True, "questions": []}
