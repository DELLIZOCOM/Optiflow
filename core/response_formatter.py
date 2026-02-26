"""
Response formatter — turns raw database rows into natural English responses.

Takes query results (rows, intent metadata, caveats) and produces a
human-readable string. Each intent has its own formatting logic.
This module does NOT touch the database or Claude API.
"""

import logging

logger = logging.getLogger(__name__)

_MAX_DISPLAY = 10


# ── Currency formatting (Indian style) ────────────────────────────────


def _fmt_currency(amount):
    """Format amount in Indian currency style.

    Below 1 lakh:    Rs 5,586
    1L to 1cr:       Rs 17.48L
    Above 1cr:       Rs 2.40cr
    """
    if amount is None:
        return "Rs 0"
    amount = float(amount)
    negative = amount < 0
    abs_amount = abs(amount)

    if abs_amount >= 1_00_00_000:  # 1 crore
        result = f"Rs {abs_amount / 1_00_00_000:.2f}cr"
    elif abs_amount >= 1_00_000:  # 1 lakh
        result = f"Rs {abs_amount / 1_00_000:.2f}L"
    else:
        result = f"Rs {int(round(abs_amount)):,}"

    if negative:
        result = f"-{result}"
    return result


# ── Helpers ───────────────────────────────────────────────────────────

_REDIRECT_MESSAGES = {
    "projects_overdue": (
        "No active projects have delivery dates set in BizFlow. "
        "Here are your longest-running active projects instead:"
    ),
    "ops_overdue": (
        "No delivery dates are recorded for most operations projects. "
        "Here are the active (non-COC) projects ordered by how long "
        "they have been open:"
    ),
}


def _overflow(total):
    """Return '...and N more.' string if total exceeds display limit."""
    if total > _MAX_DISPLAY:
        return f"\n...and {total - _MAX_DISPLAY} more."
    return ""


def _empty_message(intent_name, params_used):
    """Friendly empty-results message."""
    if intent_name == "amc_by_customer":
        name = params_used.get("CUSTOMER_NAME", "that customer")
        return f"No AMC contracts found for '{name}'."
    if intent_name == "ops_by_customer":
        name = (
            params_used.get("CUSTOMER_NAME")
            or params_used.get("CUSTOMER_CODE", "that customer")
        )
        return f"No operations projects found for '{name}'."
    if intent_name == "amc_expiry":
        return "No AMC contracts are expiring in the next 60 days."
    if intent_name == "tickets_open":
        return "No open tickets found — everything is resolved."
    if intent_name == "tickets_by_person":
        name = params_used.get("PERSON_NAME", "that person")
        return f"No tickets found for '{name}'."
    return "No results found for that query."


# ── Intent-specific formatters ────────────────────────────────────────
# Each takes (rows, params_used) and returns formatted text.


def _fmt_projects_by_age(rows, params):
    lines = [f"Here are the {len(rows)} longest-running active projects:"]
    for i, r in enumerate(rows[:_MAX_DISPLAY]):
        lines.append(
            f"{i+1}. {r.get('Project_Code', '?')} — "
            f"{r.get('Project_Title', 'Untitled')} "
            f"(PIC: {r.get('PIC', '?')}, {r.get('Project_Status', '?')}, "
            f"{r.get('DaysActive', '?')} days)"
        )
    overflow = _overflow(len(rows))
    if overflow:
        lines.append(overflow)
    return "\n".join(lines)


def _fmt_projects_by_stage(rows, params):
    lines = ["Here's the project pipeline breakdown:"]
    active_count = 0
    for r in rows:
        status = r.get("Project_Status", "?")
        count = r.get("ProjectCount", 0)
        lines.append(f"- {status}: {count} projects")
        if status in ("Seed", "Root", "Ground"):
            active_count += count
    lines.append(
        f"\nActive pipeline (Seed + Root + Ground): {active_count} projects."
    )
    return "\n".join(lines)


def _fmt_projects_stuck(rows, params):
    lines = [f"{len(rows)} projects have been stuck for 30+ days:"]
    for i, r in enumerate(rows[:_MAX_DISPLAY]):
        lines.append(
            f"{i+1}. {r.get('Project_Code', '?')} — "
            f"{r.get('Project_Title', 'Untitled')} "
            f"(PIC: {r.get('PIC', '?')}, {r.get('Project_Status', '?')}, "
            f"stuck for {r.get('DaysStuck', '?')} days)"
        )
    overflow = _overflow(len(rows))
    if overflow:
        lines.append(overflow)
    return "\n".join(lines)


