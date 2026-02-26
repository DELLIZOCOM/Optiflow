from intents.project_intents import PROJECT_INTENTS
from intents.finance_intents import FINANCE_INTENTS
from intents.amc_intents import AMC_INTENTS
from intents.ops_intents import OPS_INTENTS
from intents.target_intents import TARGET_INTENTS

META_INTENTS = {
    "business_health": {
        "name": "business_health",
        "description": "Full business health summary — runs 6 intents behind the scenes and combines into one executive digest.",
        "table": "",
        "sql": "",
        "params": {},
        "caveats": [],
        "meta": True,
        "sub_intents": [
            "projects_by_stage",
            "projects_stuck",
            "invoices_pending",
            "invoice_aging",
            "amc_status_summary",
            "monthly_target",
        ],
        "retired": False,
    },
}

INTENT_REGISTRY = {
    **PROJECT_INTENTS,
    **FINANCE_INTENTS,
    **AMC_INTENTS,
    **OPS_INTENTS,
    **TARGET_INTENTS,
    **META_INTENTS,
}
