FINANCE_INTENTS = {
    "invoices_pending": {
        "name": "invoices_pending",
        "description": "Returns full outstanding breakdown: invoiced (raised but unpaid) + unbilled (work done, invoice not yet raised). INVOICE_DETAILS is a line-item table — one invoice = multiple rows. Always use COUNT(DISTINCT Invoice_No).",
        "table": "INVOICE_DETAILS",
        "sql": (
            "-- Returns full outstanding breakdown: invoiced + unbilled\n"
            "SELECT\n"
            "    COUNT(DISTINCT Invoice_No) AS TotalInvoices,\n"
            "    SUM(CASE WHEN Invoice_No IS NOT NULL\n"
            "        THEN Grand_Total ELSE 0 END) AS InvoicedPending,\n"
            "    SUM(CASE WHEN Invoice_No IS NULL\n"
            "        THEN Grand_Total ELSE 0 END) AS UnbilledPending,\n"
            "    SUM(Grand_Total) AS TotalOutstanding\n"
            "FROM INVOICE_DETAILS\n"
            "WHERE Line_Status IN ('Invoiced', 'Pending');"
        ),
        "params": {},
        "caveats": [
            "Always show the split: invoiced vs unbilled — never just one number.",
            "NULL Invoice_No with Line_Status 'Pending' = real unbilled work (work completed but invoice not yet raised), not a data error. Include in total outstanding.",
            "INVOICE_DETAILS is a line-item table. One invoice = multiple rows. Always use COUNT(DISTINCT Invoice_No), never COUNT(*).",
            "Valid Line_Status values: 'Payments Closed', 'Invoiced', 'Pending', 'FOC'. 'Under Review' does NOT exist.",
        ],
        "retired": False,
        "redirect_to": None,
    },
    "invoices_this_month": {
        "name": "invoices_this_month",
        "description": "Returns total invoiced amount for the current month. Uses Invoice_CreatedAt for date filtering — EDOP and EWOP are NULL for all records, never use them. Only includes Line_Status = 'Invoiced' (excludes unbilled Pending).",
        "table": "INVOICE_DETAILS",
        "sql": (
            "-- Uses Invoice_CreatedAt for date filtering (EDOP is NULL — do not use)\n"
            "SELECT\n"
            "    COUNT(DISTINCT Invoice_No) AS InvoicesRaised,\n"
            "    SUM(Grand_Total) AS TotalInvoiced\n"
            "FROM INVOICE_DETAILS\n"
            "WHERE Line_Status = 'Invoiced'\n"
            "  AND MONTH(Invoice_CreatedAt) = MONTH(GETDATE())\n"
            "  AND YEAR(Invoice_CreatedAt) = YEAR(GETDATE());"
        ),
        "params": {},
        "caveats": [
            "Always use Invoice_CreatedAt for date filtering. EDOP and EWOP columns are NULL for all records — never use them.",
            "Only Line_Status = 'Invoiced' is included. Pending (unbilled) is excluded from 'invoiced this month' counts.",
            "For previous months: replace MONTH(GETDATE()) with a specific month number.",
        ],
        "retired": False,
        "redirect_to": None,
    },
    "invoice_aging": {
        "name": "invoice_aging",
        "description": "Buckets pending invoices by age (0-30, 31-60, 61-90, 90+ days) based on Invoice_CreatedAt. Only covers Line_Status = 'Invoiced'. Excludes Pending (unbilled) and Payments Closed.",
        "table": "INVOICE_DETAILS",
        "sql": (
            "-- Buckets pending invoices by age from Invoice_CreatedAt\n"
            "SELECT\n"
            "    CASE\n"
            "        WHEN DATEDIFF(DAY, Invoice_CreatedAt, GETDATE()) <= 30\n"
            "            THEN '0-30 days'\n"
            "        WHEN DATEDIFF(DAY, Invoice_CreatedAt, GETDATE()) <= 60\n"
            "            THEN '31-60 days'\n"
            "        WHEN DATEDIFF(DAY, Invoice_CreatedAt, GETDATE()) <= 90\n"
            "            THEN '61-90 days'\n"
            "        ELSE '90+ days'\n"
            "    END AS AgeBucket,\n"
            "    COUNT(DISTINCT Invoice_No) AS Invoices,\n"
            "    SUM(Grand_Total) AS Amount\n"
            "FROM INVOICE_DETAILS\n"
            "WHERE Line_Status = 'Invoiced'\n"
            "  AND Invoice_No IS NOT NULL\n"
            "GROUP BY\n"
            "    CASE\n"
            "        WHEN DATEDIFF(DAY, Invoice_CreatedAt, GETDATE()) <= 30\n"
            "            THEN '0-30 days'\n"
            "        WHEN DATEDIFF(DAY, Invoice_CreatedAt, GETDATE()) <= 60\n"
            "            THEN '31-60 days'\n"
            "        WHEN DATEDIFF(DAY, Invoice_CreatedAt, GETDATE()) <= 90\n"
            "            THEN '61-90 days'\n"
            "        ELSE '90+ days'\n"
            "    END\n"
            "ORDER BY MIN(DATEDIFF(DAY, Invoice_CreatedAt, GETDATE()));"
        ),
        "params": {},
        "caveats": [
            "Always highlight the 90+ days bucket — it represents the most overdue receivables and is the alarm bell for finance.",
            "Only covers Line_Status = 'Invoiced'. Excludes Pending (unbilled) and Payments Closed.",
            "Age is calculated from Invoice_CreatedAt, not EDOP (which is NULL for all records).",
        ],
        "retired": False,
        "redirect_to": None,
    },
    "payment_summary": {
        "name": "payment_summary",
        "description": "Returns payment received for the current month from the payment_information table (NOT INVOICE_DETAILS). Shows net received, TDS deducted, and gross amount.",
        "table": "payment_information",
        "sql": (
            "-- Queries payment_information table, not INVOICE_DETAILS\n"
            "-- TDS is deducted by client before paying — add back for gross\n"
            "SELECT\n"
            "    COUNT(DISTINCT Invoice_No) AS PaymentsReceived,\n"
            "    SUM(amount) AS TotalReceived,\n"
            "    SUM(TDS_Deduction) AS TotalTDS,\n"
            "    SUM(amount) + SUM(TDS_Deduction) AS GrossReceived\n"
            "FROM payment_information\n"
            "WHERE MONTH(amount_received_date) = MONTH(GETDATE())\n"
            "  AND YEAR(amount_received_date) = YEAR(GETDATE());"
        ),
        "params": {},
        "caveats": [
            "Uses payment_information table, NOT INVOICE_DETAILS. Never mix them — INVOICE_DETAILS is for what's owed, payment_information is for receipts.",
            "TDS (Tax Deducted at Source) is deducted by the client before paying — it is claimable pre-paid tax, NOT a loss. The company claims TDS credit when filing taxes.",
            "Always show both net received (amount) and gross (amount + TDS) in the response.",
        ],
        "retired": False,
        "redirect_to": None,
    },
}
