"""
Adapters: Protocol interfaces + concrete implementations that bridge v1 code.

The agent tools depend on these Protocols, not on concrete v1 classes.
This keeps the agent module decoupled from v1 internals.

Protocols:
  DatabaseConnector  — async execute_query, db type/name
  SchemaProvider     — read schema_index.txt and per-table files
  KnowledgeProvider  — read company.md

Implementations:
  MSSQLAdapter         — wraps v1 mssql.execute_query (sync → async)
  FileSchemaProvider   — reads from data/prompts/
  FileKnowledgeProvider — reads from data/knowledge/
"""

import asyncio
import logging
import os
import re
from typing import Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ── Protocols (structural typing) ─────────────────────────────────────────────

@runtime_checkable
class DatabaseConnector(Protocol):
    async def execute_query(self, sql: str) -> list[dict]: ...
    def get_db_type(self) -> str: ...
    def get_database_name(self) -> str: ...


@runtime_checkable
class SchemaProvider(Protocol):
    def get_table_index(self) -> str: ...
    def get_table_detail(self, table_name: str) -> Optional[str]: ...
    def get_available_tables(self) -> list[str]: ...


@runtime_checkable
class KnowledgeProvider(Protocol):
    def get_company_context(self) -> str: ...
    def get_context_for_topic(self, topic: str) -> Optional[str]: ...


# ── DatabaseConnector implementation ──────────────────────────────────────────

class MSSQLAdapter:
    """Wraps the v1 MSSQL connector. Converts blocking execute_query → async."""

    def get_db_type(self) -> str:
        return "mssql"

    def get_database_name(self) -> str:
        from backend.config.loader import load_db_config
        return load_db_config().get("database", "Unknown")

    async def execute_query(self, sql: str) -> list[dict]:
        """Run a SQL query in a thread-pool executor to avoid blocking the event loop."""
        from backend.connectors.mssql import execute_query as _sync_execute
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _sync_execute, sql)


# ── SchemaProvider implementation ─────────────────────────────────────────────

class FileSchemaProvider:
    """Reads schema files written by v1 setup wizard (schema_index.txt, tables/*.txt)."""

    def __init__(self, prompts_dir: str):
        self._dir = prompts_dir

    def get_table_index(self) -> str:
        path = os.path.join(self._dir, "schema_index.txt")
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""
        except Exception as exc:
            logger.warning(f"FileSchemaProvider: could not read schema_index.txt: {exc}")
            return ""

    def get_table_detail(self, table_name: str) -> Optional[str]:
        tables_dir = os.path.join(self._dir, "tables")
        if not os.path.isdir(tables_dir):
            return None

        # Sanitise name for filesystem
        safe = re.sub(r"[^\w\-]", "_", table_name)

        # Exact-match candidates
        for fname in (f"{safe}.txt", f"{table_name}.txt"):
            path = os.path.join(tables_dir, fname)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    return f.read()

        # Case-insensitive fallback
        try:
            target = table_name.lower() + ".txt"
            for fname in os.listdir(tables_dir):
                if fname.lower() == target:
                    with open(os.path.join(tables_dir, fname), encoding="utf-8") as f:
                        return f.read()
        except Exception:
            pass

        return None

    def get_available_tables(self) -> list[str]:
        tables_dir = os.path.join(self._dir, "tables")
        if not os.path.isdir(tables_dir):
            return []
        return [
            os.path.splitext(f)[0]
            for f in os.listdir(tables_dir)
            if f.endswith(".txt")
        ]


# ── KnowledgeProvider implementation ──────────────────────────────────────────

class FileKnowledgeProvider:
    """Reads company.md from the v1 knowledge directory."""

    def __init__(self, knowledge_dir: str):
        self._dir = knowledge_dir

    def get_company_context(self) -> str:
        path = os.path.join(self._dir, "company.md")
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        except FileNotFoundError:
            return ""
        except Exception as exc:
            logger.warning(f"FileKnowledgeProvider: could not read company.md: {exc}")
            return ""

    def get_context_for_topic(self, topic: str) -> Optional[str]:
        # Return the full context regardless of topic — the model extracts what it needs.
        ctx = self.get_company_context()
        return ctx if ctx else None
