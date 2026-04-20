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
import json
import re

from app.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

# Safety regex: first meaningful keyword must be SELECT or WITH (CTE)
_SELECT_RE = re.compile(
    r"^\s*(?:--[^\n]*\n\s*|/\*.*?\*/\s*)*(WITH|SELECT)\b",
    re.IGNORECASE | re.DOTALL,
)


_DIALECT_HINTS = {
    "MSSQL": (
        "**SQL dialect: SQL Server** — "
        "row limit: `SELECT TOP N` | dates: `GETDATE()`, `CONVERT(date, GETDATE())` | "
        "nulls: `ISNULL(col, 0)` | identifiers: `[col name]` | "
        "date parts: `YEAR(col)`, `MONTH(col)`, `DATEPART(quarter, col)` | "
        "arithmetic: `DATEDIFF(day, start, end)`, `DATEADD(day, -7, GETDATE())`"
    ),
    "POSTGRESQL": (
        "**SQL dialect: PostgreSQL** — "
        "row limit: `LIMIT N` | dates: `NOW()`, `CURRENT_DATE` | "
        "nulls: `COALESCE(col, 0)` | identifiers: `\"col name\"` | "
        "date parts: `EXTRACT(year FROM col)`, `DATE_TRUNC('month', col)`"
    ),
    "MYSQL": (
        "**SQL dialect: MySQL** — "
        "row limit: `LIMIT N` | dates: `NOW()`, `CURDATE()` | "
        "nulls: `IFNULL(col, 0)` | identifiers: `` `col name` `` | "
        "date parts: `YEAR(col)`, `MONTH(col)`"
    ),
}


def _dialect_hint(db_type: str) -> str:
    return _DIALECT_HINTS.get(db_type.upper(), f"**SQL dialect: {db_type}**")


def _format_table(rows: list[dict], max_rows: int = 100) -> tuple[str, int, list[str]]:
    """Format list-of-dicts as a readable text table. Returns (text, row_count, columns)."""
    if not rows:
        return "Query returned 0 rows.", 0, []

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

    return f"{header}\n{sep}\n{body}{note}", count, cols


def _json_safe(v):
    """Coerce a DB value into something JSON.dumps handles without `default=str`.

    Keeps numbers and booleans as-is so the frontend can plot them; turns
    dates / decimals / bytes into strings.
    """
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    try:
        import decimal
        if isinstance(v, decimal.Decimal):
            # Decimals plot fine as floats; keep precision reasonable.
            return float(v)
    except Exception:
        pass
    return str(v)


def _build_structured_result(rows: list[dict], max_preview: int = 20) -> dict:
    if not rows:
        return {
            "row_count": 0,
            "columns": [],
            "preview_rows": [],
            "sample_row": None,
        }

    preview = rows[:max_preview]
    return {
        "row_count": len(rows),
        "columns": list(preview[0].keys()),
        "preview_rows": preview,
        "sample_row": preview[0],
    }


# ── Source resolver ───────────────────────────────────────────────────────────

def _resolve_source(registry, source_name: str):
    """
    Return (source, error_string).

    Resolution rules:
      1. Exact match → use it.
      2. No match + exactly one source registered → auto-route to it.
      3. No match + multiple sources → return error listing available names.
    """
    source = registry.get(source_name)
    if source:
        return source, None

    all_sources = registry.get_all()
    if len(all_sources) == 1:
        return all_sources[0], None

    available = registry.names()
    if not available:
        return None, "No data sources are connected yet. Complete Setup → Add Data Source."
    return None, (
        f"Source '{source_name}' not found. "
        f"Available sources: {available}. "
        "Use one of these names in your tool calls."
    )


# ── Tool implementations ───────────────────────────────────────────────────────

class ListTablesTool(BaseTool):
    name        = "list_tables"
    description = (
        "Orient yourself to the database. Returns in one response: "
        "(1) the SQL dialect to use, "
        "(2) every table with its type (transaction/reference/etc.), description, and row count, "
        "(3) the complete relationship map — which tables join to which and the exact column names. "
        "Call this FIRST at the start of every question to plan your approach. "
        "After this call you know what tables exist, how they relate, and what SQL syntax to use."
    )
    parameters  = {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Data source name. Omit if only one source is connected.",
            },
        },
        "required": [],
    }

    def __init__(self, source_registry):
        self._registry = source_registry

    async def execute(self, input: dict) -> ToolResult:
        source_name = (input.get("source") or "").strip()

        source, err = _resolve_source(self._registry, source_name)
        if err:
            return ToolResult(tool_call_id="", content=err, is_error=True)

        index = source.get_table_index()
        if not index:
            return ToolResult(
                tool_call_id="",
                content=(
                    f"Schema index not found for source '{source.name}'. "
                    "Run Setup → Add Source → Discover Schema to generate it."
                ),
                is_error=True,
            )

        db_type = source.get_db_type().upper()
        dialect_hint = _dialect_hint(db_type)

        sections = [
            f"## Database: {source.get_database_name()}  |  Source: `{source.name}`  |  Type: {db_type}",
            "",
            dialect_hint,
            "",
            "## Tables",
            "",
            index.strip(),
        ]

        # Append relationship map if available
        rels = source.get_relationships()
        if rels:
            sections += ["", "## Relationships", "", rels.strip()]

        return ToolResult(tool_call_id="", content="\n".join(sections))


