"""Loaders for JSON config files. All return empty dicts if files are missing."""
import json
import logging
import os
from backend.config.paths import (
    MODEL_CONFIG_PATH, DB_CONFIG_PATH, CONFIG_DIR
)

logger = logging.getLogger(__name__)


def load_business_context() -> dict:
    path = CONFIG_DIR / "business_context.json"
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"Could not load business_context.json: {e}")
        return {}


def load_model_config() -> dict:
    try:
        with open(MODEL_CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"Could not load model_config.json: {e}")
        return {}


def load_ai_config() -> dict:
    from backend.config.crypto import decrypt_secret
    raw = load_model_config()
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
    # Legacy flat structure
    agent = raw.get("agent_mode", {})
    resp  = raw.get("response_interpreter", {})
    intent_fb = raw.get("intent_parser", {}).get("fallback", {})
    model = (agent.get("model") or resp.get("model") or intent_fb.get("model")
             or "claude-sonnet-4-20250514")
    ip = raw.get("intent_parser", {})
    return {
        "provider": "anthropic", "api_key": "", "api_key_hint": "", "model": model,
        "custom_endpoint": "",
        "local_enabled":  ip.get("provider") == "ollama",
        "local_endpoint": ip.get("endpoint", "http://localhost:11434").replace("/api/generate", ""),
        "local_model":    ip.get("model", "qwen3:8b"),
    }


def load_db_config() -> dict:
    from backend.config.crypto import decrypt_secret
    try:
        with open(DB_CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"Could not load db_config.json: {e}")
        return {}
    encrypted_pw = cfg.get("password", "")
    return {
        "server":   cfg.get("server", ""),
        "database": cfg.get("database", ""),
        "user":     cfg.get("user", ""),
        "password": decrypt_secret(encrypted_pw) if encrypted_pw else "",
    }


def save_ai_config(data: dict) -> None:
    from backend.config.crypto import encrypt_secret
    raw_key = data.get("api_key", "")
    encrypted_key = encrypt_secret(raw_key) if raw_key else ""
    hint = raw_key[-4:] if len(raw_key) >= 4 else raw_key
    cfg = {
        "cloud_provider": {
            "provider": data.get("provider", "anthropic"),
            "api_key": encrypted_key, "api_key_hint": hint,
            "model": data.get("model", "claude-sonnet-4-20250514"),
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
    with open(MODEL_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    logger.info("AI config saved to data/config/model_config.json")


def save_db_config(data: dict) -> None:
    from backend.config.crypto import encrypt_secret
    raw_pw = data.get("password", "")
    cfg = {
        "server": data.get("server", ""), "database": data.get("database", ""),
        "user": data.get("user", ""),
        "password": encrypt_secret(raw_pw) if raw_pw else "",
    }
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(DB_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    logger.info("DB config saved to data/config/db_config.json")
