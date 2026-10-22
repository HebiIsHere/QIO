# -*- coding: utf-8 -*-
"""用户要求台账与有界授权记录（R9-F，契约 docs/r9f-contracts.md §3.1）。

为什么需要它们：
* 受限模式下自然语言授权的依据是**具体用户要求**（用户原话 + 真实来源），
  不是 turn / topic / session / 任务生命周期。后续修正、撤回必须能回溯到真实用户消息。
* 模型审查通过后只形成**有界授权记录**：绑定 requirement + operation fingerprint
  （含对象身份与模式）+ 对象/操作/限制。它用于同一范围内避免反复审批、重启后重建，
  而不是长期权限；requirement 被修正/撤回/替换、对象身份变化、模式变化、过期/撤销
  后一律失效。
* 持久化到迁移 55 的 user_requirements / authorization_records；进程内不保存唯一副本，
  重启后从库里重建。

生命周期（本模块实现）：
    record_user_message  -> 新建 active 要求（每条用户消息一个）
    add_correction       -> append 子要求：父要求 superseded、父链授权记录撤销、子要求 active
    withdraw             -> 要求及后代 withdrawn，授权记录撤销
    supersede            -> 显式替换：要求 superseded，授权记录撤销
    current / by_id      -> 读取；每次从库里取，重启可重建

安全边界：
* 本模块没有任何面向模型/工具的直接写入入口；谁调用由 engine/gateway/循环决定。
* AuthorizationLedger.reuse 只认 allow 记录，且要求对应 requirement 仍为 active、
  记录未撤销、未过期；fingerprint 由 review.operation_fingerprint 计算（含模式/对象身份）。
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable

from agent.storage.db import transaction

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注
    from agent.security.review import ReviewOutcome, ReviewRequest

logger = logging.getLogger(__name__)

#: 用户要求状态（与迁移 55 的 user_requirements.status 对应）。
STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_WITHDRAWN = "withdrawn"
VALID_STATUSES = frozenset({STATUS_ACTIVE, STATUS_SUPERSEDED, STATUS_WITHDRAWN})

#: 授权记录来源。模型审查 / 用户批准 / 固定规则都可能形成记录，但只有 allow 可用于复用。
SOURCE_MODEL_REVIEW = "model_review"
SOURCE_USER_APPROVAL = "user_approval"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _loads(raw: object, fallback: Any) -> Any:
    text = str(raw or "").strip()
    if not text:
        return fallback
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        logger.warning("用户要求/授权记录里的 JSON 列损坏，按空值处理", exc_info=False)
        return fallback


def _tuple_of_str(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return ()


# ---------------------------------------------------------------------------
# 用户要求
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserRequirement:
    """一条具体用户要求（用户原话 + 来源 + 生命周期 + 用户明确写出的限制/修正）。

    字段语义见契约 §3.1：
    * text 是用户原话，后端不得让模型改写；修正产生新的子要求，而不是改写原行；
    * provenance 记录 kind/message_id/turn_id/topic_id（修正行额外带 parent_requirement_id）；
    * limits 只来自用户原话或用户确认：新建时为空，后续修正原话会追加进去；
    * status 只取 active / superseded / withdrawn，授权记录只在 active 期间可复用。
    """

    requirement_id: str
    text: str
    provenance: dict
    parent_id: str | None
    status: str
    limits: tuple[str, ...]
    created_at: str
    updated_at: str

    def as_dict(self) -> dict:
        """供审查输入/审计使用的只读结构（不包含模型推理，不含密钥）。"""
        return {
            "requirement_id": self.requirement_id,
            "text": self.text,
            "provenance": dict(self.provenance),
            "parent_id": self.parent_id,
            "status": self.status,
            "limits": list(self.limits),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class RequirementLedger:
    """用户要求台账（由 AppContext 注入 db 连接；持久化后重启可重建）。"""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    # -- 读取 ---------------------------------------------------------------

    def _row(self, requirement_id: str) -> Any:
        return self.conn.execute(
            "SELECT * FROM user_requirements WHERE id = ?", (str(requirement_id),)
        ).fetchone()

    @staticmethod
    def _from_row(row: Any) -> UserRequirement:
        provenance = _loads(row["provenance"], {})
        if not isinstance(provenance, dict):
            provenance = {}
        limits = _loads(row["limits"], [])
        if not isinstance(limits, list):
            limits = []
        status = str(row["status"] or STATUS_ACTIVE)
        return UserRequirement(
            requirement_id=str(row["id"]),
            text=str(row["text"] or ""),
            provenance=provenance,
            parent_id=str(row["parent_id"]) if row["parent_id"] else None,
            status=status,
            limits=_tuple_of_str(limits),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def by_id(self, requirement_id: str) -> UserRequirement | None:
        if not str(requirement_id or "").strip():
            return None
        row = self._row(requirement_id)
        return self._from_row(row) if row is not None else None

    def current(self) -> UserRequirement | None:
        """最近一条 active 要求；没有则 None。"""
        row = self.conn.execute(
            "SELECT * FROM user_requirements WHERE status = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (STATUS_ACTIVE,),
        ).fetchone()
        return self._from_row(row) if row is not None else None

    def chain(self, requirement_id: str) -> list[UserRequirement]:
        """根 -> 当前要求的链（含当前）；找不到返回空列表。"""
        found: list[UserRequirement] = []
        seen: set[str] = set()
        current = self.by_id(requirement_id)
        while current is not None and current.requirement_id not in seen:
            found.append(current)
            seen.add(current.requirement_id)
            if not current.parent_id:
                break
            current = self.by_id(current.parent_id)
        found.reverse()
        return found

    def corrections_for(self, requirement_id: str) -> tuple[str, ...]:
        """当前要求之前的修正原话（根 -> 父，按时间顺序）。

        用途：组装 ReviewRequest.corrections。修正文本只来自用户原话（add_correction
        传入的 text 原样保存），不由模型生成。
        """
        chain = self.chain(requirement_id)
        return tuple(item.text for item in chain[:-1] if item.text.strip())

    # -- 写入 ---------------------------------------------------------------

    def record_user_message(
        self,
        *,
        text: object,
        message_id: object = None,
        turn_id: object = None,
        topic_id: object = None,
    ) -> UserRequirement:
        """把一条用户消息登记为 active 要求（每条用户消息一个，不自动复用旧要求）。

        说明：这里**不**自动 supersede 其它 active 要求。每条消息都是独立要求；
        是否延续原要求由调用方显式表达（修正用 add_correction；替换用 supersede）。
        授权记录按 requirement_id 绑定，因此新要求天然不会命中旧记录。
        """
        requirement_id = "req_" + uuid.uuid4().hex[:16]
        now = _now_iso()
        provenance = {
            "kind": "user_message",
            "message_id": None if message_id is None else str(message_id),
            "turn_id": None if turn_id is None else str(turn_id),
            "topic_id": None if topic_id is None else str(topic_id),
        }
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO user_requirements"
                " (id, text, provenance, parent_id, status, limits, turn_id, topic_id,"
                "  created_at, updated_at)"
                " VALUES (?, ?, ?, NULL, ?, '[]', ?, ?, ?, ?)",
                (
                    requirement_id,
                    str(text if text is not None else ""),
                    _dumps(provenance),
                    STATUS_ACTIVE,
                    provenance["turn_id"],
                    provenance["topic_id"],
                    now,
                    now,
                ),
            )
        stored = self.by_id(requirement_id)
        if stored is None:  # pragma: no cover - 写入后读不到说明库已损坏
            raise RuntimeError("用户要求写入后无法读回（数据库异常）")
        return stored

    def add_correction(
        self,
        requirement_id: str,
        text: object,
        *,
        message_id: object = None,
    ) -> UserRequirement:
        """追加一条用户修正：新建子要求，父要求 superseded，父链授权记录撤销。

        契约语义「修正 = append」：
        * 原要求行保留（历史可回溯），只改状态；
        * 子要求 text 是用户这次修正的原话，parent_id 指向被修正要求；
        * limits 继承父要求的限制并追加本次修正原话（limits 定义明确包含"后续修正"）；
        * 被修正要求（及其祖先链）的授权记录立即失效，不能借着旧审查继续执行。
        """
        parent = self.by_id(requirement_id)
        if parent is None:
            raise KeyError("用户要求不存在：" + str(requirement_id))
        if parent.status != STATUS_ACTIVE:
            raise ValueError("只能修正 active 的用户要求（当前状态：" + parent.status + "）")
        requirement_id_new = "req_" + uuid.uuid4().hex[:16]
        now = _now_iso()
        correction_text = str(text if text is not None else "")
        limits = list(parent.limits)
        if correction_text.strip():
            limits.append(correction_text)
        provenance = {
            "kind": "user_correction",
            "message_id": None if message_id is None else str(message_id),
            "turn_id": str(parent.provenance.get("turn_id") or "") or None,
            "topic_id": str(parent.provenance.get("topic_id") or "") or None,
            "parent_requirement_id": parent.requirement_id,
        }
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO user_requirements"
                " (id, text, provenance, parent_id, status, limits, turn_id, topic_id,"
                "  created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    requirement_id_new,
                    correction_text,
                    _dumps(provenance),
                    parent.requirement_id,
                    STATUS_ACTIVE,
                    _dumps(limits),
                    provenance["turn_id"],
                    provenance["topic_id"],
                    now,
                    now,
                ),
            )
            self._set_status(parent.requirement_id, STATUS_SUPERSEDED)
            for ancestor_id in [item.requirement_id for item in self.chain(parent.requirement_id)]:
                AuthorizationLedger(self.conn).revoke_for_requirement(ancestor_id)
        stored = self.by_id(requirement_id_new)
        if stored is None:  # pragma: no cover - 写入后读不到说明库已损坏
            raise RuntimeError("用户修正写入后无法读回（数据库异常）")
        return stored

    def withdraw(self, requirement_id: str) -> None:
        """撤回要求：要求及全部后代 withdrawn，相关授权记录全部撤销。"""
        target = self.by_id(requirement_id)
        if target is None:
            raise KeyError("用户要求不存在：" + str(requirement_id))
        ids = self._descendant_ids(target.requirement_id)
        with transaction(self.conn):
            for item in ids:
                self._set_status(item, STATUS_WITHDRAWN)
                AuthorizationLedger(self.conn).revoke_for_requirement(item)

    def supersede(self, requirement_id: str) -> None:
        """显式替换要求：标记 superseded 并撤销它的授权记录（后代由调用方处理）。"""
        target = self.by_id(requirement_id)
        if target is None:
            raise KeyError("用户要求不存在：" + str(requirement_id))
        with transaction(self.conn):
            if target.status == STATUS_ACTIVE:
                self._set_status(target.requirement_id, STATUS_SUPERSEDED)
            AuthorizationLedger(self.conn).revoke_for_requirement(target.requirement_id)

    # -- 内部辅助 -----------------------------------------------------------

    def _set_status(self, requirement_id: str, status: str) -> None:
        if status not in VALID_STATUSES:
            raise ValueError("非法状态：" + str(status))
        self.conn.execute(
            "UPDATE user_requirements SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now_iso(), str(requirement_id)),
        )

    def _descendant_ids(self, requirement_id: str) -> list[str]:
        """要求自身 + 所有后代 id（BFS）。"""
        result = [str(requirement_id)]
        frontier = [str(requirement_id)]
        while frontier:
            current = frontier.pop(0)
            rows = self.conn.execute(
                "SELECT id FROM user_requirements WHERE parent_id = ?", (current,)
            ).fetchall()
            for row in rows:
                child = str(row["id"])
                if child not in result:
                    result.append(child)
                    frontier.append(child)
        return result


# ---------------------------------------------------------------------------
# 有界授权记录
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorizationRecord:
    """模型审查/用户批准形成的有界授权记录（契约 §3.1）。

    它只证明「这条 requirement 下，这个 operation fingerprint（含对象身份与模式）
    在 limits 内被允许」，不是长期权限，也不能覆盖新对象/新程序/新接收方。
    """

    record_id: str
    requirement_id: str
    operation_kind: str
    fingerprint: str
    decision: str
    reason: str
    allowed_operations: tuple[str, ...]
    objects: tuple[dict, ...]
    limits: tuple[str, ...]
    source: str
    mode: str
    created_at: str
    expires_at: str | None
    revoked_at: str | None
    review_id: str | None

    def as_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "requirement_id": self.requirement_id,
            "operation_kind": self.operation_kind,
            "fingerprint": self.fingerprint,
            "decision": self.decision,
            "reason": self.reason,
            "allowed_operations": list(self.allowed_operations),
            "objects": [dict(item) for item in self.objects],
            "limits": list(self.limits),
            "source": self.source,
            "mode": self.mode,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "revoked_at": self.revoked_at,
            "review_id": self.review_id,
        }


class AuthorizationLedger:
    """有界授权记录台账（持久化到迁移 55 的 authorization_records）。"""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    @staticmethod
    def _from_row(row: Any) -> AuthorizationRecord:
        objects = _loads(row["objects"], [])
        if not isinstance(objects, list):
            objects = []
        return AuthorizationRecord(
            record_id=str(row["id"]),
            requirement_id=str(row["requirement_id"]),
            operation_kind=str(row["operation_kind"]),
            fingerprint=str(row["fingerprint"]),
            decision=str(row["decision"]),
            reason=str(row["reason"] or ""),
            allowed_operations=_tuple_of_str(_loads(row["allowed_operations"], [])),
            objects=tuple(item for item in objects if isinstance(item, dict)),
            limits=_tuple_of_str(_loads(row["limits"], [])),
            source=str(row["source"] or ""),
            mode=str(row["mode"] or ""),
            created_at=str(row["created_at"]),
            expires_at=str(row["expires_at"]) if row["expires_at"] else None,
            revoked_at=str(row["revoked_at"]) if row["revoked_at"] else None,
            review_id=str(row["review_id"]) if row["review_id"] else None,
        )

    def reuse(
        self,
        *,
        requirement_id: str,
        operation_kind: str,
        fingerprint: str,
    ) -> AuthorizationRecord | None:
        """查找可复用的有界授权记录。

        缓存键的实质 = (requirement_id, operation fingerprint, 对象身份, 模式)：
        fingerprint 由 review.operation_fingerprint 计算，已包含操作类型/对象身份/
        程序/argv/cwd/脚本摘要/网络接收方/模式；对象或模式一变 fingerprint 就变，
        因此这里只按三元组精确匹配，不做模糊匹配。
        另外要求：record.decision == allow、未撤销、未过期、对应 requirement 仍 active。
        """
        req_id = str(requirement_id or "").strip()
        if not req_id:
            return None
        rows = self.conn.execute(
            "SELECT r.* FROM authorization_records AS r"
            " JOIN user_requirements AS u ON u.id = r.requirement_id"
            " WHERE r.requirement_id = ? AND r.operation_kind = ? AND r.fingerprint = ?"
            "   AND r.decision = 'allow' AND r.revoked_at IS NULL AND u.status = ?"
            " ORDER BY r.created_at DESC, r.rowid DESC",
            (req_id, str(operation_kind), str(fingerprint), STATUS_ACTIVE),
        ).fetchall()
        now = datetime.now(timezone.utc)
        for row in rows:
            record = self._from_row(row)
            if record.expires_at and _expired(record.expires_at, now):
                continue
            return record
        return None

    def record(self, record: AuthorizationRecord) -> None:
        if not isinstance(record, AuthorizationRecord):
            raise TypeError("record 必须是 AuthorizationRecord")
        if not str(record.record_id or "").strip():
            raise ValueError("record_id 不能为空")
        if not str(record.requirement_id or "").strip():
            raise ValueError("requirement_id 不能为空")
        if not str(record.fingerprint or "").strip():
            raise ValueError("fingerprint 不能为空")
        self.conn.execute(
            "INSERT INTO authorization_records"
            " (id, requirement_id, operation_kind, fingerprint, decision, reason,"
            "  allowed_operations, objects, limits, source, mode, created_at,"
            "  expires_at, revoked_at, review_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.record_id,
                record.requirement_id,
                record.operation_kind,
                record.fingerprint,
                record.decision,
                record.reason,
                _dumps(list(record.allowed_operations)),
                _dumps([dict(item) for item in record.objects]),
                _dumps(list(record.limits)),
                record.source,
                record.mode,
                record.created_at,
                record.expires_at,
                record.revoked_at,
                record.review_id,
            ),
        )

    def revoke_for_requirement(self, requirement_id: str) -> int:
        """撤销该要求的全部有效记录，返回受影响行数。"""
        cursor = self.conn.execute(
            "UPDATE authorization_records SET revoked_at = ?"
            " WHERE requirement_id = ? AND revoked_at IS NULL",
            (_now_iso(), str(requirement_id)),
        )
        count = int(cursor.rowcount or 0)
        if count:
            logger.info("requirement %s 的 %d 条授权记录已撤销", requirement_id, count)
        return count

    def revoke(self, record_id: str) -> None:
        self.conn.execute(
            "UPDATE authorization_records SET revoked_at = ?"
            " WHERE id = ? AND revoked_at IS NULL",
            (_now_iso(), str(record_id)),
        )

    def records_for_requirement(
        self, requirement_id: str, *, include_revoked: bool = False
    ) -> list[AuthorizationRecord]:
        sql = "SELECT * FROM authorization_records WHERE requirement_id = ?"
        params: list[Any] = [str(requirement_id)]
        if not include_revoked:
            sql += " AND revoked_at IS NULL"
        sql += " ORDER BY created_at ASC, rowid ASC"
        return [self._from_row(row) for row in self.conn.execute(sql, params).fetchall()]


def _expired(expires_at: str, now: datetime) -> bool:
    try:
        moment = datetime.fromisoformat(str(expires_at))
    except ValueError:
        return True  # 无法解析的有效期一律视为已过期（不放宽）
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment <= now


# ---------------------------------------------------------------------------
# 由已校验的审查结果形成有界记录（供 Gateway/引擎接线使用）
# ---------------------------------------------------------------------------


def authorization_record_from_outcome(
    outcome: "ReviewOutcome",
    *,
    request: "ReviewRequest",
    source: str = SOURCE_MODEL_REVIEW,
    review_id: str | None = None,
    expires_at: str | None = None,
) -> AuthorizationRecord:
    """把 validate_outcome 校验后的 ALLOW 结果转成有界 AuthorizationRecord。

    * 对象只来自审查结果里**经后端归一化**的 ReviewObject（原始来源/身份来自请求，
      不接受模型自造字段）；
    * fingerprint 由 operation_fingerprint(request.operation, mode=request.mode) 计算，
      因此对象身份/程序/argv/cwd/脚本摘要/网络接收方/模式变化都会导致不能复用；
    * 只允许 decision == allow；其它 decision 调用方应走 APPROVE/DENY，不形成记录。
    """
    from agent.security.review import ReviewDecision, operation_fingerprint

    if not isinstance(outcome, object) or not hasattr(outcome, "decision"):
        raise TypeError("outcome 不是审查结果")
    if outcome.decision != ReviewDecision.ALLOW:
        raise ValueError("只有 allow 的审查结果才能形成授权记录")
    requirement = getattr(request, "requirement", None)
    if requirement is None or not getattr(requirement, "requirement_id", ""):
        raise ValueError("请求没有真实用户要求，不能形成授权记录")
    operation = getattr(request, "operation", None)
    if operation is None:
        raise ValueError("请求没有具体操作，不能形成授权记录")
    return AuthorizationRecord(
        record_id="authrec_" + uuid.uuid4().hex[:16],
        requirement_id=requirement.requirement_id,
        operation_kind=str(operation.kind),
        fingerprint=operation_fingerprint(operation, mode=str(request.mode)),
        decision=outcome.decision.value,
        reason=str(outcome.reason or ""),
        allowed_operations=tuple(str(item) for item in outcome.allowed_operations),
        objects=tuple(
            {
                "kind": str(item.kind),
                "value": str(item.value),
                "identity_key": item.identity_key,
                "file_origin": item.file_origin,
            }
            for item in outcome.objects
        ),
        limits=tuple(str(item) for item in outcome.limits),
        source=str(source),
        mode=str(request.mode),
        created_at=_now_iso(),
        expires_at=expires_at,
        revoked_at=None,
        review_id=review_id,
    )


__all__ = [
    "AuthorizationLedger",
    "AuthorizationRecord",
    "RequirementLedger",
    "SOURCE_MODEL_REVIEW",
    "SOURCE_USER_APPROVAL",
    "STATUS_ACTIVE",
    "STATUS_SUPERSEDED",
    "STATUS_WITHDRAWN",
    "UserRequirement",
    "authorization_record_from_outcome",
]