def _fmt_projects_by_pic(rows, params):
    lines = [f"Project workload across {len(rows)} customer contacts:"]
    for i, r in enumerate(rows[:_MAX_DISPLAY]):
        lines.append(
            f"{i+1}. {r.get('PIC', '?')} — "
            f"{r.get('TotalProjects', 0)} projects "
            f"({r.get('Active', 0)} active, {r.get('Completed', 0)} completed)"
        )
    overflow = _overflow(len(rows))
    if overflow:
        lines.append(overflow)
    return "\n".join(lines)


def _fmt_projects_by_customer(rows, params):
    lines = [f"Project count across {len(rows)} customers:"]
    for i, r in enumerate(rows[:_MAX_DISPLAY]):
        name = r.get("client_Name") or "Unknown"
        lines.append(f"{i+1}. {name} — {r.get('TotalProjects', 0)} projects")
    overflow = _overflow(len(rows))
    if overflow:
        lines.append(overflow)
    return "\n".join(lines)


def _fmt_projects_lifecycle(rows, params):
    r = rows[0]
    included = r.get("ProjectsIncluded", "?")
    avg = r.get("AvgDays", "?")
    fastest = r.get("Fastest", "?")
    slowest = r.get("Slowest", "?")
    return (
        f"Based on {included} completed projects with valid date records:\n\n"
        f"- Average time from Seed to Plant: {avg} days\n"
        f"- Fastest completion: {fastest} days\n"
        f"- Slowest completion: {slowest} days"
    )


def _fmt_invoices_pending(rows, params):
    r = rows[0]
    invoiced = _fmt_currency(r.get("InvoicedPending"))
    unbilled = _fmt_currency(r.get("UnbilledPending"))
    total = _fmt_currency(r.get("TotalOutstanding"))
    n_inv = r.get("TotalInvoices", 0)
    return (
        f"Here's the outstanding invoice summary:\n\n"
        f"- Invoiced (raised but unpaid): {invoiced} across {n_inv} invoices\n"
        f"- Unbilled (work done, not yet invoiced): {unbilled}\n"
        f"- Total outstanding: {total}"
    )


def _fmt_invoices_this_month(rows, params):
    r = rows[0]
    total = _fmt_currency(r.get("TotalInvoiced"))
    n = r.get("InvoicesRaised", 0)
    return f"This month's invoicing: {total} across {n} invoices raised."


def _fmt_invoice_aging(rows, params):
    lines = ["Here's the invoice aging breakdown:"]
    bucket_90 = None
    for r in rows:
        bucket = r.get("AgeBucket", "?")
        n = r.get("Invoices", 0)
        amt = _fmt_currency(r.get("Amount"))
        if "90+" in str(bucket):
            lines.append(
                f"- {bucket}: {n} invoices ({amt}) <- needs urgent attention"
            )
            bucket_90 = r
        else:
            lines.append(f"- {bucket}: {n} invoices ({amt})")

    if bucket_90:
        amt_90 = _fmt_currency(bucket_90.get("Amount"))
        lines.append(
            f"\nThe 90+ days bucket totals {amt_90} "
            f"— these are your most overdue receivables."
        )
    return "\n".join(lines)


def _fmt_payment_summary(rows, params):
    r = rows[0]
    net = _fmt_currency(r.get("TotalReceived"))
    tds = _fmt_currency(r.get("TotalTDS"))
    gross = _fmt_currency(r.get("GrossReceived"))
    n = r.get("PaymentsReceived", 0)
    return (
        f"This month's payment summary:\n\n"
        f"- Net received: {net} across {n} payments\n"
        f"- TDS held by clients (claimable): {tds}\n"
        f"- Gross amount (net + TDS): {gross}"
    )


def _fmt_amc_expiry(rows, params):
    lines = [f"{len(rows)} AMC contracts expiring in the next 60 days:"]
    for i, r in enumerate(rows[:_MAX_DISPLAY]):
        days = r.get("DaysRemaining", "?")
        lines.append(
            f"{i+1}. {r.get('CustomerName', '?')} — "
            f"{r.get('ProjectTitle', 'Untitled')} "
            f"({r.get('Status', '?')}, {days} days remaining)"
        )
    overflow = _overflow(len(rows))
    if overflow:
        lines.append(overflow)
    return "\n".join(lines)


def _fmt_amc_status_summary(rows, params):
    lines = ["Here's the AMC contract breakdown by status:"]
    total_contracts = 0
    for r in rows:
        status = r.get("Status", "?")
        count = r.get("Count", 0)
        val = _fmt_currency(r.get("TotalValue"))
        lines.append(f"- {status}: {count} contracts ({val})")
        total_contracts += count
    lines.append(f"\nTotal: {total_contracts} contracts.")
    return "\n".join(lines)


