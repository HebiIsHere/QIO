"""Knowledge correction: user feedback -> revoke or supersede.

Phase 1 C（契约 §6.3 / §7）：

* 参数模型保留 content / new_content / delete / candidate_index；
  新增稳定 knowledge_id 与 expected_version；
* 定位顺序：knowledge_id > candidate_index（仅在候选列表仍有效时）> content 匹配；
* 结果结构化：completed / conflict（版本冲突 code=version_conflict），字段不用中文匹配；
* 明确纠正当场生效：同一轮工具调用返回真实结果，不要求用户二次确认。

R02/R03：纠正与删除都走 knowledge/lifecycle 的**同一套原子规则**——
先在链上解析「当前版本」，再带 expected_version 做单事务替换；
旧注入快照指向的版本被取代过时，反馈必须说清楚实际删/改的是哪一条。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict

from agent.knowledge.lifecycle import (
    KnowledgeItem,
    KnowledgeNotFound,
    KnowledgeService,
    VersionConflict,
)
from agent.prompts import TOOL_DESCRIPTIONS
from agent.tools.base import TOOL_RESULT_PROTOCOL_VERSION, Tool, ToolResult
from agent.tools.contracts import json_schema_for, validate_model

MAX_CANDIDATES = 8


class CorrectKnowledgeArgs(BaseModel):
    """correct_knowledge 参数（展示 schema 与本地校验同源）。"""

    model_config = ConfigDict(extra="forbid")

    content: str | None = None
    new_content: str | None = None
    delete: bool = False
    candidate_index: int | None = None
    knowledge_id: str | None = None
    expected_version: int | None = None


def _completed(
    message: str, *, code: str, data: dict[str, Any]
) -> ToolResult:
    return ToolResult(
        ok=True,
        content=message,
        protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
        status="completed",
        code=code,
        data=data,
    )


def _conflict(exc: VersionConflict) -> ToolResult:
    message = (
        f"版本冲突：{exc.reason}；当前版本 {exc.current_id}（v{exc.current_version}），"
        "未做修改，请重新确认后再纠正"
    )
    return ToolResult(
        ok=False,
        content=message,
        error=message,
        recoverable=True,
        protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
        status="conflict",
        code="version_conflict",
        data={**exc.to_dict(), "expected_version": exc.expected_version},
    )


def _failure(code: str, message: str, *, recoverable: bool = True, data: dict | None = None) -> ToolResult:
    return ToolResult(
        ok=False,
        content=message,
        error=message,
        recoverable=recoverable,
        protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
        status="failed",
        code=code,
        data=dict(data or {}),
    )


class CorrectKnowledgeTool(Tool):
    name = "correct_knowledge"
    description = TOOL_DESCRIPTIONS["correct_knowledge"]
    parameters = json_schema_for(CorrectKnowledgeArgs)

    def __init__(
        self,
        conn: sqlite3.Connection,
        snapshot_provider: Callable[[], list[dict]] | None = None,
    ) -> None:
        self.conn = conn
        self.snapshot_provider = snapshot_provider or (lambda: [])

    # -- matching ---------------------------------------------------------

    def _match(self, content: str) -> list[dict]:
        """Candidates in priority order: injection snapshot, then active library."""
        seen: dict[str, dict] = {}
        for item in self.snapshot_provider() or []:
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

    # -- target resolution ------------------------------------------------

    def _locate(
        self, args: CorrectKnowledgeArgs
    ) -> tuple[KnowledgeItem | None, ToolResult | None, bool]:
        """返回 (匹配条目, 失败结果, 是否来自旧快照)。定位顺序按契约执行。"""
        ks = KnowledgeService(self.conn)
        content = str(args.content or "").strip()
        if args.knowledge_id:
            matched = ks.get(str(args.knowledge_id))
            if matched is None:
                return None, _failure(
                    "not_found",
                    f"未找到知识条目：{args.knowledge_id}",
                    recoverable=True,
                ), False
            current = ks.resolve_current(matched.id) or matched
            return current, None, current.id != matched.id
        if not content:
            return None, _failure("invalid_arguments", "content 或 knowledge_id 至少需要一个"), False
        candidates = self._match(content)
        if not candidates:
            return None, _failure("not_found", f"未找到匹配的知识条目：{content}"), False
        if args.candidate_index is not None:
            try:
                picked = candidates[int(args.candidate_index) - 1]
            except (ValueError, IndexError):
                picked = None
            if picked is not None:
                matched = ks.get(picked["item_id"])
                if matched is not None:
                    current = ks.resolve_current(matched.id) or matched
                    return current, None, current.id != matched.id
        if len(candidates) > 1 and args.candidate_index is None:
            listing = "\n".join(
                f"{i}. {c['content']}" for i, c in enumerate(candidates, start=1)
            )
            return None, _failure(
                "ambiguous",
                f"匹配到多个知识条目，请用 candidate_index 或 knowledge_id 选择：\n{listing}",
            ), False
        matched = ks.get(candidates[0]["item_id"])
        if matched is None:
            return None, _failure("not_found", "目标条目不存在"), False
        current = ks.resolve_current(matched.id) or matched
        return current, None, current.id != matched.id

    # -- run --------------------------------------------------------------

    async def run(self, **kwargs: Any) -> ToolResult:
        args, errors = validate_model(CorrectKnowledgeArgs, kwargs)
        if args is None:
            return _failure(
                "invalid_arguments",
                "参数不合法：" + "；".join(errors[:5]),
                data={"field_errors": errors},
            )
        new_content = str(args.new_content or "").strip()
        if not args.delete and not new_content:
            return _failure("invalid_arguments", "new_content 必填（除非 delete=true 表示删除）")
        current, failure, stale_snapshot = self._locate(args)
        if failure is not None or current is None:
            return failure or _failure("not_found", "目标条目不存在")
        if (
            args.expected_version is not None
            and int(args.expected_version) != int(current.version)
        ):
            return _conflict(
                VersionConflict(
                    current.id,
                    expected_version=int(args.expected_version),
                    current_id=current.id,
                    current_version=int(current.version),
                    reason=f"版本不符：期望 v{args.expected_version}，当前 v{current.version}",
                )
            )
        expected = int(args.expected_version) if args.expected_version is not None else int(current.version)
        if args.delete:
            return self._delete(current, expected, stale_snapshot=stale_snapshot)
        return self._revise(current, new_content, expected, stale_snapshot=stale_snapshot)

    # -- actions ----------------------------------------------------------

    def _delete(
        self,
        current: KnowledgeItem,
        expected_version: int,
        *,
        stale_snapshot: bool,
    ) -> ToolResult:
        ks = KnowledgeService(self.conn)
        try:
            outcome = ks.deactivate_atomic(current.id, expected_version=expected_version)
        except VersionConflict as exc:
            return _conflict(exc)
        except (KnowledgeNotFound, ValueError) as exc:
            return _failure("delete_failed", f"删除失败：{exc}")
        data = {
            "knowledge_id": current.id,
            "deleted": outcome.deleted,
            "already_inactive": outcome.already_inactive,
            "current_id": outcome.current_id,
            "current_version": outcome.current_version,
        }
        if outcome.already_inactive:
            return _completed(
                f"该知识已不是有效条目，无需删除：「{current.content[:60]}」",
                code="already_inactive",
                data=data,
            )
        message = f"已删除知识：「{current.content[:60]}」"
        if stale_snapshot:
            message += f"（你看到的旧快照 {current.id} 已被取代，本次删除作用于当前版本）"
        return _completed(message, code="deleted", data=data)

    def _revise(
        self,
        current: KnowledgeItem,
        new_content: str,
        expected_version: int,
        *,
        stale_snapshot: bool,
    ) -> ToolResult:
        if new_content == current.content:
            return _completed(
                f"内容未变化，未新增版本：「{current.content[:60]}」",
                code="unchanged",
                data={"knowledge_id": current.id, "version": current.version},
            )
        ks = KnowledgeService(self.conn)
        try:
            updated = ks.revise_atomic(
                current.id,
                expected_version,
                {"content": new_content, "node_ids": list(current.node_ids)},
                source="user_correction",
                actor="user",
            )
        except VersionConflict as exc:
            return _conflict(exc)
        except (KnowledgeNotFound, ValueError) as exc:
            return _failure("revise_failed", f"纠正失败：{exc}")
        message = f"已修正知识：「{current.content[:60]}」→「{new_content[:60]}」"
        if stale_snapshot:
            message += f"（你看到的旧快照 {current.id} 已被取代，本次修正作用于当前版本）"
        return _completed(
            message,
            code="updated",
            data={
                "knowledge_id": updated.id,
                "supersedes_id": updated.supersedes_id,
                "previous_version": int(current.version),
                "version": int(updated.version),
                "state": updated.state.value,
            },
        )
