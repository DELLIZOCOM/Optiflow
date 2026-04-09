"""
Source-scoped database tools.

All tools accept a required `source` parameter so the agent can work with
multiple databases simultaneously. The tool looks up the source by name in
the SourceRegistry and routes the operation to the right connector.

Tools:
  list_tables(source, filter?)           — discover what tables exist
  get_table_schema(source, tables)       — understand column structure
  execute_sql(source, sql, explanation)  — run read-only SELECT queries
  get_business_context(topic?)           — look up domain knowledge (global)
"""

import logging
import re

from app.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

# Safety regex: first meaningful keyword must be SELECT or WITH (CTE)
_SELECT_RE = re.compile(
    r"^\s*(?:--[^\n]*\n\s*|/\*.*?\*/\s*)*(WITH|SELECT)\b",
    re.IGNORECASE | re.DOTALL,
)


def _format_table(rows: list[dict], max_rows: int = 100) -> tuple[str, int]:
    """Format list-of-dicts as a readable text table. Returns (text, row_count)."""
    if not rows:
        return "Query returned 0 rows.", 0

    display = rows[:max_rows]
    cols    = list(display[0].keys())
    widths  = {
        c: max(len(str(c)), max(
            len(str(r.get(c) if r.get(c) is not None else "NULL")) for r in display
        ))
        for c in cols
    }

    def _cell(v) -> str:
        return "NULL" if v is None else str(v)

    header = " | ".join(str(c).ljust(widths[c]) for c in cols)
    sep    = "-+-".join("-" * widths[c] for c in cols)
    body   = "\n".join(
        " | ".join(_cell(r.get(c)).ljust(widths[c]) for c in cols)
        for r in display
    )
    count  = len(rows)
    note   = f"\n({count} row{'s' if count != 1 else ''} returned)"
    if count > max_rows:
        note += f" — showing first {max_rows}"

    return f"{header}\n{sep}\n{body}{note}", count


# ── Tool implementations ───────────────────────────────────────────────────────

class ListTablesTool(BaseTool):
    name        = "list_tables"
    description = (
        "List all tables in a connected data source with descriptions and row counts. "
        "Use this FIRST to understand what data is available before writing any SQL. "
        "Pass an optional filter keyword to narrow down by table name or description."
    )
    parameters  = {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Name of the data source to list tables from (e.g. 'sales_db')",
            },
            "filter": {
                "type": "string",
                "description": "Optional keyword to filter by table name or description",
            },
        },
        "required": ["source"],
    }

    def __init__(self, source_registry):
        self._registry = source_registry

    async def execute(self, input: dict) -> ToolResult:
        source_name = (input.get("source") or "").strip()
        filter_kw   = (input.get("filter") or "").lower().strip()

        source = self._registry.get(source_name)
        if not source:
            available = self._registry.names()
            return ToolResult(
                tool_call_id="",
                content=(
                    f"Source '{source_name}' not found. "
                    f"Available sources: {available}"
                ),
                is_error=True,
            )

        index = source.get_table_index()
        if not index:
            return ToolResult(
                tool_call_id="",
                content=(
                    f"Schema index not found for source '{source_name}'. "
                    "Run Setup → Add Source → Discover Schema to generate it."
                ),
                is_error=True,
            )

        lines = [l for l in index.splitlines() if l.strip()]
        if filter_kw:
            lines = [l for l in lines if filter_kw in l.lower()]

        header = (
            f"Source: {source_name}  "
            f"Database: {source.get_database_name()}  "
            f"({source.get_db_type().upper()})\n\n"
        )
        body = "\n".join(lines) if lines else "(no tables match filter)"
        return ToolResult(tool_call_id="", content=header + body)


