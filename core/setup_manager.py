"""
Setup Manager — shared logic for database connection, schema discovery, and config.

Used by:
  - setup.py          (CLI wizard)
  - app.py            (web wizard endpoints  GET/POST /setup/*)
"""

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── Split-schema helpers ───────────────────────────────────────────────────────
# These generate schema_index.txt (one line/table) and per-table detail files
# used by the two-step SQL generation in agent_sql_generator.py.

_TABLE_NAME_OVERRIDES: dict = {
    # Add project-specific human descriptions here, keyed by exact table name.
    # Example: "ProSt": "Project pipeline and status tracking",
}

_DESCRIPTION_PATTERNS: list = [
    # (compiled_pattern, description) — matched against table name, case-insensitive.
    (re.compile(r"project",              re.I), "Project records and tracking"),
    (re.compile(r"customer|client",      re.I), "Customer and client data"),
    (re.compile(r"order|invoice",        re.I), "Order and invoice records"),
    (re.compile(r"product|item|sku",     re.I), "Product and item catalogue"),
    (re.compile(r"employee|staff",       re.I), "Employee and staff records"),
    (re.compile(r"sales|revenue",        re.I), "Sales and revenue data"),
    (re.compile(r"target|budget|forecast", re.I), "Target, budget, and forecast data"),
    (re.compile(r"master",               re.I), "Master reference data"),
    (re.compile(r"lookup|ref(?:erence)?",re.I), "Reference and lookup table"),
    (re.compile(r"log|audit|history|event", re.I), "Log and audit records"),
    (re.compile(r"report|summary",       re.I), "Report and summary data"),
    (re.compile(r"payment|finance|account", re.I), "Financial and payment records"),
    (re.compile(r"status|state|stage",   re.I), "Status and stage tracking"),
    (re.compile(r"categor|type|class",   re.I), "Category and classification data"),
    (re.compile(r"contact|address|location", re.I), "Contact and address information"),
    (re.compile(r"notif|alert",          re.I), "Notifications and alerts"),
    (re.compile(r"user|admin|role|permission", re.I), "User and access management"),
]

_KEY_COL_SKIP_TYPES: frozenset = frozenset({
    "image", "varbinary", "binary", "text", "ntext", "xml",
    "geography", "geometry",
})

_KEY_COL_SKIP_WORDS: tuple = (
    "attach", "file", "photo", "image", "blob", "thumb", "icon", "logo",
    "content", "body", "remark", "comment",
)

# Keyword → score for key column selection (higher = more important to show in index)
_KEY_COL_SCORE: dict = {
    "id": 5, "code": 5, "no": 5, "num": 5, "number": 5,
    "name": 4, "title": 4,
    "status": 3, "type": 3, "stage": 3, "state": 3,
    "date": 3, "time": 3, "year": 2, "month": 2,
    "amount": 3, "total": 3, "value": 3, "price": 3, "qty": 2, "count": 2,
    "customer": 3, "client": 3, "project": 3, "item": 2, "product": 2,
    "user": 2, "owner": 2, "manager": 2, "pic": 2,
}


def _derive_table_description(table_name: str, columns: list) -> str:
    """Return a human-readable one-line description for a table without AI."""
    if table_name in _TABLE_NAME_OVERRIDES:
        return _TABLE_NAME_OVERRIDES[table_name]

    for pattern, description in _DESCRIPTION_PATTERNS:
        if pattern.search(table_name):
            return description

    # Column-signature fallback
    col_names_lower = [c["name"].lower() for c in columns]
    if any("project" in n or "proj" in n for n in col_names_lower):
        return "Project-related records"
    if any("customer" in n or "client" in n for n in col_names_lower):
        return "Customer-related records"
    if any("invoice" in n or "order" in n for n in col_names_lower):
        return "Order or invoice records"
    if any("employee" in n or "staff" in n for n in col_names_lower):
        return "Employee or staff records"

    return "Business data table"


def _score_key_column(col_name: str, col_type: str) -> int:
    """Score how important a column is for the schema index. -1 means skip."""
    base_type = col_type.split("(")[0].lower()
    if base_type in _KEY_COL_SKIP_TYPES:
        return -1

    col_lower = col_name.lower()
    if any(skip in col_lower for skip in _KEY_COL_SKIP_WORDS):
        return -1

    score = 0
    parts = re.split(r"[_\s]", col_lower)
    for part in parts:
        score += _KEY_COL_SCORE.get(part, 0)

    # Also check for keywords embedded in compound names (e.g. "ProjectCode")
    for keyword, pts in _KEY_COL_SCORE.items():
        if pts >= 3 and keyword in col_lower:
            score = max(score, pts)

    return score


