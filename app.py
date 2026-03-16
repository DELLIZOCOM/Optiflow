"""
OptiFlow AI — FastAPI server.

GET  /        → serves the chat UI
POST /ask     → template mode: parse intent → query → format
               agent mode (single):    generate SQL → return for approval
               agent mode (chain):     generate multi-step SQL → return for approval
               agent mode (deep_dive): pre-built SQL chain for project/customer
POST /approve → execute approved SQL (single, chain, or deep_dive) → interpret via Claude
POST /reject  → log user rejection of generated SQL
"""

import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from decimal import Decimal

import anthropic
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from config import settings
from config.settings import ANTHROPIC_API_KEY
from core.agent_sql_generator import generate_chain, generate_deep_dive
from core.db import execute_query
from core.filter_injector import inject_filters
if settings.INTENT_PARSER_MODE == "local":
    from core.local_intent_parser import parse
else:
    from core.intent_parser import parse
from core.query_engine import run
from core.response_formatter import format_response, format_business_health, format_welcome

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

_anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


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

_WELCOME_INTENTS = {
    "invoice_aging":  {"intent": "invoice_aging"},
    "amc_expiry":     {"intent": "amc_expiry", "days": 30},
    "projects_stuck": {"intent": "projects_stuck", "days": 90},
    "tickets_open":   {"intent": "tickets_open"},
}


def _run_one_intent(name_and_dict):
    name, intent_dict = name_and_dict
    try:
        return name, run(intent_dict)
    except Exception as e:
        logger.error(f"Welcome sub-intent '{name}' failed: {e}")
        return name, {"rows": [], "error": str(e)}


def _run_welcome() -> dict:
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = dict(pool.map(_run_one_intent, _WELCOME_INTENTS.items()))
    message = format_welcome(results)
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
    response = _anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        temperature=0,
        system=(
            "You are a business analyst for Ecosoft Zolutions' BizFlow ERP system. "
            "Interpret database query results for a non-technical manager in 3-5 sentences. "
            "Use Indian currency format (Rs, lakhs, crores). "
            "Lead with the key insight. Highlight anything unusual or actionable. "
            "Do not repeat the question back. Tell them what the data means, not what it contains."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"The user asked: {question}\n\n"
                f"{row_note}"
                f"Query results ({len(display_rows)} rows shown):\n{rows_json}"
            ),
        }],
    )
    return response.content[0].text.strip()