class GetTableSchemaTool(BaseTool):
    name        = "get_table_schema"
    description = (
        "Get full column details — names, types, nullability, and sample values — "
        "for one or more tables. Always call this before writing SQL. "
        "Request ALL tables you need in a single call — never one table at a time."
    )
    parameters  = {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Data source name. Omit if only one source is connected.",
            },
            "tables": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Table names to retrieve schemas for. Pass all needed tables at once.",
            },
        },
        "required": ["tables"],
    }

    def __init__(self, source_registry):
        self._registry = source_registry

    async def execute(self, input: dict) -> ToolResult:
        source_name = (input.get("source") or "").strip()
        tables      = input.get("tables") or []

        source, err = _resolve_source(self._registry, source_name)
        if err:
            return ToolResult(tool_call_id="", content=err, is_error=True)

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
        "Execute a read-only SQL SELECT query and return results. "
        "Only SELECT (and WITH…SELECT CTEs) are allowed — modifications are rejected. "
        "Use explicit column names, TOP/LIMIT for row cap, and ORDER BY. "
        "On error: read the message, fix the SQL, and retry. "
        "The result includes a readable table plus structured JSON metadata for reliable reasoning."
    )
    parameters  = {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Data source name. Omit if only one source is connected.",
            },
            "sql": {
                "type": "string",
                "description": "The SQL SELECT query to execute",
            },
            "explanation": {
                "type": "string",
                "description": "One-line description of what this query retrieves",
            },
        },
        "required": ["sql", "explanation"],
    }

    def __init__(self, source_registry):
        self._registry = source_registry

    async def execute(self, input: dict) -> ToolResult:
        source_name = (input.get("source") or "").strip()
        sql         = (input.get("sql") or "").strip()
        explanation = input.get("explanation", "")

        source, err = _resolve_source(self._registry, source_name)
        if err:
            return ToolResult(tool_call_id="", content=err, is_error=True)

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
            text, row_count, columns = _format_table(rows)
            structured = _build_structured_result(rows)
            content = (
                "RESULT PREVIEW\n"
                f"{text}\n\n"
                "RESULT JSON\n"
                f"{json.dumps(structured, default=str, ensure_ascii=False)}"
            )
            # Keep a chart-ready slice of the rows in metadata so the
            # orchestrator can attach them verbatim to a chart event
            # without the LLM re-serializing (and thus potentially
            # hallucinating) any numbers. Cap at 200 rows — charts with
            # more than that are unreadable anyway.
            _CHART_ROW_CAP = 200
            rows_for_chart = [
                {k: _json_safe(v) for k, v in r.items()}
                for r in rows[:_CHART_ROW_CAP]
            ]
            return ToolResult(
                tool_call_id="",
                content=content,
                metadata={
                    "row_count": row_count,
                    "columns": columns,
                    "explanation": explanation,
                    "structured": structured,
                    "rows_for_chart": rows_for_chart,
                    "rows_truncated": row_count > _CHART_ROW_CAP,
                },
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


class GetRelationshipsTool(BaseTool):
    name        = "get_relationships"
    description = (
        "Get the complete relationship map for a database: which tables join to which, "
        "the exact column names to use in JOIN conditions, and common multi-table join paths. "
        "Call this BEFORE writing any SQL that joins two or more tables. "
        "Do not guess join columns from column names — use this tool to get confirmed or "
        "inferred join conditions."
    )
    parameters  = {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Data source name. Omit if only one source is connected.",
            },
        },
        "required": [],
    }

    def __init__(self, source_registry):
        self._registry = source_registry

    async def execute(self, input: dict) -> ToolResult:
        source_name = (input.get("source") or "").strip()
        source, err = _resolve_source(self._registry, source_name)
        if err:
            return ToolResult(tool_call_id="", content=err, is_error=True)

        rels = source.get_relationships()
        if not rels:
            return ToolResult(
                tool_call_id="",
                content=(
                    "No relationships file found for this source. "
                    "Run Setup → Discover Schema to generate it."
                ),
            )
        return ToolResult(tool_call_id="", content=rels)


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
    """Return all database tools wired to the given SourceRegistry."""
    return [
        ListTablesTool(source_registry),
        GetTableSchemaTool(source_registry),
        ExecuteSQLTool(source_registry),
        GetBusinessContextTool(),
    ]
