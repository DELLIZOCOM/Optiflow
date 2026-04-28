"""
render_chart tool — the LLM calls this to surface a chart in the UI.

The orchestrator intercepts the call: it emits an SSE `chart` event with the
spec to the frontend (which renders it via Chart.js), then runs the tool's
own `execute()` to give the LLM a confirmation it can build its text answer
on top of.

Spec contract (matches frontend/js/chat.js renderChartCard):

    {
      "type":        "bar" | "line" | "area" | "pie" | "doughnut" | "table",
      "title":       "string (required, ≤ 120 chars)",
      "explanation": "string (1–2 sentences)",
      "x":           "column-name-from-rows",
      "y":           "column-name-from-rows" | ["col1", "col2", ...],
      "rows":        [ { col: value, ... }, ... ]   // ≤ 200 rows
    }

This tool is only injected into the LLM's tool list when the user picked
"Chart" mode (`visualise=True`). In text mode it's hidden.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

_VALID_TYPES = {"bar", "line", "area", "pie", "doughnut", "table"}
_MAX_ROWS    = 200
_MAX_TITLE   = 120
_MAX_EXPLAIN = 600


def _coerce_y(y: Any) -> list[str]:
    if isinstance(y, list):
        return [str(s).strip() for s in y if str(s).strip()]
    if isinstance(y, str) and y.strip():
        return [y.strip()]
    return []


def validate_chart_spec(spec: dict) -> tuple[bool, str]:
    """
    Sanity-check a chart spec before we forward it to the UI.

    Returns (ok, error_message_or_empty).
    """
    if not isinstance(spec, dict):
        return False, "spec must be an object"

    ctype = (spec.get("type") or "").strip().lower()
    if ctype not in _VALID_TYPES:
        return False, f"type must be one of {sorted(_VALID_TYPES)}, got {ctype!r}"

    title = (spec.get("title") or "").strip()
    if not title:
        return False, "title is required"
    if len(title) > _MAX_TITLE:
        return False, f"title is too long (>{_MAX_TITLE} chars)"

    explanation = (spec.get("explanation") or "").strip()
    if len(explanation) > _MAX_EXPLAIN:
        return False, f"explanation is too long (>{_MAX_EXPLAIN} chars)"

    x = (spec.get("x") or "").strip()
    if not x:
        return False, "x (the column name for the x-axis) is required"

    y_cols = _coerce_y(spec.get("y"))
    if not y_cols:
        return False, "y must be a column name or list of column names"

    rows = spec.get("rows")
    if not isinstance(rows, list) or not rows:
        return False, "rows must be a non-empty list of objects"
    if len(rows) > _MAX_ROWS:
        return False, f"rows has too many entries (max {_MAX_ROWS})"

    # Spot-check the first row's columns line up with x + y
    first = rows[0]
    if not isinstance(first, dict):
        return False, "each row must be an object"
    missing: list[str] = []
    if x not in first:
        missing.append(x)
    for k in y_cols:
        if k not in first:
            missing.append(k)
    if missing:
        return False, f"row missing column(s): {missing}"

    return True, ""


def normalize_chart_spec(spec: dict) -> dict:
    """Return a clean copy with normalized fields, ready to send over SSE."""
    return {
        "type":        (spec.get("type") or "bar").strip().lower(),
        "title":       (spec.get("title") or "").strip(),
        "explanation": (spec.get("explanation") or "").strip(),
        "x":           (spec.get("x") or "").strip(),
        "y":           _coerce_y(spec.get("y")),
        "rows":        spec.get("rows") or [],
    }


class RenderChartTool(BaseTool):
    """
    The agent calls this once it has the data and wants to draw a chart.

    Note: the orchestrator emits the SSE 'chart' event itself by inspecting
    `tool_name == 'render_chart'` *before* invoking this tool. This tool
    only validates the spec and returns a confirmation to the LLM so it
    can continue to its short text answer.
    """

    name = "render_chart"
    description = (
        "Render a chart in the user interface. Call this exactly once after "
        "you have queried the data with execute_sql / search_emails / etc. "
        "Provide the rows you already retrieved, the column names that map "
        "to x and y axes, a short title, and a 1-2 sentence explanation. "
        "After calling this, give the user a brief 1-3 sentence text answer "
        "describing what the chart shows. Only use this tool when the user "
        "explicitly asked to see a chart or visualization."
    )
    parameters = {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["bar", "line", "area", "pie", "doughnut", "table"],
                "description": (
                    "Chart shape. Use 'bar' for category comparisons, 'line' "
                    "or 'area' for time series, 'pie'/'doughnut' for parts of "
                    "a whole (single y-column, ≤8 slices), 'table' if no "
                    "chart shape fits the data well."
                ),
            },
            "title": {
                "type": "string",
                "description": "Short chart title (≤ 120 chars).",
            },
            "explanation": {
                "type": "string",
                "description": "1–2 sentence reading of what the chart shows.",
            },
            "x": {
                "type": "string",
                "description": "Name of the column in `rows` to use as the x-axis / categories.",
            },
            "y": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
                "description": (
                    "Column name (or list of names) to plot as series. "
                    "For pie/doughnut, pass a single column."
                ),
            },
            "rows": {
                "type": "array",
                "description": (
                    f"Data rows (max {_MAX_ROWS}). Each item is an object "
                    "with the columns named in x and y. Pass the rows you "
                    "already fetched — do NOT re-query."
                ),
                "items": {"type": "object"},
            },
        },
        "required": ["type", "title", "x", "y", "rows"],
    }

    async def execute(self, input: dict) -> ToolResult:
        ok, err = validate_chart_spec(input or {})
        if not ok:
            logger.warning("[render_chart] rejected: %s | spec=%s",
                           err, json.dumps(input)[:300])
            return ToolResult(
                tool_call_id="",
                content=(
                    f"Chart spec invalid: {err}. "
                    "Fix the spec and call render_chart again, or skip the chart "
                    "and answer in text only."
                ),
                is_error=True,
            )

        spec = normalize_chart_spec(input)
        logger.info(
            "[render_chart] OK type=%s title=%r rows=%d y=%s",
            spec["type"], spec["title"][:60], len(spec["rows"]), spec["y"],
        )
        return ToolResult(
            tool_call_id="",
            content=(
                f"Chart rendered to the user ({spec['type']}, "
                f"{len(spec['rows'])} rows, y={spec['y']}). "
                "Now write a short 1-3 sentence text answer describing the result."
            ),
            metadata={"chart_spec": spec},
        )
