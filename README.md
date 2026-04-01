# 🧠 OptiFlow AI

> **A self-learning, universal natural language interface for relational databases.**  
> Ask questions in plain English. Get structured business insights powered by Claude AI, local Ollama models, and SQL Server. OptiFlow adapts to *any* schema and gets smarter over time by learning from how you use it.

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Claude AI](https://img.shields.io/badge/Claude-Anthropic-orange?logo=anthropic&logoColor=white)](https://anthropic.com)
[![Ollama](https://img.shields.io/badge/Ollama-Local%20LLM-black?logo=ollama&logoColor=white)](https://ollama.com)
[![SQL Server](https://img.shields.io/badge/SQL_Server-Compatible-CC2927?logo=microsoftsqlserver&logoColor=white)](https://www.microsoft.com/en-us/sql-server)

---

## 🚀 What Is OptiFlow AI?

OptiFlow AI evolved from a hardcoded internal tool into a **universal, schema-driven intelligent agent**. Rather than relying on static SQL templates tied to specific tables, managers can interact with their data dynamically by typing natural language questions.

OptiFlow reads the live database schema, consults the company's business rules, and authors safe, read-only SQL on the fly.

### 🔁 The Smart Learning Loop

```
User asks question → LLM generates SQL → Human approves → SQL cached → Instantly reused forever
```

Each company's OptiFlow gets smarter through its own usage — organically building a tailored library of highly accurate, human-verified queries **without ever requiring a developer to write new code.**

---

## ✨ Key Features

| Feature | Description |
|---|---|
| 🌐 **Universal Schema Support** | Point it at any SQL Server database and it learns your tables automatically via the Setup Wizard |
| 🧠 **Smart Learning Loop** | Auto-generated SQL → Human Approved → Cached → Reused. Gets better with every query |
| 📋 **Company Knowledge Base** | A `company.md` file where non-technical admins write business rules the AI strictly follows |
| 📊 **Business Health Summaries** | Auto-generates multi-step executive dashboards based on whatever your schema tracks |
| 🔍 **Entity Deep Dives** | 360° investigations into any client, project, or entity across all relevant tables |
| 🔗 **Query Chaining** | Multi-step sequential SQL investigations for complex analytics |
| 🏠 **Local LLM Support** | Switch between Claude API and local Ollama models for privacy and cost savings |
| 🛡️ **Zero-Trust Security** | Read-only enforcement, human approval gate, audit logging — hardened for enterprise |

---

## 🏗️ Architecture

```
┌───────────────────────────────────────────────────────────┐
│                      Browser (Client)                     │
│         Natural Language Question → Approval UI           │
└────────────────────────┬──────────────────────────────────┘
                         │ HTTP / SSE
┌────────────────────────▼──────────────────────────────────┐
│                  FastAPI Server (app.py)                   │
│    Auth → Cache Check → Intent Parse → Approve Gate       │
└──┬───────────────┬──────────────────┬─────────────────────┘
   │               │                  │
   ▼               ▼                  ▼
Intent Parser   Cache Layer     Agent SQL Generator
(Cloud/Local)  (approved_queries) (agent_sql_generator.py)
                                       │
                              ┌────────▼────────┐
                              │   Claude / LLM   │
                              │  (SQL Authoring) │
                              └────────┬────────┘
                                       │
                              ┌────────▼────────┐
                              │  SQL Server DB   │
                              │  (Read-Only)     │
                              └─────────────────┘
```

**Three query paths:**
1. **Agent** — Specific questions like *"Show me pending invoices for Hyundai"*
2. **Business Health** — Executive digests like *"How's the business today?"*
3. **Deep Dive** — Complete entity investigations like *"Tell me everything about project P-2024-001"*

---

## 📁 Project Structure

```
optiflow-ai/
├── app.py                        # FastAPI server — routes, auth, orchestration
├── requirements.txt              # Python dependencies
├── .env                          # Environment variables (not committed)
│
├── config/
│   ├── company.md                # Human-written business rulebook for the AI
│   ├── loader.py                 # Configuration serialization logic
│   └── settings.py               # Loads static env variables
│
├── core/                         # Core business logic
│   ├── agent_sql_generator.py    # The Brain: Schema-driven SQL generation via LLM
│   ├── approved_queries.py       # Manages the persistent approved queries log
│   ├── audit_logger.py           # Tracks all user actions & generated SQL
│   ├── auth.py                   # User authentication & session management
│   ├── db.py                     # Database connection & execution
│   ├── intent_parser.py          # Cloud LLM semantic router
│   ├── local_intent_parser.py    # Local Ollama semantic router
│   ├── query_cache.py            # In-memory performance cache
│   └── setup_manager.py          # Orchestrates the first-run onboarding wizard
│
├── intents/
│   └── __init__.py               # Core categorical intent definitions
│
├── logs/                         # Persistent system logs (gitignored)
│
├── prompts/
│   └── schema_context.txt        # Auto-discovered database schema (generated)
│
└── templates/                    # HTML UI (Jinja2)
    ├── chat.html                 # Main chat interface
    ├── company_editor.html       # Admin UI for editing company.md
    ├── login.html                # Authentication gateway
    ├── settings.html             # System configuration UI
    └── setup.html                # First-run onboarding wizard
```

---

## ⚡ Quick Start

### Prerequisites
- Python 3.10+
- Access to a Microsoft SQL Server instance (with a **read-only** user)
- An [Anthropic API key](https://console.anthropic.com/) (Claude) **or** [Ollama](https://ollama.com/) running locally

### 1. Clone & Set Up Environment

```bash
git clone https://github.com/vinayakajith/optiflow-ai.git
cd optiflow-ai

python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

### 2. Configure Environment

Create a `.env` file in the project root:

```env
SECRET_KEY=your_secure_random_key_here
PORT=8000
```

### 3. (Optional) Pull Local LLM

If you want to use local intent parsing instead of cloud:

```bash
ollama pull qwen2.5-coder:3b
```

### 4. Start the Server

```bash
uvicorn app:app --port 8000
```

Navigate to `http://localhost:8000` — if it's the first run, the **Setup Wizard** will automatically guide you through connecting your database and configuring your AI provider.

---

## 🔧 Setup Wizard

On first launch, OptiFlow's Setup Wizard walks you through:

1. **Admin Account Creation** — Set up the root administrator
2. **AI Provider** — Choose Claude (cloud) or Ollama (local) and validate API keys
3. **Database Connection** — Enter SQL Server credentials (read-only user required)
4. **Schema Discovery** — OptiFlow automatically interrogates your database and extracts table definitions, columns, and relationships
5. **Business Knowledge Base** — Write plain-English business rules in `company.md` that the AI will follow strictly

---

## 🛡️ Security Design

OptiFlow is built with a **zero-trust** approach:

- **Read-Only Enforcement** — On startup, OptiFlow checks and refuses to run if the DB user has write permissions (`sysadmin`, `db_owner`, `db_datawriter`)
- **Human Approval Gate** — No generated SQL executes without an explicit human click on "Approve"
- **LLM Restraints** — All generation prompts explicitly ban `INSERT`, `UPDATE`, `DELETE`, `DROP`
- **Audit Logging** — Every login, query generation, and SQL execution is logged

---

## 🤝 Contributing

This project is primarily maintained as an internal tool, but PRs and ideas are welcome! Please open an issue first to discuss what you'd like to change.

---

## 📄 Documentation

See [DOCUMENTATION.md](./DOCUMENTATION.md) for the complete technical deep-dive including module documentation, architecture details, and the full query flow walkthrough.

---

## 📜 License

MIT License — see [LICENSE](./LICENSE) for details.

---

<p align="center">Built with ❤️ using FastAPI, Claude AI, and Ollama</p>