def _run_agent_approval(question: str, sql: str, tables_used: list) -> dict:
    """Execute approved agent SQL and interpret results via Claude."""
    t0 = time.perf_counter()

    # Safety net: inject mandatory data quality filters for each referenced table
    for table in tables_used:
        sql = inject_filters(sql, table)

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
    total_rows = 0
    for sr in step_results:
        step_num = sr.get("step", "?")
        explanation = sr.get("explanation", "")
        rows = sr.get("rows", [])
        total_rows += len(rows)
        rows_json = json.dumps(rows[:50], default=str)
        parts.append(
            f"=== Step {step_num}: {explanation} ({len(rows)} rows) ===\n{rows_json}"
        )

    combined = "\n\n".join(parts)
    context = f"The user asked: {question}\n\n{combined}" if question else combined

    response = _anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=700,
        temperature=0,
        system=(
            "You are a business analyst for Ecosoft Zolutions' BizFlow ERP system. "
            "Interpret multi-step database query results for a non-technical manager. "
            "Use Indian currency format (Rs, lakhs, crores). "
            "Lead with the most important insight. Be concise — 4-7 sentences. "
            "Highlight anything unusual, actionable, or concerning. "
            "Do not describe the data structure — tell the manager what it means."
        ),
        messages=[{
            "role": "user",
            "content": f"{summary_prompt}\n\n{context}",
        }],
    )
    return response.content[0].text.strip()


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

        # Apply safety filters
        for table in tables:
            sql = inject_filters(sql, table)

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
    """Parse intent → route to template or agent mode."""
    t0 = time.perf_counter()

    intent_dict = parse(question)
    intent_name       = intent_dict.get("intent", "unknown")
    match_confidence  = intent_dict.get("match_confidence", "low")
    logger.info(f"Parsed intent: {intent_dict}")

    # Parser-level error (API failure, parse failure, etc.)
    if intent_dict.get("error"):
        elapsed = int((time.perf_counter() - t0) * 1000)
        return {
            "mode": "template",
            "answer": (
                "I couldn't understand that question. "
                "Try asking about projects, invoices, AMC contracts, "
                "operations, or tickets."
            ),
            "intent": intent_name,
            "time_ms": elapsed,
        }

    # ── Deep Dive Mode ───────────────────────────────────────────────────
    if intent_name == "deep_dive":
        entity_type = intent_dict.get("entity_type", "")
        entity_id   = intent_dict.get("entity_id", "")
        entity_name = intent_dict.get("entity_name", "")
        logger.info(
            f"Routing to Deep Dive — type={entity_type!r}  "
            f"id={entity_id!r}  name={entity_name!r}"
        )
        dive = generate_deep_dive(entity_type, entity_id, entity_name)
        elapsed = int((time.perf_counter() - t0) * 1000)
        return {
            "mode": "deep_dive",
            "entity_type": dive["entity_type"],
            "entity_label": dive["entity_label"],
            "steps": dive["steps"],
            "summary_prompt": dive["summary_prompt"],
            "confidence": dive["confidence"],
            "warnings": dive["warnings"],
            "requires_approval": True,
            "time_ms": elapsed,
        }

    # ── Agent Mode ───────────────────────────────────────────────────────
    # Route to agent when:
    #   - intent is unknown (no template exists), OR
    #   - match_confidence is "medium" (template returns data but can't fully
    #     answer a prediction / trend / analysis / comparison question), OR
    #   - match_confidence is "low" (cross-domain, aggregation templates lack)
    if intent_name == "unknown" or match_confidence != "high":
        logger.info(
            f"Routing to Agent Mode — intent={intent_name!r}  "
            f"match_confidence={match_confidence!r}"
        )
        agent = generate_chain(question)
        elapsed = int((time.perf_counter() - t0) * 1000)

        if agent["mode"] == "chain":
            logger.info(
                f"CHAIN GENERATED: steps={len(agent['steps'])}  "
                f"confidence={agent['confidence']}  ({elapsed}ms)"
            )
            return {
                "mode": "chain",
                "steps": agent["steps"],
                "summary_prompt": agent["summary_prompt"],
                "confidence": agent["confidence"],
                "warnings": agent["warnings"],
                "requires_approval": True,
                "time_ms": elapsed,
            }
        else:
            # Single SQL
            logger.info(
                f"AGENT GENERATED: confidence={agent['confidence']}  "
                f"tables={agent.get('tables_used', [])}  ({elapsed}ms)"
            )
            return {
                "mode": "agent",
                "sql": agent["sql"],
                "explanation": agent["explanation"],
                "tables_used": agent.get("tables_used", []),
                "confidence": agent["confidence"],
                "warnings": agent["warnings"],
                "requires_approval": True,
                "time_ms": elapsed,
            }

    # ── Template Mode — known intent ─────────────────────────────────────
    result = run(intent_dict)

    if result.get("fallback"):
        elapsed = int((time.perf_counter() - t0) * 1000)
        suggestions = result.get("suggestions", [])
        answer = result.get("message", "I don't understand that question.")
        if suggestions:
            answer += "\n\nTry one of these:\n" + "\n".join(f"- {s}" for s in suggestions)
        return {"mode": "template", "answer": answer, "intent": intent_name, "time_ms": elapsed}

    if result.get("meta"):
        answer = format_business_health(result["sub_results"])
    else:
        answer = format_response(
            rows=result["rows"],
            intent_name=result["intent_name"],
            params_used=result["params_used"],
            caveats=result["caveats"],
            redirected_from=result.get("redirected_from"),
        )

    elapsed = int((time.perf_counter() - t0) * 1000)
    logger.info(f"TEMPLATE: intent={result['intent_name']}  time={elapsed}ms")
    return {
        "mode": "template",
        "answer": answer,
        "intent": result["intent_name"],
        "time_ms": elapsed,
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("chat.html", {"request": request})


@app.get("/welcome")
async def welcome():
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
                "Hello! I can help you with BizFlow data — projects, "
                "invoices, AMC contracts, operations, and tickets. "
                "Ask me anything or pick a question below.\n\n"
                "I can also answer custom questions about your data — "
                "I'll show you my work before running anything."
            ),
            "time_ms": WELCOME_TIMEOUT * 1000,
        }
    return _safe_json(result)


@app.post("/ask")
async def ask(request: Request):
    body = await request.json()
    question = body.get("question", "").strip()

    if not question:
        return JSONResponse(
            {"mode": "template", "answer": "Please ask a question.", "intent": None, "time_ms": 0}
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
            "mode": "template",
            "answer": (
                "That took too long. The database might be slow right now. "
                "Please try again in a moment."
            ),
            "intent": None,
            "time_ms": PIPELINE_TIMEOUT * 1000,
        }

    return _safe_json(result)


@app.post("/approve")
async def approve(request: Request):
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

    return _safe_json(result)


@app.post("/reject")
async def reject(request: Request):
    body     = await request.json()
    question = body.get("question", "")
    sql      = body.get("sql", "")[:120]
    logger.info(f"AGENT REJECT — {question[:80]!r}  sql={sql!r}")
    return JSONResponse({"status": "rejected"})
