"""Knowledge correction: user feedback -> revoke or supersede.

The tool locates the target entry without exposing internal ids: first
within the current turn's injection snapshot (the knowledge the model
just saw), then across the active library; ambiguous matches return a
numbered candidate list and the model picks by index.
"""

from __future__ import annotations

import sqlite3
from typing import Callable

from agent.knowledge.lifecycle import KnowledgeService
from agent.prompts import TOOL_CORRECT_KNOWLEDGE_DESC
from agent.tools.base import Tool, ToolResult

MAX_CANDIDATES = 8


class CorrectKnowledgeTool(Tool):
    name = "correct_knowledge"
    description = TOOL_CORRECT_KNOWLEDGE_DESC
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "要纠正的知识内容片段"},
            "new_content": {"type": "string", "description": "修正后的内容（修正时必填）"},
            "delete": {"type": "boolean", "description": "是否删除该知识，默认 false"},
            "candidate_index": {"type": "integer", "description": "多个候选时选择序号（1 起）"},
        },
        "required": ["content"],
    }

    def __init__(self, conn: sqlite3.Connection, snapshot_provider: Callable[[], list[dict]]) -> None:
        self.conn = conn
        self.snapshot_provider = snapshot_provider

    # -- matching ---------------------------------------------------------

    def _match(self, content: str) -> list[dict]:
        """Candidates in priority order: injection snapshot, then active library."""
        seen: dict[str, dict] = {}
        for item in self.snapshot_provider():
            body = str(item.get("content") or "")
            if content in body or body in content:
                seen.setdefault(item["item_id"], {"item_id": item["item_id"], "content": body})
        if seen:
            return list(seen.values())
        rows = self.conn.execute(
            "SELECT id, content FROM knowledge WHERE state = 'active' ORDER BY updated_at DESC"
        ).fetchall()
        for row in rows:
            body = row["content"] or ""
            if content in body or body in content:
                seen.setdefault(row["id"], {"item_id": row["id"], "content": body})
        return list(seen.values())[:MAX_CANDIDATES]

    # -- run --------------------------------------------------------------

    async def run(self, **kwargs) -> ToolResult:
        content = str(kwargs.get("content") or "").strip()
        new_content = str(kwargs.get("new_content") or "").strip()
        delete = bool(kwargs.get("delete", False))
        candidate_index = kwargs.get("candidate_index")
        if not content:
            return ToolResult(ok=False, error="content 必填")
        if not delete and not new_content:
            return ToolResult(
                ok=False, error="new_content 必填（除非 delete=true 表示删除）"
            )
        candidates = self._match(content)
        if not candidates:
            return ToolResult(ok=False, error=f"未找到匹配的知识条目：{content}")
        if len(candidates) > 1 and candidate_index is None:
            listing = "\n".join(
                f"{i}. {c['content']}" for i, c in enumerate(candidates, start=1)
            )
            return ToolResult(
                ok=False,
                error=f"匹配到多个知识条目，请用 candidate_index 选择：\n{listing}",
            )
        if candidate_index is not None:
            try:
                target = candidates[int(candidate_index) - 1]
            except (ValueError, IndexError):
                return ToolResult(ok=False, error=f"candidate_index 无效：{candidate_index}")
        else:
            target = candidates[0]
        ks = KnowledgeService(self.conn)
        item = ks.get(target["item_id"])
        if item is None:
            return ToolResult(ok=False, error="目标条目不存在")
        if delete:
            ks.revoke(item.id)
            return ToolResult(ok=True, content=f"已删除知识：「{item.content[:60]}」")
        new_item = ks.create(
            category=item.category,
            content=new_content,
            node_ids=list(item.node_ids),
            supersedes_id=item.id,
            provenance={"corrected_from": item.id},
        )
        # 用户纠错即确认：submit -> verify -> activate（激活时旧条目自动 revoked）
        ks.submit(new_item.id)
        ks.verify(new_item.id, verified_by="user")  # user correction is explicit confirmation
        ks.activate(new_item.id)
        return ToolResult(
            ok=True,
            content=f"已修正知识：「{item.content[:60]}」→「{new_content[:60]}」",
        )
