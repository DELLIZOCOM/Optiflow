"""
Data classes for the agent module.

ToolResult  — output of a single tool execution
AgentResponse — final response returned to the caller
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ToolResult:
    """Result produced by a single tool execution."""
    tool_call_id: str
    content: str
    is_error: bool = False
    metadata: dict = field(default_factory=dict)


@dataclass
class AgentResponse:
    """Final response after the agent loop completes."""
    status: str          # "complete" | "error" | "max_iterations"
    session_id: str
    answer: Optional[str] = None
    error: Optional[str] = None
    iterations: int = 0
    tools_used: list = field(default_factory=list)
    queries_executed: int = 0
