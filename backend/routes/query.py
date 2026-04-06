"""
Query routes — GET /, POST /ask, POST /approve, POST /reject.

No auth. The app opens directly to chat or setup wizard.
"""

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.ai.client import RateLimitExhausted
from backend.services.pipeline import (
    run_pipeline, run_agent_approval, run_chain_approval, get_db_access_level,
)
from backend.services.schema_manager import is_setup_complete
from backend.templates import templates
from backend.utils import safe_json

router = APIRouter()
logger = logging.getLogger(__name__)

PIPELINE_TIMEOUT = 30
AGENT_TIMEOUT    = 30
CHAIN_TIMEOUT    = 90


@router.get("/")
async def index(request: Request):
    if not is_setup_complete():
        return templates.TemplateResponse("pages/setup.html", {"request": request})

    db_warning: str | None = None
    level = get_db_access_level()
    if level == "warning":
        db_warning = "Database user has write permissions. Contact your admin to use a read-only user."
    elif level == "unknown":
        db_warning = "Database user permissions could not be verified. Ensure this is a read-only user."

    return templates.TemplateResponse(
        "pages/chat.html",
        {"request": request, "db_warning": db_warning},
    )


@router.post("/ask")
async def ask(request: Request):
    body = await request.json()
    question = body.get("question", "").strip()

    if not question:
        return JSONResponse(
            {"mode": "error", "answer": "Please ask a question.", "time_ms": 0}
        )

    logger.info(f"Question: {question!r}")
    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, run_pipeline, question),
            timeout=PIPELINE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error(f"Pipeline timeout after {PIPELINE_TIMEOUT}s")
        result = {
            "mode": "error",
            "answer": "That took too long. The database might be slow right now. Please try again in a moment.",
            "time_ms": PIPELINE_TIMEOUT * 1000,
        }
    except RateLimitExhausted as rl:
        logger.warning(f"Rate limit during /ask — retry_after={rl.retry_after}s")
        return JSONResponse({"rate_limited": True, "retry_after": rl.retry_after})

    return safe_json(result)


@router.post("/approve")
async def approve(request: Request):
    body       = await request.json()
    agent_type = body.get("agent_type", "single")
    question   = body.get("question", "").strip()
    loop       = asyncio.get_event_loop()

    if agent_type in ("chain", "deep_dive"):
        steps          = body.get("steps", [])
        summary_prompt = body.get("summary_prompt", "")
        entity_label   = body.get("entity_label", "")

        if not steps:
            return JSONResponse({"error": "No steps provided."}, status_code=400)

        logger.info(f"CHAIN APPROVE ({agent_type}) — {question[:80]!r}  steps={len(steps)}")
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, run_chain_approval, question, steps, summary_prompt, agent_type, entity_label),
                timeout=CHAIN_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error(f"Chain execution timeout after {CHAIN_TIMEOUT}s")
            result = {"answer": f"Query chain timed out after {CHAIN_TIMEOUT} seconds.", "step_results": [], "total_rows": 0, "time_ms": CHAIN_TIMEOUT * 1000}
        except RateLimitExhausted as rl:
            return JSONResponse({"rate_limited": True, "retry_after": rl.retry_after})
        return safe_json(result)

    sql         = body.get("sql", "").strip()
    tables_used = body.get("tables_used", [])

    if not sql:
        return JSONResponse({"error": "No SQL provided."}, status_code=400)

    logger.info(f"AGENT APPROVE — {question[:80]!r}")
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, run_agent_approval, question, sql, tables_used),
            timeout=AGENT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error(f"Agent execution timeout after {AGENT_TIMEOUT}s")
        result = {"answer": "Query timed out after 30 seconds. Try a more specific question.", "rows_returned": 0, "time_ms": AGENT_TIMEOUT * 1000}
    except RateLimitExhausted as rl:
        return JSONResponse({"rate_limited": True, "retry_after": rl.retry_after})

    return safe_json(result)


@router.post("/reject")
async def reject(request: Request):
    body     = await request.json()
    question = body.get("question", "")
    sql      = body.get("sql", "")[:120]
    logger.info(f"AGENT REJECT — {question[:80]!r}  sql={sql!r}")
    return JSONResponse({"status": "rejected"})
