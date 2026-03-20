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
POST /ask     → classify intent → cache → approved log → generate SQL → return for approval
POST /approve → execute approved SQL (single, chain, or deep_dive) → interpret
POST /reject  → log user rejection of generated SQL
GET  /admin/company         → view/edit company knowledge (admin only)
POST /admin/update-ai-config → update AI provider config (admin only)
"""

import asyncio
import json
import logging
import os
import secrets
import sys
import time
from datetime import datetime
from decimal import Decimal

import anthropic
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

import core.auth as auth
from config import settings
from config.loader import load_model_config, load_ai_config, save_ai_config
from config.ai_client import get_completion, test_connection as test_ai_connection
from core.setup_manager import (
    get_db_connection,
    is_setup_complete,
    load_security_config,
    run_schema_discovery,
    save_business_context,
    save_db_credentials,
    save_security_config,
    verify_readonly_access,
)
from core.agent_sql_generator import (
    generate_chain,
    generate_business_health_chain,
    generate_deep_dive_chain,
    _load_company_knowledge,
)
import core.approved_queries as approved_queries
import core.audit_logger as audit_logger
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


def _parse(question: str) -> dict:
    """Dispatch to local or cloud intent parser based on current AI config."""
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

def _interpret_results(question: str, rows: list, total_rows: int) -> str:
    """Call Claude to explain query results in plain English for a manager."""
    display_rows = rows[:100]
    rows_json = json.dumps(display_rows, default=str)
    row_note = (
        f"Note: query returned {total_rows} total rows; only the first 100 are shown.\n\n"
        if total_rows > 100 else ""
    )
    knowledge = _load_company_knowledge()
    knowledge_note = f"\n\nCompany context:\n{knowledge[:600]}" if knowledge else ""

    return get_completion(
        system=(
            "You are a business analyst interpreting database query results for a non-technical manager. "
            "Format your response as clear markdown with:\n"
            "- A bold **key insight** lead sentence\n"
            "- 2-4 bullet points with exact figures from the data\n"
            "- A brief actionable takeaway if the data warrants it\n"
            "Do not repeat the question. Report exact figures — do not round or estimate. "
            "If two values are within 10% of each other, describe them as comparable."
            f"{knowledge_note}"
        ),
        user=(
            f"The user asked: {question}\n\n"
            f"{row_note}"
            f"Query results ({len(display_rows)} rows shown):\n{rows_json}"
        ),
        max_tokens=600,
        temperature=0,
    )


def _run_agent_approval(question: str, sql: str, tables_used: list) -> dict:
    """Execute approved agent SQL and interpret results via Claude."""
    t0 = time.perf_counter()
    original_sql = sql

    logger.info(f"AGENT EXECUTING:\n{sql}")

    try:
        rows = execute_query(sql)
    except Exception as e:
        elapsed = int((time.perf_counter() - t0) * 1000)
        logger.error(f"Agent execution error ({elapsed}ms): {e}")
        return {
            "answer": f"Query execution failed: {e}",
            "rows_returned": 0,
            "time_ms": elapsed,
        }

    total_rows = len(rows)
    logger.info(f"Agent query returned {total_rows} rows")

    try:
        answer = _interpret_results(question, rows, total_rows)
        if total_rows > 100:
            answer = f"Showing first 100 of {total_rows} results.\n\n{answer}"
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
    """Call Claude to synthesise results from multiple SQL steps."""
    parts = []
    for sr in step_results:
        step_num = sr.get("step", "?")
        explanation = sr.get("explanation", "")
        rows = sr.get("rows", [])
        rows_json = json.dumps(rows[:50], default=str)
        parts.append(
            f"=== Step {step_num}: {explanation} ({len(rows)} rows) ===\n{rows_json}"
        )

    combined = "\n\n".join(parts)
    context = f"The user asked: {question}\n\n{combined}" if question else combined

    knowledge = _load_company_knowledge()
    knowledge_note = f"\n\nCompany context:\n{knowledge[:600]}" if knowledge else ""

    return get_completion(
        system=(
            "You are a business analyst interpreting multi-step database query results for a non-technical manager. "
            "Format your response as clear markdown with:\n"
            "- A bold **key insight** lead sentence\n"
            "- Organized sections for each major area (use ### headings if 3+ steps)\n"
            "- Bullet points with exact figures\n"
            "- An **Action Items** section at the end if there are urgent issues\n"
            "Do not describe the data structure — tell the manager what it means. "
            "Report exact figures — do not round or estimate."
            f"{knowledge_note}"
        ),
        user=f"{summary_prompt}\n\n{context}",
        max_tokens=800,
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
        try:
            rows = execute_query(sql)
            logger.info(f"Chain step {step_num}: {len(rows)} rows")
            total_rows += len(rows)
            step_results.append({
                "step": step_num,
                "explanation": explanation,
                "rows": rows,
            })
        except Exception as e:
            logger.error(f"Chain step {step_num} failed: {e}")
            step_results.append({
                "step": step_num,
                "explanation": explanation,
                "rows": [],
                "error": str(e),
            })

    # Interpret combined results
    try:
        answer = _interpret_chain_results(
            question, step_results, summary_prompt, entity_label
        )
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
    """Classify intent → route to appropriate agent chain."""
    t0 = time.perf_counter()

    intent_dict = _parse(question)
    intent_name = intent_dict.get("intent", "unknown")
    logger.info(f"Parsed intent: {intent_dict}")

    # ── Parser-level error (API failure, etc.) ───────────────────────────
    if intent_dict.get("error"):
        elapsed = int((time.perf_counter() - t0) * 1000)
        error_code = intent_dict.get("error", "")
        if error_code == "api_failed":
            answer = (
                "AI service is unavailable right now. "
                "Check that your ANTHROPIC_API_KEY is set in .env and valid, "
                "or that Ollama is running if using local mode."
            )
        else:
            answer = "I couldn't understand that question. Please try rephrasing."
        return {"mode": "error", "answer": answer, "intent": intent_name, "time_ms": elapsed}

    # ── Business Health — dynamic multi-step chain ───────────────────────
    if intent_name == "business_health":
        logger.info("Routing to Business Health chain")
        chain = generate_business_health_chain(question)
        elapsed = int((time.perf_counter() - t0) * 1000)
        return {
            "mode":             "chain",
            "steps":            chain["steps"],
            "summary_prompt":   chain["summary_prompt"],
            "confidence":       chain["confidence"],
            "warnings":         chain["warnings"],
            "from_cache":       False,
            "requires_approval": True,
            "time_ms":          elapsed,
        }

    # ── Deep Dive — dynamic entity investigation chain ───────────────────
    if intent_name == "deep_dive":
        entity_label = intent_dict.get("entity_label", "")
        entity_type  = intent_dict.get("entity_type", "")
        logger.info(f"Routing to Deep Dive — type={entity_type!r}  label={entity_label!r}")
        dive = generate_deep_dive_chain(entity_label, question)
        elapsed = int((time.perf_counter() - t0) * 1000)
        return {
            "mode":             "deep_dive",
            "entity_type":      entity_type,
            "entity_label":     dive["entity_label"],
            "steps":            dive["steps"],
            "summary_prompt":   dive["summary_prompt"],
            "confidence":       dive["confidence"],
            "warnings":         dive["warnings"],
            "requires_approval": True,
            "time_ms":          elapsed,
        }

    # ── Agent Mode (everything else) — cache → approved log → generate ───
    from_cache        = False
    from_approved_log = False

    # 1. In-memory cache (exact question, TTL=1h)
    agent = query_cache.get(question)
    if agent:
        from_cache = True
    else:
        # 2. Approved-query log (similar past question)
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
            # 3. Generate new SQL via Claude
            agent = generate_chain(question)
            if agent.get("sql") or agent.get("steps"):
                query_cache.put(question, agent)

    elapsed = int((time.perf_counter() - t0) * 1000)

    if agent["mode"] == "chain":
        logger.info(
            f"CHAIN {'CACHED' if from_cache else 'GENERATED'}: "
            f"steps={len(agent['steps'])}  confidence={agent['confidence']}  ({elapsed}ms)"
        )
        return {
            "mode":             "chain",
            "steps":            agent["steps"],
            "summary_prompt":   agent["summary_prompt"],
            "confidence":       agent["confidence"],
            "warnings":         agent["warnings"],
            "from_cache":       from_cache,
            "requires_approval": True,
            "time_ms":          elapsed,
        }
    else:
        # Single SQL — check for API failure
        if agent.get("sql") is None and agent.get("confidence") == "none":
            explanation = agent.get("explanation", "")
            if "api" in explanation.lower() or "failed" in explanation.lower():
                logger.error(f"Agent API failure: {explanation}")
                return {
                    "mode":    "error",
                    "answer":  "AI service is unavailable. Check your ANTHROPIC_API_KEY in .env.",
                    "intent":  "unknown",
                    "time_ms": elapsed,
                }
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
        actor = _check_session(request)
        audit_logger.log_action(
            actor["username"] if actor else "setup",
            "setup_completed",
            {
                "database":          database,
                "tables_discovered": len(schema_data.get("tables", [])),
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


_COMPANY_MD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "company.md")


@app.post("/setup/generate-company-draft")
async def setup_generate_company_draft(request: Request):
    """Use Claude to generate an initial company.md draft from the discovered schema."""
    if (err := _setup_auth_check(request)):
        return err
    body           = await request.json()
    db_name        = body.get("db_name", "the database")
    schema_summary = body.get("schema_summary", "").strip()

    if not schema_summary:
        return _safe_json({"success": False, "error": "No schema summary provided."})

    try:
        content = get_completion(
            system=(
                "You are helping set up a business intelligence tool. "
                "Generate a company.md knowledge file in markdown format. "
                "Based on the database schema provided, write helpful sections that explain: "
                "1. A brief business overview (what kind of business this likely is), "
                "2. Key tables and what they mean, "
                "3. Important terminology (based on column/table names), "
                "4. Data quality notes (placeholder for the user to fill in). "
                "Keep it practical and concise. Use ## headings. "
                "Be honest about what you're inferring vs. what's certain — "
                "tell the user to update any guesses. "
                "Do NOT add a title line (the user will see the filename). "
                "Write in plain English, not technical jargon."
            ),
            user=(
                f"Database name: {db_name}\n\n"
                f"Tables discovered:\n{schema_summary}\n\n"
                "Generate the company knowledge file."
            ),
            max_tokens=1200,
            temperature=0,
        )
        return _safe_json({"success": True, "content": content})
    except Exception as e:
        logger.error(f"generate-company-draft error: {e}")
        return _safe_json({"success": False, "error": str(e)})


@app.post("/setup/save-company-knowledge")
async def setup_save_company_knowledge(request: Request):
    """Save company knowledge markdown to config/company.md."""
    if (err := _setup_auth_check(request)):
        return err
    body = await request.json()
    content = body.get("content", "").strip()
    try:
        os.makedirs(os.path.dirname(_COMPANY_MD_PATH), exist_ok=True)
        with open(_COMPANY_MD_PATH, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info("Company knowledge saved to config/company.md")
        return _safe_json({"success": True})
    except Exception as e:
        logger.error(f"save-company-knowledge error: {e}")
        return _safe_json({"success": False, "error": str(e)})


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

    ctx = {
        "request":           request,
        "db_server":         db_cfg.get("server")   or "(not configured)",
        "db_name":           db_cfg.get("database") or "(not configured)",
        "db_user":           db_cfg.get("user")     or "(not configured)",
        # AI provider
        "ai_provider":       ai_cfg.get("provider", "anthropic"),
        "ai_model":          ai_cfg.get("model", "—"),
        "ai_key_hint":       ai_cfg.get("api_key_hint", ""),
        "local_enabled":     ai_cfg.get("local_enabled", False),
        "local_model":       ai_cfg.get("local_model", ""),
        "local_endpoint":    ai_cfg.get("local_endpoint", ""),
        # Runtime
        "cache_size":        query_cache.size(),
        "approved_count":    aq_count,
        "uptime":            uptime_str,
        "security":          security,
        "company_md_exists": company_md_exists,
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
