"""Tool registry with per-call failure isolation."""

from __future__ import annotations

import logging
from typing import Any

from agent.adapters.base import ToolCall, ToolSpec
from agent.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name=t.name,
                description=t.description,
                parameters=t.parameters,
            )
            for t in self._tools.values()
        ]

    async def execute(self, call: ToolCall) -> ToolResult:
        """Execute one tool call; never raises — errors become failed results."""
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(
                ok=False,
                error=f"unknown tool '{call.name}' (registered: {sorted(self._tools)})",
            )
        try:
            result = await tool.run(**call.arguments)
            if result.ok:
                return result
            return ToolResult(ok=False, error=result.error or "tool returned failure")
        except Exception as exc:  # noqa: BLE001 - isolation boundary
            logger.warning("tool %s failed: %s", call.name, exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")