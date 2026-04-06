"""Application settings — reads from data/config/ JSON files with env var fallback."""
import os
from backend.config.loader import load_db_config, load_ai_config

_db = load_db_config()
_ai = load_ai_config()

DB_SERVER   = _db.get("server")   or os.getenv("DB_SERVER")
DB_NAME     = _db.get("database") or os.getenv("DB_NAME")
DB_USER     = _db.get("user")     or os.getenv("DB_USER")
DB_PASSWORD = _db.get("password") or os.getenv("DB_PASSWORD")

ANTHROPIC_API_KEY = _ai.get("api_key") or os.getenv("ANTHROPIC_API_KEY")

INTENT_PARSER_MODE = (
    "local" if _ai.get("local_enabled")
    else os.getenv("INTENT_PARSER_MODE", "cloud")
)
