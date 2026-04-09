# OptiFlow AI — Documentation

## What is OptiFlow AI?

OptiFlow AI is an autonomous data analyst agent. You ask a question in plain English — the agent explores your database, writes and executes SQL queries, and returns a direct answer. No SQL approval step, no manual query editing.

**Core flow:**
```
You ask → Agent thinks → Agent calls tools → Agent writes SQL → Agent executes → Agent answers
```

No login. No admin panel. Run setup once, then ask questions.

---

## How to Run

```bash
# Activate virtual environment (first time only)
python -m venv .venv
source .venv/bin/activate        # Mac / Linux
.venv\Scripts\activate           # Windows

# Install dependencies (first time only)
pip install -r requirements.txt

# Start the server
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Then open `http://localhost:8000` in your browser.

On first run the setup wizard opens automatically. On subsequent runs it goes straight to chat.

---

## Setup Wizard

| Step | What happens |
|------|-------------|
| **1 — AI Provider** | Enter your Anthropic API key and model name. Key is encrypted and saved. |
| **2 — Test Connection** | Enter SQL Server host, database, username, password. Connection is verified. |
| **3 — Check Permissions** | Confirms the DB user is read-only. Warns if it has write/admin access. |
| **4 — Schema Discovery** | Scans all tables, columns, row counts, and sample values. Writes `.md` schema files to `data/sources/{name}/`. Auto-saves the source config. |
| **5 — Business Context** | AI drafts a `company.md` document describing your business from the schema. You review, edit, and save it. |

After completing setup, all config is saved to `data/config/`. Re-running the app skips the wizard if a source is already configured.

**Reset:** Click the **New Company** button in the chat header to clear all source data, schema files, and business context. AI provider settings are kept.

---

## File Structure

```
optiflow-ai/
│
├── app/                            ← All server-side Python
│   ├── main.py                     ← FastAPI entry point, startup wiring, route registration
│   ├── config.py                   ← All file paths, config load/save helpers
│   │
│   ├── agent/
│   │   ├── orchestrator.py         ← ReAct agent loop: think → tool call → observe → answer
│   │   ├── prompts.py              ← Static system prompt (company-agnostic instructions)
│   │   └── memory.py               ← SessionStore: in-memory conversation history (TTL + LRU)
│   │
│   ├── ai/
│   │   └── client.py               ← Unified LLM call site (Anthropic async + sync, OpenAI, custom)
│   │
│   ├── sources/
│   │   ├── base.py                 ← DataSource Protocol + SourceRegistry
│   │   └── database/
│   │       ├── base.py             ← DatabaseSource base class, schema file I/O (.md read/write)
│   │       ├── mssql.py            ← SQL Server connector (connect, execute, schema discovery)
│   │       ├── postgresql.py       ← PostgreSQL stub (not yet implemented)
│   │       └── mysql.py            ← MySQL stub (not yet implemented)
│   │
│   ├── tools/
│   │   ├── base.py                 ← BaseTool ABC, ToolResult, ToolRegistry
│   │   └── database.py             ← list_tables, get_table_schema, execute_sql, get_business_context
│   │
│   ├── routes/
│   │   ├── agent.py                ← POST /ask (SSE streaming), GET/DELETE /session/{id}
│   │   ├── setup.py                ← POST /setup/* (all wizard endpoints), POST /setup/reset
│   │   └── sources.py              ← GET/DELETE /sources, POST /sources/{name}/rediscover
│   │
│   └── utils/
│       ├── crypto.py               ← Fernet encryption for API keys and DB passwords
│       └── helpers.py              ← safe_json(), sanitize_name()
│
├── frontend/
│   ├── pages/
│   │   ├── chat.html               ← Main chat UI
│   │   └── setup.html              ← Setup wizard (5-step)
│   ├── css/
│   │   ├── chat.css                ← Chat styles (messages, trace panel, header buttons)
│   │   └── setup.css               ← Wizard styles
│   └── js/
│       ├── chat.js                 ← Chat logic: SSE reader, trace panel, Clear Chat, New Company
│       └── setup.js                ← Wizard logic: AI test, DB connect, schema discover, company draft
│
├── data/                           ← All runtime data (git-ignored)
│   ├── config/
│   │   ├── app.json                ← AI provider, encrypted API key, model name
│   │   ├── .secret                 ← Fernet encryption key (auto-generated, never share)
│   │   ├── security.json           ← DB permission check results
│   │   └── sources/
│   │       └── {name}.json         ← One config file per connected source (encrypted password)
│   ├── sources/
│   │   └── {name}/
│   │       ├── schema_index.md     ← Markdown table: table name, description, row count
│   │       └── tables/
│   │           └── {Table}.md      ← Per-table schema: columns, types, nullability, sample values
│   ├── knowledge/
│   │   └── company.md              ← Business context document (edited in setup step 5)
│   └── logs/
│       ├── audit.jsonl             ← Audit log
│       └── queries.jsonl           ← Query log
│
├── requirements.txt
└── DOCUMENTATION.md
```

