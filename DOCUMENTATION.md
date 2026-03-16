# OptiFlow AI — Complete Technical Documentation

> A natural language interface for querying BizFlow ERP data. Ask questions in plain English, get structured business insights powered by Claude AI and SQL Server.

---

## Table of Contents

1. [What Is OptiFlow AI?](#what-is-optiflow-ai)
2. [Architecture Overview](#architecture-overview)
3. [How a Question Flows Through the System](#how-a-question-flows-through-the-system)
4. [Project Structure](#project-structure)
5. [Module Deep-Dives](#module-deep-dives)
   - [config/settings.py](#configsettingspy)
   - [app.py — The Server](#apppy--the-server)
   - [core/intent_parser.py — Understanding Questions](#coreintent_parserpy--understanding-questions)
   - [core/query_engine.py — Running Queries](#corequery_enginepy--running-queries)
   - [core/db.py — Database Connection](#coredbpy--database-connection)
   - [core/filter_injector.py — Safety Net](#corefilter_injectorpy--safety-net)
   - [core/response_formatter.py — Human-Readable Answers](#coreresponse_formatterpy--human-readable-answers)
6. [The Intent System](#the-intent-system)
   - [How Intents Work](#how-intents-work)
   - [Intent Registry](#intent-registry)
   - [All Available Intents](#all-available-intents)
   - [Meta Intents](#meta-intents)
   - [Retired Intents](#retired-intents)
7. [The System Prompt](#the-system-prompt)
8. [The Frontend](#the-frontend)
9. [Database Tables](#database-tables)
10. [Environment Variables](#environment-variables)
11. [How to Run](#how-to-run)
12. [How to Add a New Intent](#how-to-add-a-new-intent)
13. [Known Data Quirks](#known-data-quirks)

---

## What Is OptiFlow AI?

OptiFlow AI is a chat-based dashboard for **Ecosoft Zolutions' BizFlow ERP system**. Instead of clicking through reports, managers type questions like:

- *"How many projects are in the pipeline?"*
- *"Show me pending invoices"*
- *"Which AMC contracts are expiring?"*
- *"Open tickets?"*

The system understands the question, queries the SQL Server database, and returns a formatted, actionable answer — with insights, alerts, and data.

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────┐
│                       BROWSER (chat.html)                    │
│  User types a question → POST /ask → Gets JSON answer       │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                   app.py (FastAPI Server)                     │
│                                                              │
│  GET /         → Serves chat.html                            │
│  GET /welcome  → Runs 4 health checks, returns greeting      │
│  POST /ask     → Runs the full pipeline (below)              │
└──────────────────────┬───────────────────────────────────────┘
                       │
              ┌────────┴────────┐
              ▼                 │
┌─────────────────────┐        │
│  1. INTENT PARSER   │        │  The Pipeline
│  (Claude AI API)    │        │
│                     │        │  question → intent → SQL → rows → answer
│  "Pending invoices?"│        │
│  → {intent:         │        │
│     "invoices_      │        │
│      pending"}      │        │
└─────────┬───────────┘        │
          ▼                    │
┌─────────────────────┐        │
│  2. QUERY ENGINE    │        │
│                     │        │
│  Looks up intent    │        │
│  in INTENT_REGISTRY │        │
│  Binds parameters   │        │
│  Injects filters    │        │
│  Executes SQL       │        │
└─────────┬───────────┘        │
          ▼                    │
┌─────────────────────┐        │
│  3. DATABASE (db.py)│        │
│                     │        │
│  SQL Server via     │        │
│  pyodbc + ODBC 18   │        │
│  Returns rows as    │        │
│  list[dict]         │        │
└─────────┬───────────┘        │
          ▼                    │
┌─────────────────────┐        │
│  4. FORMATTER       │◄───────┘
│                     │
│  Turns raw rows     │
│  into human text    │
│  with insights +    │
│  alerts + data      │
└─────────────────────┘
```

**Key principle**: Each module does ONE job. The intent parser doesn't touch the database. The query engine doesn't format text. The formatter doesn't know about SQL. This makes each piece easy to understand, test, and change independently.

---

## How a Question Flows Through the System

Let's trace what happens when a user types **"Pending invoices?"**:

### Step 1: Frontend → Server
The browser sends `POST /ask` with body `{"question": "Pending invoices?"}`.

### Step 2: Intent Parsing (intent_parser.py)
The server calls `parse("Pending invoices?")`. This sends the question to **Claude AI** along with a system prompt listing all available intents. Claude returns:
```json
{"intent": "invoices_pending"}
```

### Step 3: Query Lookup (query_engine.py)
The engine looks up `"invoices_pending"` in the `INTENT_REGISTRY` dictionary. It finds:
- **SQL template**: A pre-written query that calculates total outstanding, invoiced, and unbilled amounts
- **Table**: `INVOICE_DETAILS`
- **Params**: None (this intent needs no parameters)
- **Caveats**: Notes about data quirks

### Step 4: Parameter Binding
If the SQL has `[PLACEHOLDER]` tokens (e.g., `[CUSTOMER_NAME]`), they get replaced with `?` markers and actual values from the parsed intent. This prevents SQL injection.

### Step 5: Filter Injection (filter_injector.py)
The `inject_filters()` function checks if mandatory WHERE clauses are present (e.g., excluding test data from `ProSt`). If any are missing, they're automatically added.

### Step 6: Database Execution (db.py)
The SQL runs against SQL Server via `pyodbc`. Results come back as a list of dictionaries:
```python
[{"TotalInvoices": 45, "InvoicedPending": 1500000, "UnbilledPending": 300000, "TotalOutstanding": 1800000}]
```

### Step 7: Formatting (response_formatter.py)
The `_fmt_invoices_pending()` function takes those rows and creates:
```
Total outstanding: Rs 18.00L. Of this, Rs 15.00L is formally
invoiced and Rs 3.00L is work completed but not yet billed.

Rs 3.00L in completed work hasn't been invoiced yet
— this is revenue sitting on the table.

- Invoiced (raised but unpaid): Rs 15.00L across 45 invoices
- Unbilled (work done, not yet invoiced): Rs 3.00L
- Total outstanding: Rs 18.00L
```

### Step 8: Response
The server returns JSON to the browser:
```json
{
  "answer": "Total outstanding: Rs 18.00L...",
  "intent": "invoices_pending",
  "time_ms": 1976
}
```

---

## Project Structure

```
optiflow-ai/
├── app.py                      # FastAPI server — routes + pipeline orchestration
├── requirements.txt            # Python dependencies
├── .env                        # Secrets (DB credentials, API key)
├── .gitignore                  # Git exclusions
│
├── config/
│   ├── __init__.py
│   └── settings.py             # Loads .env variables into Python constants
│
├── core/                       # Business logic (the engine)
│   ├── __init__.py
│   ├── intent_parser.py        # Calls Claude API to extract intent from text
│   ├── query_engine.py         # Maps intents → SQL → database execution
│   ├── db.py                   # Database connection + query execution
│   ├── filter_injector.py      # Injects mandatory WHERE clauses
│   └── response_formatter.py   # Turns raw DB rows into human-readable text
│
├── intents/                    # Intent definitions (SQL templates + metadata)
│   ├── __init__.py             # Merges all intents into INTENT_REGISTRY
│   ├── project_intents.py      # 7 intents for ProSt table
│   ├── finance_intents.py      # 4 intents for invoices + payments
│   ├── amc_intents.py          # 4 intents for AMC contracts
│   ├── ops_intents.py          # 4 intents for operations
│   └── target_intents.py       # 3 intents for targets + tickets
│
├── prompts/
│   └── system_prompt.txt       # Claude system prompt listing all intents
│
├── templates/
│   └── chat.html               # Frontend UI (HTML + CSS + JS, all inline)
│
└── tests/
    ├── test_db.py              # Unit tests for database module
    └── test_filters.py         # Unit tests for filter injector
```

---

## Module Deep-Dives

### config/settings.py

**Purpose**: Load secrets from the `.env` file so no credentials are hardcoded.

```python
DB_SERVER = os.getenv("DB_SERVER")      # e.g., 192.168.1.198
DB_NAME = os.getenv("DB_NAME")          # e.g., Ezee_BizFlow_Original
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
```

Uses `python-dotenv` to read from `.env` at import time.

---

### app.py — The Server

**Purpose**: FastAPI application with 3 routes.

| Route | Method | What It Does |
|-------|--------|-------------|
| `/` | GET | Serves `templates/chat.html` |
| `/welcome` | GET | Runs 4 parallel health checks, returns a greeting message |
| `/ask` | POST | Accepts `{"question": "..."}`, runs the full pipeline |

**Key features**:
- **Timeouts**: Pipeline has a 30-second timeout. Welcome has 15 seconds.
- **Async execution**: Uses `asyncio.wait_for()` + `run_in_executor()` so the synchronous DB calls don't block the async event loop.
- **Welcome message**: Runs 4 intents in parallel using `ThreadPoolExecutor` — `invoice_aging`, `amc_expiry`, `projects_stuck`, `tickets_open` — and combines them into an executive summary.

**The pipeline** (`_run_pipeline`):
1. Parse intent via Claude
2. Handle parse errors
3. Run query via query engine
4. Handle fallback (unknown intent)
5. Format response
6. Return with timing info

---

### core/intent_parser.py — Understanding Questions

**Purpose**: Takes a plain English question and returns a structured intent dict.

**How it works**:
1. Loads `prompts/system_prompt.txt` as the Claude system prompt (once at startup)
2. Sends the user's question to Claude (`claude-sonnet-4-20250514`)
3. Claude returns JSON like `{"intent": "amc_expiry", "days": 60}`
4. Strips any markdown code fences Claude might add
5. Parses the JSON and returns it

**Error handling**:
- Empty question → `{"intent": "unknown", "error": "empty_question"}`
- API failure → `{"intent": "unknown", "error": "api_failed"}`
- Bad JSON from Claude → `{"intent": "unknown", "error": "parse_failed"}`

**Settings**: `temperature=0` (deterministic), `max_tokens=200` (just need a short JSON).

---

### core/query_engine.py — Running Queries

**Purpose**: The central router. Takes a parsed intent, finds its SQL template, binds parameters, injects safety filters, executes the query, and returns raw results.

**Key components**:

#### `run(intent_dict)` — Main function
1. Looks up intent in `INTENT_REGISTRY`
2. Handles meta-intents (runs sub-intents)
3. Handles retired intents (follows redirects)
4. Binds parameters into SQL
5. Injects mandatory filters
6. Executes query via `db.py`
7. Returns result dict

#### `_bind_params(sql, intent_dict, defaults)` — Parameter binding
Replaces `[PLACEHOLDER]` tokens in SQL with `?` markers:
- Normal: `WHERE id = [ID]` → `WHERE id = ?` with value `123`
- LIKE pattern: `WHERE name LIKE '%[NAME]%'` → `WHERE name LIKE ?` with value `%Hyundai%`
- Prevents SQL injection by using parameterized queries

#### `_run_meta(intent_name, definition)` — Meta-intent execution
For intents like `business_health`, runs multiple sub-intents and bundles results.

#### `_build_fallback()` — Unknown intent handling
Returns a friendly "I don't understand" message with suggested questions.

---

### core/db.py — Database Connection

**Purpose**: Connects to SQL Server and executes queries.

**Connection string**:
```
DRIVER={ODBC Driver 18 for SQL Server}
SERVER=192.168.1.198,1433
DATABASE=Ezee_BizFlow_Original
TrustServerCertificate=yes
Encrypt=optional
```

#### `get_connection()` — With retry logic
- Tries up to **3 times** with a **2-second delay** between retries
- Handles transient connection errors gracefully
- Connection timeout: 10 seconds

#### `execute_query(sql, params=None)` — Execute + return dicts
- Opens connection
- Executes SQL with optional parameters
- Reads column names from `cursor.description`
- Zips column names with row values → list of dicts
- Always closes connection in `finally` block

**Example**:
```python
# Input
execute_query("SELECT Project_Code, PIC FROM ProSt WHERE PIC = ?", ("John",))

# Output
[
    {"Project_Code": "PRJ001", "PIC": "John"},
    {"Project_Code": "PRJ015", "PIC": "John"},
]
```

---

### core/filter_injector.py — Safety Net

**Purpose**: Ensures mandatory data quality filters are present in every query, even if the SQL template accidentally omits them.

**Mandatory filters by table**:

| Table | Filter | Why |
|-------|--------|-----|
| `ProSt` | `Created_Date != '2025-04-21'` | Excludes test/migration data created on a specific bulk import date |
| `ProSt` | `PIC NOT IN ('XXX','NONE','66','25','64')` | Excludes junk PIC values that aren't real people |
| `ProSt` | `PIC IS NOT NULL` | Excludes projects with no assigned contact |
| `AMC_MASTER` | `Status IS NOT NULL` | Excludes 5 records with blank status |
| `AMC_MASTER` | `Status != ''` | Same as above, catches empty strings |

**How injection works**:
1. Check if each filter pattern already exists in the SQL
2. Find the injection point (before `GROUP BY`, `ORDER BY`, or end of query)
3. Append any missing filters as `AND` clauses

**Design**: No flags to disable. No bypass. It's a mandatory safety net.

---

### core/response_formatter.py — Human-Readable Answers

**Purpose**: Transforms raw database rows into natural English summaries that managers can actually read.

**Response structure** (consistent across all formatters):
1. **Insight lead** — What does the data say? (e.g., "89 active projects in pipeline")
2. **Alert** — Why should the manager care? (e.g., "Pipeline is top-heavy")
3. **Data listing** — The actual numbers, formatted as a list
4. **Caveats** — Data quality notes appended at the end

**Special features**:
- **Indian currency formatting** (`_fmt_currency`): Rs 5,586 / Rs 17.48L / Rs 2.40cr
- **Max 10 rows** displayed, with "...and N more" for overflow
- **20 dedicated formatters** — one per intent, each with domain-specific insights
- **Generic fallback** (`_fmt_generic`) for any intent without a custom formatter

**Key formatters**:

| Formatter | Intent | Special Logic |
|-----------|--------|--------------|
| `_fmt_projects_by_stage` | `projects_by_stage` | Detects top-heavy pipeline (Seed > Root + Ground) |
| `_fmt_invoice_aging` | `invoice_aging` | Highlights 90+ day bucket as urgent |
| `_fmt_amc_expiry` | `amc_expiry` | Detects renewal waves (all same date) |
| `_fmt_monthly_target` | `monthly_target` | Time-aware alerts (e.g., "<10 days left, only 40% achieved") |
| `format_business_health` | `business_health` | Combines 6 sub-intents into executive digest with action items |
| `format_welcome` | Welcome message | Time-of-day greeting + 4 health check alerts |

---

## The Intent System

### How Intents Work

An **intent** is a predefined query template. Each intent has:

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

All intents are merged into one dictionary in `intents/__init__.py`:

```python
INTENT_REGISTRY = {
    **PROJECT_INTENTS,    # 7 intents
    **FINANCE_INTENTS,    # 4 intents
    **AMC_INTENTS,        # 4 intents
    **OPS_INTENTS,        # 4 intents
    **TARGET_INTENTS,     # 3 intents (includes tickets)
    **META_INTENTS,       # 1 meta-intent (business_health)
}
```

Total: **23 intents** (19 active + 2 retired + 1 meta + 1 variant).

### All Available Intents

#### Project Intents (table: `ProSt`)

| Intent | Description | Has Parameters? |
|--------|-------------|-----------------|
| `projects_by_age` | Longest-running active projects | No |
| `projects_by_stage` | Pipeline stage counts (Seed/Root/Ground/Plant...) | No |
| `projects_stuck` | Projects with no movement in 30+ days | No |
| `projects_by_pic` | Workload by customer-side project manager | No |
| `projects_by_customer` | Project count by company name | No |
| `projects_lifecycle` | Average time from Seed to Plant completion | No |
| `projects_overdue` | **RETIRED** → redirects to `projects_by_age` | No |

#### Finance Intents (tables: `INVOICE_DETAILS`, `payment_information`)

| Intent | Description | Has Parameters? |
|--------|-------------|-----------------|
| `invoices_pending` | Total outstanding (invoiced + unbilled) | No |
| `invoices_this_month` | Invoices raised this month | No |
| `invoice_aging` | Age buckets (0-30, 31-60, 61-90, 90+ days) | No |
| `payment_summary` | Payments received this month | No |

#### AMC Intents (table: `AMC_MASTER`)

| Intent | Description | Has Parameters? |
|--------|-------------|-----------------|
| `amc_expiry` | Contracts expiring in 60 days | No |
| `amc_status_summary` | Breakdown by status with counts | No |
| `amc_by_customer` | Contracts for a specific customer | Yes: `CUSTOMER_NAME` |
| `amc_revenue` | Revenue breakdown by status | No |

#### Operations Intents (table: `OPERATIONS`)

| Intent | Description | Has Parameters? |
|--------|-------------|-----------------|
| `ops_status` | Count by status | No |
| `ops_active` | Active (non-COC) projects by age | No |
| `ops_by_customer` | Projects for a specific customer | Yes: `CUSTOMER_CODE`, `CUSTOMER_NAME` |
| `ops_overdue` | **RETIRED** → redirects to `ops_active` | No |

#### Target & Ticket Intents (tables: `Monthly_Target`, `TICKET_DETAILS`)

| Intent | Description | Has Parameters? |
|--------|-------------|-----------------|
| `monthly_target` | Target vs achieved for all departments | No |
| `tickets_open` | All open/unresolved tickets | No |
| `tickets_by_person` | Tickets by assignee (summary or detail) | Yes: `PERSON_NAME` |

### Meta Intents

The `business_health` intent is special — it's a **meta-intent** that runs 6 sub-intents behind the scenes:
1. `projects_by_stage` — Pipeline health
2. `projects_stuck` — Stuck projects
3. `invoices_pending` — Cash flow
4. `invoice_aging` — Overdue invoices
5. `amc_status_summary` — Recurring revenue
6. `monthly_target` — Target progress

It combines all results into a single executive summary with **action items**.

**Triggers**: "How's the business?", "Give me a summary", "Daily digest", "Business overview", "Dashboard".

### Retired Intents

Two intents are retired due to bad data:
- `projects_overdue` → No active projects have delivery dates set. Redirects to `projects_by_age`.
- `ops_overdue` → PDD column is broken (55 NULL + 9 fake dates out of 73 records). Redirects to `ops_active`.

When a retired intent is requested, the system follows the redirect and prepends an explanation to the response.

---

## The System Prompt

Located at `prompts/system_prompt.txt`, this is what Claude sees on every question. It:

1. Lists all 19 active intents with their parameters
2. Lists retired intents with redirect instructions
3. Defines 7 rules (JSON-only output, unknown handling, date parsing, etc.)
4. Provides table-specific notes (e.g., PIC = customer-side, not internal)

Claude's job is strictly **intent extraction** — it returns JSON, never SQL, never explanations.

---

## The Frontend

`templates/chat.html` is a single self-contained HTML file with inline CSS and JavaScript.

**Features**:
- Dark theme header (`#1a1a2e`) with light chat bubbles
- Quick-start chips for common questions (hidden after first use)
- Auto-fetches a welcome message on page load via `GET /welcome`
- Loading animation (bouncing dots) while waiting for responses
- Intent name and response time shown as metadata under each AI message
- Responsive design for mobile screens

**JavaScript flow**:
1. Page loads → `loadWelcome()` calls `/welcome`
2. User types question (or clicks chip) → `sendQuestion()` calls `POST /ask`
3. Response displayed as a new chat bubble with metadata

---

## Database Tables

OptiFlow queries these SQL Server tables:

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `ProSt` | Projects (pipeline) | `Project_Code`, `Project_Title`, `PIC`, `Project_Status`, `Customer`, `Created_Date`, `Plant_Date` |
| `CLIENT_MASTER` | Customer company names | `client_Code`, `client_Name` |
| `INVOICE_DETAILS` | Invoice line items | `Invoice_No`, `Grand_Total`, `Line_Status`, `Invoice_CreatedAt` |
| `payment_information` | Payment receipts | `Invoice_No`, `amount`, `TDS_Deduction`, `amount_received_date` |
| `AMC_MASTER` | AMC contracts | `AmcID`, `CustomerName`, `ProjectTitle`, `Status`, `AMCEndDate`, `TotalAmount`, `AMC_Amount` |
| `OPERATIONS` | Operations projects | `Project_Code`, `Customer_Code`, `Project_Title`, `Status`, `PSD`, `Created_At` |
| `Monthly_Target` | Targets vs achieved | `Department`, `TargetAmount`, `AchievedAmount`, `BacklogAmount`, `CurrentMonth` |
| `TICKET_DETAILS` | Support tickets | `Ticket_ID`, `Assigned_To`, `Task_Title`, `Priority`, `Ticket_Status`, `Resolved` |

---

## Environment Variables

Create a `.env` file in the project root:

```env
DB_SERVER=192.168.1.198
DB_NAME=Ezee_BizFlow_Original
DB_USER=your_db_username
DB_PASSWORD=your_db_password
ANTHROPIC_API_KEY=sk-ant-...
```

---

## How to Run

### Prerequisites
- Python 3.12+
- SQL Server accessible on the network
- ODBC Driver 18 for SQL Server installed
- Anthropic API key

### Steps

```bash
# 1. Create virtual environment
python -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set up .env file (see above)

# 4. Run the server
uvicorn app:app --port 8000

# 5. Open browser
# http://127.0.0.1:8000
```

### macOS ODBC Driver Installation
```bash
brew install unixodbc
brew tap microsoft/mssql-release https://github.com/microsoft/homebrew-mssql-release
brew install msodbcsql18
```

---

## How to Add a New Intent

Follow these 4 steps:

### 1. Write the SQL and add an intent definition

Create or edit a file in `intents/`. Example:

```python
# In intents/finance_intents.py
"top_debtors": {
    "name": "top_debtors",
    "description": "Returns customers with highest outstanding amounts.",
    "table": "INVOICE_DETAILS",
    "sql": (
        "SELECT Customer, SUM(Grand_Total) AS Outstanding "
        "FROM INVOICE_DETAILS "
        "WHERE Line_Status = 'Invoiced' "
        "GROUP BY Customer "
        "ORDER BY Outstanding DESC;"
    ),
    "params": {},
    "caveats": [
        "Only includes Line_Status = 'Invoiced', not unbilled Pending.",
    ],
    "retired": False,
    "redirect_to": None,
},
```

### 2. Register it in the INTENT_REGISTRY

If you added it to an existing file (e.g., `finance_intents.py`), it's already registered via the `**FINANCE_INTENTS` spread in `intents/__init__.py`. No changes needed.

### 3. Update the system prompt

Add the intent name and description to `prompts/system_prompt.txt` so Claude knows it exists:

```
- top_debtors: Customers with highest outstanding (no params)
```

### 4. Add a formatter (optional but recommended)

In `core/response_formatter.py`:

```python
def _fmt_top_debtors(rows, params):
    parts = [f"{len(rows)} customers with outstanding invoices."]
    def fmt_row(r):
        return f"{r.get('Customer', '?')} — {_fmt_currency(r.get('Outstanding'))}"
    parts.append(_list_rows(rows, fmt_row))
    return "\n\n".join(parts)
```

Then add it to `_FORMATTERS`:
```python
"top_debtors": _fmt_top_debtors,
```

If no formatter is registered, the generic fallback will display the data (but without smart insights).

---

## Known Data Quirks

These are documented in the intent caveats and are important to understand:

| Issue | Impact |
|-------|--------|
| No delivery dates on active projects | Cannot calculate "overdue" — use age as proxy |
| `PIC` values include junk (`XXX`, `NONE`, `66`, `25`, `64`) | Filtered out in queries and via `filter_injector` |
| `Created_Date = '2025-04-21'` bulk import | ~60 projects created during data migration — excluded |
| `EDOP` and `EWOP` columns are NULL | Never use these for date filtering — use `Invoice_CreatedAt` |
| `PDD` in OPERATIONS is mostly NULL/fake | 55 NULL + 9 fake `1900-01-01` values out of 73 records |
| `Monthly_Target` shows duplicated achievement amounts | Same `AchievedAmount` across all 6 departments — likely data entry error |
| Only 9 tickets in `TICKET_DETAILS` | Small dataset — table may be newly introduced |
| Some tickets have `Status='Resolved'` but `Resolved=0` | Query uses `OR` to catch both conditions |
| Multiple Hyundai entities | `Autoever`, `Glovis`, `Mobis`, `Motor`, etc. — same group, different plants |
| `Sales` appears as `Assigned_To` in tickets | Department name, not a person |
