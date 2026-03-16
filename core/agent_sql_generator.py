"""
Agent SQL Generator — takes a natural language question and generates SQL.

This is the brain of Agent Mode. It calls Claude with the full database schema
and strict SQL generation rules, returning structured output that includes the
SQL string, plain-English explanation, tables used, confidence level, and any
data quality warnings.

Also provides:
- generate_chain(question): multi-step investigation (up to 3 SQL steps)
- generate_deep_dive(entity_type, entity_id, entity_name, entity_code):
  pre-built SQL chains for project or customer deep dives — no LLM call.

IMPORTANT: This module NEVER executes SQL. It only generates it.
Execution happens after explicit human approval.
"""

import json
import logging
import os
import re
from datetime import date

import anthropic

from config.settings import ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

# Path to the schema context file (populated from db_context_summary.md)
_SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "prompts",
    "schema_context.txt",
)

# Regex to strip ```json ... ``` wrappers Claude sometimes adds.
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?\s*```$", re.DOTALL)

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def _load_schema() -> str:
    """Load schema context from file. Raises FileNotFoundError if missing."""
    if not os.path.exists(_SCHEMA_PATH):
        raise FileNotFoundError(
            f"Schema context file not found: {_SCHEMA_PATH}\n"
            "Populate prompts/schema_context.txt from db_context_summary.md before using Agent Mode."
        )
    with open(_SCHEMA_PATH, encoding="utf-8") as f:
        return f.read()


def _build_system_prompt(schema: str) -> str:
    today = date.today()
    current_date = today.strftime("%Y-%m-%d")
    current_year = today.year
    current_month = today.strftime("%B %Y")

    return f"""Today's date is {current_date}. When the user says 'this year' they mean {current_year}, 'this month' means {current_month}.

You are an expert SQL analyst for Ecosoft Zolutions' BizFlow ERP system (SQL Server at 192.168.1.198, database: Ezee_BizFlow_Original).

Your ONLY job: generate a safe, read-only SQL query that answers the user's question.

=== DATABASE SCHEMA ===
{schema}

=== DATA QUALITY RULES (MANDATORY — always apply these) ===

ProSt table:
- Always add WHERE Created_Date != '2025-04-21' to exclude 150+ migration batch records
- When PIC column is involved, always add: PIC NOT IN ('XXX','NONE','66','25','64') AND PIC IS NOT NULL

INVOICE_DETAILS table:
- Always use COUNT(DISTINCT Invoice_No), never COUNT(*)
- 'Under Review' status does NOT exist. Valid statuses: Pending, Invoiced, Payments Closed, FOC
- Use Invoice_CreatedAt for date filtering. EDOP and EWOP are NULL for all records — never use them.

AMC_MASTER table:
- Always filter Status IS NOT NULL AND Status != ''
- AMC_Amount = recurring revenue. TotalAmount = one-time project cost. Use AMC_Amount for revenue queries.
- 122 of 204 records have NULL AMCEndDate — warn the user when querying expiry dates.

OPERATIONS table:
- PDD column is broken (55 NULL + 9 fake 1900-01-01 dates). Never use PDD. Use Created_At for age calculations.

TICKET_DETAILS table:
- For open tickets use: WHERE Resolved = 0 OR Ticket_Status = 'In Progress'

Monthly_Target table:
- Feb 2026 has identical AchievedAmount across all departments (known data entry error — warn if querying that period).

=== BUSINESS TERMINOLOGY ===
- PIC = customer-side project contact, NOT an internal employee
- Project stages: Seed (lead) → Root (quoted) → Ground (confirmed) → Plant (completed)
- Active pipeline = Seed + Root + Ground only (Plant = completed, exclude from pipeline)
- COC = Certificate of Completion (project is done)
- Awaiting PO = verbally agreed AMC contract, PO not yet received (pipeline, not active revenue)
- Under AMC = active contract generating recurring revenue

=== TABLE RELATIONSHIPS ===
- ProSt.Customer → CLIENT_MASTER.client_Code
- ProSt.Project_Code → OPERATIONS.Project_Code
- INVOICE_DETAILS.Customer → CLIENT_MASTER.client_Code
- OPERATIONS.Customer_Code → CLIENT_MASTER.client_Code
- payment_information.Invoice_No → INVOICE_DETAILS.Invoice_No

=== SQL GENERATION RULES ===
1. SQL Server syntax only: use GETDATE(), DATEDIFF(), TOP, ISNULL(), CONVERT(), etc.
2. Always SELECT only. NEVER generate INSERT, UPDATE, DELETE, DROP, ALTER, EXEC, TRUNCATE, or any write/DDL operation.
3. Always apply the mandatory data quality filters listed above for any table you query.
4. Use LEFT JOIN when joining to CLIENT_MASTER (some codes have no matching client record).
5. Limit to TOP 100 rows unless the question explicitly asks for all records.
6. Use human-readable column aliases (e.g., AS "Customer Name", AS "Invoice Amount").
7. Always include ORDER BY for meaningful sorting (e.g., by date DESC, amount DESC).
8. Format currency columns as-is (no rounding).
9. For partial name matches use LIKE '%value%' (case-insensitive on SQL Server default collation).

=== OUTPUT FORMAT ===
Respond with ONLY a valid JSON object — no markdown, no explanation outside the JSON, nothing else.

If the question CAN be answered:
{{
  "sql": "SELECT TOP 100 ... FROM ... WHERE ... ORDER BY ...",
  "explanation": "This query finds ...",
  "tables_used": ["TableA", "TableB"],
  "confidence": "high",
  "warnings": ["Any data quality concerns the user should know about"]
}}

confidence values:
- "high"   = schema fully supports the question, all filters applied
- "medium" = minor ambiguity or partial data coverage
- "low"    = significant data gaps, assumptions made

If the question CANNOT be answered with this database:
{{
  "sql": null,
  "explanation": "This database doesn't contain ...",
  "tables_used": [],
  "confidence": "none",
  "warnings": []
}}
"""


def generate_sql(question: str) -> dict:
    """Generate SQL for a natural language question against the BizFlow schema.

    Args:
        question: The user's question in plain English.

    Returns:
        dict with keys:
            sql          (str | None)  — the generated SQL, or None if unanswerable
            explanation  (str)         — plain-English description of what the query does
            tables_used  (list[str])   — table names referenced in the query
            confidence   (str)         — "high" | "medium" | "low" | "none"
            warnings     (list[str])   — data quality concerns the user should know

        On API or parse failure, returns an error dict with the same keys but
        sql=None and confidence="none".
    """
    if not question or not question.strip():
        return {
            "sql": None,
            "explanation": "Empty question provided.",
            "tables_used": [],
            "confidence": "none",
            "warnings": [],
        }

    # Load schema (fails fast if schema_context.txt is missing)
    try:
        schema = _load_schema()
    except FileNotFoundError as e:
        logger.error(str(e))
        return {
            "sql": None,
            "explanation": str(e),
            "tables_used": [],
            "confidence": "none",
            "warnings": ["Schema context file is missing. Run schema export first."],
        }

    system_prompt = _build_system_prompt(schema)

    # Call Claude API
    try:
        response = _client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            temperature=0,
            system=system_prompt,
            messages=[{"role": "user", "content": question}],
        )
        text = response.content[0].text.strip()
    except Exception as e:
        logger.error(f"Claude API call failed: {e}")
        return {
            "sql": None,
            "explanation": f"Claude API call failed: {e}",
            "tables_used": [],
            "confidence": "none",
            "warnings": ["API error — please retry."],
        }

    # Strip markdown code fences if present
    fence_match = _CODE_FENCE_RE.match(text)
    if fence_match:
        text = fence_match.group(1).strip()

    # Parse JSON response
    try:
        result = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        logger.warning(f"Failed to parse Claude response as JSON: {text!r}")
        return {
            "sql": None,
            "explanation": "Failed to parse Claude response as JSON.",
            "tables_used": [],
            "confidence": "none",
            "warnings": [f"Raw response: {text[:200]}"],
        }

    if not isinstance(result, dict):
        logger.warning(f"Claude returned non-dict JSON: {result!r}")
        return {
            "sql": None,
            "explanation": "Unexpected response format from Claude.",
            "tables_used": [],
            "confidence": "none",
            "warnings": [],
        }

    # Normalise — ensure all expected keys are present
    return {
        "sql": result.get("sql"),
        "explanation": result.get("explanation", ""),
        "tables_used": result.get("tables_used", []),
        "confidence": result.get("confidence", "none"),
        "warnings": result.get("warnings", []),
    }


# ---------------------------------------------------------------------------
# Query Chaining
# ---------------------------------------------------------------------------

def _build_chain_system_prompt(schema: str) -> str:
    today = date.today()
    current_date = today.strftime("%Y-%m-%d")
    current_year = today.year
    current_month = today.strftime("%B %Y")

    return f"""Today's date is {current_date}. When the user says 'this year' they mean {current_year}, 'this month' means {current_month}.

You are an expert SQL analyst for Ecosoft Zolutions' BizFlow ERP system.

Your job: decide if a question needs a single SQL query or a CHAIN of up to 3 sequential queries, then generate them.

Use a chain when the question requires:
- Lookup then filter (e.g. "find customer code then get their invoices")
- Multi-step aggregation (e.g. "get stuck projects, then their invoice status")
- Cross-domain investigation (e.g. "projects with no AMC renewal AND overdue invoices")

Use a single query for everything else.

=== DATABASE SCHEMA ===
{schema}

=== DATA QUALITY RULES (always apply) ===
ProSt: WHERE Created_Date != '2025-04-21'. PIC NOT IN ('XXX','NONE','66','25','64') AND PIC IS NOT NULL when PIC is used.
INVOICE_DETAILS: COUNT(DISTINCT Invoice_No). Valid statuses: Pending, Invoiced, Payments Closed, FOC.
AMC_MASTER: Status IS NOT NULL AND Status != ''. Use AMC_Amount for revenue. 122 NULL AMCEndDate records.
OPERATIONS: Never use PDD column.
TICKET_DETAILS: Open = WHERE Resolved = 0 OR Ticket_Status = 'In Progress'

=== SQL RULES ===
- SQL Server syntax only. SELECT only — no writes or DDL.
- Apply all data quality filters for every table used.
- TOP 100 unless question asks for all.
- Use LEFT JOIN to CLIENT_MASTER.
- Human-readable column aliases. Always ORDER BY.

=== OUTPUT FORMAT ===
Return ONLY valid JSON, no markdown.

For a CHAIN:
{{
  "mode": "chain",
  "steps": [
    {{"step": 1, "sql": "SELECT ...", "explanation": "Finds all stuck projects", "tables": ["ProSt"]}},
    {{"step": 2, "sql": "SELECT ...", "explanation": "Gets invoice status for those projects", "tables": ["INVOICE_DETAILS"]}}
  ],
  "summary_prompt": "Summarise how stuck projects correlate with invoice collection problems",
  "confidence": "high",
  "warnings": []
}}

For a SINGLE query:
{{
  "mode": "single",
  "sql": "SELECT ...",
  "explanation": "...",
  "tables_used": ["TableA"],
  "confidence": "high",
  "warnings": []
}}

Note: In a chain, step 2+ may reference results from the previous step — write the SQL standalone (it will be run after reviewing step 1 results, not joined). Steps share context through the summary_prompt interpretation.
"""


def generate_chain(question: str) -> dict:
    """Generate a single SQL query or a chain of up to 3 for complex questions.

    Returns:
        For single: same shape as generate_sql() output, plus mode="single"
        For chain: {
            mode: "chain",
            steps: [{step, sql, explanation, tables}, ...],
            summary_prompt: str,
            confidence: str,
            warnings: list
        }
        On failure: {mode: "single", sql: None, ...error fields...}
    """
    if not question or not question.strip():
        return {
            "mode": "single",
            "sql": None,
            "explanation": "Empty question provided.",
            "tables_used": [],
            "confidence": "none",
            "warnings": [],
        }

    try:
        schema = _load_schema()
    except FileNotFoundError as e:
        logger.error(str(e))
        return {
            "mode": "single",
            "sql": None,
            "explanation": str(e),
            "tables_used": [],
            "confidence": "none",
            "warnings": ["Schema context file is missing."],
        }

    system_prompt = _build_chain_system_prompt(schema)

    try:
        response = _client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            temperature=0,
            system=system_prompt,
            messages=[{"role": "user", "content": question}],
        )
        text = response.content[0].text.strip()
    except Exception as e:
        logger.error(f"Claude API call failed (chain): {e}")
        return {
            "mode": "single",
            "sql": None,
            "explanation": f"Claude API call failed: {e}",
            "tables_used": [],
            "confidence": "none",
            "warnings": ["API error — please retry."],
        }

    fence_match = _CODE_FENCE_RE.match(text)
    if fence_match:
        text = fence_match.group(1).strip()

    try:
        result = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        logger.warning(f"Failed to parse chain response as JSON: {text!r}")
        return {
            "mode": "single",
            "sql": None,
            "explanation": "Failed to parse Claude response.",
            "tables_used": [],
            "confidence": "none",
            "warnings": [f"Raw response: {text[:200]}"],
        }

    if not isinstance(result, dict):
        return {
            "mode": "single",
            "sql": None,
            "explanation": "Unexpected response format.",
            "tables_used": [],
            "confidence": "none",
            "warnings": [],
        }

    mode = result.get("mode", "single")

    if mode == "chain":
        steps = result.get("steps", [])
        # Normalise each step
        norm_steps = []
        for s in steps[:3]:  # max 3
            norm_steps.append({
                "step": s.get("step", len(norm_steps) + 1),
                "sql": s.get("sql", ""),
                "explanation": s.get("explanation", ""),
                "tables": s.get("tables", []),
            })
        return {
            "mode": "chain",
            "steps": norm_steps,
            "summary_prompt": result.get("summary_prompt", "Summarise these results for a business manager."),
            "confidence": result.get("confidence", "medium"),
            "warnings": result.get("warnings", []),
        }
    else:
        return {
            "mode": "single",
            "sql": result.get("sql"),
            "explanation": result.get("explanation", ""),
            "tables_used": result.get("tables_used", []),
            "confidence": result.get("confidence", "none"),
            "warnings": result.get("warnings", []),
        }


# ---------------------------------------------------------------------------
# Deep Dive — pre-built SQL chains, no LLM call
# ---------------------------------------------------------------------------

def _sanitize_id(value: str) -> str:
    """Sanitize an identifier for safe use in SQL string interpolation."""
    return str(value).replace("'", "''").replace(";", "").replace("--", "")[:100]


def _deep_dive_project(project_code: str, project_title: str) -> dict:
    """Return a 4-step deep dive chain for a specific project."""
    code = _sanitize_id(project_code)
    title = _sanitize_id(project_title) if project_title else code

    steps = [
        {
            "step": 1,
            "sql": (
                f"SELECT p.Project_Code, p.Project_Title, p.Project_Status, p.Life_Cycle, "
                f"p.Customer, c.client_Name AS [Customer Name], p.PIC, p.Internal_PIC, "
                f"p.Sales_Amount, p.PO_No, p.PO_Date, p.PO_Amount, "
                f"p.Created_Date, p.Plant_Date, p.Project_Description "
                f"FROM ProSt p "
                f"LEFT JOIN CLIENT_MASTER c ON p.Customer = c.client_Code "
                f"WHERE p.Project_Code = '{code}' "
                f"AND p.Created_Date != '2025-04-21'"
            ),
            "explanation": f"Core project details for {title}",
            "tables": ["ProSt", "CLIENT_MASTER"],
        },
        {
            "step": 2,
            "sql": (
                f"SELECT DISTINCT Invoice_No AS [Invoice No], Inv_Project_Title AS [Project Title], "
                f"Line_Status AS [Status], Total_Amount AS [Line Amount], Grand_Total AS [Invoice Total], "
                f"Invoice_CreatedAt AS [Created], PaymentsClosed_At AS [Payment Date] "
                f"FROM INVOICE_DETAILS "
                f"WHERE Project_Code = '{code}' "
                f"ORDER BY Invoice_CreatedAt DESC"
            ),
            "explanation": f"All invoices raised for project {title}",
            "tables": ["INVOICE_DETAILS"],
        },
        {
            "step": 3,
            "sql": (
                f"SELECT Project_Code, Project_Title, Status, Customer_Code, "
                f"PSD AS [Start Date], Created_At AS [Created] "
                f"FROM OPERATIONS "
                f"WHERE Project_Code = '{code}' "
                f"ORDER BY Created_At DESC"
            ),
            "explanation": f"Operations / implementation status for {title}",
            "tables": ["OPERATIONS"],
        },
        {
            "step": 4,
            "sql": (
                f"SELECT AmcID AS [AMC ID], ProjectTitle AS [Project Title], "
                f"CustomerName AS [Customer], Status AS [AMC Status], "
                f"AMCStartDate AS [Start], AMCEndDate AS [End], CoverageEnd AS [Coverage End], "
                f"AMC_Amount AS [AMC Amount], TotalAmount AS [Total Amount] "
                f"FROM AMC_MASTER "
                f"WHERE ProjectCode = '{code}' "
                f"AND Status IS NOT NULL AND Status != '' "
                f"ORDER BY AMCStartDate DESC"
            ),
            "explanation": f"AMC contracts linked to project {title}",
            "tables": ["AMC_MASTER"],
        },
    ]

    return {
        "mode": "deep_dive",
        "entity_type": "project",
        "entity_label": title,
        "steps": steps,
        "summary_prompt": (
            f"Give a complete business summary of project '{title}' covering: "
            f"current status and lifecycle stage, invoice collection health, "
            f"implementation progress, and AMC contract situation."
        ),
        "confidence": "high",
        "warnings": [],
    }


def _deep_dive_customer(customer_code: str, customer_name: str) -> dict:
    """Return a 5-step deep dive chain for a customer matched by exact code."""
    code = _sanitize_id(customer_code)
    name = _sanitize_id(customer_name) if customer_name else code

    steps = [
        {
            "step": 1,
            "sql": (
                f"SELECT client_Code AS [Code], client_Name AS [Name], "
                f"client_GSTIN AS [GSTIN], client_Address1 AS [Address], "
                f"client_State AS [State], client_Status AS [Status] "
                f"FROM CLIENT_MASTER "
                f"WHERE client_Code = '{code}'"
            ),
            "explanation": f"Master profile for customer {name}",
            "tables": ["CLIENT_MASTER"],
        },
        {
            "step": 2,
            "sql": (
                f"SELECT Project_Code, Project_Title, Project_Status, Life_Cycle, "
                f"PIC, Sales_Amount, PO_Amount, Created_Date, Plant_Date "
                f"FROM ProSt "
                f"WHERE Customer = '{code}' "
                f"AND Created_Date != '2025-04-21' "
                f"ORDER BY Created_Date DESC"
            ),
            "explanation": f"All projects for customer {name}",
            "tables": ["ProSt"],
        },
        {
            "step": 3,
            "sql": (
                f"SELECT DISTINCT Invoice_No AS [Invoice No], Inv_Project_Title AS [Project], "
                f"Line_Status AS [Status], Total_Amount AS [Amount], Grand_Total AS [Invoice Total], "
                f"Invoice_CreatedAt AS [Created], PaymentsClosed_At AS [Paid] "
                f"FROM INVOICE_DETAILS "
                f"WHERE Customer = '{code}' "
                f"ORDER BY Invoice_CreatedAt DESC"
            ),
            "explanation": f"Invoice history for customer {name}",
            "tables": ["INVOICE_DETAILS"],
        },
        {
            "step": 4,
            "sql": (
                f"SELECT AmcID AS [ID], ProjectTitle AS [Project], Status AS [AMC Status], "
                f"AMCStartDate AS [Start], AMCEndDate AS [End], CoverageEnd AS [Coverage End], "
                f"AMC_Amount AS [AMC Amount] "
                f"FROM AMC_MASTER "
                f"WHERE CustomerCode = '{code}' "
                f"AND Status IS NOT NULL AND Status != '' "
                f"ORDER BY AMCEndDate DESC"
            ),
            "explanation": f"AMC contracts for customer {name}",
            "tables": ["AMC_MASTER"],
        },
        {
            "step": 5,
            "sql": (
                f"SELECT Project_Code, Project_Title, Status, PSD AS [Start Date], "
                f"Created_At AS [Created] "
                f"FROM OPERATIONS "
                f"WHERE Customer_Code = '{code}' "
                f"ORDER BY Created_At DESC"
            ),
            "explanation": f"Active operations / implementations for customer {name}",
            "tables": ["OPERATIONS"],
        },
    ]

    return {
        "mode": "deep_dive",
        "entity_type": "customer",
        "entity_label": name,
        "steps": steps,
        "summary_prompt": (
            f"Give a complete 360-degree business summary of customer '{name}' covering: "
            f"their overall profile, project history and current status, "
            f"invoice collection and outstanding payments, AMC renewals, "
            f"and active implementation work."
        ),
        "confidence": "high",
        "warnings": [],
    }


def _deep_dive_customer_by_name(customer_name: str) -> dict:
    """Return a 5-step deep dive chain for a customer matched by name (LIKE search).

    Used when the user provides a name but no code.  All 5 steps use a subquery
    against CLIENT_MASTER so results span every matching customer code.
    """
    name = _sanitize_id(customer_name)
    # Subquery reused in every step
    sub = f"SELECT client_Code FROM CLIENT_MASTER WHERE client_Name LIKE '%{name}%'"

    steps = [
        {
            "step": 1,
            "sql": (
                f"SELECT client_Code AS [Code], client_Name AS [Name], "
                f"client_GSTIN AS [GSTIN], client_Address1 AS [Address], "
                f"client_State AS [State], client_Status AS [Status] "
                f"FROM CLIENT_MASTER "
                f"WHERE client_Name LIKE '%{name}%' "
                f"ORDER BY client_Name"
            ),
            "explanation": f"Customer profiles matching '{customer_name}'",
            "tables": ["CLIENT_MASTER"],
        },
        {
            "step": 2,
            "sql": (
                f"SELECT Project_Code, Project_Title, Project_Status, Life_Cycle, "
                f"Customer, PIC, Sales_Amount, PO_Amount, Created_Date, Plant_Date "
                f"FROM ProSt "
                f"WHERE Customer IN ({sub}) "
                f"AND Created_Date != '2025-04-21' "
                f"ORDER BY Created_Date DESC"
            ),
            "explanation": f"All projects for customers matching '{customer_name}'",
            "tables": ["ProSt", "CLIENT_MASTER"],
        },
        {
            "step": 3,
            "sql": (
                f"SELECT DISTINCT Invoice_No AS [Invoice No], Customer, "
                f"Inv_Project_Title AS [Project], "
                f"Line_Status AS [Status], Total_Amount AS [Amount], Grand_Total AS [Invoice Total], "
                f"Invoice_CreatedAt AS [Created], PaymentsClosed_At AS [Paid] "
                f"FROM INVOICE_DETAILS "
                f"WHERE Customer IN ({sub}) "
                f"ORDER BY Invoice_CreatedAt DESC"
            ),
            "explanation": f"Invoice history for customers matching '{customer_name}'",
            "tables": ["INVOICE_DETAILS", "CLIENT_MASTER"],
        },
        {
            "step": 4,
            "sql": (
                f"SELECT AmcID AS [ID], CustomerCode AS [Code], CustomerName AS [Customer], "
                f"ProjectTitle AS [Project], Status AS [AMC Status], "
                f"AMCStartDate AS [Start], AMCEndDate AS [End], CoverageEnd AS [Coverage End], "
                f"AMC_Amount AS [AMC Amount] "
                f"FROM AMC_MASTER "
                f"WHERE CustomerCode IN ({sub}) "
                f"AND Status IS NOT NULL AND Status != '' "
                f"ORDER BY AMCEndDate DESC"
            ),
            "explanation": f"AMC contracts for customers matching '{customer_name}'",
            "tables": ["AMC_MASTER", "CLIENT_MASTER"],
        },
        {
            "step": 5,
            "sql": (
                f"SELECT Project_Code, Project_Title, Status, Customer_Code, "
                f"PSD AS [Start Date], Created_At AS [Created] "
                f"FROM OPERATIONS "
                f"WHERE Customer_Code IN ({sub}) "
                f"ORDER BY Created_At DESC"
            ),
            "explanation": f"Operations / implementations for customers matching '{customer_name}'",
            "tables": ["OPERATIONS", "CLIENT_MASTER"],
        },
    ]

    return {
        "mode": "deep_dive",
        "entity_type": "customer",
        "entity_label": customer_name,
        "steps": steps,
        "summary_prompt": (
            f"Give a complete 360-degree business summary of customer '{customer_name}' covering: "
            f"their overall profile, project history and current status, "
            f"invoice collection and outstanding payments, AMC renewals, "
            f"and active implementation work."
        ),
        "confidence": "medium",
        "warnings": [
            f"Matched by name '{customer_name}' — results include all customers "
            f"whose name contains this text."
        ],
    }


def generate_deep_dive(
    entity_type: str,
    entity_id: str,
    entity_name: str = "",
    entity_code: str = "",
) -> dict:
    """Generate a pre-built deep dive chain for a project or customer.

    Args:
        entity_type:  "project" or "customer"
        entity_id:    The primary identifier (Project_Code or client_Code).
                      May be empty when only a name was provided.
        entity_name:  Human-readable name for display (optional).
        entity_code:  Alias for entity_id (optional).

    Returns:
        Deep dive chain dict with mode="deep_dive", entity_type, entity_label, steps, etc.
        On unknown entity_type returns an error dict.
    """
    primary_id = entity_id or entity_code

    # ── Customer: name-only lookup ────────────────────────────────────────
    if entity_type == "customer" and not primary_id and entity_name:
        return _deep_dive_customer_by_name(entity_name)

    if not primary_id:
        return {
            "mode": "deep_dive",
            "entity_type": entity_type,
            "entity_label": entity_name or "Unknown",
            "steps": [],
            "summary_prompt": "",
            "confidence": "none",
            "warnings": ["No entity identifier provided."],
        }

    if entity_type == "project":
        return _deep_dive_project(primary_id, entity_name)
    elif entity_type == "customer":
        return _deep_dive_customer(primary_id, entity_name)
    else:
        return {
            "mode": "deep_dive",
            "entity_type": entity_type,
            "entity_label": entity_name or primary_id,
            "steps": [],
            "summary_prompt": "",
            "confidence": "none",
            "warnings": [f"Unknown entity type: {entity_type!r}. Use 'project' or 'customer'."],
        }
