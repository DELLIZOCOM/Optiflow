from intents.project_intents import PROJECT_INTENTS
from intents.finance_intents import FINANCE_INTENTS
from intents.amc_intents import AMC_INTENTS
from intents.ops_intents import OPS_INTENTS
from intents.target_intents import TARGET_INTENTS

INTENT_REGISTRY = {
    **PROJECT_INTENTS,
    **FINANCE_INTENTS,
    **AMC_INTENTS,
    **OPS_INTENTS,
    **TARGET_INTENTS,
}
