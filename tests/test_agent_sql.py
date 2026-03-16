"""
Agent Mode SQL Generator — integration tests.

Generates SQL for 10 novel questions that fall outside the template-intent
system, runs safety and structure checks, then executes each query against
the live database to catch bad column names and wrong syntax.

Usage:  python3 tests/test_agent_sql.py
"""

import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.agent_sql_generator import generate_sql
from core.db import execute_query

# ---------------------------------------------------------------------------
# Test questions — all novel, none handled by template intents
# ---------------------------------------------------------------------------

QUESTIONS = [
    "Compare Hyundai's project count vs Inalfa's",
    "Which customers have both active projects AND expiring AMC contracts?",
    "Show me everything about project 692",
    "What's the total value of projects in Seed stage?",
    "Which PICs have projects stuck in Seed for over 6 months?",
    "List all invoices for customer HNTI with payment status",
    "How many projects were created each month this year?",
    "Which customers have never had a project reach Plant?",
    "Show AMC contracts where coverage ends before the AMC starts",
    "What's the average sales amount by project status?",
]

BANNED_KEYWORDS = {"INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "EXEC"}
VALID_CONFIDENCE = {"high", "medium", "low"}

_SEP = "─" * 78


# ---------------------------------------------------------------------------
# Safety checker
# ---------------------------------------------------------------------------

def check_safety(result: dict) -> list[tuple[str, bool, str]]:
    """Run safety and structure checks.

    Returns a list of (label, passed, detail) tuples.
    """
    checks = []
    sql = result.get("sql")
    tables = result.get("tables_used", [])
    explanation = result.get("explanation", "")
    confidence = result.get("confidence")

    # 1. sql must not be None
    checks.append(("sql is not None", sql is not None, ""))
    if sql is None:
        # Remaining checks are meaningless without SQL
        return checks

    sql_upper = sql.strip().upper()

    # 2. starts with SELECT
    starts_select = sql_upper.startswith("SELECT")
    checks.append((
        "starts with SELECT",
        starts_select,
        f"starts with: {sql.strip()[:40]!r}" if not starts_select else "",
    ))

    # 3. no banned write/DDL keywords
    found_banned = [kw for kw in BANNED_KEYWORDS if kw in sql_upper]
    checks.append((
        "no banned keywords (INSERT/UPDATE/DELETE/DROP/ALTER/EXEC)",
        len(found_banned) == 0,
        f"found: {found_banned}" if found_banned else "",
    ))

    # 4. tables_used is not empty
    checks.append(("tables_used is not empty", bool(tables), ""))

    # 5. ProSt migration filter
    if "ProSt" in tables:
        has_filter = "2025-04-21" in sql
        checks.append((
            "ProSt migration filter present ('2025-04-21')",
            has_filter,
            "WHERE Created_Date != '2025-04-21' is required" if not has_filter else "",
        ))

    # 6. INVOICE_DETAILS DISTINCT rule
    if "INVOICE_DETAILS" in tables:
        uses_count = "COUNT" in sql_upper
        has_distinct_inv = "DISTINCT" in sql_upper and "INVOICE_NO" in sql_upper
        if uses_count:
            checks.append((
                "INVOICE_DETAILS uses COUNT(DISTINCT Invoice_No)",
                has_distinct_inv,
                "COUNT(*) found without DISTINCT Invoice_No" if not has_distinct_inv else "",
            ))

    # 7. explanation is not empty
    checks.append(("explanation is not empty", bool(explanation and explanation.strip()), ""))

    # 8. confidence is valid
    checks.append((
        f"confidence is high/medium/low (got: '{confidence}')",
        confidence in VALID_CONFIDENCE,
        f"got '{confidence}'" if confidence not in VALID_CONFIDENCE else "",
    ))

    return checks


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_one(index: int, question: str) -> bool:
    """Run generation + safety checks + live execution for one question.

    Prints formatted output. Returns True if all checks pass and execution
    succeeds without error.
    """
    print(f"\n{_SEP}")
    print(f"[{index}/10] {question}")
    print(_SEP)

    # --- Generate SQL ---
    try:
        result = generate_sql(question)
    except Exception as e:
        print(f"  GENERATION ERROR: {e}")
        return False

    sql = result.get("sql")
    tables = result.get("tables_used", [])
    confidence = result.get("confidence", "none")
    explanation = result.get("explanation", "")
    warnings = result.get("warnings", [])

    # Print metadata
    print(f"  Tables     : {', '.join(tables) if tables else '(none)'}")
    print(f"  Confidence : {confidence}")
    if warnings:
        for w in warnings:
            print(f"  Warning    : {w}")

    # Print explanation
    print(f"\n  Explanation:")
    print(f"  {explanation}")

    # Print generated SQL
    if sql:
        print(f"\n  Generated SQL:")
        for line in sql.strip().splitlines():
            print(f"    {line}")
    else:
        print(f"\n  Generated SQL: (none)")

    # --- Safety checks ---
    print(f"\n  Safety checks:")
    checks = check_safety(result)
    all_passed = True
    for label, passed, detail in checks:
        mark = "✓" if passed else "✗"
        suffix = f"  ← {detail}" if detail else ""
        print(f"    {mark} {label}{suffix}")
        if not passed:
            all_passed = False

    safety_verdict = "PASS" if all_passed else "FAIL"
    print(f"\n  Safety result: {safety_verdict}")

    # --- Live execution ---
    print(f"\n  Execution:")
    if sql is None:
        print(f"    SKIP — no SQL to execute")
        return all_passed

    try:
        rows = execute_query(sql)
        row_count = len(rows)
        print(f"    Rows returned: {row_count}")
        if rows:
            for i, row in enumerate(rows[:2], 1):
                # Truncate long values for readability
                display = {
                    k: (str(v)[:60] + "…" if isinstance(v, str) and len(str(v)) > 60 else v)
                    for k, v in row.items()
                }
                print(f"    Row {i}: {json.dumps(display, default=str, ensure_ascii=False)}")
        else:
            print(f"    (no rows returned — query valid but empty result set)")
        exec_ok = True
    except Exception as e:
        print(f"    EXECUTION ERROR: {e}")
        print(f"    SQL that failed:")
        for line in (sql or "").strip().splitlines():
            print(f"      {line}")
        exec_ok = False

    return all_passed and exec_ok


def main() -> int:
    total = len(QUESTIONS)
    passed = 0
    failed = 0

    print(f"\n{'='*78}")
    print(f"  OptiFlow Agent Mode — SQL Generator Tests ({total} questions)")
    print(f"{'='*78}")

    for i, question in enumerate(QUESTIONS, 1):
        ok = run_one(i, question)
        if ok:
            passed += 1
        else:
            failed += 1

    print(f"\n{'='*78}")
    print(f"  Results: {passed} passed, {failed} failed, {total} total")
    print(f"{'='*78}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
