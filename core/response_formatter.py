"""
Response formatter — turns raw database rows into natural English responses.

Every response answers TWO questions:
  1. "What does the data say?"   (the insight lead)
  2. "Why should the manager care?"  (the alert/action)

Structure: insight → alert → data → caveats.
This module does NOT touch the database or Claude API.
"""

import logging
from collections import Counter
from datetime import datetime

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


def _list_rows(rows, fmt_row):
    """Build numbered list of top 10 rows using fmt_row function."""
    lines = []
    for i, r in enumerate(rows[:_MAX_DISPLAY]):
        lines.append(f"{i+1}. {fmt_row(r)}")
    overflow = _overflow(len(rows))
    if overflow:
        lines.append(overflow)
    return "\n".join(lines)


# ── Intent-specific formatters ────────────────────────────────────────
# Each takes (rows, params_used) and returns formatted text.
# Structure: insight lead → conditional alert → data listing.


def _fmt_projects_by_stage(rows, params):
    # Extract counts by status
    counts = {r.get("Project_Status", "?"): r.get("ProjectCount", 0)
              for r in rows}
    seed = counts.get("Seed", 0)
    root = counts.get("Root", 0)
    ground = counts.get("Ground", 0)
    active = seed + root + ground

    # Insight lead
    parts = [
        f"Your active pipeline has {active} projects (Seed + Root + Ground). "
        f"Seed is your largest stage at {seed} — these are early-stage leads "
        f"that haven't been quoted yet."
    ]

    # Alert
    if seed > root + ground:
        parts.append(
            "Your pipeline is top-heavy — most projects are still in Seed. "
            "Focus on converting Seed to Root."
        )

    # Data
    lines = []
    for r in rows:
        status = r.get("Project_Status", "?")
        count = r.get("ProjectCount", 0)
        lines.append(f"- {status}: {count} projects")
    parts.append("\n".join(lines))

    return "\n\n".join(parts)


def _fmt_projects_by_age(rows, params):
    total = len(rows)
    oldest = rows[0].get("DaysActive", 0) if rows else 0

    # Insight lead
    parts = [
        f"You have {total} active projects. The oldest has been running "
        f"for {oldest} days."
    ]

    # Alert
    over_90 = sum(1 for r in rows if (r.get("DaysActive") or 0) > 90)
    if over_90 > 10:
        parts.append(
            f"{over_90} projects have been active for over 90 days "
            f"— these need management attention."
        )

    # Data
    def fmt_row(r):
        return (
            f"{r.get('Project_Code', '?')} — "
            f"{r.get('Project_Title', 'Untitled')} "
            f"(PIC: {r.get('PIC', '?')}, {r.get('Project_Status', '?')}, "
            f"{r.get('DaysActive', '?')} days)"
        )
    parts.append(_list_rows(rows, fmt_row))

    return "\n\n".join(parts)


def _fmt_projects_stuck(rows, params):
    total = len(rows)

    # Count by status
    status_counts = Counter(r.get("Project_Status", "?") for r in rows)
    most_common_status, most_common_count = status_counts.most_common(1)[0]

    # Insight lead
    parts = [f"{total} projects haven't moved in 30+ days."]

    # Alert
    if most_common_status == "Seed" and most_common_count > total * 0.4:
        parts.append(
            "Most stuck projects are in Seed — these leads may have gone cold."
        )
    elif most_common_status == "Root" and most_common_count > total * 0.4:
        parts.append(
            "Most stuck projects are in Root — quotations may be stalled "
            "waiting for client response."
        )

    # Data
    def fmt_row(r):
        return (
            f"{r.get('Project_Code', '?')} — "
            f"{r.get('Project_Title', 'Untitled')} "
            f"(PIC: {r.get('PIC', '?')}, {r.get('Project_Status', '?')}, "
            f"stuck for {r.get('DaysStuck', '?')} days)"
        )
    parts.append(_list_rows(rows, fmt_row))

    return "\n\n".join(parts)


