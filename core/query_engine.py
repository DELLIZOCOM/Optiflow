"""
Central query router. Connects parsed intents to SQL execution.

Takes a parsed intent dict, looks it up in the registry, binds parameters,
injects mandatory filters, executes via db, and returns raw results.
Does NOT format responses — that is the formatter's job.
"""

import re
import logging

from intents import INTENT_REGISTRY
from core.filter_injector import inject_filters
from core.db import execute_query

logger = logging.getLogger(__name__)

# Regex to find [PLACEHOLDER] tokens in SQL templates.
_PLACEHOLDER_RE = re.compile(r"\[([A-Z_]+)\]")

SUGGESTED_QUESTIONS = [
    "Which projects have been active the longest?",
    "What is our total pending invoice amount?",
    "Which AMC contracts are expiring soon?",
    "Show me active operations projects",
    "How are we doing against this month's target?",
]

QUERY_TIMEOUT = 10  # seconds


def _build_fallback():
    """Return a fallback result when no intent matches."""
    return {
        "rows": [],
        "intent_name": None,
        "params_used": {},
        "caveats": [],
        "redirected_from": None,
        "error": None,
        "fallback": True,
        "message": "I don't understand that question",
        "suggestions": SUGGESTED_QUESTIONS,
    }


def _bind_params(sql, intent_dict, defaults):
    """Replace [PLACEHOLDER] tokens with pyodbc ? markers.

    For LIKE patterns (e.g. '%[NAME]%'), the wildcard wrapping is baked
    into the parameter value so the ? placeholder works with LIKE.

    Args:
        sql: SQL template string with [PLACEHOLDER] tokens.
        intent_dict: The parsed intent dict (e.g. {'intent': 'amc_by_customer', 'CUSTOMER_NAME': 'Hanil'}).
        defaults: The params dict from the intent definition with default values.

    Returns:
        (bound_sql, param_values, params_used) tuple.
        bound_sql has ? in place of each [PLACEHOLDER].
        param_values is a tuple of values in order of appearance.
        params_used is a dict of placeholder_name -> value used.
    """
    param_values = []
    params_used = {}

    def _replacer(match):
        full_match = match.group(0)          # e.g. [CUSTOMER_NAME]
        param_name = match.group(1)          # e.g. CUSTOMER_NAME

        # Resolve value: intent dict first, then defaults
        value = intent_dict.get(param_name)
        if value is None:
            # Try lowercase key from intent dict
            value = intent_dict.get(param_name.lower())
        if value is None:
            value = defaults.get(param_name, "")

        # Check if this placeholder is inside a LIKE '%...%' pattern.
        # Look at the characters immediately before and after in the sql.
        start = match.start()
        end = match.end()
        prefix_char = sql[start - 1] if start > 0 else ""
        suffix_char = sql[end] if end < len(sql) else ""

        if prefix_char == "%" or suffix_char == "%":
            # LIKE pattern: bake wildcards into the value, replace the
            # entire '%[PLACEHOLDER]%' with just '?'
            like_value = ""
            if prefix_char == "%":
                like_value += "%"
            like_value += str(value)
            if suffix_char == "%":
                like_value += "%"
            param_values.append(like_value)
            params_used[param_name] = value
            # We need to remove the surrounding % signs since they're now
            # in the value. We'll handle this with a second pass.
            return full_match  # Placeholder for now, handled below
        else:
            param_values.append(value)
            params_used[param_name] = value
            return "?"

    # Two-pass approach: first collect values and handle LIKE patterns.
    # Reset for clean single-pass.
    param_values.clear()
    params_used.clear()

    # Find all placeholders and their LIKE context in one pass.
    result_parts = []
    last_end = 0

    for match in _PLACEHOLDER_RE.finditer(sql):
        param_name = match.group(1)
        start = match.start()
        end = match.end()

        value = intent_dict.get(param_name)
        if value is None:
            value = intent_dict.get(param_name.lower())
        if value is None:
            value = defaults.get(param_name, "")

        params_used[param_name] = value

        # Check for LIKE '%[PLACEHOLDER]%' pattern
        like_prefix = start >= 2 and sql[start - 2 : start] == "'%"
        like_suffix = end + 2 <= len(sql) and sql[end : end + 2] == "%'"

        if like_prefix and like_suffix:
            # Replace '%[PLACEHOLDER]%' with ?
            # Cut before the '%
            result_parts.append(sql[last_end : start - 2])
            result_parts.append("?")
            param_values.append(f"%{value}%")
            last_end = end + 2
        elif like_prefix:
            result_parts.append(sql[last_end : start - 2])
            result_parts.append("?")
            param_values.append(f"%{value}")
            last_end = end + 1 if (end < len(sql) and sql[end] == "'") else end
        elif like_suffix:
            result_parts.append(sql[last_end : start - 1] if start > 0 and sql[start - 1] == "'" else sql[last_end:start])
            result_parts.append("?")
            param_values.append(f"{value}%")
            last_end = end + 2
        else:
            # Simple replacement: '[PLACEHOLDER]' -> ?
            # Check if wrapped in single quotes: '[PLACEHOLDER]'
            if start > 0 and sql[start - 1] == "'" and end < len(sql) and sql[end] == "'":
                result_parts.append(sql[last_end : start - 1])
                result_parts.append("?")
                param_values.append(str(value))
                last_end = end + 1
            else:
                result_parts.append(sql[last_end:start])
                result_parts.append("?")
                param_values.append(str(value))
                last_end = end

    result_parts.append(sql[last_end:])
    bound_sql = "".join(result_parts)

    return bound_sql, tuple(param_values), params_used


