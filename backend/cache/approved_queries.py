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


from backend.config.paths import APPROVED_Q_PATH as _LOG_PATH

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
    Skips entries flagged as wrong via user feedback.
    Returns the entry dict if similarity ≥ SIMILARITY_THRESHOLD, else None.
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

            # Skip entries flagged as wrong by user feedback, or stale (table removed)
            if entry.get("flagged") or entry.get("stale"):
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
            f"confirmed={best_entry.get('confirmed', False)}  "
            f"'{best_entry['question'][:60]}'"
        )
        return best_entry

    return None


def _update_entry(question: str, sql: str, updates: dict) -> bool:
    """Rewrite the log with the first matching entry updated in-place.

    Matches on question text (case-insensitive) OR first 200 chars of SQL.
    Returns True if an entry was found and updated.
    """
    if not os.path.exists(_LOG_PATH):
        return False

    q_lower  = question.strip().lower()
    sql_trim = sql.strip()[:200]
    updated  = False
    lines    = []

    try:
        with open(_LOG_PATH, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    lines.append(line)
                    continue
                try:
                    entry = json.loads(stripped)
                    if (
                        entry.get("question", "").strip().lower() == q_lower
                        or entry.get("sql", "").strip()[:200] == sql_trim
                    ):
                        if not updated:   # update only the first match
                            entry.update(updates)
                            updated = True
                    lines.append(json.dumps(entry, ensure_ascii=False) + "\n")
                except json.JSONDecodeError:
                    lines.append(line)

        if updated:
            with open(_LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(lines)
    except OSError as e:
        logger.error(f"_update_entry failed: {e}")
        return False

    return updated


def flag_entry(question: str, sql: str) -> bool:
    """Mark an entry as flagged (thumbs-down feedback).

    Flagged entries are skipped by find_similar(), preventing reuse of bad SQL.
    """
    result = _update_entry(question, sql, {"flagged": True, "confirmed": False})
    if result:
        logger.info(f"APPROVED LOG FLAGGED: '{question[:60]}'")
    return result


def confirm_entry(question: str, sql: str) -> bool:
    """Mark an entry as confirmed (thumbs-up feedback).

    Confirmed status is logged for visibility; find_similar() prefers them.
    """
    result = _update_entry(question, sql, {"confirmed": True})
    if result:
        logger.info(f"APPROVED LOG CONFIRMED: '{question[:60]}'")
    return result


def mark_stale(removed_table_names: set) -> int:
    """Mark approved queries that use any removed table as stale.

    Stale entries are still kept in the log but skipped by find_similar()
    when the tables they reference no longer exist in the schema.
    Returns count of entries marked.
    """
    if not os.path.exists(_LOG_PATH) or not removed_table_names:
        return 0

    removed_lower = {t.lower() for t in removed_table_names}
    count = 0
    lines = []

    try:
        with open(_LOG_PATH, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    lines.append(line)
                    continue
                try:
                    entry = json.loads(stripped)
                    tables = [t.lower() for t in entry.get("tables_used", [])]
                    if any(t in removed_lower for t in tables) and not entry.get("stale"):
                        entry["stale"] = True
                        count += 1
                    lines.append(json.dumps(entry, ensure_ascii=False) + "\n")
                except json.JSONDecodeError:
                    lines.append(line)

        if count > 0:
            with open(_LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(lines)
            logger.info(f"APPROVED LOG: marked {count} entries stale (removed tables: {removed_table_names})")
    except OSError as e:
        logger.error(f"mark_stale failed: {e}")
        return 0

    return count
