"""
Pipeline service — main query execution pipeline.
"""

import asyncio
import logging
import time

from backend.ai.client import RateLimitExhausted
from backend.cache import query_cache as _cache_mod
from backend.cache import approved_queries
from backend.connectors.mssql import execute_query, get_db_connection, verify_readonly_access
from backend.services.sql_generator import generate_universal, fix_sql
from backend.services.interpreter import interpret_results, interpret_chain_results

logger = logging.getLogger(__name__)

_DB_ACCESS_LEVEL: str | None = None

_SQL_ERROR_MAP = [
    ("Invalid column name",         "The query referenced a column that doesn't exist in this table. Try rephrasing your question."),
    ("Invalid object name",         "The query referenced a table that doesn't exist in the database. Try rephrasing your question."),
    ("GROUP BY",                    "The query has a grouping conflict. The AI was unable to fix it automatically — try rephrasing your question."),
    ("conversion failed",           "There's a data type mismatch in the query (e.g. comparing text to a number). Try rephrasing your question."),
    ("Incorrect syntax near",       "The query has a SQL syntax error. The AI was unable to fix it automatically — try rephrasing."),
    ("Ambiguous column name",       "Two tables have the same column name and the query didn't specify which one. Try rephrasing your question."),
    ("subquery returned more than", "The query expected a single value but got multiple rows. Try rephrasing to be more specific."),
    ("divide by zero",              "The query encountered a division by zero (likely a table with no matching rows). Try refining the filter."),
    ("string or binary data",       "A value was too long for the column. The data may have an unexpected format."),
]


def _translate_sql_error(error_str: str) -> str:
    for pattern, message in _SQL_ERROR_MAP:
        if pattern.lower() in error_str.lower():
            return message
    return "The query encountered a database error. Try rephrasing your question."


def run_pipeline(question: str) -> dict:
    """Universal pipeline: cache → approved log → generate_universal → return for approval."""
    t0 = time.perf_counter()

    from_cache        = False
    from_approved_log = False

    agent = _cache_mod.get(question)
    if agent:
        from_cache = True
    else:
        similar = approved_queries.find_similar(question)
        if similar:
            from_approved_log = True
            agent = {
                "mode":        "single",
                "sql":         similar["sql"],
                "explanation": "This SQL was previously approved for a similar question.",
                "tables_used": similar.get("tables_used", []),
                "confidence":  "high",
                "warnings":    [f"Reusing proven query from: \"{similar['question'][:100]}\""],
            }
        else:
            agent = generate_universal(question)
            if agent.get("sql") or agent.get("steps"):
                _cache_mod.put(question, agent)

    elapsed = int((time.perf_counter() - t0) * 1000)
    mode = agent.get("mode", "single")

    if mode in ("chain", "deep_dive"):
        logger.info(
            f"{mode.upper()} {'CACHED' if from_cache else 'GENERATED'}: "
            f"steps={len(agent.get('steps', []))}  confidence={agent.get('confidence')}  ({elapsed}ms)"
        )
        result = dict(agent)
        result.update({"from_cache": from_cache, "requires_approval": True, "time_ms": elapsed})
        return result

    if agent.get("sql") is None and agent.get("confidence") == "none":
        explanation = agent.get("explanation", "")
        logger.error(f"Pipeline failure — explanation: {explanation}")
        if "api" in explanation.lower() or "failed" in explanation.lower() or "configured" in explanation.lower():
            return {"mode": "error", "answer": f"AI error: {explanation}", "time_ms": elapsed}

    logger.info(
        f"AGENT {'CACHED' if from_cache else 'LOG' if from_approved_log else 'GENERATED'}: "
        f"confidence={agent.get('confidence')}  tables={agent.get('tables_used', [])}  ({elapsed}ms)"
    )
    return {
        "mode":              "agent",
        "sql":               agent.get("sql"),
        "explanation":       agent.get("explanation", ""),
        "tables_used":       agent.get("tables_used", []),
        "confidence":        agent.get("confidence"),
        "warnings":          agent.get("warnings", []),
        "from_cache":        from_cache,
        "from_approved_log": from_approved_log,
        "requires_approval": True,
        "time_ms":           elapsed,
    }


