"""
App-wide configuration: paths, config loaders/savers, constants.

All runtime data lives under data/ at the project root.
Source configs:  data/config/sources/{name}.json
Source schemas:  data/sources/{name}/
AI config:       data/config/app.json  (falls back to model_config.json)
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR           = _PROJECT_ROOT / "data"
CONFIG_DIR         = DATA_DIR / "config"
SOURCES_CONFIG_DIR = CONFIG_DIR / "sources"   # data/config/sources/
SOURCES_DATA_DIR   = DATA_DIR / "sources"     # data/sources/{name}/
KNOWLEDGE_DIR      = DATA_DIR / "knowledge"
LOGS_DIR           = DATA_DIR / "logs"
CACHE_DIR          = DATA_DIR / "cache"        # data/cache/ (SQLite sessions, etc.)

SECRET_PATH        = CONFIG_DIR / ".secret"
APP_CONFIG_PATH    = CONFIG_DIR / "app.json"
_LEGACY_AI_PATH    = CONFIG_DIR / "model_config.json"  # backward compat
COMPANY_MD_PATH    = KNOWLEDGE_DIR / "company.md"
SECURITY_PATH      = CONFIG_DIR / "security.json"


# ── AI config ─────────────────────────────────────────────────────────────────

def load_ai_config() -> dict:
    """Load AI provider config. Reads app.json first, falls back to model_config.json."""
    from app.utils.crypto import decrypt_secret

    raw: dict = {}
    for path in (APP_CONFIG_PATH, _LEGACY_AI_PATH):
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f)
                break
            except Exception as e:
                logger.warning(f"Could not load {path.name}: {e}")

    if "cloud_provider" in raw:
        cloud = raw["cloud_provider"]
        local = raw.get("local_provider", {})
        encrypted_key = cloud.get("api_key", "")
        api_key = decrypt_secret(encrypted_key) if encrypted_key else ""
        return {
            "provider":        cloud.get("provider", "anthropic"),
            "api_key":         api_key,
            "api_key_hint":    cloud.get("api_key_hint", ""),
            "model":           cloud.get("model", "claude-sonnet-4-20250514"),
            "custom_endpoint": cloud.get("custom_endpoint", ""),
            "local_enabled":   bool(local.get("enabled", False)),
            "local_endpoint":  local.get("endpoint", "http://localhost:11434"),
            "local_model":     local.get("model", "qwen3:8b"),
        }

    return {
        "provider": "anthropic", "api_key": "", "api_key_hint": "",
        "model": "claude-sonnet-4-20250514", "custom_endpoint": "",
        "local_enabled": False, "local_endpoint": "http://localhost:11434",
        "local_model": "qwen3:8b",
    }


def save_ai_config(data: dict) -> None:
    """Save AI provider config to app.json (API key Fernet-encrypted)."""
    from app.utils.crypto import encrypt_secret

    raw_key = data.get("api_key", "")
    encrypted_key = encrypt_secret(raw_key) if raw_key else ""
    hint = raw_key[-4:] if len(raw_key) >= 4 else raw_key
    cfg = {
        "cloud_provider": {
            "provider":     data.get("provider", "anthropic"),
            "api_key":      encrypted_key,
            "api_key_hint": hint,
            "model":        data.get("model", "claude-sonnet-4-20250514"),
        },
        "local_provider": {
            "enabled":  bool(data.get("local_enabled", False)),
            "endpoint": data.get("local_endpoint", "http://localhost:11434"),
            "model":    data.get("local_model", "qwen3:8b"),
        },
    }
    if data.get("custom_endpoint"):
        cfg["cloud_provider"]["custom_endpoint"] = data["custom_endpoint"]

    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(APP_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

    # Cached rate-limit headers belong to the old key. Clear them so the
    # next request doesn't wait based on another account's bucket state.
    try:
        from app.ai.client import _rl_headers
        for k in list(_rl_headers.keys()):
            _rl_headers[k] = None
    except Exception:
        pass

    logger.info("AI config saved to data/config/app.json")


# ── Source configs ─────────────────────────────────────────────────────────────

def load_source_configs() -> list[dict]:
    """Load all source configs from data/config/sources/. Returns list of dicts."""
    if not SOURCES_CONFIG_DIR.exists():
        return []
    configs = []
    for path in sorted(SOURCES_CONFIG_DIR.glob("*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
            configs.append(cfg)
        except Exception as e:
            logger.warning(f"Could not load source config {path.name}: {e}")
    return configs


def save_source_config(config: dict) -> None:
    """Save source config to data/config/sources/{name}.json. Encrypts password."""
    from app.utils.crypto import encrypt_secret, is_encrypted

    name = config["name"]
    cfg = {k: v for k, v in config.items()}  # shallow copy

    # Encrypt credentials.password if it's plaintext
    if "credentials" in cfg and "password" in cfg["credentials"]:
        pw = cfg["credentials"]["password"]
        if pw and not is_encrypted(pw):
            cfg["credentials"] = dict(cfg["credentials"])
            cfg["credentials"]["password"] = encrypt_secret(pw)

    os.makedirs(SOURCES_CONFIG_DIR, exist_ok=True)
    path = SOURCES_CONFIG_DIR / f"{name}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    logger.info(f"Source config saved: data/config/sources/{name}.json")


def delete_source_config(name: str) -> None:
    """Delete a source config file."""
    path = SOURCES_CONFIG_DIR / f"{name}.json"
    if path.exists():
        path.unlink()
        logger.info(f"Source config deleted: {name}")


def is_ai_configured() -> bool:
    """Return True if an AI API key is saved."""
    cfg = load_ai_config()
    return bool(cfg.get("api_key"))


def is_setup_complete() -> bool:
    """Return True when AI is configured AND at least one source is connected."""
    if not is_ai_configured():
        return False
    return bool(load_source_configs())