def _select_key_columns(columns: list, max_cols: int = 5) -> list:
    """Return up to max_cols column names that best represent the table."""
    scored = [(s, col["name"]) for col in columns
              if (s := _score_key_column(col["name"], col["type"])) >= 0]
    scored.sort(key=lambda x: -x[0])
    result = [name for score, name in scored if score > 0][:max_cols]
    if not result and scored:
        result = [scored[0][1]]
    return result


def _write_schema_index(tables_data: list) -> None:
    """Write prompts/schema_index.txt — one line per table."""
    index_path = os.path.join(_ROOT, "prompts", "schema_index.txt")
    lines = []
    for t in tables_data:
        desc = _derive_table_description(t["name"], t["columns"])
        key_cols = _select_key_columns(t["columns"])
        key_str = ", ".join(key_cols) if key_cols else "—"
        lines.append(f"{t['name']} | {t['row_count']:,} rows | {desc} | Key columns: {key_str}")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    logger.info(f"Schema index written: {len(lines)} tables → prompts/schema_index.txt")


def _write_table_file(table: dict) -> None:
    """Write a per-table detail file to prompts/tables/{TableName}.txt."""
    tables_dir = os.path.join(_ROOT, "prompts", "tables")
    os.makedirs(tables_dir, exist_ok=True)

    safe_name = re.sub(r"[^\w\-]", "_", table["name"])
    path = os.path.join(tables_dir, f"{safe_name}.txt")

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

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ── Status check ─────────────────────────────────────────────────────────────