def _fmt_amc_by_customer(rows, params):
    name = params.get("CUSTOMER_NAME", "customer")
    lines = [f"{len(rows)} AMC contracts found for '{name}':"]
    for i, r in enumerate(rows[:_MAX_DISPLAY]):
        status = r.get("Status", "?")
        end_date = r.get("AMCEndDate")
        end_str = str(end_date)[:10] if end_date else "no end date"
        lines.append(
            f"{i+1}. {r.get('ProjectTitle', 'Untitled')} "
            f"({status}, expires: {end_str})"
        )
    overflow = _overflow(len(rows))
    if overflow:
        lines.append(overflow)
    return "\n".join(lines)


def _fmt_amc_revenue(rows, params):
    lines = ["Here's the AMC revenue breakdown by status:"]
    total_amc = 0
    for r in rows:
        status = r.get("Status", "?")
        contracts = r.get("Contracts", 0)
        amc_amt = r.get("AMC_Amount") or 0
        total_amt = r.get("TotalAmount") or 0
        total_amc += amc_amt
        lines.append(
            f"- {status}: {contracts} contracts — "
            f"AMC revenue: {_fmt_currency(amc_amt)} "
            f"(project value: {_fmt_currency(total_amt)} for reference)"
        )
    lines.append(f"\nTotal annual AMC revenue: {_fmt_currency(total_amc)}")
    return "\n".join(lines)


def _fmt_ops_status(rows, params):
    lines = ["Here's the operations project breakdown:"]
    active_count = 0
    for r in rows:
        status = r.get("Status", "?")
        count = r.get("Count", 0)
        label = " (completed)" if status == "COC" else ""
        lines.append(f"- {status}{label}: {count} projects")
        if status != "COC":
            active_count += count
    lines.append(f"\nActive pipeline (non-COC): {active_count} projects.")
    return "\n".join(lines)


def _fmt_ops_active(rows, params):
    lines = [f"{len(rows)} active operations projects (non-COC):"]
    for i, r in enumerate(rows[:_MAX_DISPLAY]):
        lines.append(
            f"{i+1}. {r.get('Project_Code', '?')} — "
            f"{r.get('Project_Title', 'Untitled')} "
            f"({r.get('Status', '?')}, {r.get('DaysActive', '?')} days)"
        )
    overflow = _overflow(len(rows))
    if overflow:
        lines.append(overflow)
    return "\n".join(lines)


def _fmt_ops_by_customer(rows, params):
    name = (
        params.get("CUSTOMER_NAME")
        or params.get("CUSTOMER_CODE", "customer")
    )
    if rows and rows[0].get("client_Name"):
        name = rows[0]["client_Name"]
    lines = [f"{len(rows)} operations projects for {name}:"]
    for i, r in enumerate(rows[:_MAX_DISPLAY]):
        lines.append(
            f"{i+1}. {r.get('Project_Code', '?')} — "
            f"{r.get('Project_Title', 'Untitled')} "
            f"({r.get('Status', '?')})"
        )
    overflow = _overflow(len(rows))
    if overflow:
        lines.append(overflow)
    return "\n".join(lines)


def _fmt_monthly_target(rows, params):
    lines = ["Here's this month's target vs achievement:"]
    for r in rows:
        dept = r.get("Department", "?")
        target = _fmt_currency(r.get("TargetAmount"))
        achieved = _fmt_currency(r.get("AchievedAmount"))
        backlog = _fmt_currency(r.get("BacklogAmount"))
        pct = r.get("PctAchieved")
        pct_str = f"{pct:.1f}%" if pct is not None else "N/A"
        lines.append(
            f"- {dept}: {achieved} of {target} target ({pct_str}), "
            f"backlog: {backlog}"
        )
    lines.append(
        "\nWARNING: Achievement figures appear duplicated across departments "
        "— verify with finance team."
    )
    return "\n".join(lines)


def _fmt_tickets_open(rows, params):
    lines = [f"There are {len(rows)} open tickets:"]
    for i, r in enumerate(rows[:_MAX_DISPLAY]):
        lines.append(
            f"{i+1}. {r.get('Ticket_ID', '?')} — "
            f"{r.get('Task_Title', 'Untitled')} "
            f"(Priority: {r.get('Priority', '?')}, "
            f"assigned to: {r.get('Assigned_To', '?')}, "
            f"status: {r.get('Ticket_Status', '?')})"
        )
    overflow = _overflow(len(rows))
    if overflow:
        lines.append(overflow)
    return "\n".join(lines)


