"""
Setup wizard routes — /setup/* endpoints.

Step 1 — AI Provider:
  POST /setup/test-ai-provider
  POST /setup/save-ai-config

Step 2 — Add Data Source (repeatable):
  POST /setup/test-connection       — validate DB credentials
  POST /setup/check-permissions     — verify read-only access
  POST /setup/discover-schema       — run full discovery, write data/sources/{name}/
  POST /setup/save-source           — persist source config + reload registries

Step 3 — Business Context:
  POST /setup/generate-company-draft
  POST /setup/company-followup
  POST /setup/save-company-knowledge

Status:
  GET  /setup/status
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import (
    load_ai_config, save_ai_config,
    load_source_configs, save_source_config,
    is_ai_configured, is_setup_complete,
    DATA_DIR, COMPANY_MD_PATH, SOURCES_CONFIG_DIR, SOURCES_DATA_DIR, SECURITY_PATH,
)
from app.ai.client import test_connection as test_ai_connection, get_completion
from app.utils.helpers import safe_json, sanitize_name

router = APIRouter(prefix="/setup")
logger = logging.getLogger(__name__)

# Injected by main.py after startup
_source_registry = None
_tool_registry   = None
_sessions        = None


def init_router(source_registry, tool_registry, sessions=None):
    global _source_registry, _tool_registry, _sessions
    _source_registry = source_registry
    _tool_registry   = tool_registry
    _sessions        = sessions


def _reload_source(config: dict) -> None:
    """Instantiate the right DataSource class and register it."""
    if _source_registry is None:
        return
    from app.sources.database.mssql import MSSQLSource
    from app.sources.database.postgresql import PostgreSQLSource
    from app.sources.database.mysql import MySQLSource

    _type = config.get("type", "").lower()
    name  = config["name"]

    cls_map = {"mssql": MSSQLSource, "postgresql": PostgreSQLSource, "mysql": MySQLSource}
    cls = cls_map.get(_type)
    if cls is None:
        logger.warning(f"Unknown source type '{_type}' for source '{name}' — skipping")
        return

    try:
        source = cls(name, config)
        _source_registry.register(source)
        logger.info(f"Source '{name}' loaded into registry")
    except Exception as e:
        logger.error(f"Failed to load source '{name}': {e}")


# ── Step 1: AI Provider ────────────────────────────────────────────────────────

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
    result = await loop.run_in_executor(
        None, test_ai_connection, provider, api_key, model, custom_endpoint
    )
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
        models = [m.get("name") for m in resp.json().get("models", [])]
        return safe_json({"success": True, "models": models})
    except Exception as e:
        return safe_json({"success": False, "error": f"Cannot reach Ollama at {endpoint}: {e}"})


# ── Step 2: Data Source ────────────────────────────────────────────────────────

@router.post("/test-connection")
async def setup_test_connection(request: Request):
    body = await request.json()
    source_type = body.get("source_type", "mssql").strip().lower()
    server   = body.get("server", "").strip()
    database = body.get("database", "").strip()
    user     = body.get("user", "").strip()
    password = body.get("password", "").strip()

    if not all([server, database, user, password]):
        return safe_json({"success": False, "error": "All fields are required."})

    loop = asyncio.get_event_loop()

    if source_type == "mssql":
        from app.sources.database.mssql import MSSQLSource
        _tmp = MSSQLSource("_tmp", {"type": "mssql", "credentials": {
            "server": server, "database": database, "user": user, "password": password
        }})

        def _connect():
            conn, driver, error = _tmp.connect(server, database, user, password)
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
                return {"success": True, "message": "Connection successful.", "driver": driver}
            return {"success": False, "error": error}

        result = await loop.run_in_executor(None, _connect)
    else:
        result = {"success": False, "error": f"Source type '{source_type}' not yet supported."}

    return safe_json(result)


@router.post("/check-permissions")
async def setup_check_permissions(request: Request):
    body = await request.json()
    source_type = body.get("source_type", "mssql").strip().lower()
    server   = body.get("server", "").strip()
    database = body.get("database", "").strip()
    user     = body.get("user", "").strip()
    password = body.get("password", "").strip()

    if not all([server, database, user, password]):
        return safe_json({"success": False, "error": "All fields are required."})

    if source_type != "mssql":
        return safe_json({"success": False, "error": f"Source type '{source_type}' not yet supported."})

    from app.sources.database.mssql import MSSQLSource
    _tmp = MSSQLSource("_tmp", {"type": "mssql", "credentials": {
        "server": server, "database": database, "user": user, "password": password
    }})

    loop = asyncio.get_event_loop()

    def _check():
        conn, _, error = _tmp.connect(server, database, user, password)
        if not conn:
            return {"success": False, "error": error}
        try:
            result = _tmp.verify_readonly_access(conn)
            # Save security config
            import json as _json
            import os
            cfg = {
                "db_user": user, "access_level": result["access_level"],
                "permissions": result["permissions"], "roles": result["roles"],
                "last_checked": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                "setup_warnings": result.get("warnings", []),
            }
            os.makedirs(str(SECURITY_PATH.parent), exist_ok=True)
            with open(SECURITY_PATH, "w", encoding="utf-8") as f:
                _json.dump(cfg, f, indent=2)
            return {"success": True, **result}
        finally:
            try:
                conn.close()
            except Exception:
                pass

    result = await loop.run_in_executor(None, _check)
    return safe_json(result)


@router.post("/discover-schema")
async def setup_discover_schema(request: Request):
    body = await request.json()
    source_type = body.get("source_type", "mssql").strip().lower()
    source_name = body.get("source_name", "").strip()
    server   = body.get("server", "").strip()
    database = body.get("database", "").strip()
    user     = body.get("user", "").strip()
    password = body.get("password", "").strip()

    if not all([server, database, user, password]):
        return safe_json({"success": False, "error": "All fields are required."})

    # Auto-generate source name from database name if not provided
    if not source_name:
        source_name = sanitize_name(database)

    if source_type != "mssql":
        return safe_json({"success": False, "error": f"Source type '{source_type}' not yet supported."})

    from app.sources.database.mssql import MSSQLSource
    _tmp = MSSQLSource(source_name, {"type": "mssql", "credentials": {
        "server": server, "database": database, "user": user, "password": password
    }})

    loop = asyncio.get_event_loop()

    def _discover():
        conn, driver, error = _tmp.connect(server, database, user, password)
        if not conn:
            return {"success": False, "error": error}
        try:
            schema_data = _tmp.discover_schema(conn, database, server)
            return {
                "success":     True,
                "source_name": source_name,
                "db_name":     schema_data["db_name"],
                "server":      schema_data["server"],
                "table_count": len(schema_data["tables"]),
                "tables":      schema_data["tables"],
            }
        finally:
            try:
                conn.close()
            except Exception:
                pass

    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _discover), timeout=300
        )
        # Auto-save source config and register it so generate-company-draft works immediately
        if result.get("success"):
            config = {
                "name":        source_name,
                "type":        source_type,
                "description": f"{database} on {server}",
                "credentials": {
                    "server": server, "database": database,
                    "user": user, "password": password,
                },
                "schema_discovered": True,
            }
            try:
                await loop.run_in_executor(None, save_source_config, config)
                _reload_source(config)
            except Exception as e:
                logger.warning(f"Auto-save after discover-schema failed: {e}")
        return safe_json(result)
    except asyncio.TimeoutError:
        return safe_json({"success": False, "error": "Schema discovery timed out (>300s)."})
    except Exception as e:
        logger.error(f"discover-schema error: {e}")
        return safe_json({"success": False, "error": str(e)})


@router.post("/save-source")
async def setup_save_source(request: Request):
    """Save source config + load it into the live registry (no restart needed)."""
    body = await request.json()
    name        = (body.get("name") or "").strip()
    source_type = (body.get("type") or body.get("source_type") or "mssql").strip().lower()
    description = body.get("description", "").strip()
    credentials = body.get("credentials", {})

    if not name:
        return safe_json({"success": False, "error": "Source name is required."})
    if not credentials:
        return safe_json({"success": False, "error": "Credentials are required."})

    config = {
        "name":        name,
        "type":        source_type,
        "description": description,
        "credentials": credentials,
        "created_at":  datetime.now(timezone.utc).isoformat(),
        "schema_discovered": True,
    }

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, save_source_config, config)
        # Load into live registry
        _reload_source(config)
        return safe_json({"success": True, "name": name})
    except Exception as e:
        logger.error(f"save-source error: {e}")
        return safe_json({"success": False, "error": str(e)})


# ── Step 3: Business Context ───────────────────────────────────────────────────

_COMPANY_DRAFT_SYSTEM = """\
You are a business analyst. Given a database schema, write a comprehensive company.md \
knowledge document that describes:
1. What the company does (inferred from table names and columns)
2. Each table: purpose, when to use it, key relationships
3. Important business terminology and status codes found in the data
4. How different entities relate (e.g. customers → orders → invoices)

