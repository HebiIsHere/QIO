"""Builtin tools shipped alongside native tools (post-approval parity)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from agent.tools.base import Tool, ToolResult


class EchoTool(Tool):
    name = "echo"
    description = "Echoes back the provided text. Useful for smoke tests."
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

    async def run(self, **kwargs):
        return ToolResult(ok=True, content=kwargs.get("text", ""))


class NowTool(Tool):
    name = "now"
    description = "Returns the current UTC time."
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs):
        return ToolResult(ok=True, content=datetime.now(timezone.utc).isoformat())