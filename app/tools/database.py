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
        "List all tables with descriptions and row counts. "
        "You usually don't need this — the Business Context in your prompt already describes all tables. "
        "Only call this if the Business Context doesn't cover the tables you're looking for, "
        "or if the user explicitly asks what tables exist."
    )
    parameters  = {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Data source name. Omit if only one source is connected.",
            },
            "filter": {
                "type": "string",
                "description": "Optional keyword to filter results by table name or description",
            },
        },
        "required": [],
    }

    def __init__(self, source_registry):
        self._registry = source_registry

    async def execute(self, input: dict) -> ToolResult:
        source_name = (input.get("source") or "").strip()
        filter_kw   = (input.get("filter") or "").lower().strip()

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

        lines = [l for l in index.splitlines() if l.strip()]
        if filter_kw:
            lines = [l for l in lines if filter_kw in l.lower()]

        header = (
            f"Source: **{source.name}** — use this name in execute_sql and get_table_schema calls\n"
            f"Database: {source.get_database_name()}  |  Type: {source.get_db_type().upper()}\n\n"
        )
        body = "\n".join(lines) if lines else "(no tables match filter)"
        return ToolResult(tool_call_id="", content=header + body)


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
        "On error: read the message, fix the SQL, and retry."
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
