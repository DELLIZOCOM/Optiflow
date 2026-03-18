"""
Agent SQL Generator — takes a natural language question and generates SQL.

This is the brain of Agent Mode. It calls Claude with the full database schema,
company knowledge (config/company.md), and strict SQL generation rules.

Functions:
  generate_sql(question)                    — single SQL query
  generate_chain(question)                  — single query or multi-step chain
  generate_business_health_chain(question)  — health summary chain (dynamic, schema-aware)
  generate_deep_dive_chain(entity_label, question) — entity deep dive chain (dynamic)

IMPORTANT: This module NEVER executes SQL. It only generates it.
Execution happens after explicit human approval.
"""

import json
import logging
import os
import re
from datetime import date

import anthropic

from config.loader import load_model_config
from config.settings import ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Path to the schema context file (populated by setup schema discovery)
_SCHEMA_PATH = os.path.join(_ROOT, "prompts", "schema_context.txt")

# Path to company knowledge file (populated by setup wizard Step 4)
_COMPANY_MD_PATH = os.path.join(_ROOT, "config", "company.md")

# Regex to strip ```json ... ``` wrappers Claude sometimes adds.
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?\s*```$", re.DOTALL)

_client     = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY or "not-configured")
_AGENT_MODEL = load_model_config().get("agent_mode", {}).get("model", "claude-sonnet-4-6")


def _load_schema() -> str:
    """Load schema context from file. Raises FileNotFoundError if missing."""
    if not os.path.exists(_SCHEMA_PATH):
        raise FileNotFoundError(
            f"Schema context file not found: {_SCHEMA_PATH}\n"
            "Run setup → Discover Schema to populate it."
        )
    with open(_SCHEMA_PATH, encoding="utf-8") as f:
        return f.read()


def _load_company_knowledge() -> str:
    """Load company knowledge from config/company.md.

    Returns an empty string if the file doesn't exist or is empty.
    """
    if not os.path.exists(_COMPANY_MD_PATH):
        return ""
    try:
        with open(_COMPANY_MD_PATH, encoding="utf-8") as f:
            content = f.read().strip()
        return content
    except Exception as exc:
        logger.warning(f"Could not load company.md: {exc}")
        return ""


def _company_section(knowledge: str) -> str:
    """Wrap company knowledge into a prompt section, or return empty string."""
    if not knowledge:
        return ""
    return f"=== COMPANY KNOWLEDGE (use this to write better queries) ===\n{knowledge}"


def _build_system_prompt(schema: str, knowledge: str = "") -> str:
    today = date.today()
    current_date  = today.strftime("%Y-%m-%d")
    current_year  = today.year
    current_month = today.strftime("%B %Y")

    company_section = _company_section(knowledge)

    return f"""Today's date is {current_date}. When the user says 'this year' they mean {current_year}, 'this month' means {current_month}.

You are an expert SQL analyst. Your ONLY job: generate a safe, read-only SQL query that answers the user's question.

=== DATABASE SCHEMA ===
{schema}

{company_section}

=== SQL GENERATION RULES ===
1. SQL Server syntax only: use GETDATE(), DATEDIFF(), TOP, ISNULL(), CONVERT(), etc.
2. Always SELECT only. NEVER generate INSERT, UPDATE, DELETE, DROP, ALTER, EXEC, TRUNCATE, or any write/DDL operation.
3. Apply any data quality rules or filters mentioned in the company knowledge above.
4. Use LEFT JOIN when joining to lookup/master tables (some codes may have no matching record).
5. Limit to TOP 100 rows unless the question explicitly asks for all records.
6. Use human-readable column aliases (e.g., AS "Customer Name", AS "Invoice Amount").
7. Always include ORDER BY for meaningful sorting (e.g., by date DESC, amount DESC).
8. For partial name matches use LIKE '%value%'.

=== OUTPUT FORMAT ===
Respond with ONLY a valid JSON object — no markdown, no explanation outside the JSON, nothing else.

If the question CAN be answered:
{{
  "sql": "SELECT TOP 100 ... FROM ... WHERE ... ORDER BY ...",
  "explanation": "This query finds ...",
  "tables_used": ["TableA", "TableB"],
  "confidence": "high",
  "warnings": ["Any data quality concerns the user should know about"]
}}

confidence values:
- "high"   = schema fully supports the question
- "medium" = minor ambiguity or partial data coverage
- "low"    = significant data gaps or assumptions made

If the question CANNOT be answered with this database:
{{
  "sql": null,
  "explanation": "This database doesn't contain ...",
  "tables_used": [],
  "confidence": "none",
  "warnings": []
}}
"""


def _build_chain_system_prompt(schema: str, knowledge: str = "") -> str:
    today = date.today()
    current_date  = today.strftime("%Y-%m-%d")
    current_year  = today.year
    current_month = today.strftime("%B %Y")

    company_section = _company_section(knowledge)

    return f"""Today's date is {current_date}. When the user says 'this year' they mean {current_year}, 'this month' means {current_month}.

You are an expert SQL analyst. Your job: decide if a question needs a single SQL query or a CHAIN of up to 3 sequential queries, then generate them.

Use a chain when the question requires:
- Lookup then filter (e.g. "find customer code then get their orders")
- Multi-step aggregation across different tables
- Cross-domain investigation that can't be done in one query

Use a single query for everything else.

=== DATABASE SCHEMA ===
{schema}

{company_section}

=== SQL RULES ===
- SQL Server syntax only. SELECT only — no writes or DDL.
- Apply any data quality rules mentioned in the company knowledge above.
- TOP 100 unless question asks for all.
- Use LEFT JOIN to lookup/master tables.
- Human-readable column aliases. Always ORDER BY.

=== OUTPUT FORMAT ===
Return ONLY valid JSON, no markdown.

For a CHAIN:
{{
  "mode": "chain",
  "steps": [
    {{"step": 1, "sql": "SELECT ...", "explanation": "Finds ...", "tables": ["TableA"]}},
    {{"step": 2, "sql": "SELECT ...", "explanation": "Gets ...", "tables": ["TableB"]}}
  ],
  "summary_prompt": "Summarise how these results relate to the user's question",
  "confidence": "high",
  "warnings": []
}}

For a SINGLE query:
{{
  "mode": "single",
  "sql": "SELECT ...",
  "explanation": "...",
  "tables_used": ["TableA"],
  "confidence": "high",
  "warnings": []
}}

Note: In a chain, each step's SQL is standalone — it does not reference prior step results directly.
Steps share context through the summary_prompt interpretation.
"""


def _build_deep_dive_system_prompt(schema: str, knowledge: str = "") -> str:
    today = date.today()
    current_date  = today.strftime("%Y-%m-%d")

    company_section = _company_section(knowledge)

    return f"""Today's date is {current_date}.

You are an expert SQL analyst performing a deep-dive investigation.
Your job: generate a comprehensive multi-step SQL chain that gives a COMPLETE picture of the requested entity.

=== DATABASE SCHEMA ===
{schema}

{company_section}

=== DEEP DIVE RULES ===
- Create 2-5 SQL steps that together cover the entity from every relevant angle.
- Step 1: Core record details (the main record for this entity).
- Additional steps: Related records from other tables that provide context.
  For example: related transactions, history, linked records, financial data, status, etc.
- Only include steps for tables that actually relate to this entity.
- SQL Server syntax only. SELECT only — no writes.
- Apply any data quality rules from company knowledge.
- No TOP limit on deep dives (user wants everything about this entity).
- Use human-readable column aliases. Always ORDER BY.
- Each step's SQL is standalone — does not reference prior step results.

=== OUTPUT FORMAT ===
Return ONLY valid JSON, no markdown.

{{
  "mode": "deep_dive",
  "entity_label": "the entity name/code",
  "steps": [
    {{"step": 1, "sql": "SELECT ...", "explanation": "Core details for ...", "tables": ["MainTable"]}},
    {{"step": 2, "sql": "SELECT ...", "explanation": "Related ...", "tables": ["RelatedTable"]}}
  ],
  "summary_prompt": "Give a complete business summary of [entity] covering all the data retrieved.",
  "confidence": "high",
  "warnings": []
}}
"""


def _build_health_system_prompt(schema: str, knowledge: str = "") -> str:
    today = date.today()
    current_date  = today.strftime("%Y-%m-%d")
    current_month = today.strftime("%B %Y")

    company_section = _company_section(knowledge)

    return f"""Today's date is {current_date}. Current month: {current_month}.

You are an expert SQL analyst generating a business health summary.
Your job: generate 3-5 SQL queries that together give a COMPLETE business health overview.

=== DATABASE SCHEMA ===
{schema}

{company_section}

=== HEALTH SUMMARY RULES ===
- Choose the most important metrics for this specific database/business.
- Look at the schema and identify what the key business entities are.
- Generate queries that cover: pipeline/status overview, financial health, operational status, and any urgent items.
- Only include steps for tables that are likely to contain meaningful business metrics.
- SQL Server syntax only. SELECT only — no writes.
- Apply any data quality rules from company knowledge.
- TOP 50 per step is fine for health summary aggregates.
- Use human-readable column aliases.
- Use GROUP BY for counts/aggregations.

=== OUTPUT FORMAT ===
Return ONLY valid JSON, no markdown.

{{
  "mode": "chain",
  "steps": [
    {{"step": 1, "sql": "SELECT ...", "explanation": "Pipeline/status overview", "tables": ["MainTable"]}},
    {{"step": 2, "sql": "SELECT ...", "explanation": "Financial health metrics", "tables": ["FinanceTable"]}},
    {{"step": 3, "sql": "SELECT ...", "explanation": "Operational status", "tables": ["OpsTable"]}}
  ],
  "summary_prompt": "Give a concise executive business health summary covering all key areas. Lead with the most important insight. Highlight anything urgent or actionable.",
  "confidence": "high",
  "warnings": []
}}
"""


def _call_claude(system_prompt: str, user_message: str, max_tokens: int = 2000) -> str:
    """Call Claude API and return raw text response."""
    response = _client.messages.create(
        model=_AGENT_MODEL,
        max_tokens=max_tokens,
        temperature=0,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text.strip()


def _parse_json_response(text: str) -> dict | None:
    """Strip code fences and parse JSON. Returns None on failure."""
    fence_match = _CODE_FENCE_RE.match(text)
    if fence_match:
        text = fence_match.group(1).strip()
    try:
        result = json.loads(text)
        return result if isinstance(result, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _error_result(**kwargs) -> dict:
    """Return a standardised error result."""
    defaults = {
        "sql": None,
        "explanation": "An error occurred.",
        "tables_used": [],
        "confidence": "none",
        "warnings": [],
    }
    return {**defaults, **kwargs}


def generate_sql(question: str) -> dict:
    """Generate SQL for a natural language question.

    Returns:
        dict with keys: sql, explanation, tables_used, confidence, warnings.
        On failure, sql=None and confidence="none".
    """
    if not question or not question.strip():
        return _error_result(explanation="Empty question provided.")

    try:
        schema = _load_schema()
    except FileNotFoundError as e:
        logger.error(str(e))
        return _error_result(explanation=str(e), warnings=["Schema context file is missing."])

    knowledge = _load_company_knowledge()
    system_prompt = _build_system_prompt(schema, knowledge)

    try:
        text = _call_claude(system_prompt, question, max_tokens=1000)
    except Exception as e:
        logger.error(f"Claude API call failed: {e}")
        return _error_result(explanation=f"Claude API call failed: {e}", warnings=["API error — please retry."])

    result = _parse_json_response(text)
    if not result:
        logger.warning(f"Failed to parse Claude response: {text!r}")
        return _error_result(explanation="Failed to parse Claude response.", warnings=[f"Raw response: {text[:200]}"])

    return {
        "sql":         result.get("sql"),
        "explanation": result.get("explanation", ""),
        "tables_used": result.get("tables_used", []),
        "confidence":  result.get("confidence", "none"),
        "warnings":    result.get("warnings", []),
    }


def generate_chain(question: str) -> dict:
    """Generate a single SQL query or a chain of up to 3 for complex questions.

    Returns:
        For single: {mode="single", sql, explanation, tables_used, confidence, warnings}
        For chain:  {mode="chain", steps, summary_prompt, confidence, warnings}
        On failure: {mode="single", sql=None, ...error fields...}
    """
    if not question or not question.strip():
        return {**_error_result(explanation="Empty question provided."), "mode": "single"}

    try:
        schema = _load_schema()
    except FileNotFoundError as e:
        logger.error(str(e))
        return {**_error_result(explanation=str(e), warnings=["Schema context file is missing."]), "mode": "single"}

    knowledge = _load_company_knowledge()
    system_prompt = _build_chain_system_prompt(schema, knowledge)

    try:
        text = _call_claude(system_prompt, question, max_tokens=2000)
    except Exception as e:
        logger.error(f"Claude API call failed (chain): {e}")
        return {**_error_result(explanation=f"Claude API call failed: {e}", warnings=["API error — please retry."]), "mode": "single"}

    result = _parse_json_response(text)
    if not result:
        logger.warning(f"Failed to parse chain response: {text!r}")
        return {**_error_result(explanation="Failed to parse Claude response.", warnings=[f"Raw: {text[:200]}"]), "mode": "single"}

    mode = result.get("mode", "single")

    if mode == "chain":
        steps = result.get("steps", [])
        norm_steps = []
        for s in steps[:3]:
            norm_steps.append({
                "step":        s.get("step", len(norm_steps) + 1),
                "sql":         s.get("sql", ""),
                "explanation": s.get("explanation", ""),
                "tables":      s.get("tables", []),
            })
        return {
            "mode":           "chain",
            "steps":          norm_steps,
            "summary_prompt": result.get("summary_prompt", "Summarise these results for a business manager."),
            "confidence":     result.get("confidence", "medium"),
            "warnings":       result.get("warnings", []),
        }
    else:
        return {
            "mode":        "single",
            "sql":         result.get("sql"),
            "explanation": result.get("explanation", ""),
            "tables_used": result.get("tables_used", []),
            "confidence":  result.get("confidence", "none"),
            "warnings":    result.get("warnings", []),
        }


def generate_business_health_chain(question: str = "") -> dict:
    """Generate a dynamic multi-step business health summary chain.

    Uses the live schema and company knowledge to decide which tables/metrics
    are most relevant for this specific database. No hardcoded table names.

    Returns same shape as generate_chain() with mode="chain".
    """
    user_message = question or "Give me a comprehensive business health summary."

    try:
        schema = _load_schema()
    except FileNotFoundError as e:
        logger.error(str(e))
        return {**_error_result(explanation=str(e), warnings=["Schema context file is missing."]), "mode": "chain", "steps": [], "summary_prompt": ""}

    knowledge = _load_company_knowledge()
    system_prompt = _build_health_system_prompt(schema, knowledge)

    try:
        text = _call_claude(system_prompt, user_message, max_tokens=2500)
    except Exception as e:
        logger.error(f"Claude API call failed (health chain): {e}")
        return {**_error_result(explanation=f"Claude API call failed: {e}", warnings=["API error — please retry."]), "mode": "chain", "steps": [], "summary_prompt": ""}

    result = _parse_json_response(text)
    if not result:
        logger.warning(f"Failed to parse health chain response: {text!r}")
        return {**_error_result(explanation="Failed to parse Claude response."), "mode": "chain", "steps": [], "summary_prompt": ""}

    steps = result.get("steps", [])
    norm_steps = []
    for s in steps[:5]:
        norm_steps.append({
            "step":        s.get("step", len(norm_steps) + 1),
            "sql":         s.get("sql", ""),
            "explanation": s.get("explanation", ""),
            "tables":      s.get("tables", []),
        })

    return {
        "mode":           "chain",
        "steps":          norm_steps,
        "summary_prompt": result.get("summary_prompt", "Give a concise executive business health summary."),
        "confidence":     result.get("confidence", "medium"),
        "warnings":       result.get("warnings", []),
    }


def generate_deep_dive_chain(entity_label: str, question: str = "") -> dict:
    """Generate a dynamic deep-dive chain for any entity.

    Uses the live schema and company knowledge to determine which tables
    relate to this entity and generates SQL for each. No hardcoded tables.

    Args:
        entity_label: The name/code of the entity (e.g. "P-2024-001", "Acme Corp").
        question:     The user's original question (for context).

    Returns:
        dict with mode="deep_dive", entity_label, steps, summary_prompt, confidence, warnings.
    """
    if not entity_label:
        return {
            "mode":           "deep_dive",
            "entity_label":   "Unknown",
            "steps":          [],
            "summary_prompt": "",
            "confidence":     "none",
            "warnings":       ["No entity label provided."],
        }

    try:
        schema = _load_schema()
    except FileNotFoundError as e:
        logger.error(str(e))
        return {
            "mode":           "deep_dive",
            "entity_label":   entity_label,
            "steps":          [],
            "summary_prompt": "",
            "confidence":     "none",
            "warnings":       [str(e)],
        }

    knowledge = _load_company_knowledge()
    system_prompt = _build_deep_dive_system_prompt(schema, knowledge)

    context = f"Original question: {question}\n\n" if question else ""
    user_message = f"{context}Generate a complete deep-dive investigation for entity: {entity_label}"

    try:
        text = _call_claude(system_prompt, user_message, max_tokens=2500)
    except Exception as e:
        logger.error(f"Claude API call failed (deep dive): {e}")
        return {
            "mode":           "deep_dive",
            "entity_label":   entity_label,
            "steps":          [],
            "summary_prompt": "",
            "confidence":     "none",
            "warnings":       [f"API error: {e}"],
        }

    result = _parse_json_response(text)
    if not result:
        logger.warning(f"Failed to parse deep dive response: {text!r}")
        return {
            "mode":           "deep_dive",
            "entity_label":   entity_label,
            "steps":          [],
            "summary_prompt": "",
            "confidence":     "none",
            "warnings":       ["Failed to parse Claude response."],
        }

    steps = result.get("steps", [])
    norm_steps = []
    for s in steps[:5]:
        norm_steps.append({
            "step":        s.get("step", len(norm_steps) + 1),
            "sql":         s.get("sql", ""),
            "explanation": s.get("explanation", ""),
            "tables":      s.get("tables", []),
        })

    return {
        "mode":           "deep_dive",
        "entity_label":   result.get("entity_label", entity_label),
        "steps":          norm_steps,
        "summary_prompt": result.get("summary_prompt", f"Give a complete summary of {entity_label}."),
        "confidence":     result.get("confidence", "medium"),
        "warnings":       result.get("warnings", []),
    }
