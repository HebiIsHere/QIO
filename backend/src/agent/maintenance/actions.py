"""维护动作执行（B）：merge / normalize / ask 的领域校验与事务执行。

契约 §12.4：

* merge：至少两个不同 ID、非空 new_content、无 question；来源必须存在且版本匹配；
  类别 / 归属节点 / 确定程度必须一致；新版本 provenance 保存 merged_from / evidence_ids /
  batch_id / source=maintenance_merge；来源行撤销但保留完整历史；
* normalize：恰好一个 ID、非空 new_content、无 question；只改格式；
  provenance 含 operation=normalize / batch_id / source=maintenance_normalize；
* ask：question 非空、不改知识；待回答事项持久保存；同一知识集合 + 同一问题不重复催问；
* 非法 / 未知 ID / 版本不一致 → 该项 rejected 并写原因，原知识保持不动；
  绝不回落为旧 revoke / expire / 否定词降置信度分支。

本模块不调用模型（模型调用与输出校验在 runner），只接收已通过 pydantic 结构校验的
候选，并做领域校验 + 事务写入 + 记录（事项 / 操作）。每条候选一个事务。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Mapping

from agent.knowledge import lifecycle
from agent.knowledge.lifecycle import (
    KnowledgeItem,
    KnowledgeNotFound,
    KnowledgeService,
    KnowledgeState,
    VersionConflict,
    resolve_current,
    revise_atomic,
)
from agent.maintenance import models, store

logger = logging.getLogger(__name__)


class _CandidateRejected(Exception):
    """领域规则拒绝这一条候选（只影响该项，不中断整批）。"""


@dataclass(frozen=True)
class CandidateOutcome:
    """一条候选的执行结果（runner 汇总成批次统计）。"""

    status: str
    item_id: str | None = None
    operation_id: str | None = None
    result_id: str | None = None
    reason: str = ""
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def applied(self) -> bool:
        return self.status == models.MaintenanceCandidateStatus.APPLIED.value

    @property
    def asked(self) -> bool:
        return self.status == models.MaintenanceCandidateStatus.ASKED.value

    @property
    def rejected(self) -> bool:
        return self.status == models.MaintenanceCandidateStatus.REJECTED.value

    @property
    def failed(self) -> bool:
        return self.status == models.MaintenanceCandidateStatus.FAILED.value


def _safe_error(exc: BaseException) -> str:
    from agent.trace.redact import redact_text

    return redact_text(f"{type(exc).__name__}: {exc}")


def _normalized_ids(values: Any) -> list[str]:
    out: list[str] = []
    for value in values or []:
        text = str(value).strip()
        if text and text not in out:
            out.append(text)
    return out


def _allowed_versions(allowed_knowledge: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key, value in (allowed_knowledge or {}).items():
        entry = dict(value)
        entry.setdefault("id", str(key))
        entry["version"] = int(entry.get("version") or 1)
        entry["chain_id"] = str(entry.get("chain_id") or "")
        out[str(key)] = entry
    return out


def _version_error(
    conn: sqlite3.Connection, item: KnowledgeItem, allowed: Mapping[str, Any]
) -> str | None:
    """写前版本核对：DB 当前行必须与模型输入里看到的那一版一致（CAS 语义）。"""
    expected_version = int(allowed.get("version") or 1)
    expected_chain = str(allowed.get("chain_id") or "")
    if int(item.version) != expected_version:
        return (
            f"版本不一致：输入是 v{expected_version}，当前是 v{int(item.version)}；"
            "原知识保持不动"
        )
    if expected_chain and item.chain_id and str(item.chain_id) != expected_chain:
        return "版本链已变化，原知识保持不动"
    current = resolve_current(conn, item.id)
    if current is None or current.id != item.id:
        return "目标已不是该链当前版本（已被后续修改或取代），原知识保持不动"
    if item.state is not KnowledgeState.ACTIVE:
        return f"目标状态为 {item.state.value}，不是当前有效知识；原知识保持不动"
    return None


def _knowledge_refs(items: list[KnowledgeItem]) -> list[dict[str, Any]]:
    return [
        {
            "id": item.id,
            "version": int(item.version),
            "chain_id": item.chain_id,
        }
        for item in items
    ]


def _evidence_error(
    candidate: models.MaintenanceCandidate, allowed_evidence: set[str]
) -> str | None:
    missing = [
        str(value)
        for value in candidate.evidence_ids or []
        if str(value) not in allowed_evidence
    ]
    if missing:
        return "evidence_ids 不在本轮输入提供的来源里：" + ", ".join(missing[:5])
    return None


def _record_item(
    conn: sqlite3.Connection,
    *,
    batch_id: str | None,
    kind: str,
    status: str,
    candidate: models.MaintenanceCandidate,
    knowledge_ids: list[dict[str, Any]] | None = None,
    before: list[dict[str, Any]] | None = None,
    after: list[dict[str, Any]] | None = None,
    reason: str = "",
    error: str | None = None,
) -> str:
    return store.create_item(
        conn,
        batch_id=batch_id,
        kind=kind,
        status=status,
        knowledge_ids=knowledge_ids or [],
        before=before or [],
        after=after or [],
        evidence_ids=list(candidate.evidence_ids or []),
        reason=reason or candidate.reason or None,
        error=error,
    )


def _record_rejected(
    conn: sqlite3.Connection,
    *,
    batch_id: str | None,
    kind: str,
    candidate: models.MaintenanceCandidate,
    reason: str,
    knowledge_ids: list[dict[str, Any]] | None = None,
    before: list[dict[str, Any]] | None = None,
) -> CandidateOutcome:
    item_id: str | None = None
    try:
        item_id = _record_item(
            conn,
            batch_id=batch_id,
            kind=kind,
            status=models.MaintenanceCandidateStatus.REJECTED.value,
            candidate=candidate,
            knowledge_ids=knowledge_ids,
            before=before,
            reason=reason,
            error=reason,
        )
    except Exception:  # noqa: BLE001 - 记录失败不能反过来破坏原知识
        logger.warning("维护候选拒绝记录失败", exc_info=True)
    return CandidateOutcome(
        status=models.MaintenanceCandidateStatus.REJECTED.value,
        item_id=item_id,
        reason=reason,
        error=reason,
    )


def _record_failed(
    conn: sqlite3.Connection,
    *,
    batch_id: str | None,
    kind: str,
    candidate: models.MaintenanceCandidate,
    error: str,
    knowledge_ids: list[dict[str, Any]] | None = None,
    before: list[dict[str, Any]] | None = None,
) -> CandidateOutcome:
    item_id: str | None = None
    try:
        item_id = _record_item(
            conn,
            batch_id=batch_id,
            kind=kind,
            status=models.MaintenanceCandidateStatus.FAILED.value,
            candidate=candidate,
            knowledge_ids=knowledge_ids,
            before=before,
            error=error,
            reason="执行失败，原知识保持不动",
        )
    except Exception:  # noqa: BLE001
        logger.warning("维护候选失败记录失败", exc_info=True)
    return CandidateOutcome(
        status=models.MaintenanceCandidateStatus.FAILED.value,
        item_id=item_id,
        error=error,
    )


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------


def execute_candidate(
    conn: sqlite3.Connection,
    candidate: models.MaintenanceCandidate,
    *,
    batch_id: str | None,
    allowed_knowledge: Mapping[str, Mapping[str, Any]],
    allowed_evidence: set[str],
) -> CandidateOutcome:
    """执行一条已通过结构校验的维护候选。"""
    allowed = _allowed_versions(allowed_knowledge)
    evidence_error = _evidence_error(candidate, allowed_evidence)
    if evidence_error is not None:
        return _record_rejected(
            conn,
            batch_id=batch_id,
            kind=str(candidate.action.value),
            candidate=candidate,
            reason=evidence_error,
        )
    if candidate.action is models.MaintenanceAction.MERGE:
        return _execute_merge(
            conn, candidate, batch_id=batch_id, allowed=allowed
        )
    if candidate.action is models.MaintenanceAction.NORMALIZE:
        return _execute_normalize(
            conn, candidate, batch_id=batch_id, allowed=allowed
        )
    return _execute_ask(conn, candidate, batch_id=batch_id, allowed=allowed)


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


def _merge_precheck(
    candidate: models.MaintenanceCandidate,
    allowed: Mapping[str, Mapping[str, Any]],
) -> tuple[list[str] | None, str | None]:
    ids = _normalized_ids(candidate.knowledge_ids)
    if len(ids) < 2:
        return None, "merge 至少需要两个不同的 knowledge_id"
    unknown = [knowledge_id for knowledge_id in ids if knowledge_id not in allowed]
    if unknown:
        return None, "knowledge_ids 不在本轮输入里：" + ", ".join(unknown[:5])
    content = (candidate.new_content or "").strip()
    if not content:
        return None, "merge 的 new_content 不能为空"
    if (candidate.question or "").strip():
        return None, "merge 不得提供 question"
    return ids, None


def _execute_merge(
    conn: sqlite3.Connection,
    candidate: models.MaintenanceCandidate,
    *,
    batch_id: str | None,
    allowed: Mapping[str, Mapping[str, Any]],
) -> CandidateOutcome:
    ids, error = _merge_precheck(candidate, allowed)
    if error is not None or ids is None:
        return _record_rejected(
            conn,
            batch_id=batch_id,
            kind=store.ITEM_MERGE,
            candidate=candidate,
            reason=error or "merge 候选不合法",
        )
    kind = store.ITEM_MERGE
    try:
        with lifecycle.atomic(conn):
            svc = KnowledgeService(conn)
            items: list[KnowledgeItem] = []
            for knowledge_id in ids:
                item = svc.get(knowledge_id)
                if item is None:
                    raise _CandidateRejected(f"knowledge_id 不存在：{knowledge_id}")
                version_error = _version_error(conn, item, allowed[knowledge_id])
                if version_error is not None:
                    raise _CandidateRejected(version_error)
                items.append(item)
            if len({item.category for item in items}) != 1:
                raise _CandidateRejected(
                    "来源类别不一致：不得把不同类别的知识合成更强更宽的认识"
                )
            if len(
                {tuple(sorted(str(node) for node in item.node_ids)) for item in items}
            ) != 1:
                raise _CandidateRejected(
                    "来源归属范围不一致：不得把不同适用情境合成更强更宽的认识"
                )
            if len(
                {
                    str((item.provenance or {}).get("epistemic_status") or "")
                    for item in items
                }
            ) != 1:
                raise _CandidateRejected(
                    "来源确定程度不一致：不得把不同确定程度合成更强更宽的认识"
                )
            before = [store.knowledge_snapshot(item) for item in items]
            provenance = {
                "source": models.SOURCE_MAINTENANCE_MERGE,
                "operation": "merge",
                "batch_id": batch_id,
                "merged_from": _knowledge_refs(items),
                "evidence_ids": [str(value) for value in candidate.evidence_ids or []],
                "epistemic_status": (
                    items[0].provenance or {}
                ).get("epistemic_status"),
                "scope_version": (items[0].provenance or {}).get("scope_version"),
                "scope": (items[0].provenance or {}).get("scope"),
            }
            provenance = {
                key: value for key, value in provenance.items() if value is not None
            }
            created = lifecycle.merge_active(
                conn,
                source_ids=ids,
                content=str(candidate.new_content or ""),
                provenance=provenance,
            )
            after = [store.knowledge_snapshot(created)]
            item_id = _record_item(
                conn,
                batch_id=batch_id,
                kind=kind,
                status=models.MaintenanceCandidateStatus.APPLIED.value,
                candidate=candidate,
                knowledge_ids=_knowledge_refs(items),
                before=before,
                after=after,
                reason=candidate.reason,
            )
            operation_id = store.create_operation(
                conn,
                action="merge",
                batch_id=batch_id,
                item_id=item_id,
                target_ids=[item.id for item in items],
                result_id=created.id,
                before=before,
                after=after[0],
                status=models.MaintenanceCandidateStatus.APPLIED.value,
            )
        return CandidateOutcome(
            status=models.MaintenanceCandidateStatus.APPLIED.value,
            item_id=item_id,
            operation_id=operation_id,
            result_id=created.id,
            detail={"merged_from": _knowledge_refs(items)},
        )
    except _CandidateRejected as exc:
        return _record_rejected(
            conn,
            batch_id=batch_id,
            kind=kind,
            candidate=candidate,
            knowledge_ids=[{"id": knowledge_id} for knowledge_id in ids],
            reason=str(exc),
        )
    except (VersionConflict, KnowledgeNotFound, ValueError) as exc:
        return _record_rejected(
            conn,
            batch_id=batch_id,
            kind=kind,
            candidate=candidate,
            knowledge_ids=[{"id": knowledge_id} for knowledge_id in ids],
            reason=_safe_error(exc),
        )
    except Exception as exc:  # noqa: BLE001 - 单条候选失败不阻断整批
        logger.warning("merge 执行失败", exc_info=True)
        return _record_failed(
            conn,
            batch_id=batch_id,
            kind=kind,
            candidate=candidate,
            knowledge_ids=[{"id": knowledge_id} for knowledge_id in ids],
            error=_safe_error(exc),
        )


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------


def _execute_normalize(
    conn: sqlite3.Connection,
    candidate: models.MaintenanceCandidate,
    *,
    batch_id: str | None,
    allowed: Mapping[str, Mapping[str, Any]],
) -> CandidateOutcome:
    kind = store.ITEM_NORMALIZE
    ids = _normalized_ids(candidate.knowledge_ids)
    if len(ids) != 1:
        return _record_rejected(
            conn,
            batch_id=batch_id,
            kind=kind,
            candidate=candidate,
            reason="normalize 恰好需要一个 knowledge_id",
        )
    target_id = ids[0]
    if target_id not in allowed:
        return _record_rejected(
            conn,
            batch_id=batch_id,
            kind=kind,
            candidate=candidate,
            knowledge_ids=[{"id": target_id}],
            reason=f"knowledge_id 不在本轮输入里：{target_id}",
        )
    content = (candidate.new_content or "").strip()
    if not content:
        return _record_rejected(
            conn,
            batch_id=batch_id,
            kind=kind,
            candidate=candidate,
            knowledge_ids=[{"id": target_id}],
            reason="normalize 的 new_content 不能为空",
        )
    if (candidate.question or "").strip():
        return _record_rejected(
            conn,
            batch_id=batch_id,
            kind=kind,
            candidate=candidate,
            knowledge_ids=[{"id": target_id}],
            reason="normalize 不得提供 question",
        )
    try:
        with lifecycle.atomic(conn):
            svc = KnowledgeService(conn)
            item = svc.get(target_id)
            if item is None:
                raise _CandidateRejected(f"knowledge_id 不存在：{target_id}")
            version_error = _version_error(conn, item, allowed[target_id])
            if version_error is not None:
                raise _CandidateRejected(version_error)
            if item.content.strip() == content:
                raise _CandidateRejected("normalize 未产生实际变化")
            before = [store.knowledge_snapshot(item)]
            fields = {
                "content": content,
                "provenance": {
                    "operation": "normalize",
                    "batch_id": batch_id,
                },
            }
            revised = revise_atomic(
                conn,
                item.id,
                expected_version=int(item.version),
                fields=fields,
                source=models.SOURCE_MAINTENANCE_NORMALIZE,
                actor="maintenance",
            )
            after = [store.knowledge_snapshot(revised)]
            item_id = _record_item(
                conn,
                batch_id=batch_id,
                kind=kind,
                status=models.MaintenanceCandidateStatus.APPLIED.value,
                candidate=candidate,
                knowledge_ids=_knowledge_refs([item]),
                before=before,
                after=after,
                reason=candidate.reason,
            )
            operation_id = store.create_operation(
                conn,
                action="normalize",
                batch_id=batch_id,
                item_id=item_id,
                target_ids=[item.id],
                result_id=revised.id,
                before=before,
                after=after[0],
                status=models.MaintenanceCandidateStatus.APPLIED.value,
            )
        return CandidateOutcome(
            status=models.MaintenanceCandidateStatus.APPLIED.value,
            item_id=item_id,
            operation_id=operation_id,
            result_id=revised.id,
        )
    except _CandidateRejected as exc:
        return _record_rejected(
            conn,
            batch_id=batch_id,
            kind=kind,
            candidate=candidate,
            knowledge_ids=[{"id": target_id}],
            reason=str(exc),
        )
    except (VersionConflict, KnowledgeNotFound, ValueError) as exc:
        return _record_rejected(
            conn,
            batch_id=batch_id,
            kind=kind,
            candidate=candidate,
            knowledge_ids=[{"id": target_id}],
            reason=_safe_error(exc),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("normalize 执行失败", exc_info=True)
        return _record_failed(
            conn,
            batch_id=batch_id,
            kind=kind,
            candidate=candidate,
            knowledge_ids=[{"id": target_id}],
            error=_safe_error(exc),
        )


# ---------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------


def _execute_ask(
    conn: sqlite3.Connection,
    candidate: models.MaintenanceCandidate,
    *,
    batch_id: str | None,
    allowed: Mapping[str, Mapping[str, Any]],
) -> CandidateOutcome:
    kind = store.ITEM_ASK
    question = (candidate.question or "").strip()
    if not question:
        return _record_rejected(
            conn,
            batch_id=batch_id,
            kind=kind,
            candidate=candidate,
            reason="ask 必须提供具体 question",
        )
    if (candidate.new_content or "").strip():
        return _record_rejected(
            conn,
            batch_id=batch_id,
            kind=kind,
            candidate=candidate,
            reason="ask 不得提供 new_content（不直接修改知识）",
        )
    ids = _normalized_ids(candidate.knowledge_ids)
    unknown = [knowledge_id for knowledge_id in ids if knowledge_id not in allowed]
    if unknown:
        return _record_rejected(
            conn,
            batch_id=batch_id,
            kind=kind,
            candidate=candidate,
            knowledge_ids=[{"id": knowledge_id} for knowledge_id in ids],
            reason="knowledge_ids 不在本轮输入里：" + ", ".join(unknown[:5]),
        )
    refs = [
        {
            "id": knowledge_id,
            "version": int(allowed[knowledge_id]["version"]),
            "chain_id": str(allowed[knowledge_id].get("chain_id") or ""),
        }
        for knowledge_id in ids
    ]
    try:
        duplicate = store.find_duplicate_question(
            conn, knowledge_ids=refs, question=question
        )
        if duplicate is not None:
            return CandidateOutcome(
                status=models.MaintenanceCandidateStatus.ASKED.value,
                item_id=duplicate["id"],
                reason="同一知识集合与同一问题已在等待回答，跳过（不重复催问）",
                detail={"duplicate_question": True},
            )
        with lifecycle.atomic(conn):
            item_id = store.create_item(
                conn,
                batch_id=batch_id,
                kind=kind,
                status=models.PendingStatus.AWAITING_ANSWER.value,
                knowledge_ids=refs,
                question=question,
                evidence_ids=[str(value) for value in candidate.evidence_ids or []],
                reason=candidate.reason or None,
            )
        return CandidateOutcome(
            status=models.MaintenanceCandidateStatus.ASKED.value,
            item_id=item_id,
            detail={"duplicate_question": False},
        )
    except Exception as exc:  # noqa: BLE001 - 单条候选失败不阻断整批
        logger.warning("ask 执行失败", exc_info=True)
        return _record_failed(
            conn,
            batch_id=batch_id,
            kind=kind,
            candidate=candidate,
            knowledge_ids=refs,
            error=_safe_error(exc),
        )
