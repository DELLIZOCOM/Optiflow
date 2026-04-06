"""Central registry for all runtime data file paths."""
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

DATA_DIR      = _PROJECT_ROOT / "data"
CONFIG_DIR    = DATA_DIR / "config"
PROMPTS_DIR   = DATA_DIR / "prompts"
KNOWLEDGE_DIR = DATA_DIR / "knowledge"
LOGS_DIR      = DATA_DIR / "logs"

USERS_PATH         = CONFIG_DIR / "users.json"
SECRET_PATH        = CONFIG_DIR / ".secret"
MODEL_CONFIG_PATH  = CONFIG_DIR / "model_config.json"
DB_CONFIG_PATH     = CONFIG_DIR / "db_config.json"
SECURITY_PATH      = CONFIG_DIR / "security.json"
SCHEMA_META_PATH   = CONFIG_DIR / "schema.json"

COMPANY_MD_PATH  = KNOWLEDGE_DIR / "company.md"
SUGGESTED_Q_PATH = KNOWLEDGE_DIR / "suggested_questions.json"

SCHEMA_CONTEXT_PATH = PROMPTS_DIR / "schema_context.txt"
SCHEMA_INDEX_PATH   = PROMPTS_DIR / "schema_index.txt"
TABLES_DIR          = PROMPTS_DIR / "tables"

AUDIT_LOG_PATH    = LOGS_DIR / "audit.jsonl"
APPROVED_Q_PATH   = LOGS_DIR / "approved_queries.jsonl"
FEEDBACK_LOG_PATH = LOGS_DIR / "feedback.jsonl"
