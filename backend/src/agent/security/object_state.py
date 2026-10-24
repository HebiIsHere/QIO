"""共同对象状态判定（R9-H11 契约 §2/§3，冻结接口）。

为什么需要这个模块：生产代码过去把对象判定压成布尔值（"匹配 / 不匹配"），
于是"绑定缺失（UNBOUND）/ 绑定失效（INVALID）/ 事实不足（UNKNOWN）"全部退化成
False（=无关），再被模式规则或普通审批放行。本模块把判定结果做成结构化状态：

* MATCH     有效命中（应拒绝/应恢复）；
* UNRELATED **唯一**允许按模式继续的状态（必须有可靠依据）；
* UNBOUND   绑定缺失（明确禁止但保存时没形成对象绑定，需专用恢复/澄清）；
* INVALID   绑定失效（原路径对象被替换，或编号相同但生命周期不同）；
* UNKNOWN   事实不足（失败关闭，不能当无关）。

身份 / 生命周期比较只有 compare_bound_identity 一个入口（契约 §2/§3.4）：

* 只看操作系统给的文件身份（volume+file_id）与生命周期依据（birth_key）；
* 绝不用内容摘要 / 大小 / mtime / 名称相似 / 调用方自报冒充生命周期；
* 事实不可读时返回 UNKNOWN 语义（identity_unavailable /
  lifecycle_unverifiable），不退化成 False。

本模块是纯判定：不写数据库、不补建绑定、不创建文件；resolve_program_name
只是当前 PATH 的只读澄清（契约 §3.5）。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from agent.security.effects import DECISION_PROGRAM_NAME_UNRESOLVED
from agent.services import file_identity

__all__ = [
    "IdentityComparison",
    "LimitAssessment",
    "ObjectDecision",
    "ObjectFact",
    "ObjectState",
    "ProgramResolution",
    "compare_bound_identity",
    "decide_object",
    "decision_code_for",
    "resolve_path",
    "resolve_program_name",
    "same_path_text",
]

#: 审计序列化里单个字符串的上界（路径/身份/说明文字都可能超长；审计必须可序列化）。
MAX_AUDIT_CHARS = 512

#: IdentityComparison.state 的稳定取值（契约 §2）。
IDENTITY_SAME = "same"
IDENTITY_DIFFERENT_OBJECT = "different_object"
IDENTITY_DIFFERENT_LIFECYCLE = "different_lifecycle"
IDENTITY_LIFECYCLE_UNVERIFIABLE = "lifecycle_unverifiable"
IDENTITY_UNAVAILABLE = "identity_unavailable"

#: 结构化决策码（契约 §2/§5；授权行为只认这些稳定字符串）。
DECISION_PROGRAM_BINDING_INVALIDATED = "program_binding_invalidated"
DECISION_OBJECT_STATE_UNCONFIRMED = "object_state_unconfirmed"


class ObjectState(str, Enum):
    """对象判定状态（契约 §2；绝不返回布尔）。"""

    MATCH = "match"  # 有效匹配
    UNRELATED = "unrelated"  # 有可靠依据证明无关（唯一允许按模式继续的状态）
    UNBOUND = "unbound"  # 绑定缺失：明确禁止但保存时未形成对象绑定
    INVALID = "invalid"  # 绑定失效：原路径对象被替换，或编号相同但生命周期不同
    UNKNOWN = "unknown"  # 事实不足，无法可靠判断


@dataclass(frozen=True)
class ObjectFact:
    """一条可审计的结构化事实；equal=None 表示不可比较（不能当 False）。"""

    field: str
    expected: str | None = None
    observed: str | None = None
    source: str = ""
    equal: bool | None = None

    def as_dict(self) -> dict:
        return {
            "field": _bounded(self.field),
            "expected": _bounded(self.expected),
            "observed": _bounded(self.observed),
            "source": _bounded(self.source),
            "equal": self.equal,
        }


@dataclass(frozen=True)
class IdentityComparison:
    """身份 + 生命周期的结构化比较结果（匹配与失效检查共用）。"""

    state: str
    facts: tuple[ObjectFact, ...] = ()

    def as_dict(self) -> dict:
        return {
            "state": _bounded(self.state),
            "facts": [fact.as_dict() for fact in self.facts],
        }


@dataclass(frozen=True)
class ProgramResolution:
    """程序名称的只读澄清结果（不持久化、不补建绑定、不改变历史 JSON）。"""

    name: str
    path: str | None = None
    identity_key: str | None = None
    birth_key: str | None = None
    source: str = "path_lookup"
    error: str = ""

    @property
    def resolved(self) -> bool:
        return bool(self.path and self.identity_key)


@dataclass(frozen=True)
class ObjectDecision:
    """一次"限制对象 × 本次真实影响对象"的判定（契约 §2/§3）。"""

    state: ObjectState
    basis: str
    operation: str | None = None
    object_kind: str | None = None
    object_value: str | None = None
    facts: tuple[ObjectFact, ...] = ()
    detail: str = ""

    @property
    def matched(self) -> bool:
        return self.state is ObjectState.MATCH

    @property
    def unrelated(self) -> bool:
        return self.state is ObjectState.UNRELATED

    @property
    def needs_recovery(self) -> bool:
        return self.state in (ObjectState.UNBOUND, ObjectState.INVALID)

    @property
    def fail_closed(self) -> bool:
        return self.state is ObjectState.UNKNOWN

    def as_dict(self) -> dict:
        """审计序列化：facts 全部为有界字符串，不含密钥/自由文本。"""
        return {
            "state": _state_value(self.state),
            "basis": _bounded(self.basis),
            "operation": _bounded(self.operation),
            "object_kind": _bounded(self.object_kind),
            "object_value": _bounded(self.object_value),
            "facts": [fact.as_dict() for fact in self.facts],
            "detail": _bounded(self.detail),
        }


@dataclass(frozen=True)
class LimitAssessment:
    """一条限制的聚合评估结果（Lead 的引擎聚合消费，契约 §4）。"""

    decisions: tuple[ObjectDecision, ...] = ()
    reason: str = ""
    decision_code: str | None = None

    @property
    def conflict(self) -> bool:
        """是否有任何命中/未确定/失效（全部 UNRELATED 才算不冲突）。"""
        return any(decision.state is not ObjectState.UNRELATED for decision in self.decisions)

    def as_dict(self) -> dict:
        return {
            "conflict": self.conflict,
            "reason": _bounded(self.reason),
            "decision_code": _bounded(self.decision_code),
            "decisions": [decision.as_dict() for decision in self.decisions],
        }


# ---------------------------------------------------------------------------
# 有界文本 / 内部小工具
# ---------------------------------------------------------------------------


def _clean(value: object) -> str:
    return str(value or "").strip()


def _bounded(value: object) -> str | None:
    """审计用的有界字符串（None 原样保留：不可比较不是空串）。"""
    if value is None:
        return None
    return str(value)[:MAX_AUDIT_CHARS]


def _state_value(state: ObjectState | str) -> str:
    return str(getattr(state, "value", state))


def _coerce_base(value: Any) -> Path | None:
    """workspace/effect_base 只接受 Path/str；其它类型视为不可确定（不猜 cwd）。"""
    if value is None:
        return None
    if isinstance(value, (str, os.PathLike)):
        try:
            return Path(value)
        except (TypeError, ValueError):
            return None
    return None


def _normcase(value: str) -> str:
    return os.path.normcase(value)


def _same_resolved_path(left: str, right: str) -> bool:
    return _normcase(os.path.normpath(left)) == _normcase(os.path.normpath(right))


def _canonical_path(value: object) -> str | None:
    """realpath + normcase：判断"是不是同一个路径项"（不是身份比较）。"""
    text = _clean(value)
    if not text:
        return None
    try:
        resolved = os.path.realpath(text)
    except (OSError, ValueError):
        resolved = text
    return _normcase(os.path.normpath(resolved))


def _cache_key(path: str) -> str:
    try:
        absolute = os.path.abspath(path)
    except (OSError, ValueError):
        absolute = path
    return _normcase(absolute)


def _observed_from_cache_value(raw: object) -> tuple[str, str | None] | None:
    """缓存值 -> (identity_key, birth_key)；None 表示"已查过但身份不可读"。"""
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        identity = raw.get("identity_key") or raw.get("identity") or raw.get("key")
        birth = raw.get("birth_key") or raw.get("birth")
    elif isinstance(raw, (tuple, list)) and len(raw) >= 2:
        identity, birth = raw[0], raw[1]
    else:
        identity = getattr(raw, "key", None) or getattr(raw, "identity_key", None)
        birth = getattr(raw, "birth_key", None)
    identity_text = _clean(identity)
    if not identity_text:
        return None
    return identity_text, (_clean(birth) or None)


def _observe_path(path: str, cache: dict | None) -> tuple[str, str | None] | None:
    """路径当前对象的 (identity, birth)；读不到返回 None（不猜）。"""
    keys = [str(path), _cache_key(str(path))]
    if isinstance(cache, dict):
        for key in keys:
            if key in cache:
                return _observed_from_cache_value(cache.get(key))
    found = file_identity.identity_of(path)
    value: tuple[str, str | None] | None
    if found is None:
        value = None
    else:
        value = (str(found.key), _clean(found.birth_key) or None)
    if isinstance(cache, dict):
        try:
            cache[_cache_key(str(path))] = value
        except Exception:  # noqa: BLE001 - 缓存写失败不影响判定
            pass
    return value


# ---------------------------------------------------------------------------
# §2 纯函数：路径规范化 / 文本相等
# ---------------------------------------------------------------------------


def resolve_path(value: object, base: Any) -> str | None:
    """规范化绝对路径（expanduser+resolve）；不可解析返回 None。

    base=None 时只接受绝对路径（不拿进程 cwd 兜底，避免用错基准）。
    """
    text = _clean(value)
    if not text:
        return None
    try:
        candidate = Path(text).expanduser()
    except (OSError, ValueError, RuntimeError):
        return None
    if not candidate.is_absolute():
        base_path = _coerce_base(base)
        if base_path is None:
            return None
        candidate = base_path / candidate
    try:
        return str(candidate.resolve())
    except (OSError, ValueError, RuntimeError):
        return None


def same_path_text(left: object, right: object) -> bool:
    """分隔符/大小写/尾斜杠规范化后的文本相等（不可解析时的唯一可靠依据）。

    只在两边都无法做真实对象核对时使用；它**不**证明是同一份文件，因此调用方
    只能据此返回 MATCH（保守）或 UNKNOWN，不能据此声称"无关"。
    """

    def normalize(value: object) -> str:
        return _clean(value).replace("\\", "/").rstrip("/").lower()

    normalized_left = normalize(left)
    normalized_right = normalize(right)
    return bool(normalized_left) and normalized_left == normalized_right


# ---------------------------------------------------------------------------
# §2/§3.5 程序名称只读澄清（不写库、不补绑定）
# ---------------------------------------------------------------------------


def resolve_program_name(name: str) -> ProgramResolution:
    """当前 PATH 只读查找入口名 -> 真实程序对象（澄清用）。

    只读：不写数据库、不补建绑定、不改变历史 JSON、不创建/删除文件。
    找不到 / 身份不可读时如实返回 path 与 error，绝不按名称相似猜测。
    """
    text = _clean(name)
    if not text:
        return ProgramResolution(name=text, error="empty_name")
    try:
        found = shutil.which(text)
    except Exception:  # noqa: BLE001 - 查找失败按找不到处理（不猜测）
        found = None
    if not found:
        return ProgramResolution(name=text, error="program_not_found")
    try:
        resolved = str(Path(os.path.realpath(found)))
    except (OSError, ValueError):
        resolved = str(found)
    identity = file_identity.identity_of(Path(resolved))
    if identity is None:
        return ProgramResolution(
            name=text,
            path=resolved,
            error="identity_unavailable",
        )
    return ProgramResolution(
        name=text,
        path=resolved,
        identity_key=str(identity.key),
        birth_key=_clean(identity.birth_key) or None,
        source="path_lookup",
        error="",
    )


# ---------------------------------------------------------------------------
# §2/§3.4 compare_bound_identity：唯一身份 + 生命周期比较入口
# ---------------------------------------------------------------------------


def compare_bound_identity(
    bound_identity: str | None,
    bound_birth: str | None,
    *,
    candidate_path: str | None = None,
    observed_identity: str | None = None,
    observed_birth: str | None = None,
    cache: dict | None = None,
) -> IdentityComparison:
    """共同身份契约（匹配与失效检查共用；禁止各分支自己比字段）。

    * identity 不可读 -> identity_unavailable；
    * identity 不同 -> different_object；
    * identity 相同且两侧 birth 都在且不同 -> different_lifecycle（编号复用）；
    * identity 相同、bound birth 存在但 observed birth 取不到 ->
      lifecycle_unverifiable；
    * 否则 same：bound birth 缺失时按"保存时无生命周期依据"的弱依据匹配，
      fact 里标 lifecycle_basis=binding_without_birth。

    禁止用内容摘要 / mtime / 名称相似 / 调用方自报代替生命周期。
    """
    bound = _clean(bound_identity)
    bound_lifecycle = _clean(bound_birth) or None
    identity_fact_source = "path_compare"
    facts: list[ObjectFact] = []
    if candidate_path is not None:
        facts.append(
            ObjectFact(
                field="candidate_path",
                observed=_clean(candidate_path) or None,
                source="path_lookup",
            )
        )
    observed = _clean(observed_identity)
    observed_lifecycle = _clean(observed_birth) or None
    if not observed and candidate_path is not None:
        lookup = _observe_path(str(candidate_path), cache)
        if lookup is not None:
            observed, observed_lifecycle = lookup[0], lookup[1]
    if not bound:
        facts.append(
            ObjectFact(
                field="identity_key",
                expected=None,
                observed=observed or None,
                source=identity_fact_source,
                equal=None,
            )
        )
        facts.append(
            ObjectFact(
                field="identity_availability",
                expected="binding_missing",
                observed="unavailable",
                source="program_binding",
            )
        )
        return IdentityComparison(IDENTITY_UNAVAILABLE, tuple(facts))
    if not observed:
        facts.append(
            ObjectFact(
                field="identity_key",
                expected=bound,
                observed=None,
                source=identity_fact_source,
                equal=None,
            )
        )
        facts.append(
            ObjectFact(
                field="identity_availability",
                expected=bound,
                observed="unreadable",
                source="file_identity",
            )
        )
        return IdentityComparison(IDENTITY_UNAVAILABLE, tuple(facts))
    identity_equal = bound == observed
    facts.append(
        ObjectFact(
            field="identity_key",
            expected=bound,
            observed=observed,
            source=identity_fact_source,
            equal=identity_equal,
        )
    )
    if not identity_equal:
        return IdentityComparison(IDENTITY_DIFFERENT_OBJECT, tuple(facts))
    # identity 相同：生命周期决定"同一对象"还是"同一编号上的另一个对象"。
    if bound_lifecycle and observed_lifecycle:
        birth_equal = bound_lifecycle == observed_lifecycle
        facts.append(
            ObjectFact(
                field="birth_key",
                expected=bound_lifecycle,
                observed=observed_lifecycle,
                source=identity_fact_source,
                equal=birth_equal,
            )
        )
        return IdentityComparison(
            IDENTITY_SAME if birth_equal else IDENTITY_DIFFERENT_LIFECYCLE,
            tuple(facts),
        )
    if bound_lifecycle and not observed_lifecycle:
        facts.append(
            ObjectFact(
                field="birth_key",
                expected=bound_lifecycle,
                observed=None,
                source=identity_fact_source,
                equal=None,
            )
        )
        return IdentityComparison(IDENTITY_LIFECYCLE_UNVERIFIABLE, tuple(facts))
    # bound birth 缺失：保存时没有生命周期依据，只能按身份弱依据匹配（fact 明确标注）。
    facts.append(
        ObjectFact(
            field="birth_key",
            expected=None,
            observed=observed_lifecycle,
            source=identity_fact_source,
            equal=None,
        )
    )
    facts.append(
        ObjectFact(
            field="lifecycle_basis",
            expected="binding_without_birth",
            observed="observed_birth_present" if observed_lifecycle else "unknown",
            source="program_binding",
            equal=None,
        )
    )
    return IdentityComparison(IDENTITY_SAME, tuple(facts))


# ---------------------------------------------------------------------------
# §2/§5 决策码
# ---------------------------------------------------------------------------


def decision_code_for(state: ObjectState | str, object_kind: str | None) -> str | None:
    """UNBOUND + program_name -> 程序名称绑定恢复码；INVALID/UNKNOWN 各自专用码。

    object_kind 传**限制对象**的类别（限制里的 kind），不是候选对象类别；
    UNBOUND 的专用恢复只针对程序名称（契约 §2）。
    """
    kind = _clean(object_kind).lower()
    try:
        normalized = ObjectState(_state_value(state))
    except ValueError:
        return None
    if normalized is ObjectState.UNBOUND:
        if kind == "program_name":
            return DECISION_PROGRAM_NAME_UNRESOLVED
        return None
    if normalized is ObjectState.INVALID:
        return DECISION_PROGRAM_BINDING_INVALIDATED
    if normalized is ObjectState.UNKNOWN:
        return DECISION_OBJECT_STATE_UNCONFIRMED
    return None
