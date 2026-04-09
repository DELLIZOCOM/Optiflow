"""
DataSource protocol and SourceRegistry.

Every connected data source (database, email, knowledge base) implements
the DataSource protocol. The SourceRegistry holds all live sources and
provides helpers for building system prompts and collecting tools.
"""

import logging
from typing import Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class DataSource(Protocol):
    """
    A connected data source.

    Each source:
    - Knows its own name, type, and description
    - Reads its own schema files (table index + per-table details)
    - Executes queries asynchronously
    - Provides its own system prompt section (dialect notes, etc.)
    """

    @property
    def name(self) -> str:
        """User-given identifier, e.g. 'sales_db'. Used as the source parameter in tools."""
        ...

    @property
    def source_type(self) -> str:
        """Type identifier: 'mssql', 'postgresql', 'mysql', etc."""
        ...

    @property
    def description(self) -> str:
        """Human-readable description of what this source contains."""
        ...

    def get_table_index(self) -> str:
        """One-line-per-table summary for list_tables tool."""
        ...

    def get_compact_index(self) -> str:
        """Same as get_table_index — used by the system prompt builder."""
        ...

    def get_table_detail(self, table_name: str) -> Optional[str]:
        """Full per-table schema text for get_table_schema tool."""
        ...

    def get_database_name(self) -> str:
        """Actual database name (used in tool headers)."""
        ...

    def get_db_type(self) -> str:
        """Database dialect: 'mssql', 'postgresql', 'mysql'."""
        ...

    def get_system_prompt_section(self) -> str:
        """Dialect-specific rules and notes to inject into the system prompt."""
        ...

    async def execute_query(self, sql: str) -> list[dict]:
        """Execute a read-only SQL query and return rows as list of dicts."""
        ...


class SourceRegistry:
    """Holds all connected data sources."""

    def __init__(self):
        self._sources: dict[str, DataSource] = {}

    def register(self, source: DataSource) -> None:
        self._sources[source.name] = source
        logger.info(f"SourceRegistry: registered source '{source.name}' ({source.source_type})")

    def get(self, name: str) -> Optional[DataSource]:
        return self._sources.get(name)

    def get_all(self) -> list[DataSource]:
        return list(self._sources.values())

    def remove(self, name: str) -> None:
        if name in self._sources:
            del self._sources[name]
            logger.info(f"SourceRegistry: removed source '{name}'")

    def names(self) -> list[str]:
        return list(self._sources.keys())

    def build_system_prompt_context(self) -> str:
        """
        Combine all source sections into a block for the system prompt.
        Returns an empty string if no sources are registered.
        """
        sources = self.get_all()
        if not sources:
            return ""

        parts = ["## Connected Data Sources\n"]
        for source in sources:
            parts.append(f"### {source.name}  ({source.source_type.upper()})")
            parts.append(source.description)
            index = source.get_compact_index()
            if index:
                parts.append(f"\nAvailable tables:\n{index}")
            parts.append("")

        # Dialect notes per source
        for source in sources:
            section = source.get_system_prompt_section()
            if section:
                parts.append(section)

        return "\n".join(parts)
