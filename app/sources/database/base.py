"""
DatabaseSource — shared base class for all SQL database sources.

Provides:
  - Schema file I/O (read/write schema_index.txt, tables/*.txt)
  - Table description derivation (rule-based, no AI)
  - Key column selection for schema index
  - Shared properties (name, source_type, description, get_database_name)

Each subclass (MSSQLSource, PostgreSQLSource, MySQLSource) implements:
  - connect(server, database, user, password) → (conn, driver, error)
  - execute_query(sql) → list[dict]  (async)
  - discover_schema(conn, db_name, server) → dict
  - get_system_prompt_section() → str  (dialect-specific SQL rules)
  - get_db_type() → str
  - validate_credentials() → dict
"""

import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── Table description helpers ─────────────────────────────────────────────────

_TABLE_NAME_OVERRIDES: dict = {}

_DESCRIPTION_PATTERNS = [
    (re.compile(r"project",                re.I), "Project records and tracking"),
    (re.compile(r"customer|client",        re.I), "Customer and client data"),
    (re.compile(r"order|invoice",          re.I), "Order and invoice records"),
    (re.compile(r"product|item|sku",       re.I), "Product and item catalogue"),
    (re.compile(r"employee|staff",         re.I), "Employee and staff records"),
    (re.compile(r"sales|revenue",          re.I), "Sales and revenue data"),
    (re.compile(r"target|budget|forecast", re.I), "Target, budget, and forecast data"),
    (re.compile(r"master",                 re.I), "Master reference data"),
    (re.compile(r"lookup|ref(?:erence)?",  re.I), "Reference and lookup table"),
    (re.compile(r"log|audit|history|event",re.I), "Log and audit records"),
    (re.compile(r"report|summary",         re.I), "Report and summary data"),
    (re.compile(r"payment|finance|account",re.I), "Financial and payment records"),
    (re.compile(r"status|state|stage",     re.I), "Status and stage tracking"),
    (re.compile(r"categor|type|class",     re.I), "Category and classification data"),
    (re.compile(r"contact|address|location",re.I),"Contact and address information"),
    (re.compile(r"notif|alert",            re.I), "Notifications and alerts"),
    (re.compile(r"user|admin|role|permission",re.I),"User and access management"),
]

_KEY_COL_SKIP_TYPES = frozenset({
    "image", "varbinary", "binary", "text", "ntext", "xml",
    "geography", "geometry",
})
_KEY_COL_SKIP_WORDS = (
    "attach", "file", "photo", "image", "blob", "thumb", "icon", "logo",
    "content", "body", "remark", "comment",
)
_KEY_COL_SCORE = {
    "id": 5, "code": 5, "no": 5, "num": 5, "number": 5,
    "name": 4, "title": 4,
    "status": 3, "type": 3, "stage": 3, "state": 3,
    "date": 3, "time": 3, "year": 2, "month": 2,
    "amount": 3, "total": 3, "value": 3, "price": 3, "qty": 2, "count": 2,
    "customer": 3, "client": 3, "project": 3, "item": 2, "product": 2,
    "user": 2, "owner": 2, "manager": 2, "pic": 2,
}


def _derive_table_description(table_name: str, columns: list) -> str:
    if table_name in _TABLE_NAME_OVERRIDES:
        return _TABLE_NAME_OVERRIDES[table_name]
    for pattern, description in _DESCRIPTION_PATTERNS:
        if pattern.search(table_name):
            return description
    col_names_lower = [c["name"].lower() for c in columns]
    if any("project" in n or "proj" in n for n in col_names_lower):
        return "Project-related records"
    if any("customer" in n or "client" in n for n in col_names_lower):
        return "Customer-related records"
    if any("invoice" in n or "order" in n for n in col_names_lower):
        return "Order or invoice records"
    return "Business data table"


def _score_key_column(col_name: str, col_type: str) -> int:
    base_type = col_type.split("(")[0].lower()
    if base_type in _KEY_COL_SKIP_TYPES:
        return -1
    col_lower = col_name.lower()
    if any(skip in col_lower for skip in _KEY_COL_SKIP_WORDS):
        return -1
    score = 0
    for part in re.split(r"[_\s]", col_lower):
        score += _KEY_COL_SCORE.get(part, 0)
    for keyword, pts in _KEY_COL_SCORE.items():
        if pts >= 3 and keyword in col_lower:
            score = max(score, pts)
    return score


