# OptiFlow AI Accuracy Improvement Plan

## Goal

Make OptiFlow AI a more accurate, company-agnostic autonomous SQL analyst by improving:

- agent grounding
- tool output reliability
- company knowledge generation
- summary-query discipline
- product defaults for ambiguous analytics questions

This plan is written as a product roadmap and implementation guide. Some items are implemented now; others are proposed next steps.

## Problems Observed

### 1. Relative dates are under-grounded

The agent was asked for "last 10 days" and treated 2026 data as suspicious. That means the runtime prompt did not anchor the current date and timezone strongly enough.

Impact:
- wrong date windows
- inconsistent inclusion/exclusion of rows
- answers that sound uncertain even when the database is correct

### 2. Business events are being mixed

In many company schemas, "projects", "operations", "quotations", "purchase orders", "invoices", and "payments" are separate events. The current agent can merge them into one summary too easily.

Impact:
- inflated counts
- misleading finance summaries
- accidental row multiplication through joins

### 3. Tool results are optimized for readability, not machine reasoning

The SQL tool returns a formatted table string. That is readable for humans, but a weaker substrate for LLM reasoning than structured rows and metadata.

Impact:
- the model can misread aggregates
- the model can confuse preview rows with complete result sets
- final synthesis becomes less reliable

### 4. The generated `company.md` is too generic

The current knowledge generator infers broad purposes, but it does not reliably document:

- row grain
- source-of-truth tables
- date columns used for reporting
- amount/status columns
- when similar tables should stay separate
- ambiguity that requires confirmation

Impact:
- the agent starts from a vague mental model
- good schema discovery is partially wasted

### 5. The prompt does not force validation for management summaries

For summary questions, the agent should treat counts/totals as fragile and validate them with a second query or a detail check. Right now that discipline is only implied.

Impact:
- easy summarization drift
- joins used when single-table queries would be safer

## Implemented In This Pass

### Runtime grounding

- Inject current date and local datetime into the system prompt
- Instruct the agent to interpret relative dates using that runtime context

### Prompt discipline

- Require exact date-window grounding
- Require separation of projects, invoices, payments, and other business events
- Encourage single-source, single-table summaries where possible
- Encourage one aggregate query plus one detail query for validation-sensitive questions

### Tool improvements

- Keep the human-readable SQL result preview
- Add structured JSON result content alongside the preview
- Include columns and preview rows in metadata for more reliable reasoning

### Company knowledge generation

- Replace the generic company-draft prompt with a stricter, evidence-based version
- Add explicit analytical guardrails and ambiguity capture
- Feed the generator both the schema index and selected detailed table files instead of only the top-level schema index

## Next Recommended Changes

### Agent / orchestration

1. Add question classification before tool use
   - classify whether the question is about counts, trends, finance, operations, support, master data, or cross-entity linkage
   - use that classification to constrain table selection

2. Add a post-query self-check step
   - before final answer, ask the model to verify:
     - date window used
     - table(s) used
     - whether joins could multiply rows
     - whether multiple finance metrics were incorrectly merged

3. Add answer metadata in SSE
   - expose date range used
   - expose source tables used
   - expose whether answer is aggregate-only or validated by detail rows

### Tooling

1. Add a dedicated `profile_table` or `inspect_metric` tool
   - purpose: answer "what date column should I use?", "is this a header or detail table?", "what are the distinct status values?"
   - this reduces ad hoc exploratory SQL

2. Add a `validate_summary` helper
   - given a SQL aggregate and a date window, run a compact backing-detail query automatically
   - useful for management-style reports

3. Return richer metadata from SQL execution
   - query duration
   - preview truncation flag
   - aggregate column detection
   - possible duplicate-key warning when repeated identifiers are present

4. Add SQL linting before execution
   - detect missing ORDER BY
   - detect joins without explicit join predicates
   - detect `COUNT(*)` over joined tables where duplication risk is high

### Schema discovery

1. Capture more profiling data into schema files
   - likely date columns
   - likely amount columns
   - likely identifier columns
   - candidate business keys
   - categorical sample values with counts

2. Distinguish header tables from line-item tables during discovery
   - heuristics based on naming and repeated business keys
   - include that explicitly in per-table files

3. Store basic relationship hints
   - likely joins such as `Project_Code`, `Invoice_No`, `Customer_Code`, `client_Code`
   - these should be hints, not enforced truth

### `company.md` generation

1. Generate per-table sections with stronger structure
   - Purpose
   - Grain
   - Use for
   - Avoid using for
   - Key columns
   - Important dates
   - Important amounts
   - Important statuses
   - Join hints
   - Caveats

2. Generate analytical guardrails section
   - examples:
     - "Use `ProSt` for new-project counts"
     - "Use `payment_information` for cash-received questions"
     - "Do not combine invoice totals from multiple invoice-like tables unless the user asks for both"

3. Generate ambiguity section explicitly
   - status meanings unknown
   - duplicate invoice tables
   - unclear source-of-truth finance table
   - whether `pending_amount` reflects line-level or invoice-level pending balance

4. Follow-up questions should target unresolved business semantics
   - source-of-truth tables
   - status definitions
   - fiscal calendar
   - open pipeline definition
   - definition of backlog / achieved revenue / collections

### Product UX

1. Show the interpreted date range in the chat trace
2. Show tables used in the final answer panel
3. For finance queries, optionally show "Metric source"
4. Add a warning banner when the answer combines multiple business-event tables

## Product Principles

These should remain true across all companies and schemas:

- Be evidence-based first, infer second
- Prefer the narrowest reliable query over the broadest possible join
- Keep business events separate unless the user asks for linkage
- Always ground relative time using the runtime date
- Make source-of-truth choices explicit
- Preserve internal read-only safety

## Suggested Validation Cases

Use these manual tests after future changes:

1. "How many new projects were created in the last 10 days?"
2. "Show invoice activity in the last 10 days."
3. "How much payment was received in the last 10 days?"
4. "Compare quotations created this month vs invoices created this month."
5. "List projects in Root status."
6. "Which customer had the highest invoiced value in the last 30 days?"
7. "Give me project updates and finance updates for the last 10 days."

Expected behavior:
- exact date range is stated
- tables used are appropriate
- counts do not drift because of joins
- finance metrics are separated by event type

## Files Touched In This Pass

- `app/agent/orchestrator.py`
- `app/agent/prompts.py`
- `app/tools/database.py`
- `app/routes/setup.py`
- `PLAN.md`
