"""
OptiFlow AI — FastAPI server.

GET  /                      → setup wizard (first run) or chat UI (after setup)
GET  /setup/status          → {"setup_complete": bool}
POST /setup/test-ai-provider → verify AI API key
POST /setup/save-ai-config  → save AI provider config to model_config.json
POST /setup/test-ollama     → test local Ollama connection
POST /setup/test-connection → test DB credentials
POST /setup/discover-schema → run schema discovery, save db_config.json + schema file
POST /setup/save-company-knowledge → save config/company.md
POST /ask     → cache → approved log → table selection → generate SQL → return for approval
POST /approve → execute approved SQL (single, chain, or deep_dive) → interpret
POST /reject  → log user rejection of generated SQL
GET  /admin/company         → view/edit company knowledge (admin only)
POST /admin/update-ai-config → update AI provider config (admin only)
"""

import asyncio
import json
import logging
import os
import re
import secrets
import sys
import time
from datetime import datetime, date
from decimal import Decimal

import anthropic
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

import core.auth as auth
from config import settings
from config.loader import load_model_config, load_ai_config, save_ai_config
from config.ai_client import get_completion, test_connection as test_ai_connection, RateLimitExhausted
from core.setup_manager import (
    get_db_connection,
    is_setup_complete,
    load_schema_meta,
    load_old_schema_state,
    load_security_config,
    run_schema_discovery,
    save_business_context,
    save_db_credentials,
    save_schema_meta,
    save_security_config,
    verify_readonly_access,
)
from core.agent_sql_generator import (
    generate_chain,
    generate_business_health_chain,
    generate_deep_dive_chain,
    generate_universal,
    fix_sql,
    _load_company_knowledge,
)
import core.approved_queries as approved_queries
import core.audit_logger as audit_logger
import core.feedback_logger as feedback_logger
import core.query_cache as query_cache
from core.db import execute_query

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="OptiFlow AI")
templates = Jinja2Templates(directory="templates")

PIPELINE_TIMEOUT = 30   # seconds — intent parse + SQL generation
AGENT_TIMEOUT    = 30   # seconds — approved SQL execution + interpretation
CHAIN_TIMEOUT    = 90   # seconds — multi-step chain execution + interpretation
WELCOME_TIMEOUT  = 15   # seconds

def _get_anthropic_client():
    """Return a fresh Anthropic client with the currently configured API key."""
    ai_cfg = load_ai_config()
    key = ai_cfg.get("api_key") or ""
    return anthropic.Anthropic(api_key=key or "not-configured")


# _parse() is no longer called in the main flow — intent classification was removed.
# Every question now goes through generate_universal() which uses table selection
# to determine query type. Kept here for possible future use or A/B testing.
def _parse(question: str) -> dict:
    """(Unused) Dispatch to local or cloud intent parser."""
    ai_cfg = load_ai_config()
    if ai_cfg.get("local_enabled"):
        from core.local_intent_parser import parse as _local
        return _local(question)
    from core.intent_parser import parse as _cloud
    return _cloud(question)


# ── Session store ─────────────────────────────────────────────────────────────
# token (64-char hex) → {username, created_at, last_active}
_sessions: dict = {}
_SESSION_TTL    = 8 * 3600  # 8 hours of inactivity
_START_TIME     = time.time()
_DB_ACCESS_LEVEL: str | None = None   # set by startup permission check


def _check_session(request: Request) -> dict | None:
    """Return the session dict if the request carries a valid session cookie."""
    token = request.cookies.get("session_token")
    if not token:
        return None
    session = _sessions.get(token)
    if not session:
        return None
    if time.time() - session["last_active"] > _SESSION_TTL:
        del _sessions[token]
        return None
    session["last_active"] = time.time()
    return session


def _create_session(username: str) -> str:
    """Create a new session, return its token."""
    token = secrets.token_hex(32)
    _sessions[token] = {
        "username":    username,
        "created_at":  time.time(),
        "last_active": time.time(),
    }
    return token


def _get_response_model() -> str:
    """Return the currently configured response model name."""
    ai_cfg = load_ai_config()
    return ai_cfg.get("model", "claude-sonnet-4-6")


def _json_default(obj):
    """JSON serialiser that handles Decimal, datetime, date from DB rows."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, date)):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _safe_json(data) -> Response:
    """Return a JSON Response that safely serialises DB-originated values."""
    return Response(
        content=json.dumps(data, default=_json_default),
        media_type="application/json",
    )


@app.on_event("startup")
async def _startup_permission_check() -> None:
    """On startup, verify the configured DB user has only read-only access.

    - readonly → log INFO and continue.
    - warning  → log WARNING; set _DB_ACCESS_LEVEL so the chat UI shows a banner.
    - blocked  → log CRITICAL and exit the process immediately.
    - unknown  → log WARNING and continue (sys table access may be restricted).
    Skipped if setup is not yet complete (first-run install).
    """
    global _DB_ACCESS_LEVEL

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
        try:
            conn.close()
        except Exception:
            pass

    level = result["access_level"]
    _DB_ACCESS_LEVEL = level

    # Also refresh the last_checked timestamp in security.json
    save_security_config(result, settings.DB_USER or "")

    if level == "blocked":
        logger.critical(
            "BLOCKED: Database user has admin privileges. "
            "Reconfigure with a read-only user."
        )
        sys.exit(1)
    elif level == "warning":
        logger.warning(f"DB permission WARNING: {result['message']}")
    elif level == "readonly":
        logger.info("DB access verified: read-only")
    else:
        logger.warning(f"Could not verify DB permissions: {result['message']}")


def _run_welcome() -> dict:
    """Generate a welcome message using company knowledge and time-of-day greeting."""
    t0 = time.perf_counter()
    hour = datetime.now().hour
    if hour < 12:
        greeting = "Good morning"
    elif hour < 17:
        greeting = "Good afternoon"
    else:
        greeting = "Good evening"

    knowledge = _load_company_knowledge()

    if knowledge:
        # Use LLM to generate a personalized welcome based on company.md
        try:
            body = get_completion(
                system=(
                    "You are a business intelligence assistant. "
                    "Write a short (2-3 sentence) welcome message for a user logging in. "
                    "Use the company knowledge to personalize the greeting. "
                    "Mention 1-2 types of questions they can ask (based on what this business tracks). "
                    "End with 'Ask me anything or type your question below.' "
                    "Do NOT make up data or statistics. Be friendly and concise."
                ),
                user=(
                    f"Time of day: {greeting.lower().replace('good ', '')}. "
                    f"Company knowledge:\n{knowledge[:800]}"
                ),
                max_tokens=200,
                temperature=0,
            )
            message = f"{greeting}! {body}"
        except Exception as e:
            logger.warning(f"Welcome LLM call failed: {e}")
            message = (
                f"{greeting}! I'm ready to help you explore your database. "
                "Ask me anything — I'll generate the SQL, show it to you for review, "
                "then run it and explain the results."
            )
    else:
        message = (
            f"{greeting}! I'm ready to help you explore your database. "
            "Ask me anything — I'll generate the SQL, show it to you for review, "
            "then run it and explain the results.\n\n"
            "Tip: Try asking for a business health summary, or ask about any specific "
            "area of your data."
        )

    elapsed = int((time.perf_counter() - t0) * 1000)
    logger.info(f"Welcome message generated in {elapsed}ms")
    return {"message": message, "time_ms": elapsed}


# ---------------------------------------------------------------------------
# Agent mode helpers
# ---------------------------------------------------------------------------

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
    """Convert a raw SQL Server error message into a plain-English explanation."""
    for pattern, message in _SQL_ERROR_MAP:
        if pattern.lower() in error_str.lower():
            return message
    return "The query encountered a database error. Try rephrasing your question."

_ADVISOR_SYSTEM = """\
You are a sharp business advisor who knows this company intimately. \
You translate database query results into actionable business insights.