def _fmt_projects_by_pic(rows, params):
    total_pics = len(rows)
    top = rows[0] if rows else {}
    top_name = top.get("PIC", "?")
    top_active = top.get("Active", 0)

    # Insight lead
    parts = [
        f"{total_pics} client contacts are managing your projects. "
        f"Top: {top_name} with {top_active} active."
    ]

    # Alert — high concentration
    high_load = [r for r in rows if (r.get("Active") or 0) >= 5]
    if high_load:
        name = high_load[0].get("PIC", "?")
        count = high_load[0].get("Active", 0)
        parts.append(
            f"Consider whether {name}'s {count} projects need additional "
            f"support — high concentration risk."
        )

    # Data
    def fmt_row(r):
        return (
            f"{r.get('PIC', '?')} — "
            f"{r.get('TotalProjects', 0)} projects "
            f"({r.get('Active', 0)} active, {r.get('Completed', 0)} completed)"
        )
    parts.append(_list_rows(rows, fmt_row))

    return "\n\n".join(parts)


def _fmt_projects_by_customer(rows, params):
    total_customers = len(rows)
    top = rows[0] if rows else {}
    top_name = top.get("client_Name") or "Unknown"
    top_count = top.get("TotalProjects", 0)
    total_projects = sum(r.get("TotalProjects", 0) for r in rows)

    # Insight lead
    parts = [
        f"You're working with {total_customers} customers. "
        f"Top: {top_name} with {top_count} projects."
    ]

    # Alert — concentration
    if total_projects > 0 and len(rows) >= 3:
        top3_total = sum(r.get("TotalProjects", 0) for r in rows[:3])
        top3_pct = round(top3_total / total_projects * 100)
        if top3_pct > 30:
            tone = (
                "high client concentration"
                if top3_pct > 50
                else "healthy diversification"
            )
            parts.append(
                f"Your top 3 customers account for {top3_pct}% of projects "
                f"— {tone}."
            )

    # Data
    def fmt_row(r):
        name = r.get("client_Name") or "Unknown"
        return f"{name} — {r.get('TotalProjects', 0)} projects"
    parts.append(_list_rows(rows, fmt_row))

    return "\n\n".join(parts)


def _fmt_projects_lifecycle(rows, params):
    r = rows[0]
    included = r.get("ProjectsIncluded", "?")
    avg = r.get("AvgDays", 0) or 0
    fastest = r.get("Fastest", "?")
    slowest = r.get("Slowest", "?")

    # Insight lead
    parts = [
        f"Average project takes {avg} days from Seed to Plant. "
        f"Fastest was {fastest} days, slowest {slowest} days."
    ]

    # Alert
    if isinstance(avg, (int, float)):
        if avg > 60:
            parts.append(
                "Projects are taking over 2 months on average "
                "— look for bottleneck stages."
            )
        elif avg < 30:
            parts.append(
                "Strong turnaround — projects are completing in under a month."
            )

    # Data
    parts.append(
        f"Based on {included} completed projects with valid date records:\n"
        f"- Average: {avg} days\n"
        f"- Fastest: {fastest} days\n"
        f"- Slowest: {slowest} days"
    )

    return "\n\n".join(parts)


def _fmt_invoices_pending(rows, params):
    r = rows[0]
    total_raw = r.get("TotalOutstanding") or 0
    invoiced_raw = r.get("InvoicedPending") or 0
    unbilled_raw = r.get("UnbilledPending") or 0
    n_inv = r.get("TotalInvoices", 0)

    total = _fmt_currency(total_raw)
    invoiced = _fmt_currency(invoiced_raw)
    unbilled = _fmt_currency(unbilled_raw)

    # Insight lead
    parts = [
        f"Total outstanding: {total}. Of this, {invoiced} is formally "
        f"invoiced and {unbilled} is work completed but not yet billed."
    ]

    # Alert — unbilled proportion
    if total_raw > 0 and unbilled_raw / total_raw > 0.20:
        parts.append(
            f"{unbilled} in completed work hasn't been invoiced yet "
            f"— this is revenue sitting on the table."
        )

    # Data
    parts.append(
        f"- Invoiced (raised but unpaid): {invoiced} across {n_inv} invoices\n"
        f"- Unbilled (work done, not yet invoiced): {unbilled}\n"
        f"- Total outstanding: {total}"
    )

    return "\n\n".join(parts)


