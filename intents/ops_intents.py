OPS_INTENTS = {
    "ops_status": {
        "name": "ops_status",
        "description": "Counts operations projects by status. 5 valid statuses: COC (47), Implementation (13), Development (6), In Progress (5), Testing (2). COC = Certificate of Completion (project done). Active pipeline = all non-COC statuses.",
        "table": "OPERATIONS",
        "sql": (
            "SELECT Status, COUNT(*) AS Count\n"
            "FROM OPERATIONS\n"
            "WHERE Status IS NOT NULL\n"
            "GROUP BY Status\n"
            "ORDER BY Count DESC;"
        ),
        "params": {},
        "caveats": [
            "COC = Certificate of Completion — project delivered and signed off. Equivalent to 'Plant' in ProSt.",
            "Active operations (non-COC) = 26 projects. Active pipeline = Implementation + Development + In Progress + Testing.",
            "'In Progress' status was not in original spec — discovered during validation.",
        ],
        "retired": False,
        "redirect_to": None,
    },
    "ops_overdue": {
        "name": "ops_overdue",
        "description": "RETIRED: PDD (Project Delivery Date) column is unusable. 55 of 73 records have NULL PDD, 9 have fake date 1900-01-01 (SQL Server default for empty date). Only 9 records have real delivery dates. Overdue detection is not viable. Redirect to ops_active.",
        "table": "OPERATIONS",
        "sql": "",
        "params": {},
        "caveats": [
            "PDD column is broken — 55 NULL + 9 fake (1900-01-01) out of 73 records. Do NOT use PDD for any query.",
            "Only 9 of 73 records have real delivery dates — overdue detection is not viable.",
            "When user asks about overdue operations, respond: 'No delivery dates are recorded for most operations projects. Would you like to see active (non-COC) projects ordered by how long they have been open instead?'",
        ],
        "retired": True,
        "redirect_to": "ops_active",
    },
    "ops_active": {
        "name": "ops_active",
        "description": "Returns all active (non-COC) operations projects ordered by age. Uses Created_At for age calculation because PDD is broken (mostly NULL or 1900-01-01). Replaces ops_overdue.",
        "table": "OPERATIONS",
        "sql": (
            "-- COC = completed. All other statuses = active.\n"
            "-- PDD is unusable (mostly NULL or 1900-01-01) -- use Created_At for age\n"
            "SELECT Project_Code, Customer_Code, Project_Title,\n"
            "       Status, PSD, Created_At,\n"
            "       DATEDIFF(DAY, Created_At, GETDATE()) AS DaysActive\n"
            "FROM OPERATIONS\n"
            "WHERE Status != 'COC'\n"
            "  AND Status IS NOT NULL\n"
            "ORDER BY DaysActive DESC;"
        ),
        "params": {},
        "caveats": [
            "PDD is unusable — uses Created_At for age calculation as proxy for time-at-risk.",
            "COC = completed. Active = all non-COC statuses (Implementation, Development, In Progress, Testing).",
            "When user asks about overdue ops projects, frame response as: 'No delivery dates are recorded for most operations projects. Here are the 26 active (non-COC) projects ordered by how long they have been open.'",
        ],
        "retired": False,
        "redirect_to": None,
    },
    "ops_by_customer": {
        "name": "ops_by_customer",
        "description": "Returns operations projects for a specific customer. Supports both exact Customer_Code match (e.g. 'IGS') and LIKE partial match on client_Name. JOINs CLIENT_MASTER for full company name.",
        "table": "OPERATIONS",
        "sql": (
            "-- Customer_Code is a short code (IGS, PHA, HMI) — JOIN for full name\n"
            "SELECT o.Customer_Code, c.client_Name,\n"
            "       o.Project_Code, o.Project_Title,\n"
            "       o.Status, o.PSD\n"
            "FROM OPERATIONS o\n"
            "LEFT JOIN CLIENT_MASTER c ON c.client_Code = o.Customer_Code\n"
            "WHERE o.Customer_Code = '[CUSTOMER_CODE]'\n"
            "  OR c.client_Name LIKE '%[CUSTOMER_NAME]%'\n"
            "ORDER BY o.Status;"
        ),
        "params": {
            "CUSTOMER_CODE": "",
            "CUSTOMER_NAME": "",
        },
        "caveats": [
            "3 customer codes (MAN, PHA(P), SWH) have no CLIENT_MASTER match — show code only for these.",
            "Customer_Code is case-sensitive in the join — always use exact code or LIKE on client_Name.",
            "Multiple Aptiv entities exist (APT, APT(A), APT(B), APT(AP), APT(G)) — same corporate group, different plants.",
            "If user gives a company name, use LIKE on client_Name. If user gives a code (e.g. 'IGS'), use exact match on Customer_Code.",
        ],
        "retired": False,
        "redirect_to": None,
    },
}
