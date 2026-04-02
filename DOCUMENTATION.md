# OptiFlow AI — Complete Technical Documentation

> A self-learning, universal natural language interface for Relational Databases. Ask questions in plain English, get structured business insights powered by Claude AI, local Ollama models, and SQL Server. OptiFlow adapts to any schema and gets smarter over time by learning from how you use it.

---

## Table of Contents

1. [What Is OptiFlow AI?](#what-is-optiflow-ai)
2. [The Setup Wizard & Knowledge Base](#the-setup-wizard--knowledge-base)
3. [Architecture & The Smart Learning Loop](#architecture--the-smart-learning-loop)
4. [The Universal Query Engine](#the-universal-query-engine)
5. [How a Question Flows Through the System](#how-a-question-flows-through-the-system)
6. [Project Structure](#project-structure)
7. [Module Deep-Dives](#module-deep-dives)
8. [Security & Safety](#security--safety)
9. [The Frontend](#the-frontend)
10. [Environment Variables](#environment-variables)
11. [How to Run](#how-to-run)

---

## What Is OptiFlow AI?

OptiFlow AI has evolved from a hardcoded internal tool into a **universal, schema-driven intelligent agent**. Rather than relying on static SQL templates tied to specific tables, managers interact with their data dynamically by typing questions. OptiFlow reads the live database schema, consults the company's business rules, and authors safe, read-only SQL on the fly.

Most importantly, OptiFlow is a **learning product**. As users ask novel questions, the AI generates custom SQL, human supervisors approve it, and the system caches and reuses it. **Each company's OptiFlow gets smarter over time through their own usage**, organically building a tailored library of highly accurate queries without ever requiring a developer to write new code.

**Key capabilities:**
- **Universal Schema Support** — Point it at any SQL Server database, and it will learn the tables automatically.
- **The Smart Learning Loop** — Auto-generated SQL → Human Approved → Cached → Reused.
- **Company Knowledge Base (`company.md`)** — A dynamic markdown file where non-technical admins can write business rules that the AI rigorously follows.
- **Dynamic Business Health** — Auto-generates multi-step executive summaries based on whatever the schema tracks.
- **Entity Deep Dives** — Generates complete 360° investigations into specific clients, projects, or entities across multiple tables.
- **Query Chaining** — Multi-step investigations with up to 3 sequential SQL queries for complex analytics.
- **Local LLM Support** — Switchable intent parsing between Claude API and local Ollama models for privacy and cost savings.

---

## The Setup Wizard & Knowledge Base

OptiFlow features a seamless onboarding experience designed to adapt to a new company's specific data environment instantly. 

During the first run (`GET /`), if the system detects it has not been configured, it routes the user to the Setup Wizard (`templates/setup.html`), handled by `core/setup_manager.py`.

### Setup Flow:
1. **Admin Creation**: Create the root administrator account.
2. **AI Provider Configuration**: Select between Claude (Cloud) or Ollama (Local) and validate API keys.
3. **Database Connection**: Input SQL Server credentials. Crucially, the system requires a `Read-Only` user.
4. **Schema Discovery**: OptiFlow interrogates the SQL Server, extracting table definitions, column names, and foreign key relationships, saving them to `prompts/schema_context.txt`.
5. **Company Knowledge Base**: The admin drafts a plain English summary of the business operations, jargon, and mandatory data filters (saved to `config/company.md`). 

**The Power of `company.md`:**
This is OptiFlow's business logic layer. Instead of hardcoding SQL `WHERE` clauses (e.g., filtering out dummy data or specific status codes), administrators simply type rules into `company.md` (e.g., *"Always exclude projects where Created_Date = '2025-04-21' as they are test records"*). The Agent SQL Generator injects this knowledge into every LLM prompt, forcing the AI to apply these filters dynamically.

---

## Architecture & The Smart Learning Loop

OptiFlow AI operates on a modern, multi-path architecture designed to balance speed, cost, and safety. 

**1. The Client Layer (Frontend):**
The browser sends the natural language question to the FastAPI backend. It also handles the interactive review UI where users explicitly approve or reject AI-generated SQL before it executes.

**2. The Understanding Layer (Intent Parsing):**
The system uses a highly efficient local LLM (Ollama) or a cloud LLM (Claude) simply to categorize the user's string into 3 core buckets: `business_health`, `deep_dive`, or `agent`. 

**3. The Universal Generation Engine (`agent_sql_generator.py`):**
Regardless of the category, OptiFlow sends the raw question, the `schema_context.txt`, and the `company.md` file to Claude. Claude authors one or multiple robust, read-only SQL queries designed specifically to answer the prompt.

**4. The Execution & Safety validation:**
The generated SQL is presented to the user. Execution is blocked until a human clicks "Approve."

**5. The Product Learning Pattern (The Cache Layer):**
When an Agent-generated query is approved and successfully executes, it enters the **Smart Learning Loop**. The query, alongside its original natural language question, is written to an persistent log (`logs/approved_queries.jsonl`) managed by `core/approved_queries.py`.

**The Product Pattern in Action:**
Auto-generated SQL → Human Approved → Cached → Reused.

The next time a user asks that exact same question, OptiFlow intercepts it before making an expensive LLM call. It instantly serves the known-good, human-verified SQL from its cache. In this way, OptiFlow transitions from a blank-slate AI into a highly specialized corporate asset.

---

## The Universal Query Engine

Because OptiFlow no longer utilizes hardcoded templates, the core intent pipeline (`intents/__init__.py`) has been reduced to three dynamic routing paths.

### 1. General "Agent" Routing
For specific questions like *"Show me pending invoices for Hyundai."*
Claude determines if the question requires a **Single Query** or a **Chain** (Multi-step). It generates the SQL, awaits approval, executes it, and then Claude interprets the raw data rows into a business-readable paragraph.

### 2. Business Health Summaries
Triggered by *"Give me a daily digest"* or *"How's the business?"*.
Instead of hardcoded queries, the `generate_business_health_chain` function asks Claude to look at the schema, determine the 3-5 most critical tables representing the company's core operations, and generate an aggregate summary query for each. Claude then synthesizes the results into a unified Executive Dashboard response.

### 3. Deep Dive Investigations
Triggered by *"Tell me everything about project P-2024-001"* or *"Deep dive into client Acme."*
The `generate_deep_dive_chain` function asks Claude to map every table in the schema connected to the requested entity. It generates a 4-to-5 step structural chain of SQL queries that extracts the entity's core record, related financials, historical operations, and pending tickets, presenting a total 360-degree view.

---

## How a Question Flows Through the System

### A Novel Question: "Compare Hyundai vs Inalfa across all tables"

1. The user asks the novel question.
2. The **Intent Parser** categorizes this as a general `agent` request.
3. **Cache Check**: `app.py` checks `core/query_cache.py` and the `logs/approved_queries.jsonl` persistent log. Finding no match, it routes to `agent_sql_generator.generate_chain()`.
4. Claude reads the schema and company knowledge, acknowledges the complexity, and outputs a multi-step **Chain** of SQL queries.
5. The SQL is sent back to the browser as a **Preview Card**, waiting for human approval.
6. The user clicks **Approve & Run**.
7. OptiFlow connects to SQL Server via `core/db.py` and executes the steps sequentially.
8. **The Learning Step**: The approved SQL and the original question are securely saved to the persistent log and cached in memory.
9. Claude interprets the numerical results into a plain-English summary, which is sent back to the user.

### Reusing a Learned Query

1. A week later, a manager asks: *"Compare Hyundai vs Inalfa across all tables"*.
2. The system checks the cache/log via `approved_queries.find_similar()` and finds the exact match that was previously verified by a human.
3. The expensive LLM generation step is bypassed entirely. The system instantly loads the proven SQL.
4. Upon approval, it executes instantly, proving the product's ability to self-optimize and learn.

---

## Project Structure

```
optiflow-ai/
├── app.py                        # FastAPI server — routes, auth, orchestration
├── requirements.txt              # Python dependencies
├── .env                          # Static Environment Variables
│
├── config/
│   ├── business_context.json     # Saved dynamic business settings
│   ├── company.md                # The malleable human-written business rulebook
│   ├── loader.py                 # Configuration serialization logic
│   ├── model_config.json         # AI API keys and model preferences
│   └── settings.py               # Loads .env static variables
│
├── core/                         # Business logic
│   ├── agent_sql_generator.py    # The Brain: Schema-driven SQL generation
│   ├── approved_queries.py       # Manages the persistent approved queries log
│   ├── audit_logger.py           # Tracks all user actions & generated SQL
│   ├── auth.py                   # User authentication & session management
│   ├── db.py                     # Database connection & execution
│   ├── intent_parser.py          # Cloud LLM semantic router
│   ├── local_intent_parser.py    # Local Ollama semantic router
│   ├── query_cache.py            # In-memory performance cache
│   └── setup_manager.py          # Orchestrates the initial onboarding wizard
│
├── intents/                      
│   └── __init__.py               # Core categorical intent definitions
│
├── logs/                         # Persistent system logs and JSONL files
│
├── prompts/
│   └── schema_context.txt        # The actively discovered database schema
│
└── templates/                    # HTML UI
    ├── chat.html                 # Main interface
    ├── company_editor.html       # Admin UI for modifying company.md
    ├── login.html                # Authentication gateway
    ├── settings.html             # System Configuration UI
    └── setup.html                # First-run Onboarding Wizard
```

---

## Module Deep-Dives

### core/agent_sql_generator.py

This is the brain of the Universal Engine. It never executes SQL; it only reads context and drafts code.
It contains functions for:
- `generate_sql(question)`: Generates a single SQL target.
- `generate_chain(question)`: Determines if a prompt requires 1 step or multiple sequential steps.
- `generate_business_health_chain()`: Analyzes the schema and drafts 3-5 holistic KPI queries.
- `generate_deep_dive_chain(entity)`: Drafts up to 5 inter-related queries completely outlining an entity.

The module aggressively enforces system prompts explicitly banning `INSERT`, `UPDATE`, `DELETE`, `DROP`, and demanding `LEFT JOINs` and formatting rules.

### core/setup_manager.py

Orchestrates the blank-slate initialization of the app. It connects to the SQL DB, extracts metadata, tables, constraints, and relationships using raw ADO.NET/ODBC introspection, and formats a dense, tokens-efficient representation into `schema_context.txt` so Claude understands exactly how the company's data is structured.

### app.py

Acts as the FastAPI traffic controller. It dictates strict execution timeouts (Pipeline 30s, Agent execution 30s, Chain execution 90s), handles asynchronous UI requests via SSE or JSON, and acts as the gatekeeper applying the Human-In-The-Loop approval requirement before calling `core.db.execute_query()`.

---

## Security & Safety

OptiFlow is designed for zero-trust enterprise deployment:

1. **Read-Only Verification**: During startup (`@app.on_event("startup")`), OptiFlow runs an aggressive permission check (`verify_readonly_access`). If the SQL Server user possesses `sysadmin`, `db_owner`, or `db_datawriter` credentials, OptiFlow will log a Critical Alert and **refuse to start**.
2. **LLM Restraints**: All generation prompts explicitly ban Data Manipulation Language (DML).
3. **Execution Wall**: No generated SQL can touch the database without explicit human UI approval (`POST /approve`).
4. **Audit Logging**: `core/audit_logger.py` tracks every user login, every generated query, and every approved SQL string.

---

## The Frontend

Built primarily via lightweight HTML/JS within `templates/chat.html`.

### Adaptive UI Cards
The UI dynamically shifts shape based on the Agent mode:
- **Agent Preview Cards**: Distinct bordered elements featuring raw SQL code blocks, data warnings sourced directly from `company.md` rules, and clear **Approve** / **Reject** buttons.
- **Chain Investigation Cards**: Multi-step visual tracking logic displaying the progression of an investigation.
- **Deep Dive UI**: Emphasized presentation focusing on comprehensive entity visibility.

---

## Environment Variables

The system requires a minimal `.env` file at the root. AI Keys and DB credentials are now handled dynamically via the Setup Wizard.

```env
# Optional fallback configurations
PORT=8000
SECRET_KEY=generate_a_secure_random_string_here
```

---

## How to Run

### Steps
```bash
# 1. Establish python environment
python -m venv .venv
source .venv/bin/activate

# 2. Dependency resolution
pip install -r requirements.txt

# 3. Local Model Initialization (If utilizing local parsing)
ollama pull qwen2.5-coder:3b

# 4. Ignite Server
uvicorn app:app --port 8000
```

Once running, navigate to `http://localhost:8000/`. If it is the first launch, the Setup Wizard will automatically initialize to connect your database and AI API keys.


./start.sh