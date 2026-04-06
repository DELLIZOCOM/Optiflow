"""
SQL Server connector — implements DatabaseConnector for Microsoft SQL Server.

Also exposes module-level get_db_connection() and execute_query() wrappers
for backward compatibility with service modules.
"""

import logging
import os
import re
import time

from backend.connectors.base import DatabaseConnector
from backend.config.paths import (
    SCHEMA_CONTEXT_PATH,
    SCHEMA_INDEX_PATH,
    TABLES_DIR,
)

logger = logging.getLogger(__name__)


class MSSQLConnector(DatabaseConnector):
    """SQL Server connection and introspection using pyodbc."""

    def connect(self, server: str, database: str, user: str, password: str):
        """Try ODBC 18 then 17. Returns (conn, driver, error)."""
        try:
            import pyodbc
        except ImportError:
            return None, None, "pyodbc is not installed — run: pip install pyodbc"

        def _clean_error(exc, server=server, database=database):
            sqlstate = str(exc.args[0]).upper() if exc.args else ""
            raw = str(exc)
            low = raw.lower()
            if "cannot open database" in low:
                return f"Database '{database}' not found on server."
            if sqlstate == "28000" or "login failed" in low:
                return "Authentication failed. Check username and password."
            if (sqlstate == "08001"
                    or "tcp provider" in low
                    or "named pipes provider" in low
                    or "network-related" in low
                    or "no connection could be made" in low
                    or "connection refused" in low):
                return (
                    f"Cannot reach server at '{server}'. "
                    "Check the address and make sure you're on the same network."
                )
            if sqlstate == "HYT00" or "timeout expired" in low or "login timeout" in low:
                return (
                    f"Server at '{server}' did not respond within 10 seconds. "
                    "Check firewall and network."
                )
            if sqlstate in ("IM002", "IM004") or (
                "driver" in low and (
                    "not found" in low
                    or "data source name not found" in low
                    or "specified driver could not be loaded" in low
                )
            ):
                return "ODBC Driver not installed. Run: brew install msodbcsql18"
            parts = raw.rsplit("]", 1)
            return parts[-1].strip(" ()") if len(parts) > 1 else raw

        last_error = "No ODBC drivers found. Install ODBC Driver 17 or 18 for SQL Server."
        for driver in ["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"]:
            conn_str = (
                f"DRIVER={{{driver}}};"
                f"SERVER={server};DATABASE={database};"
                f"UID={user};PWD={password};"
                "TrustServerCertificate=yes;"
            )
            try:
                import pyodbc as _pyodbc
                conn = _pyodbc.connect(conn_str, timeout=10)
                return conn, driver, None
            except Exception as e:
                last_error = _clean_error(e)

        return None, None, last_error

    def execute_query(self, sql: str, params: list = None) -> list:
        """Execute a query using current credentials. Retry up to 3 times."""
        from backend.config.loader import load_db_config
        cfg = load_db_config()
        conn = None
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                import pyodbc
                conn_str = (
                    f"DRIVER={{ODBC Driver 18 for SQL Server}};"
                    f"SERVER={cfg['server']},1433;"
                    f"DATABASE={cfg['database']};"
                    f"UID={cfg['user']};"
                    f"PWD={cfg['password']};"
                    "TrustServerCertificate=yes;"
                    "Encrypt=optional"
                )
                conn = pyodbc.connect(conn_str, timeout=10)
                cursor = conn.cursor()
                if params:
                    cursor.execute(sql, params)
                else:
                    cursor.execute(sql)
                columns = [col[0] for col in cursor.description]
                results = [dict(zip(columns, row)) for row in cursor.fetchall()]
                return results
            except Exception as e:
                logger.warning(f"Query attempt {attempt} failed: {e}")
                if attempt < max_retries:
                    time.sleep(2)
                else:
                    raise
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass

    def discover_schema(self, conn, db_name: str, server: str) -> dict:
        """Run full schema discovery. Writes schema files and returns tables dict."""
        return run_schema_discovery(conn, db_name, server)

    def check_permissions(self, conn) -> dict:
        """Check DB user access level via sys tables."""
        return verify_readonly_access(conn)

    def test_connection(self, server: str, database: str, user: str, password: str) -> dict:
        conn, driver, error = self.connect(server, database, user, password)
        if conn:
            try:
                conn.close()
            except Exception:
                pass
            return {"success": True}
        return {"success": False, "error": error}

    def close(self):
        pass


# ── Module-level wrappers for backward compat ─────────────────────────────────

def get_db_connection(server: str, database: str, user: str, password: str):
    """Returns (conn, driver, error)."""
    return MSSQLConnector().connect(server, database, user, password)


def execute_query(sql: str, params=None) -> list:
    """Execute SQL using configured DB credentials."""
    return MSSQLConnector().execute_query(sql, params)


# ── Schema discovery ──────────────────────────────────────────────────────────

