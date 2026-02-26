"""
Mandatory data quality filter injector.

Injects required WHERE-clause filters into SQL queries as a safety net.
The intent templates already include these filters — this catches any query
that might slip through without them.

No flags to disable. No optional mode. No bypass.
"""

import re

# Filters keyed by table name (case-insensitive match).
# Each entry is a list of (check_pattern, inject_clause) tuples.
# check_pattern: regex to detect if the filter is already present.
# inject_clause: the raw SQL AND clause to append if missing.
_MANDATORY_FILTERS = {
    "prost": [
        (
            re.compile(r"Created_Date\s*!=\s*'2025-04-21'", re.IGNORECASE),
            "AND Created_Date != '2025-04-21'",
        ),
        (
            re.compile(
                r"PIC\s+NOT\s+IN\s*\(\s*'XXX'\s*,\s*'NONE'\s*,\s*'66'\s*,\s*'25'\s*,\s*'64'\s*\)",
                re.IGNORECASE,
            ),
            "AND PIC NOT IN ('XXX','NONE','66','25','64')",
        ),
        (
            re.compile(r"PIC\s+IS\s+NOT\s+NULL", re.IGNORECASE),
            "AND PIC IS NOT NULL",
        ),
    ],
    "amc_master": [
        (
            re.compile(r"Status\s+IS\s+NOT\s+NULL", re.IGNORECASE),
            "AND Status IS NOT NULL",
        ),
        (
            re.compile(r"Status\s*!=\s*''", re.IGNORECASE),
            "AND Status != ''",
        ),
    ],
}


def _find_injection_point(sql):
    """Find where to inject AND clauses in a SQL statement.

    Looks for the end of the WHERE block — just before GROUP BY, ORDER BY,
    HAVING, a semicolon, or end-of-string, whichever comes first.
    If there is no WHERE clause, injects one before those same keywords.

    Returns (prefix, suffix, needs_where) tuple.
    """
    # Try to find GROUP BY / ORDER BY / HAVING / trailing semicolon
    boundary = re.search(
        r"\b(GROUP\s+BY|ORDER\s+BY|HAVING)\b",
        sql,
        re.IGNORECASE,
    )

    if boundary:
        split_pos = boundary.start()
    else:
        # Strip trailing whitespace/semicolons to append before them
        stripped = sql.rstrip()
        if stripped.endswith(";"):
            split_pos = len(sql) - len(sql) - len(stripped) + len(stripped) - 1
            # Simpler: find last semicolon
            split_pos = stripped.rfind(";")
        else:
            split_pos = len(sql)

    prefix = sql[:split_pos].rstrip()
    suffix = sql[split_pos:]

    has_where = bool(re.search(r"\bWHERE\b", prefix, re.IGNORECASE))
    return prefix, suffix, not has_where


def inject_filters(sql, table_name):
    """Inject mandatory data quality filters into a SQL query.

    Args:
        sql: The SQL query string.
        table_name: The primary table being queried (e.g. 'ProSt', 'AMC_MASTER').

    Returns:
        The SQL string with mandatory filters present.
        If all filters already exist, returns the SQL unchanged.
    """
    if not sql or not table_name:
        return sql

    key = table_name.strip().lower()
    filters = _MANDATORY_FILTERS.get(key)
    if not filters:
        return sql

    missing = []
    for check_pattern, inject_clause in filters:
        if not check_pattern.search(sql):
            missing.append(inject_clause)

    if not missing:
        return sql

    prefix, suffix, needs_where = _find_injection_point(sql)

    injected = "\n  ".join(missing)
    if needs_where:
        # Turn the first AND into WHERE
        injected = "WHERE " + injected[4:]  # strip leading "AND "
        return f"{prefix}\n{injected}\n{suffix.lstrip()}"
    else:
        return f"{prefix}\n  {injected}\n{suffix.lstrip()}"