def _fmt_invoices_this_month(rows, params):
    r = rows[0]
    total_raw = r.get("TotalInvoiced") or 0
    total = _fmt_currency(total_raw)
    n = r.get("InvoicesRaised", 0)

    # Insight lead
    parts = [f"{n} invoices raised this month totaling {total}."]

    # Alert
    if total_raw < 10_00_000:  # less than 10L
        parts.append(
            "Billing is light this month — check if any completed projects "
            "are pending invoicing."
        )

    return "\n\n".join(parts)


def _fmt_invoice_aging(rows, params):
    # Find 90+ bucket
    bucket_90 = None
    total_amount = 0
    total_invoices = 0
    for r in rows:
        amt = r.get("Amount") or 0
        n = r.get("Invoices") or 0
        total_amount += amt
        total_invoices += n
        if "90+" in str(r.get("AgeBucket", "")):
            bucket_90 = r

    # Insight lead — alarm first
    parts = []
    if bucket_90:
        amt_90 = bucket_90.get("Amount") or 0
        n_90 = bucket_90.get("Invoices") or 0
        parts.append(
            f"{_fmt_currency(amt_90)} across {n_90} invoices have been "
            f"unpaid for over 90 days — this needs immediate follow-up."
        )
        # Alert — proportion
        if total_amount > 0 and amt_90 / total_amount > 0.30:
            pct = round(amt_90 / total_amount * 100)
            parts.append(
                f"Over {pct}% of your receivables are in the danger zone "
                f"(90+ days)."
            )
    else:
        parts.append("No invoices are in the 90+ days bucket — receivables are healthy.")

    # Data — all buckets
    lines = []
    for r in rows:
        bucket = r.get("AgeBucket", "?")
        n = r.get("Invoices", 0)
        amt = _fmt_currency(r.get("Amount"))
        if "90+" in str(bucket):
            lines.append(f"- {bucket}: {n} invoices ({amt}) <- urgent")
        else:
            lines.append(f"- {bucket}: {n} invoices ({amt})")
    parts.append("\n".join(lines))

    return "\n\n".join(parts)


def _fmt_payment_summary(rows, params):
    r = rows[0]
    net_raw = r.get("TotalReceived") or 0
    tds_raw = r.get("TotalTDS") or 0
    gross_raw = r.get("GrossReceived") or 0
    n = r.get("PaymentsReceived", 0)

    net = _fmt_currency(net_raw)
    tds = _fmt_currency(tds_raw)
    gross = _fmt_currency(gross_raw)

    # Insight lead
    parts = [
        f"{net} received this month ({gross} gross before TDS). "
        f"{n} payments collected."
    ]

    # Alert — TDS proportion
    if gross_raw > 0 and tds_raw / gross_raw > 0.05:
        tds_pct = round(tds_raw / gross_raw * 100, 1)
        parts.append(
            f"TDS accounts for {tds_pct}% of gross — {tds} held by clients "
            f"(claimable at tax filing)."
        )

    # Data
    parts.append(
        f"- Net received: {net}\n"
        f"- TDS held by clients (claimable): {tds}\n"
        f"- Gross amount (net + TDS): {gross}"
    )

    return "\n\n".join(parts)


def _fmt_amc_expiry(rows, params):
    total = len(rows)

    # Check for renewal wave (all same date)
    dates = [str(r.get("AMCEndDate", ""))[:10] for r in rows if r.get("AMCEndDate")]
    unique_dates = set(dates)

    # Insight lead
    parts = [
        f"{total} contracts expiring in the next 60 days "
        f"— renewal action needed."
    ]

    # Alert — renewal wave
    if len(unique_dates) == 1 and total > 1:
        parts.append(
            f"All {total} expire on {dates[0]} — this is a renewal wave, "
            f"prepare bulk renewals."
        )

    # Data
    def fmt_row(r):
        days = r.get("DaysRemaining", "?")
        return (
            f"{r.get('CustomerName', '?')} — "
            f"{r.get('ProjectTitle', 'Untitled')} "
            f"({r.get('Status', '?')}, {days} days remaining)"
        )
    parts.append(_list_rows(rows, fmt_row))

    return "\n\n".join(parts)


