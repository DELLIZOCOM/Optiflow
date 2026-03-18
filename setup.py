#!/usr/bin/env python3
"""
OptiFlow AI Setup Wizard (CLI)
==============================
Connects to your SQL Server database and configures OptiFlow AI for it.

Run once per new database connection:
    python3 setup.py

For a web-based wizard, start the server and open http://localhost:8000
"""

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))


def main() -> None:
    print()
    print("╔══════════════════════════════════════════╗")
    print("║        OptiFlow AI  —  Setup Wizard      ║")
    print("╚══════════════════════════════════════════╝")
    print()

    from core.setup_manager import (
        get_db_connection,
        run_schema_discovery,
        save_business_context,
        save_db_credentials,
    )

    # ── Step 1: Credentials ──────────────────────────────────────────────────
    print("Step 1 of 4: Database Credentials")
    print("─" * 42)
    server   = input("  SQL Server host / IP address : ").strip()
    database = input("  Database name                : ").strip()
    user     = input("  Username                     : ").strip()
    password = input("  Password                     : ").strip()

    if not all([server, database, user, password]):
        print("\n✗ All fields are required. Exiting.")
        sys.exit(1)

    # ── Step 2: Connect and discover schema ──────────────────────────────────
    print()
    print("Step 2 of 4: Connecting and discovering schema")
    print("─" * 42)

    conn, driver, error = get_db_connection(server, database, user, password)
    if not conn:
        print(f"\n✗ Could not connect: {error}")
        print("  Make sure pyodbc and ODBC Driver 17/18 for SQL Server are installed.")
        print("    brew install msodbcsql18   # macOS")
        print("    pip install pyodbc")
        sys.exit(1)

    print(f"  Connected via {driver}")
    save_db_credentials(server, database, user, password)
    print("  ✓ Credentials saved to .env")

    schema_data = run_schema_discovery(conn, database, server)
    tables = schema_data["tables"]
    print(f"  ✓ Schema saved to prompts/schema_context.txt ({len(tables)} tables)")

    # ── Step 3: Generate business context template ───────────────────────────
    print()
    print("Step 3 of 4: Generating domain configuration")
    print("─" * 42)

    config_path = os.path.join(_ROOT, "config", "business_context.json")
    if not os.path.exists(config_path):
        template = {
            "_comment": "Business context for OptiFlow AI. Edit this to teach OptiFlow your domain.",
            "company_name":       "",
            "business_type":      "",
            "data_quality_rules": [],
            "terminology":        [],
            "column_warnings":    [],
        }
        save_business_context(template)
        print("  ✓ Template saved to config/business_context.json")
    else:
        print("  ✓ config/business_context.json already exists — not overwritten")

    print()
    print("  ► Edit config/business_context.json with your business rules.")
    print("    This teaches OptiFlow your data quality rules and terminology.")

    # ── Step 4: Test query ───────────────────────────────────────────────────
    print()
    print("Step 4 of 4: Testing connection")
    print("─" * 42)

    largest = max(tables, key=lambda t: t["row_count"], default=None)
    if largest:
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM [{largest['name']}]")
        live_count = cursor.fetchone()[0]
        print(f"  ✓ Test query passed: {largest['name']} has {live_count:,} rows")

    conn.close()

    print()
    print("✓ Setup complete!")
    print()
    print("  Start the server:  uvicorn app:app --port 8000")
    print("  Then open:         http://localhost:8000")
    print()


if __name__ == "__main__":
    main()
