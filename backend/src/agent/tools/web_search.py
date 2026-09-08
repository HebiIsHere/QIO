"""web_search tool: query a pluggable search service (stability-first)."""

from __future__ import annotations

from typing import Any

from agent.tools.base import Tool, ToolResult


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "联网搜索，返回标题、链接、摘要列表。"
        "调用时机：问题需要实时/外部资料。"
        "query 必填，top_k 可选（默认 5，上限 20）。"
        "若搜索不可用会明确说明，不会编造结果。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索关键词"},
            "top_k": {"type": "integer", "description": "返回条数，默认 5，上限 20"},
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
            return ToolResult(ok=False, error=f"无法联网搜索：{outcome.error}")
        if outcome.status == "empty" or not outcome.hits:
            return ToolResult(ok=True, content="未找到相关结果")
        lines = [
            f"- {h.title} {h.url}" + (f" — {h.snippet}" if h.snippet else "")
            for h in outcome.hits
        ]
        return ToolResult(ok=True, content="\n".join(lines))