def _fmt_amc_status_summary(rows, params):
    counts = {r.get("Status", "?"): r.get("Count", 0) for r in rows}
    total = sum(counts.values())
    under_amc = counts.get("Under AMC", 0)
    awaiting_po = counts.get("Awaiting PO", 0)

    # Insight lead
    parts = [
        f"{total} total AMC contracts. {under_amc} are active (Under AMC), "
        f"but {awaiting_po} are still Awaiting PO — that's your biggest "
        f"conversion opportunity."
    ]

    # Alert
    if awaiting_po > under_amc:
        parts.append(
            "You have more contracts waiting for POs than active ones "
            "— converting these is the fastest path to recurring revenue."
        )

    # Data
    lines = []
    for r in rows:
        status = r.get("Status", "?")
        count = r.get("Count", 0)
        val = _fmt_currency(r.get("TotalValue"))
        lines.append(f"- {status}: {count} contracts ({val})")
    parts.append("\n".join(lines))

    return "\n\n".join(parts)


def _fmt_amc_by_customer(rows, params):
    name = params.get("CUSTOMER_NAME", "customer")
    total = len(rows)
    active = sum(1 for r in rows if r.get("Status") == "Under AMC")
    awaiting = sum(1 for r in rows if r.get("Status") == "Awaiting PO")

    # Insight lead
    parts = [
        f"{total} contracts found for '{name}'. "
        f"{active} active, {awaiting} awaiting PO."
    ]

    # Data
    def fmt_row(r):
        status = r.get("Status", "?")
        end_date = r.get("AMCEndDate")
        end_str = str(end_date)[:10] if end_date else "no end date"
        return (
            f"{r.get('ProjectTitle', 'Untitled')} "
            f"({status}, expires: {end_str})"
        )
    parts.append(_list_rows(rows, fmt_row))

    return "\n\n".join(parts)


def _fmt_amc_revenue(rows, params):
    # Extract key figures
    revenue_by_status = {}
    total_amc = 0
    total_project = 0
    for r in rows:
        status = r.get("Status", "?")
        amc_amt = r.get("AMC_Amount") or 0
        proj_amt = r.get("TotalAmount") or 0
        revenue_by_status[status] = amc_amt
        total_amc += amc_amt
        total_project += proj_amt

    confirmed = revenue_by_status.get("Under AMC", 0)
    awaiting = revenue_by_status.get("Awaiting PO", 0)

    # Insight lead
    parts = [
        f"Total AMC revenue pipeline: {_fmt_currency(total_amc)}. "
        f"Confirmed recurring (Under AMC): {_fmt_currency(confirmed)}. "
        f"Awaiting conversion: {_fmt_currency(awaiting)}."
    ]

    # Alert
    if awaiting > confirmed:
        parts.append(
            "More AMC revenue is waiting for POs than currently active "
            "— focus on PO collection."
        )

    # Data
    lines = []
    for r in rows:
        status = r.get("Status", "?")
        contracts = r.get("Contracts", 0)
        amc_amt = r.get("AMC_Amount") or 0
        total_amt = r.get("TotalAmount") or 0
        lines.append(
            f"- {status}: {contracts} contracts — "
            f"AMC revenue: {_fmt_currency(amc_amt)} "
            f"(project value: {_fmt_currency(total_amt)} for reference)"
        )
    parts.append("\n".join(lines))

    return "\n\n".join(parts)


