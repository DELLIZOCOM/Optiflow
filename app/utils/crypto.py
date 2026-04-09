"""Fernet-based symmetric encryption for secrets stored in data/config/."""

import logging
import os

logger = logging.getLogger(__name__)


def _get_fernet():
    from cryptography.fernet import Fernet
    from app.config import SECRET_PATH

    secret_path = str(SECRET_PATH)
    if os.path.exists(secret_path):
        with open(secret_path, "rb") as f:
            key = f.read().strip()
    else:
        key = Fernet.generate_key()
        os.makedirs(os.path.dirname(secret_path), exist_ok=True)
        with open(secret_path, "wb") as f:
            f.write(key)
        logger.info("Generated new encryption key at data/config/.secret")
    return Fernet(key)


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(token: str) -> str:
    if not token:
        return ""
    try:
        from cryptography.fernet import InvalidToken
        return _get_fernet().decrypt(token.encode()).decode()
    except (InvalidToken, Exception) as e:
        logger.warning(f"decrypt_secret failed ({type(e).__name__}) — returning empty string")
        return ""


def is_encrypted(value: str) -> bool:
    """Return True if value looks like a Fernet token (starts with gAAAA)."""
    return isinstance(value, str) and value.startswith("gAAAA")
