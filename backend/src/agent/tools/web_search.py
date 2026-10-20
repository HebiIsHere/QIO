"""web_search tool: query a pluggable search service (stability-first).

失败时的行为约定：把中文原因（含建议）如实回报给模型，并明确不要在
同一轮里对同一查询反复重试 —— 通道被风控时重试只会得到同样的结果。
"""

from __future__ import annotations

from typing import Any

from agent import prompts
from agent.tools.base import Tool, ToolResult


class WebSearchTool(Tool):
    name = "web_search"
    description = prompts.TOOL_DESCRIPTIONS["web_search"]
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": prompts.argument_description("web_search", "query"),
            },
            "top_k": {
                "type": "integer",
                "description": prompts.argument_description("web_search", "top_k"),
            },
        },
        "required": ["query"],
    }
    is_concurrency_safe = True
    timeout_ms = 30_000

    def __init__(self, search_service: Any = None) -> None:
        self.search_service = search_service

    async def run(self, **kwargs: Any) -> ToolResult:
        query = str(kwargs.get("query", "") or "").strip()
        if not query:
            return ToolResult(ok=False, error="query is required")
        try:
            top_k = int(kwargs.get("top_k", 5))
        except (TypeError, ValueError):
            top_k = 5
        top_k = max(1, min(top_k, 20))
        if self.search_service is None:
            return ToolResult(ok=False, error="搜索服务未配置")
        outcome = await self.search_service.search(query, top_k)
        if outcome.status == "error":
            return ToolResult(
                ok=False,
                error=f"无法联网搜索：{outcome.error}（不要重复重试同一查询）",
            )
        if outcome.status == "empty" or not outcome.hits:
            return ToolResult(ok=True, content="未找到相关结果")
        lines = [
            f"- {h.title} {h.url}" + (f" — {h.snippet}" if h.snippet else "")
            for h in outcome.hits
        ]
        return ToolResult(ok=True, content="\n".join(lines))