def _fmt_ops_status(rows, params):
    counts = {r.get("Status", "?"): r.get("Count", 0) for r in rows}
    coc = counts.pop("COC", 0)
    active_count = sum(counts.values())

    # Find largest active group
    largest_status = max(counts, key=counts.get) if counts else "?"
    largest_count = counts.get(largest_status, 0)

    # Insight lead
    parts = [
        f"{active_count} operations projects active, {coc} completed (COC). "
        f"Largest active group: {largest_status} ({largest_count})."
    ]

    # Data
    lines = []
    for r in rows:
        status = r.get("Status", "?")
        count = r.get("Count", 0)
        label = " (completed)" if status == "COC" else ""
        lines.append(f"- {status}{label}: {count} projects")
    parts.append("\n".join(lines))

    return "\n\n".join(parts)


def _fmt_ops_active(rows, params):
    total = len(rows)

    # Count by status
    status_counts = Counter(r.get("Status", "?") for r in rows)
    largest_status, largest_count = (
        status_counts.most_common(1)[0] if status_counts else ("?", 0)
    )

    # Insight lead
    parts = [
        f"{total} active operations projects. "
        f"{largest_status} is the largest stage at {largest_count}."
    ]

    # Alert — old projects
    old_projects = [r for r in rows if (r.get("DaysActive") or 0) > 180]
    if old_projects:
        oldest = old_projects[0]
        parts.append(
            f"{len(old_projects)} projects have been active over 180 days. "
            f"Oldest: {oldest.get('Project_Code', '?')} — "
            f"{oldest.get('Project_Title', 'Untitled')} "
            f"({oldest.get('DaysActive', '?')} days)."
        )

    # Data
    def fmt_row(r):
        return (
            f"{r.get('Project_Code', '?')} — "
            f"{r.get('Project_Title', 'Untitled')} "
            f"({r.get('Status', '?')}, {r.get('DaysActive', '?')} days)"
        )
    parts.append(_list_rows(rows, fmt_row))

    return "\n\n".join(parts)


def _fmt_ops_by_customer(rows, params):
    name = (
        params.get("CUSTOMER_NAME")
        or params.get("CUSTOMER_CODE", "customer")
    )
    if rows and rows[0].get("client_Name"):
        name = rows[0]["client_Name"]

    total = len(rows)
    active = sum(1 for r in rows if r.get("Status") != "COC")
    completed = total - active

    # Insight lead
    parts = [
        f"{total} operations projects for {name}. "
        f"{active} active, {completed} completed."
    ]

    # Data
    def fmt_row(r):
        return (
            f"{r.get('Project_Code', '?')} — "
            f"{r.get('Project_Title', 'Untitled')} "
            f"({r.get('Status', '?')})"
        )
    parts.append(_list_rows(rows, fmt_row))

    return "\n\n".join(parts)


def _fmt_monthly_target(rows, params):
    # Compute overall achievement
    total_target = sum(r.get("TargetAmount") or 0 for r in rows)
    total_achieved = sum(r.get("AchievedAmount") or 0 for r in rows)
    overall_pct = (
        round(total_achieved / total_target * 100, 1)
        if total_target > 0 else 0
    )

    # Insight lead
    parts = [
        f"{overall_pct}% of this month's target achieved "
        f"({_fmt_currency(total_achieved)} of {_fmt_currency(total_target)})."
    ]

    # Alert — time vs progress
    day_of_month = datetime.now().day
    if overall_pct < 50 and day_of_month > 20:
        days_left = 30 - day_of_month
        remaining_pct = round(100 - overall_pct, 1)
        parts.append(
            f"With less than {days_left} days left, achieving the remaining "
            f"{remaining_pct}% will be challenging."
        )
    elif overall_pct > 80:
        parts.append("Strong month — on track to hit target.")

    # Data
    lines = []
    for r in rows:
        dept = r.get("Department", "?")
        target = _fmt_currency(r.get("TargetAmount"))
        achieved = _fmt_currency(r.get("AchievedAmount"))
        backlog = _fmt_currency(r.get("BacklogAmount"))
        pct = r.get("PctAchieved")
        pct_str = f"{pct:.1f}%" if pct is not None else "N/A"
        lines.append(
            f"- {dept}: {achieved} of {target} ({pct_str}), "
            f"backlog: {backlog}"
        )
    lines.append(
        "\nWARNING: Achievement figures appear duplicated across departments "
        "— verify with finance team."
    )
    parts.append("\n".join(lines))

    return "\n\n".join(parts)


