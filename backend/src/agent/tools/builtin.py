"""Builtin tools shipped alongside native tools (post-approval parity)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from agent import prompts
from agent.tools.base import Tool, ToolResult


class EchoTool(Tool):
    name = "echo"
    description = prompts.TOOL_DESCRIPTIONS["echo"]
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": prompts.argument_description("echo", "text"),
            }
        },
        "required": ["text"],
    }

    async def run(self, **kwargs):
        return ToolResult(ok=True, content=kwargs.get("text", ""))


class NowTool(Tool):
    name = "now"
    description = prompts.TOOL_DESCRIPTIONS["now"]
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs):
        return ToolResult(ok=True, content=datetime.now(timezone.utc).isoformat())
