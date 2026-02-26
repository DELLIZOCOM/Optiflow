TARGET_INTENTS = {
    "monthly_target": {
        "name": "monthly_target",
        "description": "Returns target vs achieved for all 6 departments in the current month with achievement percentage. 6 departments: Sales, Invoiced, Payments Closed, AMC Master, Invoiced AMC, Payments Closed AMC. Uses CurrentMonth (first day of month format) for date filtering.",
        "table": "Monthly_Target",
        "sql": (
            "-- Returns all 6 departments for current month with achievement %\n"
            "SELECT Department,\n"
            "       TargetAmount,\n"
            "       AchievedAmount,\n"
            "       BacklogAmount,\n"
            "       ROUND((AchievedAmount / NULLIF(TargetAmount, 0)) * 100, 1) AS PctAchieved\n"
            "FROM Monthly_Target\n"
            "WHERE MONTH(CurrentMonth) = MONTH(GETDATE())\n"
            "  AND YEAR(CurrentMonth) = YEAR(GETDATE())\n"
            "ORDER BY Department;"
        ),
        "params": {},
        "caveats": [
            "Achievement figures appear duplicated across departments — verify with finance team. Feb 2026 shows identical AchievedAmount (558693) for ALL 6 departments, which is almost certainly a data entry error.",
            "AMC Master BacklogAmount is negative (-774982) — unusual, may indicate overbilling or data error.",
            "6 departments tracked: Sales, Invoiced, Payments Closed, AMC Master, Invoiced AMC, Payments Closed AMC. Original spec only mentioned 3 — actual data has 6.",
        ],
        "retired": False,
        "redirect_to": None,
    },
    "tickets_open": {
        "name": "tickets_open",
        "description": "Returns all open/unresolved tickets. Uses BOTH Resolved=0 AND Ticket_Status='In Progress' with OR due to known data inconsistency — some tickets have Status='Resolved' but Resolved flag=0. Never use just one condition alone.",
        "table": "TICKET_DETAILS",
        "sql": (
            "-- Use BOTH Resolved=0 AND Ticket_Status='In Progress' due to data inconsistency\n"
            "-- Some tickets have Status='Resolved' but Resolved flag=0 — OR catches both\n"
            "SELECT Ticket_ID, Assigned_To, Assigned_By,\n"
            "       Task_Title, Priority, Ticket_Status,\n"
            "       Created_Date, Date_Of_Delivery\n"
            "FROM TICKET_DETAILS\n"
            "WHERE Resolved = 0\n"
            "  OR Ticket_Status = 'In Progress'\n"
            "ORDER BY Priority DESC, Created_Date ASC;"
        ),
        "params": {},
        "caveats": [
            "Uses WHERE Resolved=0 OR Ticket_Status='In Progress' — never use just one condition alone. Known inconsistency: TKTGEN007 and TKTGEN009 have Ticket_Status='Resolved' but Resolved=0.",
            "All 6 open tickets are High priority.",
            "Only 9 tickets total in the system — this is a small dataset, table may be newly introduced or underutilized.",
            "Oldest open ticket (TKTGEN007) is from Nov 2025 — over 3 months old.",
        ],
        "retired": False,
        "redirect_to": None,
    },
    "tickets_by_person": {
        "name": "tickets_by_person",
        "description": "Shows ticket workload by person. Has two SQL views: a summary view (GROUP BY Assigned_To) for overall workload, and a detail view for a specific person's tickets. Use exact name match on Assigned_To for person queries.",
        "table": "TICKET_DETAILS",
        "sql": (
            "-- Summary view: all people with ticket counts\n"
            "SELECT Assigned_To,\n"
            "       COUNT(*) AS TotalTickets,\n"
            "       SUM(CASE WHEN Resolved = 0 THEN 1 ELSE 0 END) AS OpenTickets,\n"
            "       SUM(CASE WHEN Resolved = 1 THEN 1 ELSE 0 END) AS ResolvedTickets\n"
            "FROM TICKET_DETAILS\n"
            "GROUP BY Assigned_To\n"
            "ORDER BY OpenTickets DESC;\n"
            "\n"
            "-- Detail view: for specific person\n"
            "SELECT Ticket_ID, Task_Title, Ticket_Status,\n"
            "       Priority, Created_Date, Date_Of_Delivery\n"
            "FROM TICKET_DETAILS\n"
            "WHERE Assigned_To = '[PERSON_NAME]'\n"
            "ORDER BY Created_Date DESC;"
        ),
        "params": {
            "PERSON_NAME": "",
        },
        "caveats": [
            "'Sales' appears as Assigned_To — this is a department name, not a person. May indicate tickets assigned to the sales team generally.",
            "Srini and Srinivasan S are likely the same person with two name formats — cannot confirm without clarification.",
            "Only 9 tickets total in the system — this is a small dataset.",
            "For specific person queries, use exact name match on Assigned_To.",
        ],
        "retired": False,
        "redirect_to": None,
    },
}
