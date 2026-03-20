"""
Loaders for JSON config files.

All loaders return empty dicts / sensible defaults if the file is missing,
so OptiFlow works out-of-the-box even without config files.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))


# ── Read helpers ───────────────────────────────────────────────────────────────

def load_business_context() -> dict:
    """Load config/business_context.json.

    Returns {} if the file is missing — in that case Agent Mode works
    without data quality filters or custom terminology.
    """
    path = os.path.join(_CONFIG_DIR, "business_context.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"Could not load business_context.json: {e}")
        return {}


def load_model_config() -> dict:
    """Load config/model_config.json (raw, no decryption).

    Returns {} if the file is missing — callers fall back to hardcoded defaults.
    Used by settings page display only; use load_ai_config() for LLM calls.
    """
    path = os.path.join(_CONFIG_DIR, "model_config.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"Could not load model_config.json: {e}")
        return {}


def load_ai_config() -> dict:
    """Load AI provider config with decrypted API key.

    Returns a flat dict:
        provider        str   "anthropic" | "openai" | "custom"
        api_key         str   decrypted plaintext key (empty if not configured)
        api_key_hint    str   last 4 chars of original key (for display)
        model           str   model name
        custom_endpoint str   only set for "custom" provider
        local_enabled   bool  True if Ollama should be used for intent parsing
        local_endpoint  str   e.g. "http://localhost:11434"
        local_model     str   e.g. "qwen3:8b"
    """
    from config.crypto import decrypt_secret

    raw = load_model_config()

    # ── New structure (set by setup wizard) ───────────────────────────────────
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

    # ── Legacy structure (pre-wizard config files) — backward compat ──────────
    agent = raw.get("agent_mode", {})
    resp  = raw.get("response_interpreter", {})
    intent_fb = raw.get("intent_parser", {}).get("fallback", {})
    model = (
        agent.get("model")
        or resp.get("model")
        or intent_fb.get("model")
        or "claude-sonnet-4-20250514"
    )
    ip = raw.get("intent_parser", {})
    return {
        "provider":        "anthropic",
        "api_key":         "",  # legacy: read from .env via settings.py
        "api_key_hint":    "",
        "model":           model,
        "custom_endpoint": "",
        "local_enabled":   ip.get("provider") == "ollama",
        "local_endpoint":  ip.get("endpoint", "http://localhost:11434").replace("/api/generate", ""),
        "local_model":     ip.get("model", "qwen3:8b"),
    }


def load_db_config() -> dict:
    """Load config/db_config.json with decrypted password.

    Returns {} if the file is missing.
    """
    from config.crypto import decrypt_secret

    path = os.path.join(_CONFIG_DIR, "db_config.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"Could not load db_config.json: {e}")
        return {}

    encrypted_pw = cfg.get("password", "")
    password = decrypt_secret(encrypted_pw) if encrypted_pw else ""

    return {
        "server":   cfg.get("server", ""),
        "database": cfg.get("database", ""),
        "user":     cfg.get("user", ""),
        "password": password,
    }


# ── Write helpers ──────────────────────────────────────────────────────────────

def save_ai_config(data: dict) -> None:
    """Encrypt the API key and write config/model_config.json.

    data keys: provider, api_key, model, custom_endpoint (opt),
               local_enabled, local_endpoint, local_model
    """
    from config.crypto import encrypt_secret

    raw_key = data.get("api_key", "")
    encrypted_key = encrypt_secret(raw_key) if raw_key else ""
    hint = raw_key[-4:] if len(raw_key) >= 4 else raw_key

    cfg = {
        "cloud_provider": {
            "provider":        data.get("provider", "anthropic"),
            "api_key":         encrypted_key,
            "api_key_hint":    hint,
            "model":           data.get("model", "claude-sonnet-4-20250514"),
        },
        "local_provider": {
            "enabled":  bool(data.get("local_enabled", False)),
            "endpoint": data.get("local_endpoint", "http://localhost:11434"),
            "model":    data.get("local_model", "qwen3:8b"),
        },
    }
    if data.get("custom_endpoint"):
        cfg["cloud_provider"]["custom_endpoint"] = data["custom_endpoint"]

    path = os.path.join(_CONFIG_DIR, "model_config.json")
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    logger.info("AI config saved to config/model_config.json")


def save_db_config(data: dict) -> None:
    """Encrypt the password and write config/db_config.json.

    data keys: server, database, user, password
    """
    from config.crypto import encrypt_secret

    raw_pw = data.get("password", "")
    encrypted_pw = encrypt_secret(raw_pw) if raw_pw else ""

    cfg = {
        "server":   data.get("server", ""),
        "database": data.get("database", ""),
        "user":     data.get("user", ""),
        "password": encrypted_pw,
    }
    path = os.path.join(_CONFIG_DIR, "db_config.json")
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    logger.info("DB config saved to config/db_config.json")