---

## Agent Architecture

### ReAct Loop

The agent runs an autonomous Reason → Act → Observe loop. Every question starts a new iteration cycle:

```
User question
    │
    ▼
Build system prompt
  (static instructions + source name + company.md)
    │
    ▼
┌─────────────────────────────────────────┐
│  LLM call with tool definitions          │
│                                          │
│  1. LLM writes <thinking> block          │
│  2. LLM calls a tool                     │
│  3. Tool executes, result returned       │
│  4. LLM observes result, thinks again    │
│  5. Repeat until stop_reason=end_turn    │
└─────────────────────────────────────────┘
    │
    ▼
Final answer streamed to browser (SSE)
```

Events emitted during the loop:
| Event type | Payload |
|------------|---------|
| `status` | Progress message ("Thinking… step N") |
| `thinking` | Content of each `<thinking>` block |
| `tool_call` | Tool name + input |
| `tool_result` | Summary of what the tool returned |
| `answer` | Final answer text + metadata |
| `error` | Error message + optional `retry_after` |

### System Prompt Construction

Built fresh on every request by `orchestrator._build_system_prompt()`:

1. **Static instructions** (`prompts.SYSTEM_PROMPT`) — generic agent behaviour, SQL rules, efficiency rules. Never changes.
2. **Connected Database** — source name, db type, database name. Injected from the live SourceRegistry.
3. **Business Context** — full contents of `data/knowledge/company.md`. Gives the agent domain knowledge so it doesn't waste iterations rediscovering table purposes.

### Agent Tools

| Tool | When called | What it does |
|------|-------------|-------------|
| `get_table_schema(tables)` | Before writing SQL | Returns column names, types, nullability, sample values for requested tables. Request all needed tables in one call. |
| `execute_sql(sql, explanation)` | To retrieve data | Runs a SELECT query, returns formatted rows. Auto-routes to the connected source. |
| `list_tables()` | Rarely needed | Returns `schema_index.md` — only needed if Business Context doesn't describe the tables. |
| `get_business_context(topic?)` | Rarely needed | Re-reads `company.md` — only needed for specific term clarification. |

**Source auto-routing:** All tools accept an optional `source` parameter. If omitted (or wrong), the tool auto-routes to the single connected source. If multiple sources are connected, the `source` parameter is required and available names are listed in the system prompt.

### Forced Final Answer

At `max_iterations - 1`, the orchestrator makes one final LLM call with no tools available. The model must produce a text answer from whatever data it gathered, so the user always gets a response — never an error due to iteration limits.

### Session Memory

Conversation history is stored in `SessionStore` (in-memory, TTL 1 hour, LRU eviction at 100 sessions). Each session preserves the full Anthropic message list across turns, enabling follow-up questions.

- **Clear Chat** button: calls `DELETE /session/{id}` — clears history for the current session. Agent starts fresh. Sources and knowledge are untouched.
- **New Company** button: calls `POST /setup/reset` — deletes all source configs, schema files, logs, and company knowledge. AI config is kept. Redirects to setup wizard.

---

## Data Flow: Schema Discovery

When you run schema discovery in the setup wizard, `MSSQLSource.discover_schema()`:

1. Queries `INFORMATION_SCHEMA.TABLES` for all base tables
2. For each table: gets column names/types/nullability, row count, and distinct values for short string columns (≤ 30 unique values → treated as categorical)
3. Writes `data/sources/{name}/schema_index.md` — a markdown table listing all tables
4. Writes `data/sources/{name}/tables/{Table}.md` — one file per table with full column detail
5. Auto-saves source config to `data/config/sources/{name}.json` (password Fernet-encrypted)
6. Registers the source in the live `SourceRegistry` immediately — no restart needed

**Schema file format:**

`schema_index.md`:
```markdown
# source_name (MSSQL)

| Table | Description | Rows |
|-------|-------------|------|
| CLIENT_MASTER | Customer and client data | 1,842 |
| INVOICE_DETAILS | Order and invoice records | 5,230 |
```