def run_agent_approval(question: str, sql: str, tables_used: list) -> dict:
    """Execute approved agent SQL and interpret results."""
    t0 = time.perf_counter()
    original_sql = sql

    logger.info(f"AGENT EXECUTING:\n{sql}")

    _SQL_MAX_RETRIES = 2
    current_sql = sql
    rows = None
    for attempt in range(_SQL_MAX_RETRIES + 1):
        try:
            rows = execute_query(current_sql)
            break
        except Exception as exec_err:
            error_str = str(exec_err)
            logger.error(f"Agent execution error attempt {attempt + 1}: {error_str}")
            if attempt < _SQL_MAX_RETRIES:
                fix = fix_sql(question, current_sql, error_str, tables_used=tables_used)
                if fix.get("sql"):
                    current_sql = fix["sql"]
                    logger.info(f"Retrying with fixed SQL:\n{current_sql}")
                else:
                    elapsed = int((time.perf_counter() - t0) * 1000)
                    return {"answer": _translate_sql_error(error_str), "rows_returned": 0, "time_ms": elapsed}
            else:
                elapsed = int((time.perf_counter() - t0) * 1000)
                return {"answer": _translate_sql_error(error_str), "rows_returned": 0, "time_ms": elapsed}

    if rows is None:
        elapsed = int((time.perf_counter() - t0) * 1000)
        return {"answer": "Query execution failed.", "rows_returned": 0, "time_ms": elapsed}

    total_rows = len(rows)
    logger.info(f"Agent query returned {total_rows} rows")

    try:
        answer = interpret_results(question, rows, total_rows)
        if total_rows > 100:
            answer = f"Showing first 100 of {total_rows} results.\n\n{answer}"
    except RateLimitExhausted:
        raise
    except Exception as e:
        logger.error(f"Interpretation failed: {e}")
        answer = f"{total_rows} row{'s' if total_rows != 1 else ''} returned."

    elapsed = int((time.perf_counter() - t0) * 1000)
    logger.info(f"AGENT COMPLETE: rows={total_rows}  time={elapsed}ms")

    approved_queries.append(question, original_sql, tables_used, total_rows, elapsed)

    return {
        "answer":       answer,
        "rows_returned": total_rows,
        "time_ms":       elapsed,
    }


def run_chain_approval(question: str, steps: list, summary_prompt: str, agent_type: str, entity_label: str = "") -> dict:
    """Execute all chain steps, collect results, then interpret together."""
    t0 = time.perf_counter()
    step_results = []
    total_rows = 0

    for step in steps:
        step_num    = step.get("step", "?")
        sql         = step.get("sql", "").strip()
        explanation = step.get("explanation", "")
        tables      = step.get("tables", [])

        if not sql:
            step_results.append({"step": step_num, "explanation": explanation, "rows": [], "error": "No SQL provided."})
            continue

        logger.info(f"CHAIN step {step_num} EXECUTING:\n{sql}")
        current_sql = sql
        step_rows = None
        step_error = None
        for attempt in range(3):
            try:
                step_rows = execute_query(current_sql)
                break
            except Exception as exec_err:
                step_error = str(exec_err)
                logger.error(f"Chain step {step_num} attempt {attempt + 1} failed: {step_error}")
                if attempt < 2:
                    fix = fix_sql(question, current_sql, step_error, tables_used=tables)
                    if fix.get("sql"):
                        current_sql = fix["sql"]
                    else:
                        break

        if step_rows is not None:
            total_rows += len(step_rows)
            step_results.append({"step": step_num, "explanation": explanation, "rows": step_rows})
        else:
            plain_err = _translate_sql_error(step_error or "") if step_error else "No SQL provided."
            step_results.append({"step": step_num, "explanation": explanation, "rows": [], "error": plain_err})

    try:
        answer = interpret_chain_results(question, step_results, summary_prompt, entity_label)
    except RateLimitExhausted:
        raise
    except Exception as e:
        logger.error(f"Chain interpretation failed: {e}")
        answer = f"Ran {len(steps)} queries, {total_rows} total rows returned."

    elapsed = int((time.perf_counter() - t0) * 1000)
    logger.info(f"CHAIN COMPLETE: steps={len(steps)}  rows={total_rows}  time={elapsed}ms")
    return {
        "answer":       answer,
        "step_results": step_results,
        "total_rows":   total_rows,
        "time_ms":      elapsed,
    }


async def startup_permission_check() -> None:
    """On startup, verify the configured DB user has only read-only access."""
    global _DB_ACCESS_LEVEL

    from backend.services.schema_manager import is_setup_complete, save_security_config
    from backend.config import settings

    if not is_setup_complete():
        return
    if not all([settings.DB_SERVER, settings.DB_NAME, settings.DB_USER, settings.DB_PASSWORD]):
        return

    loop = asyncio.get_running_loop()
    conn, _, error = await loop.run_in_executor(
        None, get_db_connection,
        settings.DB_SERVER, settings.DB_NAME, settings.DB_USER, settings.DB_PASSWORD,
    )
    if not conn:
        logger.warning(f"Startup permission check: could not connect — {error}")
        return

    try:
        result = await loop.run_in_executor(None, verify_readonly_access, conn)
    finally:
        try: conn.close()
        except Exception: pass

    level = result["access_level"]
    _DB_ACCESS_LEVEL = level
    save_security_config(result, settings.DB_USER or "")

    import sys
    if level == "blocked":
        logger.critical("BLOCKED: Database user has admin privileges. Reconfigure with a read-only user.")
        sys.exit(1)
    elif level == "warning":
        logger.warning(f"DB permission WARNING: {result['message']}")
    elif level == "readonly":
        logger.info("DB access verified: read-only")
    else:
        logger.warning(f"Could not verify DB permissions: {result['message']}")


def get_db_access_level() -> str | None:
    return _DB_ACCESS_LEVEL
