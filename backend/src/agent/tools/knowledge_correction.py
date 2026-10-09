"""Knowledge correction: user feedback -> revoke or supersede.

The tool locates the target entry without exposing internal ids: first
within the current turn's injection snapshot (the knowledge the model
just saw), then across the active library; ambiguous matches return a
numbered candidate list and the model picks by index.

R02/R03：纠正与删除都走 `knowledge/lifecycle` 的**同一套原子规则**——
先在链上解析「当前版本」，再带 `expected_version` 做单事务替换；
旧注入快照指向的版本被取代过时，反馈必须说清楚实际删/改的是哪一条，
不能删了旧行却报「当前已删」。版本冲突（并发纠正）明确报冲突，不静默二次改写。
"""

from __future__ import annotations

import sqlite3
from typing import Callable

from agent.knowledge.lifecycle import (
    KnowledgeItem,
    KnowledgeNotFound,
    KnowledgeService,
    VersionConflict,
)
from agent.prompts import TOOL_CORRECT_KNOWLEDGE_DESC
from agent.tools.base import Tool, ToolResult

MAX_CANDIDATES = 8


def _conflict_text(exc: VersionConflict) -> str:
    return (
        f"版本冲突：{exc.reason}；当前版本 {exc.current_id}（v{exc.current_version}），"
        "未做修改，请重新确认后再纠正"
    )


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
        matched = ks.get(target["item_id"])
        if matched is None:
            return ToolResult(ok=False, error="目标条目不存在")
        # 快照可能指向已被取代的旧版本：按链解析真正的当前版本。
        current = ks.resolve_current(matched.id) or matched
        stale_snapshot = current.id != matched.id
        if delete:
            return self._delete(ks, matched, current, stale_snapshot=stale_snapshot)
        return self._revise(ks, matched, current, new_content, stale_snapshot=stale_snapshot)

    # -- actions ----------------------------------------------------------

    def _delete(
        self,
        ks: KnowledgeService,
        matched: KnowledgeItem,
        current: KnowledgeItem,
        *,
        stale_snapshot: bool,
    ) -> ToolResult:
        try:
            outcome = ks.deactivate_atomic(current.id, expected_version=current.version)
        except VersionConflict as exc:
            return ToolResult(ok=False, error=_conflict_text(exc))
        except (KnowledgeNotFound, ValueError) as exc:
            return ToolResult(ok=False, error=f"删除失败：{exc}")
        if outcome.already_inactive:
            return ToolResult(
                ok=True,
                content=f"该知识已不是有效条目，无需删除：「{current.content[:60]}」",
            )
        if stale_snapshot:
            return ToolResult(
                ok=True,
                content=(
                    f"已删除当前有效版本：「{current.content[:60]}」"
                    f"（你看到的旧快照 {matched.id} 已被它取代）"
                ),
            )
        return ToolResult(ok=True, content=f"已删除知识：「{current.content[:60]}」")

    def _revise(
        self,
        ks: KnowledgeService,
        matched: KnowledgeItem,
        current: KnowledgeItem,
        new_content: str,
        *,
        stale_snapshot: bool,
    ) -> ToolResult:
        if new_content == current.content:
            return ToolResult(
                ok=True,
                content=f"内容未变化，未新增版本：「{current.content[:60]}」",
            )
        try:
            ks.revise_atomic(
                current.id,
                current.version,
                {"content": new_content, "node_ids": list(current.node_ids)},
                source="user_correction",
                actor="user",
            )
        except VersionConflict as exc:
            return ToolResult(ok=False, error=_conflict_text(exc))
        except (KnowledgeNotFound, ValueError) as exc:
            return ToolResult(ok=False, error=f"纠正失败：{exc}")
        message = f"已修正知识：「{matched.content[:60]}」→「{new_content[:60]}」"
        if stale_snapshot:
            message += f"（你看到的旧快照 {matched.id} 已被取代，本次修正作用于当前版本）"
        return ToolResult(ok=True, content=message)
