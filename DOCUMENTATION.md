# OptiFlow AI — Technical Documentation

> A self-learning, schema-driven natural language interface for SQL Server. Ask questions in plain English, get structured business insights powered by Claude AI or local Ollama models. OptiFlow adapts to any database schema and gets smarter over time through its built-in learning loop.

---

## Table of Contents

1. [What Is OptiFlow AI?](#1-what-is-optiflow-ai)
2. [How to Run](#2-how-to-run)
3. [Project Structure](#3-project-structure)
4. [Setup Wizard — Full Flow](#4-setup-wizard--full-flow)
5. [Architecture Overview](#5-architecture-overview)
6. [The Query Pipeline — Step by Step](#6-the-query-pipeline--step-by-step)
7. [The Smart Learning Loop](#7-the-smart-learning-loop)
8. [API Reference — All Endpoints](#8-api-reference--all-endpoints)
9. [Module Reference](#9-module-reference)
10. [Config & Runtime Files](#10-config--runtime-files)
11. [Security Model](#11-security-model)
12. [The Frontend](#12-the-frontend)
13. [AI Provider Configuration](#13-ai-provider-configuration)
14. [Schema Management](#14-schema-management)
15. [Admin Operations](#15-admin-operations)

---

## 1. What Is OptiFlow AI?

OptiFlow AI is a **universal natural language query engine** for SQL Server databases. It requires no code changes to adapt to a new company's data — point it at any SQL Server, run the setup wizard, and it immediately understands the schema, learns the business context, and starts answering questions.

**Core design principles:**

- **Schema-driven, not template-driven.** No hardcoded SQL. Every query is authored on-the-fly by reading the live database schema and business rules.
- **Human-in-the-loop.** No AI-generated SQL ever reaches the database without an explicit human approval click. Users see the SQL, understand what it does, then approve.
- **Self-improving.** Approved queries are persisted and reused. The more a team uses it, the fewer expensive AI calls are needed — the system builds a verified library of proven SQL.
- **Configurable business logic.** Admins write plain-English rules in `config/company.md` (e.g., *"Exclude projects created on 2025-04-21 — they are test records"*). These rules are injected into every AI prompt, making OptiFlow enforce business rules without touching any code.
- **Read-only by design.** The DB user must have SELECT-only permissions. OptiFlow verifies this at startup and refuses to run if write permissions are detected.

---

## 2. How to Run

### Quick Start (Recommended)

```bash
# Clone / download the project, then:
./start.sh
```

`start.sh` handles everything automatically:
1. Checks Python 3 is installed
2. Creates `.venv` virtual environment if it doesn't exist
3. Activates the environment
4. Installs `requirements.txt` (skips if already up-to-date, using MD5 hash comparison)
5. Starts the server at `http://localhost:8000`

On Windows, use `start.bat` (double-click or run in terminal).

### Manual Steps

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

### First Launch

Navigate to `http://localhost:8000`. If no admin account exists, the setup wizard opens automatically. Complete all 5 steps to configure the AI provider, connect the database, and generate the company knowledge base. After setup, the chat interface is ready.

### Local LLM (Optional)

OptiFlow can use a local Ollama model for intent parsing instead of the cloud API. Install [Ollama](https://ollama.ai) and pull a model:

```bash
ollama pull qwen3:8b
```

Enable it in the setup wizard (Step 2) or in Admin → Settings.

---

## 3. Project Structure

```
optiflow-ai/
│
├── start.sh                      # One-command startup (Mac/Linux)
├── start.bat                     # One-command startup (Windows)
├── app.py                        # FastAPI server — all routes, auth, orchestration
├── requirements.txt              # Python dependencies
├── setup.py                      # CLI setup wizard (alternative to web UI)
│
├── config/                       # All configuration (git-ignored except .py files)
│   ├── __init__.py
│   ├── ai_client.py              # Unified AI provider abstraction (Anthropic / OpenAI / custom)
│   ├── crypto.py                 # Fernet-based encryption for secrets
│   ├── loader.py                 # Load / save config files with encryption
│   ├── settings.py               # Module-level settings (falls back to env vars)
│   │
│   ├── .secret                   # ← GENERATED: Fernet encryption key (git-ignored)
│   ├── users.json                # ← GENERATED: Hashed user accounts (git-ignored)
│   ├── model_config.json         # ← GENERATED: AI provider + encrypted API key (git-ignored)
│   ├── db_config.json            # ← GENERATED: DB credentials + encrypted password (git-ignored)
│   ├── security.json             # ← GENERATED: DB permission check result (git-ignored)
│   ├── company.md                # ← GENERATED: Business knowledge base (admin-editable)
│   ├── schema.json               # ← GENERATED: Schema metadata + refresh history (git-ignored)
│   └── suggested_questions.json  # ← GENERATED: Dynamic chat chip questions (git-ignored)
│
├── core/                         # Business logic
│   ├── agent_sql_generator.py    # SQL generation engine — the core "brain"
│   ├── approved_queries.py       # Persistent learning log + similarity matching
│   ├── audit_logger.py           # Append-only audit trail with rotation
│   ├── auth.py                   # User authentication (bcrypt)
│   ├── db.py                     # SQL Server connection + query execution
│   ├── feedback_logger.py        # User thumbs-up/down log
│   ├── intent_parser.py          # Cloud LLM intent parser (legacy/unused)
│   ├── local_intent_parser.py    # Local Ollama intent parser (optional)
│   ├── query_cache.py            # In-memory TTL cache
│   └── setup_manager.py          # Schema discovery, DB introspection, config persistence
│
├── intents/
│   └── __init__.py               # Intent type definitions
│
├── logs/                         # Runtime logs (git-ignored)
│   ├── audit.jsonl               # Every user action + AI-generated SQL
│   ├── approved_queries.jsonl    # Every human-approved query (the learning corpus)
│   └── feedback.jsonl            # User thumbs-up/down ratings
│
├── prompts/                      # AI context files (git-ignored — may contain DB structure)
│   ├── schema_context.txt        # Full concatenated schema
│   ├── schema_index.txt          # One-line-per-table compact index
│   └── tables/                   # Per-table detail files
│       └── {TableName}.txt
│
└── templates/                    # Jinja2 HTML templates
    ├── chat.html                 # Main chat interface
    ├── setup.html                # First-run setup wizard
    ├── login.html                # Login page
    ├── settings.html             # Admin settings panel
    ├── company_editor.html       # Admin company.md editor
    ├── audit.html                # Admin audit log viewer
    └── feedback.html             # Admin feedback dashboard
```

---

## 4. Setup Wizard — Full Flow

The wizard runs at `GET /` on first launch (or after a reset). It is a single-page multi-panel HTML form in `templates/setup.html`.

### Panel 1 — Create Admin Account
- **Endpoint:** `POST /setup/create-admin`
- Creates the first admin user in `config/users.json` (bcrypt-hashed password, minimum 8 characters)
- On first run this endpoint is open (no auth required). After setup completes, it requires an existing admin session to add more users.
- On success, logs the user in automatically and advances to Panel 2.

### Panel 2 — Configure AI Provider
- **Endpoints:** `POST /setup/test-ai-provider`, `POST /setup/save-ai-config`, `POST /setup/test-ollama`
- Choose provider: **Anthropic** (Claude), **OpenAI**, or **Custom** (any OpenAI-compatible endpoint)
- Enter API key → click **Test API Key** → backend calls `test_connection()` with a minimal 1-token request
- Model name field is pre-filled on provider change (`claude-sonnet-4-20250514` for Anthropic, `gpt-4o` for OpenAI)
- **Optional local model:** toggle on Ollama for intent parsing; enter endpoint (default `http://localhost:11434`) and model name (default `qwen3:8b`); test connection via `POST /setup/test-ollama` which calls `/api/tags`
- On save: API key is encrypted with Fernet → written to `config/model_config.json`; `config/.secret` is auto-generated if it doesn't exist

### Panel 3 — Database Connection
- **Endpoints:** `POST /setup/test-connection`, `POST /setup/check-permissions`
- Fields: Server (hostname or IP), Database name, Username, Password
- **Test Connection** — attempts ODBC connect (tries Driver 18, then Driver 17) — returns success/fail
- **Check Permissions** — calls `verify_readonly_access()`:
  - `readonly` — user has SELECT-only; safe to proceed
  - `warning` — user has some write permissions; warning banner shown in chat UI
  - `blocked` — user has admin/owner/writer role; **setup cannot continue**
- Permission result saved to `config/security.json`

### Panel 4 — Schema Discovery
- **Endpoint:** `POST /setup/discover-schema` (300-second timeout)
- Connects to DB with provided credentials
- Runs `run_schema_discovery()` which:
  1. Queries `INFORMATION_SCHEMA.TABLES` for all BASE TABLE names
  2. For each table: fetches column names/types/nullable, row count, categorical values (columns with ≤30 distinct non-null values)
  3. Writes `prompts/schema_context.txt` — full concatenated schema
  4. Writes `prompts/schema_index.txt` — one compact line per table
  5. Writes `prompts/tables/{TableName}.txt` — per-table detail file
- After discovery: saves `config/db_config.json` (encrypted password), `config/schema.json` (metadata), and generates `config/suggested_questions.json` (AI-generated chat chips)
- Returns table count and names to frontend

### Panel 5 — Company Knowledge
- **Endpoints:** `POST /setup/generate-company-draft`, `POST /setup/company-followup`, `POST /setup/save-company-knowledge`
- **Generate Draft** — reads `prompts/schema_context.txt`, sends full schema to Claude with a system prompt asking it to explain: what each table tracks, what status/type values mean, how tables relate, and what business questions each table answers. Returns a rich markdown document (up to 4,000 tokens).
- **Follow-up Questions** — Claude analyzes the draft and generates 3–5 targeted questions to fill knowledge gaps (e.g., "What does status = 'WIP' mean?"). User can answer inline.
- **Save** — writes `config/company.md` with the draft content plus any follow-up answers appended under `## Additional Context`

### Setup Complete
- `is_setup_complete()` now returns `True` (both `schema_context.txt` and `model_config.json` with `cloud_provider` key exist)
- User is redirected to `/login` → then to the chat interface

---

## 5. Architecture Overview

```
Browser
  │
  ├── POST /ask ──────────────────────────────────────────────────────────────┐
  │                                                                            │
  │   ┌─────────────────────────────────────────────────────────────────┐     │
  │   │                    _run_pipeline()                              │     │
  │   │                                                                 │     │
  │   │  1. query_cache.get(question)        ← In-memory cache (1h)    │     │
  │   │         ↓ miss                                                  │     │
  │   │  2. approved_queries.find_similar()  ← Jaccard similarity log  │     │
  │   │         ↓ miss                                                  │     │
  │   │  3. generate_universal(question)     ← Claude API call         │     │
  │   │       ├─ _select_tables_with_type()  ← Table selection (cheap) │     │
  │   │       └─ generate SQL (full context) ← SQL authoring (full)    │     │
  │   │  4. query_cache.put(question, result)                           │     │
  │   └─────────────────────────────────────────────────────────────────┘     │
  │                                                                            │
  │   Returns: {mode, sql, explanation, tables, confidence, requires_approval}│
  │◄───────────────────────────────────────────────────────────────────────────┘
  │
  │   User reads SQL preview → clicks Approve or Reject
  │
  ├── POST /approve ────────────────────────────────────────────────────────────┐
  │                                                                              │
  │   ┌──────────────────────────────────────────────────────────────────┐      │
  │   │               _run_agent_approval() / _run_chain_approval()      │      │
  │   │                                                                   │      │
  │   │  1. core.db.execute_query(sql)   ← SQL Server via pyodbc         │      │
  │   │       ↓ on error (attempt < 2): fix_sql() → retry               │      │
  │   │  2. _interpret_results()         ← Claude as business advisor    │      │
  │   │  3. approved_queries.log_entry() ← Persist to JSONL log          │      │
  │   └──────────────────────────────────────────────────────────────────┘      │
  │                                                                              │
  │   Returns: {answer (markdown), rows_returned, time_ms}                      │
  │◄─────────────────────────────────────────────────────────────────────────────┘
  │
  └── POST /feedback → update approved_queries.jsonl (flagged / confirmed)
```

### Query Modes

| Mode | Trigger | SQL Steps | Example |
|------|---------|-----------|---------|
| `agent` (single) | Simple lookup | 1 query | "Show pending invoices for Hyundai" |
| `agent` (chain) | Cross-table analysis | 2–3 queries | "Compare revenue Q1 vs Q2" |
| `deep_dive` | Entity investigation | 4–5 queries | "Tell me everything about project P-001" |
| `business_health` | Overview request | 3–5 queries | "How's the business doing?" |

---

## 6. The Query Pipeline — Step by Step

### Step 1 — Cache Check (`query_cache.py`)

The question is normalized (lowercase, collapsed whitespace) and looked up in an in-memory dictionary. If found and not expired (TTL: 1 hour), the cached agent dict is returned immediately — no AI call made.

### Step 2 — Approved Query Log (`approved_queries.py`)

`find_similar()` tokenizes the question (split on whitespace/punctuation, lowercase, filter stop words) and compares against every entry in `logs/approved_queries.jsonl` using **Jaccard similarity**:

```
similarity = |tokens_A ∩ tokens_B| / |tokens_A ∪ tokens_B|
```

Threshold: `0.72` (roughly 3 of 4 meaningful words match). Entries with `flagged=True` or `stale=True` are skipped. If a match is found, the proven SQL is returned without any AI call.

**Stop words excluded:** a, an, the, is, are, was, were, be, been, being, have, has, had, do, does, did, will, would, could, should, may, might, shall, of, in, on, at, to, for, with, by, from, up, about, and, but, or, if, as, not, show, me, my, get, list, all, what, who, when, where, how, which, give, find, tell, describe

### Step 3 — Universal Generation (`agent_sql_generator.py`)

`generate_universal()` is the main entry point. It uses a **two-step process for databases with >15 tables**:

**Step 3a — Table Selection (cheap call)**

`_select_tables_with_type()` sends only `schema_index.txt` (compact, one line per table) plus the question to Claude. It returns:
- Which table names are relevant to this question
- The query type: `single`, `chain`, or `deep_dive`

This is a cheap call (~200–400 tokens) that avoids loading the full schema for every question.

**Step 3b — SQL Generation (full context call)**

The selected tables' detail files are loaded from `prompts/tables/`. Combined with the system prompt (which includes `company.md` rules), Claude generates the actual SQL.

**Fallback:** If `schema_index.txt` or per-table files are missing (e.g., small DB or legacy setup), the full `schema_context.txt` is loaded instead.

### Step 4 — SQL Auto-Fix (`fix_sql()`)

If SQL execution fails, `fix_sql()` sends the failed SQL + the SQL Server error message back to Claude, which suggests a corrected version. Up to 2 retry attempts per query.

### Step 5 — Interpretation (`_interpret_results()`)

Claude acts as a "Business Advisor" — it receives the raw result rows and writes a plain-English summary. The system prompt enforces:
- Use exact figures from the data — never estimate or round ("Rs 14,23,567.50" not "~14.2 lakhs")
- Attribute every number to its source table/column
- Add business context (is this high/low? what does this mean?)
- No re-aggregation or re-calculation — present what the query returned

---

## 7. The Smart Learning Loop

OptiFlow learns through three tiers:

### Tier 1 — In-Memory Cache (`core/query_cache.py`)

| Property | Value |
|----------|-------|
| Storage | Python dict (process memory) |
| Key | Normalized question string |
| TTL | 3,600 seconds (1 hour) |
| Eviction | On TTL expiry or manual `clear()` |
| Cleared by | Schema refresh, manual reset |

Best for: repeat questions within the same session or working day.

### Tier 2 — Approved Query Log (`logs/approved_queries.jsonl`)

Each approved and successfully-executed query is appended as a JSON line:

```json
{
  "question": "Show pending invoices for last month",
  "sql": "SELECT ...",
  "tables_used": ["Invoices", "Projects"],
  "row_count": 42,
  "execution_time_ms": 312,
  "approved_by": "admin",
  "approved_at": "2025-04-02T10:23:11",
  "flagged": false,
  "confirmed": false,
  "stale": false
}
```

**Feedback integration:**
- User clicks 👍 → `confirmed = true` — entry is trusted, preferred in future
- User clicks 👎 → `flagged = true` — entry is skipped by `find_similar()` indefinitely
- Table removed from schema → `stale = true` — entry skipped until re-approved

### Tier 3 — Fresh Generation

When neither cache nor log finds a match, Claude generates new SQL with full context. The result is cached (Tier 1) immediately and logged to Tier 2 after approval.

**Net effect:** A frequently-asked question costs one AI call on the first ask, then zero forever after.

---

## 8. API Reference — All Endpoints

### Authentication

| Method | Path | Auth Required | Description |
|--------|------|---------------|-------------|
| GET | `/` | Optional | Entry point — setup wizard (first run) or chat UI |
| GET | `/login` | No | Login form |
| POST | `/login` | No | Authenticate; sets `session_token` cookie |
| POST | `/logout` | Yes | Invalidate session |
| GET | `/setup/status` | No | `{setup_complete, admin_exists}` |

### Setup Wizard

| Method | Path | Description |
|--------|------|-------------|
| POST | `/setup/create-admin` | Create first admin account |
| POST | `/setup/test-ai-provider` | Verify API key + model (1-token test call) |
| POST | `/setup/save-ai-config` | Encrypt and save AI config to `model_config.json` |
| POST | `/setup/test-ollama` | Verify local Ollama is running (calls `/api/tags`) |
| POST | `/setup/test-connection` | Test DB credentials (connect attempt) |
| POST | `/setup/check-permissions` | Verify DB user is read-only; save `security.json` |
| POST | `/setup/discover-schema` | Run schema discovery (300s timeout); save all schema files + `schema.json` + suggested questions |
| POST | `/setup/generate-company-draft` | AI generates `company.md` draft from schema |
| POST | `/setup/company-followup` | AI generates 3–5 follow-up questions |
| POST | `/setup/save-company-knowledge` | Write `company.md` + optional follow-up answers |

### Chat & Query

| Method | Path | Description |
|--------|------|-------------|
| GET | `/welcome` | AI-generated welcome message (uses time-of-day + company knowledge) |
| GET | `/api/suggested-questions` | Returns cached chat chips; generates lazily on first call |
| POST | `/ask` | Main query pipeline — returns SQL for approval |
| POST | `/approve` | Execute approved SQL → interpret → persist to learning log |
| POST | `/reject` | Log SQL rejection (no execution) |
| POST | `/feedback` | Log thumbs-up/down; update `approved_queries.jsonl` |

#### `POST /ask` — Request & Response

```json
// Request
{ "question": "Show pending invoices for last month" }

// Response
{
  "mode": "agent",          // agent | chain | deep_dive | error
  "sql": "SELECT ...",
  "explanation": "This query...",
  "tables_used": ["Invoices"],
  "confidence": "high",
  "warnings": [],
  "from_cache": false,
  "requires_approval": true,
  "time_ms": 1240
}
```

#### `POST /approve` — Request & Response

```json
// Request
{
  "question": "...",
  "sql": "SELECT ...",       // or "steps": [...] for chain/deep_dive
  "tables_used": ["..."],
  "mode": "agent"
}

// Response
{
  "answer": "## Invoice Summary\n...",   // Markdown
  "rows_returned": 42,
  "time_ms": 890
}
```

### Admin

| Method | Path | Role | Description |
|--------|------|------|-------------|
| GET | `/admin/settings` | admin | Settings page (DB info, AI config, schema stats, runtime stats) |
| POST | `/admin/update-ai-config` | admin | Update AI key/model (tests before saving) |
| GET | `/admin/company` | admin | Company knowledge editor |
| POST | `/admin/company` | admin | Save `company.md` |
| GET | `/admin/audit` | admin | Audit log viewer (last 200 entries) |
| GET | `/admin/feedback` | admin | Feedback dashboard (accuracy %, negative list) |
| POST | `/admin/refresh-schema` | admin | Re-discover schema, compute diff, update everything |
| POST | `/admin/reset` | admin | Nuclear reset — wipes all config/logs/schema (requires password) |

---

## 9. Module Reference

### `core/agent_sql_generator.py`

The SQL generation engine. Never executes SQL — only reads context and drafts queries.

| Function | Description |
|----------|-------------|
| `generate_universal(question)` | Main entry point; resolves schema, selects tables, determines query type, generates SQL |
| `generate_sql(question)` | Single SELECT query generation |
| `generate_chain(question)` | Multi-step query chain (2–3 steps) for complex analytics |
| `generate_business_health_chain()` | 3–5 step executive summary chain based on schema's most critical tables |
| `generate_deep_dive_chain(entity_label, question)` | 4–5 step entity investigation across all related tables |
| `fix_sql(question, failed_sql, error, tables_used)` | AI-powered SQL fix with error context |
| `_select_tables_with_type(question)` | Cheap table selection call against schema_index.txt |
| `_load_schema()` | Reads `prompts/schema_context.txt`; raises FileNotFoundError if missing |
| `_load_company_knowledge()` | Reads `config/company.md`; returns `""` if missing |
| `_load_schema_index()` | Reads `prompts/schema_index.txt`; returns `None` if missing |

**Enforced SQL rules in every system prompt:**
- SQL Server syntax only (square bracket identifiers, `TOP N`, `GETDATE()`, `CONVERT()`)
- `SELECT` only — `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `EXEC`, `sp_` are banned
- `TOP 100` default unless the question asks for all records
- `LEFT JOIN` preferred over `INNER JOIN` to avoid silent data loss
- Meaningful column aliases (no `col1`, `a.x`)
- All numbers formatted (commas, 2 decimal places for currency)
- `GROUP BY` must list all non-aggregated columns

---

### `core/setup_manager.py`

| Function | Description |
|----------|-------------|
| `is_setup_complete()` | True if `schema_context.txt` AND `model_config.json` with `cloud_provider` key both exist |
| `get_db_connection(server, database, user, password)` | Tries ODBC Driver 18 then Driver 17; returns `(conn, driver_name, error)` |
| `run_schema_discovery(conn, db_name, server)` | Discovers all tables; writes schema files; returns table list dict |
| `verify_readonly_access(conn)` | Tests DB user permissions; returns `{access_level, message}` |
| `save_db_credentials(server, database, user, password)` | Encrypts password, writes `db_config.json` |
| `save_business_context(context)` | Writes `config/business_context.json` (legacy) |
| `save_schema_meta(tables_data, history_entry)` | Writes `config/schema.json` (table count, column count, refresh history) |
| `load_schema_meta()` | Returns contents of `config/schema.json` or `{}` |
| `load_old_schema_state()` | Reads current schema_index.txt + per-table files to get pre-refresh state |
| `mark_stale(removed_table_names)` | Marks entries in `approved_queries.jsonl` stale if their tables were removed |

**`verify_readonly_access()` levels:**

| Level | Condition | App Behaviour |
|-------|-----------|---------------|
| `readonly` | No INSERT/UPDATE/DELETE/DROP perms | Normal operation |
| `warning` | Some write perms detected | Continues but shows yellow banner in chat UI |
| `unknown` | Cannot query sys tables | Continues with warning |
| `blocked` | Has `sysadmin`, `db_owner`, or `db_datawriter` | `sys.exit(1)` — app refuses to start |

---

### `core/approved_queries.py`

| Function | Description |
|----------|-------------|
| `find_similar(question)` | Jaccard similarity search over `approved_queries.jsonl`; returns best match above 0.72 threshold, skipping flagged/stale |
| `log_entry(question, sql, tables_used, row_count, execution_time_ms, approved_by)` | Appends new approved query |
| `flag_entry(question)` | Sets `flagged=True` on matching entry (negative feedback) |
| `confirm_entry(question)` | Sets `confirmed=True` on matching entry (positive feedback) |
| `mark_stale(removed_table_names)` | Sets `stale=True` on entries that reference removed tables |

---

### `core/query_cache.py`

| Function | Description |
|----------|-------------|
| `get(question)` | Returns cached agent dict or `None` if missing/expired |
| `put(question, data)` | Stores result with current timestamp |
| `clear()` | Evicts all entries; returns count removed |
| `clear_expired()` | Evicts only TTL-expired entries |
| `size()` | Returns current entry count |

---

### `core/audit_logger.py`

Append-only structured log at `logs/audit.jsonl`. Thread-safe. Rotates at 10 MB, keeps 5 files (50 MB total history).

Auto-redacts values with keys: `password`, `api_key`, `secret`, `token`, `key`, `credentials` (recursive).

**Actions logged:** `login`, `logout`, `login_failed`, `query_agent_generated`, `query_agent_cached`, `query_chain`, `query_deep_dive`, `query_agent_approved`, `query_agent_rejected`, `feedback_positive`, `feedback_negative`, `setup_completed`, `update_ai_config`, `company_knowledge_updated`, `refresh_schema`, `reset_optiflow`

---

### `core/auth.py`

| Function | Description |
|----------|-------------|
| `users_exist()` | True if `config/users.json` exists and has at least one user |
| `find_user(username)` | Returns user dict or `None` |
| `verify_password(username, password)` | bcrypt check; returns True/False |
| `create_user(username, password, role)` | Hashes password (bcrypt rounds=12), appends to `users.json` |

---

### `core/db.py`

Loads credentials fresh from `config/db_config.json` on every call (no restart needed after setup).

| Function | Description |
|----------|-------------|
| `get_connection()` | Retry logic: 3 attempts, 2-second backoff |
| `execute_query(sql, params)` | Runs query; returns `list[dict]` (column names as keys) |

---

### `config/ai_client.py`

| Function | Description |
|----------|-------------|
| `get_completion(system, user, max_tokens, temperature)` | Reads provider/key/model from `model_config.json` on every call; routes to correct provider |
| `test_connection(provider, api_key, model, custom_endpoint)` | 1-token test call; returns `{success, error?}` |
| `RateLimitExhausted` | Exception raised on 429; carries `retry_after` seconds |

Rate limiter: 25 calls/minute (60-second rolling window). Sleeps up to 5 seconds if approaching limit; raises immediately if wait would exceed 5 seconds.

---

### `config/crypto.py`

| Function | Description |
|----------|-------------|
| `encrypt_secret(plaintext)` | Fernet-encrypts and returns URL-safe base64 token |
| `decrypt_secret(token)` | Decrypts; returns `""` (not plaintext) if decryption fails (e.g., after `.secret` is deleted) |

Key file `config/.secret` is auto-generated on first use if absent.

---

### `config/loader.py`

| Function | Description |
|----------|-------------|
| `load_ai_config()` | Returns `{provider, api_key, model, api_key_hint, custom_endpoint, local_enabled, local_endpoint, local_model}` |
| `load_db_config()` | Returns `{server, database, user, password}` (decrypted) |
| `load_model_config()` | Raw `model_config.json` dict (no decryption — for display only) |
| `load_business_context()` | Returns `business_context.json` dict or `{}` |
| `save_ai_config(data)` | Encrypts API key, writes `model_config.json` with `cloud_provider` structure |
| `save_db_config(data)` | Encrypts password, writes `db_config.json` |

Backward-compatible: handles both new `cloud_provider` structure and old flat `model_config.json` format.

---

## 10. Config & Runtime Files

### Files Created by Setup Wizard

| File | Content | Encrypted |
|------|---------|-----------|
| `config/users.json` | `{users: [{username, password_hash, created_at, role}]}` | Bcrypt hash (not reversible) |
| `config/.secret` | 44-byte Fernet key (raw bytes) | N/A — is the key |
| `config/model_config.json` | `{cloud_provider: {provider, api_key, api_key_hint, model}, local_provider: {enabled, endpoint, model}}` | API key: Fernet |
| `config/db_config.json` | `{server, database, user, password}` | Password: Fernet |
| `config/security.json` | `{access_level, message, db_user, last_checked}` | No |
| `config/company.md` | Business knowledge markdown | No |
| `config/schema.json` | `{last_refreshed, table_count, total_columns, refresh_history[]}` | No |
| `config/suggested_questions.json` | `[{label, question}]` — dynamic chat chips | No |

### Files Created by Schema Discovery

| File | Content |
|------|---------|
| `prompts/schema_context.txt` | Full schema: all tables with columns, types, row counts, categorical values |
| `prompts/schema_index.txt` | `TableName \| N,NNN rows \| description \| Key columns: Col1, Col2` |
| `prompts/tables/{TableName}.txt` | Per-table: all columns + types + nullable flag + categorical values |

### Runtime Logs

| File | Format | Rotation |
|------|--------|----------|
| `logs/audit.jsonl` | One JSON line per action | 10 MB / file, 5 files max |
| `logs/approved_queries.jsonl` | One JSON line per approved query | No rotation (append-only) |
| `logs/feedback.jsonl` | One JSON line per thumbs-up/down | No rotation |

---

## 11. Security Model

### Read-Only Enforcement

At startup (`@app.on_event("startup")`), `verify_readonly_access()` is called. If it returns `blocked` (user has sysadmin, db_owner, or db_datawriter), the process calls `sys.exit(1)` — the application will not start.

After setup, the permission level is displayed in Admin → Settings.

### Human-in-the-Loop

Every SQL query requires explicit human approval before execution:

1. User sends a question → receives a **preview card** with the SQL, an explanation, the tables it will touch, confidence level, and any warnings from `company.md`
2. User reads and clicks **Approve** or **Reject**
3. Only after Approve does `core/db.execute_query()` run

There is no automated execution path. `POST /approve` is the only endpoint that touches the database.

### LLM SQL Constraints

Every AI system prompt bans:
- `INSERT`, `UPDATE`, `DELETE`, `MERGE`
- `DROP`, `CREATE`, `ALTER`, `TRUNCATE`
- `EXEC`, `sp_`, `xp_`, `OPENROWSET`, `BULK`

Violation of these rules by the model would be a hallucination — the SQL would then be rejected by SQL Server since the DB user has SELECT-only permissions.

### Session Security

- Session tokens: 64-character hex (crypto-random via `secrets.token_hex(32)`)
- HttpOnly + SameSite cookie
- Inactivity TTL: 8 hours
- Sessions stored in-memory only (not persisted to disk; lost on restart)

### Encryption at Rest

API keys and database passwords are never stored in plaintext:
- First use: `config/.secret` is generated (44-byte Fernet key)
- Save time: secret is Fernet-encrypted → stored in JSON
- Load time: Fernet-decrypted in memory → used for API call → not persisted
- If `.secret` is deleted: `decrypt_secret()` returns `""` → API calls fail with "No API key configured" → user re-enters key via Settings

### Audit Trail

Every action is logged to `logs/audit.jsonl` with timestamp, username, action type, and scrubbed details. Sensitive values (passwords, API keys, tokens) are recursively replaced with `"[REDACTED]"` before logging.

---

## 12. The Frontend

### `templates/chat.html` — Main Chat Interface

The chat UI is a single-page JavaScript application with no framework dependency.

**Key components:**
- **Chat area** — displays user messages and AI responses (Markdown rendered via `marked.js`)
- **Suggested question chips** — dynamically fetched from `GET /api/suggested-questions` on page load; AI-generated from the actual database schema; hidden after first question is sent
- **Input bar** — text field + Send button; Enter key submits
- **SQL approval cards** — shown before execution; display SQL code block, explanation, tables, confidence, and any `company.md` warnings

**Three approval card modes:**

| Mode | Shows | Approve action |
|------|-------|----------------|
| `agent` (single) | SQL + explanation | Execute one query |
| `agent` (chain) | Step list with SQL for each | Execute all steps sequentially |
| `deep_dive` | Entity label + step list | Execute all steps, focused on one entity |

**After approval:**
- Loading spinner while executing
- Result displayed as rendered Markdown
- 👍 / 👎 feedback buttons appear
- Negative feedback prompts for optional comment

**Session restore:** Conversation history is saved in `sessionStorage`. On refresh, history is re-rendered without making new API calls. Hard-refresh (`Cmd+Shift+R`) clears history.

---

### `templates/settings.html` — Admin Settings

Organized into cards:
- **Database** — server, database name, DB user, permissions status (readonly / warning / blocked)
- **AI Provider** — provider badge (Anthropic / OpenAI / Custom), model name, masked key (`••••xxxx`), local model status
- **Database Schema** — table count, total columns, last refreshed timestamp; **Refresh Schema** button with inline diff result
- **Runtime** — cache entries, approved query count, server uptime, session count
- **Danger Zone** — **Reset OptiFlow** button (requires admin password confirmation); wipes all data except Python source files

---

### `templates/setup.html` — Setup Wizard

Single-page, multi-panel form. Progress tracked with a dot-indicator (5 steps). Uses JavaScript `fetch()` for all API calls with inline success/error display. Panels cannot be skipped — each must succeed before advancing.

---

## 13. AI Provider Configuration

### Supported Providers

| Provider | `provider` value | Notes |
|----------|-----------------|-------|
| Anthropic Claude | `anthropic` | Default; uses `anthropic` Python SDK |
| OpenAI | `openai` | Uses `openai` Python SDK |
| Custom endpoint | `custom` | Any OpenAI-compatible API (e.g., Azure, local vLLM); provide base URL |

### Model Configuration

Stored in `config/model_config.json`:

```json
{
  "cloud_provider": {
    "provider": "anthropic",
    "api_key": "<fernet-encrypted>",
    "api_key_hint": "K6G5",
    "model": "claude-sonnet-4-20250514",
    "custom_endpoint": ""
  },
  "local_provider": {
    "enabled": false,
    "endpoint": "http://localhost:11434",
    "model": "qwen3:8b"
  }
}
```

### Changing the API Key

Admin → Settings → AI Provider → **Change API Key** → enter new key → **Verify & Save**. The key is tested before saving. No restart required — `get_completion()` reads `model_config.json` on every call.

### Rate Limiting

`config/ai_client.py` enforces a 25 calls/minute rolling window. If approaching the limit, requests are queued (sleep up to 5 seconds). If the cloud provider returns HTTP 429, `RateLimitExhausted` is raised with the `Retry-After` header value. The frontend shows a countdown timer and auto-retries.

---

## 14. Schema Management

### Initial Discovery

Run automatically in Setup Step 4. Discovers all `BASE TABLE` types from `INFORMATION_SCHEMA.TABLES`, excluding `sys%` tables.

For each table:
- Column names, data types, nullable flag
- Row count (`SELECT COUNT(*)`)
- Categorical values: for columns with ≤30 distinct non-null values, all distinct values are stored (useful for status/type columns)

### Refresh Schema

Admin → Settings → Database Schema → **Refresh Schema** (`POST /admin/refresh-schema`).

Computes a diff against the previous state:

| Change type | Action |
|------------|--------|
| New table | Per-table file created; AI generates a new `company.md` section |
| Removed table | Per-table file deleted; note appended to `company.md`; matching approved queries marked `stale=True` |
| Row count change | Noted in diff (no action required) |
| New/removed columns | Noted in diff; per-table file updated |

After refresh:
- `config/schema.json` updated (new counts, history entry added)
- `config/suggested_questions.json` regenerated from updated schema
- In-memory query cache cleared
- Returns change summary JSON to the UI

### Schema Splitting (Large Databases)

For databases with **>15 tables**, OptiFlow uses a two-step approach to avoid sending the entire schema in every prompt:

1. **Table selection** — Send `schema_index.txt` (compact) + question → Claude returns relevant table names + query type
2. **SQL generation** — Load only those tables' detail files + system prompt → Claude generates SQL

This reduces token usage significantly for large schemas while maintaining accuracy.

---

## 15. Admin Operations

### Admin → Audit Log (`/admin/audit`)

Displays the last 200 audit entries in reverse chronological order. Shows: timestamp, username, action type, and pretty-printed detail JSON (passwords/keys are `[REDACTED]`).

### Admin → Feedback (`/admin/feedback`)

Dashboard showing:
- Total feedback count
- Positive / Negative counts and percentage accuracy
- List of all negative feedback entries with the question, SQL used, and user comment

### Admin → Company Knowledge (`/admin/company`)

Live markdown editor for `config/company.md`. Changes take effect on the next query (no restart needed — `_load_company_knowledge()` reads the file on each generation call).

**Effective company.md entries:**
```markdown
## Data Filters
- Always exclude records where status = 'TEST' — these are test entries
- Filter out rows where Created_Date = '2025-04-21'

## Terminology
- "Pipeline" refers to the Quotations table
- "AMC" means Annual Maintenance Contract (AMC_MASTER table)

## Business Rules
- Revenue = sum of Invoice_Amount where Payment_Status = 'Paid'
- Active projects: Project_Status IN ('Active', 'WIP', 'On Hold')
```

### Reset OptiFlow (`POST /admin/reset`)

Requires admin password confirmation. Deletes:
- All files in `config/` **except** `.py` files (preserves `ai_client.py`, `crypto.py`, `loader.py`, `settings.py`, `__init__.py`)
- All files in `prompts/` (schema files)
- All files in `logs/` (audit trail, approved queries, feedback)

Preserves: all Python source code, templates, `requirements.txt`, `start.sh`.

After reset, all sessions are invalidated and the setup wizard opens automatically on the next visit.