Rules:
- Every number you state MUST come directly from a cell in the query results. \
Never calculate, estimate, or approximate.
- State exact figures. Never round or abbreviate the number itself \
(e.g. Rs 14,23,567.50 not 'approximately Rs 14.2L').
- Format currency in Indian style (Rs X.XXL / Rs X.XXCr) as a label after the exact figure \
when helpful for readability, but always preserve the exact value.
- Add business context: is this number good or bad? Concerning? Should someone act on it?
- Use the company knowledge to frame numbers in terms management understands.
- Be direct and opinionated: "Rs 51,05,990 (51.06L) overdue beyond 90 days — \
this needs immediate follow-up" not just "Total overdue: Rs 51,05,990".
- Attribute every number to its source: \
"Total project value (SUM of Sales_Amount, ProSt table): Rs 14,23,567"
- When comparing two values: state both exact figures, the exact difference, \
and what it means for the business.
- If the result set is empty: say "No data found for this query." — do not guess why.
- If results are a list (not aggregates), describe what the list shows and highlight \
the top/bottom items with exact values.
- Format: clean markdown. Bold **key insight** first, then 2-5 bullet points, \
then an **Action** line only if something is genuinely urgent."""


def _interpret_results(question: str, rows: list, total_rows: int) -> str:
    """Call Claude to interpret query results as a business advisor."""
    rows_json = json.dumps(rows, default=str)   # ALL rows — never truncate
    knowledge = _load_company_knowledge()
    knowledge_section = f"\n\nCompany knowledge:\n{knowledge}" if knowledge else ""

    return get_completion(
        system=_ADVISOR_SYSTEM + knowledge_section,
        user=(
            f"The user asked: {question}\n\n"
            f"Query results ({total_rows} rows):\n{rows_json}"
        ),
        max_tokens=800,
        temperature=0,
    )


def _run_agent_approval(question: str, sql: str, tables_used: list) -> dict:
    """Execute approved agent SQL and interpret results via Claude."""
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
                logger.info("Asking AI to fix SQL…")
                fix = fix_sql(question, current_sql, error_str, tables_used=tables_used)
                if fix.get("sql"):
                    current_sql = fix["sql"]
                    logger.info(f"Retrying with fixed SQL:\n{current_sql}")
                else:
                    elapsed = int((time.perf_counter() - t0) * 1000)
                    plain_err = _translate_sql_error(error_str)
                    return {"answer": plain_err, "rows_returned": 0, "time_ms": elapsed}
            else:
                elapsed = int((time.perf_counter() - t0) * 1000)
                plain_err = _translate_sql_error(error_str)
                return {"answer": plain_err, "rows_returned": 0, "time_ms": elapsed}
    if rows is None:
        elapsed = int((time.perf_counter() - t0) * 1000)
        return {"answer": "Query execution failed.", "rows_returned": 0, "time_ms": elapsed}

    total_rows = len(rows)
    logger.info(f"Agent query returned {total_rows} rows")

    try:
        answer = _interpret_results(question, rows, total_rows)
        if total_rows > 100:
            answer = f"Showing first 100 of {total_rows} results.\n\n{answer}"
    except RateLimitExhausted:
        raise  # bubble up to endpoint handler — frontend will show countdown + retry
    except Exception as e:
        logger.error(f"Interpretation failed: {e}")
        answer = f"{total_rows} row{'s' if total_rows != 1 else ''} returned."

    elapsed = int((time.perf_counter() - t0) * 1000)
    logger.info(f"AGENT COMPLETE: rows={total_rows}  time={elapsed}ms")

    # Persist this approved query for future reuse
    approved_queries.append(question, original_sql, tables_used, total_rows, elapsed)

    return {
        "answer": answer,
        "rows_returned": total_rows,
        "time_ms": elapsed,
    }


def _interpret_chain_results(
    question: str,
    step_results: list,
    summary_prompt: str,
    entity_label: str = "",
) -> str:
    """Call Claude to synthesise results from multiple SQL steps as a business advisor."""
    parts = []
    for sr in step_results:
        step_num    = sr.get("step", "?")
        explanation = sr.get("explanation", "")
        rows        = sr.get("rows", [])
        rows_json   = json.dumps(rows, default=str)  # ALL rows — never truncate
        parts.append(
            f"=== Step {step_num}: {explanation} ({len(rows)} rows) ===\n{rows_json}"
        )

    combined = "\n\n".join(parts)
    context  = f"The user asked: {question}\n\n{combined}" if question else combined

    knowledge = _load_company_knowledge()
    knowledge_section = f"\n\nCompany knowledge:\n{knowledge}" if knowledge else ""

    chain_system = (
        _ADVISOR_SYSTEM
        + "\n\nFor multi-step results: use ### headings to organise each area. "
        "Synthesise across all steps — don't just repeat them. "
        "Lead with the most important cross-cutting insight."
        + knowledge_section
    )

    return get_completion(
        system=chain_system,
        user=f"{summary_prompt}\n\n{context}",
        max_tokens=1200,
        temperature=0,
    )


def _run_chain_approval(
    question: str,
    steps: list,
    summary_prompt: str,
    agent_type: str,
    entity_label: str = "",
) -> dict:
    """Execute all chain steps, collect results, then interpret together."""
    t0 = time.perf_counter()
    step_results = []
    total_rows = 0

    for step in steps:
        step_num = step.get("step", "?")
        sql = step.get("sql", "").strip()
        explanation = step.get("explanation", "")
        tables = step.get("tables", [])

        if not sql:
            logger.warning(f"Chain step {step_num} has no SQL — skipping")
            step_results.append({
                "step": step_num,
                "explanation": explanation,
                "rows": [],
                "error": "No SQL provided for this step.",
            })
            continue

        logger.info(f"CHAIN step {step_num} EXECUTING:\n{sql}")
        _SQL_MAX_RETRIES = 2
        current_sql = sql
        step_rows = None
        step_error = None
        for attempt in range(_SQL_MAX_RETRIES + 1):
            try:
                step_rows = execute_query(current_sql)
                break
            except Exception as exec_err:
                step_error = str(exec_err)
                logger.error(f"Chain step {step_num} attempt {attempt + 1} failed: {step_error}")
                if attempt < _SQL_MAX_RETRIES:
                    fix = fix_sql(question, current_sql, step_error, tables_used=tables)
                    if fix.get("sql"):
                        current_sql = fix["sql"]
                    else:
                        break
        if step_rows is not None:
            logger.info(f"Chain step {step_num}: {len(step_rows)} rows")
            total_rows += len(step_rows)
            step_results.append({"step": step_num, "explanation": explanation, "rows": step_rows})
        else:
            plain_err = _translate_sql_error(step_error or "") if step_error else "No SQL provided."
            step_results.append({"step": step_num, "explanation": explanation, "rows": [], "error": plain_err})

    # Interpret combined results
    try:
        answer = _interpret_chain_results(
            question, step_results, summary_prompt, entity_label
        )
    except RateLimitExhausted:
        raise  # bubble up to endpoint handler — frontend will show countdown + retry
    except Exception as e:
        logger.error(f"Chain interpretation failed: {e}")
        answer = f"Ran {len(steps)} queries, {total_rows} total rows returned."

    elapsed = int((time.perf_counter() - t0) * 1000)
    logger.info(f"CHAIN COMPLETE: steps={len(steps)}  rows={total_rows}  time={elapsed}ms")
    return {
        "answer": answer,
        "step_results": step_results,
        "total_rows": total_rows,
        "time_ms": elapsed,
    }


# ---------------------------------------------------------------------------
# Main pipeline — routes to template or agent mode
# ---------------------------------------------------------------------------

def _run_pipeline(question: str) -> dict:
    """Universal pipeline: cache → approved log → generate_universal → return for approval.

    No intent classifier. Every question follows the same path. query_type is
    determined by table selection (cheap LLM call) inside generate_universal().
    """
    t0 = time.perf_counter()

    from_cache        = False
    from_approved_log = False

    # 1. In-memory cache (exact question, TTL=1h)
    agent = query_cache.get(question)
    if agent:
        from_cache = True
    else:
        # 2. Approved-query log (similar past question — proven SQL, instant)
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
            # 3. Generate — table selection picks type, AI generates SQL
            agent = generate_universal(question)
            if agent.get("sql") or agent.get("steps"):
                query_cache.put(question, agent)

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

    # Single query (mode == "single") — check for API failure
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
        "mode":              "agent",   # frontend expects "agent" for single-query approval cards
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


def _audit_ask(username: str, question: str, result: dict) -> None:
    """Dispatch a single audit log call for a completed /ask pipeline result."""
    mode = result.get("mode")
    if result.get("from_cache"):
        audit_logger.log_action(username, "query_agent_cached", {"question": question})
    elif mode == "agent":
        audit_logger.log_action(username, "query_agent_generated", {
            "question":    question,
            "sql":         (result.get("sql") or "")[:500],
            "tables_used": result.get("tables_used", []),
            "confidence":  result.get("confidence"),
        })
    elif mode == "chain":
        audit_logger.log_action(username, "query_chain", {
            "question":    question,
            "steps_count": len(result.get("steps", [])),
        })
    elif mode == "deep_dive":
        audit_logger.log_action(username, "query_deep_dive", {
            "question":     question,
            "entity_type":  result.get("entity_type"),
            "entity_label": result.get("entity_label"),
        })


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # First-time install: no users created yet → show setup (no auth needed)
    if not auth.users_exist():
        return templates.TemplateResponse("setup.html", {"request": request})

    session = _check_session(request)
    if not session:
        return RedirectResponse("/login", status_code=302)

    if not is_setup_complete():
        return templates.TemplateResponse("setup.html", {"request": request})

    user_obj  = auth.find_user(session["username"])
    user_role = user_obj.get("role", "user") if user_obj else "user"

    db_warning: str | None = None
    if _DB_ACCESS_LEVEL == "warning":
        db_warning = "Database user has write permissions. Contact your admin to use a read-only user."
    elif _DB_ACCESS_LEVEL == "unknown":
        db_warning = "Database user permissions could not be verified. Ensure this is a read-only user."

    return templates.TemplateResponse(
        "chat.html",
        {
            "request":    request,
            "username":   session["username"],
            "user_role":  user_role,
            "db_warning": db_warning,
        },
    )


# ── Auth endpoints ─────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    # Already logged in → go home
    if _check_session(request):
        return RedirectResponse("/", status_code=302)
    expired = request.query_params.get("expired") == "1"
    return templates.TemplateResponse("login.html", {"request": request, "expired": expired})


@app.post("/login")
async def login(request: Request):
    body     = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")

    if not username or not password:
        return _safe_json({"success": False, "error": "Username and password are required."})

    ip   = request.client.host if request.client else "unknown"
    loop = asyncio.get_event_loop()
    ok   = await loop.run_in_executor(None, auth.verify_password, username, password)
    if not ok:
        logger.warning(f"Failed login attempt for user '{username}'")
        audit_logger.log_action(username, "login_failed", {"ip": ip})
        return _safe_json({"success": False, "error": "Invalid username or password."})

    token    = _create_session(username)
    response = _safe_json({"success": True})
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        secure=True,
        max_age=_SESSION_TTL,
        samesite="lax",
    )
    logger.info(f"Login: user='{username}'")
    audit_logger.log_action(username, "login", {"success": True, "ip": ip})
    return response


@app.post("/logout")
async def logout(request: Request):
    token = request.cookies.get("session_token")
    if token and token in _sessions:
        username = _sessions[token].get("username", "?")
        del _sessions[token]
        logger.info(f"Logout: user='{username}'")
        audit_logger.log_action(username, "logout", {})
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("session_token")
    return response


# ── Setup wizard endpoints ────────────────────────────────────────────────────

@app.get("/setup/status")
async def setup_status():
    return _safe_json({
        "setup_complete": is_setup_complete(),
        "admin_exists":   auth.users_exist(),
    })


@app.post("/setup/create-admin")
async def setup_create_admin(request: Request):
    """Create the first admin user. Only works if no users exist yet."""
    if auth.users_exist():
        # Admin already exists — require session for re-setup
        if not _check_session(request):
            return _safe_json({"success": False, "error": "Already configured. Please log in."})

    body     = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")

    if not username:
        return _safe_json({"success": False, "error": "Username is required."})
    if len(password) < 8:
        return _safe_json({"success": False, "error": "Password must be at least 8 characters."})

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, auth.create_user, username, password, "admin")
    except Exception as e:
        logger.error(f"create-admin error: {e}")
        return _safe_json({"success": False, "error": str(e)})

    # Auto-login: create session so remaining setup steps work
    token    = _create_session(username)
    response = _safe_json({"success": True, "username": username})
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        secure=True,
        max_age=_SESSION_TTL,
        samesite="lax",
    )
    logger.info(f"Admin user '{username}' created and auto-logged in")
    return response


def _setup_auth_check(request: Request):
    """Return error response if setup requires auth (admin exists but no session)."""
    if auth.users_exist() and not _check_session(request):
        return _safe_json({"success": False, "error": "Session expired. Please log in again."})
    return None


@app.post("/setup/test-ai-provider")
async def setup_test_ai_provider(request: Request):
    """Verify an AI API key by making a minimal API call."""
    if (err := _setup_auth_check(request)):
        return err
    body = await request.json()
    provider         = body.get("provider", "anthropic").strip()
    api_key          = body.get("api_key", "").strip()
    model            = body.get("model", "").strip()
    custom_endpoint  = body.get("custom_endpoint", "").strip()

    if not api_key:
        return _safe_json({"success": False, "error": "API key is required."})
    if not model:
        return _safe_json({"success": False, "error": "Model name is required."})

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, test_ai_connection, provider, api_key, model, custom_endpoint
    )
    return _safe_json(result)


@app.post("/setup/save-ai-config")
async def setup_save_ai_config(request: Request):
    """Save AI provider configuration to config/model_config.json."""
    if (err := _setup_auth_check(request)):
        return err
    body = await request.json()
    data = {
        "provider":        body.get("provider", "anthropic").strip(),
        "api_key":         body.get("api_key", "").strip(),
        "model":           body.get("model", "").strip(),
        "custom_endpoint": body.get("custom_endpoint", "").strip(),
        "local_enabled":   bool(body.get("local_enabled", False)),
        "local_endpoint":  body.get("local_endpoint", "http://localhost:11434").strip(),
        "local_model":     body.get("local_model", "qwen3:8b").strip(),
    }
    if not data["api_key"]:
        return _safe_json({"success": False, "error": "API key is required."})
    if not data["model"]:
        return _safe_json({"success": False, "error": "Model name is required."})

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, save_ai_config, data)
        return _safe_json({"success": True})
    except Exception as e:
        logger.error(f"save-ai-config error: {e}")
        return _safe_json({"success": False, "error": str(e)})


@app.post("/setup/test-ollama")
async def setup_test_ollama(request: Request):
    """Test if a local Ollama instance is running at the given endpoint."""
    if (err := _setup_auth_check(request)):
        return err
    body     = await request.json()
    endpoint = body.get("endpoint", "http://localhost:11434").strip().rstrip("/")

    import requests as _requests
    try:
        resp = _requests.get(f"{endpoint}/api/tags", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        models = [m.get("name") for m in data.get("models", [])]
        return _safe_json({"success": True, "models": models})
    except Exception as e:
        return _safe_json({"success": False, "error": f"Cannot reach Ollama at {endpoint}: {e}"})


@app.post("/setup/test-connection")
async def setup_test_connection(request: Request):
    if (err := _setup_auth_check(request)):
        return err
    body = await request.json()
    server   = body.get("server", "").strip()
    database = body.get("database", "").strip()
    user     = body.get("user", "").strip()
    password = body.get("password", "").strip()

    if not all([server, database, user, password]):
        return _safe_json({"success": False, "error": "All fields are required."})

    loop = asyncio.get_event_loop()
    conn, driver, error = await loop.run_in_executor(
        None, get_db_connection, server, database, user, password
    )
    if conn:
        conn.close()
        return _safe_json({"success": True, "message": "Connection successful."})
    return _safe_json({"success": False, "error": error})


@app.post("/setup/check-permissions")
async def setup_check_permissions(request: Request):
    """Check what access level the supplied DB user has (read-only, write, admin).
    Saves result to config/security.json.
    """
    if (err := _setup_auth_check(request)):
        return err
    body = await request.json()
    server   = body.get("server", "").strip()
    database = body.get("database", "").strip()
    user     = body.get("user", "").strip()
    password = body.get("password", "").strip()

    if not all([server, database, user, password]):
        return _safe_json({"success": False, "error": "All fields are required."})

    loop = asyncio.get_event_loop()
    conn, _, error = await loop.run_in_executor(
        None, get_db_connection, server, database, user, password
    )
    if not conn:
        return _safe_json({"success": False, "error": error})

    try:
        result = await loop.run_in_executor(None, verify_readonly_access, conn)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Persist so the settings page and startup check can display it
    try:
        save_security_config(result, user)
    except Exception as e:
        logger.warning(f"Could not save security.json: {e}")

    return _safe_json({"success": True, **result})


@app.post("/setup/discover-schema")
async def setup_discover_schema(request: Request):
    if (err := _setup_auth_check(request)):
        return err
    body = await request.json()
    server   = body.get("server", "").strip()
    database = body.get("database", "").strip()
    user     = body.get("user", "").strip()
    password = body.get("password", "").strip()

    if not all([server, database, user, password]):
        return _safe_json({"success": False, "error": "All fields are required."})

    loop = asyncio.get_event_loop()
    conn, driver, error = await loop.run_in_executor(
        None, get_db_connection, server, database, user, password
    )
    if not conn:
        return _safe_json({"success": False, "error": error})

    def _discover():
        try:
            schema = run_schema_discovery(conn, database, server)
            save_db_credentials(server, database, user, password)
            return schema
        finally:
            conn.close()

    try:
        schema_data = await asyncio.wait_for(
            loop.run_in_executor(None, _discover),
            timeout=300,
        )
        tables_data = schema_data.get("tables", [])
        save_schema_meta(tables_data, {"event": "initial_setup", "tables": len(tables_data)})

        # Generate suggested questions from the new schema (best-effort)
        try:
            qs = _generate_suggested_questions()
            if qs:
                _save_suggested_questions(qs)
        except Exception as _exc:
            logger.warning(f"setup: suggested questions generation skipped: {_exc}")

        actor = _check_session(request)
        audit_logger.log_action(
            actor["username"] if actor else "setup",
            "setup_completed",
            {
                "database":          database,
                "tables_discovered": len(tables_data),
            },
        )
        return _safe_json({"success": True, **schema_data})
    except asyncio.TimeoutError:
        return _safe_json({"success": False, "error": "Schema discovery timed out (>300s). Try again — large databases may take a few minutes."})
    except Exception as e:
        logger.error(f"Schema discovery error: {e}")
        return _safe_json({"success": False, "error": str(e)})


@app.post("/setup/save-context")
async def setup_save_context(request: Request):
    if (err := _setup_auth_check(request)):
        return err
    body = await request.json()
    context = {
        "company_name":       body.get("company_name", ""),
        "business_type":      body.get("business_type", ""),
        "data_quality_rules": body.get("data_quality_rules", []),
        "terminology":        body.get("terminology", []),
        "column_warnings":    body.get("column_warnings", []),
    }
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, save_business_context, context)
        return _safe_json({"success": True})
    except Exception as e:
        logger.error(f"save-context error: {e}")
        return _safe_json({"success": False, "error": str(e)})


_COMPANY_MD_PATH        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "company.md")
_SCHEMA_CONTEXT_PATH    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts", "schema_context.txt")
_SCHEMA_INDEX_PATH      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts", "schema_index.txt")
_SUGGESTED_Q_PATH       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "suggested_questions.json")

_SUGGEST_QUESTIONS_SYSTEM = """You are a helpful data analyst. Given a database schema index (one line per table), suggest 6 short, natural-language business questions a user might want to ask about this data.

