"""memory_search tool: lets the main loop query historical memory."""

from __future__ import annotations

from typing import Any

from agent.services.retrieval import Retriever
from agent.tools.base import Tool, ToolResult


class MemorySearchTool(Tool):
    name = "memory_search"
    description = (
        "检索历史记忆。当需要回忆之前的对话、用户偏好、决定或事实时使用；"
        "返回相关记忆片段及其来源话题。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "要检索的内容描述"},
            "top_k": {"type": "integer", "description": "返回条数，默认 5"},
            "topic_id": {"type": "string", "description": "限定检索的话题（可选）"},
        },
        "required": ["query"],
    }

    def __init__(self, retriever: Retriever) -> None:
        self.retriever = retriever

    async def run(self, **kwargs: Any) -> ToolResult:
        query = str(kwargs.get("query", "")).strip()
        if not query:
            return ToolResult(ok=False, error="query is required")
        top_k = int(kwargs.get("top_k", 5))
        topic_id = kwargs.get("topic_id")
        hits = self.retriever.search(
            query, anchor_topic_id=topic_id or None, top_k=max(1, min(top_k, 20))
        )
        if not hits:
            return ToolResult(ok=True, content="未找到相关记忆")
        lines = [
            f"- [{h.title or h.topic_id or '?'}] {h.preview} "
            f"(score={h.score:.2f}, sources={','.join(h.sources)})"
            for h in hits
        ]
        return ToolResult(ok=True, content="\n".join(lines))