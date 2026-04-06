"""
Interpreter service — calls AI to translate query results into business insights.
"""

import json
import logging

from backend.ai.client import get_completion
from backend.ai.prompts import ADVISOR_SYSTEM
from backend.services.sql_generator import _load_company_knowledge

logger = logging.getLogger(__name__)


def interpret_results(question: str, rows: list, total_rows: int) -> str:
    """Call AI to interpret query results as a business advisor."""
    rows_json = json.dumps(rows, default=str)
    knowledge = _load_company_knowledge()
    knowledge_section = f"\n\nCompany knowledge:\n{knowledge}" if knowledge else ""

    return get_completion(
        system=ADVISOR_SYSTEM + knowledge_section,
        user=(
            f"The user asked: {question}\n\n"
            f"Query results ({total_rows} rows):\n{rows_json}"
        ),
        max_tokens=800,
        temperature=0,
    )


def interpret_chain_results(question: str, step_results: list, summary_prompt: str, entity_label: str = "") -> str:
    """Call AI to synthesise results from multiple SQL steps."""
    parts = []
    for sr in step_results:
        step_num    = sr.get("step", "?")
        explanation = sr.get("explanation", "")
        rows        = sr.get("rows", [])
        rows_json   = json.dumps(rows, default=str)
        parts.append(f"=== Step {step_num}: {explanation} ({len(rows)} rows) ===\n{rows_json}")

    combined = "\n\n".join(parts)
    context  = f"The user asked: {question}\n\n{combined}" if question else combined

    knowledge = _load_company_knowledge()
    knowledge_section = f"\n\nCompany knowledge:\n{knowledge}" if knowledge else ""

    chain_system = (
        ADVISOR_SYSTEM
        + "\n\nFor multi-step results: use ### headings to organise each area. "
        "Synthesise across all steps — don't just repeat them. "
        "Lead with the most important cross-cutting insight."
        + knowledge_section
    )

    return get_completion(
        system=chain_system,
        user=f"{summary_prompt}\n\n{context}",
        max_tokens=1200,
        temperature=0,
    )
