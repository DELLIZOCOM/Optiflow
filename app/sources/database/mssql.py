"""
MSSQLSource — Microsoft SQL Server data source.

Implements DatabaseSource for SQL Server using pyodbc.
Provides connection, schema discovery, async query execution,
permission checking, and MSSQL-specific system prompt dialect notes.

Schema discovery writes to data/sources/{name}/ (not data/prompts/).
"""

import asyncio
import logging
import time

from app.sources.database.base import (
    DatabaseSource,
    enrich_tables_data,
    write_schema_index,
    write_table_file,
    write_relationships_file,
    _infer_relationships,
)

logger = logging.getLogger(__name__)

_MSSQL_DIALECT_NOTES = """\
## SQL Server Dialect Rules

- Row limit syntax: `SELECT TOP 100 col1, col2 FROM ...` (TOP goes after SELECT, before columns)
- Identifier quoting: square brackets — `[TableName].[ColumnName]`
- Current timestamp: `GETDATE()`  |  Today's date: `CONVERT(date, GETDATE())`
- NULL handling: `ISNULL(col, fallback)`
- Default schema prefix: `dbo.` (e.g. `dbo.MyTable`) — include if schema is ambiguous
- Date arithmetic: `DATEDIFF(day, start_date, end_date)`, `DATEADD(month, -3, GETDATE())`
- Extract parts: `YEAR(col)`, `MONTH(col)`, `DATEPART(quarter, col)`
- String functions: `LEN()`, `SUBSTRING()`, `CHARINDEX()`, `UPPER()`, `LOWER()`
- Type casting: `CAST(col AS DECIMAL(18,2))`, `CONVERT(varchar, col, 103)`
- Strict GROUP BY: every column in SELECT and ORDER BY must be in GROUP BY or wrapped \
in an aggregate (COUNT, SUM, AVG, MIN, MAX). Non-grouped context columns → wrap in MAX()/MIN().
  BAD:  SELECT Customer, OrderDate, COUNT(*) FROM T GROUP BY Customer
  GOOD: SELECT Customer, MAX(OrderDate) AS LatestOrder, COUNT(*) AS Total FROM T GROUP BY Customer\
"""

# Permission classification constants
_BLOCKED_PERMS = frozenset({"CONTROL"})
_WARN_ROLES    = frozenset({"db_owner", "db_datawriter", "db_ddladmin"})
_WARN_PERMS    = frozenset({"INSERT", "UPDATE", "DELETE", "ALTER", "DROP", "TRUNCATE"})


