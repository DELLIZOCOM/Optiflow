"""
Feedback logger — records user thumbs-up / thumbs-down on AI responses.

Each line in logs/feedback.jsonl is a JSON object:
{
  "timestamp":        "2026-03-18T14:30:00",
  "username":         "admin",
  "question":         "Total sales for Hyundai",
  "sql":              "SELECT SUM(Sales_Amount)...",
  "tables_used":      ["ProSt"],
  "answer_preview":   "Total project value...",
  "rating":           "negative",
  "comment":          "Wrong customer",
  "response_time_ms": 6200,
  "was_cached":       false
}
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

_ROOT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOG_PATH = os.path.join(_ROOT, "logs", "feedback.jsonl")


def append(entry: dict) -> None:
    """Write one feedback entry to logs/feedback.jsonl."""
    os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info(
            f"FEEDBACK: {entry.get('rating')}  "
            f"'{entry.get('question', '')[:60]}'  "
            f"comment={bool(entry.get('comment'))}"
        )
    except OSError as e:
        logger.error(f"feedback_logger.append failed: {e}")


def read_entries(limit: int = 500) -> list:
    """Return the most recent `limit` entries, newest first."""
    if not os.path.exists(_LOG_PATH):
        return []
    entries = []
    try:
        with open(_LOG_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except OSError:
        return []
    # Reverse so newest is first, then cap at limit
    return list(reversed(entries))[:limit]
