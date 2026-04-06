"""
Table selector — two-step schema resolution for the SQL generator.

Given a natural-language question, asks the AI to pick the relevant tables
and classify the query type (single / chain / deep_dive). Falls back to the
full schema_context.txt when the table count is small or selection fails.

Extracted from services/sql_generator.py to keep that module under 200 lines.
"""

import json
import logging
import re

from backend.ai.client import get_completion
from backend.ai.prompts import TABLE_SELECT_SYSTEM
from backend.services.schema_loader import (
    load_schema,
    load_company_knowledge,
    load_schema_index,
    load_table_schemas,
    extract_use_when_sections,
)

logger = logging.getLogger(__name__)

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?\s*```$", re.DOTALL)
_MIN_TABLES_FOR_TWO_STEP = 15


def _call_table_select(user_msg: str) -> str:
    return get_completion(system=TABLE_SELECT_SYSTEM, user=user_msg, max_tokens=400, temperature=0)


def select_tables(question: str, schema_index: str, purpose: str = "query", company_hints: str = "") -> dict:
    """Ask the AI which tables to use and what query type to generate.

    Returns {"tables": [...], "query_type": "single|chain|deep_dive", "reason": "..."}.
    """
    hints_section = (
        f"\n\nBusiness context (use when asked about):\n{company_hints}"
        if company_hints else ""
    )
    if purpose == "health":
        user_msg = (
            "Select the most important tables for a business health summary "
            "(metrics, pipeline, financials, operations). Use query_type 'chain'.\n\n"
            f"Available tables:\n{schema_index}{hints_section}"
        )
    elif purpose == "entity":
        user_msg = (
            f"Select ALL tables that reference entity: {question}. Use query_type 'deep_dive'.\n\n"
            f"Available tables:\n{schema_index}{hints_section}"
        )
    else:
        user_msg = (
            f"Question: {question}\n\n"
            f"Available tables:\n{schema_index}{hints_section}\n\n"
            "Select tables and classify the query type."
        )

    _fallback = {"tables": [], "query_type": "chain", "reason": ""}
    try:
        text = _call_table_select(user_msg)
        fence_match = _CODE_FENCE_RE.match(text)
        if fence_match:
            text = fence_match.group(1).strip()
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "tables" in parsed:
            return {
                "tables":     [str(t) for t in parsed.get("tables", [])],
                "query_type": parsed.get("query_type", "chain"),
                "reason":     parsed.get("reason", ""),
            }
        if isinstance(parsed, list):
            return {"tables": [str(t) for t in parsed], "query_type": "chain", "reason": ""}
    except Exception as exc:
        logger.warning(f"Table selection failed ({purpose}): {exc}")
    return _fallback


def resolve_schema(question: str, purpose: str = "query", cached_tables: list | None = None) -> tuple:
    """Return (schema_text, tables) for a question.

    If the schema is small (≤ _MIN_TABLES_FOR_TWO_STEP) the full schema_context.txt
    is returned directly. Otherwise AI selects the relevant tables.
    """
    index = load_schema_index()
    if index is None:
        return load_schema(), None
    table_count = sum(1 for line in index.splitlines() if line.strip())
    if table_count <= _MIN_TABLES_FOR_TWO_STEP:
        return load_schema(), None
    if cached_tables:
        tables = cached_tables
    else:
        company_hints = extract_use_when_sections(load_company_knowledge())
        tables = select_tables(question, index, purpose, company_hints)["tables"]
    if not tables:
        return load_schema(), None
    schema_text = load_table_schemas(tables)
    if not schema_text:
        return load_schema(), None
    return schema_text, tables


def resolve_schema_with_type(question: str) -> tuple:
    """Return (schema_text, tables, query_type) for a question.

    Same as resolve_schema() but also returns the AI-classified query_type.
    """
    index = load_schema_index()
    if index is None:
        return load_schema(), None, "chain"
    table_count = sum(1 for line in index.splitlines() if line.strip())
    if table_count <= _MIN_TABLES_FOR_TWO_STEP:
        return load_schema(), None, "chain"
    company_hints = extract_use_when_sections(load_company_knowledge())
    selection = select_tables(question, index, "query", company_hints)
    tables = selection["tables"]
    query_type = selection["query_type"]
    logger.info(f"Table selection: query_type={query_type!r}  tables={tables}")
    if not tables:
        return load_schema(), None, query_type
    schema_text = load_table_schemas(tables)
    if not schema_text:
        return load_schema(), None, query_type
    return schema_text, tables, query_type
