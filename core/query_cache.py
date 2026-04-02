"""
In-memory query cache for Agent Mode SQL generation.

Key  = normalized question (lowercase, collapsed whitespace).
Value = the dict returned by generate_chain() — same shape passed back to the
        caller so it can skip the Claude API call entirely.

TTL  = 3600 seconds (1 hour).  Same question asked within an hour gets the
       exact same SQL, guaranteeing identical answers.
"""

import logging
import re
import time

logger = logging.getLogger(__name__)

_TTL: int = 3600  # seconds

# { normalized_key: {"data": dict, "timestamp": float} }
_cache: dict = {}


def _normalize(question: str) -> str:
    """Lowercase, strip, collapse whitespace."""
    return re.sub(r"\s+", " ", question.lower().strip())


def get(question: str) -> dict | None:
    """Return cached agent result for *question*, or None if not found / expired."""
    key = _normalize(question)
    entry = _cache.get(key)
    if entry is None:
        return None
    if time.time() - entry["timestamp"] > _TTL:
        del _cache[key]
        logger.debug(f"CACHE EXPIRED: {question[:60]!r}")
        return None
    logger.info(f"CACHE HIT: {question[:60]!r}")
    return entry["data"]


def put(question: str, data: dict) -> None:
    """Store *data* (generate_chain result) under *question*."""
    key = _normalize(question)
    _cache[key] = {"data": data, "timestamp": time.time()}
    logger.info(f"CACHE SET: {question[:60]!r}")


def clear_expired() -> int:
    """Evict all expired entries. Returns the count removed."""
    now = time.time()
    expired = [k for k, v in _cache.items() if now - v["timestamp"] > _TTL]
    for k in expired:
        del _cache[k]
    return len(expired)


def clear() -> int:
    """Evict all entries. Returns count removed."""
    count = len(_cache)
    _cache.clear()
    if count:
        logger.info(f"CACHE CLEARED: {count} entries removed")
    return count


def size() -> int:
    return len(_cache)
