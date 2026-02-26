AMC_INTENTS = {
    "amc_expiry": {
        "name": "amc_expiry",
        "description": "Returns AMC contracts expiring within a time window (default 60 days). Uses AMCEndDate for the window filter. Excludes Awaiting PO (no active contract yet). 122 of 204 records have NULL AMCEndDate and are not included.",
        "table": "AMC_MASTER",
        "sql": (
            "-- Default window: 60 days. Adjust DATEADD(DAY, 60, ...) for different windows.\n"
            "-- Excludes Awaiting PO — those don't have active contracts yet\n"
            "SELECT AmcID, CustomerName, ProjectTitle, AMCEndDate,\n"
            "       DATEDIFF(DAY, GETDATE(), AMCEndDate) AS DaysRemaining,\n"
            "       Status, TotalAmount\n"
            "FROM AMC_MASTER\n"
            "WHERE AMCEndDate BETWEEN GETDATE() AND DATEADD(DAY, 60, GETDATE())\n"
            "  AND Status NOT IN ('', 'Awaiting PO')\n"
            "ORDER BY DaysRemaining ASC;"
        ),
        "params": {},
        "caveats": [
            "Default expiry window is 60 days, not 30.",
            "Awaiting PO contracts are excluded — they don't have active contracts yet.",
            "122 of 204 AMC records have no AMCEndDate — these contracts are not shown in expiry results.",
            "TotalAmount is NULL for many expiring contracts — cannot always show renewal value.",
        ],
        "retired": False,
        "redirect_to": None,
    },
    "amc_status_summary": {
        "name": "amc_status_summary",
        "description": "Breaks down AMC contracts by status with count and TotalAmount per group. 5 valid statuses: Under AMC, Awaiting PO, Work In Progress, Under Coverage, Active. Filters out 5 blank/NULL records.",
        "table": "AMC_MASTER",
        "sql": (
            "SELECT Status,\n"
            "       COUNT(*) AS Count,\n"
            "       SUM(TotalAmount) AS TotalValue\n"
            "FROM AMC_MASTER\n"
            "WHERE Status IS NOT NULL AND Status != ''\n"
            "GROUP BY Status\n"
            "ORDER BY Count DESC;"
        ),
        "params": {},
        "caveats": [
            "Awaiting PO (88) = PO not yet received from client, contract not yet active. This is pipeline revenue waiting to convert, not active contracts.",
            "Under AMC (50) = active recurring contracts currently generating revenue. Do not combine with Awaiting PO into 'active'.",
            "Work In Progress (40) = AMC setup in progress, not yet live.",
            "5 records with NULL/blank Status are filtered out.",
        ],
        "retired": False,
        "redirect_to": None,
    },
    "amc_by_customer": {
        "name": "amc_by_customer",
        "description": "Returns all AMC contracts for a specific customer using LIKE partial name match. CustomerName is the full company name (not a code). Extract the search term from the user's question and use it in the LIKE pattern.",
        "table": "AMC_MASTER",
        "sql": (
            "-- Uses LIKE for partial name match (customers don't always type exact name)\n"
            "SELECT AmcID, CustomerName, ProjectTitle,\n"
            "       Status, AMCEndDate, TotalAmount\n"
            "FROM AMC_MASTER\n"
            "WHERE CustomerName LIKE '%[CUSTOMER_NAME]%'\n"
            "ORDER BY AMCEndDate DESC;"
        ),
        "params": {
            "CUSTOMER_NAME": "",
        },
        "caveats": [
            "Uses LIKE '%name%' for partial matching — case-insensitive in SQL Server by default.",
            "AMCEndDate is NULL for most records — only active Under AMC contracts tend to have dates.",
            "TotalAmount is NULL for many records — cannot always show contract values.",
        ],
        "retired": False,
        "redirect_to": None,
    },
    "amc_revenue": {
        "name": "amc_revenue",
        "description": "Shows AMC revenue breakdown by status. CRITICAL: TotalAmount = original project deployment cost (one-time). AMC_Amount = annual maintenance fee (recurring revenue). Always present AMC_Amount as the revenue figure, never TotalAmount.",
        "table": "AMC_MASTER",
        "sql": (
            "-- Use AMC_Amount for revenue — TotalAmount is the original project cost, not AMC fee\n"
            "SELECT Status,\n"
            "       COUNT(*) AS Contracts,\n"
            "       SUM(TotalAmount) AS TotalAmount,\n"
            "       SUM(AMC_Amount) AS AMC_Amount\n"
            "FROM AMC_MASTER\n"
            "WHERE Status IS NOT NULL AND Status != ''\n"
            "GROUP BY Status\n"
            "ORDER BY TotalAmount DESC;"
        ),
        "params": {},
        "caveats": [
            "ALWAYS use AMC_Amount for revenue figures, NEVER TotalAmount. TotalAmount is the original one-time deployment cost. AMC_Amount is the annual maintenance fee — the actual recurring revenue.",
            "Under AMC AMC_Amount = confirmed recurring revenue from active contracts.",
            "Awaiting PO AMC_Amount = largest pipeline value, but not yet converted to active contracts (PO not received from client).",
            "Both columns are included in the SQL for reference, but only AMC_Amount should be presented as revenue.",
        ],
        "retired": False,
        "redirect_to": None,
    },
}