def run_schema_discovery(conn, db_name: str, server: str) -> dict:
    """Discover all tables, columns, row counts, categorical values.

    Writes schema_context.txt, schema_index.txt, and per-table files.
    Returns {"db_name", "server", "tables": [...]}
    """
    from backend.services.schema_manager import (
        _write_schema_index,
        _write_table_file,
    )

    cursor = conn.cursor()
    cursor.execute("""
        SELECT TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_TYPE = 'BASE TABLE'
          AND TABLE_NAME NOT LIKE 'sys%'
        ORDER BY TABLE_NAME
    """)
    table_names = [row[0] for row in cursor.fetchall()]

    tables_data = []
    file_lines = [
        f"DATABASE SCHEMA: {db_name}",
        f"Server: {server}  |  Auto-generated from live database",
        "=" * 78,
        "",
        f"TABLES ({len(table_names)} total): {', '.join(table_names)}",
        "",
    ]

    for table_name in table_names:
        try:
            cursor.execute(f"SELECT COUNT(*) FROM [{table_name}]")
            row_count = cursor.fetchone()[0]
        except Exception:
            row_count = 0

        cursor.execute("""
            SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = ?
            ORDER BY ORDINAL_POSITION
        """, table_name)
        raw_cols = cursor.fetchall()

        columns = []
        categorical_candidates = []
        for col_name, data_type, max_len, nullable in raw_cols:
            if max_len == -1:
                full_type = f"{data_type}(-1)"
            elif max_len:
                full_type = f"{data_type}({max_len})"
            else:
                full_type = data_type
            columns.append({
                "name":     col_name,
                "type":     full_type,
                "nullable": nullable == "YES",
            })
            if data_type in ("varchar", "nvarchar") and max_len and 0 < max_len <= 100:
                categorical_candidates.append(col_name)

        categorical: dict = {}
        for col_name in categorical_candidates:
            try:
                cursor.execute(f"""
                    SELECT DISTINCT TOP 31 [{col_name}]
                    FROM [{table_name}]
                    WHERE [{col_name}] IS NOT NULL
                    ORDER BY [{col_name}]
                """)
                vals = [str(r[0]) for r in cursor.fetchall()]
                if len(vals) <= 30:
                    categorical[col_name] = vals
            except Exception:
                pass

        tables_data.append({
            "name":        table_name,
            "row_count":   row_count,
            "columns":     columns,
            "categorical": categorical,
        })

        file_lines.append("─" * 78)
        file_lines.append(f"TABLE: {table_name}")
        file_lines.append(f"  Row count: {row_count}")
        file_lines.append(f"  Columns ({len(columns)}):")
        for col in columns:
            null_str = "NULL" if col["nullable"] else "NOT NULL"
            file_lines.append(f"    {col['name']:<35} {col['type']:<25} {null_str}")
        if categorical:
            file_lines.append("  Distinct values for key columns:")
            for col_name, vals in categorical.items():
                file_lines.append(f"    {col_name}: {vals}")
        file_lines.append("")

    SCHEMA_CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SCHEMA_CONTEXT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(file_lines))
    logger.info(f"Schema discovery complete: {len(table_names)} tables → data/prompts/schema_context.txt")

    try:
        _write_schema_index(tables_data)
        for t in tables_data:
            _write_table_file(t)
        logger.info(f"Per-table files written: {len(tables_data)} → data/prompts/tables/")
    except Exception as exc:
        logger.warning(f"Split schema generation failed (non-fatal): {exc}")

    return {
        "db_name": db_name,
        "server":  server,
        "tables":  tables_data,
    }


# ── Permission check ──────────────────────────────────────────────────────────

_BLOCKED_PERMS = frozenset({"CONTROL"})
_WARN_ROLES    = frozenset({"db_owner", "db_datawriter", "db_ddladmin"})
_WARN_PERMS    = frozenset({"INSERT", "UPDATE", "DELETE", "ALTER", "DROP", "TRUNCATE"})


def verify_readonly_access(conn) -> dict:
    """Query sys tables to determine the access level of the current DB user."""
    try:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT dp.permission_name
            FROM sys.database_permissions dp
            JOIN sys.database_principals pr
              ON dp.grantee_principal_id = pr.principal_id
            WHERE pr.name = CURRENT_USER
              AND dp.state_desc IN ('GRANT', 'GRANT_WITH_GRANT_OPTION')
        """)
        permissions = [row[0].upper() for row in cursor.fetchall()]

        cursor.execute("""
            SELECT r.name AS role_name
            FROM sys.database_role_members rm
            JOIN sys.database_principals r
              ON rm.role_principal_id = r.principal_id
            JOIN sys.database_principals u
              ON rm.member_principal_id = u.principal_id
            WHERE u.name = CURRENT_USER
        """)
        roles = [row[0].lower() for row in cursor.fetchall()]

        is_sysadmin = False
        try:
            cursor.execute("SELECT IS_SRVROLEMEMBER('sysadmin')")
            val = cursor.fetchone()
            is_sysadmin = bool(val and val[0] == 1)
        except Exception:
            pass

    except Exception as exc:
        logger.warning(f"Permission check failed (sys view access restricted): {exc}")
        return {
            "access_level": "unknown",
            "permissions":  [],
            "roles":        [],
            "message":      "Could not verify permissions. Make sure this is a read-only user.",
            "warnings":     ["sys view access restricted — unable to verify permissions"],
        }

    blocked_p = _BLOCKED_PERMS & set(permissions)
    if is_sysadmin or blocked_p:
        reason = (
            "sysadmin server role" if is_sysadmin
            else f"{', '.join(sorted(blocked_p))} permission"
        )
        return {
            "access_level": "blocked",
            "permissions":  permissions,
            "roles":        roles,
            "message":      (
                f"User has {reason}. "
                "OptiFlow cannot connect with this level of access. "
                "Please create a read-only user."
            ),
            "warnings":     [f"BLOCKED: {reason}"],
        }

    warn_r = _WARN_ROLES & set(roles)
    warn_p = _WARN_PERMS & set(permissions)
    if warn_r or warn_p:
        found = sorted(warn_r | warn_p)
        return {
            "access_level": "warning",
            "permissions":  permissions,
            "roles":        roles,
            "message":      (
                f"User has write permissions: {', '.join(found)}. "
                "OptiFlow requires a read-only database user."
            ),
            "warnings":     [f"Write-level access detected: {', '.join(found)}"],
        }

    return {
        "access_level": "readonly",
        "permissions":  permissions,
        "roles":        roles,
        "message":      "Read-only access confirmed. This user can only read data.",
        "warnings":     [],
    }
