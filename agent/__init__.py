"""
agent — autonomous SQL query agent (v2).

Public API:
    AgentOrchestrator   — core agent loop
    AIClient            — async Anthropic wrapper
    SessionStore        — in-memory session memory
    ToolRegistry        — tool registration + dispatch
    create_database_tools — factory for the four built-in DB tools
    create_agent_router   — FastAPI router factory
    MSSQLAdapter          — v1 MSSQL connector adapter
    FileSchemaProvider    — reads v1 schema prompt files
    FileKnowledgeProvider — reads v1 knowledge files
    AgentResponse         — final response data class
    ToolResult            — tool execution result data class
"""

from agent.models import AgentResponse, ToolResult
from agent.orchestrator import AgentOrchestrator, AIClient
from agent.memory import SessionStore
from agent.tools.base import ToolRegistry
from agent.tools.database import create_database_tools
from agent.adapters import MSSQLAdapter, FileSchemaProvider, FileKnowledgeProvider
from agent.routes import create_agent_router

__all__ = [
    "AgentOrchestrator",
    "AIClient",
    "SessionStore",
    "ToolRegistry",
    "create_database_tools",
    "create_agent_router",
    "MSSQLAdapter",
    "FileSchemaProvider",
    "FileKnowledgeProvider",
    "AgentResponse",
    "ToolResult",
]
