"""Builtin tools shipped alongside native tools (post-approval parity)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from agent.tools.base import Tool, ToolResult


class EchoTool(Tool):
    name = "echo"
    description = "原样回显输入文本，用于连通性/冒烟自检。"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

    async def run(self, **kwargs):
        return ToolResult(ok=True, content=kwargs.get("text", ""))


class NowTool(Tool):
    name = "now"
    description = "返回当前 UTC 时间。"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs):
        return ToolResult(ok=True, content=datetime.now(timezone.utc).isoformat())
