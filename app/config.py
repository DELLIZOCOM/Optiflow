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
from typing import Optional

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

# Email integration — at most one provider connected at a time (Outlook OR IMAP)
EMAIL_CONFIG_DIR    = CONFIG_DIR / "email"
OUTLOOK_CONFIG_PATH = EMAIL_CONFIG_DIR / "outlook.json"
IMAP_CONFIG_PATH    = EMAIL_CONFIG_DIR / "imap.json"
EMAIL_DB_PATH       = CACHE_DIR / "email.db"


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


# ── Email (Outlook admin-consent) credentials ─────────────────────────────────

def load_outlook_config() -> Optional[dict]:
    """
    Load Outlook admin-consent credentials. Returns a dict with
    {tenant_id, client_id, client_secret, tenant_display_name, added_at,
    added_by, backfill_days} or None if not configured. client_secret is
    returned decrypted.
    """
    if not OUTLOOK_CONFIG_PATH.exists():
        return None
    from app.utils.crypto import decrypt_secret
    try:
        with open(OUTLOOK_CONFIG_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        logger.warning(f"Could not load outlook.json: {e}")
        return None
    enc = raw.get("client_secret", "")
    return {
        "tenant_id":           raw.get("tenant_id", ""),
        "client_id":           raw.get("client_id", ""),
        "client_secret":       decrypt_secret(enc) if enc else "",
        "tenant_display_name": raw.get("tenant_display_name", ""),
        "added_at":            raw.get("added_at", 0),
        "added_by":            raw.get("added_by", ""),
        "backfill_days":       int(raw.get("backfill_days", 365)),
    }


def save_outlook_config(data: dict) -> None:
    """Persist Outlook credentials; client_secret Fernet-encrypted at rest."""
    from app.utils.crypto import encrypt_secret, is_encrypted
    import time as _time
    secret = data.get("client_secret", "")
    if secret and not is_encrypted(secret):
        secret = encrypt_secret(secret)
    cfg = {
        "tenant_id":           data.get("tenant_id", "").strip(),
        "client_id":           data.get("client_id", "").strip(),
        "client_secret":       secret,
        "tenant_display_name": data.get("tenant_display_name", "").strip(),
        "added_at":            data.get("added_at") or _time.time(),
        "added_by":            data.get("added_by", ""),
        "backfill_days":       int(data.get("backfill_days", 365)),
    }
    os.makedirs(EMAIL_CONFIG_DIR, exist_ok=True)
    with open(OUTLOOK_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    try:
        os.chmod(OUTLOOK_CONFIG_PATH, 0o600)
    except OSError:
        pass
    logger.info("Outlook config saved to data/config/email/outlook.json")


def delete_outlook_config() -> None:
    """Remove the Outlook credentials file. Does NOT touch email.db."""
    if OUTLOOK_CONFIG_PATH.exists():
        OUTLOOK_CONFIG_PATH.unlink()
        logger.info("Outlook config deleted")


def is_email_configured() -> bool:
    cfg = load_outlook_config()
    if cfg and cfg.get("tenant_id") and cfg.get("client_id") and cfg.get("client_secret"):
        return True
    icfg = load_imap_config()
    return bool(icfg and icfg.get("host") and (icfg.get("mailboxes") or []))


# ── Email (IMAP / GoDaddy / generic) credentials ──────────────────────────────

def load_imap_config() -> Optional[dict]:
    """
    Load IMAP config. Returns:
      {
        provider:            "godaddy" | "generic",
        tenant_display_name: str,
        host:                str,
        port:                int,
        use_ssl:             bool,
        backfill_days:       int,
        mailboxes: [
          { account_email, password, display_name?, folder },   # password decrypted
          ...
        ],
        added_at:            float,
        added_by:            str,
      }
    Returns None when not configured. Per-mailbox passwords are decrypted.
    """
    if not IMAP_CONFIG_PATH.exists():
        return None
    from app.utils.crypto import decrypt_secret
    try:
        with open(IMAP_CONFIG_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        logger.warning(f"Could not load imap.json: {e}")
        return None

    mailboxes_in = raw.get("mailboxes") or []
    mailboxes_out: list[dict] = []
    for mb in mailboxes_in:
        if not isinstance(mb, dict):
            continue
        addr = (mb.get("account_email") or "").strip()
        if not addr:
            continue
        pw = mb.get("password", "")
        try:
            pw_plain = decrypt_secret(pw) if pw else ""
        except Exception:
            pw_plain = ""
        mailboxes_out.append({
            "account_email": addr,
            "password":      pw_plain,
            "display_name":  (mb.get("display_name") or "").strip() or None,
            "folder":        (mb.get("folder") or "INBOX").strip() or "INBOX",
        })

    return {
        "provider":            (raw.get("provider") or "generic"),
        "tenant_display_name": raw.get("tenant_display_name", ""),
        "host":                raw.get("host", ""),
        "port":                int(raw.get("port", 993)),
        "use_ssl":             bool(raw.get("use_ssl", True)),
        "backfill_days":       int(raw.get("backfill_days", 365)),
        "mailboxes":           mailboxes_out,
        "added_at":            raw.get("added_at", 0),
        "added_by":            raw.get("added_by", ""),
    }


def save_imap_config(data: dict) -> None:
    """Persist IMAP config; per-mailbox passwords Fernet-encrypted at rest."""
    from app.utils.crypto import encrypt_secret, is_encrypted
    import time as _time

    mailboxes_in = data.get("mailboxes") or []
    mailboxes_out: list[dict] = []
    for mb in mailboxes_in:
        if not isinstance(mb, dict):
            continue
        addr = (mb.get("account_email") or "").strip().lower()
        if not addr:
            continue
        pw = mb.get("password", "")
        if pw and not is_encrypted(pw):
            pw = encrypt_secret(pw)
        mailboxes_out.append({
            "account_email": addr,
            "password":      pw,
            "display_name":  (mb.get("display_name") or "").strip() or None,
            "folder":        (mb.get("folder") or "INBOX").strip() or "INBOX",
        })

    cfg = {
        "provider":            (data.get("provider") or "generic").strip().lower(),
        "tenant_display_name": (data.get("tenant_display_name") or "").strip(),
        "host":                (data.get("host") or "").strip(),
        "port":                int(data.get("port", 993)),
        "use_ssl":             bool(data.get("use_ssl", True)),
        "backfill_days":       int(data.get("backfill_days", 365)),
        "mailboxes":           mailboxes_out,
        "added_at":            data.get("added_at") or _time.time(),
        "added_by":            data.get("added_by", ""),
    }
    os.makedirs(EMAIL_CONFIG_DIR, exist_ok=True)
    with open(IMAP_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    try:
        os.chmod(IMAP_CONFIG_PATH, 0o600)
    except OSError:
        pass
    logger.info("IMAP config saved to data/config/email/imap.json")


def delete_imap_config() -> None:
    """Remove the IMAP credentials file. Does NOT touch email.db."""
    if IMAP_CONFIG_PATH.exists():
        IMAP_CONFIG_PATH.unlink()
        logger.info("IMAP config deleted")


def active_email_provider() -> Optional[str]:
    """
    Return the provider name of the currently configured email source, or None.
    Outlook wins if both are present (shouldn't happen in practice).
    """
    if OUTLOOK_CONFIG_PATH.exists():
        return "outlook"
    if IMAP_CONFIG_PATH.exists():
        return "imap"
    return None


def is_ai_configured() -> bool:
    """Return True if an AI API key is saved."""
    cfg = load_ai_config()
    return bool(cfg.get("api_key"))


def is_setup_complete() -> bool:
    """Return True when AI is configured AND at least one source is connected."""
    if not is_ai_configured():
        return False
    return bool(load_source_configs())
