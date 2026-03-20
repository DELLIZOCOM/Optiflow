import pyodbc
import time
import logging

logger = logging.getLogger(__name__)


def _get_credentials():
    """Load DB credentials from config/db_config.json at call time.

    Falls back to module-level settings for backward compat with .env installs.
    Reading fresh on each call ensures chat queries work immediately after setup
    without a server restart.
    """
    from config.loader import load_db_config
    cfg = load_db_config()
    if cfg.get("server"):
        return cfg["server"], cfg["database"], cfg["user"], cfg["password"]

    # Legacy fallback: read from config.settings (which may still read .env)
    from config.settings import DB_SERVER, DB_NAME, DB_USER, DB_PASSWORD
    return DB_SERVER, DB_NAME, DB_USER, DB_PASSWORD


def get_connection():
    """
    Establishes a connection to the SQL Server database.
    Includes retry logic for transient connection errors.
    """
    server, database, user, password = _get_credentials()

    connection_string = (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};"
        f"SERVER={server},1433;"
        f"DATABASE={database};"
        f"UID={user};"
        f"PWD={password};"
        f"TrustServerCertificate=yes;"
        f"Encrypt=optional"
    )

    max_retries = 3
    retry_delay = 2  # seconds

    for attempt in range(1, max_retries + 1):
        try:
            conn = pyodbc.connect(connection_string, timeout=10)
            return conn
        except pyodbc.Error as e:
            logger.warning(f"Database connection attempt {attempt} failed: {e}")
            if attempt < max_retries:
                time.sleep(retry_delay)
            else:
                logger.error("Max retries reached. Could not connect to the database.")
                raise e


def execute_query(sql, params=None):
    """
    Executes a SQL query and returns the results as a list of dictionaries.

    Args:
        sql (str): The SQL query string.
        params (tuple, optional): Parameters to pass to the query (to prevent SQL injection).

    Returns:
        list[dict]: A list of dictionaries representing the result set.
    """
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()

        if params:
            cursor.execute(sql, params)
        else:
            cursor.execute(sql)

        columns = [column[0] for column in cursor.description]
        results = []
        for row in cursor.fetchall():
            results.append(dict(zip(columns, row)))

        return results
    except Exception as e:
        logger.error(f"Error executing query: {e}")
        raise e
    finally:
        if conn:
            conn.close()


if __name__ == '__main__':
    # Setup basic logging for standalone execution
    logging.basicConfig(level=logging.INFO)
    print("Testing Database Connection...")
    try:
        result = execute_query("SELECT COUNT(*) as count FROM ProSt")
        print(f"Connection successful! Row count in ProSt: {result[0]['count']}")
    except Exception as e:
        print(f"Connection failed: {e}")
