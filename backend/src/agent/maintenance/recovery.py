"""安全恢复（B）：维护影响的预览与撤回（契约 §12.4 / §12.5）。

原则：

* 只通过领域操作与**新版本记录**恢复：不删除、不覆盖历史，也不覆盖用户后续修改；
* 维护产物已不是当前链版本（用户后来改过）→ 该项拒绝恢复，结果里明确列出；
* 多来源合并逐来源恢复 / 说明，不拿一个 knowledge_id 冒充完整合并；
* preview 不写库；restore 逐项恢复并如实汇总 rolled_back / rollback_partial；
* 恢复产生的版本 provenance 含 source=maintenance_restore / restore_of / batch_id。
"""

from __future__ import annotations

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
)
from agent.maintenance import models, store

logger = logging.getLogger(__name__)

_PLAN_RESTOREABLE = "restoreable"
_PLAN_ALREADY = "already_restored"
_PLAN_BLOCKED = "blocked"


def _safe_error(exc: BaseException) -> str:
    from agent.trace.redact import redact_text

    return redact_text(f"{type(exc).__name__}: {exc}")


def _product_state(conn: sqlite3.Connection, product_id: str | None) -> dict[str, Any]:
    """维护产物（维护产生的当前版本行）现在是否仍是该链的当前 active 版本。"""
    if not product_id:
        return {"id": None, "state": "missing", "reason": "操作没有结果版本 id"}
    svc = KnowledgeService(conn)
    row = svc.get(product_id)
    if row is None:
        return {"id": product_id, "state": "missing", "reason": "维护产物已不存在"}
    if row.state is not KnowledgeState.ACTIVE:
        return {
            "id": product_id,
            "state": "modified",
            "reason": f"维护产物状态为 {row.state.value}，已被后续操作改变",
        }
    current = resolve_current(conn, product_id)
    if current is None or current.id != row.id:
        return {
            "id": product_id,
            "state": "modified",
            "reason": "维护产物已不是该链的当前版本（可能被用户后来修改）",
        }
    return {"id": product_id, "state": "current", "reason": ""}


def _source_state(
    conn: sqlite3.Connection, snapshot: dict[str, Any]
) -> dict[str, Any]:
    """合并来源可否恢复：来源链上不能已有用户后续版本（幂等识别已恢复）。"""
    knowledge_id = str(snapshot.get("id") or "")
    target = {
        "knowledge_id": knowledge_id,
        "before": snapshot,
        "status": _PLAN_BLOCKED,
        "reasons": [],
    }
    if not knowledge_id:
        target["reasons"].append("来源快照缺少 id")
        return target
    svc = KnowledgeService(conn)
    row = svc.get(knowledge_id)
    if row is None:
        target["reasons"].append("来源对象缺失")
        target["status"] = _PLAN_BLOCKED
        return target
    expected_version = int(snapshot.get("version") or row.version)
    if row.state is KnowledgeState.ACTIVE:
        target["reasons"].append("来源仍是有效知识，无法判断合并关系")
        return target
    current = resolve_current(conn, knowledge_id)
    if current is not None and int(current.version) > expected_version:
        provenance = current.provenance or {}
        if (
            current.state is KnowledgeState.ACTIVE
            and str(provenance.get("restore_of") or "") == knowledge_id
        ):
            target["status"] = _PLAN_ALREADY
            target["reasons"].append("该来源已恢复")
            return target
        target["reasons"].append("来源链已有后续版本（用户后来改过），拒绝覆盖")
        return target
    target["status"] = _PLAN_RESTOREABLE
    return target


def _already_restored_normalize(
    conn: sqlite3.Connection, target_snapshot: dict[str, Any]
) -> bool:
    reference_id = str(target_snapshot.get("id") or "")
    if not reference_id:
        return False
    current = resolve_current(conn, reference_id)
    if current is None:
        return False
    provenance = current.provenance or {}
    return (
        current.state is KnowledgeState.ACTIVE
        and str(provenance.get("restore_of") or "") == reference_id
    )