def is_setup_complete() -> bool:
    """Return True when both the schema file and AI config exist.

    Both conditions must be met:
    1. prompts/schema_context.txt  — DB was discovered
    2. config/model_config.json with cloud_provider key — AI was configured

    Existing installs that have the schema but not the new model_config are
    redirected to the setup wizard to complete the AI Provider step.
    """
    schema_path = os.path.join(_ROOT, "prompts", "schema_context.txt")
    if not os.path.exists(schema_path):
        return False

    model_cfg_path = os.path.join(_ROOT, "config", "model_config.json")
    if os.path.exists(model_cfg_path):
        try:
            with open(model_cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
            if "cloud_provider" in cfg:
                return True
        except Exception:
            pass

    return False


# ── Credentials ───────────────────────────────────────────────────────────────

def save_db_credentials(server: str, database: str, user: str, password: str) -> None:
    """Save DB credentials to config/db_config.json (encrypted password)."""
    from config.loader import save_db_config
    save_db_config({
        "server":   server,
        "database": database,
        "user":     user,
        "password": password,
    })


# ── Connection ────────────────────────────────────────────────────────────────

def get_db_connection(server: str, database: str, user: str, password: str):
    """Try to connect to SQL Server.

    Returns (conn, driver_name, error_message).
    conn is None if connection failed.
    Tries ODBC Driver 18 then 17.
    """
    try:
        import pyodbc
    except ImportError:
        return None, None, "pyodbc is not installed — run: pip install pyodbc"

    def _clean_error(exc: Exception) -> str:
        """Map a pyodbc exception to a user-friendly message.

        pyodbc stores the SQLSTATE in exc.args[0] — use that first, then fall
        back to message-text patterns for cases where the SQLSTATE is ambiguous.

        Common SQL Server SQLSTATE codes:
          28000  login failed (wrong user/password, or wrong DB name via login path)
          08001  TCP/network: cannot reach server
          HYT00  timeout expired
          IM002  ODBC driver not found (no drivers installed)
          IM004  driver load error
        """
        sqlstate = str(exc.args[0]).upper() if exc.args else ""
        raw = str(exc)
        low = raw.lower()

        # "Cannot open database" appears inside a 28000 message when DB name is wrong.
        # Check this before the generic 28000 → auth-failure branch.
        if "cannot open database" in low:
            return f"Database '{database}' not found on server."

        # 28000: authentication failure (wrong username or password)
        if sqlstate == "28000" or "login failed" in low:
            return "Authentication failed. Check username and password."

        # 08001: network unreachable / wrong host
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

        # HYT00: connection timed out (also covers some 08001 timeout variants)
        if sqlstate == "HYT00" or "timeout expired" in low or "login timeout" in low:
            return (
                f"Server at '{server}' did not respond within 10 seconds. "
                "Check firewall and network."
            )

        # IM002 / IM004: ODBC driver not installed
        if sqlstate in ("IM002", "IM004") or (
            "driver" in low and (
                "not found" in low
                or "data source name not found" in low
                or "specified driver could not be loaded" in low
            )
        ):
            return "ODBC Driver not installed. Run: brew install msodbcsql18"

        # Fallback: strip verbose [code] prefixes, keep the last human-readable part
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


# ── Schema discovery ──────────────────────────────────────────────────────────

def run_schema_discovery(conn, db_name: str, server: str) -> dict:
    """Discover all tables, columns, row counts, and categorical values.

    Saves the schema to prompts/schema_context.txt.

    Returns a dict:
        {
            "db_name": str,
            "server":  str,
            "tables":  [
                {
                    "name":        str,
                    "row_count":   int,
                    "columns":     [{"name", "type", "nullable"}, ...],
                    "categorical": {"ColumnName": ["Val1", "Val2", ...], ...}
                },
                ...
            ]
        }
    """
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
        # Row count
        try:
            cursor.execute(f"SELECT COUNT(*) FROM [{table_name}]")
            row_count = cursor.fetchone()[0]
        except Exception:
            row_count = 0

        # Columns
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

        # Enumerate distinct values for short string columns.
        # Use TOP 31 in a single query: if we get ≤30 values, they're all distinct;
        # if we get 31, there are too many to enumerate — skip.
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

        # Schema file lines
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

    # Write schema file
    schema_path = os.path.join(_ROOT, "prompts", "schema_context.txt")
    os.makedirs(os.path.dirname(schema_path), exist_ok=True)
    with open(schema_path, "w", encoding="utf-8") as f:
        f.write("\n".join(file_lines))

    logger.info(f"Schema discovery complete: {len(table_names)} tables → prompts/schema_context.txt")

    # Write split-schema files for two-step SQL generation
    try:
        _write_schema_index(tables_data)
        for t in tables_data:
            _write_table_file(t)
        logger.info(f"Per-table files written: {len(tables_data)} → prompts/tables/")
    except Exception as exc:
        logger.warning(f"Split schema generation failed (non-fatal): {exc}")

    return {
        "db_name": db_name,
        "server":  server,
        "tables":  tables_data,
    }


# ── Permission check ──────────────────────────────────────────────────────────

# Permissions that mean the user can control the entire DB — block these.
_BLOCKED_PERMS = frozenset({"CONTROL"})

# Database roles that grant write access — warn on these.
_WARN_ROLES = frozenset({"db_owner", "db_datawriter", "db_ddladmin"})

# Individual permissions that allow writes — warn on these.
_WARN_PERMS = frozenset({"INSERT", "UPDATE", "DELETE", "ALTER", "DROP", "TRUNCATE"})


def verify_readonly_access(conn) -> dict:
    """Query sys tables to determine the access level of the current DB user.

    Never performs write operations.  Returns a dict:
        {
            "access_level": "readonly" | "warning" | "blocked" | "unknown",
            "permissions":  list[str],
            "roles":        list[str],
            "message":      str,
            "warnings":     list[str],
        }

    Returns access_level="unknown" if the sys table queries fail (some
    locked-down servers restrict access to sys views) — this never raises.
    """
    try:
        cursor = conn.cursor()

        # 1. Direct permissions granted to this user
        cursor.execute("""
            SELECT dp.permission_name
            FROM sys.database_permissions dp
            JOIN sys.database_principals pr
              ON dp.grantee_principal_id = pr.principal_id
            WHERE pr.name = CURRENT_USER
              AND dp.state_desc IN ('GRANT', 'GRANT_WITH_GRANT_OPTION')
        """)
        permissions = [row[0].upper() for row in cursor.fetchall()]

        # 2. Database role membership
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

        # 3. Server-level sysadmin check (function call, not a sys view — rarely restricted)
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

    # ── Classify ──────────────────────────────────────────────────────────────

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


# ── Security config ────────────────────────────────────────────────────────────

_SECURITY_PATH = os.path.join(_ROOT, "config", "security.json")


def save_security_config(result: dict, db_user: str) -> None:
    """Persist the permission check result to config/security.json."""
    from datetime import datetime, timezone
    config = {
        "db_user":        db_user,
        "access_level":   result["access_level"],
        "permissions":    result["permissions"],
        "roles":          result["roles"],
        "last_checked":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "setup_warnings": result.get("warnings", []),
    }
    os.makedirs(os.path.dirname(_SECURITY_PATH), exist_ok=True)
    with open(_SECURITY_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    logger.info(f"Security config saved: access_level={config['access_level']}")


def load_security_config() -> dict | None:
    """Return config/security.json as a dict, or None if the file doesn't exist."""
    if not os.path.exists(_SECURITY_PATH):
        return None
    try:
        with open(_SECURITY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning(f"Could not load security.json: {exc}")
        return None


# ── Business context ──────────────────────────────────────────────────────────

def save_business_context(context: dict) -> None:
    """Save business context to config/business_context.json."""
    config_dir = os.path.join(_ROOT, "config")
    os.makedirs(config_dir, exist_ok=True)
    path = os.path.join(config_dir, "business_context.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(context, f, indent=2, ensure_ascii=False)
    logger.info("Business context saved to config/business_context.json")
