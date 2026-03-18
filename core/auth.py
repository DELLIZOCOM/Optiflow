"""
User authentication helpers.

Users are stored in config/users.json as bcrypt hashes — plaintext passwords
are never written to disk.
"""

import json
import logging
import os
from datetime import datetime, timezone

import bcrypt

logger = logging.getLogger(__name__)

_ROOT       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_USERS_PATH = os.path.join(_ROOT, "config", "users.json")


# ── File helpers ──────────────────────────────────────────────────────────────

def users_exist() -> bool:
    """Return True if config/users.json exists (any users have been created)."""
    return os.path.exists(_USERS_PATH)


def _load_users() -> list:
    if not os.path.exists(_USERS_PATH):
        return []
    try:
        with open(_USERS_PATH, encoding="utf-8") as f:
            return json.load(f).get("users", [])
    except Exception as e:
        logger.error(f"Failed to load users.json: {e}")
        return []


def _save_users(users: list) -> None:
    os.makedirs(os.path.dirname(_USERS_PATH), exist_ok=True)
    with open(_USERS_PATH, "w", encoding="utf-8") as f:
        json.dump({"users": users}, f, indent=2, ensure_ascii=False)


# ── User operations ───────────────────────────────────────────────────────────

def find_user(username: str) -> dict | None:
    """Return the user dict for *username*, or None."""
    for u in _load_users():
        if u.get("username") == username:
            return u
    return None


def verify_password(username: str, password: str) -> bool:
    """Return True if *password* matches the stored bcrypt hash for *username*."""
    user = find_user(username)
    if not user:
        return False
    stored_hash = user.get("password_hash", "").encode("utf-8")
    try:
        return bcrypt.checkpw(password.encode("utf-8"), stored_hash)
    except Exception as e:
        logger.error(f"bcrypt check failed: {e}")
        return False


def create_user(username: str, password: str, role: str = "admin") -> None:
    """Hash *password* with bcrypt and append the user to users.json."""
    password_hash = bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt(rounds=12)
    ).decode("utf-8")

    users = _load_users()
    users.append({
        "username":      username,
        "password_hash": password_hash,
        "created_at":    datetime.now(timezone.utc).isoformat(),
        "role":          role,
    })
    _save_users(users)
    logger.info(f"User '{username}' created (role={role})")