def _fmt_tickets_open(rows, params):
    total = len(rows)

    # Count priorities
    priorities = Counter(r.get("Priority", "?") for r in rows)
    priority_summary = ", ".join(
        f"{c} {p}" for p, c in priorities.most_common()
    )

    # Insight lead
    parts = [f"{total} open tickets, all {priority_summary} priority."]

    # Alert — old tickets
    old_tickets = []
    for r in rows:
        created = r.get("Created_Date")
        if created:
            try:
                created_str = str(created)[:10]
                created_date = datetime.strptime(created_str, "%Y-%m-%d")
                age = (datetime.now() - created_date).days
                if age > 60:
                    old_tickets.append((r, age))
            except (ValueError, TypeError):
                pass

    if old_tickets:
        parts.append(
            f"{len(old_tickets)} tickets have been open for over 60 days "
            f"— these may be stuck or forgotten."
        )

    # Data
    def fmt_row(r):
        return (
            f"{r.get('Ticket_ID', '?')} — "
            f"{r.get('Task_Title', 'Untitled')} "
            f"(Priority: {r.get('Priority', '?')}, "
            f"assigned to: {r.get('Assigned_To', '?')}, "
            f"status: {r.get('Ticket_Status', '?')})"
        )
    parts.append(_list_rows(rows, fmt_row))

    return "\n\n".join(parts)


def _fmt_tickets_by_person(rows, params):
    if not rows:
        return ""

    # Detect summary vs detail by checking columns in the first row.
    if "TotalTickets" in rows[0]:
        # Summary mode
        total_people = len(rows)

        # Top 2 by open tickets
        sorted_by_open = sorted(
            rows, key=lambda r: r.get("OpenTickets", 0), reverse=True
        )
        top_names = []
        for r in sorted_by_open[:2]:
            name = r.get("Assigned_To", "?")
            open_count = r.get("OpenTickets", 0)
            if open_count > 0:
                top_names.append(f"{name} ({open_count})")

        # Insight lead
        if top_names:
            top_str = " and ".join(top_names)
            parts = [
                f"{total_people} people have assigned tickets. "
                f"{top_str} have the most open."
            ]
        else:
            parts = [f"{total_people} people have assigned tickets."]

        # Alert — overloaded
        overloaded = [
            r for r in rows if (r.get("OpenTickets") or 0) >= 3
        ]
        if overloaded:
            r = overloaded[0]
            parts.append(
                f"{r.get('Assigned_To', '?')} has "
                f"{r.get('OpenTickets', 0)} open tickets "
                f"— check if they need support."
            )

        # Data
        def fmt_row(r):
            return (
                f"{r.get('Assigned_To', '?')} — "
                f"{r.get('TotalTickets', 0)} total "
                f"({r.get('OpenTickets', 0)} open, "
                f"{r.get('ResolvedTickets', 0)} resolved)"
            )
        parts.append(_list_rows(rows, fmt_row))

    else:
        # Detail mode for specific person
        person = params.get("PERSON_NAME", "person")
        parts = [f"Tickets for {person}:"]

        def fmt_row(r):
            return (
                f"{r.get('Ticket_ID', '?')} — "
                f"{r.get('Task_Title', 'Untitled')} "
                f"(Priority: {r.get('Priority', '?')}, "
                f"status: {r.get('Ticket_Status', '?')})"
            )
        parts.append(_list_rows(rows, fmt_row))

    return "\n\n".join(parts)


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


# ── Business Health (meta-intent) ─────────────────────────────────────


def _safe_rows(sub_results, intent_name):
    """Get rows from a sub-result, defaulting to [] on any error."""
    result = sub_results.get(intent_name, {})
    return result.get("rows") or []


