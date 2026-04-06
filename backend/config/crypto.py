"""Fernet-based symmetric encryption for secrets stored in data/config/."""
import logging
import os
from backend.config.paths import SECRET_PATH

logger = logging.getLogger(__name__)
_SECRET_PATH = str(SECRET_PATH)


def _get_fernet():
    from cryptography.fernet import Fernet
    if os.path.exists(_SECRET_PATH):
        with open(_SECRET_PATH, "rb") as f:
            key = f.read().strip()
    else:
        key = Fernet.generate_key()
        os.makedirs(os.path.dirname(_SECRET_PATH), exist_ok=True)
        with open(_SECRET_PATH, "wb") as f:
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
