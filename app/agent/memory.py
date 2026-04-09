"""
SessionStore — in-memory conversation history with TTL and LRU eviction.

Each session stores the full Anthropic messages list (user + assistant turns,
including all tool call/result blocks). This allows multi-turn follow-up questions
within the same session.
"""

import logging
import threading
import time
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_TTL      = 3600   # 1 hour
_DEFAULT_MAX      = 100    # max concurrent sessions before LRU eviction


class SessionStore:
    """Thread-safe in-memory session store with TTL and LRU eviction."""

    def __init__(
        self,
        ttl: int = _DEFAULT_TTL,
        max_sessions: int = _DEFAULT_MAX,
    ):
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._ttl  = ttl
        self._max  = max_sessions

    # ── Internals ─────────────────────────────────────────────────────────────

    def _now(self) -> float:
        return time.monotonic()

    def _evict_expired(self) -> None:
        """Remove sessions that have not been accessed within TTL."""
        now     = self._now()
        expired = [
            sid for sid, s in self._sessions.items()
            if now - s["last_access"] > self._ttl
        ]
        for sid in expired:
            del self._sessions[sid]
        if expired:
            logger.debug(f"SessionStore: evicted {len(expired)} expired sessions")

    def _evict_lru(self) -> None:
        """Remove the least-recently-used session when at capacity."""
        if len(self._sessions) >= self._max:
            oldest = min(
                self._sessions,
                key=lambda k: self._sessions[k]["last_access"],
            )
            del self._sessions[oldest]
            logger.debug(f"SessionStore: LRU evicted session {oldest}")

    # ── Public API ────────────────────────────────────────────────────────────

    def create_session(self) -> str:
        """Create a new session and return its ID."""
        session_id = uuid.uuid4().hex[:16]
        now = self._now()
        with self._lock:
            self._evict_expired()
            self._evict_lru()
            self._sessions[session_id] = {
                "messages":    [],
                "created_at":  now,
                "last_access": now,
            }
        logger.info(f"SessionStore: created session {session_id}")
        return session_id

    def get_or_create(self, session_id: Optional[str]) -> str:
        """Return session_id unchanged if valid and not expired; otherwise create a new one."""
        if session_id:
            with self._lock:
                s = self._sessions.get(session_id)
                if s and self._now() - s["last_access"] <= self._ttl:
                    s["last_access"] = self._now()
                    return session_id
        return self.create_session()

    def get_messages(self, session_id: str) -> list:
        """Return a copy of the message list for this session (empty list if not found)."""
        with self._lock:
            s = self._sessions.get(session_id)
            return list(s["messages"]) if s else []

    def set_messages(self, session_id: str, messages: list) -> None:
        """Replace the stored message list for this session."""
        with self._lock:
            s = self._sessions.get(session_id)
            if s:
                s["messages"]    = list(messages)
                s["last_access"] = self._now()

    def exists(self, session_id: str) -> bool:
        """Return True if the session exists and has not expired."""
        with self._lock:
            s = self._sessions.get(session_id)
            return bool(s and self._now() - s["last_access"] <= self._ttl)

    def destroy(self, session_id: str) -> None:
        """Delete a session immediately."""
        with self._lock:
            self._sessions.pop(session_id, None)
        logger.info(f"SessionStore: destroyed session {session_id}")

    def session_count(self) -> int:
        """Return the current number of live sessions."""
        with self._lock:
            return len(self._sessions)