def _fmt_tickets_by_person(rows, params):
    if not rows:
        return ""

    # Detect summary vs detail by checking columns in the first row.
    if "TotalTickets" in rows[0]:
        lines = [f"Ticket workload across {len(rows)} people:"]
        for i, r in enumerate(rows[:_MAX_DISPLAY]):
            lines.append(
                f"{i+1}. {r.get('Assigned_To', '?')} — "
                f"{r.get('TotalTickets', 0)} total "
                f"({r.get('OpenTickets', 0)} open, "
                f"{r.get('ResolvedTickets', 0)} resolved)"
            )
        overflow = _overflow(len(rows))
        if overflow:
            lines.append(overflow)
    else:
        person = params.get("PERSON_NAME", "person")
        lines = [f"Tickets for {person}:"]
        for i, r in enumerate(rows[:_MAX_DISPLAY]):
            lines.append(
                f"{i+1}. {r.get('Ticket_ID', '?')} — "
                f"{r.get('Task_Title', 'Untitled')} "
                f"(Priority: {r.get('Priority', '?')}, "
                f"status: {r.get('Ticket_Status', '?')})"
            )
        overflow = _overflow(len(rows))
        if overflow:
            lines.append(overflow)
    return "\n".join(lines)


def _fmt_generic(rows, params):
    """Fallback for any intent without a dedicated formatter."""
    if len(rows) == 1:
        parts = [f"- {k}: {v}" for k, v in rows[0].items()]
        return "Result:\n\n" + "\n".join(parts)

    lines = [f"{len(rows)} results:"]
    for i, r in enumerate(rows[:_MAX_DISPLAY]):
        summary = ", ".join(f"{k}: {v}" for k, v in r.items())
        lines.append(f"{i+1}. {summary}")
    overflow = _overflow(len(rows))
    if overflow:
        lines.append(overflow)
    return "\n".join(lines)


# ── Dispatch ──────────────────────────────────────────────────────────

_FORMATTERS = {
    "projects_by_age": _fmt_projects_by_age,
    "projects_by_stage": _fmt_projects_by_stage,
    "projects_stuck": _fmt_projects_stuck,
    "projects_by_pic": _fmt_projects_by_pic,
    "projects_by_customer": _fmt_projects_by_customer,
    "projects_lifecycle": _fmt_projects_lifecycle,
    "invoices_pending": _fmt_invoices_pending,
    "invoices_this_month": _fmt_invoices_this_month,
    "invoice_aging": _fmt_invoice_aging,
    "payment_summary": _fmt_payment_summary,
    "amc_expiry": _fmt_amc_expiry,
    "amc_status_summary": _fmt_amc_status_summary,
    "amc_by_customer": _fmt_amc_by_customer,
    "amc_revenue": _fmt_amc_revenue,
    "ops_status": _fmt_ops_status,
    "ops_active": _fmt_ops_active,
    "ops_by_customer": _fmt_ops_by_customer,
    "monthly_target": _fmt_monthly_target,
    "tickets_open": _fmt_tickets_open,
    "tickets_by_person": _fmt_tickets_by_person,
}


# ── Main entry point ─────────────────────────────────────────────────


def format_response(rows, intent_name, params_used, caveats,
                    redirected_from=None):
    """Format raw query results into a natural English response.

    Args:
        rows: List of row dicts from the database.
        intent_name: The resolved intent name (after redirect).
        params_used: Dict of parameters that were bound into the query.
        caveats: List of caveat strings from the intent definition.
        redirected_from: Original intent name if this was a redirect.

    Returns:
        Formatted response string.
    """
    parts = []

    # Redirect preamble
    if redirected_from:
        preamble = _REDIRECT_MESSAGES.get(redirected_from)
        if preamble:
            parts.append(preamble)

    # Empty results
    if not rows:
        parts.append(_empty_message(intent_name, params_used))
    else:
        formatter = _FORMATTERS.get(intent_name, _fmt_generic)
        try:
            parts.append(formatter(rows, params_used))
        except Exception as e:
            logger.error(f"Formatter failed for '{intent_name}': {e}")
            parts.append(_fmt_generic(rows, params_used))

    # Caveats always at the end
    if caveats:
        caveat_lines = "\n".join(f"Note: {c}" for c in caveats)
        parts.append(caveat_lines)

    return "\n\n".join(p for p in parts if p)
