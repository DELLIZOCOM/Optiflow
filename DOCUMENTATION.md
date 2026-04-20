# OptiFlow AI — Complete System Documentation

---

## Table of Contents

1. [What is OptiFlow AI?](#1-what-is-optiflow-ai)
2. [System Architecture Overview](#2-system-architecture-overview)
3. [High-Level Data Flow](#3-high-level-data-flow)
4. [How to Run](#4-how-to-run)
5. [Setup Wizard — Step by Step](#5-setup-wizard--step-by-step)
6. [Complete File Structure](#6-complete-file-structure)
7. [Backend — Module by Module](#7-backend--module-by-module)
8. [Agent Architecture — Deep Dive](#8-agent-architecture--deep-dive)
9. [Schema Discovery Pipeline](#9-schema-discovery-pipeline)
10. [Frontend Architecture](#10-frontend-architecture)
11. [SSE Streaming Protocol](#11-sse-streaming-protocol)
12. [Session Management](#12-session-management)
13. [AI Client & Provider Support](#13-ai-client--provider-support)
14. [Security Model](#14-security-model)
15. [API Reference](#15-api-reference)
16. [Config Files Reference](#16-config-files-reference)
17. [Dependencies](#17-dependencies)
18. [Resetting & Starting Over](#18-resetting--starting-over)

---

## 1. What is OptiFlow AI?

OptiFlow AI is an autonomous, conversational data analyst. It connects to your SQL database, understands your business, and answers plain-English questions by writing and executing SQL queries on your behalf — without you ever seeing or approving the SQL.

**No login. No SQL editor. No approvals.** Ask a question, get an answer.

### What it does

- Connects to SQL Server (Microsoft MSSQL), with PostgreSQL and MySQL stubs
- Discovers your schema automatically — tables, columns, data types, row counts, key relationships
- Builds a semantic map of your database (column roles, table types, FK relationships)
- Lets an AI agent autonomously plan and execute queries to answer your questions
- Streams the agent's reasoning and actions live as it works
- Retains conversation history within a session so you can ask follow-ups

### What it does NOT do

- Does not modify data — all SQL is validated as read-only before execution
- Does not require an internet connection to query your database (only the AI API call goes out)
- Does not have user authentication — intended for internal/trusted network use

---

## 2. System Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                             BROWSER (Frontend)                                │
│                                                                               │
│   chat.html / chat.js                     setup.html / setup.js               │
│   ─────────────────────                   ───────────────────────             │
│   • Renders chat messages                 • 5-step wizard UI                  │
│   • Trace panel (thinking + tools)        • DB connection test                │
│   • AbortController SSE streaming         • Schema discovery trigger          │
│   • Session storage (history)             • Business context editor           │
│   • Clear Chat / New Company buttons                                           │
└───────────────────────────┬──────────────────────────┬─────────────────────-─┘
                            │ SSE (POST /ask)            │ REST (POST /setup/*)
                            ▼                            ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                          FastAPI Application (app/)                            │
│                                                                               │
│  ┌──────────────┐   ┌───────────────┐   ┌──────────────────────────────────┐ │
│  │  routes/     │   │   routes/     │   │         routes/                  │ │
│  │  agent.py    │   │   setup.py    │   │         sources.py               │ │
│  │  POST /ask   │   │  /setup/*     │   │         /sources/*               │ │
│  │  SSE stream  │   │  wizard steps │   │  list / delete / rediscover      │ │
│  └──────┬───────┘   └──────┬────────┘   └──────────────────────────────────┘ │
│         │                  │                                                   │
│         ▼                  ▼                                                   │
│  ┌────────────────────────────────────────────────────────────────────────┐   │
│  │                     AgentOrchestrator                                  │   │
│  │                     (agent/orchestrator.py)                            │   │
│  │                                                                        │   │
│  │  ReAct Loop: Think → Call Tool → Observe → Repeat → Answer            │   │
│  │  • Builds system prompt dynamically (static + source + company.md)    │   │
│  │  • Calls AIClient with full message history + tool definitions         │   │
│  │  • Parses <thinking> blocks and tool calls from LLM response           │   │
│  │  • Executes tools via ToolRegistry                                     │   │
│  │  • Emits SSE events: status, thinking, tool_call, tool_result, answer  │   │
│  └────────┬─────────────────────────────────┬──────────────────────────--┘   │
│           │                                 │                                  │
│           ▼                                 ▼                                  │
│  ┌─────────────────┐              ┌──────────────────────┐                    │
│  │    AIClient      │              │    ToolRegistry       │                   │
│  │  (ai/client.py)  │              │   (tools/base.py)     │                   │
│  │                  │              │                       │                   │
│  │  AsyncAnthropic  │              │  list_tables          │                   │
│  │  RateLimiter     │              │  get_table_schema     │                   │
│  │  Anthropic API   │              │  execute_sql          │                   │
│  └────────┬─────────┘              │  get_business_context │                   │
│           │                        └──────────┬────────────┘                   │
│           │                                   │                                │
│           ▼                                   ▼                                │
│  ┌─────────────────┐              ┌──────────────────────┐                    │
│  │  Anthropic API  │              │   SourceRegistry      │                   │
│  │  (external)     │              │  (sources/base.py)    │                   │
│  └─────────────────┘              │                       │                   │
│                                   │  MSSQLSource          │                   │
│  ┌─────────────────┐              │  PostgreSQLSource     │                   │
│  │  SessionStore    │              │  MySQLSource          │                   │
│  │(agent/memory.py) │              └──────────┬────────────┘                   │
│  │  TTL=1hr         │                         │                                │
│  │  LRU=100 sess.   │                         ▼                                │
│  └─────────────────┘              ┌──────────────────────┐                    │
│                                   │   SQL Database        │                   │
│  ┌─────────────────┐              │   (via pyodbc)        │                   │
│  │   config.py     │              └──────────────────────-┘                   │
│  │  All file paths  │                                                          │
│  │  load/save AI   │                                                           │
│  │  load/save src  │                                                           │
│  └─────────────────┘                                                           │
└──────────────────────────────────────────────────────────────────────────────┘
                                        │
                             ┌──────────┴──────────┐
                             ▼                      ▼
                    ┌──────────────┐     ┌────────────────────┐
                    │  data/config/│     │   data/sources/    │
                    │  app.json    │     │  {name}/           │
                    │  .secret     │     │  schema_index.md   │
                    │  security.   │     │  relationships.md  │
                    │  json        │     │  tables/{T}.md     │
                    │  sources/    │     └────────────────────┘
                    │  {name}.json │
                    └──────────────┘
```

---

## 3. High-Level Data Flow

### 3.1 First-time Setup Flow

```
User opens browser → GET /
    │
    ├─ No source configured → redirect to GET /setup
    │
    ▼
Setup Wizard (5 steps)
    │
    ├── Step 1: AI Provider
    │   POST /setup/test-ai-provider  → Anthropic API test call
    │   POST /setup/save-ai-config    → Fernet-encrypt key → save to data/config/app.json
    │
    ├── Step 2: Test DB Connection
    │   POST /setup/test-connection   → pyodbc connect (ODBC Driver 18 → 17 fallback)
    │
    ├── Step 3: Check Permissions
    │   POST /setup/check-permissions → query sys.database_permissions
    │                                   → blocked / warning / readonly
    │                                   → save data/config/security.json
    │
    ├── Step 4: Discover Schema
    │   POST /setup/discover-schema   → MSSQLSource.discover_schema()
    │                                   → write schema_index.md
    │                                   → write tables/{T}.md (one per table)
    │                                   → write relationships.md
    │                                   → auto-save data/config/sources/{name}.json
    │                                   → register source in live SourceRegistry
    │
    └── Step 5: Business Context
        POST /setup/generate-company-draft → LLM drafts company.md from schema files
        POST /setup/company-followup       → LLM suggests follow-up questions
        POST /setup/save-company-knowledge → write data/knowledge/company.md
            │
            ▼
        Redirect to GET /  (chat page)
```

### 3.2 Chat Request Flow (per question)

```
User types question + hits Enter / Send
    │
    ▼
sendQuestion() in chat.js
    ├── Abort any in-flight SSE stream (AbortController)
    ├── addMessage(question, 'user')
    ├── setDisabled(true)
    └── _readSSE(POST /ask, {question, session_id})
            │
            ▼ (SSE connection opened)
        FastAPI: POST /ask
            │
            ▼
        AgentOrchestrator.ask_stream(question, session_id)
            │
            ├── get_or_create session → load message history
            ├── _build_system_prompt()
            │     ├── SYSTEM_PROMPT (static instructions)
            │     ├── Runtime context (IST date/time)
            │     ├── Connected Database section (source name + type)
            │     └── Business Context (data/knowledge/company.md)
            │
            └── ReAct Loop (max 15 iterations)
                    │
                    ├── Iteration N:
                    │   ├── yield {"type": "status", "message": "Thinking… (step N)"}
                    │   ├── AIClient.complete(messages, system, tools)
                    │   │       └── anthropic.AsyncAnthropic.messages.create(...)
                    │   │
                    │   ├── Parse response content blocks:
                    │   │   ├── text blocks → extract <thinking> tags
                    │   │   └── tool_use blocks → collect tool calls
                    │   │
                    │   ├── yield {"type": "thinking", ...} for each <thinking>
                    │   ├── yield {"type": "thinking", ...} for pre-tool reasoning text
                    │   │
                    │   ├── If stop_reason == "tool_use":
                    │   │   ├── For each tool call:
                    │   │   │   ├── yield {"type": "tool_call", "tool": ..., "input": ...}
                    │   │   │   ├── ToolRegistry.execute(tool_name, tool_id, input)
                    │   │   │   └── yield {"type": "tool_result", "result_summary": ..., "is_error": bool}
                    │   │   └── Append tool results → messages → loop
                    │   │
                    │   └── If stop_reason == "end_turn":
                    │       ├── save messages to SessionStore
                    │       └── yield {"type": "answer", "content": ..., ...}
                    │
                    ├── At iteration >= 13 (max-2): tools set to None → force text answer
                    │
                    └── yield "data: [DONE]\n\n"
            │
            ▼ (browser receives SSE events)
        chat.js event handlers:
            ├── "status"      → updateTraceStatus(panel, message)
            ├── "thinking"    → appendThinkingStep(panel, content)
            ├── "tool_call"   → appendToolCallStep(panel, tool, input)
            ├── "tool_result" → appendToolResult(stepEl, summary, isError)
            ├── "answer"      → collapseTrace(panel) + addMessage(content, 'ai')
            └── "error"       → collapseTrace(panel) + show error / rate-limit countdown
                │
                └── finally: unlock() → setDisabled(false) + input.focus()
```

### 3.3 Tool Execution Flow

```
ToolRegistry.execute(tool_name, tool_id, input)
    │
    ├── "list_tables"
    │   └── ListTablesTool.execute(input)
    │       ├── _resolve_source(registry, source_name)  ← auto-routes if single source
    │       ├── source.get_table_index()                 ← reads schema_index.md
    │       ├── _dialect_hint(db_type)                   ← SQL syntax rules for this DB
    │       └── source.get_relationships()               ← reads relationships.md
    │           → Returns: dialect hint + table directory + relationship map
    │
    ├── "get_table_schema"
    │   └── GetTableSchemaTool.execute(input)
    │       ├── _resolve_source(registry, source_name)
    │       └── source.get_table_detail(table_name) for each table
    │           → Returns: full column details with types, roles, sample values
    │
    ├── "execute_sql"
    │   └── ExecuteSQLTool.execute(input)
    │       ├── _resolve_source(registry, source_name)
    │       ├── _SELECT_RE.match(sql) → reject if not SELECT / WITH
    │       ├── source.execute_query(sql)
    │       │   └── MSSQLSource.execute_query()
    │       │       └── asyncio.run_in_executor → _execute_sync()
    │       │           └── pyodbc.connect → cursor.execute → fetchall
    │       ├── _format_table(rows)       → human-readable text table
    │       └── _build_structured_result(rows) → JSON metadata
    │           → Returns: RESULT PREVIEW (text) + RESULT JSON (structured)
    │
    └── "get_business_context"
        └── GetBusinessContextTool.execute(input)
            └── read data/knowledge/company.md
                → Returns: full company knowledge document
```

---

## 4. How to Run

### Requirements

- Python 3.11+
- ODBC Driver 18 for SQL Server (Mac: `brew install msodbcsql18`)
- An Anthropic API key (Claude model)
- Network access to your SQL Server

### Installation

```bash
# 1. Clone or unzip the project
cd optiflow-ai

# 2. Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate        # Mac / Linux
.venv\Scripts\activate           # Windows

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Install ODBC driver (Mac only — skip if already installed)
brew tap microsoft/mssql-release https://github.com/Microsoft/homebrew-mssql-release
brew install msodbcsql18
```

### Starting the server

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Then open `http://localhost:8000` in your browser.

- **First run:** the app redirects to `/setup` — complete the 5-step wizard
- **Subsequent runs:** goes straight to the chat interface
- **Reload sources without restart:** adding/removing a source via the wizard takes effect immediately

### Development mode (auto-reload on code changes)

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## 5. Setup Wizard — Step by Step

The setup wizard lives at `GET /setup` (served from `frontend/pages/setup.html`). It walks through 5 stages, each backed by a `/setup/*` endpoint.

### Step 1 — AI Provider

**Endpoint:** `POST /setup/test-ai-provider` → `POST /setup/save-ai-config`

- Accepts provider (`anthropic` / `openai` / `custom`), API key, and model name
- Optionally a local Ollama endpoint for a local LLM (agent mode requires Anthropic)
- Test call sends a 1-token request to verify the key and model are valid
- On save: API key is Fernet-encrypted before writing to `data/config/app.json`
- Hint (last 4 chars of key) is stored unencrypted for display

**Config written:**
```json
{
  "cloud_provider": {
    "provider": "anthropic",
    "api_key": "<fernet-encrypted>",
    "api_key_hint": "XXXX",
    "model": "claude-sonnet-4-6"
  },
  "local_provider": { "enabled": false, "endpoint": "...", "model": "..." }
}
```

### Step 2 — Test DB Connection

**Endpoint:** `POST /setup/test-connection`

- Accepts: server, database, username, password
- Tries `ODBC Driver 18 for SQL Server`, falls back to `ODBC Driver 17`
- Returns human-readable error messages for common failures (wrong password, server unreachable, driver not installed, timeout)
- Connection string: `DRIVER={...};SERVER=...;DATABASE=...;UID=...;PWD=...;TrustServerCertificate=yes;`

### Step 3 — Check Permissions

**Endpoint:** `POST /setup/check-permissions`

- Queries `sys.database_permissions` and `sys.database_role_members` for the current user
- Checks `IS_SRVROLEMEMBER('sysadmin')`
- Returns one of three access levels:
  - `blocked` — user has `CONTROL` permission or `sysadmin` role → cannot proceed
  - `warning` — user has write permissions (`INSERT`, `UPDATE`, `DELETE`, etc.) → proceeds with caution message
  - `readonly` — safe to use
- Result saved to `data/config/security.json`

### Step 4 — Discover Schema

**Endpoint:** `POST /setup/discover-schema`

Runs `MSSQLSource.discover_schema()` — the most complex step. See [Schema Discovery Pipeline](#9-schema-discovery-pipeline) for full detail.

**Outputs:**
- `data/sources/{name}/schema_index.md` — all tables with type, description, row count
- `data/sources/{name}/tables/{T}.md` — per-table: columns, types, roles, grain, relationships
- `data/sources/{name}/relationships.md` — confirmed FKs + inferred relationships + join paths
- `data/config/sources/{name}.json` — source config (auto-saved, password encrypted)
- Source registered in live `SourceRegistry` immediately (no restart needed)

### Step 5 — Business Context

**Endpoints:** `POST /setup/generate-company-draft` → `POST /setup/company-followup` → `POST /setup/save-company-knowledge`

- AI reads all schema files and generates a `company.md` document:
  - Company overview (inferred from schema)
  - Analytical guardrails (what each table represents, what not to mix)
  - Per-table guide (purpose, grain, key columns, typical joins)
  - Business process flow
  - Ambiguities to confirm
- Follow-up questions are generated to identify gaps (e.g. "What does Status='Pending' mean in INVOICE_DETAILS?")
- User reviews and edits the draft in a text editor in the wizard
- On save: written to `data/knowledge/company.md`
- Injected into the agent's system prompt on every subsequent chat request

---

## 6. Complete File Structure

```
optiflow-ai/
│
├── app/                            ← All server-side Python
│   ├── main.py                     ← FastAPI entry point, startup wiring, singleton creation
│   ├── config.py                   ← All file paths, load/save helpers for AI config + source configs
│   │
│   ├── agent/
│   │   ├── orchestrator.py         ← ReAct loop: builds prompt, calls LLM, dispatches tools,
│   │   │                              emits SSE events, manages forced final answer
│   │   ├── prompts.py              ← SYSTEM_PROMPT constant (company-agnostic agent instructions)
│   │   └── memory.py               ← SessionStore: thread-safe, TTL=1hr, LRU eviction at 100 sessions
│   │
│   ├── ai/
│   │   └── client.py               ← Unified LLM call site
│   │                                  AIClient (async, for agent loop, Anthropic-only)
│   │                                  get_completion() (sync, for setup wizard + company builder)
│   │                                  RateLimitExhausted exception
│   │                                  Module-level rate limiter (25 calls/min)
│   │
│   ├── sources/
│   │   ├── base.py                 ← DataSource Protocol definition + SourceRegistry
│   │   └── database/
│   │       ├── base.py             ← DatabaseSource base class:
│   │       │                          Semantic metadata (column roles, table types, grain, rels)
│   │       │                          Schema file writers (write_schema_index, write_table_file,
│   │       │                          write_relationships_file)
│   │       │                          Schema file readers (get_table_index, get_table_detail,
│   │       │                          get_relationships)
│   │       ├── mssql.py            ← MSSQLSource:
│   │       │                          connect() with ODBC Driver 18→17 fallback
│   │       │                          execute_query() async (via run_in_executor)
│   │       │                          discover_schema() full pipeline
│   │       │                          _query_pk_fk() INFORMATION_SCHEMA queries
│   │       │                          verify_readonly_access() permission check
│   │       ├── postgresql.py       ← PostgreSQLSource (stub — not yet implemented)
│   │       └── mysql.py            ← MySQLSource (stub — not yet implemented)
│   │
│   ├── tools/
│   │   ├── base.py                 ← BaseTool ABC, ToolResult dataclass, ToolRegistry
│   │   └── database.py             ← Tool implementations:
│   │                                  ListTablesTool      (list_tables)
│   │                                  GetTableSchemaTool  (get_table_schema)
│   │                                  ExecuteSQLTool      (execute_sql)
│   │                                  GetBusinessContextTool (get_business_context)
│   │                                  _DIALECT_HINTS dict, _resolve_source() auto-router
│   │                                  _format_table(), _build_structured_result()
│   │
│   ├── routes/
│   │   ├── agent.py                ← POST /ask (SSE stream), GET/DELETE /session/{id}
│   │   │                              event_stream() generator → yields SSE events → [DONE]
│   │   ├── setup.py                ← All /setup/* endpoints + _collect_schema_context()
│   │   │                              for company draft generation
│   │   └── sources.py              ← GET/DELETE/POST /sources and /sources/{name}/rediscover
│   │
│   └── utils/
│       ├── crypto.py               ← Fernet encryption: encrypt_secret(), decrypt_secret(),
│       │                              is_encrypted() — auto-generates .secret key on first use
│       └── helpers.py              ← safe_json() (NaN/Inf-safe JSONResponse),
│                                      sanitize_name() (source name slug generator)
│
├── frontend/
│   ├── pages/
│   │   ├── chat.html               ← Chat UI shell (no Jinja2 — served as FileResponse)
│   │   └── setup.html              ← Setup wizard (5-step, single-page JS-driven)
│   ├── css/
│   │   ├── chat.css                ← Chat styles: messages, trace panel, SQL disclosure,
│   │   │                              rate-limit notice, header buttons, thinking steps
│   │   └── setup.css               ← Wizard styles
│   └── js/
│       ├── chat.js                 ← Chat logic:
│       │                              SSE reader (_readSSE with AbortController)
│       │                              Trace panel (thinking, tool call, tool result, collapse)
│       │                              Session storage (chat history in sessionStorage)
│       │                              Clear Chat (DELETE /session/{id})
│       │                              New Company (POST /setup/reset)
│       │                              Rate-limit countdown + auto-retry
│       └── setup.js                ← Wizard logic: all 5 steps, AI test, DB connect,
│                                      schema discover, company draft/edit/save
│
├── data/                           ← All runtime data (git-ignored except .gitkeep)
│   ├── config/
│   │   ├── app.json                ← AI provider settings (encrypted API key)
│   │   ├── .secret                 ← 44-byte Fernet key (auto-generated; NEVER share)
│   │   ├── security.json           ← DB permission check results
│   │   └── sources/
│   │       └── {name}.json         ← One config file per connected source
│   ├── sources/
│   │   └── {name}/
│   │       ├── schema_index.md     ← All tables: name, type, description, row count
│   │       ├── relationships.md    ← Confirmed FKs, inferred joins, common join paths
│   │       └── tables/
│   │           └── {Table}.md      ← Per-table: type, grain, columns, roles, relationships,
│   │                                  categorical values
│   ├── knowledge/
│   │   └── company.md              ← Business context document (domain knowledge for agent)
│   └── logs/
│       ├── audit.jsonl             ← Audit log
│       └── queries.jsonl           ← Query log
│
├── requirements.txt
├── DOCUMENTATION.md                ← This file
└── PLAN.md                         ← Internal architecture planning notes
```

---

## 7. Backend — Module by Module

### `app/main.py` — Application Entry Point

Creates a FastAPI app, mounts static files, registers routes, and wires all singletons together during startup.

**Startup sequence (in `_startup`):**
1. `load_sources()` — reads `data/config/sources/*.json`, instantiates `MSSQLSource` / `PostgreSQLSource` / `MySQLSource`, registers in `SourceRegistry`
2. `build_tool_registry()` — calls `create_database_tools(source_registry)` → registers `list_tables`, `get_table_schema`, `execute_sql`, `get_business_context` in `ToolRegistry`
3. `setup_init(...)` — injects singletons into setup router
4. `sources_init(...)` — injects singletons into sources router
5. Creates `AgentOrchestrator` with all four singletons
6. Registers agent router (needs orchestrator to exist first)

**Module-level singletons** (shared across all requests):
```
_source_registry: SourceRegistry
_tool_registry:   ToolRegistry
_sessions:        SessionStore
_orchestrator:    AgentOrchestrator
```

**Routes served directly:**
- `GET /` → `frontend/pages/chat.html` (FileResponse)
- `GET /setup` → `frontend/pages/setup.html` (FileResponse)
- `GET /static/*` → entire `frontend/` directory

---

### `app/config.py` — Paths & Config I/O

Single source of truth for all file paths and JSON load/save operations.

**Key paths:**
```python
DATA_DIR           = project_root / "data"
CONFIG_DIR         = DATA_DIR / "config"
SOURCES_CONFIG_DIR = CONFIG_DIR / "sources"      # data/config/sources/
SOURCES_DATA_DIR   = DATA_DIR / "sources"         # data/sources/{name}/
KNOWLEDGE_DIR      = DATA_DIR / "knowledge"
LOGS_DIR           = DATA_DIR / "logs"
SECRET_PATH        = CONFIG_DIR / ".secret"
APP_CONFIG_PATH    = CONFIG_DIR / "app.json"
COMPANY_MD_PATH    = KNOWLEDGE_DIR / "company.md"
SECURITY_PATH      = CONFIG_DIR / "security.json"
```

**Key functions:**
- `load_ai_config()` — reads `app.json`, decrypts API key, returns flat dict
- `save_ai_config(data)` — encrypts API key, writes `app.json`
- `load_source_configs()` — reads all `data/config/sources/*.json`
- `save_source_config(config)` — encrypts password if plaintext, writes `{name}.json`
- `is_ai_configured()` — returns True if API key exists
- `is_setup_complete()` — returns True if AI configured AND at least one source exists

---

### `app/sources/base.py` — DataSource Protocol & SourceRegistry

**`DataSource` Protocol** (structural typing via `@runtime_checkable`):

Every connected data source must implement:
```
name              → str       identifier used in tool calls
source_type       → str       'mssql', 'postgresql', 'mysql'
description       → str       human-readable description
get_table_index() → str       reads schema_index.md
get_table_detail(name) → str  reads tables/{name}.md
get_database_name() → str     actual database name
get_db_type()       → str     dialect identifier
get_system_prompt_section() → str  dialect SQL rules
execute_query(sql) → list[dict]    async SQL execution
```

**`SourceRegistry`** — dict-backed registry:
```
register(source)     → adds source (keyed by name)
get(name)            → lookup by name (returns None if not found)
get_all()            → list of all sources
remove(name)         → removes source from registry
names()              → list of registered names
```

---

### `app/sources/database/base.py` — DatabaseSource & Semantic Metadata

The most complex module — handles schema file I/O and semantic enrichment.

**Semantic classification functions:**

`_classify_column_role(col_name, col_type, cardinality, row_count)` → returns one of:
| Role | Condition |
|------|-----------|
| `date_column` | datetime/date types, or name matches `date|time|_at|_on|created|updated` |
| `identifier` | name matches `_id|_pk|_fk|_code|_no|_number|_key|_ref` |
| `measure` | money types, or int + financial keyword (`amount|total|price|cost|...`) |
| `status` | string + ≤20 distinct values + name matches `status|state|type|category|stage` |
| `name_text` | string + name matches `name|title|description|address|email|phone|...` |
| `dimension` | string + ≤50 distinct values |
| `other` | binary/blob types, or no pattern matched |

`_classify_table_type(table_name, columns, row_count, pk_columns, all_relationships)` → returns one of:
| Type | Condition |
|------|-----------|
| `configuration` | row_count < 50 AND name contains `setting\|config\|param` |
| `junction` | composite PK (2+ columns) |
| `reporting` | name contains `target\|budget\|forecast\|summary\|report` |
| `transaction` | has both date columns AND measure columns |
| `reference` | referenced by other tables, or name contains `master\|lookup\|dict`, or only dimensions |

`_detect_grain(table_name, pk_columns, table_type)` → plain English:
- "One row = one {subject} line item" for detail tables
- "One row = one {subject} record" for master tables
- "One row = one {table} record (identified by {pk})" for PK-identified tables

`_infer_relationships(tables_data, confirmed_fks)` → cross-table column name matching:
1. Build map of column names that appear in 2+ tables
2. Skip generic names (`id`, `name`, `status`, `code`, etc.)
3. If column is PK in one table → that's the reference (one) side
4. If no PK, use cardinality ratio — higher ratio (more unique) = reference side
5. Returns list of inferred `{from_table, from_column, to_table, to_column, confidence: "inferred"}`

**Schema file writers:**
- `write_schema_index(tables_data, schema_dir, source_name, db_type)` → `schema_index.md`
- `write_table_file(table, tables_dir)` → `tables/{Name}.md` (with type, grain, roles, relationships, categorical values)
- `write_relationships_file(schema_dir, confirmed_fks, inferred_rels, tables_data)` → `relationships.md`

---

### `app/sources/database/mssql.py` — SQL Server Connector

**`MSSQLSource`** implements `DatabaseSource` for Microsoft SQL Server.

Key methods:

**`connect(server, database, user, password)`** → `(conn, driver, error)`
- Tries `ODBC Driver 18 for SQL Server` first, then `ODBC Driver 17`
- Translates raw pyodbc exceptions into human-readable messages
- Returns `(None, None, error_string)` on failure

**`execute_query(sql)`** (async)
- Runs in a thread-pool executor to avoid blocking the async event loop
- Retries up to 3 times with 2-second delays on transient failures
- Opens a fresh connection per query (stateless)
- Returns `list[dict]` — one dict per row

**`discover_schema(conn, db_name, server)`**
1. Get table names from `INFORMATION_SCHEMA.TABLES`
2. `_query_pk_fk()` — queries `INFORMATION_SCHEMA.TABLE_CONSTRAINTS` and `REFERENTIAL_CONSTRAINTS`
3. For each table: get columns, row count, categorical samples (string cols ≤ 100 chars with ≤ 30 distinct values)
4. `enrich_tables_data()` — adds column roles, table types, grain, relationships
5. Write schema files (index + per-table + relationships)

**`verify_readonly_access(conn)`**
- Queries `sys.database_permissions` and `sys.database_role_members`
- Checks for `sysadmin` server role
- Returns `access_level` = `blocked` / `warning` / `readonly`

---

### `app/tools/base.py` — Tool Primitives

**`ToolResult`** dataclass:
```python
tool_call_id: str   # Anthropic tool call ID (for message threading)
content: str        # Text content returned to the LLM
is_error: bool      # If True, LLM sees this as a tool error
metadata: dict      # Extra data (row_count, columns, etc.) — not sent to LLM
```

**`BaseTool`** ABC:
```python
name: str           # Anthropic tool name
description: str    # Tool description shown to LLM
parameters: dict    # JSON Schema for tool input
execute(input: dict) → ToolResult   # async implementation
```

**`ToolRegistry`**:
- `register(tool)` → stores by name
- `get_api_definitions()` → returns Anthropic-format `[{name, description, input_schema}]`
- `execute(tool_name, tool_call_id, input)` → dispatches, wraps exceptions as `is_error` results
- `clear()` → empties registry (used by reset)

---

### `app/tools/database.py` — Database Tool Implementations

**`_resolve_source(registry, source_name)`** — Smart routing:
1. Exact name match → use it
2. No match + exactly 1 source registered → auto-route to it
3. No match + multiple sources → return error listing available names

All four tools use `_resolve_source`, so the `source` parameter is always optional when only one database is connected.

**`ListTablesTool`** — Orientation call:
- Returns SQL dialect hint (from `_DIALECT_HINTS` dict) + schema_index.md content + relationships.md content
- Everything the agent needs to plan its queries in a single call

**`GetTableSchemaTool`** — Column detail:
- Reads per-table `.md` files
- Falls back to `.txt` format for backward compatibility
- Case-insensitive filename matching
- Accepts multiple table names; returns concatenated content

**`ExecuteSQLTool`** — Query runner:
- Rejects non-SELECT with `_SELECT_RE` regex check
- Calls `source.execute_query(sql)` (async)
- Returns both a human-readable text table and structured JSON metadata (row_count, columns, preview_rows)
- Error messages instruct the LLM to fix and retry

**`GetBusinessContextTool`** — Domain knowledge:
- Reads `data/knowledge/company.md`
- Called by LLM only when a business term isn't clear from schema metadata

---

### `app/ai/client.py` — Unified LLM Interface

**`AIClient`** (async, for agent loop):
- `complete(messages, system, tools, max_tokens=16000)` → full Anthropic response object
- Reads config fresh on every call (no restart needed after setup changes)
- Agent mode requires Anthropic — raises `NotImplementedError` for OpenAI/custom
- Catches `anthropic.RateLimitError` → raises `RateLimitExhausted(retry_after=N)`

**`get_completion(system, user, max_tokens=8000)`** (sync, for setup wizard):
- Supports Anthropic, OpenAI, and custom (OpenAI-compatible) endpoints
- Returns plain text string
- Used by company draft generation and follow-up question generation

**Module-level rate limiter:**
- Deque-based sliding window: 25 calls per 60 seconds
- If approaching limit: sleeps up to 5 seconds to queue the call
- If wait would be > 5 seconds: logs warning and proceeds immediately

---

### `app/agent/memory.py` — Session Store

**`SessionStore`** — thread-safe in-memory sessions:

| Property | Value |
|----------|-------|
| Default TTL | 3600 seconds (1 hour) |
| Max concurrent sessions | 100 (LRU eviction) |
| Storage | `dict[session_id, {messages, created_at, last_access}]` |
| Thread safety | `threading.Lock()` on all reads/writes |
| Session ID format | 16-char hex UUID fragment |

**Key methods:**
- `get_or_create(session_id)` — returns existing session if valid, creates new one otherwise
- `get_messages(session_id)` → copy of full Anthropic message list
- `set_messages(session_id, messages)` → updates history + last_access timestamp
- `destroy(session_id)` → immediate deletion (Clear Chat button)
- `clear_all()` → nuke all sessions (New Company reset)

Sessions store the full Anthropic message format, including all tool call and tool result blocks. This enables multi-turn conversations where the agent remembers what it already queried.

---

### `app/agent/orchestrator.py` — The ReAct Loop

The orchestrator is the heart of the system. `ask_stream()` is the main entry point.

**`_build_system_prompt()`** assembles three parts per request:
1. `SYSTEM_PROMPT` from `prompts.py` — static, company-agnostic instructions
2. Runtime context — current IST date/time, relative date interpretation rules
3. Connected Database — source name, type, database name; pointer to call `list_tables`
4. Business Context — full content of `data/knowledge/company.md` (if it exists)

**`ask_stream()` generator loop:**

```python
while iteration < max_iterations (15):
    iteration += 1

    # Throttle: minimum 200ms between LLM calls
    elapsed = time.monotonic() - _last_call_ts
    if elapsed < 0.2: await asyncio.sleep(0.2 - elapsed)

    # Force final answer at iteration 13+ (max-2)
    force_final = (iteration >= max_iter - 2)
    tools = None if force_final else registry.get_api_definitions()

    # LLM call
    response = await ai_client.complete(messages, system, tools)

    # Parse content blocks
    for block in response.content:
        if block.type == "text":
            extract <thinking> tags → emit "thinking" events
            keep remaining text
        elif block.type == "tool_use":
            collect tool blocks

    # Append assistant turn to message history

    # Emit thinking events (and pre-tool reasoning text as thinking)

    if stop_reason == "end_turn":
        save messages to session
        yield "answer" event
        return

    if stop_reason == "tool_use":
        for each tool_block:
            yield "tool_call" event
            result = await tool_registry.execute(...)
            yield "tool_result" event
        append tool results to messages
        continue loop

    # Unexpected stop_reason → yield error, return

# Loop exhausted without answer → yield error
```

**Forced final answer:**
- At `iteration >= max_iter - 2` (iteration 13 of 15), `tools` is set to `None`
- With no tools available, the LLM must produce a text answer from its accumulated knowledge
- Gives 2 full iterations without tools to compose a proper answer
- User always gets a response — never a silent iteration limit error

---

### `app/routes/agent.py` — SSE Endpoint

```python
@router.post("/ask")
async def ask(req: AskRequest, stream: bool = Query(default=True)):
    async def event_stream():
        try:
            async for event in orchestrator.ask_stream(question, session_id):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        finally:
            yield "data: [DONE]\n\n"    # always sent, even on error

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

- `[DONE]` is guaranteed in `finally` — browser always gets a clean stream termination
- `X-Accel-Buffering: no` disables nginx buffering for real-time streaming
- Non-streaming mode (`stream=false`) available for testing — returns full JSON

---

## 8. Agent Architecture — Deep Dive

### The System Prompt

Built fresh on each request by `_build_system_prompt()`. Contains:

**Part 1 — Static instructions (`prompts.SYSTEM_PROMPT`):**
- Agent role: expert data analyst
- Tool descriptions and when to use each
- 5-step workflow: Orient → Plan → Schema → Execute → Answer
- Efficiency rules (list_tables mandatory first, batch schema calls, 2 SQL max)
- SQL rules (SELECT only, explicit columns, TOP/LIMIT, ORDER BY, NULL handling, GROUP BY)
- Response guidelines (lead with direct answer, exact figures, plain language)
- Safety rules (read-only, no PII columns, say so if 0 rows returned)

**Part 2 — Runtime context (dynamic per request):**
```
Today is `2026-04-13`.
Current local datetime is `2026-04-13 14:30:22 IST`.
Interpret relative dates using this date and timezone unless the user specifies otherwise.
```

**Part 3 — Connected Database (dynamic, from live SourceRegistry):**
```
## Connected Database
Source: `my_db` | Type: MSSQL | Database: MyDatabase
Call `list_tables` to get the full schema directory, relationships, and dialect rules.
```

**Part 4 — Business Context (from `data/knowledge/company.md`):**
Full company knowledge document — table purposes, status meanings, business flows, guardrails.

### The Four Agent Tools

| Tool | Purpose | Called when |
|------|---------|-------------|
| `list_tables()` | Orientation — returns SQL dialect + all tables + relationships | First call every question |
| `get_table_schema(tables)` | Column detail — names, types, roles, sample values | After list_tables, before writing SQL |
| `execute_sql(sql, explanation)` | Run a SELECT query | When ready to retrieve data |
| `get_business_context(topic?)` | Domain knowledge lookup | When business term isn't clear from schema |

### Typical Agent Flow (4 iterations)

```
Iteration 1:
  <thinking> I'll start with list_tables. </thinking>
  → list_tables()
  ← dialect hint + table directory + relationship map

Iteration 2:
  <thinking>
  INVOICE_DETAILS is the transaction table (147 rows, transaction type).
  SQL Server dialect — use TOP N and GETDATE().
  Need INVOICE_DETAILS and CLIENT_MASTER, joined on Client_Code.
  </thinking>
  → get_table_schema(["INVOICE_DETAILS", "CLIENT_MASTER"])
  ← full column details with types, roles, sample values

Iteration 3:
  <thinking>
  INVOICE_DETAILS has Invoice_Date (date), Amount (decimal), Client_Code (varchar).
  CLIENT_MASTER has Client_Code (PK), Client_Name.
  I'll SUM(Amount) for the current month, grouped by client.
  </thinking>
  → execute_sql("SELECT TOP 20 cm.Client_Name, SUM(id.Amount) AS Total_Revenue ...")
  ← 12 rows returned

Iteration 4 (end_turn):
  Final answer in plain English with exact figures.
```

### Rate Limiting & Retry

When the Anthropic API returns HTTP 429:
- `AIClient.complete()` raises `RateLimitExhausted(retry_after=N)`
- Orchestrator catches it, saves message history, yields `{"type": "error", "retry_after": N}`
- Frontend shows countdown timer: "Rate limit reached. Retrying in 60s…"
- After countdown, `sendQuestion(question)` is automatically called again
- The session_id is preserved — the agent resumes with full history

---

## 9. Schema Discovery Pipeline

Triggered by `POST /setup/discover-schema`. Runs `MSSQLSource.discover_schema()`.

### Step-by-step

```
1. INFORMATION_SCHEMA.TABLES
   → list of all BASE TABLE names (excludes sys* tables)

2. INFORMATION_SCHEMA.TABLE_CONSTRAINTS + KEY_COLUMN_USAGE
   → pk_map: {table_name: [pk_col_names...]}

3. INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS
   → fk_list: [{from_table, from_column, to_table, to_column}]

4. For each table:
   a. COUNT(*) → row_count
   b. INFORMATION_SCHEMA.COLUMNS → columns (name, type, nullable)
   c. SELECT DISTINCT TOP 31 [{col}] for each varchar(≤100) column
      → if ≤ 30 distinct values: categorical dict {col_name: [val1, val2, ...]}

5. enrich_tables_data(tables_data, pk_fk_data):
   a. Assign pk_columns per table from pk_map
   b. For each column: cardinality from categorical count, role classification
   c. _infer_relationships() — cross-table column name matching
   d. Combine confirmed + inferred into all_relationships
   e. _classify_table_type() per table (needs all_relationships)
   f. _detect_grain() per table
   g. Attach relevant relationships per table

6. Write files:
   a. schema_index.md     ← markdown table of all tables
   b. tables/{T}.md       ← per-table schema (for each table)
   c. relationships.md    ← confirmed + inferred + join paths
```

### Schema File Formats

**`schema_index.md`:**
```markdown
# my_database (MSSQL)

| Table | Type | Description | Rows |
|-------|------|-------------|------|
| CLIENT_MASTER | Reference | Customer and client data | 127 |
| INVOICE_DETAILS | Transaction | Order and invoice records | 147 |
| ProSt | Transaction | Project-related records | 299 |
```

**`tables/INVOICE_DETAILS.md`:**
```markdown
# INVOICE_DETAILS

**Type**: Transaction table
**Grain**: One row = one INVOICE line item
**Row count**: 147
**Primary key**: Invoice_ID

## Columns

| Column | Type | Role | Nullable | Sample Values |
|--------|------|------|----------|---------------|
| Invoice_ID | int | identifier | NOT NULL | |
| Invoice_Date | date | date | NULL | |
| Client_Code | varchar(50) → CLIENT_MASTER.Client_Code | identifier | NULL | |
| Amount | decimal | measure | NULL | |
| Status | varchar(20) | status | NULL | "Draft", "Paid", "Pending" |

## Relationships
- **Client_Code** → CLIENT_MASTER.Client_Code (confirmed)
- **Project_Code** → ProSt.Project_Code (inferred)

## Categorical values
- **Status**: "Draft", "Paid", "Pending"
```

**`relationships.md`:**
```markdown
# Relationships: my_database

## Confirmed (from database constraints)
- INVOICE_DETAILS.Client_Code → CLIENT_MASTER.Client_Code
- PO_DETAILS.PO_No → PO_MASTER.PO_No

## Inferred (from column name matching)
- INVOICE_DETAILS.Project_Code → ProSt.Project_Code

## Common join paths
- INVOICE_DETAILS.Client_Code → CLIENT_MASTER.Client_Code
- INVOICE_DETAILS → ProSt → CLIENT_MASTER (via Project_Code/Client_Code)
```

---

## 10. Frontend Architecture

### `frontend/pages/chat.html`

Minimal HTML shell. No Jinja2 templating — served as `FileResponse`. Contains:
- Header with title, **Clear Chat** button, **New Company** button
- `#chatArea` div — all messages and trace panels appended here
- Input bar: `#questionInput` + `#sendBtn`
- Loads `marked.js` (markdown rendering) and `chat.js`

### `frontend/js/chat.js`

All chat logic. Key patterns:

**Module-level abort controller:**
```javascript
let _activeAbort = null;

async function sendQuestion(question) {
    // Cancel previous stream before starting new one
    if (_activeAbort) { _activeAbort.abort(); _activeAbort = null; }

    setDisabled(true);
    const ctrl = new AbortController();
    _activeAbort = ctrl;

    try {
        await _readSSE('/ask', {...}, ctrl.signal, onEvent);
    } catch (err) {
        if (err.name !== 'AbortError' && !answered) { /* show error */ }
    } finally {
        // Only unlock if this request wasn't superseded by a newer one
        if (_activeAbort === ctrl) _activeAbort = null;
        if (!isRetrying) unlock();
    }
}
```
This pattern prevents the "second question stuck" bug: when a new question aborts the old stream, the old stream's `finally` block detects `_activeAbort !== ctrl` and does not call `unlock()`.

**SSE reader (`_readSSE`):**
```javascript
async function _readSSE(url, body, signal, onEvent) {
    const res = await fetch(url, { method: 'POST', signal, ... });
    const reader = res.body.getReader();
    while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        // Parse "data: {...}\n\n" lines
        // If raw === '[DONE]': return
        // Else: onEvent(JSON.parse(raw))
    }
}
```

**Trace panel:**
- `createTracePanel()` → spinner + "Agent working…" header
- `appendThinkingStep(panel, content)` → brain emoji + reasoning text
- `appendToolCallStep(panel, tool, input)` → tool-specific icon + label + SQL in `<details>` (collapsed)
- `appendToolResult(stepEl, summary, isError)` → → arrow + result summary
- `collapseTrace(panel, stepCount)` → replaces spinning header with ✓ checkmark + step count + Show/Hide toggle

**Session persistence:**
- Chat history saved to `sessionStorage` under key `optiflow_chat_history_v2`
- Max 200 messages, FIFO overflow
- Restored on page reload
- Cleared on Clear Chat and New Company

**Clear Chat:**
```
clearChat()
  → abort in-flight stream
  → DELETE /session/{session_id}  ← clears server-side history
  → clear sessionStorage
  → reset UI
```

**New Company:**
```
resetData()
  → confirm dialog
  → abort in-flight stream
  → POST /setup/reset
  → clear sessionStorage
  → redirect to /setup
```

---

## 11. SSE Streaming Protocol

Server-Sent Events (SSE) format: each message is `data: {json}\n\n`.

Stream terminates with `data: [DONE]\n\n` (always sent in `finally`).

### Event Types

| Type | Fields | Description |
|------|--------|-------------|
| `status` | `message: str` | Progress text ("Thinking… step 3") |
| `thinking` | `content: str` | Agent's reasoning (from `<thinking>` tags or pre-tool text) |
| `tool_call` | `tool: str`, `input: dict` | Tool name and input parameters |
| `tool_result` | `tool: str`, `result_summary: str`, `is_error: bool` | One-line summary of what the tool returned |
| `answer` | `content: str`, `session_id: str`, `iterations: int`, `tools_used: list`, `queries_executed: int` | Final answer + metadata |
| `error` | `message: str`, `retry_after?: int` | Error. If `retry_after` present → show countdown |

### Frontend Event Handling

```
"status"      → updateTraceStatus(panel, event.message)
"thinking"    → stepCount++; appendThinkingStep(panel, event.content)
"tool_call"   → stepCount++; lastStepEl = appendToolCallStep(panel, event.tool, event.input)
"tool_result" → appendToolResult(lastStepEl, event.result_summary, event.is_error)
"answer"      → answered=true; collapseTrace(); addMessage(content, 'ai', meta)
"error"       → answered=true; collapseTrace()
                if retry_after: showRateLimitCountdown(N, () => sendQuestion(question))
                else: addMessage(error_message, 'ai', '⚠ Error')
```

---

## 12. Session Management

### Server-side (`SessionStore`)

```
┌─────────────────────────────────────────────────┐
│  SessionStore                                   │
│                                                 │
│  _sessions: {                                   │
│    "abc123def456789a": {                        │
│      messages: [                                │
│        { role: "user", content: "Q1" },         │
│        { role: "assistant", content: [...] },   │
│        { role: "user", content: [tool_result] },│
│        { role: "assistant", content: "Answer" } │
│        ...                                      │
│      ],                                         │
│      created_at: 12345.6,                       │
│      last_access: 12399.1                       │
│    }                                            │
│  }                                              │
│                                                 │
│  TTL: 1 hour from last access                   │
│  Max: 100 concurrent sessions (LRU evict)       │
└─────────────────────────────────────────────────┘
```

The `messages` list is the full Anthropic conversation format, including all tool call and tool result blocks. This is passed directly to the LLM on each turn, giving the agent complete context of what it already explored.

### Client-side

```
sessionStorage["agent_session_id"]    ← 16-char hex session ID
sessionStorage["optiflow_chat_history_v2"]  ← JSON array of {text, type, meta}
```

The session ID is sent with every `/ask` request. If the server session has expired (TTL), a new one is created transparently.

---

## 13. AI Client & Provider Support

### Provider Support Matrix

| Feature | Anthropic | OpenAI | Custom (OpenAI-compat) |
|---------|-----------|--------|------------------------|
| Setup wizard (test + save) | ✓ | ✓ | ✓ |
| Company draft generation | ✓ | ✓ | ✓ |
| Agent / chat mode | ✓ | ✗ | ✗ |

Agent mode requires Anthropic because it relies on Anthropic's native tool use API (structured `tool_use` content blocks). OpenAI function calling has a different API shape and is not implemented.

### Model

Configured during setup. Stored in `data/config/app.json`. Read fresh on every LLM call.

Recommended models (as of April 2026):
- `claude-sonnet-4-6` — best balance of speed and capability
- `claude-opus-4-6` — highest capability, slower
- `claude-haiku-4-5-20251001` — fastest, lowest cost

### Token Limits

| Use case | Default `max_tokens` |
|----------|---------------------|
| Agent loop (per iteration) | 16,000 |
| Company draft generation | 4,000 |
| Follow-up questions | 600 |
| Sync completions (general) | 8,000 |

---

## 14. Security Model

### Read-Only SQL Enforcement

Before any SQL reaches the database, it is validated with a regex:

```python
_SELECT_RE = re.compile(
    r"^\s*(?:--[^\n]*\n\s*|/\*.*?\*/\s*)*(WITH|SELECT)\b",
    re.IGNORECASE | re.DOTALL,
)
```

Only `SELECT` statements and `WITH ... SELECT` CTEs pass. Any other keyword (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `TRUNCATE`, `EXEC`) causes immediate rejection with an error message.

### Credential Encryption

All sensitive values are encrypted at rest using [Fernet symmetric encryption](https://cryptography.io/en/latest/fernet/) (AES-128-CBC + HMAC-SHA256):

```
data/config/.secret     ← 44-byte Fernet key (auto-generated on first use)
data/config/app.json    ← encrypted API key (gAAAA... prefix)
data/config/sources/*.json ← encrypted DB password (gAAAA... prefix)
```

The `.secret` file is never logged, never included in API responses, and should never be committed to version control. If it is deleted, all encrypted values become unreadable and credentials must be re-entered in setup.

`is_encrypted(value)` checks for the `gAAAA` prefix to avoid double-encrypting.

### No Authentication

The app has no login, no tokens, no RBAC. It is designed for:
- Local development (`localhost:8000`)
- Internal network with trusted users
- Behind a VPN

**Do not expose to the public internet without adding authentication.**

### PII Guardrails (Agent Instruction)

The agent system prompt instructs:
> "Do not include raw values from columns that appear to be passwords, tokens, or PII"

This is a soft guardrail (LLM instruction) — not a hard technical filter. Do not rely on it for regulated data handling.

---

## 15. API Reference

### Agent

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/ask` | Chat endpoint. Body: `{question, session_id?}`. Query: `stream=true` (default). Returns SSE stream or JSON. |
| `GET` | `/session/{id}` | Session status: `{session_id, exists, message_count, total_sessions}` |
| `DELETE` | `/session/{id}` | Clear session conversation history |

### Setup Wizard

| Method | Path | Body | Description |
|--------|------|------|-------------|
| `POST` | `/setup/test-ai-provider` | `{provider, api_key, model, custom_endpoint?}` | Validate API key with test call |
| `POST` | `/setup/save-ai-config` | `{provider, api_key, model, custom_endpoint?, local_enabled?, local_endpoint?, local_model?}` | Save AI config (encrypts key) |
| `POST` | `/setup/test-ollama` | `{endpoint}` | Test local Ollama server |
| `POST` | `/setup/test-connection` | `{source_type, server, database, user, password}` | Test DB connection |
| `POST` | `/setup/check-permissions` | Same as test-connection | Verify read-only DB access level |
| `POST` | `/setup/discover-schema` | `{source_type, source_name?, server, database, user, password}` | Full schema discovery + auto-save source |
| `POST` | `/setup/save-source` | `{name, type, description?, credentials}` | Explicitly save and register a source |
| `POST` | `/setup/generate-company-draft` | `{db_name?}` | AI generates company.md draft from schema |
| `POST` | `/setup/company-followup` | `{draft}` | AI generates 3-5 follow-up questions |
| `POST` | `/setup/save-company-knowledge` | `{content, followup_answers?}` | Save company.md |
| `GET` | `/setup/status` | — | `{setup_complete, ai_configured, source_count, sources}` |
| `POST` | `/setup/reset` | — | Full company reset (keeps AI config) |

### Sources Management

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/sources` | List all connected sources with summary |
| `GET` | `/sources/{name}` | Single source detail |
| `DELETE` | `/sources/{name}` | Remove source (config + schema data) |
| `POST` | `/sources/{name}/rediscover` | Re-run schema discovery for existing source |

### Static

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Chat page (`frontend/pages/chat.html`) |
| `GET` | `/setup` | Setup wizard (`frontend/pages/setup.html`) |
| `GET` | `/static/*` | Frontend assets (CSS, JS) |

---

## 16. Config Files Reference

### `data/config/app.json`

AI provider configuration. Written by setup step 1.

```json
{
  "cloud_provider": {
    "provider": "anthropic",
    "api_key": "gAAAAA...<fernet-encrypted>",
    "api_key_hint": "XXXX",
    "model": "claude-sonnet-4-6",
    "custom_endpoint": ""
  },
  "local_provider": {
    "enabled": false,
    "endpoint": "http://localhost:11434",
    "model": "qwen3:8b"
  }
}
```

### `data/config/sources/{name}.json`

One file per connected database. Auto-written during schema discovery.

```json
{
  "name": "my_database",
  "type": "mssql",
  "description": "MyDatabase on 192.168.1.100",
  "credentials": {
    "server": "192.168.1.100",
    "database": "MyDatabase",
    "user": "optiflow_reader",
    "password": "gAAAAA...<fernet-encrypted>"
  },
  "schema_discovered": true,
  "created_at": "2026-04-13T09:00:00+00:00"
}
```

### `data/config/security.json`

Written during permission check step. For audit purposes.

```json
{
  "db_user": "optiflow_reader",
  "access_level": "readonly",
  "permissions": ["SELECT", "CONNECT"],
  "roles": ["db_datareader"],
  "last_checked": "2026-04-13T09:00:00",
  "setup_warnings": []
}
```

### `data/config/.secret`

44-byte Fernet key. Auto-generated on first use. **Never commit this file.** Example (for illustration only):
```
VGhpcyBpcyBhIHNhbXBsZSBrZXkgZm9yIGlsbHVzdHJhdGlvbiBvbmx5AAAA
```

### `data/knowledge/company.md`

Business context document. AI-generated, human-reviewed. Injected into every agent request. Sections:
1. Company Overview
2. Analytical Guardrails (what each table is for, what not to mix)
3. Table Guide (per-table: purpose, grain, key columns, typical joins)
4. Business Process Flow
5. Ambiguities / Needs Confirmation

### `data/sources/{name}/schema_index.md`

Markdown table listing all tables. Example:
```markdown
# my_database (MSSQL)

| Table | Type | Description | Rows |
|-------|------|-------------|------|
| CLIENT_MASTER | Reference | Customer and client data | 127 |
| INVOICE_DETAILS | Transaction | Order and invoice records | 147 |
```

### `data/sources/{name}/relationships.md`

Source-level relationship map with confirmed constraints, inferred joins, and common multi-hop join paths.

### `data/sources/{name}/tables/{Table}.md`

Per-table schema enriched with semantic metadata:
- Table type and grain
- Column roles (identifier, measure, date, status, dimension, name/text)
- FK annotations on column types (where confirmed)
- Outgoing and incoming relationships
- Categorical value samples for status/dimension columns

---

## 17. Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `fastapi` | 0.135.3 | Web framework — routing, request parsing, SSE streaming |
| `uvicorn[standard]` | 0.44.0 | ASGI server with uvloop for async performance |
| `anthropic` | 0.92.0 | Anthropic Claude API — async agent loop, sync completions |
| `openai` | 2.31.0 | OpenAI API + OpenAI-compatible custom endpoints |
| `pyodbc` | 5.3.0 | ODBC driver interface for SQL Server |
| `cryptography` | 46.0.7 | Fernet symmetric encryption for API keys and DB passwords |
| `requests` | 2.33.1 | Sync HTTP (used for Ollama connection test) |

**Frontend (CDN, no install):**
- `marked.js` v12.0.1 — Markdown → HTML rendering for AI answers

**System dependency:**
```bash
# macOS
brew tap microsoft/mssql-release https://github.com/Microsoft/homebrew-mssql-release
brew install msodbcsql18

# Ubuntu/Debian
curl https://packages.microsoft.com/keys/microsoft.asc | apt-key add -
curl https://packages.microsoft.com/config/ubuntu/20.04/prod.list > /etc/apt/sources.list.d/mssql-release.list
apt-get update && ACCEPT_EULA=Y apt-get install msodbcsql18
```

---

## 18. Resetting & Starting Over

### Clear Chat (in-session)

Clears conversation history for the current session. Sources, schema, and business context are untouched. The agent starts fresh on the next question.

**Button:** "Clear Chat" in the chat header
**What it does:**
1. Aborts any in-flight SSE stream
2. `DELETE /session/{session_id}` — destroys server-side session
3. Clears `sessionStorage` (chat UI history + session ID)
4. Shows "Chat cleared" message

### New Company (full reset)

Destroys all company-specific data. AI provider settings are kept.

**Button:** "New Company" (🔄) in the chat header  
**Confirmation dialog required**  
**What it deletes:**
- `data/config/sources/*.json` — all source configs
- `data/sources/{name}/` — all schema directories (index + tables + relationships)
- `data/knowledge/company.md` — business context
- `data/config/security.json` — permission check results
- `data/logs/*.jsonl` contents — cleared (files kept)
- All in-memory sessions, source registry, tool registry

**What it keeps:**
- `data/config/app.json` — AI provider and model
- `data/config/.secret` — encryption key

After reset, redirects to `/setup` to run the wizard again for a new database.

### Manual data directory cleanup

If needed, you can also manually delete specific files:
```bash
# Remove a specific source
rm -rf data/sources/my_database/
rm data/config/sources/my_database.json

# Remove business context
rm data/knowledge/company.md

# Full data wipe (keep .secret and app.json)
find data/ -name "*.json" -not -name "app.json" -delete
rm -rf data/sources/*/
rm -f data/knowledge/company.md
```

---

---

## 19. Recent Changes (April 2026)

A log of material changes made in the April 2026 hardening pass. These are implemented and live, not proposals.

### 19.1 Context pruning — `_strip_tool_blocks`

**Problem:** every turn persisted the full `tool_use` / `tool_result` chain to `SessionStore`. By turn 5 the LLM was replaying 100+ KB of old SQL results on every request, causing 1–2 minute latency and inflated token bills.

**Fix:** `app/agent/orchestrator.py:_strip_tool_blocks()` is now the single persistence gate. It keeps only user strings and flattened assistant text; every save path (happy, error, rate-limit, force-final) routes through it. Legacy sessions are also stripped on load, so old bloat self-heals.

### 19.2 True token streaming — `AIClient.complete_stream`

**Problem:** the orchestrator was using `messages.create` (blocking) and buffering the whole response before emitting anything to the UI. The user stared at a static "Thinking…" bubble for 15–30s.

**Fix:** new async generator `AIClient.complete_stream()` wraps `client.messages.stream()`, yielding:

| Event | When |
|-------|------|
| `text_delta` | Each text chunk from the model |
| `tool_use_start` | Model begins a tool call |
| `rate_limit_wait` | 429 received, before retry |
| `rate_limit_tick` | 1 Hz countdown during the wait |
| `rate_limit_resume` | Wait done, retrying |
| `final_message` | Stream finished; full message object |

The dead non-streaming `AIClient.complete()` method was removed.

### 19.3 Tag-safe thinking stream — `_ThinkingStripper`

The model writes `<thinking>…</thinking>` in its text stream. We strip those tags before emitting to the UI, but chunks don't respect tag boundaries (`<thin` can end one chunk, `king>` the next). `_ThinkingStripper.feed()` holds back a partial-tag suffix until the next chunk arrives, so the client never sees half a tag.

Fresh stripper instances are created on `rate_limit_resume` so the retried stream starts clean.

### 19.4 Rate-limit handling — user-visible wait

**Problem:** the Anthropic SDK's default `max_retries=2` silently retried 429s for up to 20 seconds. The UI showed nothing; the user assumed the app was frozen.

**Fix:**
- `AsyncAnthropic(api_key=..., max_retries=0)` — disable silent retries.
- Custom retry loop in `complete_stream()` emits `rate_limit_wait` → `rate_limit_tick` (1 Hz) → `rate_limit_resume`, up to 3 attempts, capped at 90s per wait.
- Proactive throttle in `_async_record_call()` reads `anthropic-ratelimit-requests-remaining` from the last response. When the bucket is nearly empty it spaces the next call by 1–4s, which prevents most 429s before they happen.
- Frontend renders an inline banner inside the trace panel with a countdown, attempt counter, and shrinking progress bar.

### 19.5 Dead code + redundant gates

- Removed the 100ms gap check in the orchestrator — `AIClient._MIN_CALL_GAP_S` already enforces a stricter gate.
- Dropped `_MIN_CALL_GAP_S` from 0.5s to 0.2s (≈1.2s saved on a typical 4-iteration question).
- Removed unused `complete()` non-streaming method (~70 lines).

### 19.6 `company.md` cached in memory

`_build_system_prompt()` used to re-read `data/knowledge/company.md` from disk on every question. Now cached in module-level `_COMPANY_MD_CACHE` with mtime invalidation — edits to the file still take effect without restart, but normal requests skip the read.

### 19.7 Follow-up hint trimmed

**Before:** every follow-up question appended a 300-token system note telling the model to skip `list_tables` and always write `<thinking>`.

**After:** the full hint fires only on turn 2 (exactly one prior turn). Turns 3+ get a one-line reminder. The model carries the behavior forward from context after that.

### 19.8 Duplicate `<thinking>` instruction collapsed

`SYSTEM_PROMPT` and the follow-up hint both used to tell the model to write `<thinking>`. Now consolidated into a single clear rule in the system prompt.

### 19.9 `force_final` empty-answer fix

**Problem:** when the agent ran near the iteration limit, tools were disabled to force a final answer. The system prompt still said "always begin with `<thinking>`", so the model sometimes wrapped its entire answer inside `<thinking>…</thinking>`. The stripper removed it and the user saw an empty bubble ("No answer.").

**Fix (two-layer):**
1. On `force_final`, the system prompt is augmented with a **FINAL ANSWER MODE** section that explicitly forbids `<thinking>` and tool calls, and requires direct prose.
2. **Fail loud:** if the stream still ends with `stop_reason=end_turn` and no text, the orchestrator rolls back the turn and emits an `error` event with a user-facing "agent finished without producing an answer" message and a Retry button — no more silent empty bubbles.

Contract: **correct answer or an explicit error** — never a blank response.

### 19.10 Frontend revamp (chat.html / chat.css / chat.js)

- **Wider chat area** — `.chat-inner` capped at 1120px (was 900px); bubble `max-width` raised to 94%.
- **Larger typography** — base font 14px → 15px, AI message line-height 1.65 → 1.7, headings scaled up (h1 17→20, h2 15→17).
- **Better output rendering** — markdown tables get zebra striping, hover highlight, stronger header row; blockquotes use a 4px accent rail with filled background.
- **Trace panel improvements:**
  - Body capped at 240px — streaming thinking can't push the input bar off-screen.
  - `scrollTraceBottom(panel)` helper auto-scrolls *inside* the trace body (not the whole page) as text streams in, but only if the user is already near the bottom — if they've scrolled up to read an earlier step, their position is preserved.
  - Panel auto-collapses when the answer arrives; click "Show" to re-expand.
- **Removed** the "OF" header logo and the Enter/Shift+Enter/Esc keyboard-hint strip.
- **Session pill** with idle/running/error states and a pulse animation on "running".
- **Copy button** on AI messages; timestamps + query/step count badges.
- **Empty state** with sample-question chips.
- **Error messages** get a Retry button that re-sends the original question.
- **`sessionStorage` history key** bumped to `optiflow_chat_history_v3` for schema invalidation.

### 19.11 Orchestrator constants

| Constant | Value |
|----------|-------|
| `_MAX_ITERATIONS` | 15 (force_final triggers at iter 13) |
| `_MIN_CALL_GAP_S` | 0.2s |
| `_MAX_CALLS_PER_MIN` | 15 |
| Rate-limit retry cap | 3 attempts, 90s max wait per attempt |

### 19.12 Files touched in this pass

- `app/agent/orchestrator.py` — context pruning, streaming loop, force_final fix, fail-loud on empty answer, company.md cache, trimmed follow-up hint
- `app/agent/prompts.py` — consolidated `<thinking>` instruction
- `app/ai/client.py` — `complete_stream()`, `RateLimitExhausted`, proactive throttle, removed dead `complete()`
- `frontend/pages/chat.html` — removed logo + keyboard hints
- `frontend/css/chat.css` — full visual refresh (see §19.10)
- `frontend/js/chat.js` — streaming thinking, rate-limit banner, trace-body scroll, retry button, empty state

---

*Last updated: 2026-04-17*
