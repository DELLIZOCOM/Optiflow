# OptiFlow AI — Documentation

## What is OptiFlow AI?

OptiFlow AI is a natural language interface for SQL Server databases. You ask a question in plain English — it generates the SQL, shows it to you for review, executes it after your approval, then explains the results in plain language.

**Core flow:**
```
You ask → AI generates SQL → You review → You approve → DB executes → AI explains
```

No login. No admin panel. Open the app, run setup once, then ask questions.

---

## How to Run

```bash
./start.sh          # Mac / Linux
start.bat           # Windows
```

Then open `http://localhost:8000` in your browser.

Custom port:
```bash
PORT=9000 ./start.sh
```

On first run the setup wizard opens automatically. On subsequent runs it goes straight to chat.

---

## Setup Wizard (5 steps)

| Step | What happens |
|------|-------------|
| **1 — AI Provider** | Enter your Anthropic or OpenAI API key. Key is encrypted and saved. |
| **2 — Database** | Enter SQL Server host, database, username, password. Permissions are checked. |
| **3 — Schema Discovery** | Scans all tables, columns, row counts, sample values. Writes schema files to `data/prompts/`. |
| **4 — Teach** | AI drafts a business knowledge document from your schema. You review and edit it. |
| **5 — Launch** | Setup complete. Chat is ready. |

Setup writes to `data/config/` and `data/prompts/`. Re-running the app skips the wizard if those files exist.

---

## File Structure

```
optiflow-ai/
│
├── backend/                    ← All server-side Python
│   ├── app.py                  ← FastAPI entry point. Mounts static files, registers routers.
│   ├── templates.py            ← Jinja2 template engine, points at frontend/
│   ├── utils.py                ← safe_json() helper (handles non-serializable types)
│   │
│   ├── routes/
│   │   ├── query.py            ← GET /, POST /ask, POST /approve, POST /reject
│   │   └── setup.py            ← POST /setup/* (all wizard endpoints)
│   │
│   ├── services/
│   │   ├── pipeline.py         ← Main query pipeline: cache → SQL gen → approval → execute → interpret
│   │   ├── sql_generator.py    ← AI SQL generation (single, chain, deep-dive modes)
│   │   ├── interpreter.py      ← AI result interpretation (plain-English explanation)
│   │   ├── schema_manager.py   ← Schema file management, is_setup_complete(), save helpers
│   │   ├── schema_loader.py    ← Reads schema_context.txt and per-table files for prompts
│   │   ├── table_selector.py   ← AI picks which tables are relevant for a question
│   │   └── company_builder.py  ← AI drafts company.md from schema during setup
│   │
│   ├── ai/
│   │   ├── client.py           ← Single LLM call site: get_completion(). Handles Anthropic/OpenAI/custom. Rate limiting.
│   │   └── prompts.py          ← All system prompt strings (SQL gen, table select, interpret, fix, etc.)
│   │
│   ├── cache/
│   │   ├── query_cache.py      ← In-memory TTL cache. Same question within 5 min → skip AI.
│   │   └── approved_queries.py ← JSONL log of approved queries. Similar questions reuse proven SQL.
│   │
│   ├── config/
│   │   ├── paths.py            ← All file paths in one place. Everything reads from here.
│   │   ├── loader.py           ← load/save AI config and DB config (with encryption).
│   │   ├── crypto.py           ← Fernet encryption for API keys and DB passwords.
│   │   └── settings.py         ← Module-level constants (DB_SERVER, DB_NAME, etc.) loaded at startup.
│   │
│   └── connectors/
│       ├── base.py             ← Abstract connector interface
│       └── mssql.py            ← SQL Server connector: connect, execute, schema discovery, permission check
│
├── frontend/                   ← All browser-side files
│   ├── pages/
│   │   ├── chat.html           ← Main chat UI
│   │   └── setup.html          ← Setup wizard UI (5-panel wizard)
│   ├── css/
│   │   ├── chat.css            ← Chat page styles (messages, SQL cards, approve/reject buttons)
│   │   └── setup.css           ← Setup wizard styles (panels, form fields, step indicator)
│   └── js/
│       ├── chat.js             ← Chat logic: send question, render AI card, approve/reject, session history
│       └── setup.js            ← Wizard logic: AI test, DB test, schema discover, company draft
│
├── data/                       ← All runtime data (git-ignored, generated at runtime)
│   ├── config/
│   │   ├── model_config.json   ← AI provider, encrypted API key, model name
│   │   ├── db_config.json      ← DB host, name, user, encrypted password
│   │   ├── .secret             ← Fernet encryption key (auto-generated, never share)
│   │   └── security.json       ← DB permission level (readonly / warning / blocked)
│   ├── knowledge/
│   │   ├── company.md          ← Business context document (edited in setup step 4)
│   │   └── suggested_questions.json  ← Reserved, not currently used in UI
│   ├── prompts/
│   │   ├── schema_context.txt  ← Full schema dump (all tables, columns, samples)
│   │   ├── schema_index.txt    ← One-line-per-table index for fast table selection
│   │   └── tables/             ← One .txt file per table with full column detail
│   └── logs/
│       └── approved_queries.jsonl   ← Log of every user-approved query
│
├── requirements.txt            ← Python dependencies
├── start.sh                    ← One-command startup (Mac/Linux)
├── start.bat                   ← One-command startup (Windows)
└── DOCUMENTATION.md            ← This file
```

---

## Query Pipeline (what happens when you ask a question)

