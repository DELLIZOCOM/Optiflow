# Intent registry — universal, schema-agnostic.
#
# OptiFlow uses three intent categories:
#
#   business_health  — "How's the business?", "Daily digest", "Give me a summary"
#                      Routes to a dynamically-generated multi-step health chain.
#
#   deep_dive        — "Tell me everything about project X", "Deep dive on customer Y"
#                      Routes to a dynamically-generated entity investigation chain.
#
#   agent            — Everything else. Generated SQL, human approval required.
#
# No hardcoded SQL or table names live here. The agent generates all queries
# from the live schema at runtime.

INTENT_CATEGORIES = ("business_health", "deep_dive", "agent", "unknown")
