"""
Database tools: list_tables, get_table_schema, execute_sql, get_business_context.

These four tools give the agent everything it needs to answer data questions:
  1. list_tables          — discover what exists
  2. get_table_schema     — understand column structure before writing SQL
  3. execute_sql          — run read-only SELECT queries
  4. get_business_context — look up domain terminology and business rules
"""

import logging
import re

from agent.models import ToolResult
from agent.tools.base import BaseTool

logger = logging.getLogger(__name__)

# Regex: first meaningful keyword must be SELECT or WITH (CTE)
_SELECT_RE = re.compile(
    r"^\s*(?:--[^\n]*\n\s*|/\*.*?\*/\s*)*(WITH|SELECT)\b",
    re.IGNORECASE | re.DOTALL,
)


def _format_table(rows: list[dict], max_rows: int = 100) -> tuple[str, int]:
    """Format a list-of-dicts result as a readable text table.

    Returns (formatted_string, row_count).
    """
    if not rows:
        return "Query returned 0 rows.", 0

    display = rows[:max_rows]
    cols = list(display[0].keys())

    # Column widths: max of header and any cell value
    widths: dict[str, int] = {}
    for c in cols:
        cell_max = max(
            (len(str(r.get(c) if r.get(c) is not None else "NULL")) for r in display),
            default=0,
        )
        widths[c] = max(len(str(c)), cell_max)

    def _cell(value) -> str:
        return "NULL" if value is None else str(value)

    header = " | ".join(str(c).ljust(widths[c]) for c in cols)
    sep    = "-+-".join("-" * widths[c] for c in cols)
    body   = "\n".join(
        " | ".join(_cell(r.get(c)).ljust(widths[c]) for c in cols)
        for r in display
    )

    count = len(rows)
    note = f"\n({count} row{'s' if count != 1 else ''} returned)"
    if count > max_rows:
        note += f" — showing first {max_rows}"

    return f"{header}\n{sep}\n{body}{note}", count


# ── Tool implementations ───────────────────────────────────────────────────────

class ListTablesTool(BaseTool):
    name = "list_tables"
    description = (
        "List all tables in the connected database with brief descriptions and row counts. "
        "Use this FIRST to understand what data is available before writing any queries. "
        "Pass an optional filter keyword to narrow down by table name or description."
    )
    parameters = {
        "type": "object",
        "properties": {
            "filter": {
                "type": "string",
                "description": (
                    "Optional keyword to filter table names or descriptions "
                    "(e.g. 'invoice', 'customer')"
                ),
            }
        },
        "required": [],
    }

    def __init__(self, schema_provider, connector):
        self._schema    = schema_provider
        self._connector = connector

    async def execute(self, input: dict) -> ToolResult:
        filter_kw = (input.get("filter") or "").lower().strip()
        index = self._schema.get_table_index()

        if not index:
            return ToolResult(
                tool_call_id="",
                content=(
                    "Schema index not found. "
                    "Run Setup → Schema Discovery to generate it."
                ),
                is_error=True,
            )

        lines = [l for l in index.splitlines() if l.strip()]
        if filter_kw:
            lines = [l for l in lines if filter_kw in l.lower()]

        db_name = self._connector.get_database_name()
        db_type = self._connector.get_db_type().upper()
        header  = f"Database: {db_name}  ({db_type})\n\n"
        body    = "\n".join(lines) if lines else "(no tables match filter)"
        return ToolResult(tool_call_id="", content=header + body)


class GetTableSchemaTool(BaseTool):
    name = "get_table_schema"
    description = (
        "Get full schema details — columns, data types, nullability, and sample/enum values — "
        "for one or more tables. "
        "Always call this after list_tables before writing SQL to understand column names and types. "
        "Request multiple tables at once when you need to understand join relationships."
    )
    parameters = {
        "type": "object",
        "properties": {
            "tables": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of table names to retrieve schemas for",
            }
        },
        "required": ["tables"],
    }

    def __init__(self, schema_provider):
        self._schema = schema_provider

    async def execute(self, input: dict) -> ToolResult:
        tables = input.get("tables") or []
        if not tables:
            return ToolResult(
                tool_call_id="",
                content="No table names provided.",
                is_error=True,
            )

        parts: list[str] = []
        missing: list[str] = []
        for name in tables:
            detail = self._schema.get_table_detail(name)
            if detail:
                parts.append(detail)
            else:
                missing.append(name)

        if not parts:
            return ToolResult(
                tool_call_id="",
                content=f"No schema found for: {', '.join(tables)}",
                is_error=True,
            )

        content = "\n\n".join(parts)
        if missing:
            content += f"\n\nNOTE: Schema not found for: {', '.join(missing)}"
        return ToolResult(tool_call_id="", content=content)