```
POST /ask
  │
  ├─ 1. Check in-memory cache (same question, <5 min TTL)
  │       └─ HIT → return cached SQL card immediately
  │
  ├─ 2. Check approved_queries.jsonl (Jaccard similarity match)
  │       └─ SIMILAR FOUND → reuse proven SQL, skip AI
  │
  ├─ 3. generate_universal(question)
  │       ├─ table_selector: AI picks relevant tables from schema_index.txt
  │       ├─ loads full schema for those tables from data/prompts/tables/*.txt
  │       ├─ loads company.md for business context
  │       └─ AI generates SQL + explanation + confidence + warnings
  │           Mode: single | chain | deep_dive
  │
  └─ Return SQL card to browser for user review
       │
       ├─ User clicks REJECT → POST /reject → logged and discarded
       │
       └─ User clicks APPROVE → POST /approve
               ├─ execute_query(sql)
               │     └─ on error: fix_sql() retries up to 2 times
               ├─ interpret_results() → AI explains rows in plain English
               ├─ append to approved_queries.jsonl
               └─ Return answer to browser
```

---

## AI Query Modes

| Mode | When used | What happens |
|------|-----------|-------------|
| **single** | Most questions | One SQL query, one table or join |
| **chain** | Multi-step questions | 2–5 queries run in sequence, results interpreted together |
| **deep_dive** | Entity deep-dives | Multiple queries about one entity (e.g. "tell me everything about client X") |

---

## Config Files Explained

### `data/config/model_config.json`
Stores AI provider settings. Written by setup step 1.
```json
{
  "cloud_provider": {
    "provider": "anthropic",
    "api_key": "<fernet-encrypted>",
    "api_key_hint": "AB12",
    "model": "claude-sonnet-4-20250514"
  },
  "local_provider": {
    "enabled": false,
    "endpoint": "http://localhost:11434",
    "model": "qwen3:8b"
  }
}
```

### `data/config/db_config.json`
Database credentials. Written by setup step 3 (schema discovery).
```json
{
  "server": "192.168.1.100",
  "database": "MyDatabase",
  "user": "optiflow_reader",
  "password": "<fernet-encrypted>"
}
```

### `data/config/.secret`
Auto-generated 44-byte Fernet key. Used to encrypt/decrypt the API key and DB password. **Never commit or share this file.** If deleted, re-enter credentials in setup.

### `data/knowledge/company.md`
Markdown document describing your business. Written during setup step 4, injected into every AI prompt as context. Edit it anytime to improve query accuracy.

### `data/prompts/schema_context.txt`
Full schema of every table — columns, types, sample values. Generated by schema discovery.

### `data/prompts/schema_index.txt`
One line per table: `TableName — brief description`. Used by the table selector to pick relevant tables without sending the full schema to the AI on every request.

### `data/prompts/tables/*.txt`
One file per table with full column detail. Loaded only for tables selected as relevant to a question — keeps prompts short and fast.

### `data/logs/approved_queries.jsonl`
Every query the user approved, stored as JSON lines. Used to match similar future questions and reuse proven SQL without calling the AI again.

---

## Key Modules in Detail

### `backend/ai/client.py`
The only place that calls the LLM API. All other code calls `get_completion(system, user)`.
- Reads config fresh on every call — no restart needed after changing the key.
- Built-in rate limiter (25 calls/min rolling window).
- Raises `RateLimitExhausted` on 429 — the frontend shows a countdown timer and retries.
- Supports Anthropic, OpenAI, and any OpenAI-compatible endpoint.

### `backend/cache/approved_queries.py`
Learns from approved queries. Uses Jaccard token similarity (threshold 0.72) to match new questions to previously approved ones. If a match is found, the proven SQL is reused immediately — no AI call needed.

### `backend/cache/query_cache.py`
Simple in-memory TTL cache. Identical questions within 5 minutes return instantly. Cleared on server restart.

### `backend/config/crypto.py`
Fernet symmetric encryption. The `.secret` key file is auto-created on first use. `encrypt_secret()` and `decrypt_secret()` are called whenever credentials are saved or loaded.

### `backend/connectors/mssql.py`
All direct database interaction:
- `get_db_connection()` — tries pyodbc with multiple ODBC drivers
- `execute_query()` — runs SELECT, returns list of dicts
- `run_schema_discovery()` — scans tables/columns/samples, writes all prompt files
- `verify_readonly_access()` — checks if the user has write/admin privileges (blocks setup if so)

---

## Security Model

- **Read-only enforcement**: Setup blocks admin/owner DB users. Only `db_datareader` role users are accepted without warning.
- **Encrypted credentials**: API keys and DB passwords are Fernet-encrypted at rest. Never stored in `.env` or plaintext.
- **SQL approval gate**: The AI never executes SQL automatically. Every query is shown to the user first.
- **No auth**: The app has no login. It is designed for internal/local network use. Do not expose to the public internet.

---

## Re-running Setup

To connect to a different database or change the AI provider without reinstalling:

**Change AI provider:** Delete `data/config/model_config.json`, restart — wizard opens at step 1.

**Change database:** Delete `data/config/db_config.json` and `data/prompts/schema_context.txt`, restart — wizard opens at step 2.

**Full reset:** Delete `data/config/` and `data/prompts/`, restart — wizard opens from the beginning.

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `fastapi` | Web framework |
| `uvicorn` | ASGI server |
| `jinja2` | HTML templating |
| `anthropic` | Anthropic Claude API client |
| `openai` | OpenAI API client (also used for custom endpoints) |
| `pyodbc` | SQL Server ODBC driver |
| `cryptography` | Fernet encryption for secrets |
| `requests` | HTTP calls (Ollama connection test) |
| `python-multipart` | Form parsing for FastAPI |


cd /Users/vinayakajith/Desktop/optiflow-ai
python -m venv .venv          # only needed once
source .venv/bin/activate
pip install -r requirements.txt   # only needed once
uvicorn backend.app:app --host 0.0.0.0 