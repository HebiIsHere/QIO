# -*- coding: utf-8 -*-
"""用户要求台账、要求版本绑定与有界授权记录（R9-G，契约 docs/r9g-contracts.md §1、§3）。

为什么需要它们：
* 操作必须绑定到**真实用户要求的具体版本**，不能"取数据库最近一条 active 要求"
  （契约 §1.3）：新消息可能修正/撤回/替换旧要求，turn/topic/session 不是授权有效期。
* 模型审查通过后只形成**有界授权记录**：绑定 requirement + requirement_version +
  operation fingerprint（含对象身份/程序/参数/cwd/脚本摘要/内容摘要/接收方/模式）。
  修正后旧版本记录立即失效；对象、内容或动作变化后 fingerprint 变化，不能复用。
* 每个用户 turn 的"新要求 / 补充修正 / 撤回"由模型解析（security/resolution.py），
  后端核对目标要求存在、active 且版本一致后再落实（apply_resolution），并写
  requirement_resolutions 审计；解析失败按新要求记录并标 unresolved，不做关键词兜底。
* 持久化到迁移 55/57 的 user_requirements / authorization_records /
  requirement_resolutions；进程内不保存唯一副本，重启后从库里重建。

生命周期（本模块实现）：
    record_user_message  -> 新建 active 要求（version=1）
    add_correction       -> append 子要求（version=parent+1）：父要求 superseded、父链授权记录撤销
    apply_resolution     -> 按模型解析结果落实 new/amend/withdraw + 版本核对 + 审计
    withdraw             -> 要求及后代 withdrawn（保留历史），授权记录撤销
    supersede            -> 显式替换：要求 superseded（保留历史），授权记录撤销
    binding_for / validate_binding / candidates -> 供 engine/gateway 做要求绑定与候选

安全边界：
* 本模块没有任何面向模型/工具的直接授权写入入口；语义由模型理解，事实由这里核对。
* AuthorizationLedger.reuse 只认 allow 记录，且要求仍 active、版本匹配、记录未撤销/未过期。
* current() 仅为只读展示保留；**不得**再作为授权判定依据（契约 §3）。
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from agent.security import requirement_limits
from agent.storage.db import transaction

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注
    from agent.security.resolution import ResolutionOutcome
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

#: 用户要求版本：新要求从 1 开始；每次补充/修正产生 version=parent+1 的子要求。
DEFAULT_REQUIREMENT_VERSION = 1

#: 要求解析审计结果（requirement_resolutions.outcome）。
RESOLUTION_OUTCOME_APPLIED = "applied"
RESOLUTION_OUTCOME_REJECTED = "rejected"
RESOLUTION_OUTCOME_UNAVAILABLE = "unavailable"

#: 持久化到 user_requirements.limits 的每条限制文本上限（结构化限制会先展平为可读文本）。
MAX_PERSISTED_LIMIT_CHARS = 500


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


def _row_value(row: Any, name: str, default: Any = None) -> Any:
    """按列名读取一行（兼容老库/最小测试库没有新增列）。"""
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return default


def _tuple_of_str(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return ()


def _flatten_limit(limit: dict) -> str:
    """把结构化用户限制展平成可持久化的可读文本（user_requirements.limits 是字符串数组）。

    结构化限制本身只限制、不授权；展平文本用于重启后重新审查时仍能看到用户限制。
    """
    if not isinstance(limit, dict):
        return ""
    operations = [
        str(item).strip() for item in (limit.get("operations") or ()) if str(item).strip()
    ]
    objects: list[str] = []
    for item in limit.get("objects") or ():
        if isinstance(item, dict):
            value = str(item.get("value") or "").strip()
            kind = str(item.get("kind") or "").strip()
            if value:
                objects.append((kind + ":" if kind else "") + value)
        else:
            text = str(item or "").strip()
            if text:
                objects.append(text)
    reason = str(limit.get("reason") or "").strip()
    parts: list[str] = []
    if operations:
        parts.append("操作=" + "/".join(operations[:8]))
    if objects:
        parts.append("对象=" + "；".join(objects[:8]))
    if not parts:
        return ""
    text = "限制：" + "，".join(parts)
    if reason:
        text += "（原因：" + reason[:200] + "）"
    return text[:MAX_PERSISTED_LIMIT_CHARS]


def _normalize_limits_tolerant(
    raw: Sequence[object],
) -> tuple[tuple[dict, ...], bool]:
    """逐条规范化模型限制；坏条目跳过并标记 invalid，保留其余可靠条目。

    R9-H4 §2：个别条目无法处理时不能清空全部；调用方用 returned_invalid
    把状态标 unresolved，但已接受的结构化条目仍要持久化（限制只收紧）。
    """
    valid: list[dict] = []
    invalid = False
    for item in raw or ():
        if not isinstance(item, dict):
            invalid = True
            continue
        try:
            normalized = requirement_limits.normalize_limits([item])
        except ValueError:
            invalid = True
            continue
        valid.extend(normalized)
    try:
        return requirement_limits.normalize_limits(valid), invalid
    except ValueError:  # pragma: no cover - 逐条已校验，兜底
        return (), True


def _sanitize_error_text(exc: object) -> str:
    try:
        from agent.trace.redact import sanitize_error_text

        return sanitize_error_text(str(exc), limit=300)
    except Exception:  # noqa: BLE001 - 脱敏失败时宁可少写
        return "[redacted]"


# ---------------------------------------------------------------------------
# 用户要求与版本绑定
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequirementBinding:
    """一个操作实际依据的用户要求（id + 当时版本）。

    契约 §3、§7：turn 内工具/子智能体/后台派生共享同一个绑定；engine 只用绑定，
    缺失时需人工确认，不回退"最近一条 active 要求"。
    """

    requirement_id: str
    version: int

    def as_dict(self) -> dict:
        return {"requirement_id": self.requirement_id, "version": int(self.version)}


@dataclass(frozen=True)
class AppliedResolution:
    """apply_resolution 的落实结果（供 turn 运行时写入绑定上下文）。"""

    resolution_id: str
    relation: str
    requirement: UserRequirement | None
    target_requirement_id: str | None
    superseded_requirement_id: str | None
    limits: tuple[dict, ...]
    corrections: tuple[str, ...]
    reason: str
    #: 结构化限制状态/内容（R9-H 契约 §6 允许只追加字段）：
    #: structured = 已理解并持久化；legacy/unresolved = 未解决（不得当作无限制）。
    limits_state: str = requirement_limits.STATE_LEGACY
    structured_limits: tuple[dict, ...] | None = None


#: 要求链恢复：单个祖先/当前节点（原样读自 user_requirements）。
#: 注意：structured_limits=None 表示历史未知；state=legacy/unresolved 时即使
#: 有已知结构化内容也不得当作"完整限制"。
@dataclass(frozen=True)
class RequirementChainNode:
    requirement_id: str
    version: int
    parent_id: str | None
    text: str
    status: str
    provenance: dict
    structured_limits: tuple[dict, ...] | None
    structured_limits_state: str
    structured_limits_source: str | None

    def as_dict(self) -> dict:
        return {
            "requirement_id": self.requirement_id,
            "version": int(self.version),
            "parent_id": self.parent_id,
            "text": self.text,
            "status": self.status,
            "provenance": dict(self.provenance),
            "structured_limits": (
                [dict(item) for item in self.structured_limits]
                if self.structured_limits is not None
                else None
            ),
            "structured_limits_state": self.structured_limits_state,
            "structured_limits_source": self.structured_limits_source,
        }


#: 历史要求链恢复上下文（R9-H2 契约 §3.1）；由 RequirementLedger.recovery_context 组装，
#: resolution.py 的 extract_limits_from_context 消费，app.py（B）据此做条件写回。
#: chain 只在完整（complete=True）且当前 active 时可用于恢复；否则调用方不得标 structured。
@dataclass(frozen=True)
class RequirementRecoveryContext:
    requirement_id: str
    version: int
    status: str
    parent_id: str | None
    chain: tuple[RequirementChainNode, ...]
    complete: bool
    reason: str
    chain_digest: str

    def as_dict(self) -> dict:
        return {
            "requirement_id": self.requirement_id,
            "version": int(self.version),
            "status": self.status,
            "parent_id": self.parent_id,
            "chain": [node.as_dict() for node in self.chain],
            "complete": bool(self.complete),
            "reason": self.reason,
            "chain_digest": self.chain_digest,
        }


def _requirement_chain_digest(nodes: Sequence["RequirementChainNode"]) -> str:
    """对 (requirement_id, version, parent_id, text, status) 有序序列做 sha256。

    text 原样参与（用户原话）；不含模型摘要/推理，不含密钥。
    """
    import hashlib

    payload = [
        [
            str(node.requirement_id),
            int(node.version),
            None if node.parent_id is None else str(node.parent_id),
            str(node.text),
            str(node.status),
        ]
        for node in nodes
    ]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class RequirementVersionMismatch(ValueError):
    """模型解析出的目标版本与当前库中的版本不一致（要求已被修正/替换）。"""


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
    #: 要求版本（R9-G 契约 §3）：新要求为 1，修正子要求为 parent.version + 1。
    #: 操作授权记录与审批单都绑定这个版本；版本变化后旧记录不得复用。
    version: int = DEFAULT_REQUIREMENT_VERSION
    #: 结构化限制（R9-H 契约 §6）：tuple（可为空 = 明确无限制）或 None（未知）。
    #: state=legacy/unresolved 时即使有已知新增也不得当作完整限制。
    structured_limits: tuple[dict, ...] | None = None
    structured_limits_state: str = requirement_limits.STATE_LEGACY
    structured_limits_source: str | None = None

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
            "version": int(self.version),
            "structured_limits": (
                [dict(item) for item in self.structured_limits]
                if self.structured_limits is not None
                else None
            ),
            "structured_limits_state": self.structured_limits_state,
            "structured_limits_source": self.structured_limits_source,
        }

    def as_binding(self) -> RequirementBinding:
        return RequirementBinding(requirement_id=self.requirement_id, version=int(self.version))


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
        try:
            version = int(row["version"] or DEFAULT_REQUIREMENT_VERSION)
        except (KeyError, IndexError, TypeError, ValueError):  # pragma: no cover - 旧库/异常行
            version = DEFAULT_REQUIREMENT_VERSION
        structured_state = str(
            _row_value(row, "structured_limits_state", requirement_limits.STATE_LEGACY)
            or requirement_limits.STATE_LEGACY
        )
        if structured_state not in requirement_limits.STRUCTURED_LIMIT_STATES:
            structured_state = requirement_limits.STATE_LEGACY
        structured_source = _row_value(row, "structured_limits_source", None)
        structured = requirement_limits.deserialize_limits(
            _row_value(row, "structured_limits", None)
        )
        return UserRequirement(
            requirement_id=str(row["id"]),
            text=str(row["text"] or ""),
            provenance=provenance,
            parent_id=str(row["parent_id"]) if row["parent_id"] else None,
            status=status,
            limits=_tuple_of_str(limits),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            version=version,
            structured_limits=structured,
            structured_limits_state=structured_state,
            structured_limits_source=(
                None if structured_source is None else str(structured_source)
            ),
        )

    def by_id(self, requirement_id: str) -> UserRequirement | None:
        if not str(requirement_id or "").strip():
            return None
        row = self._row(requirement_id)
        return self._from_row(row) if row is not None else None

    def current(self) -> UserRequirement | None:
        """最近一条 active 要求；没有则 None。

        **仅为只读展示/兼容保留**。R9-G 契约 §3：engine/tools 不得再把"最近一条
        active 要求"当作授权依据；操作必须使用 RequirementBinding（binding_for）。
        """
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

    def binding_for(self, requirement_id: str) -> RequirementBinding | None:
        """返回该要求的版本绑定；**仅 active 要求**返回（契约 §3）。

        操作在开始前应取得绑定并携带到执行/审批/恢复各环节；要求被修正、撤回或
        替换后 binding_for 返回 None，旧版本记录也不能复用。
        """
        requirement = self.by_id(requirement_id)
        if requirement is None or requirement.status != STATUS_ACTIVE:
            return None
        return requirement.as_binding()

    def validate_binding(self, binding: RequirementBinding) -> bool:
        """核对绑定仍是当前库里的 active 同版本要求（用于执行前重核对）。"""
        if not isinstance(binding, RequirementBinding):
            return False
        if not str(binding.requirement_id or "").strip():
            return False
        if not isinstance(binding.version, int) or binding.version < 1:
            return False
        requirement = self.by_id(binding.requirement_id)
        if requirement is None or requirement.status != STATUS_ACTIVE:
            return False
        return int(requirement.version) == int(binding.version)

    # -- 历史要求链恢复（R9-H2 契约 §3.1）------------------------------------

    @staticmethod
    def _chain_node(requirement: UserRequirement) -> RequirementChainNode:
        return RequirementChainNode(
            requirement_id=requirement.requirement_id,
            version=int(requirement.version),
            parent_id=requirement.parent_id,
            text=requirement.text,
            status=requirement.status,
            provenance=dict(requirement.provenance),
            structured_limits=requirement.structured_limits,
            structured_limits_state=requirement.structured_limits_state,
            structured_limits_source=requirement.structured_limits_source,
        )

    def recovery_context(self, requirement_id: str) -> RequirementRecoveryContext:
        """按真实 parent_id 链组装"根 -> 当前"恢复上下文（R9-H2 §3.1）。

        * 只用台账真实父子关系，不看 candidates/最近 active/全部历史消息；
        * 父行缺失、id 循环、版本不连续、当前非 active → complete=False + reason，
          **不抛异常**；
        * text/provenance/state/source 原样；chain_digest 覆盖有序
          (requirement_id, version, parent_id, text, status)。
        """
        empty_digest = _requirement_chain_digest(())
        rid = str(requirement_id or "").strip()
        if not rid:
            return RequirementRecoveryContext(
                requirement_id="",
                version=0,
                status="",
                parent_id=None,
                chain=(),
                complete=False,
                reason="要求 id 为空",
                chain_digest=empty_digest,
            )
        current = self.by_id(rid)
        if current is None:
            return RequirementRecoveryContext(
                requirement_id=rid,
                version=0,
                status="",
                parent_id=None,
                chain=(),
                complete=False,
                reason="要求不存在：" + rid,
                chain_digest=empty_digest,
            )

        reverse_chain: list[UserRequirement] = []
        seen: set[str] = set()
        reason = ""
        node: UserRequirement | None = current
        while node is not None:
            if node.requirement_id in seen:
                reason = "父链存在循环：" + node.requirement_id
                break
            seen.add(node.requirement_id)
            reverse_chain.append(node)
            if not node.parent_id:
                break
            parent = self.by_id(node.parent_id)
            if parent is None:
                reason = "父要求缺失：" + str(node.parent_id)
                break
            if parent.requirement_id in seen:
                reason = "父链存在循环：" + parent.requirement_id
                break
            if int(node.version) != int(parent.version) + 1:
                reason = (
                    "版本不连续：子要求 %s(v=%s) 的父要求 %s(v=%s) 不满足 parent+1"
                    % (
                        node.requirement_id,
                        node.version,
                        parent.requirement_id,
                        parent.version,
                    )
                )
                break
            node = parent

        chain = tuple(
            self._chain_node(item) for item in reversed(reverse_chain)
        )
        complete = not reason
        if complete and current.status != STATUS_ACTIVE:
            complete = False
            reason = "当前要求不是 active（status=" + str(current.status) + "）"
        if not reason:
            reason = ""
        return RequirementRecoveryContext(
            requirement_id=current.requirement_id,
            version=int(current.version),
            status=current.status,
            parent_id=current.parent_id,
            chain=chain,
            complete=complete,
            reason=reason,
            chain_digest=_requirement_chain_digest(chain),
        )

    # -- 结构化限制（R9-H 契约 §6）------------------------------------------

    def set_structured_limits(
        self,
        requirement_id: str,
        limits: Sequence[dict] | None,
        *,
        source: object = "manual_recovery",
        state: str = requirement_limits.STATE_STRUCTURED,
        expected_version: int | None = None,
        expected_status: str | None = None,
        expected_chain_digest: str | None = None,
    ) -> UserRequirement:
        """写入/恢复一条要求的结构化限制（恢复入口 + apply_resolution 共用）。

        * state=structured：limits 必须是数组（空数组 = 明确无限制），持久化为 JSON；
        * state=unresolved：可保存**已知新增**，但状态保持未解决（不得声称完整）；
        * state=legacy：不接受结构化内容（列置 NULL）；
        * limits=None 且 state!=legacy：保留该要求已有的 known limits（避免恢复失败时
          被空数组误清空）；显式 () 表示"已知集合为空"。
        * **条件写入（R9-H2 §3.1）**：expected_version / expected_status /
          expected_chain_digest 在**同一事务**内重新读取并核对（chain_digest 用
          recovery_context 现场重算），任一不匹配抛 RequirementVersionMismatch；
          UPDATE 同时带 id/version/status 条件并核对 rowcount，绝不覆盖新版本/已撤回要求。
        * 展平文字 limits 同步追加可读文本（保留历史展示/审查材料），不覆盖旧文本；
        * 非法结构抛 ValueError（不写到库里，不静默降级为"无限制"）。
        """
        requirement = self.by_id(requirement_id)
        if requirement is None:
            raise KeyError("用户要求不存在：" + str(requirement_id))
        target_state = str(state or requirement_limits.STATE_LEGACY)
        if target_state not in requirement_limits.STRUCTURED_LIMIT_STATES:
            raise ValueError("非法结构化限制状态：" + target_state)
        if target_state == requirement_limits.STATE_LEGACY:
            known: tuple[dict, ...] | None = None
            structured_json = None
        else:
            raw_limits: Sequence[dict]
            if limits is None:
                raw_limits = requirement.structured_limits or ()
            else:
                raw_limits = limits
            known = requirement_limits.normalize_limits(list(raw_limits or ()))
            known = requirement_limits.attach_program_bindings(
                known,
                requirement_id=str(requirement.requirement_id),
                version=int(requirement.version),
                status=str(requirement.status),
            )
            structured_json = requirement_limits.serialize_limits(known)
        flattened = [
            _flatten_limit(dict(item))
            for item in (known or ())
        ]
        limits_text = list(requirement.limits)
        for piece in flattened:
            if piece and piece not in limits_text:
                limits_text.append(piece[:MAX_PERSISTED_LIMIT_CHARS])
        expected_chain_value = (
            None if expected_chain_digest is None else str(expected_chain_digest)
        )
        with transaction(self.conn):
            row = self._row(str(requirement_id))
            if row is None:
                raise RequirementVersionMismatch(
                    "用户要求已被删除/替换：" + str(requirement_id)
                )
            current = self._from_row(row)
            if expected_version is not None and int(current.version) != int(expected_version):
                raise RequirementVersionMismatch(
                    "要求版本已变化（期望=%r，当前=%r），拒绝写入结构化限制"
                    % (expected_version, current.version)
                )
            if expected_status is not None and str(current.status) != str(expected_status):
                raise RequirementVersionMismatch(
                    "要求状态已变化（期望=%r，当前=%r），拒绝写入结构化限制"
                    % (expected_status, current.status)
                )
            if expected_chain_value is not None:
                ctx = self.recovery_context(str(requirement_id))
                if ctx.chain_digest != expected_chain_value:
                    raise RequirementVersionMismatch(
                        "要求链已变化（期望 digest=%s，当前=%s），拒绝写入旧恢复结果"
                        % (expected_chain_value[:16], ctx.chain_digest[:16])
                    )
            cursor = self.conn.execute(
                "UPDATE user_requirements SET structured_limits = ?,"
                " structured_limits_state = ?, structured_limits_source = ?,"
                " limits = ?, updated_at = ?"
                " WHERE id = ? AND version = ? AND status = ?",
                (
                    structured_json,
                    target_state,
                    None if source is None else str(source)[:300],
                    _dumps(limits_text),
                    _now_iso(),
                    str(requirement_id),
                    int(current.version),
                    str(current.status),
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise RequirementVersionMismatch(
                    "要求行在事务内已变化（更新未命中），拒绝写入结构化限制"
                )
        reloaded = self.by_id(requirement_id)
        if reloaded is None:  # pragma: no cover - 写入后读不到说明库已损坏
            raise RuntimeError("结构化限制写入后无法读回（数据库异常）")
        return reloaded

    def requirement_needs_binding_recovery(self, requirement_id: str) -> bool:
        """该要求是否为 structured 且有未绑定的 program_name 对象（只读，不查找）。

        R9-H11 §2：历史 structured-but-unbound 是"需要专用恢复/澄清"，不是无关。
        """
        requirement = self.by_id(str(requirement_id or ""))
        if requirement is None:
            return False
        if str(requirement.structured_limits_state or "") != requirement_limits.STATE_STRUCTURED:
            return False
        return requirement_limits.needs_binding_recovery(requirement.structured_limits)

    def apply_binding_recovery(
        self,
        requirement_id: str,
        limits: Sequence[dict] | None,
        *,
        source: object,
        expected_version: int | None = None,
        expected_status: str | None = None,
        expected_chain_digest: str | None = None,
    ) -> UserRequirement:
        """专用恢复写回：只持久化**已完整绑定**的限制集合（R9-H11 §3）。

        * 写前要求每个 program_name 对象都有有效 program_binding（path+identity）；
          任一缺失 → ValueError，一行都不写（保留原限制/known/来源）；
        * id/version/status/chain_digest 由 set_structured_limits 在同一事务内条件核对，
          恢复等待期间要求被修改/撤回时抛 RequirementVersionMismatch；
        * 只写限制，不生成普通授权、不调用普通独立授权审查。
        """
        known = requirement_limits.normalize_limits(list(limits or ()))
        if not requirement_limits.binding_recovery_complete(known):
            raise ValueError("程序对象尚未全部绑定，拒绝写回恢复结果")
        return self.set_structured_limits(
            str(requirement_id),
            known,
            source=source,
            state=requirement_limits.STATE_STRUCTURED,
            expected_version=expected_version,
            expected_status=expected_status,
            expected_chain_digest=expected_chain_digest,
        )

    def structured_limits_for(self, requirement_id: str) -> tuple[dict, ...] | None:
        """按 requirement_id 重新加载结构化限制（执行前重核对/恢复用）。"""
        requirement = self.by_id(requirement_id)
        return requirement_limits.limits_from_requirement(requirement)

    def legacy_requirements(self, *, limit: int = 50) -> list[UserRequirement]:
        """待重新理解/恢复的历史要求（state=legacy/unresolved），供模型重新理解。"""
        count = max(1, min(int(limit or 50), 500))
        rows = self.conn.execute(
            "SELECT * FROM user_requirements WHERE structured_limits_state IN (?, ?)"
            " ORDER BY updated_at DESC, rowid DESC LIMIT ?",
            (
                requirement_limits.STATE_LEGACY,
                requirement_limits.STATE_UNRESOLVED,
                count,
            ),
        ).fetchall()
        return [self._from_row(row) for row in rows]

    def candidates(self, *, limit: int = 12) -> list[dict]:
        """给模型要求解析用的有限候选（契约 §3：id/原话摘录/版本/时间）。

        只返回 active 要求，按最近更新倒序；文本只给有界摘录，不暴露内部推理/材料。
        """
        count = max(1, min(int(limit or 12), 50))
        rows = self.conn.execute(
            "SELECT * FROM user_requirements WHERE status = ?"
            " ORDER BY updated_at DESC, rowid DESC LIMIT ?",
            (STATUS_ACTIVE, count),
        ).fetchall()
        result: list[dict] = []
        for row in rows:
            requirement = self._from_row(row)
            text = requirement.text.strip()
            if len(text) > 300:
                text = text[:295] + "…"
            result.append(
                {
                    "requirement_id": requirement.requirement_id,
                    "version": int(requirement.version),
                    "text_excerpt": text,
                    "created_at": requirement.created_at,
                    "updated_at": requirement.updated_at,
                    "parent_id": requirement.parent_id,
                    "topic_id": str(requirement.provenance.get("topic_id") or "") or None,
                }
            )
        return result

    # -- 写入 ---------------------------------------------------------------

    def record_user_message(
        self,
        *,
        text: object,
        message_id: object = None,
        turn_id: object = None,
        topic_id: object = None,
        structured_state: str = requirement_limits.STATE_STRUCTURED,
        structured_limits: Sequence[dict] = (),
        structured_source: object = "user_message",
    ) -> UserRequirement:
        """把一条用户消息登记为 active 要求（每条用户消息一个，不自动复用旧要求）。

        说明：这里**不**自动 supersede 其它 active 要求。每条消息都是独立要求；
        是否延续原要求由调用方显式表达（修正用 add_correction；替换用 supersede）。
        授权记录按 requirement_id 绑定，因此新要求天然不会命中旧记录。

        R9-H2 §3.1：结构化限制状态/内容/来源由调用方显式给出：
        * 默认 structured + 空数组 + "user_message"（兼容旧直接调用）；
        * **生产 fallback 必须显式传 STATE_UNRESOLVED**（解析失败不是"确认无限制"）；
        * state=legacy 时不接受结构化内容（列 NULL）；非法 state 抛 ValueError。
        """
        requirement_id = "req_" + uuid.uuid4().hex[:16]
        now = _now_iso()
        provenance = {
            "kind": "user_message",
            "message_id": None if message_id is None else str(message_id),
            "turn_id": None if turn_id is None else str(turn_id),
            "topic_id": None if topic_id is None else str(topic_id),
        }
        target_state = str(structured_state or requirement_limits.STATE_STRUCTURED)
        if target_state not in requirement_limits.STRUCTURED_LIMIT_STATES:
            raise ValueError("非法结构化限制状态：" + target_state)
        if target_state == requirement_limits.STATE_LEGACY:
            known: tuple[dict, ...] | None = None
            structured_json = None
        else:
            known = requirement_limits.normalize_limits(list(structured_limits or ()))
            if known:
                known = requirement_limits.attach_program_bindings(
                    known,
                    requirement_id=requirement_id,
                    version=1,
                    status=STATUS_ACTIVE,
                )
            structured_json = requirement_limits.serialize_limits(known)
        limits_text: list[str] = []
        for item in known or ():
            piece = _flatten_limit(dict(item))
            if piece and piece not in limits_text:
                limits_text.append(piece[:MAX_PERSISTED_LIMIT_CHARS])
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO user_requirements"
                " (id, text, provenance, parent_id, status, limits, turn_id, topic_id,"
                "  created_at, updated_at, version, structured_limits,"
                "  structured_limits_state, structured_limits_source)"
                " VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    requirement_id,
                    str(text if text is not None else ""),
                    _dumps(provenance),
                    STATUS_ACTIVE,
                    _dumps(limits_text),
                    provenance["turn_id"],
                    provenance["topic_id"],
                    now,
                    now,
                    DEFAULT_REQUIREMENT_VERSION,
                    structured_json,
                    target_state,
                    None if structured_source is None else str(structured_source)[:300],
                ),
            )
        stored = self.by_id(requirement_id)
        if stored is None:  # pragma: no cover - 写入后读不到说明库已损坏
            raise RuntimeError("用户要求写入后无法读回（数据库异常）")
        return stored

    @staticmethod
    def _child_structured_limits(
        parent: UserRequirement,
        *,
        added: Sequence[dict],
        removed: Sequence[dict],
    ) -> tuple[tuple[dict, ...] | None, str, str | None]:
        """计算 amend 子要求的结构化限制与状态（R9-H4 §4）。

        返回 (child_limits, child_state, ambiguity_code)。
        * 父 structured：精确合并 added/removed，结果 structured；
        * 父 legacy/unresolved 但已有 known limits：**继承 known** 再做精确合并，
          状态保持 legacy/unresolved（部分已知不等于完整）；
        * removed 无法精确表达：保留父 known，标 unresolved，返回 ambiguity_code；
        * 父没有 known 列：只能保存本次新增（若有），状态 unresolved。
        """
        parent_known = requirement_limits.known_limits_from_requirement(parent)
        parent_state = str(
            getattr(parent, "structured_limits_state", requirement_limits.STATE_LEGACY)
            or requirement_limits.STATE_LEGACY
        )
        if parent_known is None:
            added_limits = requirement_limits.normalize_limits(list(added or ()))
            child_state = (
                parent_state
                if parent_state != requirement_limits.STATE_STRUCTURED
                else requirement_limits.STATE_UNRESOLVED
            )
            ambiguity = "unknown_parent"
            if removed:
                ambiguity = "removed_unknown_parent"
            return (
                added_limits if added_limits else None,
                child_state,
                ambiguity,
            )
        try:
            merged = requirement_limits.merge_limits(
                parent_known, added=added, removed=removed
            )
        except requirement_limits.LimitRemovalAmbiguous:
            return parent_known, requirement_limits.STATE_UNRESOLVED, "removed_ambiguous"
        child_state = (
            requirement_limits.STATE_STRUCTURED
            if parent_state == requirement_limits.STATE_STRUCTURED
            else parent_state
        )
        ambiguity = None
        if parent_state != requirement_limits.STATE_STRUCTURED:
            ambiguity = "partial_parent_known"
        return merged, child_state, ambiguity

    def add_correction(
        self,
        requirement_id: str,
        text: object,
        *,
        message_id: object = None,
        extra_limits: Sequence[str] = (),
        structured_limits: Sequence[dict] = (),
        removed_limits: Sequence[dict] = (),
        structured_state: str | None = None,
        structured_source: str | None = None,
    ) -> UserRequirement:
        """追加一条用户修正：新建子要求（version=parent+1），父要求 superseded。

        契约语义「修正 = append」（R9-G §3 / R9-H §6）：
        * 原要求行保留（历史可回溯），只改状态；
        * 子要求 text 是用户这次修正的原话（调用方应传真实用户消息），parent_id 指向父要求；
        * 子要求 version = parent.version + 1；父链授权记录立即失效（旧版本不得复用）；
        * limits（展平文字）继承父要求并追加本次修正原话与调用方提供的额外限制文本；
        * **结构化限制**：父 state=structured 时默认继承并与新增/解除合并（只补充一句话
          不得清空父限制；明确 removed 才移除）；父 legacy/unresolved 时保留未解决状态，
          可保存已知新增但不得声称完整；apply_resolution 传入已算好的最终状态时不重复合并。
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
            limits.append(correction_text[:MAX_PERSISTED_LIMIT_CHARS])
        for extra in extra_limits or ():
            piece = str(extra or "").strip()
            if piece:
                limits.append(piece[:MAX_PERSISTED_LIMIT_CHARS])
        child_version = int(parent.version) + 1

        added_structured = list(structured_limits or ())
        removed_structured = list(removed_limits or ())
        if structured_state is not None:
            child_state = str(structured_state or requirement_limits.STATE_LEGACY)
            if child_state not in requirement_limits.STRUCTURED_LIMIT_STATES:
                raise ValueError("非法结构化限制状态：" + child_state)
            child_structured: tuple[dict, ...] | None
            if child_state == requirement_limits.STATE_STRUCTURED:
                child_structured = requirement_limits.normalize_limits(added_structured)
            elif added_structured:
                child_structured = requirement_limits.normalize_limits(added_structured)
            else:
                child_structured = None
            child_source = structured_source
            # apply_resolution 已经把最终集合算好；展平文字仍用于历史可读性。
            for item in child_structured or ():
                flattened = _flatten_limit(dict(item))
                if flattened and flattened not in limits:
                    limits.append(flattened[:MAX_PERSISTED_LIMIT_CHARS])
        else:
            child_structured, child_state, ambiguity = self._child_structured_limits(
                parent, added=added_structured, removed=removed_structured
            )
            child_source = structured_source or parent.structured_limits_source
            if child_state != requirement_limits.STATE_STRUCTURED:
                child_source = (
                    (child_source or "unresolved_parent")
                    + ("" if ambiguity is None else ";" + ambiguity)
                )[:300]
            for item in added_structured:
                flattened = _flatten_limit(dict(item))
                if flattened and flattened not in limits:
                    limits.append(flattened[:MAX_PERSISTED_LIMIT_CHARS])
        if child_structured is not None:
            child_structured = requirement_limits.attach_program_bindings(
                child_structured,
                requirement_id=requirement_id_new,
                version=child_version,
                status=STATUS_ACTIVE,
            )
        structured_json = (
            requirement_limits.serialize_limits(child_structured)
            if child_structured is not None
            else None
        )
        provenance = {
            "kind": "user_correction",
            "message_id": None if message_id is None else str(message_id),
            "turn_id": str(parent.provenance.get("turn_id") or "") or None,
            "topic_id": str(parent.provenance.get("topic_id") or "") or None,
            "parent_requirement_id": parent.requirement_id,
            "parent_version": int(parent.version),
        }
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO user_requirements"
                " (id, text, provenance, parent_id, status, limits, turn_id, topic_id,"
                "  created_at, updated_at, version, structured_limits,"
                "  structured_limits_state, structured_limits_source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    child_version,
                    structured_json,
                    child_state,
                    child_source,
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

    # -- 模型要求解析的落实与审计（R9-G 契约 §3）----------------------------

    def apply_resolution(
        self,
        resolution: "ResolutionOutcome",
        *,
        message_id: object = None,
        turn_id: object = None,
        topic_id: object = None,
        message: object = None,
        latency_ms: int | None = None,
    ) -> AppliedResolution:
        """按模型解析结果落实 new / amend / withdraw（目标身份、active、版本由后端核对）。

        * resolution 只提供语义判断；真实用户原话由调用方用 message 传入（优先），
          没有时退回 resolution.corrections（提示词要求保留原话要点、不得改写含义）；
        * amend 生成 version=parent+1 的子要求；withdraw 保留历史行只改状态；
        * 目标不存在/非 active/版本不符 → 抛错（不静默新建），并写 rejected 审计；
        * 成功写 requirement_resolutions 审计（relation/target/outcome/reason/model/prompt_version）。
        """
        from agent.security.resolution import RequirementRelation

        relation_raw = getattr(resolution, "relation", None)
        relation = str(getattr(relation_raw, "value", relation_raw) or "").strip().lower()
        target_id = str(getattr(resolution, "target_requirement_id", None) or "").strip() or None
        target_version = getattr(resolution, "target_version", None)
        corrections = tuple(
            str(item).strip() for item in (getattr(resolution, "corrections", ()) or ()) if str(item).strip()
        )
        raw_limits = list(getattr(resolution, "limits", ()) or ())
        raw_removed = list(getattr(resolution, "removed_limits", ()) or ())
        # R9-H4 §2：逐条容错；个别非法条目只标 unresolved，不能清空全部可靠限制。
        structured_limits, limits_invalid = _normalize_limits_tolerant(raw_limits)
        removed_limits, removed_invalid = _normalize_limits_tolerant(raw_removed)
        reason = str(getattr(resolution, "reason", "") or "")[:500]
        model = str(getattr(resolution, "model", "") or "") or None
        prompt_version = str(getattr(resolution, "prompt_version", "") or "") or None
        limit_state = (
            requirement_limits.STATE_UNRESOLVED
            if limits_invalid
            else requirement_limits.STATE_STRUCTURED
        )
        limit_source = "resolution:%s:%s" % (
            model or "unknown",
            prompt_version or "unknown",
        )
        resolution_id = "reqres_" + uuid.uuid4().hex[:16]
        relation_values = {
            RequirementRelation.NEW.value,
            RequirementRelation.AMEND.value,
            RequirementRelation.WITHDRAW.value,
        }
        try:
            if relation not in relation_values:
                raise ValueError("未知的要求关系：" + relation)
            if relation == RequirementRelation.NEW.value:
                text = str(message) if message is not None else "\n".join(corrections)
                requirement = self.record_user_message(
                    text=text, message_id=message_id, turn_id=turn_id, topic_id=topic_id
                )
                new_state = (
                    requirement_limits.STATE_UNRESOLVED
                    if limits_invalid
                    else limit_state
                )
                requirement = self.set_structured_limits(
                    requirement.requirement_id,
                    structured_limits,
                    source=limit_source + (";invalid_limits" if limits_invalid else ""),
                    state=new_state,
                )
                applied = AppliedResolution(
                    resolution_id=resolution_id,
                    relation=relation,
                    requirement=requirement,
                    target_requirement_id=None,
                    superseded_requirement_id=None,
                    limits=structured_limits,
                    corrections=corrections,
                    reason=reason,
                    limits_state=new_state,
                    structured_limits=(
                        structured_limits
                        if structured_limits or not limits_invalid
                        else None
                    ),
                )
            elif relation == RequirementRelation.AMEND.value:
                if not target_id:
                    raise ValueError("amend 必须给出 target_requirement_id")
                parent = self._require_active_target(target_id, target_version)
                if not corrections and message is None:
                    raise ValueError("amend 缺少用户补充/修正内容")
                child_text = str(message) if message is not None else "\n".join(corrections)
                parent_known = requirement_limits.known_limits_from_requirement(parent)
                parent_state = str(
                    getattr(parent, "structured_limits_state", requirement_limits.STATE_LEGACY)
                    or requirement_limits.STATE_LEGACY
                )
                ambiguity: str | None = None
                if parent_known is None:
                    # 父限制列未知：可保存本次可靠新增，但不得声称完整；removed 无法核对。
                    child_limits = structured_limits if structured_limits else None
                    child_state = (
                        parent_state
                        if parent_state != requirement_limits.STATE_STRUCTURED
                        else requirement_limits.STATE_UNRESOLVED
                    )
                    codes: list[str] = []
                    if limits_invalid:
                        codes.append("invalid_limits")
                    if removed_invalid:
                        codes.append("removed_invalid")
                    if removed_limits:
                        codes.append("removed_unknown_parent")
                    ambiguity = ";".join(codes) or "unknown_parent"
                else:
                    try:
                        child_limits = requirement_limits.merge_limits(
                            parent_known,
                            added=structured_limits,
                            removed=removed_limits,
                        )
                    except requirement_limits.LimitRemovalAmbiguous:
                        # 无法精确确定解除范围：保留父 known，标 unresolved，不猜测。
                        child_limits = parent_known
                        child_state = requirement_limits.STATE_UNRESOLVED
                        ambiguity = "removed_ambiguous"
                    else:
                        codes = []
                        if limits_invalid:
                            codes.append("invalid_limits")
                        if removed_invalid:
                            codes.append("removed_invalid")
                        if codes:
                            child_state = requirement_limits.STATE_UNRESOLVED
                            ambiguity = ";".join(codes)
                        elif parent_state != requirement_limits.STATE_STRUCTURED:
                            child_state = parent_state
                            ambiguity = "partial_parent_known"
                        else:
                            child_state = requirement_limits.STATE_STRUCTURED
                child_source = limit_source
                if child_state != requirement_limits.STATE_STRUCTURED:
                    child_source += ";" + (ambiguity or "unresolved_parent")
                child = self.add_correction(
                    parent.requirement_id,
                    child_text,
                    message_id=message_id,
                    structured_limits=tuple(child_limits or ()),
                    structured_state=child_state,
                    structured_source=child_source[:300],
                )
                applied = AppliedResolution(
                    resolution_id=resolution_id,
                    relation=relation,
                    requirement=child,
                    target_requirement_id=parent.requirement_id,
                    superseded_requirement_id=parent.requirement_id,
                    limits=tuple(child_limits or ()),
                    corrections=corrections,
                    reason=reason,
                    limits_state=child_state,
                    structured_limits=child_limits,
                )
            else:
                if not target_id:
                    raise ValueError("withdraw 必须给出 target_requirement_id")
                parent = self._require_active_target(target_id, target_version)
                self.withdraw(parent.requirement_id)
                applied = AppliedResolution(
                    resolution_id=resolution_id,
                    relation=relation,
                    requirement=self.by_id(parent.requirement_id),
                    target_requirement_id=parent.requirement_id,
                    superseded_requirement_id=None,
                    limits=(),
                    corrections=corrections,
                    reason=reason,
                    limits_state=parent.structured_limits_state,
                    structured_limits=parent.structured_limits,
                )
        except Exception as exc:  # noqa: BLE001 - 失败要留审计并原样抛出，绝不静默新建
            self._record_resolution(
                resolution_id=resolution_id,
                message_id=message_id,
                turn_id=turn_id,
                relation=relation or "unknown",
                target_requirement_id=target_id,
                outcome=RESOLUTION_OUTCOME_REJECTED,
                reason=reason,
                model=model,
                prompt_version=prompt_version,
                latency_ms=latency_ms,
                error=_sanitize_error_text(exc),
            )
            raise
        self._record_resolution(
            resolution_id=resolution_id,
            message_id=message_id,
            turn_id=turn_id,
            relation=relation,
            target_requirement_id=target_id,
            outcome=RESOLUTION_OUTCOME_APPLIED,
            reason=reason,
            model=model,
            prompt_version=prompt_version,
            latency_ms=latency_ms,
            error=None,
        )
        return applied

    def record_resolution_failure(
        self,
        *,
        error: object,
        message_id: object = None,
        turn_id: object = None,
        relation: object = None,
        target_requirement_id: object = None,
        model: object = None,
        prompt_version: object = None,
        latency_ms: int | None = None,
    ) -> str:
        """登记一次要求解析失败（模型不可用/输出非法）；调用方随后按"新要求"继续。

        契约 §3：解析失败不关键词兜底；审计保留失败事实，供授权审查看到
        requirement_resolution_status="unavailable"。
        """
        resolution_id = "reqres_" + uuid.uuid4().hex[:16]
        self._record_resolution(
            resolution_id=resolution_id,
            message_id=message_id,
            turn_id=turn_id,
            relation=str(relation or "unknown"),
            target_requirement_id=(
                None if target_requirement_id is None else str(target_requirement_id)
            ),
            outcome=RESOLUTION_OUTCOME_UNAVAILABLE,
            reason="",
            model=None if model is None else str(model),
            prompt_version=None if prompt_version is None else str(prompt_version),
            latency_ms=latency_ms,
            error=_sanitize_error_text(error),
        )
        return resolution_id

    def resolutions_for(
        self, *, target_requirement_id: str | None = None, limit: int = 50
    ) -> list[dict]:
        """读取要求解析审计（测试/排查用）；不包含模型推理全文。"""
        count = max(1, min(int(limit or 50), 500))
        if target_requirement_id:
            rows = self.conn.execute(
                "SELECT * FROM requirement_resolutions WHERE target_requirement_id = ?"
                " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (str(target_requirement_id), count),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM requirement_resolutions ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (count,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _require_active_target(self, requirement_id: str, expected_version: object) -> UserRequirement:
        target = self.by_id(requirement_id)
        if target is None:
            raise KeyError("目标用户要求不存在：" + str(requirement_id))
        if target.status != STATUS_ACTIVE:
            raise ValueError("目标用户要求不是 active（status=" + target.status + "），不能修正/撤回")
        if not isinstance(expected_version, int) or int(expected_version) != int(target.version):
            raise RequirementVersionMismatch(
                "目标用户要求版本不符（请求=%r，当前=%r），可能已被修正/替换"
                % (expected_version, target.version)
            )
        return target

    def _append_persisted_limits(
        self, requirement_id: str, limits: Sequence[str]
    ) -> UserRequirement:
        requirement = self.by_id(requirement_id)
        if requirement is None:
            raise KeyError("用户要求不存在：" + str(requirement_id))
        merged = list(requirement.limits)
        for piece in limits:
            text = str(piece or "").strip()
            if text:
                merged.append(text[:MAX_PERSISTED_LIMIT_CHARS])
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE user_requirements SET limits = ?, updated_at = ? WHERE id = ?",
                (_dumps(merged), _now_iso(), str(requirement_id)),
            )
        reloaded = self.by_id(requirement_id)
        if reloaded is None:  # pragma: no cover - 写入后读不到说明库已损坏
            raise RuntimeError("用户要求限制写入后无法读回（数据库异常）")
        return reloaded

    def _record_resolution(
        self,
        *,
        resolution_id: str,
        message_id: object,
        turn_id: object,
        relation: str,
        target_requirement_id: str | None,
        outcome: str,
        reason: str,
        model: str | None,
        prompt_version: str | None,
        latency_ms: int | None,
        error: str | None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO requirement_resolutions"
            " (id, message_id, turn_id, relation, target_requirement_id, outcome,"
            "  reason, model, prompt_version, latency_ms, error, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                resolution_id,
                None if message_id is None else str(message_id),
                None if turn_id is None else str(turn_id),
                str(relation or "unknown"),
                None if target_requirement_id is None else str(target_requirement_id),
                str(outcome or RESOLUTION_OUTCOME_APPLIED),
                None if reason is None else str(reason)[:500],
                None if model is None else str(model)[:200],
                None if prompt_version is None else str(prompt_version)[:100],
                None if latency_ms is None else int(latency_ms),
                None if error is None else str(error)[:300],
                _now_iso(),
            ),
        )

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
    #: 记录绑定的要求版本（R9-G 迁移 57）；旧记录为 None 时不得复用（契约 §10）。
    requirement_version: int | None = None

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
            "requirement_version": self.requirement_version,
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
            requirement_version=(
                int(row["requirement_version"]) if row["requirement_version"] is not None else None
            ),
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
        另外要求：record.decision == allow、未撤销、未过期、对应 requirement 仍 active，
        **且记录绑定的 requirement_version 等于当前版本**（R9-G 契约 §3、§10：
        修正后旧版本记录不能继续复用；缺版本的历史记录也不得默认视为有效）。
        """
        req_id = str(requirement_id or "").strip()
        if not req_id:
            return None
        rows = self.conn.execute(
            "SELECT r.* FROM authorization_records AS r"
            " JOIN user_requirements AS u ON u.id = r.requirement_id"
            " WHERE r.requirement_id = ? AND r.operation_kind = ? AND r.fingerprint = ?"
            "   AND r.decision = 'allow' AND r.revoked_at IS NULL AND u.status = ?"
            "   AND r.requirement_version = u.version"
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
        requirement_version = record.requirement_version
        if requirement_version is None:
            # 要求版本是复用/失效的权威依据：调用方没填时从台账补齐（不猜、不放宽）。
            row = self.conn.execute(
                "SELECT version, status FROM user_requirements WHERE id = ?",
                (str(record.requirement_id),),
            ).fetchone()
            if row is None:
                raise ValueError("授权记录对应的用户要求不存在，无法建立版本绑定")
            requirement_version = int(row["version"] or DEFAULT_REQUIREMENT_VERSION)
        self.conn.execute(
            "INSERT INTO authorization_records"
            " (id, requirement_id, requirement_version, operation_kind, fingerprint,"
            "  decision, reason, allowed_operations, objects, limits, source, mode,"
            "  created_at, expires_at, revoked_at, review_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.record_id,
                record.requirement_id,
                int(requirement_version),
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
        requirement_version=int(getattr(requirement, "version", DEFAULT_REQUIREMENT_VERSION) or 1),
    )


__all__ = [
    "AppliedResolution",
    "AuthorizationLedger",
    "AuthorizationRecord",
    "DEFAULT_REQUIREMENT_VERSION",
    "RequirementBinding",
    "RequirementLedger",
    "RequirementVersionMismatch",
    "RESOLUTION_OUTCOME_APPLIED",
    "RESOLUTION_OUTCOME_REJECTED",
    "RESOLUTION_OUTCOME_UNAVAILABLE",
    "SOURCE_MODEL_REVIEW",
    "SOURCE_USER_APPROVAL",
    "STATUS_ACTIVE",
    "STATUS_SUPERSEDED",
    "STATUS_WITHDRAWN",
    "UserRequirement",
    "authorization_record_from_outcome",
]