def _plan_operation(conn: sqlite3.Connection, operation: dict[str, Any]) -> dict[str, Any]:
    action = str(operation.get("action") or "")
    plan: dict[str, Any] = {
        "operation_id": operation["id"],
        "batch_id": operation.get("batch_id"),
        "item_id": operation.get("item_id"),
        "action": action,
        "targets": [],
        "product": {"id": operation.get("result_id"), "state": "unknown", "reason": ""},
        "restoreable": False,
        "partial": False,
        "already_restored": False,
        "reasons": [],
    }
    product = _product_state(conn, operation.get("result_id"))
    plan["product"] = product
    targets = list(operation.get("before") or [])
    if action == "merge":
        planned = [_source_state(conn, snapshot) for snapshot in targets]
        plan["targets"] = planned
        plan["restoreable"] = product["state"] == "current" and all(
            entry["status"] in (_PLAN_RESTOREABLE, _PLAN_ALREADY) for entry in planned
        )
        plan["partial"] = (
            product["state"] == "current"
            and any(entry["status"] == _PLAN_RESTOREABLE for entry in planned)
            and any(entry["status"] == _PLAN_BLOCKED for entry in planned)
        )
        plan["already_restored"] = product["state"] != "current" and bool(planned) and all(
            entry["status"] == _PLAN_ALREADY for entry in planned
        )
        if product["state"] != "current":
            plan["reasons"].append(product["reason"])
        for entry in planned:
            if entry["status"] == _PLAN_BLOCKED:
                plan["reasons"].extend(
                    f"{entry['knowledge_id']}：{reason}" for reason in entry["reasons"]
                )
        return plan
    if action == "normalize":
        if not targets:
            plan["reasons"].append("操作缺少恢复前快照")
            return plan
        snapshot = targets[0]
        target = {
            "knowledge_id": str(snapshot.get("id") or ""),
            "before": snapshot,
            "status": _PLAN_BLOCKED,
            "reasons": [],
        }
        svc = KnowledgeService(conn)
        if target["knowledge_id"] and svc.get(target["knowledge_id"]) is None:
            target["reasons"].append("恢复前对象缺失")
        if product["state"] == "current" and not target["reasons"]:
            target["status"] = _PLAN_RESTOREABLE
            plan["restoreable"] = True
        elif _already_restored_normalize(conn, snapshot):
            target["status"] = _PLAN_ALREADY
            target["reasons"].append("该批次已恢复")
            plan["already_restored"] = True
        else:
            target["reasons"].append(product["reason"] or "维护产物已被后续修改")
        plan["targets"] = [target]
        if not plan["restoreable"]:
            plan["reasons"].extend(
                f"{target['knowledge_id']}：{reason}" for reason in target["reasons"]
            )
        return plan
    plan["reasons"].append(f"不支持的维护操作动作：{action}")
    return plan


