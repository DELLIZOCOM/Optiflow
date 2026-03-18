# OptiFlow AI — Complete Technical Documentation

> A self-learning natural language interface for BizFlow ERP data. Ask questions in plain English, get structured business insights powered by Claude AI, local Ollama models, and SQL Server. OptiFlow gets smarter over time by learning from how you use it.

---

## Table of Contents

1. [What Is OptiFlow AI?](#what-is-optiflow-ai)
2. [Architecture & The Smart Learning Loop](#architecture--the-smart-learning-loop)
3. [The Three Processing Modes](#the-three-processing-modes)
4. [How a Question Flows Through the System](#how-a-question-flows-through-the-system)
5. [Project Structure](#project-structure)
6. [Module Deep-Dives](#module-deep-dives)
   - [config/settings.py](#configsettingspy)
   - [app.py — The Server](#apppy--the-server)
   - [core/intent_parser.py — Cloud Parser (Claude)](#coreintent_parserpy--cloud-parser-claude)
   - [core/local_intent_parser.py — Local Parser (Ollama)](#corelocal_intent_parserpy--local-parser-ollama)
   - [core/query_engine.py — Template Query Router](#corequery_enginepy--template-query-router)
   - [core/agent_sql_generator.py — Agent Mode Brain](#coreagent_sql_generatorpy--agent-mode-brain)
   - [core/db.py — Database Connection](#coredbpy--database-connection)
   - [core/filter_injector.py — Safety Net](#corefilter_injectorpy--safety-net)
   - [core/response_formatter.py — Human-Readable Answers](#coreresponse_formatterpy--human-readable-answers)
7. [The Intent System](#the-intent-system)
   - [How Intents Work](#how-intents-work)
   - [Intent Registry](#intent-registry)
   - [All Available Intents](#all-available-intents)
   - [Match Confidence System](#match-confidence-system)
   - [Meta Intents](#meta-intents)
   - [Retired Intents](#retired-intents)
8. [Agent Mode — Dynamic SQL Generation](#agent-mode--dynamic-sql-generation)
   - [Single SQL Generation](#single-sql-generation)
   - [Query Chaining (Multi-Step)](#query-chaining-multi-step)
   - [Deep Dive (Pre-Built Chains)](#deep-dive-pre-built-chains)
   - [The Human-in-the-Loop & Caching Cycle](#the-human-in-the-loop--caching-cycle)
   - [Result Interpretation via Claude](#result-interpretation-via-claude)
9. [The System Prompts](#the-system-prompts)
   - [Intent Parsing Prompt](#intent-parsing-prompt)
   - [Schema Context (Agent Mode)](#schema-context-agent-mode)
10. [The Frontend](#the-frontend)
    - [Template Mode UI](#template-mode-ui)
    - [Agent Mode UI Cards](#agent-mode-ui-cards)
    - [Chain Mode UI Cards](#chain-mode-ui-cards)
    - [Deep Dive UI Cards](#deep-dive-ui-cards)
    - [Markdown Rendering](#markdown-rendering)
11. [Database Tables](#database-tables)
12. [Environment Variables](#environment-variables)
13. [How to Run](#how-to-run)
14. [Testing](#testing)
15. [How to Add a New Intent](#how-to-add-a-new-intent)
16. [Known Data Quirks](#known-data-quirks)

---

## What Is OptiFlow AI?

OptiFlow AI is designed as a mature, self-learning product that sits on top of **Ecosoft Zolutions' BizFlow ERP system**. Rather than relying on static reports or a rigid internal tool, managers interact with their data dynamically by typing questions like:

- *"How many projects are in the pipeline?"*
- *"Show me pending invoices"*
- *"Compare Hyundai vs Inalfa across all tables"*
- *"Tell me everything about project P-2024-001"*

The system understands the question, decides whether to use a lightning-fast pre-built template or dynamically generate custom SQL, and returns a formatted, actionable answer — complete with insights, alerts, and data.

Most importantly, OptiFlow is not just a static codebase; it is a **learning product**. As users ask novel questions, the AI generates custom SQL, human supervisors approve it, and the system caches and reuses it. This means **each company's OptiFlow gets smarter over time through their own usage**, organically building a tailored library of highly accurate queries without requiring a developer to write new code.

**Key capabilities:**
- **The Smart Learning Loop** — Auto-generated SQL → Human Approved → Cached → Reused.
- **Template Mode** — 23 pre-built SQL templates for common business questions (instant, zero-cost).
- **Agent Mode** — Claude AI dynamically generates custom SQL for novel questions.
- **Query Chaining** — Multi-step investigations with up to 3 sequential SQL queries.
- **Deep Dive** — Pre-built 360° analysis chains for individual projects or customers.
- **Local LLM Support** — Switchable intent parsing between Claude API and local Ollama models for privacy and cost savings.

---

## Architecture & The Smart Learning Loop

OptiFlow AI operates on a modular, multi-path architecture designed to balance speed, cost, and extreme flexibility. The system flow can be broken down into specialized layers:

**1. The Client Layer (Frontend):**
The browser sends the user's plain-English question as a JSON payload to the FastAPI backend. It also handles the interactive review UI where managers must explicitly approve or reject any AI-generated SQL before it touches the database.

**2. The Understanding Layer (Intent Parsing):**
Upon receiving a query, the system first tries to understand its intent using either a highly efficient local LLM (Ollama) or a cloud-based LLM (Claude). The parser's only job is to return a structured intent and a "match confidence" score (high, medium, or low). It does not generate SQL or interact with the database.

**3. The Routing Layer:**
Based on the match confidence, the system makes a crucial split:
- If the confidence is **high**, the question matches a known, pre-built SQL template. It routes instantly to the **Template Engine**, which injects parameters and executes the query at lightning speed.
- If the confidence is **medium or low**, the question is entirely new. It routes to the **Agent Engine**, which uses Claude AI possessing full database schema context to dynamically author custom SQL.

**4. The Execution & Safety Layer:**
Regardless of which path the query takes, it must pass through the Filter Injector — an un-bypassable safety net that forcefully appends mandatory data-quality WHERE clauses (e.g., stripping out migrated test records). For Agent-generated queries, execution is paused until a human clicks "Approve."

**5. The Product Learning Pattern:**
When an Agent-generated query is approved and successfully executes, it enters the **Smart Learning Loop**. The query, alongside its original natural language question, is written to an persistent log (`approved_queries.jsonl`) and held in an in-memory cache. 

**The Product Pattern in Action:**
Auto-generated SQL → Human Approved → Cached → Reused.

The next time a user asks that exact same question, OptiFlow intercepts it at the Routing Layer. It bypasses the expensive, slow LLM generation process and immediately serves the known-good, human-verified SQL from its cache. In this way, OptiFlow transitions from a blank-slate AI into a highly specialized, company-specific data product — learning and adapting its intelligence entirely from the managers who use it.

---

## The Three Processing Modes

OptiFlow AI has three processing modes, chosen automatically based on the question:

| Mode | When Used | How It Works | Cost | Speed |
|------|-----------|-------------|------|-------|
| **Template** | Common questions with `match_confidence: high` | Pre-built SQL template → execute → format | Free (no SQL generation API call) | Fast (~1-2s) |
| **Agent** | Novel questions with `medium` or `low` confidence | Claude generates custom SQL → human approves → cached → execute → interpret | API call to Claude (or Free if cached) | Slow (~3-8s) |
| **Deep Dive** | "Tell me everything about project/customer X" | Pre-built multi-step SQL chain → human approves → execute → interpret | Free generation (interpretation uses API) | Medium |

### Mode Selection Logic

- **"High" Confidence**: Direct route to TEMPLATE MODE.
- **"Medium" Confidence**: The question touches a known domain but requires specific analysis, prediction, or comparison. Routes to AGENT MODE.
- **"Low" Confidence**: The question spans multiple domains, needs custom aggregation, or simply has no template. Routes to AGENT MODE.
- **"Deep_Dive"**: Entity-specific 360° investigation. Routes to DEEP DIVE MODE.

---

## How a Question Flows Through the System

### Template Mode: "Pending invoices?"

1. The browser sends `POST /ask` with `{"question": "Pending invoices?"}`.
2. The **Intent Parser** processes the text and returns `{"intent": "invoices_pending", "match_confidence": "high"}`.
3. **Routing**: Because the confidence is high, the system routes the request to Template Mode.
4. The **Query Engine** pulls the `invoices_pending` SQL template from the internal registry, binds any required parameters, and injects mandatory safety filters.
5. The **Database** executes the SQL and returns the raw rows.
6. The **Formatter** translates the raw data into a human-readable text block featuring an insight lead, an alert, formatted data points, and caveats.
7. The complete response is sent back to the user instantly.

### Agent Mode (First Time): "Compare Hyundai vs Inalfa across all tables"

1. The user asks the novel question.
2. The **Intent Parser** struggles to map this to a single template and returns `{"intent": "unknown", "match_confidence": "low"}`.
3. **Routing**: Lower confidence triggers Agent Mode.
4. The system checks its internal cache and `approved_queries.jsonl` log. Finding no match, it sends the question and the full database schema to the **Agent SQL Generator** (Claude).
5. Claude authors a complex, multi-table SQL query (or chain of queries) to answer the comparison, and attaches an explanation.
6. This generated SQL is sent back to the browser as a **Preview Card**, waiting for human approval.
7. The user reviews the SQL and clicks **Approve & Run**.
8. The server injects safety filters and executes the SQL against the database.
9. **The Learning Step**: The approved SQL and the original question are securely saved to `approved_queries.jsonl` and cached in memory.
10. Claude interprets the numerical results into a plain-English summary, which is sent back to the user.

### Agent Mode (Subsequent Times): The Reused Query

1. Another user (or the same user later) asks: *"Compare Hyundai vs Inalfa across all tables"*.
2. The system checks the cache/log and finds an exact or highly similar match that was previously verified by a human.
3. The expensive LLM generation step is entirely bypassed. The system instantly loads the proven, cached SQL from the log.
4. The user is presented with the preview card, clearly marked that this query is reusing a previously approved structure.
5. Upon approval, it executes instantly, proving the product's ability to self-optimize and learn the company's specific reporting needs.

### Deep Dive Mode: "Tell me everything about customer HNTI"

1. The Intent Parser recognizes the entity request and returns `{"intent": "deep_dive", "entity_type": "customer", "entity_id": "HNTI", "entity_name": "HNTI"}`.
2. **Routing**: The system routes directly to Deep Dive Mode.
3. The system pulls a **5-step pre-built SQL chain** corresponding to a customer 360-view (no LLM generation required).
4. A customized deep dive preview card is presented to the user displaying all 5 investigation steps.
5. The user approves, the 5 queries execute sequentially, and Claude synthesizes the massive data return into a tidy, executive summary.

---

## Project Structure

```
optiflow-ai/
├── app.py                        # FastAPI server — routes + pipeline orchestration
├── requirements.txt              # Python dependencies
├── .env                          # Secrets + config (DB credentials, API key, parser mode)
├── .gitignore                    # Git exclusions
│
├── config/
│   ├── __init__.py
│   └── settings.py               # Loads .env variables including INTENT_PARSER_MODE
│
├── core/                         # Business logic
│   ├── __init__.py
│   ├── intent_parser.py          # Cloud parser — Claude API
│   ├── local_intent_parser.py    # Local parser — Ollama (qwen2.5-coder:3b)
│   ├── agent_sql_generator.py    # Agent Mode — SQL generation, chaining, deep dives
│   ├── query_engine.py           # Maps intents → SQL → database execution
│   ├── db.py                     # Database connection + query execution
│   ├── filter_injector.py        # Injects mandatory WHERE clauses
│   ├── query_cache.py            # In-memory caching for the learning loop
│   ├── approved_queries.py       # Manages the approved_queries.jsonl persistent log
│   └── response_formatter.py     # Turns raw DB rows into human-readable text
│
├── intents/                      # Intent definitions (SQL templates + metadata)
│   ├── __init__.py               # Merges all intents into INTENT_REGISTRY
│   ├── project_intents.py        # 7 intents for ProSt table
│   ├── finance_intents.py        # 4 intents for invoices + payments
│   ├── amc_intents.py            # 4 intents for AMC contracts
│   ├── ops_intents.py            # 4 intents for operations
│   └── target_intents.py         # 3 intents for targets + tickets
│
├── logs/
│   └── approved_queries.jsonl    # Persistent log of all human-approved AI queries
│
├── prompts/
│   ├── system_prompt.txt         # Intent parser system prompt (all intents + rules)
│   └── schema_context.txt        # Full database schema for Agent Mode SQL generation
│
├── templates/
│   └── chat.html                 # Frontend UI (HTML + CSS + JS, all inline)
│
└── tests/
    ├── test_db.py                # Database connection tests
    ├── test_filters.py           # Filter injector unit tests
    ├── test_pipeline.py          # Query engine unit tests (routing, params, retired)
    ├── test_queries.py           # Live query execution tests for all 23 intents
    ├── test_agent_sql.py         # Agent Mode SQL generation integration tests
    ├── test_intents.py           # Intent definition validation tests
    └── ground_truth.json         # Intent parser accuracy test fixtures
```

---

## Module Deep-Dives

### config/settings.py

**Purpose**: Load secrets and configuration from the `.env` file so no credentials are hardcoded.

```python
DB_SERVER = os.getenv("DB_SERVER")           # e.g., 192.168.1.198
DB_NAME = os.getenv("DB_NAME")              # e.g., Ezee_BizFlow_Original
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
INTENT_PARSER_MODE = os.getenv("INTENT_PARSER_MODE", "local")  # "local" or "cloud"
```

The `INTENT_PARSER_MODE` setting controls which intent parser is used:
- `"local"` → Uses Ollama with `qwen2.5-coder:3b` (free, runs locally, ~10s timeout)
- `"cloud"` → Uses Claude API via `claude-sonnet-4-20250514` (paid, more accurate)

Uses `python-dotenv` to read from `.env` at import time.

---

### app.py — The Server

**Purpose**: FastAPI application featuring 4 primary routes and full pipeline orchestration, acting as the traffic controller for the entire product.

| Route | Method | What It Does |
|-------|--------|-------------|
| `/` | GET | Serves `templates/chat.html` |
| `/welcome` | GET | Runs 4 parallel health checks, returns a greeting message |
| `/ask` | POST | Accepts `{"question": "..."}`, routes logically, manages the cache, and formulates response |
| `/approve` | POST | Executes human-approved SQL, interprets via Claude, and saves it to the learning log |
| `/reject` | POST | Logs user rejection of generated SQL |

**Key features:**

- **Switchable parser**: Dynamically imports the designated intent parser based on `INTENT_PARSER_MODE`.
- **Timeouts**: Implements strict timeouts (Pipeline 30s, Agent execution 30s, Chain execution 90s, Welcome 15s) to ensure the UI never hangs.
- **Async execution**: Uses `asyncio.wait_for()` + `run_in_executor()` ensuring synchronous DB calls never block the fast event loop.
- **Safe JSON serialisation**: Custom JSON handler gracefully manages `Decimal`, `datetime`, and `date` database types.
- **Smart Loop Implementation**: Logic inside `_run_pipeline` checks `query_cache` and `approved_queries` before making expensive LLM calls. Upon `/approve`, it finalises the learning loop by writing to the log.

---

### core/intent_parser.py — Cloud Parser (Claude)

**Purpose**: Takes a plain English question and extracts a structured intent dictionary via the Claude API.

**How it works:**
1. Loads `prompts/system_prompt.txt` as the system instruction.
2. Formats the user's string and queries Claude.
3. Claude responds with JSON indicating the `intent`, extracted parameters (like `days`), and importantly, a `match_confidence`.
4. The system normalises this structure (assigning defaults if Claude misses something) and returns it.

**Error handling:**
The parser implements strict fallback mechanisms for empty questions, API drops, and malformed JSON, defaulting to "unknown" intents to ensure the application never crashes completely.

---

### core/local_intent_parser.py — Local Parser (Ollama)

**Purpose**: A highly efficient, zero-cost drop-in replacement for the Claude intent parser, powered by Ollama on the local network.

**How it works:**
1. Shares the identical system prompt as the Cloud parser.
2. Hooks into Ollama's local generation endpoint utilizing the fast `qwen2.5-coder:3b` model.
3. Implements the exact same parsing signature, making the transition seamless.

**Graceful degradation:**
If Ollama goes offline, times out, or hallucinates, the parser captures the error and returns a safety-net dict: `{"intent": "unknown", "match_confidence": "low"}`. This triggers Agent Mode (which uses Claude API for SQL generation), guaranteeing the user still gets their answer even if the local parser fails.

---

### core/query_engine.py — Template Query Router

**Purpose**: The central nervous system for Template Mode. Maps recognized intents to their corresponding SQL blueprints, binds user parameters securely, and executes.

**Key components:**

#### `run(intent_dict)` — Main function
Coordinates intent lookup, Meta Intent routing, Retired Intent tracking, parameter binding via `_bind_params`, filter enforcement via `filter_injector`, execution, and result formatting. 

#### `_bind_params(sql, intent_dict, defaults)` — Parameter binding
A critical security layer that replaces text tokens like `[PLACEHOLDER]` into parameterized SQL markers (`?`). This neutralises SQL injection vectors inherently before they ever reach the database driver. 

---

### core/agent_sql_generator.py — Agent Mode Brain

**Purpose**: Takes any natural language question and drafts safe, read-only SQL via Claude, enriched with the entire database schema context. **This module NEVER executes SQL** — it exclusively builds preview drafts.

**Three generation modes:**

#### `generate_sql(question)` — Single SQL
Passes the database schema, business rules, and table structures to Claude. Claude outputs the exact SQL required alongside an explanation and confidence metric.

#### `generate_chain(question)` — Multi-step investigation
When a question demands cross-referencing or filtering across disparate silos (e.g. "Find cancelled projects, then get their invoices"), Claude splits the task into sequential, logical database queries.

#### `generate_deep_dive(entity_type, entity_id, entity_name)` — Pre-built chains
Triggers elaborate, hard-coded SQL chains (4-5 steps) for comprehensive "360-degree" entity overviews, requiring zero LLM latency.

**Safety baked in:**
The prompt architecture strictly enforces `SELECT` operations only, expressly banning destructive operations, mandating `LEFT JOINs`, and enforcing `TOP 100` constraints to protect database health.

---

### core/db.py — Database Connection

**Purpose**: Connects securely to the SQL Server providing reliable execution environments.

**Key Details:**
- Initiates Driver 18 for SQL Server connections over ODBC.
- Implements robust retry-logic (up to 3 distinct attempts with 2s cooldowns) anticipating transient network turbulence.
- Packages raw cursor arrays into easily consumable list-of-dictionary structures for the formatter layers.

---

### core/filter_injector.py — Safety Net

**Purpose**: An uncompromising, un-bypassable security module ensuring no query executes against the database without requisite data-hygiene filters.

**Mandatory filters by table:**

| Table | Filter | Why |
|-------|--------|-----|
| `ProSt` | `Created_Date != '2025-04-21'` | Eradicates 150+ dummy test records left over from a previous migration event. |
| `ProSt` | `PIC NOT IN ('XXX','NONE','66','25','64')` | Removes junk assignments ensuring analytics only reflect real managers. |
| `ProSt` | `PIC IS NOT NULL` | Cleanses unassigned project data. |
| `AMC_MASTER` | `Status IS NOT NULL` | Stabilizes AMC queries. |
| `AMC_MASTER` | `Status != ''` | Catches empty string anomalies alongside NULLs. |

The injector utilizes regex parsing to detect if the requisite WHERE clauses already exist in the SQL string, dynamically modifying the query AST-style prior to execution. This applies uniformly to both Template Mode and AI-authored Agent Mode queries.

---

### core/response_formatter.py — Human-Readable Answers

**Purpose**: Translates raw JSON dictionary arrays into eloquent, digestible narratives meant for non-technical business managers. **Used exclusively in Template Mode**.

**Response structure:**
1. **Insight lead** — A clear statement on what the data represents (e.g., "89 active projects identified in pipeline").
2. **Alert** — Contextual warnings focusing on the "so what?" (e.g., "Notice: Pipeline is exceptionally top-heavy").
3. **Data listing** — The quantified data structured identically via numbered lists.
4. **Caveats** — Transparent notes explicitly detailing any intentional exclusions or data limitations.

Features include seamless Indian currency localization (Lakhs/Crores), auto-truncation beyond 10 items, and intelligent contextual triggers (e.g., assessing the current day of the month when formulating target-progress alerts).

---

## The Intent System

### How Intents Work

An **intent** operates as a predefined, parameterized SQL template.

```python
{
    "name": "amc_expiry",                    # Unique identifier
    "description": "...",                     # What it does (for docs)
    "table": "AMC_MASTER",                   # Primary table queried
    "sql": "SELECT ... FROM AMC_MASTER ...",  # SQL template
    "params": {"DAYS": 60},                   # Default parameter values
    "caveats": ["..."],                       # Data quality notes
    "retired": False,                         # Is this intent deprecated?
    "redirect_to": None,                      # Where to redirect if retired
}
```

### Intent Registry
All domain-specific intents are coalesced inside `intents/__init__.py`, forming the `INTENT_REGISTRY`. This collection of 23 distinct templates encompasses Project logic, Financial health, Maintenance Contracts, Operations, and Core Target metrics.

### Match Confidence System
Every natural language input is graded with a `match_confidence` metric by the AI. This is the lynchpin determining whether the system utilizes a fast, strict Template, or initiates the dynamic, AI-heavy Agent process. 

### Meta & Retired Intents
OptiFlow understands that reporting demands change. 
- **Meta Intents**: The `business_health` intent acts as an orchestrator, triggering 6 sub-intents simultaneously to fabricate an overarching "executive digest".
- **Retired Intents**: As data fidelity changes, old intents (like `ops_overdue` relying on a broken date column) are retired. Rather than breaking, OptiFlow elegantly redirects user queries to healthier adjacent metrics, appending an explanatory note.

---

## Agent Mode — Dynamic SQL Generation

For queries exceeding the boundaries of established templates, Agent Mode acts as the system's dynamic data retrieval brain. This is where the Smart Learning Loop truly begins. 

### Single SQL Generation
When a user inputs a fundamentally new question, Claude reviews the schema, generates specific SQL logic, maps out utilized tables, assigns a confidence score, and returns an explanation. 

### Query Chaining (Multi-Step)
Some queries require synthesis across completely unrelated data silos ("Are there delayed projects that also have unpaid invoices?"). The Agent recognizes this and authors sequential step-by-step SQL queries, structuring a complex investigation automatically.

### Deep Dive (Pre-Built Chains)
A massive quality-of-life feature allowing "Tell me everything about X" prompts. These trigger complex, 5-step analysis chains exploring entity linkages (Client -> Projects -> Invoices -> Operations) resulting in total business visibility out of a single prompt. 

### The Human-in-the-Loop & Caching Cycle

This is OptiFlow's most significant product differentiation feature:

1. **Generation & Verification**: The LLM outputs structural SQL which is presented transparently to the user within a UI "Preview Card". The manager validates the logic (or rejects it).
2. **Execution & Translation**: Only upon pressing "Approve" does the SQL hit the database. The raw data returns are fed back to Claude, which acts as a data storyteller, interpreting the metrics into a clear business paragraph.
3. **The Intelligence Cache**: Upon success, OptiFlow memorializes the exact Natural Language Question alongside the structural SQL in the `approved_queries.jsonl` log. 
4. **Permanent Re-usability**: Future queries utilizing similar conceptual framing bypass the generation queue entirely. The system pulls the human-verified SQL straight from the cache, drastically reducing latency, eliminating continuous API costs, and cementing institutional knowledge into the product's architecture. 

---

## The System Prompts

### Intent Parsing Prompt
Residing at `prompts/system_prompt.txt`, this calibrates the intent parser. It documents all 19 active intents, dictates `match_confidence` rules through calibration arrays, enforces strict JSON-only outputs, and manages terminology nuance.

### Schema Context (Agent Mode)
At `prompts/schema_context.txt`, this provides the "worldview" to Claude. This includes complete column mappings, mandatory exclusions, relationship structures, and the absolute rules governing Read-Only query formulation. 

---

## The Frontend

Built via an ultra-lightweight, 1000-line monolithic HTML/CSS/JS file (`chat.html`), providing an app-like feeling directly within the browser.

### Adaptive UI Cards
The UI dynamically shifts shape based on the System Mode triggered:
- **Template Mode UI**: Standard chat bubbles returning instant Markdown-formatted tables and bullet lists.
- **Agent Mode UI Cards**: A distinct blue-bordered preview element explicitly requesting Review, featuring code blocks, warnings, and clear Approve/Reject CTA buttons.
- **Chain Mode UI Cards**: Green-bordered investigation modules displaying iterative multi-step tracking logic to the user.
- **Deep Dive UI Cards**: Purpose-built entity summaries with heavy, bolded headers indicating deep focus. 

The frontend heavily utilizes Markdown rendering (`marked.js`) and implements strong responsive design frameworks accommodating mobile devices with equal fidelity.

---

## Database Tables

OptiFlow interfaces with key tables from the SQL Server ecosystem:

| Table | Purpose |
|-------|---------|
| `ProSt` | Primary project tracking pipeline. |
| `CLIENT_MASTER` | The definitive ledger of Customer Company identities. |
| `INVOICE_DETAILS` | Invoice line item tracking. |
| `payment_information` | Payment receipts against specific invoices. |
| `AMC_MASTER` | Annual Maintenance Contract lifecycle tracking.|
| `OPERATIONS` | Operational and implementation project metrics. |
| `Monthly_Target` | Organizational Target vs Achieved statistics. |
| `TICKET_DETAILS` | Support and task ticketing endpoints. |

---

## Environment Variables

Create exactly `.env` file within the system root:

```env
DB_SERVER=192.168.1.198
DB_NAME=Ezee_BizFlow_Original
DB_USER=your_db_username
DB_PASSWORD=your_db_password
ANTHROPIC_API_KEY=sk-ant-...
INTENT_PARSER_MODE=local
```

| Variable | Description |
|----------|-------------|
| `DB_SERVER` | SQL Server IP address. |
| `DB_NAME` | Target Database name. |
| `DB_USER` | Access Username. |
| `DB_PASSWORD` | Access Password. |
| `ANTHROPIC_API_KEY` | Required API Key permitting Agent SQL Generation. |
| `INTENT_PARSER_MODE` | `"local"` (Default, utilizing Ollama) or `"cloud"` (Claude). |

---

## How to Run

### Steps
```bash
# 1. Establish python environment
python -m venv .venv
source .venv/bin/activate

# 2. Dependency resolution
pip install -r requirements.txt

# 3. Environment Population
# Ensure .env contains relevant credentials

# 4. Local Model Initialization (If utilizing local mode)
ollama pull qwen2.5-coder:3b

# 5. Ignite Server
uvicorn app:app --port 8000
```
> **macOS Note**: Under no circumstances utilize port 5000 as it conflicts critically with AirPlay Receiver infrastructure, resulting in silent HTTP 403 errors.

---

## Testing

OptiFlow is armed with 7 exhaustive testing batteries ensuring reliability:

| File | What It Tests |
|------|--------------|
| `test_db.py` | Connection resilience and throughput. |
| `test_filters.py` | Validates that injection filters properly append to strings. |
| `test_pipeline.py` | Confirms the fidelity of routing and parameter binding logic. |
| `test_queries.py` | Validates live query execution formatting. |
| `test_agent_sql.py` | Unleashes 10 novel questions to challenge Claude SQL formulation stability. |
| `test_intents.py` | Asserts the structural integrity of Template definitions. |
| `ground_truth.json` | Reference baselines. |

---

## How to Add a New Intent

Intent additions do not require touching the core engine, illustrating the platform's modularity.

1. Create the SQL Template structure inside the appropriate `intents/` file, declaring its parameters and caveats.
2. The `intents/__init__.py` aggregation system automatically imports it. 
3. Define the intent logic briefly within `prompts/system_prompt.txt` so the AI Parser recognizes it. 
4. (Optional) Provide a customized UI presentation block within `core/response_formatter.py`. 

---

## Known Data Quirks
It's imperative to recognize inherent systemic data flaws:
- Projects lack true delivery dates — "Age" acts as a proxy.
- Operations `PDD` fields are fundamentally corrupted with phantom zeroes. 
- Due to naming variants, querying "Hyundai" returns `Autoever`, `Glovis`, `Mobis`, etc., independently. Use specific strings when targeting individual plants. 
