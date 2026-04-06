"""
Setup wizard routes — /setup/* endpoints.

No auth. Setup is accessible directly on first run.
"""

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.config.loader import save_ai_config
from backend.ai.client import test_connection as test_ai_connection
from backend.connectors.mssql import get_db_connection, verify_readonly_access, run_schema_discovery
from backend.services.schema_manager import (
    save_db_credentials, save_business_context, save_schema_meta,
    save_security_config,
)
from backend.services.company_builder import generate_company_draft, generate_company_followup
from backend.config.paths import COMPANY_MD_PATH
from backend.utils import safe_json

router = APIRouter(prefix="/setup")
logger = logging.getLogger(__name__)


@router.post("/test-ai-provider")
async def setup_test_ai_provider(request: Request):
    body = await request.json()
    provider        = body.get("provider", "anthropic").strip()
    api_key         = body.get("api_key", "").strip()
    model           = body.get("model", "").strip()
    custom_endpoint = body.get("custom_endpoint", "").strip()

    if not api_key:
        return safe_json({"success": False, "error": "API key is required."})
    if not model:
        return safe_json({"success": False, "error": "Model name is required."})

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, test_ai_connection, provider, api_key, model, custom_endpoint)
    return safe_json(result)


@router.post("/save-ai-config")
async def setup_save_ai_config(request: Request):
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
        return safe_json({"success": False, "error": "API key is required."})
    if not data["model"]:
        return safe_json({"success": False, "error": "Model name is required."})

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, save_ai_config, data)
        return safe_json({"success": True})
    except Exception as e:
        logger.error(f"save-ai-config error: {e}")
        return safe_json({"success": False, "error": str(e)})


@router.post("/test-ollama")
async def setup_test_ollama(request: Request):
    body     = await request.json()
    endpoint = body.get("endpoint", "http://localhost:11434").strip().rstrip("/")

    import requests as _requests
    try:
        resp = _requests.get(f"{endpoint}/api/tags", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        models = [m.get("name") for m in data.get("models", [])]
        return safe_json({"success": True, "models": models})
    except Exception as e:
        return safe_json({"success": False, "error": f"Cannot reach Ollama at {endpoint}: {e}"})


@router.post("/test-connection")
async def setup_test_connection(request: Request):
    body = await request.json()
    server   = body.get("server", "").strip()
    database = body.get("database", "").strip()
    user     = body.get("user", "").strip()
    password = body.get("password", "").strip()

    if not all([server, database, user, password]):
        return safe_json({"success": False, "error": "All fields are required."})

    loop = asyncio.get_event_loop()
    conn, driver, error = await loop.run_in_executor(None, get_db_connection, server, database, user, password)
    if conn:
        conn.close()
        return safe_json({"success": True, "message": "Connection successful."})
    return safe_json({"success": False, "error": error})


@router.post("/check-permissions")
async def setup_check_permissions(request: Request):
    body = await request.json()
    server   = body.get("server", "").strip()
    database = body.get("database", "").strip()
    user     = body.get("user", "").strip()
    password = body.get("password", "").strip()

    if not all([server, database, user, password]):
        return safe_json({"success": False, "error": "All fields are required."})

    loop = asyncio.get_event_loop()
    conn, _, error = await loop.run_in_executor(None, get_db_connection, server, database, user, password)
    if not conn:
        return safe_json({"success": False, "error": error})

    try:
        result = await loop.run_in_executor(None, verify_readonly_access, conn)
    finally:
        try: conn.close()
        except Exception: pass

    try:
        save_security_config(result, user)
    except Exception as e:
        logger.warning(f"Could not save security.json: {e}")

    return safe_json({"success": True, **result})


@router.post("/discover-schema")
async def setup_discover_schema(request: Request):
    body = await request.json()
    server   = body.get("server", "").strip()
    database = body.get("database", "").strip()
    user     = body.get("user", "").strip()
    password = body.get("password", "").strip()

    if not all([server, database, user, password]):
        return safe_json({"success": False, "error": "All fields are required."})

    loop = asyncio.get_event_loop()
    conn, driver, error = await loop.run_in_executor(None, get_db_connection, server, database, user, password)
    if not conn:
        return safe_json({"success": False, "error": error})

    def _discover():
        try:
            schema = run_schema_discovery(conn, database, server)
            save_db_credentials(server, database, user, password)
            return schema
        finally:
            conn.close()

    try:
        schema_data = await asyncio.wait_for(
            loop.run_in_executor(None, _discover), timeout=300,
        )
        tables_data = schema_data.get("tables", [])
        save_schema_meta(tables_data, {"event": "initial_setup", "tables": len(tables_data)})
        return safe_json({"success": True, **schema_data})
    except asyncio.TimeoutError:
        return safe_json({"success": False, "error": "Schema discovery timed out (>300s). Try again."})
    except Exception as e:
        logger.error(f"Schema discovery error: {e}")
        return safe_json({"success": False, "error": str(e)})


@router.post("/generate-company-draft")
async def setup_generate_company_draft(request: Request):
    body    = await request.json()
    db_name = body.get("db_name", "the database")
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, generate_company_draft, db_name)
    return safe_json(result)


@router.post("/company-followup")
async def setup_company_followup(request: Request):
    body  = await request.json()
    draft = body.get("draft", "").strip()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, generate_company_followup, draft)
    return safe_json(result)


@router.post("/save-company-knowledge")
async def setup_save_company_knowledge(request: Request):
    body    = await request.json()
    content = body.get("content", "").strip()
    answers = body.get("followup_answers", [])

    filled = [a for a in answers if isinstance(a, dict) and a.get("answer", "").strip()]
    if filled:
        content += "\n\n## Additional Context\n"
        for a in filled:
            content += f"\n**{a['question']}**\n{a['answer']}\n"

    try:
        COMPANY_MD_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(COMPANY_MD_PATH, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info("Company knowledge saved to data/knowledge/company.md")
        return safe_json({"success": True})
    except Exception as e:
        logger.error(f"save-company-knowledge error: {e}")
        return safe_json({"success": False, "error": str(e)})


@router.get("/status")
async def setup_status():
    from backend.services.schema_manager import is_setup_complete
    return safe_json({"setup_complete": is_setup_complete()})
