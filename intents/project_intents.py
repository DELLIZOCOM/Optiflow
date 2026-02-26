PROJECT_INTENTS = {
    "projects_overdue": {
        "name": "projects_overdue",
        "description": "Checks which projects are past their delivery date. RETIRED: no active projects have ProjectDeliveryDate set in BizFlow. Redirect to projects_by_age.",
        "table": "ProSt",
        "sql": "",
        "params": {},
        "caveats": [
            "No active projects have a delivery date set in BizFlow.",
            "192 of 262 total projects have no delivery date — 73% of all data.",
            "When user asks 'which projects are overdue?' respond: 'No active projects have a delivery date set in BizFlow. Would you like to see projects by how long they have been active instead?'",
        ],
        "retired": True,
        "redirect_to": "projects_by_age",
    },
    "projects_by_age": {
        "name": "projects_by_age",
        "description": "Returns active projects ordered by age (days since Created_Date). Proxy for 'overdue' when no delivery dates exist. Use this when user asks about overdue projects.",
        "table": "ProSt",
        "sql": (
            "SELECT Project_Code, Project_Title, PIC, Project_Status,\n"
            "       DATEDIFF(DAY, Created_Date, GETDATE()) AS DaysActive\n"
            "FROM ProSt\n"
            "WHERE Project_Status NOT IN ('Plant','Lost','Adjusted','Held','NON PREFERRED')\n"
            "  AND Created_Date != '2025-04-21'\n"
            "  AND PIC IS NOT NULL\n"
            "  AND PIC NOT IN ('XXX','NONE','66','25','64')\n"
            "ORDER BY DaysActive DESC;"
        ),
        "params": {},
        "caveats": [
            "No delivery dates are set. Results show longest-running active projects as a proxy.",
            "Projects active > 90 days are worth flagging to management.",
            "PIC is the customer-side project in-charge, NOT an internal employee.",
        ],
        "retired": False,
    },
    "projects_by_stage": {
        "name": "projects_by_stage",
        "description": "Counts projects in each pipeline stage. Active pipeline = Seed + Root + Ground. Plant = completed. All 8 status values are valid.",
        "table": "ProSt",
        "sql": (
            "SELECT Project_Status, COUNT(*) AS ProjectCount\n"
            "FROM ProSt\n"
            "WHERE Created_Date != '2025-04-21'\n"
            "GROUP BY Project_Status\n"
            "ORDER BY ProjectCount DESC;"
        ),
        "params": {},
        "caveats": [
            "Active pipeline = Seed + Root + Ground (89 projects).",
            "Plant = completed. Held and NON PREFERRED are intentional paused states, not stuck.",
        ],
        "retired": False,
    },
    "projects_stuck": {
        "name": "projects_stuck",
        "description": "Returns projects that have not moved in 30+ days. Excludes Held and NON PREFERRED (deliberately paused, not stuck).",
        "table": "ProSt",
        "sql": (
            "SELECT Project_Code, Project_Title, PIC, Project_Status,\n"
            "       DATEDIFF(DAY, Created_Date, GETDATE()) AS DaysStuck\n"
            "FROM ProSt\n"
            "WHERE Project_Status NOT IN ('Plant','Lost','Adjusted','Held','NON PREFERRED')\n"
            "  AND Created_Date != '2025-04-21'\n"
            "  AND DATEDIFF(DAY, Created_Date, GETDATE()) > 30\n"
            "  AND PIC NOT IN ('XXX','NONE','66','25','64')\n"
            "  AND PIC IS NOT NULL\n"
            "ORDER BY DaysStuck DESC;"
        ),
        "params": {},
        "caveats": [
            "Held and NON PREFERRED are excluded — these are intentionally paused, not stuck.",
            "30-day minimum threshold used. 7 days returns almost every active project.",
            "Consider showing top 10 by default, offer 'show more' for full list.",
            "PIC is the customer-side project in-charge, NOT an internal employee.",
        ],
        "retired": False,
    },
    "projects_by_pic": {
        "name": "projects_by_pic",
        "description": "Shows project load by customer-side project in-charge (PIC). PIC is the customer-side project manager, NOT an internal Dellizo employee. Frame as 'client contacts' or 'customer PMs'.",
        "table": "ProSt",
        "sql": (
            "SELECT PIC,\n"
            "       COUNT(*) AS TotalProjects,\n"
            "       SUM(CASE WHEN Project_Status NOT IN\n"
            "           ('Plant','Lost','Adjusted','Held','NON PREFERRED')\n"
            "           THEN 1 ELSE 0 END) AS Active,\n"
            "       SUM(CASE WHEN Project_Status = 'Plant'\n"
            "           THEN 1 ELSE 0 END) AS Completed\n"
            "FROM ProSt\n"
            "WHERE PIC IS NOT NULL\n"
            "  AND PIC NOT IN ('XXX','NONE','66','25','64')\n"
            "  AND Created_Date != '2025-04-21'\n"
            "GROUP BY PIC\n"
            "ORDER BY Active DESC;"
        ),
        "params": {},
        "caveats": [
            "PIC = customer-side project in-charge, NOT an internal Dellizo employee.",
            "Frame as 'client contacts' or 'customer PMs', not 'team members' or 'staff'.",
        ],
        "retired": False,
    },
    "projects_by_customer": {
        "name": "projects_by_customer",
        "description": "Shows which customers have the most projects. JOINs CLIENT_MASTER for readable names instead of codes.",
        "table": "ProSt",
        "sql": (
            "SELECT c.client_Name,\n"
            "       COUNT(*) AS TotalProjects\n"
            "FROM ProSt p\n"
            "LEFT JOIN CLIENT_MASTER c ON c.client_Code = p.Customer\n"
            "WHERE p.Created_Date != '2025-04-21'\n"
            "  AND p.Customer IS NOT NULL\n"
            "GROUP BY c.client_Name\n"
            "ORDER BY TotalProjects DESC;"
        ),
        "params": {},
        "caveats": [
            "Use client_Name not Customer code in responses — codes like 'HAE' mean nothing to managers.",
            "LEFT JOIN used so projects without CLIENT_MASTER match still appear.",
            "Hyundai group appears as 7+ separate entities (Autoever, Glovis, Mobis, Motor, WIA, Steel, Kefico).",
        ],
        "retired": False,
    },
    "projects_lifecycle": {
        "name": "projects_lifecycle",
        "description": "Calculates average, fastest, and slowest time from Seed to Plant (project completion). Filters out backdated migration records.",
        "table": "ProSt",
        "sql": (
            "SELECT\n"
            "    AVG(DATEDIFF(DAY, Created_Date, Plant_Date)) AS AvgDays,\n"
            "    MIN(DATEDIFF(DAY, Created_Date, Plant_Date)) AS Fastest,\n"
            "    MAX(DATEDIFF(DAY, Created_Date, Plant_Date)) AS Slowest,\n"
            "    COUNT(*) AS ProjectsIncluded\n"
            "FROM ProSt\n"
            "WHERE Project_Status = 'Plant'\n"
            "  AND Plant_Date IS NOT NULL\n"
            "  AND Created_Date IS NOT NULL\n"
            "  AND Plant_Date > Created_Date\n"
            "  AND DATEDIFF(DAY, Created_Date, Plant_Date) > 7;"
        ),
        "params": {},
        "caveats": [
            "Based on 52 completed projects with valid date records (out of 60 Plant total).",
            "8 projects excluded due to bad timestamps (Plant_Date before Created_Date or within 7 days).",
            "Always state: 'Based on 52 completed projects with valid date records.'",
        ],
        "retired": False,
    },
}
