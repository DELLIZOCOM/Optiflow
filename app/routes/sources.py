"""
Sources management routes.

GET    /sources                   — list all connected sources
POST   /sources                   — add a new source (validates + discovers schema)
GET    /sources/{name}            — get source details
DELETE /sources/{name}            — remove a source
POST   /sources/{name}/rediscover — re-run schema discovery
"""

import asyncio
import logging
import shutil
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import (
    load_source_configs,
    save_source_config,
    delete_source_config,
    SOURCES_DATA_DIR,
)
from app.utils.helpers import safe_json

router = APIRouter(prefix="/sources")
logger = logging.getLogger(__name__)

# Injected by main.py after startup
_source_registry = None
_tool_registry   = None


def init_router(source_registry, tool_registry):
    global _source_registry, _tool_registry
    _source_registry = source_registry
    _tool_registry   = tool_registry


def _source_summary(source) -> dict:
    index = source.get_table_index()
    table_count = len([l for l in index.splitlines() if l.strip()]) if index else 0
    return {
        "name":             source.name,
        "type":             source.source_type,
        "description":      source.description,
        "database":         source.get_database_name(),
        "table_count":      table_count,
        "schema_discovered": source.schema_discovered(),
    }


@router.get("")
async def list_sources():
    """List all connected sources."""
    if _source_registry is None:
        return safe_json([])
    return safe_json([_source_summary(s) for s in _source_registry.get_all()])


@router.get("/{name}")
async def get_source(name: str):
    if _source_registry is None:
        return JSONResponse({"error": "Registry not initialised"}, status_code=503)
    source = _source_registry.get(name)
    if not source:
        return JSONResponse({"error": f"Source '{name}' not found"}, status_code=404)
    return safe_json(_source_summary(source))


@router.delete("/{name}")
async def delete_source(name: str):
    """Remove a source — deletes config and schema data directory."""
    if _source_registry is None:
        return JSONResponse({"error": "Registry not initialised"}, status_code=503)

    if not _source_registry.get(name):
        return JSONResponse({"error": f"Source '{name}' not found"}, status_code=404)

    # Remove from registry
    _source_registry.remove(name)

    # Delete config file
    delete_source_config(name)

    # Delete schema data directory
    data_dir = SOURCES_DATA_DIR / name
    if data_dir.exists():
        try:
            shutil.rmtree(data_dir)
            logger.info(f"Deleted schema data dir: data/sources/{name}")
        except Exception as e:
            logger.warning(f"Could not delete data dir for '{name}': {e}")

    return safe_json({"success": True, "deleted": name})


@router.post("/{name}/rediscover")
async def rediscover_source(name: str):
    """Re-run schema discovery for an existing source."""
    if _source_registry is None:
        return JSONResponse({"error": "Registry not initialised"}, status_code=503)

    source = _source_registry.get(name)
    if not source:
        return JSONResponse({"error": f"Source '{name}' not found"}, status_code=404)

    loop = asyncio.get_event_loop()

    def _do_discover():
        # Invalidate stale cache before writing new schema files
        if hasattr(source, "invalidate_cache"):
            source.invalidate_cache()
        conn, driver, error = source.connect()
        if not conn:
            return {"success": False, "error": error}
        try:
            result = source.discover_schema(conn, source.get_database_name(), source._server)
            # Reload cache from newly written files
            if hasattr(source, "load_cache"):
                source.load_cache()
            return {"success": True, "tables": len(result.get("tables", []))}
        finally:
            try:
                conn.close()
            except Exception:
                pass

    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _do_discover), timeout=300
        )
        return safe_json(result)
    except asyncio.TimeoutError:
        return safe_json({"success": False, "error": "Schema discovery timed out (>300s)."})
    except Exception as e:
        logger.error(f"Rediscover error for '{name}': {e}")
        return safe_json({"success": False, "error": str(e)})