def run(intent_dict):
    """Execute a parsed intent and return raw results.

    Args:
        intent_dict: Parsed intent, e.g. {'intent': 'amc_expiry', 'days': 60}.
                     Must contain an 'intent' key.

    Returns:
        dict with keys: rows, intent_name, params_used, caveats,
                        redirected_from, error, and optionally fallback/message/suggestions.
    """
    intent_name = intent_dict.get("intent")
    if not intent_name:
        return _build_fallback()

    definition = INTENT_REGISTRY.get(intent_name)
    if not definition:
        return _build_fallback()

    # Handle retired intents
    redirected_from = None
    if definition.get("retired"):
        redirect_target = definition.get("redirect_to")
        if redirect_target and redirect_target in INTENT_REGISTRY:
            redirected_from = intent_name
            intent_name = redirect_target
            definition = INTENT_REGISTRY[redirect_target]
        else:
            return {
                "rows": [],
                "intent_name": intent_name,
                "params_used": {},
                "caveats": definition.get("caveats", []),
                "redirected_from": None,
                "error": "This intent is retired and has no valid redirect.",
            }

    sql_template = definition.get("sql", "")
    if not sql_template:
        return {
            "rows": [],
            "intent_name": intent_name,
            "params_used": {},
            "caveats": definition.get("caveats", []),
            "redirected_from": redirected_from,
            "error": "No SQL template defined for this intent.",
        }

    table_name = definition.get("table", "")
    defaults = definition.get("params", {})
    caveats = definition.get("caveats", [])

    # Handle multi-statement SQL (e.g. tickets_by_person has summary + detail).
    # If a person-specific param is provided, use the detail query; otherwise summary.
    if "\n\n" in sql_template and sql_template.count("SELECT") > 1:
        statements = sql_template.split("\n\n")
        has_specific_param = any(
            intent_dict.get(k) or intent_dict.get(k.lower())
            for k in defaults
            if defaults[k] == ""
        )
        if has_specific_param:
            # Use the detail query (second statement)
            sql_template = statements[-1].strip()
        else:
            # Use the summary query (first statement)
            sql_template = statements[0].strip()

    # Bind parameters
    bound_sql, param_values, params_used = _bind_params(
        sql_template, intent_dict, defaults
    )

    # Inject mandatory filters (safety net)
    filtered_sql = inject_filters(bound_sql, table_name)

    # Execute
    try:
        rows = execute_query(filtered_sql, param_values if param_values else None)
    except Exception as e:
        logger.error(f"Query failed for intent '{intent_name}': {e}")
        return {
            "rows": [],
            "intent_name": intent_name,
            "params_used": params_used,
            "caveats": caveats,
            "redirected_from": redirected_from,
            "error": f"Database error: {e}",
        }

    return {
        "rows": rows if rows else [],
        "intent_name": intent_name,
        "params_used": params_used,
        "caveats": caveats,
        "redirected_from": redirected_from,
        "error": None,
    }
