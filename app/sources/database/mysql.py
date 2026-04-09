"""
MySQLSource — stub. Implement when adding MySQL/MariaDB support.

Dialect notes preserved so the architecture is clear.
"""

from app.sources.database.base import DatabaseSource

_MYSQL_DIALECT_NOTES = """\
## MySQL Dialect Rules

- Row limit syntax: `SELECT col1, col2 FROM ... LIMIT 100` (LIMIT at end)
- Identifier quoting: backticks — `table_name`.`column_name`
- Current timestamp: `NOW()`  |  Today's date: `CURDATE()`
- NULL handling: `IFNULL(col, fallback)`
- String concat: `CONCAT(col1, ' ', col2)`\
"""


class MySQLSource(DatabaseSource):
    """MySQL/MariaDB data source (stub — not yet implemented)."""

    def get_db_type(self) -> str:
        return "mysql"

    def get_system_prompt_section(self) -> str:
        return f"{_MYSQL_DIALECT_NOTES}\n(Applies to source: **{self.name}**)"

    def connect(self, **kwargs):
        raise NotImplementedError("MySQL connector not yet implemented.")

    async def execute_query(self, sql: str) -> list[dict]:
        raise NotImplementedError("MySQL connector not yet implemented.")

    def discover_schema(self, conn, db_name: str, server: str) -> dict:
        raise NotImplementedError("MySQL connector not yet implemented.")

    def validate_credentials(self) -> dict:
        raise NotImplementedError("MySQL connector not yet implemented.")
