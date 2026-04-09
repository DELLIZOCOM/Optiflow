"""OptiFlow AI — application entry point."""

import logging
import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend.routes import setup, query
from backend.services.pipeline import startup_permission_check

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
    datefmt="%H:%M:%S",
)

app = FastAPI(title="OptiFlow AI")

_FRONTEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
app.mount("/static", StaticFiles(directory=_FRONTEND), name="static")

# ── v1 routes (unchanged) ─────────────────────────────────────────────────────
app.include_router(setup.router)
app.include_router(query.router)

# ── v2 Agent routes ───────────────────────────────────────────────────────────
# Wrapped in try/except so a broken agent import never takes down v1.
try:
    from agent import (
        AgentOrchestrator,
        AIClient,
        SessionStore,
        ToolRegistry,
        create_database_tools,
        create_agent_router,
        MSSQLAdapter,
        FileSchemaProvider,
        FileKnowledgeProvider,
    )
    from backend.config.paths import KNOWLEDGE_DIR, PROMPTS_DIR

    _connector    = MSSQLAdapter()
    _schema       = FileSchemaProvider(str(PROMPTS_DIR))
    _knowledge    = FileKnowledgeProvider(str(KNOWLEDGE_DIR))
    _ai           = AIClient()
    _registry     = ToolRegistry()
    for _tool in create_database_tools(_connector, _schema, _knowledge):
        _registry.register(_tool)
    _sessions     = SessionStore()
    _orchestrator = AgentOrchestrator(
        _ai, _registry, _sessions, db_type="mssql"
    )

    app.include_router(
        create_agent_router(_orchestrator),
        prefix="/agent",
        tags=["agent-v2"],
    )
    logging.getLogger(__name__).info("Agent v2 registered at /agent/*")

except Exception as _agent_err:
    logging.getLogger(__name__).warning(
        f"Agent v2 not loaded (v1 unaffected): {_agent_err}"
    )


@app.on_event("startup")
async def _startup():
    await startup_permission_check()
