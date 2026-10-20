"""独立回答（B）：后台维护问题的回答处理（契约 §12.4）。

规则：

* 只加载待回答事项涉及的知识对象（按事项里记录的版本），不读主对话上下文 / 消息 /
  摘要，不写主对话；
* 输出经本地校验；失败最多一次有依据的修复；仍失败则事项 failed、不改知识；
* needs_clarification 必须有问题且不同时应用 changes；deferred 不应用 changes 并保留事项；
  no_change 不应用 changes；resolved 至少一条 revise/revoke；
* 写前对每条 change 重新检查当前版本 / 状态（CAS）；冲突则一条都不改、事项保持待回答
  并记录原因；
* 用户显式回答后不需要再审批：revise 直接生成新 active 版本（source=user_correction），
  revoke 走领域撤销；
* 权限与版本只由程序检查，模型不能自报批准 / 验证。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from agent.knowledge import lifecycle
from agent.knowledge.lifecycle import (
    KnowledgeItem,
    KnowledgeNotFound,
    KnowledgeService,
    KnowledgeState,
    VersionConflict,
    deactivate_atomic,
    resolve_current,
    revise_atomic,
)
from agent.maintenance import models, store
from agent.maintenance.models import MAX_KNOWLEDGE_CHARS, MAINTENANCE_LIMITS

logger = logging.getLogger(__name__)

#: 回答里新版本的来源键（契约 §12.4：用户回答是依据，不是维护自身来源）。
SOURCE_USER_CORRECTION = "user_correction"

#: 可以回答的事项状态（failed 允许重试；processed / rejected 不再改）。
_ANSWERABLE_STATUSES = frozenset(
    {
        models.PendingStatus.AWAITING_ANSWER.value,
        models.PendingStatus.DEFERRED.value,
        models.PendingStatus.FAILED.value,
    }
)


def _safe_error(exc: BaseException) -> str:
    from agent.trace.redact import redact_text

    return redact_text(f"{type(exc).__name__}: {exc}")


def _trim_content(text: str) -> str:
    """数据层裁剪（先裁剪对象再 json.dumps，不截断序列化结果）。"""
    value = str(text or "")
    if len(value) <= MAX_KNOWLEDGE_CHARS:
        return value
    return value[:MAX_KNOWLEDGE_CHARS]


def _answer_limits() -> dict[str, int]:
    return {
        "max_knowledge_chars": MAX_KNOWLEDGE_CHARS,
        "max_knowledge_ids": MAINTENANCE_LIMITS["max_knowledge_ids"],
        "max_question_chars": MAINTENANCE_LIMITS["max_question_chars"],
    }


def _knowledge_payload(item: KnowledgeItem) -> dict[str, Any]:
    provenance = dict(item.provenance or {})
    return {
        "id": item.id,
        "version": int(item.version),
        "chain_id": item.chain_id,
        "content": _trim_content(item.content),
        "category": item.category,
        "node_ids": list(item.node_ids),
        "entity_ids": list(item.entity_ids),
        "epistemic_status": provenance.get("epistemic_status"),
        "source": provenance.get("source"),
        "state": item.state.value,
        "evidence_ids": list(provenance.get("evidence_ids") or []),
    }


def _refs(item: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in item.get("knowledge_ids") or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        out.append(
            {
                "id": str(entry["id"]),
                "version": int(entry.get("version") or 1),
                "chain_id": str(entry.get("chain_id") or ""),
            }
        )
    return out


def _conflicts(
    conn: sqlite3.Connection, refs: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """事项涉及的知识是否仍是回答所依据的当前有效版本（CAS 预检）。"""
    svc = KnowledgeService(conn)
    conflicts: list[dict[str, str]] = []
    for ref in refs:
        knowledge_id = ref["id"]
        row = svc.get(knowledge_id)
        if row is None:
            conflicts.append({"knowledge_id": knowledge_id, "reason": "对象缺失"})
            continue
        if row.state is not KnowledgeState.ACTIVE:
            conflicts.append(
                {
                    "knowledge_id": knowledge_id,
                    "reason": f"当前状态为 {row.state.value}，不是有效知识",
                }
            )
            continue
        current = resolve_current(conn, knowledge_id)
        if current is None or current.id != row.id:
            conflicts.append(
                {
                    "knowledge_id": knowledge_id,
                    "reason": "该条目已被后续修改取代（当前版本 "
                    + (current.id if current is not None else "无")
                    + "）",
                }
            )
            continue
        if int(row.version) != int(ref.get("version") or 1):
            conflicts.append(
                {
                    "knowledge_id": knowledge_id,
                    "reason": f"版本已变化：事项记录 v{ref.get('version')}，"
                    f"当前 v{int(row.version)}",
                }
            )
    return conflicts


def _record_conflict(
    conn: sqlite3.Connection,
    item: dict[str, Any],
    *,
    answer: str,
    conflicts: list[dict[str, str]],
) -> models.MaintenanceAnswerOutcome:
    reason = "版本冲突，未修改任何知识：" + "；".join(
        f"{entry['knowledge_id']}={entry['reason']}" for entry in conflicts[:5]
    )
    status = item["status"]
    if status not in (
        models.PendingStatus.AWAITING_ANSWER.value,
        models.PendingStatus.DEFERRED.value,
    ):
        status = models.PendingStatus.AWAITING_ANSWER.value
    try:
        store.update_item(
            conn,
            item["id"],
            status=status,
            error=reason,
            answer=answer,
        )
    except Exception:  # noqa: BLE001 - 记录失败不改变「没改知识」的事实
        logger.warning("回答冲突状态记录失败", exc_info=True)
    return models.MaintenanceAnswerOutcome(
        ok=False,
        status="conflict",
        item_id=item["id"],
        error=reason,
        detail={"conflicts": conflicts},
    )


async def _call_model(ctx: Any, data: dict[str, Any]) -> tuple[Any, list[str], dict[str, Any]]:
    """组装独立回答任务并调用模型（修复最多一次）。不注入任何主对话内容。"""
    from agent.maintenance.runner import run_structured_task

    adapter = await ctx.build_adapter()
    if adapter is None:
        raise RuntimeError("没有可用模型适配器")
    return await run_structured_task(
        adapter,
        prompt_key="MAINTENANCE_ANSWER_SYSTEM",
        schema=models.resolution_output_schema(),
        limits=_answer_limits(),
        data=data,
        validate=models.validate_resolution_output,
    )


def _item_data(item: dict[str, Any], knowledge: list[dict[str, Any]], answer: str) -> dict[str, Any]:
    evidence = [
        {"id": entry["id"], "kind": "knowledge", "label": entry["content"][:60]}
        for entry in knowledge
    ]
    return {
        "item": {
            "id": item["id"],
            "kind": item["kind"],
            "status": item["status"],
            "question": item["question"],
            "created_at": item["created_at"],
        },
        "knowledge": knowledge,
        "evidence": evidence,
        "answer": answer,
        "limits": _answer_limits(),
    }


def _apply_resolution(
    conn: sqlite3.Connection,
    item: dict[str, Any],
    output: models.ResolutionOutput,
    *,
    answer: str,
) -> models.MaintenanceAnswerOutcome:
    refs = _refs(item)
    allowed_ids = {ref["id"] for ref in refs}

    if output.resolution is models.ResolutionKind.NEEDS_CLARIFICATION:
        question = (output.question or "").strip()
        store.update_item(
            conn,
            item["id"],
            status=models.PendingStatus.AWAITING_ANSWER.value,
            question=question,
            answer=answer,
            error=None,
        )
        return models.MaintenanceAnswerOutcome(
            ok=True,
            status="needs_clarification",
            item_id=item["id"],
            resolution=output.resolution.value,
            detail={"question": question},
        )
    if output.resolution is models.ResolutionKind.DEFERRED:
        store.update_item(
            conn,
            item["id"],
            status=models.PendingStatus.DEFERRED.value,
            answer=answer,
            error=None,
        )
        return models.MaintenanceAnswerOutcome(
            ok=True,
            status="deferred",
            item_id=item["id"],
            resolution=output.resolution.value,
            detail={"kept_pending": True},
        )
    if output.resolution is models.ResolutionKind.NO_CHANGE:
        store.update_item(
            conn,
            item["id"],
            status=models.PendingStatus.PROCESSED.value,
            answer=answer,
            error=None,
        )
        return models.MaintenanceAnswerOutcome(
            ok=True,
            status="no_change",
            item_id=item["id"],
            resolution=output.resolution.value,
        )

    # resolved：先做完整 CAS 预检（任何一条冲突都不改），再在一个事务里应用。
    unknown = [
        change.knowledge_id
        for change in output.changes
        if change.knowledge_id not in allowed_ids
    ]
    if unknown:
        reason = "只允许修改该事项涉及的知识；越界 ID：" + ", ".join(unknown[:5])
        store.update_item(
            conn,
            item["id"],
            status=models.PendingStatus.AWAITING_ANSWER.value,
            error=reason,
        )
        return models.MaintenanceAnswerOutcome(
            ok=False,
            status="conflict",
            item_id=item["id"],
            resolution=output.resolution.value,
            error=reason,
        )
    duplicated = {
        change.knowledge_id
        for change in output.changes
        if sum(1 for other in output.changes if other.knowledge_id == change.knowledge_id) > 1
    }
    if duplicated:
        reason = "同一知识出现多条修改，拒绝执行：" + ", ".join(sorted(duplicated))
        store.update_item(
            conn,
            item["id"],
            status=models.PendingStatus.AWAITING_ANSWER.value,
            error=reason,
        )
        return models.MaintenanceAnswerOutcome(
            ok=False,
            status="conflict",
            item_id=item["id"],
            resolution=output.resolution.value,
            error=reason,
        )
    pre_conflicts = _conflicts(conn, refs)
    if pre_conflicts:
        return _record_conflict(conn, item, answer=answer, conflicts=pre_conflicts)

    try:
        with lifecycle.atomic(conn):
            live_conflicts = _conflicts(conn, refs)
            if live_conflicts:
                raise _Conflict(live_conflicts)
            svc = KnowledgeService(conn)
            before: list[dict[str, Any]] = []
            after: list[dict[str, Any]] = []
            applied = 0
            for change in output.changes:
                target = svc.get(change.knowledge_id)
                if target is None:
                    raise _Conflict(
                        [{"knowledge_id": change.knowledge_id, "reason": "对象缺失"}]
                    )
                snapshot = store.knowledge_snapshot(target)
                if change.action is models.ResolutionAction.REVISE:
                    revised = revise_atomic(
                        conn,
                        target.id,
                        expected_version=int(target.version),
                        fields={
                            "content": str(change.new_content or ""),
                            "provenance": {
                                "answer_item_id": item["id"],
                            },
                        },
                        source=SOURCE_USER_CORRECTION,
                        actor="user",
                    )
                    result_snapshot = store.knowledge_snapshot(revised)
                    result_id: str | None = revised.id
                    operation_action = "revise"
                else:
                    deactivate_atomic(
                        conn, target.id, expected_version=int(target.version)
                    )
                    result_snapshot = None
                    result_id = None
                    operation_action = "revoke"
                before.append(snapshot)
                if result_snapshot is not None:
                    after.append(result_snapshot)
                store.create_operation(
                    conn,
                    action=operation_action,
                    batch_id=None,
                    item_id=item["id"],
                    target_ids=[target.id],
                    result_id=result_id,
                    before=[snapshot],
                    after=result_snapshot or {"state": "revoked"},
                    status=models.MaintenanceCandidateStatus.APPLIED.value,
                )
                applied += 1
            store.update_item(
                conn,
                item["id"],
                status=models.PendingStatus.PROCESSED.value,
                answer=answer,
                before=before,
                after=after,
                error=None,
            )
        return models.MaintenanceAnswerOutcome(
            ok=True,
            status="processed",
            item_id=item["id"],
            resolution=output.resolution.value,
            applied_changes=applied,
        )
    except _Conflict as exc:
        return _record_conflict(conn, item, answer=answer, conflicts=exc.conflicts)
    except (VersionConflict, KnowledgeNotFound, ValueError) as exc:
        reason = _safe_error(exc)
        store.update_item(
            conn,
            item["id"],
            status=models.PendingStatus.AWAITING_ANSWER.value,
            error=reason,
        )
        return models.MaintenanceAnswerOutcome(
            ok=False,
            status="conflict",
            item_id=item["id"],
            resolution=output.resolution.value,
            error=reason,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("独立回答执行失败", exc_info=True)
        reason = _safe_error(exc)
        try:
            store.update_item(
                conn,
                item["id"],
                status=models.PendingStatus.FAILED.value,
                error=reason,
            )
        except Exception:  # noqa: BLE001
            logger.warning("回答失败状态记录失败", exc_info=True)
        return models.MaintenanceAnswerOutcome(
            ok=False,
            status=models.PendingStatus.FAILED.value,
            item_id=item["id"],
            error=reason,
        )


class _Conflict(Exception):
    def __init__(self, conflicts: list[dict[str, str]]) -> None:
        self.conflicts = conflicts
        super().__init__("版本冲突")


async def answer_question(
    ctx: Any, item_id: str, answer: str
) -> models.MaintenanceAnswerOutcome:
    """处理一条后台维护问题的用户回答（不写主对话、不新增用户轮次）。"""
    conn: sqlite3.Connection = ctx.conn
    text = str(answer or "").strip()
    item = store.get_item(conn, item_id)
    if item is None:
        return models.MaintenanceAnswerOutcome(
            ok=False, status="not_found", item_id=item_id, error="待回答事项不存在"
        )
    if item["kind"] != store.ITEM_ASK:
        return models.MaintenanceAnswerOutcome(
            ok=False,
            status="invalid_item",
            item_id=item_id,
            error="该事项不是后台维护问题",
        )
    if item["status"] not in _ANSWERABLE_STATUSES:
        return models.MaintenanceAnswerOutcome(
            ok=False,
            status="already_processed",
            item_id=item_id,
            error=f"事项状态为 {item['status']}，不再接受回答",
        )
    if not text:
        return models.MaintenanceAnswerOutcome(
            ok=False, status="invalid_answer", item_id=item_id, error="回答不能为空"
        )
    refs = _refs(item)
    pre_conflicts = _conflicts(conn, refs)
    if pre_conflicts:
        return _record_conflict(conn, item, answer=text, conflicts=pre_conflicts)

    svc = KnowledgeService(conn)
    knowledge: list[dict[str, Any]] = []
    for ref in refs:
        row = svc.get(ref["id"])
        if row is None:  # 已由 _conflicts 覆盖；这里防御
            continue
        knowledge.append(_knowledge_payload(row))
    data = _item_data(item, knowledge, text)
    try:
        output, errors, meta = await _call_model(ctx, data)
    except Exception as exc:  # noqa: BLE001 - 模型 / 基础设施失败保留事项与知识
        logger.warning("独立回答模型调用失败", exc_info=True)
        reason = _safe_error(exc)
        try:
            store.update_item(
                conn,
                item["id"],
                status=models.PendingStatus.FAILED.value,
                error=reason,
            )
        except Exception:  # noqa: BLE001
            logger.warning("回答失败状态记录失败", exc_info=True)
        return models.MaintenanceAnswerOutcome(
            ok=False,
            status=models.PendingStatus.FAILED.value,
            item_id=item_id,
            error=reason,
            detail={"attempts": 0},
        )
    if output is None:
        reason = "回答输出校验失败（已尝试一次修复）：" + "；".join(errors[:5])
        try:
            store.update_item(
                conn,
                item["id"],
                status=models.PendingStatus.FAILED.value,
                error=reason[:1000],
            )
        except Exception:  # noqa: BLE001
            logger.warning("回答校验失败状态记录失败", exc_info=True)
        return models.MaintenanceAnswerOutcome(
            ok=False,
            status="failed",
            item_id=item_id,
            error=reason[:1000],
            detail={"attempts": meta.get("attempts", 0), "field_errors": errors[:10]},
        )
    # 重新读一次事项：模型调用期间可能的并发更新以最新行为准。
    latest = store.get_item(conn, item_id) or item
    return _apply_resolution(conn, latest, output, answer=text)