Return ONLY a JSON array with no preamble:
[
  {"label": "Short chip label", "question": "Full natural language question?"},
  ...
]

Rules:
- Cover different tables — do not repeat the same table in multiple questions
- Questions must be genuinely useful business queries (counts, summaries, trends, recent activity, status breakdowns)
- label: ≤ 30 characters, plain text (shown as a button)
- question: natural phrasing that will be sent to the query engine"""


def _generate_suggested_questions() -> list:
    """Generate 6 suggested questions from schema_index.txt via AI, with table-name fallback."""
    if not os.path.exists(_SCHEMA_INDEX_PATH):
        return []
    try:
        with open(_SCHEMA_INDEX_PATH, encoding="utf-8") as f:
            schema_index = f.read().strip()
    except Exception:
        return []
    if not schema_index:
        return []

    # Try AI generation
    try:
        text = get_completion(
            system=_SUGGEST_QUESTIONS_SYSTEM,
            user=f"Database schema:\n{schema_index}",
            max_tokens=700,
            temperature=0.3,
        )
        # Extract JSON array
        start = text.find('[')
        end   = text.rfind(']')
        fence = re.search(r'```(?:json)?\s*(.*?)\s*```', text, re.DOTALL)
        if fence:
            candidate = fence.group(1)
        elif start != -1 and end > start:
            candidate = text[start:end + 1]
        else:
            candidate = text
        questions = json.loads(candidate)
        if isinstance(questions, list):
            validated = [
                {"label": str(q["label"])[:40], "question": str(q["question"])}
                for q in questions
                if isinstance(q, dict) and q.get("label") and q.get("question")
            ]
            if len(validated) >= 2:
                return validated[:8]
    except Exception as exc:
        logger.warning(f"AI suggested questions failed, using fallback: {exc}")

    # Fallback: build simple questions from table names
    table_names = []
    for line in schema_index.splitlines():
        parts = line.split("|")
        if parts and parts[0].strip():
            table_names.append(parts[0].strip())
    if not table_names:
        return []
    templates = [
        ("Overview",    "Give me an overview of {}"),
        ("Count",       "How many {} records are there?"),
        ("Recent",      "Show the most recent {}"),
        ("Summary",     "Summarize {} data"),
        ("Top entries", "What are the top {} entries?"),
        ("Trends",      "Show trends in {}"),
    ]
    return [
        {"label": f"{table_names[i % len(table_names)]} {lbl}",
         "question": q.format(table_names[i % len(table_names)])}
        for i, (lbl, q) in enumerate(templates)
    ]


def _save_suggested_questions(questions: list) -> None:
    try:
        os.makedirs(os.path.dirname(_SUGGESTED_Q_PATH), exist_ok=True)
        with open(_SUGGESTED_Q_PATH, "w", encoding="utf-8") as f:
            json.dump(questions, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        logger.warning(f"Could not save suggested questions: {exc}")


def _load_suggested_questions() -> list:
    try:
        if os.path.exists(_SUGGESTED_Q_PATH):
            with open(_SUGGESTED_Q_PATH, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []

_COMPANY_DRAFT_SYSTEM = """You are analyzing a database schema for a business. Based on the table names, column names, data types, row counts, and enum values, write a comprehensive company knowledge document.