def format_business_health(sub_results):
    """Format the business_health meta-intent into an executive digest.

    Args:
        sub_results: dict mapping intent_name -> run() result dict.

    Returns:
        Formatted summary string with 5 sections + action items.
    """
    sections = []

    # ── PIPELINE HEALTH ──
    stage_rows = _safe_rows(sub_results, "projects_by_stage")
    stuck_rows = _safe_rows(sub_results, "projects_stuck")

    stage_counts = {
        r.get("Project_Status", "?"): r.get("ProjectCount", 0)
        for r in stage_rows
    }
    seed = stage_counts.get("Seed", 0)
    root = stage_counts.get("Root", 0)
    ground = stage_counts.get("Ground", 0)
    active_pipeline = seed + root + ground
    stuck_count = len(stuck_rows)

    pipeline_lines = [
        "PIPELINE HEALTH",
        f"{active_pipeline} active projects "
        f"(Seed: {seed}, Root: {root}, Ground: {ground}). "
        f"{stuck_count} projects stuck 30+ days.",
    ]
    if seed > root + ground and active_pipeline > 0:
        pipeline_lines.append(
            "Pipeline is top-heavy — focus on converting leads."
        )
    sections.append("\n".join(pipeline_lines))

    # ── CASH FLOW ──
    pending_rows = _safe_rows(sub_results, "invoices_pending")
    aging_rows = _safe_rows(sub_results, "invoice_aging")

    total_outstanding = 0
    invoiced_pending = 0
    unbilled_pending = 0
    if pending_rows:
        r = pending_rows[0]
        total_outstanding = r.get("TotalOutstanding") or 0
        invoiced_pending = r.get("InvoicedPending") or 0
        unbilled_pending = r.get("UnbilledPending") or 0

    # Find 90+ bucket
    amt_90 = 0
    n_90 = 0
    for r in aging_rows:
        if "90+" in str(r.get("AgeBucket", "")):
            amt_90 = r.get("Amount") or 0
            n_90 = r.get("Invoices") or 0

    cash_lines = [
        "CASH FLOW",
        f"{_fmt_currency(total_outstanding)} total outstanding "
        f"({_fmt_currency(invoiced_pending)} invoiced, "
        f"{_fmt_currency(unbilled_pending)} unbilled).",
    ]
    if amt_90 > 0:
        cash_lines.append(
            f"{_fmt_currency(amt_90)} in {n_90} invoices overdue 90+ days "
            f"— needs immediate follow-up."
        )
    if total_outstanding > 0 and unbilled_pending / total_outstanding > 0.20:
        cash_lines.append(
            f"{_fmt_currency(unbilled_pending)} in completed work "
            f"not yet invoiced."
        )
    sections.append("\n".join(cash_lines))

    # ── AMC & RECURRING REVENUE ──
    amc_rows = _safe_rows(sub_results, "amc_status_summary")
    amc_counts = {
        r.get("Status", "?"): r.get("Count", 0) for r in amc_rows
    }
    under_amc = amc_counts.get("Under AMC", 0)
    awaiting_po = amc_counts.get("Awaiting PO", 0)

    amc_lines = [
        "AMC & RECURRING REVENUE",
        f"{under_amc} active AMC contracts (Under AMC). "
        f"{awaiting_po} awaiting PO conversion.",
    ]
    if awaiting_po > under_amc:
        amc_lines.append(
            "More contracts waiting for POs than active "
            "— conversion opportunity."
        )
    sections.append("\n".join(amc_lines))

    # ── MONTHLY TARGET ──
    target_rows = _safe_rows(sub_results, "monthly_target")
    total_target = sum(r.get("TargetAmount") or 0 for r in target_rows)
    total_achieved = sum(r.get("AchievedAmount") or 0 for r in target_rows)
    target_pct = (
        round(total_achieved / total_target * 100, 1)
        if total_target > 0 else 0
    )
    day_of_month = datetime.now().day
    days_remaining = 30 - day_of_month

    target_lines = [
        "MONTHLY TARGET",
        f"{target_pct}% achieved "
        f"({_fmt_currency(total_achieved)} of "
        f"{_fmt_currency(total_target)}). "
        f"{days_remaining} days remaining.",
    ]
    if target_pct < 50 and day_of_month > 20:
        target_lines.append(
            "Behind pace — unlikely to hit target without acceleration."
        )
    elif target_pct > 80:
        target_lines.append("On track to hit target.")
    sections.append("\n".join(target_lines))

    # ── ACTION ITEMS ──
    actions = []

    # 1. Overdue invoices
    if n_90 > 0:
        actions.append(
            f"Follow up on {n_90} invoices worth "
            f"{_fmt_currency(amt_90)} overdue 90+ days"
        )

    # 2. Stuck pipeline
    if active_pipeline > 0 and stuck_count > active_pipeline * 0.5:
        actions.append(
            f"{stuck_count} of {active_pipeline} active projects are stuck "
            f"— review pipeline"
        )

    # 3. Unbilled work
    if unbilled_pending > 20_00_000:  # > Rs 20L
        actions.append(
            f"{_fmt_currency(unbilled_pending)} in completed work "
            f"needs to be invoiced"
        )

    # 4. Target at risk
    if target_pct < 50 and day_of_month > 20:
        shortfall = _fmt_currency(total_target - total_achieved)
        actions.append(
            f"Monthly target at risk — {shortfall} shortfall"
        )

    # 5. AMC conversion
    if awaiting_po > under_amc:
        actions.append(
            f"{awaiting_po} AMC contracts awaiting PO "
            f"— push for PO collection"
        )

    # Pick top 3
    actions = actions[:3]
    if actions:
        action_lines = ["ACTION ITEMS"]
        for i, a in enumerate(actions, 1):
            action_lines.append(f"{i}. {a}")
        sections.append("\n".join(action_lines))

    return "\n\n".join(sections)


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


