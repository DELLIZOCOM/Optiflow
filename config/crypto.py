"""
Fernet-based symmetric encryption for secrets stored in config/.

The encryption key is generated once and stored in config/.secret (git-ignored).
If .secret is deleted, encrypted values can no longer be decrypted — the user
must re-enter secrets through the setup wizard.
"""

import logging
import os

logger = logging.getLogger(__name__)

_SECRET_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".secret")


def _get_fernet():
    """Load or generate the Fernet key from config/.secret."""
    from cryptography.fernet import Fernet

    if os.path.exists(_SECRET_PATH):
        with open(_SECRET_PATH, "rb") as f:
            key = f.read().strip()
    else:
        key = Fernet.generate_key()
        os.makedirs(os.path.dirname(_SECRET_PATH), exist_ok=True)
        with open(_SECRET_PATH, "wb") as f:
            f.write(key)
        logger.info("Generated new encryption key at config/.secret")

    return Fernet(key)


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a plaintext string. Returns a URL-safe base64 token."""
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(token: str) -> str:
    """Decrypt a Fernet token back to plaintext.

    Returns the token as-is if decryption fails (backward compat with
    plaintext values that predate encryption, or after .secret is deleted).
    """
    if not token:
        return ""
    try:
        from cryptography.fernet import InvalidToken
        return _get_fernet().decrypt(token.encode()).decode()
    except (InvalidToken, Exception) as e:
        logger.warning(f"decrypt_secret failed ({type(e).__name__}) — returning empty string")
        return ""