class GetTableSchemaTool(BaseTool):
    name        = "get_table_schema"
    description = (
        "Get full schema details — columns, data types, nullability, and sample/enum values — "
        "for one or more tables in a specific data source. "
        "Always call this after list_tables before writing SQL to understand column names and types. "
        "Request multiple tables at once when you need to understand join relationships."
    )
    parameters  = {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Name of the data source (e.g. 'sales_db')",
            },
            "tables": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of table names to retrieve schemas for",
            },
        },
        "required": ["source", "tables"],
    }

    def __init__(self, source_registry):
        self._registry = source_registry

    async def execute(self, input: dict) -> ToolResult:
        source_name = (input.get("source") or "").strip()
        tables      = input.get("tables") or []

        source = self._registry.get(source_name)
        if not source:
            available = self._registry.names()
            return ToolResult(
                tool_call_id="",
                content=f"Source '{source_name}' not found. Available: {available}",
                is_error=True,
            )

        if not tables:
            return ToolResult(tool_call_id="", content="No table names provided.", is_error=True)

        parts: list[str] = []
        missing: list[str] = []
        for name in tables:
            detail = source.get_table_detail(name)
            if detail:
                parts.append(detail)
            else:
                missing.append(name)

        if not parts:
            return ToolResult(
                tool_call_id="",
                content=f"No schema found for: {', '.join(tables)} in source '{source_name}'",
                is_error=True,
            )

        content = "\n\n".join(parts)
        if missing:
            content += f"\n\nNOTE: Schema not found for: {', '.join(missing)}"
        return ToolResult(tool_call_id="", content=content)


class ExecuteSQLTool(BaseTool):
    name        = "execute_sql"
    description = (
        "Execute a read-only SQL SELECT query against a specific data source and return results. "
        "Only SELECT queries (and CTEs starting with WITH) are allowed — "
        "any modification statement will be rejected immediately. "
        "Rules: use explicit column names (no SELECT *); "
        "use TOP/LIMIT to cap at 100 rows; always include ORDER BY for deterministic results. "
        "If the query returns an error, read the error message carefully, "
        "fix the SQL (check column names, table names, GROUP BY completeness), and retry."
    )
    parameters  = {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Name of the data source to query (e.g. 'sales_db')",
            },
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
        "required": ["source", "sql", "explanation"],
    }

    def __init__(self, source_registry):
        self._registry = source_registry

    async def execute(self, input: dict) -> ToolResult:
        source_name = (input.get("source") or "").strip()
        sql         = (input.get("sql") or "").strip()
        explanation = input.get("explanation", "")

        source = self._registry.get(source_name)
        if not source:
            available = self._registry.names()
            return ToolResult(
                tool_call_id="",
                content=f"Source '{source_name}' not found. Available: {available}",
                is_error=True,
            )

        if not sql:
            return ToolResult(tool_call_id="", content="No SQL provided.", is_error=True)

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

        logger.info(f"[execute_sql:{source_name}] {explanation!r}: {sql[:200]}")

        try:
            rows = await source.execute_query(sql)
            text, row_count = _format_table(rows)
            return ToolResult(
                tool_call_id="",
                content=text,
                metadata={"row_count": row_count, "explanation": explanation},
            )
        except Exception as exc:
            err = str(exc)
            logger.warning(f"[execute_sql:{source_name}] error: {err}")
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
    name        = "get_business_context"
    description = (
        "Retrieve business domain knowledge — industry, workflows, table purposes, "
        "status code meanings, fiscal year boundaries, and domain-specific terminology. "
        "Use this when you encounter unfamiliar terms, status values, or business logic "
        "that isn't obvious from column names alone."
    )
    parameters  = {
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

    async def execute(self, input: dict) -> ToolResult:
        from app.config import COMPANY_MD_PATH

        try:
            ctx = COMPANY_MD_PATH.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            ctx = ""
        except Exception as exc:
            logger.warning(f"get_business_context: could not read company.md: {exc}")
            ctx = ""

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

def create_database_tools(source_registry) -> list[BaseTool]:
    """Return the four database tools wired to the given SourceRegistry."""
    return [
        ListTablesTool(source_registry),
        GetTableSchemaTool(source_registry),
        ExecuteSQLTool(source_registry),
        GetBusinessContextTool(),
    ]
