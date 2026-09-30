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
        if topic_id:
            # 工具参数说的是「限定检索的话题」：这是**过滤**，不是偏向。
            # 实体卡没有话题归属（跨话题的交流锚点），保留。
            hits = [h for h in hits if h.topic_id in (None, topic_id)]
        # 摘要检索只覆盖「封存并摘要成功」的片段；开放片段与摘要失败的内容
        # 在 memory_index 里根本不存在。这里补一条**原文**通道，让说过的话找得回来。
        covered = {h.fragment_id for h in hits if h.fragment_id}
        raw_hits = [
            hit
            for hit in self.retriever.search_messages(query, topic_id=topic_id or None, top_k=3)
            if not (hit.fragment_id and hit.fragment_id in covered)
        ]
        if not hits and not raw_hits:
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
        if raw_hits:
            lines.append(
                "（以下来自**保存的对话原文**，不是摘要记忆 —— 还没封存的片段也在其中；"
                "Text 是当时原话，可直接引用）"
            )
        role_labels = {"user": "用户", "assistant": "助手", "tool": "工具", "system": "系统"}
        for i, hit in enumerate(raw_hits, start=1):
            when = (hit.created_at or "")[:19] or "（未知时间）"
            who = role_labels.get(hit.role, hit.role)
            text = " ".join(hit.content.split())[:400]
            topic = f"{hit.topic_id or '-'}（{hit.topic_name}）" if hit.topic_name else (hit.topic_id or "-")
            lines.append(
                f"[原文{i}] Fragment: {hit.fragment_id or '（非片段来源，无法 continue）'}\n"
                f"    Topic: {topic}\n"
                f"    Time: {when} / {who}\n"
                f"    Text: {text}"
            )
        lines.append(
            "（以上为只读检索结果，不会改变当前对话位置；"
            "只有用户明确要求从这里继续时，才调用 continue_from_fragment）"
        )
        return ToolResult(ok=True, content="\n".join(lines))