class MSSQLSource(DatabaseSource):
    """SQL Server data source."""

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        from app.utils.crypto import decrypt_secret, is_encrypted
        creds = config.get("credentials", {})
        self._server   = creds.get("server", "")
        self._database = creds.get("database", "")
        self._user     = creds.get("user", "")
        # Tolerant of both shapes:
        #   * config came from disk (Fernet-encrypted, starts with 'gAAAA') → decrypt
        #   * config came straight from the wizard's form (plaintext)       → use as-is
        # This eliminates the historical bug where _reload_source(config) was
        # called with the in-memory plaintext dict but __init__ blindly tried
        # to decrypt it, silently producing self._password = "" and breaking
        # every subsequent agent query with SQL error 18456.
        pw = creds.get("password", "") or ""
        self._password = decrypt_secret(pw) if is_encrypted(pw) else pw

    def get_db_type(self) -> str:
        return "mssql"

    def get_database_name(self) -> str:
        return self._database or self._name

    def get_system_prompt_section(self) -> str:
        return f"{_MSSQL_DIALECT_NOTES}\n(Applies to source: **{self._name}**)"

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self, server: str = "", database: str = "", user: str = "", password: str = ""):
        """Try ODBC Driver 18 then 17. Returns (conn, driver, error)."""
        server   = server   or self._server
        database = database or self._database
        user     = user     or self._user
        password = password or self._password

        try:
            import pyodbc
        except ImportError:
            return None, None, "pyodbc is not installed — run: pip install pyodbc"

        def _clean_error(exc):
            sqlstate = str(exc.args[0]).upper() if exc.args else ""
            raw = str(exc)
            low = raw.lower()
            if "cannot open database" in low:
                return f"Database '{database}' not found on server."
            if sqlstate == "28000" or "login failed" in low:
                return "Authentication failed. Check username and password."
            if (sqlstate == "08001" or "tcp provider" in low
                    or "network-related" in low or "no connection could be made" in low
                    or "connection refused" in low):
                return (f"Cannot reach server at '{server}'. "
                        "Check the address and make sure you're on the same network.")
            if sqlstate == "HYT00" or "timeout expired" in low or "login timeout" in low:
                return (f"Server at '{server}' did not respond within 10 seconds. "
                        "Check firewall and network.")
            if sqlstate in ("IM002", "IM004") or (
                "driver" in low and (
                    "not found" in low or "data source name not found" in low
                    or "specified driver could not be loaded" in low)
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

    def validate_credentials(self) -> dict:
        """Test connection with stored credentials."""
        conn, driver, error = self.connect()
        if conn:
            try:
                conn.close()
            except Exception:
                pass
            return {"success": True, "driver": driver}
        return {"success": False, "error": error}

    # ── Async query execution ─────────────────────────────────────────────────

    async def execute_query(self, sql: str) -> list[dict]:
        """Execute SQL in a thread-pool executor to avoid blocking the event loop."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._execute_sync, sql)

    def _execute_sync(self, sql: str) -> list[dict]:
        """Blocking SQL execution with retry."""
        conn = None
        for attempt in range(1, 4):
            try:
                import pyodbc
                conn_str = (
                    f"DRIVER={{ODBC Driver 18 for SQL Server}};"
                    f"SERVER={self._server},1433;"
                    f"DATABASE={self._database};"
                    f"UID={self._user};"
                    f"PWD={self._password};"
                    "TrustServerCertificate=yes;"
                    "Encrypt=optional"
                )
                conn = pyodbc.connect(conn_str, timeout=10)
                cursor = conn.cursor()
                cursor.execute(sql)
                columns = [col[0] for col in cursor.description]
                results = [dict(zip(columns, row)) for row in cursor.fetchall()]
                return results
            except Exception as e:
                logger.warning(f"[{self._name}] Query attempt {attempt} failed: {e}")
                if attempt < 3:
                    time.sleep(2)
                else:
                    raise
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass

    # ── Schema discovery ──────────────────────────────────────────────────────

    def _query_pk_fk(self, cursor) -> dict:
        """
        Query MSSQL INFORMATION_SCHEMA for PK and FK constraints.
        Returns {"pk_map": {table: [col,...]}, "fk_list": [{from_table, from_column, to_table, to_column}]}
        """
        pk_map: dict  = {}
        fk_list: list = []

        # Primary keys
        try:
            cursor.execute("""
                SELECT tc.TABLE_NAME, kcu.COLUMN_NAME
                FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                  ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
                 AND tc.TABLE_SCHEMA    = kcu.TABLE_SCHEMA
                WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
                ORDER BY tc.TABLE_NAME, kcu.ORDINAL_POSITION
            """)
            for row in cursor.fetchall():
                tname, cname = row[0], row[1]
                pk_map.setdefault(tname, []).append(cname)
        except Exception as exc:
            logger.warning(f"[{self._name}] PK query failed (non-fatal): {exc}")

        # Foreign keys
        try:
            cursor.execute("""
                SELECT
                    fk_tc.TABLE_NAME  AS from_table,
                    fk_kcu.COLUMN_NAME AS from_column,
                    pk_tc.TABLE_NAME  AS to_table,
                    pk_kcu.COLUMN_NAME AS to_column
                FROM INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
                JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS fk_tc
                  ON rc.CONSTRAINT_NAME        = fk_tc.CONSTRAINT_NAME
                JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS pk_tc
                  ON rc.UNIQUE_CONSTRAINT_NAME  = pk_tc.CONSTRAINT_NAME
                JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE fk_kcu
                  ON rc.CONSTRAINT_NAME = fk_kcu.CONSTRAINT_NAME
                JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE pk_kcu
                  ON rc.UNIQUE_CONSTRAINT_NAME = pk_kcu.CONSTRAINT_NAME
                ORDER BY fk_tc.TABLE_NAME, fk_kcu.ORDINAL_POSITION
            """)
            for row in cursor.fetchall():
                fk_list.append({
                    "from_table":  row[0], "from_column": row[1],
                    "to_table":    row[2], "to_column":   row[3],
                    "confidence":  "confirmed",
                })
        except Exception as exc:
            logger.warning(f"[{self._name}] FK query failed (non-fatal): {exc}")

        return {"pk_map": pk_map, "fk_list": fk_list}

    def discover_schema(self, conn, db_name: str, server: str) -> dict:
        """
        Discover all tables/columns/values with semantic metadata enrichment.
        Writes to data/sources/{name}/:
          - schema_index.md
          - tables/{TableName}.md  (one per table, enriched with roles + relationships)
          - relationships.md       (source-level relationship map)
        """
        cursor = conn.cursor()

        # ── 1. Get table list ─────────────────────────────────────────────────
        cursor.execute("""
            SELECT TABLE_NAME
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_TYPE = 'BASE TABLE'
              AND TABLE_NAME NOT LIKE 'sys%'
            ORDER BY TABLE_NAME
        """)
        table_names = [row[0] for row in cursor.fetchall()]

        # ── 2. Get PK/FK constraints ──────────────────────────────────────────
        pk_fk_data = self._query_pk_fk(cursor)
        logger.info(
            f"[{self._name}] Found {len(pk_fk_data['pk_map'])} PKs, "
            f"{len(pk_fk_data['fk_list'])} confirmed FKs"
        )

        # ── 3. Collect raw column + sample data per table ─────────────────────
        tables_data = []

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
                    "name": col_name, "type": full_type, "nullable": nullable == "YES"
                })
                if data_type in ("varchar", "nvarchar") and max_len and 0 < max_len <= 100:
                    categorical_candidates.append(col_name)

            # Sample distinct values for short string cols
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
                "name": table_name, "row_count": row_count,
                "columns": columns, "categorical": categorical,
            })

        # ── 4. Enrich with semantic metadata (roles, PK, FK, type, grain, rels) ─
        tables_data = enrich_tables_data(tables_data, pk_fk_data)

        # ── 5. Write schema files ─────────────────────────────────────────────
        try:
            write_schema_index(
                tables_data, self._schema_dir,
                source_name=self._name, db_type=self.get_db_type(),
            )
            tables_dir = self._schema_dir / "tables"
            for t in tables_data:
                write_table_file(t, tables_dir)

            # Write source-level relationships.md
            confirmed_fks = pk_fk_data["fk_list"]
            inferred_rels = _infer_relationships(tables_data, confirmed_fks)
            write_relationships_file(
                self._schema_dir, confirmed_fks, inferred_rels, tables_data
            )

            logger.info(
                f"[{self._name}] Schema discovery complete: {len(table_names)} tables, "
                f"{len(confirmed_fks)} confirmed FKs, {len(inferred_rels)} inferred relationships "
                f"→ data/sources/{self._name}/"
            )
        except Exception as exc:
            logger.warning(f"[{self._name}] Schema file write failed (non-fatal): {exc}")

        return {"db_name": db_name, "server": server, "tables": tables_data}

    # ── Permission check ──────────────────────────────────────────────────────

    def verify_readonly_access(self, conn) -> dict:
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
                JOIN sys.database_principals r ON rm.role_principal_id = r.principal_id
                JOIN sys.database_principals u ON rm.member_principal_id = u.principal_id
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
            logger.warning(f"Permission check failed: {exc}")
            return {
                "access_level": "unknown", "permissions": [], "roles": [],
                "message": "Could not verify permissions. Make sure this is a read-only user.",
                "warnings": ["sys view access restricted — unable to verify permissions"],
            }

        blocked_p = _BLOCKED_PERMS & set(permissions)
        if is_sysadmin or blocked_p:
            reason = (
                "sysadmin server role" if is_sysadmin
                else f"{', '.join(sorted(blocked_p))} permission"
            )
            return {
                "access_level": "blocked", "permissions": permissions, "roles": roles,
                "message": (f"User has {reason}. OptiFlow cannot connect with this access level. "
                            "Please create a read-only user."),
                "warnings": [f"BLOCKED: {reason}"],
            }

        warn_r = _WARN_ROLES & set(roles)
        warn_p = _WARN_PERMS & set(permissions)
        if warn_r or warn_p:
            found = sorted(warn_r | warn_p)
            return {
                "access_level": "warning", "permissions": permissions, "roles": roles,
                "message": (f"User has write permissions: {', '.join(found)}. "
                            "OptiFlow requires a read-only database user."),
                "warnings": [f"Write-level access detected: {', '.join(found)}"],
            }

        return {
            "access_level": "readonly", "permissions": permissions, "roles": roles,
            "message": "Read-only access confirmed. This user can only read data.",
            "warnings": [],
        }