Format with ## headings per table. Be specific about column meanings.
Write in plain English. This document will be used by an AI agent to answer business questions."""

_COMPANY_FOLLOWUP_SYSTEM = """\
You are a business analyst reviewing a knowledge document. \
Generate 3-5 targeted follow-up questions to fill gaps in the document. \
Return ONLY a JSON array of strings, no other text. \
Example: ["What does Status='Pending' mean in INVOICE_DETAILS?", ...]"""


@router.post("/generate-company-draft")
async def setup_generate_company_draft(request: Request):
    body    = await request.json()
    db_name = body.get("db_name", "the database")

    # Collect schema from all connected sources
    schema_text = ""
    if _source_registry:
        for source in _source_registry.get_all():
            index = source.get_compact_index()
            if index:
                schema_text += f"\n\n=== Source: {source.name} ({source.get_database_name()}) ===\n{index}"

    # Fallback: scan data/sources/ for discovered schema files (e.g. right after discover-schema)
    if not schema_text:
        from app.config import SOURCES_DATA_DIR
        if SOURCES_DATA_DIR.exists():
            for source_dir in sorted(SOURCES_DATA_DIR.iterdir()):
                if source_dir.is_dir():
                    index_path = source_dir / "schema_index.txt"
                    if index_path.exists():
                        index = index_path.read_text(encoding="utf-8").strip()
                        if index:
                            schema_text += f"\n\n=== Source: {source_dir.name} ===\n{index}"

    if not schema_text:
        return safe_json({"success": False, "error": "No schema found. Discover schema first."})

    try:
        loop = asyncio.get_event_loop()

        def _generate():
            return get_completion(
                system=_COMPANY_DRAFT_SYSTEM,
                user=f"Database name: {db_name}\n{schema_text}",
                max_tokens=4000,
                temperature=0,
            )

        content = await loop.run_in_executor(None, _generate)
        return safe_json({"success": True, "content": content})
    except Exception as e:
        logger.error(f"generate-company-draft error: {e}")
        return safe_json({"success": False, "error": str(e)})


@router.post("/company-followup")
async def setup_company_followup(request: Request):
    body  = await request.json()
    draft = body.get("draft", "").strip()
    if not draft:
        return safe_json({"success": True, "questions": []})

    try:
        loop = asyncio.get_event_loop()

        def _followup():
            return get_completion(
                system=_COMPANY_FOLLOWUP_SYSTEM,
                user=f"Here is the knowledge document:\n\n{draft[:3000]}\n\nGenerate follow-up questions.",
                max_tokens=600,
                temperature=0,
            )

        text = await loop.run_in_executor(None, _followup)
        fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if fence:
            text = fence.group(1)
        questions = json.loads(text)
        if not isinstance(questions, list):
            questions = []
        return safe_json({"success": True, "questions": questions[:5]})
    except Exception as e:
        logger.warning(f"company-followup non-fatal: {e}")
        return safe_json({"success": True, "questions": []})


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


# ── Status ─────────────────────────────────────────────────────────────────────

@router.get("/status")
async def setup_status():
    sources = load_source_configs()
    return safe_json({
        "setup_complete":   is_setup_complete(),
        "ai_configured":    is_ai_configured(),
        "source_count":     len(sources),
        "sources":          [{"name": s.get("name"), "type": s.get("type")} for s in sources],
    })


# ── Reset ──────────────────────────────────────────────────────────────────────

@router.post("/reset")
async def setup_reset():
    """
    Full company reset — deletes all source configs, schema data, knowledge,
    logs, and legacy prompt files. Keeps data/config/app.json (AI provider)
    and data/config/.secret (encryption key).
    """
    import shutil
    from app.config import DATA_DIR, LOGS_DIR

    errors = []

    def _try_delete(path):
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except Exception as e:
            errors.append(f"{path}: {e}")

    # 1. Delete all source config JSONs
    if SOURCES_CONFIG_DIR.exists():
        for f in SOURCES_CONFIG_DIR.glob("*.json"):
            _try_delete(f)

    # 2. Delete all source schema dirs (data/sources/{name}/)
    if SOURCES_DATA_DIR.exists():
        for d in SOURCES_DATA_DIR.iterdir():
            if d.is_dir():
                _try_delete(d)

    # 3. Delete company knowledge
    if COMPANY_MD_PATH.exists():
        _try_delete(COMPANY_MD_PATH)

    # 4. Truncate log files (keep the files, clear the content)
    if LOGS_DIR.exists():
        for log_path in LOGS_DIR.glob("*.jsonl"):
            try:
                log_path.write_text("", encoding="utf-8")
            except Exception as e:
                errors.append(f"{log_path}: {e}")

    # 5. Delete security config
    if SECURITY_PATH.exists():
        _try_delete(SECURITY_PATH)

    # 6. Delete legacy data/prompts/ contents (v1 schema files)
    legacy_prompts = DATA_DIR / "prompts"
    if legacy_prompts.exists() and legacy_prompts.is_dir():
        for item in legacy_prompts.iterdir():
            if item.name == ".gitkeep":
                continue
            _try_delete(item)

    # 7. Clear in-memory SourceRegistry
    if _source_registry:
        for name in list(_source_registry.names()):
            try:
                _source_registry.remove(name)
            except Exception:
                pass

    # 8. Clear in-memory ToolRegistry
    if _tool_registry:
        try:
            _tool_registry.clear()
        except Exception:
            pass

    # 9. Clear all active sessions
    if _sessions:
        try:
            _sessions.clear_all()
        except Exception:
            pass

    if errors:
        logger.warning(f"Reset completed with {len(errors)} error(s): {errors}")
    else:
        logger.info("Reset complete — all company data cleared, AI config kept")

    return safe_json({"success": True, "errors": errors})
