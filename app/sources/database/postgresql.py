"""
PostgreSQLSource — stub. Implement when adding PostgreSQL support.

Dialect notes preserved so the architecture is clear.
"""

from app.sources.database.base import DatabaseSource

_POSTGRESQL_DIALECT_NOTES = """\
## PostgreSQL Dialect Rules

- Row limit syntax: `SELECT col1, col2 FROM ... LIMIT 100` (LIMIT at end)
- Identifier quoting: double-quotes — `"TableName"."ColumnName"`
- Current timestamp: `NOW()`  |  Today's date: `CURRENT_DATE`
- NULL handling: `COALESCE(col, fallback)`
- String concat: `col1 || ' ' || col2`
- Case-insensitive LIKE: `ILIKE '%value%'`
- Type casting: `col::NUMERIC`, `col::TEXT`, `CAST(col AS DECIMAL)`\
"""


class PostgreSQLSource(DatabaseSource):
    """PostgreSQL data source (stub — not yet implemented)."""

    def get_db_type(self) -> str:
        return "postgresql"

    def get_system_prompt_section(self) -> str:
        return f"{_POSTGRESQL_DIALECT_NOTES}\n(Applies to source: **{self.name}**)"

    def connect(self, **kwargs):
        raise NotImplementedError("PostgreSQL connector not yet implemented.")

    async def execute_query(self, sql: str) -> list[dict]:
        raise NotImplementedError("PostgreSQL connector not yet implemented.")

    def discover_schema(self, conn, db_name: str, server: str) -> dict:
        raise NotImplementedError("PostgreSQL connector not yet implemented.")

    def validate_credentials(self) -> dict:
        raise NotImplementedError("PostgreSQL connector not yet implemented.")