`tables/CLIENT_MASTER.md`:
```markdown
# CLIENT_MASTER

**Row count**: 1,842

## Columns

| Column | Type | Nullable | Sample Values |
|--------|------|----------|---------------|
| client_ID_PK | int | NOT NULL | |
| client_Code | nvarchar(20) | NULL | "CL001", "CL002", "CL003" |
| client_Status | int | NULL | "1", "0" |
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Serve chat page |
| `GET` | `/setup` | Serve setup wizard |
| `POST` | `/ask` | Agent chat — SSE streaming |
| `GET` | `/session/{id}` | Session status |
| `DELETE` | `/session/{id}` | Clear session history |
| `POST` | `/setup/test-ai-provider` | Validate API key + model |
| `POST` | `/setup/save-ai-config` | Save AI provider config |
| `POST` | `/setup/test-connection` | Test DB credentials |
| `POST` | `/setup/check-permissions` | Verify read-only DB access |
| `POST` | `/setup/discover-schema` | Run schema discovery + auto-save source |
| `POST` | `/setup/save-source` | Explicitly save + register a source |
| `POST` | `/setup/generate-company-draft` | AI drafts company.md from schema |
| `POST` | `/setup/company-followup` | Generate follow-up questions for company draft |
| `POST` | `/setup/save-company-knowledge` | Save company.md |
| `GET` | `/setup/status` | Setup completion status |
| `POST` | `/setup/reset` | Full reset (keeps AI config) |
| `GET` | `/sources` | List all connected sources |
| `GET` | `/sources/{name}` | Source details |
| `DELETE` | `/sources/{name}` | Remove a source |
| `POST` | `/sources/{name}/rediscover` | Re-run schema discovery |

---

## Config Files

### `data/config/app.json`
AI provider config. Written by setup step 1.
```json
{
  "cloud_provider": {
    "provider": "anthropic",
    "api_key": "<fernet-encrypted>",
    "api_key_hint": "SK12",
    "model": "claude-sonnet-4-20250514"
  },
  "local_provider": {
    "enabled": false,
    "endpoint": "http://localhost:11434",
    "model": "qwen3:8b"
  }
}
```

### `data/config/sources/{name}.json`
One file per connected database. Written during schema discovery.
```json
{
  "name": "my_database",
  "type": "mssql",
  "description": "MyDatabase on 192.168.1.100",
  "credentials": {
    "server": "192.168.1.100",
    "database": "MyDatabase",
    "user": "optiflow_reader",
    "password": "<fernet-encrypted>"
  },
  "schema_discovered": true
}
```

### `data/config/.secret`
Auto-generated 44-byte Fernet key. Used to encrypt/decrypt the API key and DB passwords. **Never commit or share this file.** If deleted, re-enter credentials in setup.

### `data/knowledge/company.md`
Markdown document describing your business — what the company does, what each table is for, what business terms mean, key relationships between tables. Written during setup, injected into the agent's system prompt on every request. Edit it to improve answer quality.

---

## Security Model

- **Read-only enforcement:** All SQL is validated against a regex before execution — only `SELECT` and `WITH…SELECT` CTEs are allowed. `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `TRUNCATE`, and `EXEC` are rejected immediately.
- **Encrypted credentials:** API keys and DB passwords are Fernet-encrypted at rest in `data/config/`. Never stored in plaintext or `.env`.
- **No SQL approval gate:** The agent executes queries autonomously. It is designed for trusted internal use. Do not expose to the public internet.
- **No login:** The app has no authentication. Intended for internal/local network use only.
- **PII handling:** The agent is instructed not to include raw values from columns that appear to contain passwords, tokens, or personal data.

---

## Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `fastapi` | 0.135.3 | Web framework |
| `uvicorn[standard]` | 0.44.0 | ASGI server (with uvloop, watchfiles, websockets) |
| `anthropic` | 0.92.0 | Anthropic Claude API client (async + sync) |
| `openai` | 2.31.0 | OpenAI API client (also used for custom endpoints) |
| `pyodbc` | 5.3.0 | SQL Server ODBC driver |
| `cryptography` | 46.0.7 | Fernet encryption for secrets |
| `requests` | 2.33.1 | HTTP calls (Ollama connection test) |

**ODBC driver (required for SQL Server):**
```bash
brew tap microsoft/mssql-release https://github.com/Microsoft/homebrew-mssql-release
brew install msodbcsql18
```