def format_welcome(sub_results):
    """Build a time-aware welcome message from 4 quick health checks.

    Args:
        sub_results: dict mapping intent_name -> run() result dict.
            Expected keys: invoice_aging, amc_expiry, projects_stuck, tickets_open.

    Returns:
        Formatted welcome message string.
    """
    hour = datetime.now().hour
    if hour < 12:
        greeting = "Good morning"
    elif hour < 17:
        greeting = "Good afternoon"
    else:
        greeting = "Good evening"

    items = []

    # 1. Overdue invoices (90+ days)
    aging_rows = _safe_rows(sub_results, "invoice_aging")
    for r in aging_rows:
        if "90+" in str(r.get("AgeBucket", "")):
            amt = r.get("Amount") or 0
            n = r.get("Invoices") or 0
            if n > 0:
                items.append(
                    f"\U0001f4b0 {n} invoices worth {_fmt_currency(amt)} "
                    f"are overdue 90+ days"
                )
            break

    # 2. AMC contracts expiring within 30 days
    amc_rows = _safe_rows(sub_results, "amc_expiry")
    if amc_rows:
        items.append(
            f"\U0001f4cb {len(amc_rows)} AMC contracts expiring "
            f"in the next 30 days"
        )

    # 3. Stuck projects (no movement in 90+ days)
    stuck_rows = _safe_rows(sub_results, "projects_stuck")
    if stuck_rows:
        items.append(
            f"\u26a0\ufe0f {len(stuck_rows)} projects haven't moved "
            f"in over 90 days"
        )

    # 4. Open tickets
    ticket_rows = _safe_rows(sub_results, "tickets_open")
    if ticket_rows:
        items.append(
            f"\U0001f3ab {len(ticket_rows)} support tickets are open"
        )

    # Build message
    if items:
        body = "\n".join(f"  {item}" for item in items)
        return (
            f"{greeting}! Here's what needs your attention today:\n\n"
            f"{body}\n\n"
            f"What would you like to dig into?"
        )
    else:
        return (
            f"{greeting}! Everything looks good today — no urgent items. "
            f"Ask me anything about projects, invoices, AMC, operations, "
            f"or tickets."
        )


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
