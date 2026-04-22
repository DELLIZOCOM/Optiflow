"""
OptiFlow AI — application entry point.

Startup sequence:
  1. Load AI config
  2. Load all source configs from data/config/sources/
  3. Instantiate DataSource objects and populate SourceRegistry
  4. Build ToolRegistry from all sources
  5. Create AIClient, SessionStore, AgentOrchestrator
  6. Mount routes

Routes:
  POST /ask                         — agent chat (SSE streaming)
  GET  /session/{id}                — session status
  /setup/*                          — setup wizard
  /sources/*                        — manage connected sources
  /static/*                         — frontend assets
"""

import logging
import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.sources.base import SourceRegistry
from app.tools.base import ToolRegistry
from app.tools.database import create_database_tools
from app.agent.memory import SessionStore
from app.agent.orchestrator import AgentOrchestrator
from app.ai.client import AIClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Module-level singletons (shared across requests) ──────────────────────────

_source_registry: SourceRegistry = SourceRegistry()
_tool_registry:   ToolRegistry   = ToolRegistry()
_sessions:        SessionStore   = SessionStore()
_orchestrator:    AgentOrchestrator | None = None

# Lazy singleton — instantiated on first request for an email source.
# Isolated from sessions.db so its retention + vacuum schedule is its own.
_email_store = None


def _get_email_store():
    """Return the process-wide EmailStore, creating it on first use."""
    global _email_store
    if _email_store is None:
        from app.config import EMAIL_DB_PATH
        from app.sources.email.store import EmailStore
        _email_store = EmailStore(EMAIL_DB_PATH)
    return _email_store


async def install_email_source(source) -> None:
    """
    Register an EmailSource into the live registries and begin ingestion.
    Also registers the 4 email tools (only once — a second call is a no-op
    because ToolRegistry.register overwrites by name).
    """
    from app.tools.email import register_email_tools
    _source_registry.register(source)
    register_email_tools(_tool_registry, source.store)
    await source.start()
    logger.info(f"Email source '{source.name}' installed and ingestion started")


async def _maybe_start_email_source() -> None:
    """
    On startup, check for a persisted Outlook config. If present, instantiate
    the OutlookSource and kick off ingestion. Silent no-op if not configured.
    """
    from app.config import load_outlook_config
    cfg = load_outlook_config()
    if not cfg or not cfg.get("client_id"):
        return
    from app.sources.email.outlook.auth import OutlookCredentials
    from app.sources.email.outlook.source import OutlookSource
    source = OutlookSource(
        name="outlook",
        tenant_display_name=cfg.get("tenant_display_name") or "Company Email",
        credentials=OutlookCredentials(
            tenant_id=cfg["tenant_id"],
            client_id=cfg["client_id"],
            client_secret=cfg["client_secret"],
        ),
        store=_get_email_store(),
        backfill_days=cfg.get("backfill_days", 365),
    )
    await install_email_source(source)


async def uninstall_email_source(name: str) -> None:
    """Stop ingestion and remove from the source registry. Leaves cache intact."""
    src = _source_registry.get(name)
    if src is None:
        return
    try:
        await src.stop()
    except Exception:
        logger.exception(f"Error while stopping email source '{name}'")
    _source_registry.remove(name)
    logger.info(f"Email source '{name}' uninstalled")


def _instantiate_source(config: dict):
    """Create the right DataSource subclass from a source config dict."""
    from app.sources.database.mssql      import MSSQLSource
    from app.sources.database.postgresql import PostgreSQLSource
    from app.sources.database.mysql      import MySQLSource

    cls_map = {
        "mssql":      MSSQLSource,
        "postgresql": PostgreSQLSource,
        "mysql":      MySQLSource,
    }
    source_type = config.get("type", "").lower()
    name        = config.get("name", "unknown")
    cls = cls_map.get(source_type)
    if cls is None:
        logger.warning(f"Unknown source type '{source_type}' for '{name}' — skipping")
        return None
    try:
        return cls(name, config)
    except Exception as e:
        logger.error(f"Could not instantiate source '{name}': {e}")
        return None


def load_sources() -> None:
    """Load all source configs, register DataSource instances, and warm schema cache."""
    from app.config import load_source_configs

    configs = load_source_configs()
    for cfg in configs:
        source = _instantiate_source(cfg)
        if source:
            _source_registry.register(source)
            # Warm in-memory schema cache so tools never touch disk at query time
            try:
                source.load_cache()
            except Exception as exc:
                logger.warning(f"Schema cache warm failed for '{source.name}': {exc}")

    logger.info(
        f"Sources loaded: {len(_source_registry.get_all())} source(s) — "
        f"{_source_registry.names()}"
    )


def build_tool_registry() -> None:
    """Populate the ToolRegistry with tools from all sources."""
    for tool in create_database_tools(_source_registry):
        _tool_registry.register(tool)
    logger.info(
        f"Tools registered: {[t['name'] for t in _tool_registry.get_api_definitions()]}"
    )


# ── App factory ───────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    global _orchestrator

    app = FastAPI(title="OptiFlow AI")

    # Static files (frontend)
    _FRONTEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
    app.mount("/static", StaticFiles(directory=_FRONTEND), name="static")

    # Setup wizard routes
    from app.routes.setup import router as setup_router, init_router as setup_init
    app.include_router(setup_router)

    # Sources management routes
    from app.routes.sources import router as sources_router, init_router as sources_init
    app.include_router(sources_router)

    # Email integration routes (Outlook admin-consent setup + status)
    from app.routes.email import create_email_router
    app.include_router(create_email_router(
        source_registry   = _source_registry,
        tool_registry     = _tool_registry,
        get_or_create_store = _get_email_store,
        install_source    = install_email_source,
        uninstall_source  = uninstall_email_source,
    ))

    @app.on_event("startup")
    async def _startup():
        global _orchestrator

        # 1. Load sources
        load_sources()

        # 2. Build tool registry
        build_tool_registry()

        # 3. Wire registries into setup + sources routers
        setup_init(_source_registry, _tool_registry, _sessions)
        sources_init(_source_registry, _tool_registry)

        # 4. Create orchestrator
        _orchestrator = AgentOrchestrator(
            ai_client      = AIClient(),
            tool_registry  = _tool_registry,
            source_registry= _source_registry,
            sessions       = _sessions,
        )

        # 5. Register agent router now that orchestrator is ready
        from app.routes.agent import create_agent_router
        app.include_router(create_agent_router(_orchestrator))

        # 6. If email is already configured, bring the source up
        try:
            await _maybe_start_email_source()
        except Exception:
            logger.exception("Failed to auto-start email source on boot")

        sources = _source_registry.get_all()
        if not sources:
            logger.warning(
                "No data sources configured. "
                "Open http://localhost:8000/static/pages/setup.html to complete setup."
            )
        else:
            logger.info(f"OptiFlow AI ready — {len(sources)} source(s) connected")

    # Serve chat page at root
    @app.get("/")
    async def root():
        from fastapi.responses import FileResponse
        return FileResponse(
            os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "frontend", "pages", "chat.html"
            )
        )

    # Serve setup page
    @app.get("/setup")
    async def setup_page():
        from fastapi.responses import FileResponse
        return FileResponse(
            os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "frontend", "pages", "setup.html"
            )
        )

    return app


app = create_app()
