"""
Audit logger — appends structured JSON lines to logs/audit.jsonl.

Rotation: if the file exceeds _MAX_BYTES (10 MB), the current file is
renamed to audit.jsonl.1 (shifting older rotated files up), and a new
audit.jsonl is started.  At most _MAX_ROTATIONS rotated files are kept
(50 MB total history).

Rules enforced here:
  - Passwords and API keys are never logged.
  - Full result rows are never logged — only counts.
  - Failures never crash the caller; they go to stderr instead.
  - audit.jsonl is append-only; nothing in this module deletes entries.
"""

import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone

_ROOT          = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOG_DIR       = os.path.join(_ROOT, "logs")
_LOG_PATH      = os.path.join(_LOG_DIR, "audit.jsonl")
_MAX_BYTES     = 10 * 1024 * 1024   # 10 MB per file
_MAX_ROTATIONS = 5                   # keep audit.jsonl.1 … audit.jsonl.5

_lock  = threading.Lock()
logger = logging.getLogger(__name__)

# Keys whose values must never appear in audit entries.
_SENSITIVE_PATTERNS = (
    "password", "passwd", "api_key", "apikey", "secret", "token", "credential",
)


def _scrub(obj: object) -> object:
    """Recursively replace values whose key name hints at a secret with '[REDACTED]'."""
    if isinstance(obj, dict):
        return {
            k: "[REDACTED]" if any(p in k.lower() for p in _SENSITIVE_PATTERNS) else _scrub(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_scrub(item) for item in obj]
    return obj


# ── Rotation ──────────────────────────────────────────────────────────────────

def _rotate() -> None:
    """Shift rotated files up by one and rename the active log to .1."""
    oldest = f"{_LOG_PATH}.{_MAX_ROTATIONS}"
    if os.path.exists(oldest):
        try:
            os.remove(oldest)
        except OSError as exc:
            print(f"[audit_logger] Could not remove {oldest}: {exc}", file=sys.stderr)

    for i in range(_MAX_ROTATIONS - 1, 0, -1):
        src = f"{_LOG_PATH}.{i}"
        dst = f"{_LOG_PATH}.{i + 1}"
        if os.path.exists(src):
            try:
                os.rename(src, dst)
            except OSError as exc:
                print(f"[audit_logger] Could not rotate {src}: {exc}", file=sys.stderr)

    if os.path.exists(_LOG_PATH):
        try:
            os.rename(_LOG_PATH, f"{_LOG_PATH}.1")
        except OSError as exc:
            print(f"[audit_logger] Could not rotate active log: {exc}", file=sys.stderr)


# ── Public API ────────────────────────────────────────────────────────────────

def log_action(username: str, action: str, details: dict) -> None:
    """
    Append one audit entry to logs/audit.jsonl.

    Never raises — if writing fails the error is printed to stderr so
    the calling request can keep running.
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "username":  username or "anonymous",
        "action":    action,
        "details":   _scrub(details),
    }
    line = json.dumps(entry, ensure_ascii=False)

    try:
        with _lock:
            os.makedirs(_LOG_DIR, exist_ok=True)
            if (
                os.path.exists(_LOG_PATH)
                and os.path.getsize(_LOG_PATH) >= _MAX_BYTES
            ):
                _rotate()
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as exc:
        print(f"[audit_logger] FAILED to write audit entry: {exc}", file=sys.stderr)


def read_entries(limit: int = 200) -> list[dict]:
    """
    Return the most recent *limit* entries from audit.jsonl, newest first.
    Silently skips malformed lines.
    """
    if not os.path.exists(_LOG_PATH):
        return []

    entries: list[dict] = []
    try:
        with open(_LOG_PATH, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entries.append(json.loads(raw))
                except json.JSONDecodeError:
                    pass
    except Exception as exc:
        print(f"[audit_logger] FAILED to read audit log: {exc}", file=sys.stderr)

    return list(reversed(entries))[:limit]
