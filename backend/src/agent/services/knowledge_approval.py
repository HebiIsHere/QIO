"""知识保存申请的统一处理（原子）。

为什么需要这一层：待审批知识（knowledge.state = pending_review）过去只能通过
`/api/knowledge/{id}/verify` 先批准**原文**、再用 `/revise` 改内容。两条 HTTP 请求
之间没有事务，`/revise` 失败时原文已经生效 —— 用户以为自己「修改后保存」，
实际保存的是模型写的旧内容。这个模块把三种处理都收进**一个真实事务**：

* 确认保存：pending_review -> verified -> active（同事务；高影响分类必须由 user 确认）；
* 修改后保存：以**用户确认后的内容**生成新的 active 版本，原候选标记为已被取代
  （保留原文与来源，供版本链/审计），绝不「先批准原文再改」；
* 不保存：候选撤销并写入 ignored_at（与既有 /api/knowledge/{id}/ignore 语义一致）；
* 打回：候选回到草稿并写入拒绝事实（墓碑，避免同一内容被反复重存）。

并发与重复（与 lifecycle 的版本链契约一致）：

* `expected_version` / `expected_content` / `expected_updated_at` 任一不符 → 409，
  并在响应里带**当前版本**，让界面重新展示、用户重新确认；绝不覆盖他人修改；
* 同一候选已经被处理：若处理结果恰好就是用户这次确认的内容（重试/重复提交）→
  幂等成功（already_handled=true，不新增第二行）；否则 409 并如实说明当前状态；
* 单事务 + BEGIN IMMEDIATE：两个并发提交只有一个生效，后到的拿到明确冲突。

只追加、不修改历史：本模块复用 lifecycle 的 `atomic` / `revise_atomic` 同一套
版本链实现（_insert_active / _revoke_row / chain_of），不另造一套写入语义。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from agent.knowledge.lifecycle import (
    KnowledgeItem,
    KnowledgeNotFound,
    KnowledgeService,
    KnowledgeState,
    VersionConflict,
    _insert_active,
    _now,
    _record_effective_change_safe,
    _revoke_row,
    atomic,
    chain_of,
    new_id,
    resolve_current,
)

#: 三种用户动作（结构化枚举；界面按动作分派，不按标题文案猜）。
DECISION_APPROVE = "approve"
DECISION_APPROVE_MODIFIED = "approve_modified"
DECISION_REJECT = "reject"
DECISION_IGNORE = "ignore"

DECISIONS = (DECISION_APPROVE, DECISION_APPROVE_MODIFIED, DECISION_REJECT, DECISION_IGNORE)

#: 用户确认内容的上限与 propose_knowledge 一致（memory.limits 是唯一事实源）。
from agent.memory.limits import MAX_CANDIDATE_CONTENT  # noqa: E402  (放在常量区之后，便于阅读)


class KnowledgeApprovalError(Exception):
    """知识保存申请处理失败的可读错误（路由层据此给出状态码与结构化响应）。"""

    status_code = 400
    code = "knowledge_approval_failed"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        current: dict[str, Any] | None = None,
    ) -> None:
        self.message = message
        self.current = current
        if code:
            self.code = code
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "error": self.message,
            "current": self.current,
        }


class KnowledgeApprovalNotFound(KnowledgeApprovalError):
    status_code = 404
    code = "knowledge_not_found"


class KnowledgeApprovalConflict(KnowledgeApprovalError):
    """版本/内容/状态冲突：409（「你确认的那一版已经变了」）。"""

    status_code = 409
    code = "knowledge_approval_conflict"


class KnowledgeApprovalInvalid(KnowledgeApprovalError):
    status_code = 400
    code = "knowledge_approval_invalid"


def knowledge_approval_payload(item: KnowledgeItem) -> dict[str, Any]:
    """一条知识候选/版本的结构化事实（界面只展示这里有的字段，不编造）。"""
    provenance = dict(item.provenance or {})
    return {
        "id": item.id,
        "state": item.state.value,
        "category": item.category,
        "content": item.content,
        "version": int(item.version),
        "chain_id": item.chain_id,
        "supersedes_id": item.supersedes_id,
        "scope_nodes": list(item.node_ids or []),
        "scope_global": bool(item.scope_global),
        "scope_unresolved": bool(item.scope_unresolved),
        "epistemic_status": provenance.get("epistemic_status"),
        "provenance_source": provenance.get("source"),
        "updated_at": item.updated_at,
        "created_at": item.created_at,
    }


def _current_payload(conn: sqlite3.Connection, knowledge_id: str) -> dict[str, Any] | None:
    svc = KnowledgeService(conn)
    item = svc.get(knowledge_id)
    if item is None:
        return None
    return knowledge_approval_payload(item)


def _require_expected(
    item: KnowledgeItem,
    *,
    expected_version: int | None,
    expected_content: str | None,
    expected_updated_at: str | None,
) -> None:
    """核对「用户看到的这一版」是否仍是当前候选。

    三项都是可选的（旧调用方/命令行可以不带）；带了就必须一致 ——
    界面提交时把展示过的 version + content + updated_at 一起回传，
    任何一项变了都说明用户确认的不是现在这一版。
    """
    if expected_version is not None and int(expected_version) != int(item.version):
        raise KnowledgeApprovalConflict(
            f"你确认的版本（v{expected_version}）已经变化：当前是 v{item.version}；"
            "这次没有保存任何内容，请重新确认。",
            code="version_changed",
            current=knowledge_approval_payload(item),
        )
    if expected_updated_at is not None and str(expected_updated_at) != str(item.updated_at):
        raise KnowledgeApprovalConflict(
            "你确认之后这条候选又被修改过（更新时间不一致）；这次没有保存任何内容，请重新确认。",
            code="content_changed",
            current=knowledge_approval_payload(item),
        )
    if expected_content is not None and str(expected_content) != item.content:
        raise KnowledgeApprovalConflict(
            "你确认的内容已经变了（可能在别处被修改）；这次没有保存任何内容，请重新看一遍再确认。",
            code="content_changed",
            current=knowledge_approval_payload(item),
        )


def _stamp_provenance(
    conn: sqlite3.Connection,
    knowledge_id: str,
    extra: dict[str, Any],
) -> None:
    """把审批事实写进 provenance（不改状态；调用方必须已在事务内）。"""
    svc = KnowledgeService(conn)
    item = svc.get(knowledge_id)
    if item is None:
        return
    provenance = dict(item.provenance or {})
    provenance.update(extra)
    conn.execute(
        "UPDATE knowledge SET provenance = ? WHERE id = ?",
        (json.dumps(provenance, ensure_ascii=False), knowledge_id),
    )


def _successor_of(conn: sqlite3.Connection, knowledge_id: str) -> KnowledgeItem | None:
    """候选被取代后，链上真正生效的那一版（用于幂等重试的判断）。"""
    row = conn.execute(
        "SELECT id FROM knowledge WHERE supersedes_id = ? ORDER BY created_at DESC LIMIT 1",
        (knowledge_id,),
    ).fetchone()
    if row is None:
        return None
    return KnowledgeService(conn).get(str(row["id"]))


def _already_handled_result(
    conn: sqlite3.Connection,
    knowledge_id: str,
    item: KnowledgeItem,
    requested_content: str | None,
) -> dict[str, Any] | None:
    """已经被别处处理过：只有「处理结果 == 用户这次要保存的内容」才算幂等成功。

    返回 None 表示不能当成幂等成功（调用方给 409 冲突，绝不覆盖）。
    """
    wanted = (requested_content if requested_content is not None else item.content or "").strip()
    candidate: KnowledgeItem | None = None
    if item.state is KnowledgeState.ACTIVE:
        candidate = item
    elif item.state is KnowledgeState.REVOKED:
        candidate = _successor_of(conn, knowledge_id)
    if candidate is None or candidate.state is not KnowledgeState.ACTIVE:
        return None
    if candidate.content.strip() != wanted:
        return None
    return {
        "ok": True,
        "decision": (
            DECISION_APPROVE_MODIFIED
            if requested_content is not None
            else DECISION_APPROVE
        ),
        "already_handled": True,
        "knowledge": knowledge_approval_payload(candidate),
        "message": "这条保存申请已经处理过，当前生效的就是你确认的内容；没有重复保存。",
    }


def _approve_original(conn: sqlite3.Connection, item: KnowledgeItem) -> dict[str, Any]:
    """确认保存原文：同一事务里 verify + activate（高影响分类由 user 确认）。"""
    svc = KnowledgeService(conn)
    try:
        svc.verify(item.id, verified_by="user")
        activated = svc.activate(item.id)
    except (ValueError, PermissionError) as exc:  # 状态机/高影响分类不允许
        raise KnowledgeApprovalConflict(
            f"当前不能确认保存：{exc}",
            code="invalid_state",
            current=_current_payload(conn, item.id),
        ) from exc
    _stamp_provenance(
        conn,
        activated.id,
        {
            "approved_at": _now(),
            "approved_by": "user",
            "approval_decision": DECISION_APPROVE,
        },
    )
    refreshed = KnowledgeService(conn).get(activated.id) or activated
    return {
        "ok": True,
        "decision": DECISION_APPROVE,
        "already_handled": False,
        "knowledge": knowledge_approval_payload(refreshed),
    }


def _approve_modified(
    conn: sqlite3.Connection, item: KnowledgeItem, content: str
) -> dict[str, Any]:
    """修改后保存：以用户确认后的内容生成新的 active 版本（同一事务）。

    1. 该候选必须是链上最新版本（否则说明已有更新的版本，拒绝）；
    2. 撤销链上所有 active（含候选自身），旧原文保留在历史里；
    3. 插入新版本并直接 active（version = 候选版本 + 1）；
    4. 写 provenance：approved_from / corrected_from / edited_by_user / original_content。
    """
    svc = KnowledgeService(conn)
    members = chain_of(conn, item.id)
    conflicts = [
        member
        for member in members
        if member.id != item.id and int(member.version) >= int(item.version)
    ]
    if conflicts:
        newest = max(conflicts, key=lambda member: int(member.version))
        raise KnowledgeApprovalConflict(
            "该候选已被更新的版本取代，没有保存你这次编辑的内容；请重新打开待确认列表。",
            code="superseded",
            current=knowledge_approval_payload(newest),
        )

    provenance = dict(item.provenance or {})
    provenance.update(
        {
            "approved_at": _now(),
            "approved_by": "user",
            "approval_decision": DECISION_APPROVE_MODIFIED,
            "approved_from": item.id,
            "corrected_from": item.id,
            "edited_by_user": True,
            "original_content": item.content,
        }
    )
    new_knowledge_id = new_id()
    now = _now()
    # 旧 active（以及候选自身）先撤销：被新版本取代，原文与历史保留。
    for member in members:
        if member.state is KnowledgeState.ACTIVE:
            _revoke_row(conn, member.id, superseded_by=new_knowledge_id)
    _revoke_row(conn, item.id, superseded_by=new_knowledge_id)
    _insert_active(
        conn,
        knowledge_id=new_knowledge_id,
        category=item.category,
        content=content,
        supersedes_id=item.id,
        chain_id=item.chain_id or item.id,
        version=int(item.version) + 1,
        node_ids=list(item.node_ids),
        entity_ids=list(item.entity_ids),
        provenance=provenance,
        confidence=0.9,
        export=bool(item.export),
        now=now,
    )
    created = svc.get(new_knowledge_id)
    if created is None:  # 理论不可达：插入失败会整体回滚
        raise KnowledgeApprovalError("保存后没有找到新版本（事务已回滚）", code="write_failed")
    _record_effective_change_safe(conn, created)
    return {
        "ok": True,
        "decision": DECISION_APPROVE_MODIFIED,
        "already_handled": False,
        "supersedes": item.id,
        "knowledge": knowledge_approval_payload(created),
    }


def approve_pending_knowledge(
    conn: sqlite3.Connection,
    knowledge_id: str,
    *,
    content: str | None = None,
    expected_version: int | None = None,
    expected_content: str | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """确认保存 / 修改后保存（原子；content 为空 = 按原文保存）。

    只能处理 pending_review 候选；已经被处理过时按「幂等重试 / 明确冲突」区分，
    绝不在状态已变化的情况下再写一次。
    """
    wanted = content.strip() if isinstance(content, str) else None
    if wanted is not None and not wanted:
        raise KnowledgeApprovalInvalid("修改后的内容不能为空；这次没有保存任何内容。")
    if wanted is not None and len(wanted) > MAX_CANDIDATE_CONTENT:
        raise KnowledgeApprovalInvalid(
            f"修改后的内容过长（上限 {MAX_CANDIDATE_CONTENT} 字）；这次没有保存任何内容。"
        )
    with atomic(conn):
        svc = KnowledgeService(conn)
        item = svc.get(knowledge_id)
        if item is None:
            raise KnowledgeApprovalNotFound(
                "这条保存申请已经不存在了（可能已在别处处理）；这次没有写入任何内容。"
            )
        if item.state is not KnowledgeState.PENDING_REVIEW:
            idempotent = _already_handled_result(conn, knowledge_id, item, wanted)
            if idempotent is not None:
                return idempotent
            raise KnowledgeApprovalConflict(
                f"这条保存申请已经被处理过（当前状态：{item.state.value}），"
                "没有再次保存；请刷新待确认列表。",
                code="already_handled",
                current=knowledge_approval_payload(item),
            )
        _require_expected(
            item,
            expected_version=expected_version,
            expected_content=expected_content,
            expected_updated_at=expected_updated_at,
        )
        if wanted is None or wanted == item.content.strip():
            return _approve_original(conn, item)
        return _approve_modified(conn, item, wanted)


def ignore_pending_knowledge(
    conn: sqlite3.Connection,
    knowledge_id: str,
    *,
    expected_version: int | None = None,
    expected_content: str | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """「不保存」：撤销候选并写入 ignored_at（现有 /ignore 的同一语义，原子）。"""
    with atomic(conn):
        svc = KnowledgeService(conn)
        item = svc.get(knowledge_id)
        if item is None:
            raise KnowledgeApprovalNotFound(
                "这条保存申请已经不存在了（可能已在别处处理）；这次没有写入任何内容。"
            )
        if item.state is KnowledgeState.REVOKED:
            provenance = dict(item.provenance or {})
            if provenance.get("ignored_at"):
                return {
                    "ok": True,
                    "decision": DECISION_IGNORE,
                    "already_handled": True,
                    "knowledge": knowledge_approval_payload(item),
                    "message": "这条知识已经按你的选择没有保存；没有重复处理。",
                }
        if item.state is not KnowledgeState.PENDING_REVIEW:
            raise KnowledgeApprovalConflict(
                f"这条保存申请已经被处理过（当前状态：{item.state.value}），"
                "没有再次修改；请刷新待确认列表。",
                code="already_handled",
                current=knowledge_approval_payload(item),
            )
        _require_expected(
            item,
            expected_version=expected_version,
            expected_content=expected_content,
            expected_updated_at=expected_updated_at,
        )
        _revoke_row(conn, item.id)
        _stamp_provenance(
            conn,
            item.id,
            {
                "ignored_at": _now(),
                "reason": "user_ignored",
                "approval_decision": DECISION_IGNORE,
            },
        )
        ignored = KnowledgeService(conn).get(item.id) or item
    return {
        "ok": True,
        "decision": DECISION_IGNORE,
        "already_handled": False,
        "knowledge": knowledge_approval_payload(ignored),
    }


def reject_pending_knowledge(
    conn: sqlite3.Connection,
    knowledge_id: str,
    *,
    expected_version: int | None = None,
    expected_content: str | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """打回草稿（星球「待确认」的拒绝动作）：保留条目，同时写下拒绝事实。"""
    from agent.knowledge.rejection import REASON_REJECTED, mark_rejected

    with atomic(conn):
        svc = KnowledgeService(conn)
        item = svc.get(knowledge_id)
        if item is None:
            raise KnowledgeApprovalNotFound(
                "这条保存申请已经不存在了（可能已在别处处理）；这次没有写入任何内容。"
            )
        if item.state is KnowledgeState.DRAFT:
            return {
                "ok": True,
                "decision": DECISION_REJECT,
                "already_handled": True,
                "knowledge": knowledge_approval_payload(item),
                "message": "这条申请已经打回草稿；没有重复处理。",
            }
        if item.state is not KnowledgeState.PENDING_REVIEW:
            raise KnowledgeApprovalConflict(
                f"这条保存申请已经被处理过（当前状态：{item.state.value}），"
                "不能再打回；请刷新待确认列表。",
                code="already_handled",
                current=knowledge_approval_payload(item),
            )
        _require_expected(
            item,
            expected_version=expected_version,
            expected_content=expected_content,
            expected_updated_at=expected_updated_at,
        )
        try:
            draft = svc.reject(item.id)
        except ValueError as exc:
            raise KnowledgeApprovalConflict(
                f"当前不能打回：{exc}",
                code="invalid_state",
                current=knowledge_approval_payload(item),
            ) from exc
        mark_rejected(conn, item.id, reason=REASON_REJECTED)
        rejected = KnowledgeService(conn).get(draft.id) or draft
    return {
        "ok": True,
        "decision": DECISION_REJECT,
        "already_handled": False,
        "knowledge": knowledge_approval_payload(rejected),
    }


def dispatch_knowledge_decision(
    conn: sqlite3.Connection,
    knowledge_id: str,
    decision: str,
    *,
    content: str | None = None,
    expected_version: int | None = None,
    expected_content: str | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """按结构化 decision 分派（路由层唯一入口）。"""
    normalized = str(decision or "").strip()
    if normalized not in DECISIONS:
        raise KnowledgeApprovalInvalid(
            f"未知的审批动作：{normalized or '（空）'}；这次没有写入任何内容。"
        )
    if normalized == DECISION_APPROVE:
        if isinstance(content, str) and content.strip() and content.strip() != (expected_content or "").strip():
            # 「确认保存」不接受夹带修改：避免界面把用户编辑过的内容当原文批准。
            raise KnowledgeApprovalInvalid(
                "「确认保存」不接受修改后的内容；如需保存编辑后的版本，请使用「修改后保存」。"
            )
        return approve_pending_knowledge(
            conn,
            knowledge_id,
            content=None,
            expected_version=expected_version,
            expected_content=expected_content,
            expected_updated_at=expected_updated_at,
        )
    if normalized == DECISION_APPROVE_MODIFIED:
        return approve_pending_knowledge(
            conn,
            knowledge_id,
            content=content,
            expected_version=expected_version,
            expected_content=expected_content,
            expected_updated_at=expected_updated_at,
        )
    if normalized == DECISION_IGNORE:
        return ignore_pending_knowledge(
            conn,
            knowledge_id,
            expected_version=expected_version,
            expected_content=expected_content,
            expected_updated_at=expected_updated_at,
        )
    return reject_pending_knowledge(
        conn,
        knowledge_id,
        expected_version=expected_version,
        expected_content=expected_content,
        expected_updated_at=expected_updated_at,
    )


__all__ = [
    "DECISION_APPROVE",
    "DECISION_APPROVE_MODIFIED",
    "DECISION_IGNORE",
    "DECISION_REJECT",
    "DECISIONS",
    "KnowledgeApprovalConflict",
    "KnowledgeApprovalError",
    "KnowledgeApprovalInvalid",
    "KnowledgeApprovalNotFound",
    "approve_pending_knowledge",
    "dispatch_knowledge_decision",
    "ignore_pending_knowledge",
    "knowledge_approval_payload",
    "reject_pending_knowledge",
]