def _select_key_columns(columns: list, max_cols: int = 5) -> list:
    scored = [(s, col["name"]) for col in columns
              if (s := _score_key_column(col["name"], col["type"])) >= 0]
    scored.sort(key=lambda x: -x[0])
    result = [name for score, name in scored if score > 0][:max_cols]
    if not result and scored:
        result = [scored[0][1]]
    return result


# ── Schema file helpers ───────────────────────────────────────────────────────

def write_schema_index(tables_data: list, schema_dir: Path) -> None:
    """Write schema_index.txt to schema_dir. One line per table."""
    lines = []
    for t in tables_data:
        desc = _derive_table_description(t["name"], t["columns"])
        key_cols = _select_key_columns(t["columns"])
        key_str = ", ".join(key_cols) if key_cols else "—"
        lines.append(
            f"{t['name']} | {t['row_count']:,} rows | {desc} | Key columns: {key_str}"
        )
    schema_dir.mkdir(parents=True, exist_ok=True)
    (schema_dir / "schema_index.txt").write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"Schema index written: {len(lines)} tables → {schema_dir}/schema_index.txt")


def write_table_file(table: dict, tables_dir: Path) -> None:
    """Write a per-table detail file to tables_dir/{TableName}.txt."""
    tables_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w\-]", "_", table["name"])
    path = tables_dir / f"{safe_name}.txt"
    lines = [
        f"TABLE: {table['name']}",
        f"Row count: {table['row_count']:,}",
        f"Columns ({len(table['columns'])}):",
    ]
    for col in table["columns"]:
        null_str = "NULL" if col["nullable"] else "NOT NULL"
        lines.append(f"  {col['name']:<35} {col['type']:<25} {null_str}")
    if table.get("categorical"):
        lines.append("Distinct values for key columns:")
        for col_name, vals in table["categorical"].items():
            lines.append(f"  {col_name}: {vals}")
    path.write_text("\n".join(lines), encoding="utf-8")


# ── DatabaseSource base ───────────────────────────────────────────────────────

class DatabaseSource:
    """Base class for all SQL database sources.

    Stores config, manages per-source schema directories, reads schema files.
    Subclasses implement: connect, execute_query, discover_schema,
    get_system_prompt_section, get_db_type, validate_credentials.
    """

    def __init__(self, name: str, config: dict):
        self._name   = name
        self._config = config
        from app.config import SOURCES_DATA_DIR
        self._schema_dir = SOURCES_DATA_DIR / name   # data/sources/{name}/

    # ── DataSource protocol properties ────────────────────────────────────────

    @property
    def name(self) -> str:
        return self._name

    @property
    def source_type(self) -> str:
        return self._config.get("type", "unknown")

    @property
    def description(self) -> str:
        return self._config.get(
            "description",
            f"{self.source_type.upper()} database '{self.get_database_name()}'"
        )

    # ── Schema file reads ─────────────────────────────────────────────────────

    def get_table_index(self) -> str:
        path = self._schema_dir / "schema_index.txt"
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
        except Exception as exc:
            logger.warning(f"[{self._name}] get_table_index failed: {exc}")
            return ""

    def get_compact_index(self) -> str:
        return self.get_table_index()

    def get_table_detail(self, table_name: str) -> Optional[str]:
        tables_dir = self._schema_dir / "tables"
        if not tables_dir.is_dir():
            return None
        safe = re.sub(r"[^\w\-]", "_", table_name)
        for fname in (f"{safe}.txt", f"{table_name}.txt"):
            p = tables_dir / fname
            if p.exists():
                return p.read_text(encoding="utf-8")
        try:
            target = table_name.lower() + ".txt"
            for fname in os.listdir(tables_dir):
                if fname.lower() == target:
                    return (tables_dir / fname).read_text(encoding="utf-8")
        except Exception:
            pass
        return None

    def get_available_tables(self) -> list[str]:
        tables_dir = self._schema_dir / "tables"
        if not tables_dir.is_dir():
            return []
        return [p.stem for p in tables_dir.glob("*.txt")]

    def schema_discovered(self) -> bool:
        return (self._schema_dir / "schema_index.txt").exists()

    # ── Subclass must implement ───────────────────────────────────────────────

    def get_database_name(self) -> str:
        return self._config.get("credentials", {}).get("database", self._name)

    def get_db_type(self) -> str:
        return self.source_type

    def get_system_prompt_section(self) -> str:
        raise NotImplementedError

    async def execute_query(self, sql: str) -> list[dict]:
        raise NotImplementedError

    def discover_schema(self, conn, db_name: str, server: str) -> dict:
        raise NotImplementedError

    def validate_credentials(self) -> dict:
        raise NotImplementedError
