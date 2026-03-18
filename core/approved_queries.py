"""
Approved query log — records user-approved agent SQL and finds similar past queries.

Every time a user approves and runs an agent query, `append()` writes one JSON
line to logs/approved_queries.jsonl.

Before generating new SQL, `find_similar()` scans the log using Jaccard token
overlap.  If a past query is sufficiently similar (≥ SIMILARITY_THRESHOLD),
the caller can offer the proven SQL instead of hitting Claude.
"""

import json
import logging
import os
import re
import time

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOG_PATH = os.path.join(_ROOT, "logs", "approved_queries.jsonl")

# Minimum Jaccard similarity to consider two questions a match.
# 0.72 means ~3 of 4 meaningful words must overlap.
SIMILARITY_THRESHOLD = 0.72

# Common words that don't carry query intent — excluded from token sets.
_STOP_WORDS = {
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or",
    "is", "are", "was", "were", "be", "been", "have", "has", "had",
    "do", "does", "did", "with", "from", "by", "this", "that", "all",
    "which", "what", "how", "many", "much", "list", "show", "give",
    "me", "my", "our", "can", "get", "find", "i", "it", "its",
}


def _tokens(text: str) -> set:
    """Split text into lowercase alphanumeric tokens, excluding stop words."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if w not in _STOP_WORDS and len(w) > 1}


def append(
    question: str,
    sql: str,
    tables_used: list,
    row_count: int,
    execution_time_ms: int,
) -> None:
    """Append one approved query to the log file."""
    os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
    entry = {
        "timestamp": time.time(),
        "question":  question,
        "sql":       sql,
        "tables_used":        tables_used,
        "row_count":          row_count,
        "execution_time_ms":  execution_time_ms,
    }
    with open(_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    logger.info(f"APPROVED LOG: saved '{question[:60]}'")


def find_similar(question: str) -> dict | None:
    """Return the most similar approved-query entry, or None.

    Uses Jaccard similarity on content tokens (stop words removed).
    Returns the entry dict (with keys: question, sql, tables_used, row_count,
    timestamp) if similarity ≥ SIMILARITY_THRESHOLD, else None.
    """
    if not os.path.exists(_LOG_PATH):
        return None

    q_tokens = _tokens(question)
    if not q_tokens:
        return None

    best_entry: dict | None = None
    best_score: float = 0.0

    with open(_LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            e_tokens = _tokens(entry.get("question", ""))
            if not e_tokens:
                continue

            intersection = len(q_tokens & e_tokens)
            union = len(q_tokens | e_tokens)
            score = intersection / union if union else 0.0

            if score > best_score:
                best_score = score
                best_entry = entry

    if best_entry and best_score >= SIMILARITY_THRESHOLD:
        logger.info(
            f"APPROVED LOG HIT: score={best_score:.2f}  "
            f"'{best_entry['question'][:60]}'"
        )
        return best_entry

    return None