class ExecuteSQLTool(BaseTool):
    name = "execute_sql"
    description = (
        "Execute a read-only SQL SELECT query against the database and return results. "
        "Only SELECT queries (and CTEs starting with WITH) are allowed — "
        "any modification statement will be rejected immediately. "
        "Rules: use explicit column names (no SELECT *); "
        "use TOP/LIMIT to cap at 100 rows; "
        "always include ORDER BY for deterministic results. "
        "If the query returns an error, read the error message carefully, "
        "fix the SQL (check column names, table names, GROUP BY completeness), and retry."
    )
    parameters = {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "The SQL SELECT query to execute",
            },
            "explanation": {
                "type": "string",
                "description": "Brief description of what this query retrieves",
            },
            "tables_used": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Table names referenced in this query",
            },
        },
        "required": ["sql", "explanation"],
    }

    def __init__(self, connector):
        self._connector = connector

    async def execute(self, input: dict) -> ToolResult:
        sql         = (input.get("sql") or "").strip()
        explanation = input.get("explanation", "")

        if not sql:
            return ToolResult(
                tool_call_id="",
                content="No SQL provided.",
                is_error=True,
            )

        # Safety: only SELECT / WITH allowed
        if not _SELECT_RE.match(sql):
            first_word = sql.split()[0].upper() if sql.split() else "?"
            return ToolResult(
                tool_call_id="",
                content=(
                    f"REJECTED: Only SELECT (and WITH…SELECT) queries are allowed. "
                    f"Got '{first_word}'. OptiFlow is read-only."
                ),
                is_error=True,
            )

        logger.info(f"[execute_sql] {explanation!r}: {sql[:200]}")

        try:
            rows = await self._connector.execute_query(sql)
            text, row_count = _format_table(rows)
            return ToolResult(
                tool_call_id="",
                content=text,
                metadata={"row_count": row_count, "explanation": explanation},
            )
        except Exception as exc:
            err = str(exc)
            logger.warning(f"[execute_sql] error: {err}")
            return ToolResult(
                tool_call_id="",
                content=(
                    f"SQL Error: {err}\n\n"
                    "Review the error, check column/table names and GROUP BY completeness, "
                    "then fix and retry."
                ),
                is_error=True,
            )


class GetBusinessContextTool(BaseTool):
    name = "get_business_context"
    description = (
        "Retrieve business domain knowledge about this company — "
        "industry, workflows, table purposes, status code meanings, "
        "fiscal year boundaries, and domain-specific terminology. "
        "Use this when you encounter unfamiliar terms, status values, or business logic "
        "that isn't obvious from column names alone."
    )
    parameters = {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": (
                    "Optional topic or keyword to focus the context "
                    "(e.g. 'invoice status', 'fiscal year', 'project pipeline')"
                ),
            }
        },
        "required": [],
    }

    def __init__(self, knowledge_provider):
        self._knowledge = knowledge_provider

    async def execute(self, input: dict) -> ToolResult:
        topic = (input.get("topic") or "").strip()
        ctx   = (
            self._knowledge.get_context_for_topic(topic)
            if topic
            else self._knowledge.get_company_context()
        )
        if not ctx:
            return ToolResult(
                tool_call_id="",
                content=(
                    "No business context available. "
                    "Complete Setup → Business Knowledge to add it."
                ),
            )
        return ToolResult(tool_call_id="", content=ctx)


# ── Factory ────────────────────────────────────────────────────────────────────

def create_database_tools(
    connector, schema_provider, knowledge_provider
) -> list[BaseTool]:
    """Instantiate and return all four database tools."""
    return [
        ListTablesTool(schema_provider, connector),
        GetTableSchemaTool(schema_provider),
        ExecuteSQLTool(connector),
        GetBusinessContextTool(knowledge_provider),
    ]
