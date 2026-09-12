"""memory_search tool: lets the main loop query historical memory."""

from __future__ import annotations

from typing import Any

from agent.prompts import TOOL_MEMORY_SEARCH_DESC
from agent.services.retrieval import Retriever
from agent.tools.base import Tool, ToolResult


class MemorySearchTool(Tool):
    name = "memory_search"
    description = TOOL_MEMORY_SEARCH_DESC
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "要检索的内容描述"},
            "top_k": {"type": "integer", "description": "返回条数，默认 5"},
            "topic_id": {"type": "string", "description": "限定检索的话题（可选）"},
        },
        "required": ["query"],
    }
    # 只读检索：可与其他并发安全工具并行执行
    is_concurrency_safe = True

    def __init__(self, retriever: Retriever) -> None:
        self.retriever = retriever

    async def run(self, **kwargs: Any) -> ToolResult:
        query = str(kwargs.get("query", "")).strip()
        if not query:
            return ToolResult(ok=False, error="query 必填：请描述要检索的内容")
        top_k = int(kwargs.get("top_k", 5))
        topic_id = kwargs.get("topic_id")
        hits = self.retriever.search(
            query, anchor_topic_id=topic_id or None, top_k=max(1, min(top_k, 20))
        )
        if not hits:
            return ToolResult(ok=True, content="未找到相关记忆")
        lines: list[str] = []
        for i, h in enumerate(hits, start=1):
            # Agent 必须知道「找到的是哪一个具体历史片段」，才能 continue_from_fragment
            preview = " ".join((h.preview or "").split())[:240]
            when = (h.created_at or "")[:19] or "（未知时间）"
            lines.append(
                f"[{i}] Fragment: {h.fragment_id or '（非片段来源，无法 continue）'}\n"
                f"    Topic: {h.topic_id or '-'}\n"
                f"    Title: {h.title or h.topic_id or '-'}\n"
                f"    Time: {when}\n"
                f"    Score: {h.score:.2f}（sources={','.join(h.sources) or '-'}）\n"
                f"    Preview: {preview}"
            )
        lines.append(
            "（以上为只读检索结果，不会改变当前对话位置；"
            "只有用户明确要求从这里继续时，才调用 continue_from_fragment）"
        )
        return ToolResult(ok=True, content="\n".join(lines))
