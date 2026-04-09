"""
BaseTool abstract class, ToolResult, and ToolRegistry.

Every tool:
  - Declares name, description, parameters (JSON Schema)
  - Implements async execute(input: dict) -> ToolResult

ToolRegistry holds all registered tools, exposes Anthropic API definitions,
and dispatches execution with error handling.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ToolResult:
    """Result produced by a single tool execution."""
    tool_call_id: str
    content: str
    is_error: bool = False
    metadata: dict = field(default_factory=dict)


class BaseTool(ABC):
    name: str
    description: str
    parameters: dict

    @abstractmethod
    async def execute(self, input: dict) -> ToolResult:
        ...


class ToolRegistry:
    """Holds registered tools; dispatches execution by name."""

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool
        logger.debug(f"ToolRegistry: registered '{tool.name}'")

    def clear(self) -> None:
        self._tools.clear()

    def get_api_definitions(self) -> list[dict]:
        """Return Anthropic-format tool definitions."""
        return [
            {
                "name":         t.name,
                "description":  t.description,
                "input_schema": t.parameters,
            }
            for t in self._tools.values()
        ]

    async def execute(
        self, tool_name: str, tool_call_id: str, input: dict
    ) -> ToolResult:
        """Execute a tool by name and attach tool_call_id to the result."""
        if tool_name not in self._tools:
            return ToolResult(
                tool_call_id=tool_call_id,
                content=(
                    f"Unknown tool: '{tool_name}'. "
                    f"Available: {list(self._tools)}"
                ),
                is_error=True,
            )
        try:
            result = await self._tools[tool_name].execute(input)
            result.tool_call_id = tool_call_id
            return result
        except Exception as exc:
            logger.exception(f"Tool '{tool_name}' raised an unexpected error")
            return ToolResult(
                tool_call_id=tool_call_id,
                content=f"Internal tool error: {exc}",
                is_error=True,
            )
