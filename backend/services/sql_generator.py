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

from backend.ai.client import get_completion

logger = logging.getLogger(__name__)

# Paths loaded from central registry
from backend.config.paths import SCHEMA_CONTEXT_PATH as _P_SCHEMA
_SCHEMA_PATH = str(_P_SCHEMA)

# Paths for split-schema two-step generation
from backend.config.paths import SCHEMA_INDEX_PATH as _P_IDX
_SCHEMA_INDEX_PATH = str(_P_IDX)
from backend.config.paths import TABLES_DIR as _P_TABLES
_TABLES_DIR = str(_P_TABLES)

# Path to company knowledge file (populated by setup wizard Step 4)
from backend.config.paths import COMPANY_MD_PATH as _P_CO
_COMPANY_MD_PATH = str(_P_CO)

# Regex to strip ```json ... ``` wrappers Claude sometimes adds.
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?\s*```$", re.DOTALL)

# Only use two-step generation when the schema has more than this many tables.
# Smaller databases fit comfortably in one call.
_MIN_TABLES_FOR_TWO_STEP = 15

# System prompt for Step 1 (cheap table-selection call)
_TABLE_SELECT_SYSTEM = """You are a database expert. Select which tables are needed and classify the query type.

Return ONLY a JSON object — no explanation, no markdown, nothing else.
Example: {"tables": ["TableA", "TableB"], "query_type": "single", "reason": "Customer list comes from TableA, linked to TableB via CustomerID"}

query_type rules:
- "single"    — one focused query (a specific metric, filter, or lookup)
- "chain"     — multiple queries needed (business overview, cross-table comparison, health summary — select 4-6 tables)
- "deep_dive" — one entity investigated across ALL related tables (e.g. "tell me everything about project P-001")

Table selection rules:
- "single": return 1-3 tables directly needed
- "chain":  return 4-6 most important tables (prefer high row-count tables with status/amount columns)
- "deep_dive": return ALL tables that could reference this entity

If nothing clearly matches, default to query_type "chain" with the 4 most important tables.
"""


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


def _load_schema_index() -> str | None:
    """Load schema_index.txt (one line per table). Returns None if not present."""
    if not os.path.exists(_SCHEMA_INDEX_PATH):
        return None
    try:
        with open(_SCHEMA_INDEX_PATH, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def _load_table_schemas(table_names: list) -> str:
    """Load per-table detail files for the given table names and concatenate them."""
    if not os.path.exists(_TABLES_DIR):
        return ""

    # Build case-insensitive filename lookup once
    available: dict = {}
    try:
        for fname in os.listdir(_TABLES_DIR):
            if fname.endswith(".txt"):
                available[fname[:-4].lower()] = os.path.join(_TABLES_DIR, fname)
    except Exception:
        return ""

    parts = []
    for name in table_names:
        safe = re.sub(r"[^\w\-]", "_", name)
        path = available.get(safe.lower()) or available.get(name.lower())
        if path:
            try:
                with open(path, encoding="utf-8") as f:
                    parts.append(f.read())
            except Exception:
                pass

    return "\n\n".join(parts)


def _extract_use_when_sections(company_md: str) -> str:
    """Extract 'Use when asked about' lines from company.md for table selection context.

    Returns a compact multi-line string like:
        TableName: project pipeline, active projects, ...
        OtherTable: invoices, billing, pending payments, ...
    """
    if not company_md:
        return ""
    lines = []
    current_table = None
    for line in company_md.splitlines():
        # ### TableName (N rows) heading
        if line.startswith("### "):
            current_table = line[4:].split("(")[0].strip()
        elif current_table and line.strip().lower().startswith("**use when asked about:**"):
            phrase = line.split(":**", 1)[-1].strip().lstrip("*").strip()
            if phrase:
                lines.append(f"{current_table}: {phrase}")
    return "\n".join(lines)


def _select_tables_with_type(
    question: str,
    schema_index: str,
    purpose: str = "query",
    company_hints: str = "",
) -> dict:
    """Step 1: Ask AI to pick tables and classify query type (cheap call).

    Returns {"tables": [...], "query_type": "single"|"chain"|"deep_dive", "reason": "..."}
    Falls back to {"tables": [], "query_type": "chain", "reason": ""} on failure.
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
        text = _call_claude(_TABLE_SELECT_SYSTEM, user_msg, max_tokens=400)
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
        # Old format compat: if AI returned a plain array despite updated prompt
        if isinstance(parsed, list):
            return {"tables": [str(t) for t in parsed], "query_type": "chain", "reason": ""}
    except Exception as exc:
        logger.warning(f"Table selection failed ({purpose}): {exc}")

    return _fallback


def _select_tables_for_question(
    question: str,
    schema_index: str,
    purpose: str = "query",
    company_hints: str = "",
) -> list:
    """Backward-compatible wrapper — returns just the table names list."""
    return _select_tables_with_type(question, schema_index, purpose, company_hints)["tables"]


def _resolve_schema(question: str, purpose: str = "query", cached_tables: list | None = None) -> tuple:
    """Resolve the schema text and selected tables for SQL generation.

    Two-step path (schema_index.txt exists and table count > threshold):
        Step 1 — cheap call selects relevant table names, enriched by company.md hints
        Step 2 — load only those table detail files
        Returns (detail_schema, [table_names])

    Fallback path (split files absent or selection empty):
        Returns (full_schema_context.txt, None)

    Raises FileNotFoundError if schema context is also missing.
    """
    index = _load_schema_index()

    if index is None:
        return _load_schema(), None

    table_count = sum(1 for line in index.splitlines() if line.strip())
    if table_count <= _MIN_TABLES_FOR_TWO_STEP:
        # Small DB — full schema comfortably fits; skip two-step overhead
        return _load_schema(), None

    if cached_tables:
        tables = cached_tables
    else:
        # Extract "Use when asked about" hints from company.md for richer selection
        company_hints = _extract_use_when_sections(_load_company_knowledge())
        tables = _select_tables_for_question(question, index, purpose, company_hints)

    if not tables:
        return _load_schema(), None

    schema_text = _load_table_schemas(tables)
    if not schema_text:
        return _load_schema(), None

    return schema_text, tables


def _resolve_schema_with_type(question: str) -> tuple:
    """Like _resolve_schema but also returns query_type from table selection.

    Returns (schema_text, tables, query_type).
    query_type is "single" | "chain" | "deep_dive".
    Falls back to ("full_schema", None, "chain") when split schema is absent.
    """
    index = _load_schema_index()

    if index is None:
        return _load_schema(), None, "chain"

    table_count = sum(1 for line in index.splitlines() if line.strip())
    if table_count <= _MIN_TABLES_FOR_TWO_STEP:
        return _load_schema(), None, "chain"

    company_hints = _extract_use_when_sections(_load_company_knowledge())
    selection = _select_tables_with_type(question, index, "query", company_hints)
    tables = selection["tables"]
    query_type = selection["query_type"]
    logger.info(f"Table selection: query_type={query_type!r}  tables={tables}")

    if not tables:
        return _load_schema(), None, query_type

    schema_text = _load_table_schemas(tables)
    if not schema_text:
        return _load_schema(), None, query_type

    return schema_text, tables, query_type


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

=== PRECISION RULE ===
The SQL query IS the answer. Generate SQL that computes exactly what was asked.
The interpretation layer translates results into insights — it will NOT aggregate, calculate, or analyse raw rows.
- If asked for a total → use SUM()
- If asked for a comparison → return both values in one result set with clear column aliases
- If asked for a trend → GROUP BY period, one row per period, ORDER BY period
- If asked what is highest/lowest → ORDER BY ... DESC/ASC with TOP N
- NEVER return raw row dumps expecting interpretation to aggregate
- Most queries should return fewer than 20 rows of pre-computed results
- When a list is genuinely needed (e.g. "show all invoices for customer X"), return full rows — do not aggregate

=== SQL GENERATION RULES ===
1. SQL Server syntax only: use GETDATE(), DATEDIFF(), TOP, ISNULL(), CONVERT(), etc.
2. Always SELECT only. NEVER generate INSERT, UPDATE, DELETE, DROP, ALTER, EXEC, TRUNCATE, or any write/DDL operation.
3. Apply any data quality rules or filters mentioned in the company knowledge above.
4. Use LEFT JOIN when joining to lookup/master tables (some codes may have no matching record).
5. Limit to TOP 100 rows unless the question explicitly asks for all records.
6. Use human-readable column aliases (e.g., AS "Customer Name", AS "Invoice Amount").
7. Always include ORDER BY for meaningful sorting (e.g., by date DESC, amount DESC).
8. For partial name matches use LIKE '%value%'.
9. SQL SERVER STRICT GROUP BY: Every column in SELECT and ORDER BY must be in GROUP BY or inside an aggregate (COUNT, SUM, AVG, MIN, MAX). Wrap context-only columns in MAX() or MIN().
   BAD:  SELECT Customer, Plant_Date, COUNT(*) FROM ProSt GROUP BY Customer ORDER BY Plant_Date
   GOOD: SELECT Customer, MAX(Plant_Date) AS LatestPlant, COUNT(*) AS Total FROM ProSt GROUP BY Customer ORDER BY MAX(Plant_Date) DESC

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

You are an expert SQL analyst. Your job: decide if a question needs a single SQL query or a CHAIN of up to 5 sequential queries, then generate them.

Use a chain when the question requires:
- Multi-table overview or business summary (health, pipeline, financial snapshot)
- Lookup then filter (e.g. "find customer code then get their orders")
- Multi-step aggregation across different tables
- Cross-domain investigation that can't be done in one query

Use a single query when the question targets one specific metric, filter, or lookup.

=== DATABASE SCHEMA ===
{schema}

{company_section}

=== PRECISION RULE ===
The SQL IS the answer — compute exactly what was asked. Interpretation will not aggregate raw rows.
- Total → SUM(). Comparison → both values in one result set. Trend → GROUP BY period. Top/Bottom → ORDER BY + TOP N.
- Most queries: fewer than 20 pre-computed rows. Full list only when explicitly needed.

=== SQL RULES ===
- SQL Server syntax only. SELECT only — no writes or DDL.
- Apply any data quality rules mentioned in the company knowledge above.
- TOP 100 unless question asks for all.
- Use LEFT JOIN to lookup/master tables.
- Human-readable column aliases. Always ORDER BY.
- SQL SERVER STRICT GROUP BY: Every column in SELECT/ORDER BY must be in GROUP BY or inside an aggregate. Wrap context columns in MAX() or MIN().
  BAD: SELECT Customer, Plant_Date, COUNT(*) FROM T GROUP BY Customer ORDER BY Plant_Date
  GOOD: SELECT Customer, MAX(Plant_Date) AS LatestPlant, COUNT(*) AS Total FROM T GROUP BY Customer ORDER BY MAX(Plant_Date) DESC

=== OUTPUT FORMAT ===
Return ONLY valid JSON, no markdown.

For a CHAIN (up to 5 steps):
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

=== PRECISION RULE ===
Each step's SQL IS the answer for that angle of the entity. Return complete, useful result sets.
Do not aggregate where the full list is needed. Do aggregate where counts/totals are the point.

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
- SQL SERVER STRICT GROUP BY: Every column in SELECT/ORDER BY must be in GROUP BY or inside an aggregate. Wrap context columns in MAX() or MIN().

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

=== PRECISION RULE ===
Each step's SQL must return pre-computed aggregates, not raw rows.
Use SUM, COUNT, GROUP BY so interpretation receives ready-to-read numbers, not data to crunch.

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
- SQL SERVER STRICT GROUP BY: Every column in SELECT/ORDER BY must be in GROUP BY or inside an aggregate. Wrap context columns in MAX() or MIN().

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


_FIX_SQL_SYSTEM = """You are an expert SQL analyst. A SQL Server query failed with an error.
Your ONLY job: return a corrected JSON object with the fixed SQL.

SQL SERVER STRICT GROUP BY RULE: Every column in SELECT and ORDER BY must either appear in GROUP BY or be inside an aggregate function (COUNT, SUM, AVG, MIN, MAX). Wrap non-grouped context columns in MAX() or MIN().

Return ONLY valid JSON (no markdown), same shape as the original:
{"sql": "SELECT ...", "explanation": "Fixed by ...", "tables_used": [], "confidence": "high", "warnings": []}
"""


def fix_sql(question: str, failed_sql: str, error: str, tables_used: list | None = None) -> dict:
    """Ask the AI to fix a failed SQL query given the error message.

    If tables_used is provided, loads only those table detail files (faster).
    Returns same shape as generate_sql().
    """
    try:
        if tables_used:
            schema = _load_table_schemas(tables_used) or _load_schema()
        else:
            schema, _ = _resolve_schema(question, purpose="query")
    except FileNotFoundError as e:
        return _error_result(explanation=str(e))

    knowledge = _load_company_knowledge()
    company_section = _company_section(knowledge)
    system = _FIX_SQL_SYSTEM + (f"\n\n=== DATABASE SCHEMA ===\n{schema}\n\n{company_section}" if company_section else f"\n\n=== DATABASE SCHEMA ===\n{schema}")

    user_message = (
        f"The user asked: {question}\n\n"
        f"You generated this SQL:\n{failed_sql}\n\n"
        f"SQL Server rejected it with this error:\n{error}\n\n"
        "Fix the SQL and return the corrected JSON."
    )

    try:
        text = _call_claude(system, user_message, max_tokens=1000)
    except Exception as e:
        logger.error(f"fix_sql AI call failed: {e}")
        return _error_result(explanation=f"AI fix failed: {e}")

    result = _parse_json_response(text)
    if not result or not result.get("sql"):
        return _error_result(explanation="AI could not produce a corrected SQL.")

    return {
        "sql":         result.get("sql"),
        "explanation": result.get("explanation", ""),
        "tables_used": result.get("tables_used", []),
        "confidence":  result.get("confidence", "medium"),
        "warnings":    result.get("warnings", []),
    }


def _call_claude(system_prompt: str, user_message: str, max_tokens: int = 2000) -> str:
    """Call the configured AI provider and return raw text response."""
    return get_completion(
        system=system_prompt,
        user=user_message,
        max_tokens=max_tokens,
        temperature=0,
    )


def _parse_json_response(text: str) -> dict | None:
    """Extract and parse a JSON object from an AI response.

    Handles: raw JSON, ```json ... ``` fences, JSON anywhere in the text,
    and responses with preamble text before the JSON block.
    Returns None if no valid JSON dict can be extracted.
    """
    if not text:
        return None

    # 1. Strip code fences (``` or ```json at start)
    fence_match = _CODE_FENCE_RE.match(text.strip())
    if fence_match:
        candidate = fence_match.group(1).strip()
        try:
            result = json.loads(candidate)
            return result if isinstance(result, dict) else None
        except (json.JSONDecodeError, ValueError):
            pass

    # 2. Direct parse (model returned raw JSON as instructed)
    try:
        result = json.loads(text.strip())
        return result if isinstance(result, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass

    # 3. Find first '{' ... last '}' — handles preamble/postamble text
    start = text.find('{')
    end   = text.rfind('}')
    if start != -1 and end > start:
        try:
            result = json.loads(text[start:end + 1])
            return result if isinstance(result, dict) else None
        except (json.JSONDecodeError, ValueError):
            pass

    # 4. Find JSON inside any code fence (search, not just at start)
    any_fence = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
    if any_fence:
        try:
            result = json.loads(any_fence.group(1).strip())
            return result if isinstance(result, dict) else None
        except (json.JSONDecodeError, ValueError):
            pass

    logger.warning(f"_parse_json_response: could not extract JSON from response (first 200): {text[:200]!r}")
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
        schema, _ = _resolve_schema(question, purpose="query")
    except FileNotFoundError as e:
        logger.error(str(e))
        return _error_result(explanation=str(e), warnings=["Schema context file is missing."])

    knowledge = _load_company_knowledge()
    system_prompt = _build_system_prompt(schema, knowledge)

    try:
        text = _call_claude(system_prompt, question, max_tokens=1000)
    except Exception as e:
        logger.error(f"AI call failed: {e}")
        return _error_result(explanation=f"AI call failed: {e}", warnings=["API error — please retry."])

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
        schema, _ = _resolve_schema(question, purpose="query")
    except FileNotFoundError as e:
        logger.error(str(e))
        return {**_error_result(explanation=str(e), warnings=["Schema context file is missing."]), "mode": "single"}

    knowledge = _load_company_knowledge()
    system_prompt = _build_chain_system_prompt(schema, knowledge)

    try:
        text = _call_claude(system_prompt, question, max_tokens=2000)
    except Exception as e:
        logger.error(f"AI call failed (chain): {e}")
        return {**_error_result(explanation=f"AI call failed: {e}", warnings=["API error — please retry."]), "mode": "single"}

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
        schema, _ = _resolve_schema(user_message, purpose="health")
    except FileNotFoundError as e:
        logger.error(str(e))
        return {**_error_result(explanation=str(e), warnings=["Schema context file is missing."]), "mode": "chain", "steps": [], "summary_prompt": ""}

    knowledge = _load_company_knowledge()
    system_prompt = _build_health_system_prompt(schema, knowledge)

    try:
        text = _call_claude(system_prompt, user_message, max_tokens=2500)
    except Exception as e:
        logger.error(f"AI call failed (health chain): {e}")
        return {**_error_result(explanation=f"AI call failed: {e}", warnings=["API error — please retry."]), "mode": "chain", "steps": [], "summary_prompt": ""}

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
        schema, _ = _resolve_schema(entity_label, purpose="entity")
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
        logger.error(f"AI call failed (deep dive): {e}")
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


def generate_universal(question: str) -> dict:
    """Universal entry point — replaces the intent-classifier routing.

    Uses table selection to determine query_type, then routes to the appropriate
    SQL generator. No local intent parser. No separate mode detection.

    Returns one of:
        {mode: "single",    sql, explanation, tables_used, confidence, warnings}
        {mode: "chain",     steps, summary_prompt, confidence, warnings}
        {mode: "deep_dive", entity_label, steps, summary_prompt, confidence, warnings}
    On error: {mode: "single", sql: None, confidence: "none", ...}
    """
    if not question or not question.strip():
        return {**_error_result(explanation="Empty question."), "mode": "single"}

    try:
        schema, tables, query_type = _resolve_schema_with_type(question)
    except FileNotFoundError as e:
        logger.error(str(e))
        return {**_error_result(explanation=str(e), warnings=["Schema file missing."]), "mode": "single"}

    knowledge = _load_company_knowledge()

    # ── Single focused query ──────────────────────────────────────────────
    if query_type == "single":
        system_prompt = _build_system_prompt(schema, knowledge)
        try:
            text = _call_claude(system_prompt, question, max_tokens=1000)
        except Exception as e:
            logger.error(f"generate_universal single failed: {e}")
            return {**_error_result(explanation=str(e), warnings=["API error — please retry."]), "mode": "single"}
        result = _parse_json_response(text)
        if not result:
            return {**_error_result(explanation="Failed to parse AI response."), "mode": "single"}
        return {
            "mode":        "single",
            "sql":         result.get("sql"),
            "explanation": result.get("explanation", ""),
            "tables_used": result.get("tables_used", tables or []),
            "confidence":  result.get("confidence", "none"),
            "warnings":    result.get("warnings", []),
        }

    # ── Deep dive — one entity investigated across all related tables ─────
    if query_type == "deep_dive":
        system_prompt = _build_deep_dive_system_prompt(schema, knowledge)
        user_message = f"Generate a complete deep-dive investigation for: {question}"
        try:
            text = _call_claude(system_prompt, user_message, max_tokens=2500)
        except Exception as e:
            logger.error(f"generate_universal deep_dive failed: {e}")
            return {"mode": "deep_dive", "entity_label": question, "steps": [], "summary_prompt": "", "confidence": "none", "warnings": [str(e)]}
        result = _parse_json_response(text)
        if not result:
            return {"mode": "deep_dive", "entity_label": question, "steps": [], "summary_prompt": "", "confidence": "none", "warnings": ["Failed to parse AI response."]}
        norm_steps = [
            {"step": s.get("step", i + 1), "sql": s.get("sql", ""), "explanation": s.get("explanation", ""), "tables": s.get("tables", [])}
            for i, s in enumerate(result.get("steps", [])[:5])
        ]
        return {
            "mode":           "deep_dive",
            "entity_label":   result.get("entity_label", question),
            "steps":          norm_steps,
            "summary_prompt": result.get("summary_prompt", f"Give a complete summary of: {question}"),
            "confidence":     result.get("confidence", "medium"),
            "warnings":       result.get("warnings", []),
        }

    # ── Chain — multi-step or single depending on what AI decides ─────────
    system_prompt = _build_chain_system_prompt(schema, knowledge)
    try:
        text = _call_claude(system_prompt, question, max_tokens=2500)
    except Exception as e:
        logger.error(f"generate_universal chain failed: {e}")
        return {**_error_result(explanation=str(e), warnings=["API error — please retry."]), "mode": "single"}

    result = _parse_json_response(text)
    if not result:
        return {**_error_result(explanation="Failed to parse AI response."), "mode": "single"}

    mode = result.get("mode", "single")
    if mode == "chain":
        norm_steps = [
            {"step": s.get("step", i + 1), "sql": s.get("sql", ""), "explanation": s.get("explanation", ""), "tables": s.get("tables", [])}
            for i, s in enumerate(result.get("steps", [])[:5])
        ]
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
            "tables_used": result.get("tables_used", tables or []),
            "confidence":  result.get("confidence", "none"),
            "warnings":    result.get("warnings", []),
        }