For each table, explain:
- What business process this table tracks (infer from column names)
- What each status/type value likely means
- How this table relates to other tables (follow foreign key patterns in column names)
- What key business questions this table can answer
- Any data quality concerns (NULL-heavy columns, suspicious values, test data patterns)

Also infer:
- What industry this company is in
- What the core business workflow is (e.g. Lead → Quote → Order → Invoice → Payment)
- What the key business metrics would be

Write in this exact markdown structure:

# Company: [Inferred company name, or "Unknown — please update"]

## Industry & Business Model
[2-3 sentences about what the company does, based on the schema]

## Core Business Workflow
[The main process flow using → arrows, e.g. Lead → Quote → PO → Invoice → Payment]

## Table Guide

For EACH table, write:

### [TableName] ([row_count] rows)
**Purpose:** [What this table tracks]
**Key columns:** [Most important 5-6 columns with their likely meanings]
**Status values:** [If status/type columns exist, list each value and its likely business meaning — mark uncertain with [GUESS]]
**Relationships:** [Which other tables this connects to, based on shared column name patterns]
**Use when asked about:** [CRITICAL — list specific business questions and phrases a user might type. Be detailed. Example: "project pipeline, active projects, projects by status, overdue projects, project count by customer, which projects are stuck"]
**Data quality notes:** [Any concerns — high NULL rate, columns always empty, suspicious patterns]