def _resolve_operations(
    conn: sqlite3.Connection,
    *,
    batch_id: str | None,
    item_id: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if item_id is not None:
        item = store.get_item(conn, item_id)
        if item is None:
            return [], {"error": "维护事项不存在", "status": "not_found"}
        if item["kind"] not in (store.ITEM_MERGE, store.ITEM_NORMALIZE):
            return [], {
                "error": "该事项不是可恢复的维护动作（merge / normalize）",
                "status": "invalid_item",
            }
        operations = store.list_operations_for_item(conn, item_id)
        return operations, None
    if batch_id is not None:
        batch = store.get_batch(conn, batch_id)
        if batch is None:
            return [], {"error": "维护批次不存在", "status": "not_found"}
        return store.list_restorable_operations(conn, batch_id=batch_id), None
    return [], {
        "error": "必须提供 batch_id 或 item_id",
        "status": "invalid_request",
    }


def preview_restore(
    conn: sqlite3.Connection,
    *,
    batch_id: str | None = None,
    item_id: str | None = None,
) -> dict[str, Any]:
    """逐项判断可恢复性；不写库。"""
    operations, error = _resolve_operations(conn, batch_id=batch_id, item_id=item_id)
    if error is not None:
        return {"ok": False, **error, "operations": []}
    plans = [_plan_operation(conn, operation) for operation in operations]
    restoreable = [plan for plan in plans if plan["restoreable"]]
    partial = [plan for plan in plans if plan["partial"]]
    already = [plan for plan in plans if plan["already_restored"]]
    blocked = [plan for plan in plans if not (plan["restoreable"] or plan["already_restored"])]
    impact = {
        "operations": len(plans),
        "restoreable_operations": len(restoreable),
        "partial_operations": len(partial),
        "blocked_operations": len(blocked),
        "already_restored_operations": len(already),
        "creates_versions": sum(
            len([t for t in plan["targets"] if t["status"] == _PLAN_RESTOREABLE])
            for plan in plans
        ),
        "revokes_versions": len(restoreable),
        "knowledge_ids": sorted(
            {
                str(target["knowledge_id"])
                for plan in plans
                for target in plan["targets"]
                if target["status"] == _PLAN_RESTOREABLE
            }
        ),
    }
    if batch_id is not None:
        batch = store.get_batch(conn, batch_id)
        impact["batch_status"] = batch["status"] if batch else None
        impact["batch_id"] = batch_id
    if item_id is not None:
        impact["item_id"] = item_id
    return {
        "ok": True,
        "status": "preview",
        "batch_id": batch_id,
        "item_id": item_id,
        "operations": plans,
        "restoreable": bool(restoreable) and not partial and not blocked,
        "restoreable_count": len(restoreable),
        "partial_count": len(partial),
        "blocked_count": len(blocked),
        "already_restored_count": len(already),
        "impact": impact,
        "warnings": [
            f"{plan['operation_id']}：{reason}"
            for plan in plans
            for reason in plan["reasons"]
        ],
    }


# ---------------------------------------------------------------------------
# 恢复执行
# ---------------------------------------------------------------------------


def _restore_provenance(
    snapshot: dict[str, Any], *, batch_id: str | None, operation: str
) -> dict[str, Any]:
    provenance = dict(snapshot.get("provenance") or {})
    provenance.pop("superseded_by", None)
    provenance.pop("superseded_at", None)
    provenance.update(
        {
            "source": models.SOURCE_MAINTENANCE_RESTORE,
            "operation": "restore",
            "restore_of": snapshot.get("id"),
            "restore_of_version": int(snapshot.get("version") or 1),
            "restore_operation": operation,
            "batch_id": batch_id,
        }
    )
    provenance = {key: value for key, value in provenance.items() if value is not None}
    return provenance


def _record_restore_item(
    conn: sqlite3.Connection,
    *,
    operation: dict[str, Any],
    status: str,
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    reason: str = "",
    error: str | None = None,
    evidence_ids: Any = None,
) -> tuple[str | None, str | None]:
    try:
        item_id = store.create_item(
            conn,
            batch_id=operation.get("batch_id"),
            kind=store.ITEM_RESTORE,
            status=status,
            knowledge_ids=[
                {"id": snapshot.get("id"), "version": snapshot.get("version")}
                for snapshot in before
            ],
            before=before,
            after=after,
            reason=reason or None,
            error=error,
            evidence_ids=list(evidence_ids or []),
        )
        operation_id = store.create_operation(
            conn,
            action="restore",
            batch_id=operation.get("batch_id"),
            item_id=item_id,
            target_ids=[
                str(snapshot.get("id")) for snapshot in before if snapshot.get("id")
            ],
            result_id=None,
            before=before,
            after={
                "restored": [
                    {
                        "id": snapshot.get("id"),
                        "version": snapshot.get("version"),
                    }
                    for snapshot in after
                ],
                "source_operation_id": operation["id"],
            },
            status=status,
        )
        return item_id, operation_id
    except Exception as exc:  # noqa: BLE001 - 记录失败不回滚已完成的领域写入
        logger.warning("恢复记录写入失败", exc_info=True)
        return None, None


def _restore_merge(
    conn: sqlite3.Connection, operation: dict[str, Any], plan: dict[str, Any]
) -> dict[str, Any]:
    product = plan["product"]
    if plan.get("already_restored"):
        return {
            "operation_id": operation["id"],
            "status": "already_restored",
            "restored_knowledge_ids": [],
            "blocked_knowledge_ids": [],
            "reasons": ["该批次已经恢复过"],
            "item_id": None,
        }
    if product["state"] != "current":
        reason = product["reason"] or "维护产物已被后续修改，拒绝恢复"
        item_id, _ = _record_restore_item(
            conn,
            operation=operation,
            status=models.MaintenanceCandidateStatus.REJECTED.value,
            before=[],
            after=[],
            reason=reason,
            error=reason,
        )
        return {
            "operation_id": operation["id"],
            "status": "blocked",
            "restored_knowledge_ids": [],
            "blocked_knowledge_ids": [
                str(snapshot.get("id")) for snapshot in operation.get("before") or []
            ],
            "reasons": [reason],
            "item_id": item_id,
        }
    before = list(operation.get("before") or [])
    restored: list[dict[str, Any]] = []
    blocked: list[str] = []
    reasons: list[str] = []
    for snapshot in before:
        target = _source_state(conn, snapshot)
        if target["status"] == _PLAN_ALREADY:
            restored.append(
                {"id": snapshot.get("id"), "version": snapshot.get("version"), "already": True}
            )
            continue
        if target["status"] != _PLAN_RESTOREABLE:
            blocked.append(str(snapshot.get("id")))
            reasons.extend(
                f"{snapshot.get('id')}：{reason}" for reason in target["reasons"]
            )
            continue
        provenance = _restore_provenance(
            snapshot, batch_id=operation.get("batch_id"), operation="merge"
        )
        created = lifecycle.restore_version(
            conn,
            reference_id=str(snapshot.get("id")),
            content=str(snapshot.get("content") or ""),
            provenance=provenance,
        )
        restored.append(
            {
                "id": created.id,
                "restore_of": snapshot.get("id"),
                "version": int(created.version),
            }
        )
    if not restored:
        reason = "没有可安全恢复的来源：" + "；".join(reasons[:5])
        item_id, _ = _record_restore_item(
            conn,
            operation=operation,
            status=models.MaintenanceCandidateStatus.REJECTED.value,
            before=before,
            after=[],
            reason=reason,
            error=reason,
        )
        return {
            "operation_id": operation["id"],
            "status": "blocked",
            "restored_knowledge_ids": [],
            "blocked_knowledge_ids": blocked,
            "reasons": reasons or [reason],
            "item_id": item_id,
        }
    if blocked:
        # 部分恢复：保留维护产物（不能只恢复一个来源就撤掉合并结果，那会丢信息）。
        status = models.BatchStatus.ROLLBACK_PARTIAL.value
        reason = "部分来源无法安全恢复：" + "；".join(reasons[:5])
        item_id, _ = _record_restore_item(
            conn,
            operation=operation,
            status=status,
            before=before,
            after=restored,
            reason=reason,
        )
        return {
            "operation_id": operation["id"],
            "status": "partial",
            "restored_knowledge_ids": [entry["id"] for entry in restored],
            "blocked_knowledge_ids": blocked,
            "reasons": reasons,
            "item_id": item_id,
            "product_kept": product["id"],
        }
    # 全部来源恢复成功：撤销维护产物（保留历史行）。
    deactivate_atomic(conn, str(product["id"]))
    item_id, _ = _record_restore_item(
        conn,
        operation=operation,
        status=models.MaintenanceCandidateStatus.APPLIED.value,
        before=before,
        after=restored,
        reason="多来源合并已逐来源恢复，维护产物已撤销",
    )
    return {
        "operation_id": operation["id"],
        "status": "restored",
        "restored_knowledge_ids": [entry["id"] for entry in restored],
        "blocked_knowledge_ids": [],
        "reasons": [],
        "item_id": item_id,
        "revoked_product_id": product["id"],
    }


def _restore_normalize(
    conn: sqlite3.Connection, operation: dict[str, Any], plan: dict[str, Any]
) -> dict[str, Any]:
    product = plan["product"]
    before = list(operation.get("before") or [])
    if product["state"] != "current":
        if plan["already_restored"]:
            return {
                "operation_id": operation["id"],
                "status": "already_restored",
                "restored_knowledge_ids": [],
                "blocked_knowledge_ids": [],
                "reasons": ["该批次已经恢复过"],
                "item_id": None,
            }
        reason = product["reason"] or "维护产物已被后续修改，拒绝恢复"
        item_id, _ = _record_restore_item(
            conn,
            operation=operation,
            status=models.MaintenanceCandidateStatus.REJECTED.value,
            before=before,
            after=[],
            reason=reason,
            error=reason,
        )
        return {
            "operation_id": operation["id"],
            "status": "blocked",
            "restored_knowledge_ids": [],
            "blocked_knowledge_ids": [str(s.get("id")) for s in before],
            "reasons": [reason],
            "item_id": item_id,
        }
    snapshot = before[0]
    provenance = _restore_provenance(
        snapshot, batch_id=operation.get("batch_id"), operation="normalize"
    )
    created = lifecycle.restore_version(
        conn,
        reference_id=str(snapshot.get("id")),
        content=str(snapshot.get("content") or ""),
        provenance=provenance,
    )
    item_id, _ = _record_restore_item(
        conn,
        operation=operation,
        status=models.MaintenanceCandidateStatus.APPLIED.value,
        before=[store.knowledge_snapshot(KnowledgeService(conn).get(str(product["id"])))] if product.get("id") else before,
        after=[store.knowledge_snapshot(created)],
        reason="normalize 产物已撤销，原内容以新版本恢复",
    )
    return {
        "operation_id": operation["id"],
        "status": "restored",
        "restored_knowledge_ids": [created.id],
        "blocked_knowledge_ids": [],
        "reasons": [],
        "item_id": item_id,
        "revoked_product_id": product["id"],
        "restore_of": snapshot.get("id"),
    }


def restore(
    conn: sqlite3.Connection,
    *,
    batch_id: str | None = None,
    item_id: str | None = None,
) -> dict[str, Any]:
    """逐条或整批恢复；只通过新版本记录，不删除历史、不覆盖用户后续修改。"""
    operations, error = _resolve_operations(conn, batch_id=batch_id, item_id=item_id)
    if error is not None:
        return {"ok": False, **error, "operations": []}
    if not operations:
        return {
            "ok": True,
            "status": "nothing_to_restore",
            "batch_id": batch_id,
            "item_id": item_id,
            "operations": [],
            "restored": 0,
            "blocked": 0,
        }
    if batch_id is not None:
        batch = store.get_batch(conn, batch_id)
        if batch is not None and batch["status"] in (
            models.BatchStatus.ROLLED_BACK.value,
        ):
            return {
                "ok": True,
                "status": "already_rolled_back",
                "batch_id": batch_id,
                "item_id": item_id,
                "operations": [],
                "restored": 0,
                "blocked": 0,
            }
    results: list[dict[str, Any]] = []
    for operation in operations:
        try:
            with lifecycle.atomic(conn):
                plan = _plan_operation(conn, operation)
                if operation["action"] == "merge":
                    result = _restore_merge(conn, operation, plan)
                elif operation["action"] == "normalize":
                    result = _restore_normalize(conn, operation, plan)
                else:
                    result = {
                        "operation_id": operation["id"],
                        "status": "blocked",
                        "restored_knowledge_ids": [],
                        "blocked_knowledge_ids": [],
                        "reasons": [f"不支持的维护操作动作：{operation['action']}"],
                    }
                results.append(result)
        except (VersionConflict, KnowledgeNotFound, ValueError) as exc:
            reason = _safe_error(exc)
            results.append(
                {
                    "operation_id": operation["id"],
                    "status": "blocked",
                    "restored_knowledge_ids": [],
                    "blocked_knowledge_ids": [],
                    "reasons": [reason],
                }
            )
        except Exception as exc:  # noqa: BLE001 - 单项失败不阻断其它操作
            logger.warning("恢复操作失败", exc_info=True)
            results.append(
                {
                    "operation_id": operation["id"],
                    "status": "failed",
                    "restored_knowledge_ids": [],
                    "blocked_knowledge_ids": [],
                    "reasons": [_safe_error(exc)],
                }
            )
    restored = [
        result
        for result in results
        if result["status"] in ("restored", "already_restored")
    ]
    blocked = [result for result in results if result["status"] in ("blocked", "partial")]
    all_restored = len(restored) == len(results)
    all_already = bool(results) and all(
        result["status"] == "already_restored" for result in results
    )
    status = "rolled_back" if all_restored else "rollback_partial"
    if item_id is not None:
        if all_already:
            status = "already_restored"
        elif all_restored:
            status = "restored"
        else:
            status = "partial"
    if batch_id is not None:
        try:
            with lifecycle.atomic(conn):
                row = store.get_batch(conn, batch_id)
                stats = dict((row or {}).get("stats") or {})
                stats["rollback"] = {
                    "restored_operations": len(restored),
                    "blocked_operations": len(blocked),
                    "results": [
                        {
                            "operation_id": result["operation_id"],
                            "status": result["status"],
                        }
                        for result in results
                    ],
                }
                store.finish_batch(conn, batch_id, status=status, stats=stats)
        except Exception:  # noqa: BLE001 - 回滚状态记录失败不掩盖结果
            logger.warning("批次回滚状态记录失败", exc_info=True)
    return {
        "ok": True,
        "status": status,
        "batch_id": batch_id,
        "item_id": item_id,
        "operations": results,
        "restored": len(restored),
        "blocked": len(blocked),
        "blocked_reasons": [
            {"operation_id": result["operation_id"], "reasons": result["reasons"]}
            for result in results
            if result["status"] in ("blocked", "partial")
        ],
    }
