"""
Schema loading helpers — file I/O for schema context, index, and per-table files.

Extracted from services/sql_generator.py to keep that module under 200 lines.
"""

import logging
import os
import re

from backend.config.paths import (
    SCHEMA_CONTEXT_PATH,
    SCHEMA_INDEX_PATH,
    TABLES_DIR,
    COMPANY_MD_PATH,
)

logger = logging.getLogger(__name__)


def load_schema() -> str:
    """Read the full schema_context.txt. Raises FileNotFoundError if absent."""
    if not SCHEMA_CONTEXT_PATH.exists():
        raise FileNotFoundError(
            f"Schema context file not found: {SCHEMA_CONTEXT_PATH}\n"
            "Run setup → Discover Schema to populate it."
        )
    with open(SCHEMA_CONTEXT_PATH, encoding="utf-8") as f:
        return f.read()


def load_company_knowledge() -> str:
    """Read company.md. Returns empty string if absent or unreadable."""
    if not COMPANY_MD_PATH.exists():
        return ""
    try:
        with open(COMPANY_MD_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except Exception as exc:
        logger.warning(f"Could not load company.md: {exc}")
        return ""


def load_schema_index() -> str | None:
    """Read schema_index.txt. Returns None if absent."""
    if not SCHEMA_INDEX_PATH.exists():
        return None
    try:
        with open(SCHEMA_INDEX_PATH, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def load_table_schemas(table_names: list) -> str:
    """Read per-table .txt files for the given table names, joined by blank lines."""
    if not TABLES_DIR.exists():
        return ""

    available: dict = {}
    try:
        for fname in os.listdir(TABLES_DIR):
            if fname.endswith(".txt"):
                available[fname[:-4].lower()] = TABLES_DIR / fname
    except Exception:
        return ""

    parts = []
    for name in table_names:
        safe = re.sub(r"[^\w\-]", "_", name)
        path = available.get(safe.lower()) or available.get(name.lower())
        if path:
            try:
                with open(path, encoding="utf-8") as f:
                    parts.append(f.read())
            except Exception:
                pass

    return "\n\n".join(parts)


def extract_use_when_sections(company_md: str) -> str:
    """Extract 'Use when asked about' lines from company.md for table-selection hints."""
    if not company_md:
        return ""
    lines = []
    current_table = None
    for line in company_md.splitlines():
        if line.startswith("### "):
            current_table = line[4:].split("(")[0].strip()
        elif current_table and line.strip().lower().startswith("**use when asked about:**"):
            phrase = line.split(":**", 1)[-1].strip().lstrip("*").strip()
            if phrase:
                lines.append(f"{current_table}: {phrase}")
    return "\n".join(lines)
