"""
OptiFlow AI — FastAPI server.

GET  /    → serves the chat UI
POST /ask → runs the full intent → query → format pipeline
"""

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

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

PIPELINE_TIMEOUT = 30  # seconds
WELCOME_TIMEOUT = 15  # seconds

_WELCOME_INTENTS = {
    "invoice_aging": {"intent": "invoice_aging"},
    "amc_expiry": {"intent": "amc_expiry", "days": 30},
    "projects_stuck": {"intent": "projects_stuck", "days": 90},
    "tickets_open": {"intent": "tickets_open"},
}


def _run_one_intent(name_and_dict):
    """Run a single intent. Returns (name, result)."""
    name, intent_dict = name_and_dict
    try:
        return name, run(intent_dict)
    except Exception as e:
        logger.error(f"Welcome sub-intent '{name}' failed: {e}")
        return name, {"rows": [], "error": str(e)}


def _run_welcome() -> dict:
    """Run 4 quick health-check intents in parallel and format a welcome message."""
    t0 = time.perf_counter()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = dict(pool.map(
            _run_one_intent,
            _WELCOME_INTENTS.items(),
        ))

    message = format_welcome(results)
    elapsed = int((time.perf_counter() - t0) * 1000)
    logger.info(f"Welcome message generated in {elapsed}ms")
    return {"message": message, "time_ms": elapsed}


def _run_pipeline(question: str) -> dict:
    """Synchronous pipeline: parse → query → format."""
    t0 = time.perf_counter()

    # 1. Parse intent
    intent_dict = parse(question)
    intent_name = intent_dict.get("intent", "unknown")
    logger.info(f"Parsed intent: {intent_dict}")

    # 2. Handle parser-level failures
    if intent_dict.get("error"):
        elapsed = int((time.perf_counter() - t0) * 1000)
        logger.info(f"Parser error: {intent_dict['error']}  ({elapsed}ms)")
        return {
            "answer": (
                "I couldn't understand that question. "
                "Try asking about projects, invoices, AMC contracts, "
                "operations, or tickets."
            ),
            "intent": intent_name,
            "time_ms": elapsed,
        }

    # 3. Run query
    result = run(intent_dict)

    # 4. Handle fallback (unknown intent)
    if result.get("fallback"):
        elapsed = int((time.perf_counter() - t0) * 1000)
        suggestions = result.get("suggestions", [])
        answer = result.get("message", "I don't understand that question.")
        if suggestions:
            answer += "\n\nTry one of these:\n"
            answer += "\n".join(f"- {s}" for s in suggestions)
        logger.info(f"Fallback returned  ({elapsed}ms)")
        return {"answer": answer, "intent": intent_name, "time_ms": elapsed}

    # 5. Format response
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
    logger.info(
        f"  intent={result['intent_name']}  "
        f"time={elapsed}ms"
    )
    return {
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
                "Ask me anything or pick a question below."
            ),
            "time_ms": WELCOME_TIMEOUT * 1000,
        }
    return JSONResponse(result)


@app.post("/ask")
async def ask(request: Request):
    body = await request.json()
    question = body.get("question", "").strip()

    if not question:
        return JSONResponse(
            {"answer": "Please ask a question.", "intent": None, "time_ms": 0}
        )

    logger.info(f"Question: {question}")

    # Run pipeline with timeout
    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _run_pipeline, question),
            timeout=PIPELINE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error(f"Pipeline timeout after {PIPELINE_TIMEOUT}s")
        result = {
            "answer": (
                "That took too long. The database might be slow right now. "
                "Please try again in a moment."
            ),
            "intent": None,
            "time_ms": PIPELINE_TIMEOUT * 1000,
        }

    return JSONResponse(result)