## Key Business Metrics
- [Metric name]: [How to calculate it and which table(s) to use]

## Business Terminology
- [Term from column or table name]: [What it likely means in this business context]

## Known Data Issues
- [Any patterns suggesting data quality issues]

## Fiscal Calendar
Fiscal year: [Infer from date column patterns, or "Please fill in — calendar year assumed"]

Be specific. Use actual column and table names. Mark uncertain inferences with [GUESS].
Write in plain English. This document is read by business users, not developers."""


_COMPANY_FOLLOWUP_SYSTEM = """You generated a company knowledge document from a database schema. Now generate 3-5 targeted follow-up questions to fill in gaps that cannot be inferred from the schema alone.

Consider asking about:
- Status column values you guessed at — are the guesses correct?
- If multiple tables have amount/revenue columns — which is the primary revenue metric?
- Fiscal year if date columns were found
- What the company calls its customers (clients, accounts, partners?)
- Any columns that were mostly NULL — is that expected?

Rules:
- Reference actual table and column names
- Keep each question under 2 sentences
- Make placeholder text show a concrete example answer
- Generate 3-5 questions maximum

Return ONLY a valid JSON array:
[
  {"id": "q1", "question": "...", "placeholder": "e.g. ..."},
  {"id": "q2", "question": "...", "placeholder": "e.g. ..."}
]"""


@app.post("/setup/generate-company-draft")
async def setup_generate_company_draft(request: Request):
    """Generate a rich company.md draft by sending full schema_context.txt to the AI."""
    if (err := _setup_auth_check(request)):
        return err
    body    = await request.json()
    db_name = body.get("db_name", "the database")

    if not os.path.exists(_SCHEMA_CONTEXT_PATH):
        return _safe_json({"success": False, "error": "Schema not discovered yet — complete Step 4 first."})

    try:
        with open(_SCHEMA_CONTEXT_PATH, encoding="utf-8") as f:
            schema_content = f.read()
    except Exception as e:
        return _safe_json({"success": False, "error": f"Could not read schema: {e}"})

    try:
        content = get_completion(
            system=_COMPANY_DRAFT_SYSTEM,
            user=f"Database name: {db_name}\n\n{schema_content}",
            max_tokens=4000,
            temperature=0,
        )
        return _safe_json({"success": True, "content": content})
    except RateLimitExhausted as rl:
        return _safe_json({"success": False, "error": "Rate limited", "retry_after": rl.retry_after})
    except Exception as e:
        logger.error(f"generate-company-draft error: {e}")
        return _safe_json({"success": False, "error": str(e)})


@app.post("/setup/company-followup")
async def setup_company_followup(request: Request):
    """Generate targeted follow-up questions from the AI-generated company.md draft."""
    if (err := _setup_auth_check(request)):
        return err
    body  = await request.json()
    draft = body.get("draft", "").strip()

    if not draft:
        return _safe_json({"success": True, "questions": []})

    try:
        text = get_completion(
            system=_COMPANY_FOLLOWUP_SYSTEM,
            user=f"Here is the company knowledge document I generated:\n\n{draft[:3000]}\n\nGenerate follow-up questions.",
            max_tokens=600,
            temperature=0,
        )
        import re as _re
        fence = _re.search(r"```(?:json)?\s*(.*?)\s*```", text, _re.DOTALL)
        if fence:
            text = fence.group(1)
        questions = json.loads(text)
        if not isinstance(questions, list):
            questions = []
        return _safe_json({"success": True, "questions": questions[:5]})
    except Exception as e:
        logger.warning(f"company-followup non-fatal: {e}")
        return _safe_json({"success": True, "questions": []})


@app.post("/setup/save-company-knowledge")
async def setup_save_company_knowledge(request: Request):
    """Save company knowledge markdown to config/company.md.

    Accepts optional followup_answers list [{question, answer}] to append.
    """
    if (err := _setup_auth_check(request)):
        return err
    body    = await request.json()
    content = body.get("content", "").strip()
    answers = body.get("followup_answers", [])

    # Append any non-empty follow-up answers
    filled = [a for a in answers if isinstance(a, dict) and a.get("answer", "").strip()]
    if filled:
        content += "\n\n## Additional Context\n"
        for a in filled:
            content += f"\n**{a['question']}**\n{a['answer']}\n"

    try:
        os.makedirs(os.path.dirname(_COMPANY_MD_PATH), exist_ok=True)
        with open(_COMPANY_MD_PATH, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info("Company knowledge saved to config/company.md")
        return _safe_json({"success": True})
    except Exception as e:
        logger.error(f"save-company-knowledge error: {e}")
        return _safe_json({"success": False, "error": str(e)})


@app.get("/api/suggested-questions")
async def get_suggested_questions(request: Request):
    """Return suggested chat chips. Serves from cache; generates on first call."""
    if not _check_session(request):
        return JSONResponse({"questions": []}, status_code=401)

    questions = _load_suggested_questions()
    if not questions:
        loop = asyncio.get_event_loop()
        questions = await loop.run_in_executor(None, _generate_suggested_questions)
        if questions:
            _save_suggested_questions(questions)

    return _safe_json({"questions": questions})


@app.get("/welcome")
async def welcome(request: Request):
    if not _check_session(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _run_welcome),
            timeout=WELCOME_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error(f"Welcome timeout after {WELCOME_TIMEOUT}s")
        result = {
            "message": (
                "Hello! I'm ready to help you explore your database. "
                "Ask me anything — I'll generate the SQL, show it to you for review, "
                "then run it and explain the results."
            ),
            "time_ms": WELCOME_TIMEOUT * 1000,
        }
    return _safe_json(result)


@app.post("/ask")
async def ask(request: Request):
    session = _check_session(request)
    if not session:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    body = await request.json()
    question = body.get("question", "").strip()

    if not question:
        return JSONResponse(
            {"mode": "error", "answer": "Please ask a question.", "intent": None, "time_ms": 0}
        )

    logger.info(f"Question: {question!r}")

    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _run_pipeline, question),
            timeout=PIPELINE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error(f"Pipeline timeout after {PIPELINE_TIMEOUT}s")
        result = {
            "mode": "error",
            "answer": (
                "That took too long. The database might be slow right now. "
                "Please try again in a moment."
            ),
            "intent": None,
            "time_ms": PIPELINE_TIMEOUT * 1000,
        }
    except RateLimitExhausted as rl:
        logger.warning(f"Rate limit during /ask — retry_after={rl.retry_after}s")
        return JSONResponse({"rate_limited": True, "retry_after": rl.retry_after})

    _audit_ask(session["username"], question, result)
    return _safe_json(result)


@app.post("/approve")
async def approve(request: Request):
    session = _check_session(request)
    if not session:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    body        = await request.json()
    agent_type  = body.get("agent_type", "single")   # "single" | "chain" | "deep_dive"
    question    = body.get("question", "").strip()

    loop = asyncio.get_event_loop()

    # ── Chain / Deep Dive approval ────────────────────────────────────────
    if agent_type in ("chain", "deep_dive"):
        steps          = body.get("steps", [])
        summary_prompt = body.get("summary_prompt", "")
        entity_label   = body.get("entity_label", "")

        if not steps:
            return JSONResponse({"error": "No steps provided."}, status_code=400)

        logger.info(
            f"CHAIN APPROVE ({agent_type}) — {question[:80]!r}  steps={len(steps)}"
        )
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    _run_chain_approval,
                    question, steps, summary_prompt, agent_type, entity_label,
                ),
                timeout=CHAIN_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error(f"Chain execution timeout after {CHAIN_TIMEOUT}s")
            result = {
                "answer": (
                    f"Query chain timed out after {CHAIN_TIMEOUT} seconds. "
                    "Try a simpler question or contact your database administrator."
                ),
                "step_results": [],
                "total_rows": 0,
                "time_ms": CHAIN_TIMEOUT * 1000,
            }
        except RateLimitExhausted as rl:
            logger.warning(f"Rate limit during chain approval — retry_after={rl.retry_after}s")
            return JSONResponse({"rate_limited": True, "retry_after": rl.retry_after})
        audit_logger.log_action(session["username"], "query_chain", {
            "question":      question,
            "agent_type":    agent_type,
            "steps_count":   len(steps),
            "total_rows":    result.get("total_rows", 0),
            "total_time_ms": result.get("time_ms"),
        })
        return _safe_json(result)

    # ── Single SQL approval ───────────────────────────────────────────────
    sql         = body.get("sql", "").strip()
    tables_used = body.get("tables_used", [])

    if not sql:
        return JSONResponse({"error": "No SQL provided."}, status_code=400)

    logger.info(f"AGENT APPROVE — {question[:80]!r}")

    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(
                None, _run_agent_approval, question, sql, tables_used
            ),
            timeout=AGENT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error(f"Agent execution timeout after {AGENT_TIMEOUT}s")
        result = {
            "answer": (
                "Query timed out after 30 seconds. "
                "Try a more specific question or contact your database administrator."
            ),
            "rows_returned": 0,
            "time_ms": AGENT_TIMEOUT * 1000,
        }
    except RateLimitExhausted as rl:
        logger.warning(f"Rate limit during agent approval — retry_after={rl.retry_after}s")
        return JSONResponse({"rate_limited": True, "retry_after": rl.retry_after})

    audit_logger.log_action(session["username"], "query_agent_approved", {
        "question":          question,
        "sql":               sql[:500],
        "rows_returned":     result.get("rows_returned", 0),
        "execution_time_ms": result.get("time_ms"),
    })
    return _safe_json(result)


@app.post("/reject")
async def reject(request: Request):
    session = _check_session(request)
    if not session:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    body     = await request.json()
    question = body.get("question", "")
    sql      = body.get("sql", "")[:120]
    logger.info(f"AGENT REJECT — {question[:80]!r}  sql={sql!r}")
    audit_logger.log_action(session["username"], "query_agent_rejected", {
        "question": question,
        "sql":      sql,
    })
    return JSONResponse({"status": "rejected"})


@app.post("/feedback")
async def feedback(request: Request):
    """Record user thumbs-up / thumbs-down on an AI response.

    Also flags or confirms the matching approved-query entry so future
    similarity lookups skip bad SQL and favour confirmed-good SQL.
    """
    session = _check_session(request)
    if not session:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    body     = await request.json()
    rating   = body.get("rating", "")          # "positive" | "negative"
    question = body.get("question", "").strip()
    sql      = body.get("sql", "").strip()
    answer   = body.get("answer", "").strip()
    comment  = body.get("comment", "").strip()
    tables_used    = body.get("tables_used", [])
    time_ms        = body.get("response_time_ms", 0)
    was_cached     = bool(body.get("was_cached", False))

    if rating not in ("positive", "negative"):
        return JSONResponse({"error": "rating must be 'positive' or 'negative'"}, status_code=400)

    entry = {
        "timestamp":        datetime.utcnow().isoformat(),
        "username":         session["username"],
        "question":         question,
        "sql":              sql[:500],
        "tables_used":      tables_used,
        "answer_preview":   answer[:300],
        "rating":           rating,
        "comment":          comment,
        "response_time_ms": time_ms,
        "was_cached":       was_cached,
    }

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, feedback_logger.append, entry)

    # Mutate approved_queries.jsonl based on feedback
    if sql and question:
        if rating == "negative":
            await loop.run_in_executor(None, approved_queries.flag_entry, question, sql)
        else:
            await loop.run_in_executor(None, approved_queries.confirm_entry, question, sql)

    # Audit trail
    audit_logger.log_action(session["username"],
        "feedback_positive" if rating == "positive" else "feedback_negative",
        {"question": question[:200], "comment": comment},
    )

    return JSONResponse({"status": "ok"})


# ── Admin: audit viewer ────────────────────────────────────────────────────────

@app.get("/admin/audit", response_class=HTMLResponse)
async def admin_audit(request: Request):
    session = _check_session(request)
    if not session:
        return RedirectResponse("/login", status_code=302)

    user_obj = auth.find_user(session["username"])
    if not user_obj or user_obj.get("role") != "admin":
        return HTMLResponse("<h3>403 Forbidden — admin access required.</h3>", status_code=403)

    entries = audit_logger.read_entries(limit=200)
    return templates.TemplateResponse(
        "audit.html",
        {"request": request, "entries": entries},
    )


# ── Admin: feedback dashboard ─────────────────────────────────────────────────

@app.get("/admin/feedback", response_class=HTMLResponse)
async def admin_feedback_page(request: Request):
    session = _check_session(request)
    if not session:
        return RedirectResponse("/login", status_code=302)

    user_obj = auth.find_user(session["username"])
    if not user_obj or user_obj.get("role") != "admin":
        return HTMLResponse("<h3>403 Forbidden — admin access required.</h3>", status_code=403)

    entries  = feedback_logger.read_entries(limit=500)
    total    = len(entries)
    positive = sum(1 for e in entries if e.get("rating") == "positive")
    negative = sum(1 for e in entries if e.get("rating") == "negative")
    rated    = positive + negative
    accuracy = round(100 * positive / rated, 1) if rated else None

    negatives = [e for e in entries if e.get("rating") == "negative"]

    return templates.TemplateResponse("feedback.html", {
        "request":   request,
        "total":     total,
        "positive":  positive,
        "negative":  negative,
        "accuracy":  accuracy,
        "negatives": negatives,
    })


# ── Admin: settings ────────────────────────────────────────────────────────────

@app.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings(request: Request):
    session = _check_session(request)
    if not session:
        return RedirectResponse("/login", status_code=302)

    user_obj = auth.find_user(session["username"])
    if not user_obj or user_obj.get("role") != "admin":
        return HTMLResponse("<h3>403 Forbidden — admin access required.</h3>", status_code=403)

    # ── Gather config — never include raw secrets ──────────────────────────
    ai_cfg = load_ai_config()

    from config.loader import load_db_config as _ldc
    db_cfg = _ldc()

    # Count approved queries (line count of JSONL file)
    aq_path  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "approved_queries.jsonl")
    aq_count = 0
    if os.path.exists(aq_path):
        try:
            with open(aq_path, encoding="utf-8") as f:
                aq_count = sum(1 for line in f if line.strip())
        except OSError:
            pass

    uptime_s = int(time.time() - _START_TIME)
    hours, rem = divmod(uptime_s, 3600)
    minutes, seconds = divmod(rem, 60)
    uptime_str = f"{hours}h {minutes}m {seconds}s"

    security = load_security_config()

    company_md_exists = os.path.exists(_COMPANY_MD_PATH)

    schema_meta = load_schema_meta()
    schema_last_refreshed = schema_meta.get("last_refreshed", "")
    if schema_last_refreshed:
        try:
            schema_last_refreshed = schema_last_refreshed[:19].replace("T", " ") + " UTC"
        except Exception:
            pass

    ctx = {
        "request":                request,
        "db_server":              db_cfg.get("server")   or "(not configured)",
        "db_name":                db_cfg.get("database") or "(not configured)",
        "db_user":                db_cfg.get("user")     or "(not configured)",
        # AI provider
        "ai_provider":            ai_cfg.get("provider", "anthropic"),
        "ai_model":               ai_cfg.get("model", "—"),
        "ai_key_hint":            ai_cfg.get("api_key_hint", ""),
        "local_enabled":          ai_cfg.get("local_enabled", False),
        "local_model":            ai_cfg.get("local_model", ""),
        "local_endpoint":         ai_cfg.get("local_endpoint", ""),
        # Schema metadata
        "schema_table_count":     schema_meta.get("table_count"),
        "schema_column_count":    schema_meta.get("total_columns"),
        "schema_last_refreshed":  schema_last_refreshed or None,
        # Runtime
        "cache_size":             query_cache.size(),
        "approved_count":         aq_count,
        "uptime":                 uptime_str,
        "security":               security,
        "company_md_exists":      company_md_exists,
    }
    return templates.TemplateResponse("settings.html", ctx)


# ── Admin: update AI config ───────────────────────────────────────────────────

@app.post("/admin/update-ai-config")
async def admin_update_ai_config(request: Request):
    """Update AI provider configuration. Tests the key before saving."""
    session = _check_session(request)
    if not session:
        return _safe_json({"success": False, "error": "Not authenticated."})

    user_obj = auth.find_user(session["username"])
    if not user_obj or user_obj.get("role") != "admin":
        return _safe_json({"success": False, "error": "Admin access required."})

    body = await request.json()
    provider        = body.get("provider", "anthropic").strip()
    api_key         = body.get("api_key", "").strip()
    model           = body.get("model", "").strip()
    custom_endpoint = body.get("custom_endpoint", "").strip()

    if not api_key:
        return _safe_json({"success": False, "error": "API key is required."})
    if not model:
        return _safe_json({"success": False, "error": "Model name is required."})

    # Test the key before saving
    loop = asyncio.get_event_loop()
    test_result = await loop.run_in_executor(
        None, test_ai_connection, provider, api_key, model, custom_endpoint
    )
    if not test_result["success"]:
        return _safe_json({"success": False, "error": f"Key verification failed: {test_result.get('error')}"})

    # Preserve local provider settings
    current = load_ai_config()
    data = {
        "provider":        provider,
        "api_key":         api_key,
        "model":           model,
        "custom_endpoint": custom_endpoint,
        "local_enabled":   current.get("local_enabled", False),
        "local_endpoint":  current.get("local_endpoint", "http://localhost:11434"),
        "local_model":     current.get("local_model", "qwen3:8b"),
    }
    try:
        await loop.run_in_executor(None, save_ai_config, data)
        actor = session["username"]
        audit_logger.log_action(actor, "update_ai_config", {"provider": provider, "model": model})
        return _safe_json({"success": True})
    except Exception as e:
        logger.error(f"update-ai-config error: {e}")
        return _safe_json({"success": False, "error": str(e)})


# ── Admin: company knowledge editor ──────────────────────────────────────────

@app.get("/admin/company", response_class=HTMLResponse)
async def admin_company_get(request: Request):
    session = _check_session(request)
    if not session:
        return RedirectResponse("/login", status_code=302)

    user_obj = auth.find_user(session["username"])
    if not user_obj or user_obj.get("role") != "admin":
        return HTMLResponse("<h3>403 Forbidden — admin access required.</h3>", status_code=403)

    content = ""
    if os.path.exists(_COMPANY_MD_PATH):
        try:
            with open(_COMPANY_MD_PATH, encoding="utf-8") as f:
                content = f.read()
        except OSError:
            pass

    return templates.TemplateResponse(
        "company_editor.html",
        {"request": request, "content": content},
    )


@app.post("/admin/company")
async def admin_company_post(request: Request):
    session = _check_session(request)
    if not session:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    user_obj = auth.find_user(session["username"])
    if not user_obj or user_obj.get("role") != "admin":
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    body    = await request.json()
    content = body.get("content", "").strip()
    try:
        os.makedirs(os.path.dirname(_COMPANY_MD_PATH), exist_ok=True)
        with open(_COMPANY_MD_PATH, "w", encoding="utf-8") as f:
            f.write(content)
        audit_logger.log_action(session["username"], "company_knowledge_updated", {"chars": len(content)})
        return _safe_json({"success": True})
    except Exception as e:
        logger.error(f"admin/company save error: {e}")
        return _safe_json({"success": False, "error": str(e)})


# ── Admin: reset ───────────────────────────────────────────────────────────────

@app.post("/admin/reset")
async def admin_reset(request: Request):
    """Wipe all generated config, schema, and logs so the setup wizard reruns.

    Requires admin session + correct admin password in request body.
    Deletes everything inside config/, prompts/, logs/ except .gitkeep files.
    Source code and templates are never touched.
    """
    session = _check_session(request)
    if not session:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    user_obj = auth.find_user(session["username"])
    if not user_obj or user_obj.get("role") != "admin":
        return JSONResponse({"error": "Admin access required."}, status_code=403)

    body     = await request.json()
    password = body.get("password", "")
    if not auth.verify_password(session["username"], password):
        return _safe_json({"success": False, "error": "Incorrect password."})

    _ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
    deleted: list[str] = []

    def _wipe_dir(dirpath: str, keep_py: bool = False) -> None:
        """Delete all files and subdirectories inside dirpath, leave .gitkeep.

        keep_py=True skips .py and .pyc files (used for config/ so the app
        code files are never deleted by a reset).
        """
        if not os.path.isdir(dirpath):
            return
        for name in os.listdir(dirpath):
            if name == ".gitkeep":
                continue
            if keep_py and (name.endswith(".py") or name.endswith(".pyc") or name == "__pycache__"):
                continue
            full = os.path.join(dirpath, name)
            try:
                if os.path.isdir(full):
                    import shutil
                    shutil.rmtree(full)
                    deleted.append(f"dir  {full}")
                else:
                    os.remove(full)
                    deleted.append(f"file {full}")
            except Exception as e:
                logger.warning(f"Reset: could not remove {full}: {e}")

        # Ensure .gitkeep exists so git keeps the directory
        gk = os.path.join(dirpath, ".gitkeep")
        if not os.path.exists(gk):
            try:
                open(gk, "w").close()
            except Exception:
                pass

    _wipe_dir(os.path.join(_ROOT_DIR, "config"), keep_py=True)  # preserve .py app files
    for subdir in ("prompts", "logs"):
        _wipe_dir(os.path.join(_ROOT_DIR, subdir))

    # Print to console — audit.jsonl has been deleted so console is the only record
    print(f"\n{'='*60}")
    print(f"OPTIFLOW RESET — performed by: {session['username']}")
    print(f"Deleted {len(deleted)} items:")
    for item in deleted:
        print(f"  {item}")
    print(f"{'='*60}\n")
    logger.warning(
        f"RESET COMPLETE — user='{session['username']}'  "
        f"items_deleted={len(deleted)}"
    )

    # Invalidate the current user's session so they're shown the setup wizard
    token = request.cookies.get("session_token")
    if token and token in _sessions:
        del _sessions[token]

    return _safe_json({"success": True, "message": "Reset complete. Redirecting to setup."})


# ── Admin: refresh schema ─────────────────────────────────────────────────────

@app.post("/admin/refresh-schema")
async def admin_refresh_schema(request: Request):
    """Re-discover the database schema without touching users, credentials, or company.md.

    Computes a diff (new tables, removed tables, changed row counts, new columns).
    - Removes per-table files for dropped tables
    - Appends AI-generated sections to company.md for new tables
    - Marks approved queries stale when their tables were removed
    - Clears the query cache
    - Saves schema metadata (table_count, total_columns, last_refreshed)
    - Returns a JSON change summary
    """
    session = _check_session(request)
    if not session:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    user_obj = auth.find_user(session["username"])
    if not user_obj or user_obj.get("role") != "admin":
        return JSONResponse({"error": "Admin access required."}, status_code=403)

    _ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

    # ── 1. Load old schema state before we overwrite anything ─────────────────
    old_state = load_old_schema_state()   # {table_name: {row_count, columns:[]}}

    # ── 2. Connect to database ────────────────────────────────────────────────
    from config.loader import load_db_config as _ldc
    db_cfg = _ldc()
    server   = db_cfg.get("server")
    database = db_cfg.get("database")
    if not server or not database:
        return _safe_json({"success": False, "error": "Database not configured. Run setup first."})

    try:
        conn = get_db_connection(server, database, db_cfg.get("user"), db_cfg.get("password"))
    except Exception as e:
        return _safe_json({"success": False, "error": f"Could not connect to database: {e}"})

    # ── 3. Run schema discovery (rewrites schema_context.txt + split files) ───
    try:
        result = run_schema_discovery(conn, database, server)
        conn.close()
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        return _safe_json({"success": False, "error": f"Schema discovery failed: {e}"})

    tables_data  = result["tables"]
    new_state    = {t["name"]: t for t in tables_data}
    old_names    = set(old_state.keys())
    new_names    = set(new_state.keys())

    # ── 4. Compute diff ───────────────────────────────────────────────────────
    added_tables   = sorted(new_names - old_names)
    removed_tables = sorted(old_names - new_names)
    changed_tables: list[dict] = []

    for name in sorted(new_names & old_names):
        changes: list[str] = []
        old = old_state[name]
        new = new_state[name]

        # Row count change ≥ 10%
        old_rows = old.get("row_count", 0)
        new_rows = new.get("row_count", 0)
        if old_rows != new_rows:
            changes.append(f"rows: {old_rows:,} → {new_rows:,}")

        # New columns
        old_cols = set(old.get("columns", []))
        new_cols = {c["name"] for c in new.get("columns", [])}
        added_cols   = sorted(new_cols - old_cols)
        removed_cols = sorted(old_cols - new_cols)
        if added_cols:
            changes.append(f"new columns: {', '.join(added_cols)}")
        if removed_cols:
            changes.append(f"removed columns: {', '.join(removed_cols)}")

        if changes:
            changed_tables.append({"table": name, "changes": changes})

    # ── 5. Delete per-table files for removed tables ──────────────────────────
    tables_dir = os.path.join(_ROOT_DIR, "prompts", "tables")
    for tname in removed_tables:
        safe = re.sub(r"[^A-Za-z0-9_\-]", "_", tname)
        fpath = os.path.join(tables_dir, f"{safe}.txt")
        if os.path.exists(fpath):
            try:
                os.remove(fpath)
            except Exception:
                pass

    # ── 6. AI: append new table sections to company.md ───────────────────────
    new_tables_appended: list[str] = []
    if added_tables:
        try:
            # Build a mini-schema snippet for the new tables only
            new_schema_lines: list[str] = []
            for tname in added_tables:
                t = new_state[tname]
                col_list = ", ".join(c["name"] for c in t.get("columns", [])[:20])
                new_schema_lines.append(
                    f"TABLE: {tname} ({t.get('row_count', 0):,} rows)\nColumns: {col_list}"
                )
            new_schema_text = "\n\n".join(new_schema_lines)

            new_table_system = (
                "You are a business analyst. Given schema snippets for newly added database tables, "
                "write a knowledge section for each table in this exact format:\n\n"
                "## <TableName>\n"
                "**What it stores:** <one sentence>\n"
                "**Use when asked about:** <comma-separated list of business questions this table answers>\n"
                "**Key columns:** <2-4 most important column names>\n\n"
                "Output all tables one after another with no extra commentary."
            )
            new_sections = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: get_completion(new_table_system, new_schema_text, max_tokens=1500)
                ),
                timeout=30.0,
            )
            if new_sections and os.path.exists(_COMPANY_MD_PATH):
                with open(_COMPANY_MD_PATH, "a", encoding="utf-8") as f:
                    f.write(f"\n\n---\n*New tables added during schema refresh ({datetime.utcnow().strftime('%Y-%m-%d')})*\n\n")
                    f.write(new_sections.strip())
                new_tables_appended = added_tables
        except Exception as exc:
            logger.warning(f"refresh-schema: could not generate company.md sections: {exc}")

    # ── 7. Append note about removed tables to company.md ────────────────────
    if removed_tables and os.path.exists(_COMPANY_MD_PATH):
        try:
            with open(_COMPANY_MD_PATH, "a", encoding="utf-8") as f:
                f.write(
                    f"\n\n---\n*Tables removed during schema refresh ({datetime.utcnow().strftime('%Y-%m-%d')}): "
                    f"{', '.join(removed_tables)}*\n"
                )
        except Exception:
            pass

    # ── 8. Mark stale approved queries ───────────────────────────────────────
    stale_count = approved_queries.mark_stale(set(removed_tables)) if removed_tables else 0

    # ── 9. Clear query cache ──────────────────────────────────────────────────
    query_cache.clear()

    # ── 10. Save schema metadata ──────────────────────────────────────────────
    history_entry = {
        "added_tables":   added_tables,
        "removed_tables": removed_tables,
        "changed_tables": [c["table"] for c in changed_tables],
        "stale_queries":  stale_count,
    }
    save_schema_meta(tables_data, history_entry)

    # Regenerate suggested questions to reflect updated schema (best-effort)
    try:
        qs = _generate_suggested_questions()
        if qs:
            _save_suggested_questions(qs)
    except Exception as _exc:
        logger.warning(f"refresh-schema: suggested questions regeneration skipped: {_exc}")

    # ── 11. Audit log ─────────────────────────────────────────────────────────
    audit_logger.log(
        username=session["username"],
        action="refresh_schema",
        detail=(
            f"added={len(added_tables)} removed={len(removed_tables)} "
            f"changed={len(changed_tables)} stale_queries={stale_count}"
        ),
    )

    change_summary = {
        "added_tables":          added_tables,
        "removed_tables":        removed_tables,
        "changed_tables":        changed_tables,
        "new_company_sections":  new_tables_appended,
        "stale_queries_marked":  stale_count,
        "total_tables":          len(tables_data),
        "cache_cleared":         True,
    }

    return _safe_json({"success": True, "changes": change_summary})

