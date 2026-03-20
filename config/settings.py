"""
Application settings — all config read from config/ JSON files.

Variable names are kept identical to the previous .env-based version
so all existing importers (core/db.py, app.py, etc.) continue to work.

Fallback chain for each value:
  1. config/db_config.json  (set by setup wizard)
  2. environment variable   (legacy .env migration support)
  3. None / "local"
"""

import os

from config.loader import load_db_config, load_ai_config

_db = load_db_config()
_ai = load_ai_config()

# ── Database credentials ───────────────────────────────────────────────────────
DB_SERVER   = _db.get("server")   or os.getenv("DB_SERVER")
DB_NAME     = _db.get("database") or os.getenv("DB_NAME")
DB_USER     = _db.get("user")     or os.getenv("DB_USER")
DB_PASSWORD = _db.get("password") or os.getenv("DB_PASSWORD")

# ── AI / API keys ──────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = _ai.get("api_key") or os.getenv("ANTHROPIC_API_KEY")

# ── Intent parser mode ─────────────────────────────────────────────────────────
# Derived from local_provider.enabled; env var kept for legacy compat.
INTENT_PARSER_MODE = (
    "local" if _ai.get("local_enabled")
    else os.getenv("INTENT_PARSER_MODE", "cloud")
)
