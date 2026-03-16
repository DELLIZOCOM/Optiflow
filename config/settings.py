import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Database credentials
DB_SERVER = os.getenv("DB_SERVER")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

# API keys
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Intent parser mode: "local" (Ollama) or "cloud" (Claude API)
INTENT_PARSER_MODE = os.getenv("INTENT_PARSER_MODE", "local")
