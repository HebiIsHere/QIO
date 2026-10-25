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

R9-H12 证据契约（本模块内实现）：

* UNRELATED 只能来自 UNRELATED_BASES 白名单（§2），每条依据必须带注册的必带
  事实；_decision / _merge_decisions 会校验，未注册或事实不足直接拒绝；
* 限制路径身份不可读（不存在 / 存在但读不到）不再返回 UNRELATED，而是 UNKNOWN
  （§3.1：删除/替换后的旧对象可能仍在别名路径存在）；
* 未绑定 program_name 的“入口名不同 = 无关”“当前解析到别处 = 无关”已删除：
  保存候选（纯数据 binding_candidates）优先，无候选时失败关闭（§3.2）。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from agent.security.effects import DECISION_PROGRAM_NAME_UNRESOLVED
from agent.services import file_identity

__all__ = [
    "DECISION_BINDING_INVALIDATED",
    "DECISION_OBJECT_UNCONFIRMED",
    "DECISION_OBJECT_STATE_UNCONFIRMED",
    "DECISION_PROGRAM_BINDING_INVALIDATED",
    "IdentityComparison",
    "LimitAssessment",
    "ObjectDecision",
    "ObjectFact",
    "ObjectState",
    "MAX_AUDIT_CHARS",
    "ProgramResolution",
    "UNRELATED_BASES",
    "UnrelatedBasis",
    "compare_bound_identity",
    "decide_object",
    "decision_code_for",
    "resolve_path",
    "resolve_program_name",
    "same_path_text",
    "validate_unrelated_decision",
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
#: 同时保留 Lead 引擎/审计使用的短名，避免接线处出现两套常量。
DECISION_PROGRAM_BINDING_INVALIDATED = "program_binding_invalidated"
DECISION_OBJECT_STATE_UNCONFIRMED = "object_state_unconfirmed"
DECISION_BINDING_INVALIDATED = DECISION_PROGRAM_BINDING_INVALIDATED
DECISION_OBJECT_UNCONFIRMED = DECISION_OBJECT_STATE_UNCONFIRMED

#: 限制对象 / 候选对象的 kind 取值（契约 §2）。
_PATH_KINDS = frozenset({"path", "path_prefix", "directory"})
_SERVICE_CANDIDATE_KINDS = frozenset({"service", "url", "url_host"})
_CANDIDATE_KINDS = frozenset(
    {"path", "path_prefix", "directory", "program", "program_name", "service", "url", "url_host"}
)

#: 聚合优先级（契约 §4.3）：MATCH > INVALID/UNBOUND > UNKNOWN > UNRELATED。
#: R9-H12 §3.2 复核：UNKNOWN 必须高于 UNRELATED（事实不足不能当无关放行）。
_STATE_PRIORITY = {
    "match": 3,
    "invalid": 2,
    "unbound": 2,
    "unknown": 1,
    "unrelated": 0,
}


@dataclass(frozen=True)
class UnrelatedBasis:
    """UNRELATED 白名单依据（契约 §2）：basis + 必带事实 + 成立条件说明。

    * required_facts：判定 facts 里必须出现的字段名；
    * required_equal：其中必须为指定值（True/False）的事实；
    * condition：人类可读的成立条件（只用于审计/评审，不参与判定）。
    """

    basis: str
    required_facts: tuple[str, ...] = ()
    required_equal: tuple[tuple[str, bool], ...] = ()
    condition: str = ""


#: 契约 §2 UNRELATED 白名单（唯一允许的可靠无关依据）。
#: 新增 UNRELATED 分支必须先在注册表登记依据与必带事实；判定工厂会强制校验，
#: 未注册或证据不足的 UNRELATED 直接拒绝（不允许“名称不同/查找环境不同”当依据）。
UNRELATED_BASES: Mapping[str, UnrelatedBasis] = MappingProxyType(
    {
        "operation_out_of_scope": UnrelatedBasis(
            basis="operation_out_of_scope",
            required_facts=("operation", "operation_scope"),
            required_equal=(("operation_scope", False),),
            condition="limit.operations 已知、本次 operation 已知且不在集合（操作范围先过滤）",
        ),
        "object_kind_out_of_scope": UnrelatedBasis(
            basis="object_kind_out_of_scope",
            required_facts=("limit_kind", "candidate"),
            condition="限制对象类别与候选对象类别不相交（service↔路径/文件等）",
        ),
        "candidate_not_exists": UnrelatedBasis(
            basis="candidate_not_exists",
            required_facts=("exists",),
            required_equal=(("exists", False),),
            condition="候选路径真实不存在（exists=False）：不存在的对象不可能是被禁止对象",
        ),
        "path_prefix_outside": UnrelatedBasis(
            basis="path_prefix_outside",
            required_facts=("path_within",),
            required_equal=(("path_within", False),),
            condition="候选解析路径不在 path_prefix/directory 内（路径型限制既定语义）",
        ),
        "service_outside": UnrelatedBasis(
            basis="service_outside",
            required_facts=("service_scope",),
            required_equal=(("service_scope", False),),
            condition="service/url 作用域文本不同（服务限制语义）",
        ),
        "different_object_identity": UnrelatedBasis(
            basis="different_object_identity",
            required_facts=("identity_key",),
            required_equal=(("identity_key", False),),
            condition="保存绑定 identity 可读 + 候选 identity 可读 + 不相等",
        ),
        "path_different_object": UnrelatedBasis(
            basis="path_different_object",
            required_facts=("identity_key",),
            required_equal=(("identity_key", False),),
            condition="path 限制两侧身份都可读且不相等",
        ),
        "path_different_lifecycle": UnrelatedBasis(
            basis="path_different_lifecycle",
            required_facts=("identity_key", "birth_key"),
            required_equal=(("identity_key", True), ("birth_key", False)),
            condition="path 限制同一 identity、两侧 birth 都可读且不同",
        ),
        "entry_resolves_to_other_object": UnrelatedBasis(
            basis="entry_resolves_to_other_object",
            required_facts=("entry_name", "identity_key"),
            required_equal=(("identity_key", False),),
            condition=(
                "已绑定 program_name：入口解析对象 identity 可读、与绑定 identity "
                "不同；入口名不同，或同名但入口解析路径≠绑定路径（R9-H13 §3.B3："
                "同名入口解析到独立真实对象按对象事实继续；解析路径==绑定路径而"
                "身份/生命周期变化仍 INVALID）"
            ),
        ),
    }
)


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


def validate_unrelated_decision(decision: ObjectDecision) -> tuple[str, ...]:
    """校验一条判定的 UNRELATED 依据（契约 §2 白名单 + 必带事实）。

    返回问题码元组；空元组 = 依据已注册且事实齐全（可审计、可复核）。
    非 UNRELATED 判定无需校验（返回空元组）。
    """
    if decision.state is not ObjectState.UNRELATED:
        return ()
    spec = UNRELATED_BASES.get(decision.basis)
    if spec is None:
        return ("unregistered_basis:" + _clean(decision.basis),)
    facts: dict[str, ObjectFact] = {}
    for fact in decision.facts:
        facts.setdefault(fact.field, fact)
    problems: list[str] = []
    for field in spec.required_facts:
        if field not in facts:
            problems.append("missing_fact:" + field)
    for field, expected in spec.required_equal:
        fact = facts.get(field)
        if fact is not None and fact.equal is not expected:
            problems.append("fact_not_verified:" + field)
    return tuple(problems)


def _require_unrelated_evidence(decision: ObjectDecision) -> None:
    problems = validate_unrelated_decision(decision)
    if problems:
        raise ValueError(
            "UNRELATED 依据未注册或证据不足（"
            + _clean(decision.basis)
            + "）："
            + ",".join(problems)
        )


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
# §3 判定语义表：decide_object（唯一对象判定入口）
# ---------------------------------------------------------------------------


def _path_equal(left: object, right: object) -> bool:
    left_path = _canonical_path(left)
    right_path = _canonical_path(right)
    return bool(left_path) and bool(right_path) and left_path == right_path


def _exists(path: object) -> bool:
    try:
        return os.path.exists(str(path))
    except (OSError, ValueError):
        return False


def _is_within(candidate: str, target: str) -> bool:
    try:
        left = Path(candidate)
        right = Path(target)
        return left == right or left.is_relative_to(right)
    except (OSError, ValueError):
        return False


def _is_bare_entry(value: object) -> bool:
    """不含分隔符的入口名（cmd effect 拍平后的 program 值可能是裸名称）。"""
    text = _clean(value)
    if not text:
        return False
    if "/" in text or "\\" in text:
        return False
    if os.name == "nt" and ":" in text:
        return False
    return not Path(text).is_absolute()


def _entry_key(value: object) -> str:
    return _normcase(_clean(value))


def _entry_equal(left: object, right: object) -> bool:
    left_key = _entry_key(left)
    right_key = _entry_key(right)
    return bool(left_key) and left_key == right_key


def _service_matches(candidate: object, limit: object) -> tuple[bool, bool]:
    """(命中, 是否首段作用域匹配)；与既有 _limit_service_matches 同语义。"""
    left = _clean(candidate).lower()
    right = _clean(limit).lower()
    if not left or not right:
        return False, False
    if left == right:
        return True, False
    return left.split("/")[0] == right.split("/")[0], True


_OPERATION_ALIASES = {
    "read": "read",
    "write": "write",
    "create": "create",
    "delete": "delete",
    "execute": "execute",
    "install": "install",
    "install_deps": "install",
    "system": "system_change",
    "system_change": "system_change",
    "egress": "egress",
    "network_egress": "egress",
}


def _normalize_operation(value: object) -> str | None:
    text = _clean(value).lower()
    if not text:
        return None
    return _OPERATION_ALIASES.get(text, text)


def _operation_set(raw: object) -> frozenset[str]:
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        items: Sequence[object] = (raw,)
    else:
        try:
            items = tuple(raw)  # type: ignore[arg-type]
        except TypeError:
            return frozenset()
    output: set[str] = set()
    for item in items:
        if _clean(item) == "*":
            return frozenset()
        normalized = _normalize_operation(item)
        if normalized:
            output.add(normalized)
    return frozenset(output)


def _effect_entry_name(effect: Any) -> str | None:
    """真实调用入口名（detail.requested_program / execution.requested_program /
    execution.program）；只接受不含分隔符的名称，路径不当作入口名。"""
    detail = getattr(effect, "detail", None)
    if not isinstance(detail, Mapping):
        return None
    execution = detail.get("execution")
    execution = execution if isinstance(execution, Mapping) else {}
    for raw in (
        detail.get("requested_program"),
        execution.get("requested_program"),
        execution.get("program"),
    ):
        text = _clean(raw).strip("\"'")
        if text and _is_bare_entry(text):
            return text
    return None


def _fact(
    field: str,
    expected: object = None,
    observed: object = None,
    source: str = "",
    equal: bool | None = None,
) -> ObjectFact:
    return ObjectFact(
        field=field,
        expected=None if expected is None else _clean(expected),
        observed=None if observed is None else _clean(observed),
        source=source,
        equal=equal,
    )


def _base_facts(
    operation: str | None, candidate_kind: str, candidate_value: str
) -> list[ObjectFact]:
    return [
        _fact("operation", observed=operation, source="effect"),
        _fact("candidate", observed=candidate_kind + ":" + candidate_value, source="effect"),
    ]


def _decision(
    state: ObjectState,
    basis: str,
    *,
    operation: str | None,
    candidate_kind: str,
    candidate_value: str,
    facts: Sequence[ObjectFact] = (),
    detail: str = "",
) -> ObjectDecision:
    decision = ObjectDecision(
        state=state,
        basis=basis,
        operation=operation,
        object_kind=candidate_kind,
        object_value=candidate_value,
        facts=tuple(facts),
        detail=detail,
    )
    if state is ObjectState.UNRELATED:
        _require_unrelated_evidence(decision)
    return decision


def _unrelated(
    basis: str,
    *,
    operation: str | None,
    candidate_kind: str,
    candidate_value: str,
    facts: Sequence[ObjectFact] = (),
    detail: str = "",
) -> ObjectDecision:
    """UNRELATED 的唯一构造入口（契约 §2：basis 必须先注册且带足事实）。"""
    return _decision(
        ObjectState.UNRELATED,
        basis,
        operation=operation,
        candidate_kind=candidate_kind,
        candidate_value=candidate_value,
        facts=facts,
        detail=detail,
    )


def _merge_decisions(primary: ObjectDecision, secondary: ObjectDecision) -> ObjectDecision:
    """同一候选对象的两条结构化依据按 §4.3 优先级合并（MATCH 最高）。

    典型场景：program_name 绑定 + 真实调用入口名（effect）。路径候选证明"无关"，
    但本次真实入口名与限制名称相同、当前解析对象已变 -> INVALID 必须优先停住。
    """
    primary_rank = _STATE_PRIORITY.get(_state_value(primary.state), 0)
    secondary_rank = _STATE_PRIORITY.get(_state_value(secondary.state), 0)
    winner = secondary if secondary_rank > primary_rank else primary
    facts: list[ObjectFact] = list(primary.facts)
    for fact in secondary.facts:
        if fact not in facts:
            facts.append(fact)
    merged = ObjectDecision(
        state=winner.state,
        basis=winner.basis,
        operation=primary.operation,
        object_kind=primary.object_kind,
        object_value=primary.object_value,
        facts=tuple(facts),
        detail=winner.detail,
    )
    if merged.state is ObjectState.UNRELATED:
        _require_unrelated_evidence(merged)
    return merged


def _candidate_entry_name(candidate_kind: str, candidate_value: str) -> str | None:
    if candidate_kind == "program_name":
        return candidate_value
    if candidate_kind == "program" and _is_bare_entry(candidate_value):
        return candidate_value
    return None


def _limit_base(workspace: Any, effect_base: Any) -> Path | None:
    return _coerce_base(workspace if workspace is not None else effect_base)


def _candidate_base(workspace: Any, effect_base: Any) -> Path | None:
    return _coerce_base(effect_base if effect_base is not None else workspace)


# ---------------------------------------------------------------- 路径类对象


def _path_core(
    limit_kind: str,
    target: str,
    candidate_path: str,
    observed: tuple[str, str | None] | None,
    *,
    operation: str | None,
    candidate_kind: str,
    candidate_value: str,
    base_facts: Sequence[ObjectFact],
    cache: dict | None,
    saved_object_facts: Mapping[str, Any] | None = None,
) -> ObjectDecision:
    """§3.1 path/path_prefix/directory 的结构化核对（真实文件身份，不是文本猜测）。

    R9-H12 §3.1：限制路径身份不可读（不存在 / 存在但读不到）一律 UNKNOWN，
    不再返回 UNRELATED（旧对象可能仍在别名路径存在）。显式 program 路径共用本分支。

    R9-H13 §3.B2：限制条目若带**保存时对象身份**（requirement 保存流程写入的
    identity/birth_key，纯数据），优先用保存身份核对候选——身份不同=可靠无关
    （限制路径消失/被替换后不再扩大为对无关对象的持续全局拒绝）；身份相同=
    MATCH（旧对象硬链接/别名仍保护）；同身份不同生命周期=另一个对象。
    历史数据缺这些字段时保持原失败关闭语义（UNKNOWN），不按当前路径/查找推断。
    """
    facts = list(base_facts)
    facts.append(
        _fact(
            "path",
            expected=target,
            observed=candidate_path,
            source="path_compare",
            equal=_same_resolved_path(candidate_path, target),
        )
    )
    if limit_kind in ("path_prefix", "directory"):
        if _is_within(candidate_path, target):
            return _decision(
                ObjectState.MATCH,
                "path_prefix_contains",
                operation=operation,
                candidate_kind=candidate_kind,
                candidate_value=candidate_value,
                facts=facts,
            )
        facts.append(
            _fact(
                "path_within",
                expected=target,
                observed=candidate_path,
                source="path_compare",
                equal=False,
            )
        )
        return _unrelated(
            "path_prefix_outside",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=facts,
        )
    if _same_resolved_path(candidate_path, target):
        return _decision(
            ObjectState.MATCH,
            "path_same",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=facts,
        )
    if not _exists(candidate_path):
        # §3.1：候选不存在且不是限制路径本身 -> 不可能是已存在对象，可靠无关（先判）。
        facts.append(_fact("exists", observed="false", source="path_compare", equal=False))
        return _unrelated(
            "candidate_not_exists",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=facts,
        )
    saved = saved_object_facts or {}
    saved_identity = _clean(saved.get("identity")) or None
    if saved_identity:
        candidate_observed = (
            observed if observed is not None else _observe_path(candidate_path, cache)
        )
        if candidate_observed is None:
            facts.append(
                _fact(
                    "identity_key",
                    expected=saved_identity,
                    observed=None,
                    source="file_identity",
                )
            )
            return _decision(
                ObjectState.UNKNOWN,
                "path_identity_unverifiable",
                operation=operation,
                candidate_kind=candidate_kind,
                candidate_value=candidate_value,
                facts=facts,
                detail="候选对象存在但文件身份无法读取：无法与保存时限制对象身份核对",
            )
        facts.append(
            _fact(
                "identity_source",
                observed=_clean(saved.get("source")) or "requirement_save",
                source="saved_object_fact",
            )
        )
        comparison = compare_bound_identity(
            saved_identity,
            _clean(saved.get("birth_key")) or None,
            observed_identity=candidate_observed[0],
            observed_birth=candidate_observed[1],
        )
        facts.extend(comparison.facts)
        if comparison.state == IDENTITY_SAME:
            # 保存时旧对象（或其硬链接/别名）仍是指向同一对象：继续保护。
            return _decision(
                ObjectState.MATCH,
                "path_hardlink_same_object",
                operation=operation,
                candidate_kind=candidate_kind,
                candidate_value=candidate_value,
                facts=facts,
                detail="候选与保存时限制对象身份一致（旧对象硬链接/别名），仍属用户限制对象",
            )
        if comparison.state == IDENTITY_DIFFERENT_OBJECT:
            return _unrelated(
                "path_different_object",
                operation=operation,
                candidate_kind=candidate_kind,
                candidate_value=candidate_value,
                facts=facts,
                detail="候选对象身份与保存时限制对象不同：限制路径消失/被替换不扩大为对无关对象的拒绝",
            )
        if comparison.state == IDENTITY_DIFFERENT_LIFECYCLE:
            return _unrelated(
                "path_different_lifecycle",
                operation=operation,
                candidate_kind=candidate_kind,
                candidate_value=candidate_value,
                facts=facts,
                detail="同一身份下生命周期与保存时不同：是另一个生命周期对象",
            )
        return _decision(
            ObjectState.UNKNOWN,
            "path_identity_unverifiable",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=facts,
            detail="保存时记录了生命周期依据，但候选生命周期不可读：不能默认一致",
        )
    target_observed = _observe_path(target, cache)
    if target_observed is None:
        # §3.1：限制路径身份不可读不能证明无关。
        facts.append(_fact("identity_key", observed=None, source="file_identity"))
        if not _exists(target):
            facts.append(
                _fact(
                    "exists",
                    expected="true",
                    observed="false",
                    source="path_compare",
                    equal=False,
                )
            )
            return _decision(
                ObjectState.UNKNOWN,
                "path_limit_target_unreadable",
                operation=operation,
                candidate_kind=candidate_kind,
                candidate_value=candidate_value,
                facts=facts,
                detail=(
                    "限制路径当前不存在：删除/替换后的旧对象可能仍以别名路径存在，"
                    "不能仅凭路径文本不同判无关"
                ),
            )
        return _decision(
            ObjectState.UNKNOWN,
            "path_limit_identity_unavailable",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=facts,
            detail="限制路径存在但文件身份不可读：无法确认是否为同一对象，不能判无关",
        )
    candidate_observed = observed if observed is not None else _observe_path(candidate_path, cache)
    if candidate_observed is None:
        facts.append(
            _fact("identity_key", expected=target_observed[0], observed=None, source="file_identity")
        )
        return _decision(
            ObjectState.UNKNOWN,
            "path_identity_unverifiable",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=facts,
            detail="对象存在但文件身份无法读取，不能确认是否为同一对象",
        )
    comparison = compare_bound_identity(
        target_observed[0],
        target_observed[1],
        observed_identity=candidate_observed[0],
        observed_birth=candidate_observed[1],
    )
    facts.extend(comparison.facts)
    if comparison.state == IDENTITY_SAME:
        return _decision(
            ObjectState.MATCH,
            "path_hardlink_same_object",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=facts,
        )
    if comparison.state == IDENTITY_DIFFERENT_OBJECT:
        return _unrelated(
            "path_different_object",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=facts,
        )
    if comparison.state == IDENTITY_DIFFERENT_LIFECYCLE:
        return _unrelated(
            "path_different_lifecycle",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=facts,
            detail="同一身份下生命周期不同：是另一个生命周期对象，不是限制路径的旧对象",
        )
    return _decision(
        ObjectState.UNKNOWN,
        "path_identity_unverifiable",
        operation=operation,
        candidate_kind=candidate_kind,
        candidate_value=candidate_value,
        facts=facts,
    )


def _path_limit_decision(
    limit_kind: str,
    limit_value: str,
    candidate_value: str,
    *,
    operation: str | None,
    workspace: Any,
    effect_base: Any,
    cache: dict | None,
    candidate_kind: str,
    base_facts: Sequence[ObjectFact],
    saved_object_facts: Mapping[str, Any] | None = None,
) -> ObjectDecision:
    target = resolve_path(limit_value, _limit_base(workspace, effect_base))
    candidate_path = resolve_path(candidate_value, _candidate_base(workspace, effect_base))
    if target is None or candidate_path is None:
        if same_path_text(candidate_value, limit_value):
            return _decision(
                ObjectState.MATCH,
                "path_text_same",
                operation=operation,
                candidate_kind=candidate_kind,
                candidate_value=candidate_value,
                facts=base_facts,
            )
        return _decision(
            ObjectState.UNKNOWN,
            "path_unresolvable",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=base_facts,
            detail="路径无法可靠规范化（基准不可确定），不能当作无关",
        )
    return _path_core(
        limit_kind,
        target,
        candidate_path,
        None,
        operation=operation,
        candidate_kind=candidate_kind,
        candidate_value=candidate_value,
        base_facts=base_facts,
        cache=cache,
        saved_object_facts=saved_object_facts,
    )


def _path_limit_name_decision(
    limit_kind: str,
    limit_value: str,
    name: str,
    *,
    operation: str | None,
    workspace: Any,
    effect_base: Any,
    cache: dict | None,
    resolver: Callable[[str], ProgramResolution],
    candidate_kind: str,
    candidate_value: str,
    base_facts: Sequence[ObjectFact],
    saved_object_facts: Mapping[str, Any] | None = None,
) -> ObjectDecision:
    target = resolve_path(limit_value, _limit_base(workspace, effect_base))
    if target is None:
        return _decision(
            ObjectState.UNKNOWN,
            "path_unresolvable",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=base_facts,
        )
    resolution = resolver(name)
    if not resolution.path:
        return _decision(
            ObjectState.UNKNOWN,
            "entry_name_unresolved",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=base_facts,
            detail="入口名当前无法解析到真实对象，不能按名称猜测",
        )
    candidate_path = resolve_path(resolution.path, None) or _clean(resolution.path)
    observed = None
    if resolution.identity_key:
        observed = (resolution.identity_key, _clean(resolution.birth_key) or None)
    facts = list(base_facts)
    return _path_core(
        limit_kind,
        target,
        candidate_path,
        observed,
        operation=operation,
        candidate_kind=candidate_kind,
        candidate_value=candidate_value,
        base_facts=facts,
        cache=cache,
        saved_object_facts=saved_object_facts,
    )


def _program_limit_name_decision(
    limit_value: str,
    name: str,
    *,
    operation: str | None,
    workspace: Any,
    effect_base: Any,
    cache: dict | None,
    resolver: Callable[[str], ProgramResolution],
    candidate_kind: str,
    candidate_value: str,
    base_facts: Sequence[ObjectFact],
    saved_object_facts: Mapping[str, Any] | None = None,
) -> ObjectDecision:
    """§3.1/§3.3：显式程序路径限制 + 入口名候选（与 path 限制共用 _path_core）。

    * 入口名解析回同一路径文本 -> MATCH（路径文本/入口结构化完全匹配，契约 §1）；
    * 其余交给 _path_core 用真实文件身份/生命周期核对，不再自造“解析到别处=无关”。
    """
    target = resolve_path(limit_value, _limit_base(workspace, effect_base))
    if target is None:
        return _decision(
            ObjectState.UNKNOWN,
            "path_unresolvable",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=base_facts,
        )
    resolution = resolver(name)
    if not resolution.path:
        return _decision(
            ObjectState.UNKNOWN,
            "entry_name_unresolved",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=base_facts,
            detail="入口名当前无法解析到真实对象，不能按名称猜测",
        )
    candidate_path = resolve_path(resolution.path, None) or _clean(resolution.path)
    if _same_resolved_path(candidate_path, target):
        return _decision(
            ObjectState.MATCH,
            "entry_resolves_to_limit_path",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=base_facts,
        )
    observed = None
    if resolution.identity_key:
        observed = (resolution.identity_key, _clean(resolution.birth_key) or None)
    return _path_core(
        "path",
        target,
        candidate_path,
        observed,
        operation=operation,
        candidate_kind=candidate_kind,
        candidate_value=candidate_value,
        base_facts=base_facts,
        cache=cache,
        saved_object_facts=saved_object_facts,
    )


# ---------------------------------------------------------------- program_name


def _binding_state(item: Mapping[str, Any]) -> tuple[Mapping[str, Any] | None, bool]:
    """(有效绑定, 是否结构不完整)。不完整绑定不能静默降级成"未绑定"。"""
    raw = item.get("program_binding")
    if raw is None:
        return None, False
    if not isinstance(raw, Mapping):
        return None, True
    if not _clean(raw.get("identity")):
        return None, True
    return raw, False


#: 保存候选（item["binding_candidates"]）的稳定数量上限（纯数据，不补采身份）。
_SAVED_CANDIDATE_LIMIT = 4


def _saved_candidates(item: Mapping[str, Any]) -> tuple[dict, ...]:
    """对象条目上的保存候选事实（纯数据；读取绝不重新查找程序/采集身份）。"""
    raw = item.get("binding_candidates")
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        return ()
    candidates: list[dict] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        path = _clean(entry.get("path"))
        if not path:
            continue
        candidates.append(
            {
                "path": path,
                "source": _clean(entry.get("source")),
                "state": _clean(entry.get("state")).lower(),
                "identity": _clean(entry.get("identity")) or None,
                "birth_key": _clean(entry.get("birth_key")) or None,
            }
        )
        if len(candidates) >= _SAVED_CANDIDATE_LIMIT:
            break
    return tuple(candidates)


def _saved_candidate_facts(item: Mapping[str, Any]) -> list[ObjectFact]:
    """把保存时的绑定状态/失败依据/候选路径作为纯数据事实写进判定（审计用）。"""
    facts: list[ObjectFact] = []
    binding_state = _clean(item.get("binding_state")).lower()
    if binding_state:
        facts.append(_fact("binding_state", observed=binding_state, source="saved_binding"))
    failure = _clean(item.get("binding_failure")).lower()
    if failure:
        facts.append(_fact("binding_failure", observed=failure, source="saved_binding"))
    for candidate in _saved_candidates(item):
        facts.append(
            _fact(
                "saved_candidate_path",
                expected=candidate["identity"],
                observed=candidate["path"],
                source=candidate["source"] or "binding_candidates",
            )
        )
    return facts


def _saved_object_facts(item: Mapping[str, Any]) -> dict[str, Any]:
    """限制条目上的**保存时对象身份事实**（R9-H13 §3.B2；纯数据，不重新采集）。

    生产写入方：要求保存流程（requirement_limits）在首次形成精确路径对象条目时
    采集 identity/birth_key/exists/observed_at/source；读取/反序列化只搬运，不查
    当前路径、不查询程序。字段缺失（历史数据）返回空事实 -> 判定保持失败关闭。
    """
    identity = _clean(item.get("identity")) or None
    birth_key = _clean(item.get("birth_key")) or None
    exists_raw = item.get("exists")
    exists = exists_raw if isinstance(exists_raw, bool) else None
    if not (identity or birth_key or exists is not None):
        return {}
    return {
        "identity": identity,
        "birth_key": birth_key,
        "exists": exists,
        "observed_at": _clean(item.get("observed_at")) or None,
        "source": _clean(item.get("source")) or None,
    }


def _saved_path_match(
    candidate_path: str,
    saved: Sequence[Mapping[str, Any]],
    *,
    effect_base: Any,
) -> Mapping[str, Any] | None:
    """候选路径与某条保存候选路径相同？只做路径规范化，不读对象身份。"""
    for candidate in saved:
        saved_path = resolve_path(candidate.get("path"), _coerce_base(effect_base)) or _clean(
            candidate.get("path")
        )
        if saved_path and _same_resolved_path(candidate_path, saved_path):
            return candidate
    return None


def _saved_identity_match(
    observed: tuple[str, str | None] | None,
    saved: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """候选身份与某条保存候选身份相同？（保存候选可能没记身份 -> 不可比）"""
    if observed is None:
        return None
    identity = _clean(observed[0])
    if not identity:
        return None
    for candidate in saved:
        if _clean(candidate.get("identity")) == identity:
            return candidate
    return None


def _name_branch(
    limit_value: str,
    name: str,
    *,
    binding: Mapping[str, Any] | None,
    item: Mapping[str, Any],
    operation: str | None,
    cache: dict | None,
    resolver: Callable[[str], ProgramResolution],
    candidate_kind: str,
    candidate_value: str,
    base_facts: Sequence[ObjectFact],
) -> ObjectDecision:
    """§3.2 入口名分支：平台规范化后的完全相等 + 解析对象身份核对。

    R9-H12：删除“入口名不同 -> UNRELATED”的提前返回；名称差异不是对象事实。
    """
    facts = list(base_facts)
    facts.append(_fact("entry_name", observed=name, source="effect"))
    entry_same = _entry_equal(name, limit_value)
    resolution = resolver(name)
    resolution_path = None
    if resolution.path:
        resolution_path = resolve_path(resolution.path, None) or _clean(resolution.path)
    if binding is not None:
        bound_identity = _clean(binding.get("identity"))
        bound_birth = _clean(binding.get("birth_key")) or None
        bound_path = _clean(binding.get("path"))
        bound_path_matched = bool(
            bound_path
            and resolution_path
            and _same_resolved_path(resolution_path, bound_path)
        )
        if resolution_path:
            facts.append(
                _fact(
                    "entry_path",
                    expected=bound_path or None,
                    observed=resolution_path,
                    source="path_lookup",
                    equal=(bound_path_matched if bound_path else None),
                )
            )
        if entry_same:
            if not resolution.resolved:
                # 绑定 + 同一来源名：当前目标身份不可读也按 INVALID（不是 UNKNOWN）。
                return _decision(
                    ObjectState.INVALID,
                    "entry_name_same_object_differs",
                    operation=operation,
                    candidate_kind=candidate_kind,
                    candidate_value=candidate_value,
                    facts=facts,
                    detail="限制中的程序名称与本次调用入口相同，但当前无法解析到绑定的对象：绑定已失效，需先恢复/澄清绑定",
                )
            comparison = compare_bound_identity(
                bound_identity,
                bound_birth,
                observed_identity=resolution.identity_key,
                observed_birth=resolution.birth_key,
            )
            facts.extend(comparison.facts)
            if comparison.state == IDENTITY_SAME:
                return _decision(
                    ObjectState.MATCH,
                    "entry_name_same_object_same",
                    operation=operation,
                    candidate_kind=candidate_kind,
                    candidate_value=candidate_value,
                    facts=facts,
                )
            if comparison.state == IDENTITY_DIFFERENT_OBJECT and not bound_path_matched:
                # R9-H13 §3.B3：同名入口当前解析到与绑定 A **不同的真实对象**
                # （identity 可读且不同、入口路径≠绑定路径）→ 对象事实证明是
                # 另一个对象，按当前模式继续；不再仅因入口名相同判 INVALID。
                return _unrelated(
                    "entry_resolves_to_other_object",
                    operation=operation,
                    candidate_kind=candidate_kind,
                    candidate_value=candidate_value,
                    facts=facts,
                    detail="同名入口当前解析到与保存绑定不同的真实对象（身份可读且不同、入口路径不是绑定路径）：按可靠不同对象继续",
                )
            if comparison.state == IDENTITY_DIFFERENT_OBJECT:
                # 入口仍解析到绑定路径，但该路径上的对象已被替换：绑定失效。
                return _decision(
                    ObjectState.INVALID,
                    "bound_path_object_replaced",
                    operation=operation,
                    candidate_kind=candidate_kind,
                    candidate_value=candidate_value,
                    facts=facts,
                    detail="同名入口仍解析到原绑定路径，但该路径上的对象已被替换：绑定已失效，需先恢复/澄清绑定",
                )
            if comparison.state == IDENTITY_DIFFERENT_LIFECYCLE:
                return _decision(
                    ObjectState.INVALID,
                    "lifecycle_changed_same_identity",
                    operation=operation,
                    candidate_kind=candidate_kind,
                    candidate_value=candidate_value,
                    facts=facts,
                    detail="同名入口解析对象与保存绑定身份相同但生命周期不同（编号复用）：绑定已失效，需先恢复/澄清绑定",
                )
            return _decision(
                ObjectState.INVALID,
                "entry_name_same_object_differs",
                operation=operation,
                candidate_kind=candidate_kind,
                candidate_value=candidate_value,
                facts=facts,
                detail="限制中的程序名称与本次调用入口相同，但当前解析到的对象与保存绑定不一致：绑定已失效，需先恢复/澄清绑定",
            )
        # 入口名不同：只有“入口解析对象 identity 可读且与绑定 identity 不同”才可靠无关。
        if not resolution.resolved:
            return _decision(
                ObjectState.UNKNOWN,
                "entry_name_differs_unverifiable",
                operation=operation,
                candidate_kind=candidate_kind,
                candidate_value=candidate_value,
                facts=facts,
                detail="入口名不同，但当前解析失败/身份不可读：无法用对象事实证明无关，需先澄清",
            )
        comparison = compare_bound_identity(
            bound_identity,
            bound_birth,
            observed_identity=resolution.identity_key,
            observed_birth=resolution.birth_key,
        )
        facts.extend(comparison.facts)
        if comparison.state == IDENTITY_SAME:
            return _decision(
                ObjectState.MATCH,
                "entry_resolves_to_bound_object",
                operation=operation,
                candidate_kind=candidate_kind,
                candidate_value=candidate_value,
                facts=facts,
            )
        if comparison.state == IDENTITY_DIFFERENT_OBJECT:
            return _unrelated(
                "entry_resolves_to_other_object",
                operation=operation,
                candidate_kind=candidate_kind,
                candidate_value=candidate_value,
                facts=facts,
            )
        return _decision(
            ObjectState.UNKNOWN,
            "entry_name_differs_unverifiable",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=facts,
            detail="入口解析对象与绑定对象的身份/生命周期关系不可确认，不能当作无关",
        )
    if entry_same:
        return _decision(
            ObjectState.UNBOUND,
            "binding_missing_entry_same",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=facts,
            detail="限制中的程序名称尚未可靠绑定到明确对象，本次调用的入口名与之相同：需要先完成程序名称的专用恢复/澄清",
        )
    # 未绑定 + 入口名不同：只有保存候选 identity 与入口解析对象 identity 相同才相关。
    if resolution.resolved:
        matched = _saved_identity_match(
            (resolution.identity_key, _clean(resolution.birth_key) or None),
            _saved_candidates(item),
        )
        if matched is not None:
            facts.append(
                _fact(
                    "saved_candidate_identity",
                    expected=_clean(matched.get("identity")),
                    observed=resolution.identity_key,
                    source=_clean(matched.get("source")) or "binding_candidates",
                    equal=True,
                )
            )
            return _decision(
                ObjectState.UNBOUND,
                "binding_missing_saved_candidate_identity",
                operation=operation,
                candidate_kind=candidate_kind,
                candidate_value=candidate_value,
                facts=facts,
                detail="入口名不同，但入口解析对象与保存候选身份相同：属于同一保存候选，需先完成程序名称的专用恢复/澄清",
            )
    return _decision(
        ObjectState.UNKNOWN,
        "unbound_entry_name_differs",
        operation=operation,
        candidate_kind=candidate_kind,
        candidate_value=candidate_value,
        facts=facts,
        detail="保存时没有形成对象绑定，且无法用保存候选/入口解析对象证明关系：不能按名称差异放行",
    )


def _bound_path_decision(
    binding: Mapping[str, Any],
    candidate_value: str,
    *,
    operation: str | None,
    effect_base: Any,
    cache: dict | None,
    candidate_kind: str,
    base_facts: Sequence[ObjectFact],
) -> ObjectDecision:
    """§3.4：program_name 绑定存在 + 候选是具体程序/文件对象。"""
    bound_identity = _clean(binding.get("identity"))
    bound_birth = _clean(binding.get("birth_key")) or None
    bound_path = _clean(binding.get("path"))
    candidate_path = resolve_path(candidate_value, _coerce_base(effect_base)) or _clean(candidate_value)
    facts = list(base_facts)
    if not bound_identity:
        return _decision(
            ObjectState.UNKNOWN,
            "binding_incomplete",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=facts,
            detail="保存的程序名称绑定结构不完整（缺少身份字段）：不能确认，需先恢复/澄清绑定",
        )
    same_bound_path = _path_equal(candidate_path, bound_path) if bound_path else False
    if bound_path:
        facts.append(
            _fact(
                "path",
                expected=bound_path,
                observed=candidate_path,
                source="program_binding",
                equal=same_bound_path,
            )
        )
    if not _exists(candidate_path):
        facts.append(_fact("exists", observed="false", source="path_compare", equal=False))
        if same_bound_path:
            return _decision(
                ObjectState.INVALID,
                "bound_path_object_replaced",
                operation=operation,
                candidate_kind=candidate_kind,
                candidate_value=candidate_value,
                facts=facts,
                detail="原绑定路径上的对象已不存在（被删除/替换）：绑定已失效，需先恢复/澄清绑定",
            )
        return _decision(
            ObjectState.UNRELATED,
            "candidate_not_exists",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=facts,
        )
    observed = _observe_path(candidate_path, cache)
    if observed is None:
        return _decision(
            ObjectState.UNKNOWN,
            "identity_unavailable",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=facts,
            detail="对象存在但文件身份无法读取：无法确认程序名称绑定的对象，不能默认一致",
        )
    comparison = compare_bound_identity(
        bound_identity,
        bound_birth,
        observed_identity=observed[0],
        observed_birth=observed[1],
    )
    facts.extend(comparison.facts)
    if comparison.state == IDENTITY_SAME:
        basis = "binding_identity+birth_same" if bound_birth else "binding_identity_only"
        return _decision(
            ObjectState.MATCH,
            basis,
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=facts,
        )
    if comparison.state == IDENTITY_DIFFERENT_LIFECYCLE:
        return _decision(
            ObjectState.INVALID,
            "lifecycle_changed_same_identity",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=facts,
            detail="同一身份下生命周期不同（编号复用为另一个生命周期对象）：绑定已失效，需先恢复/澄清绑定",
        )
    if comparison.state == IDENTITY_LIFECYCLE_UNVERIFIABLE:
        return _decision(
            ObjectState.UNKNOWN,
            "lifecycle_unverifiable",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=facts,
            detail="保存时记录了生命周期依据，但当前程序名称对象生命周期不可读：不能默认一致，需恢复/澄清绑定",
        )
    if comparison.state == IDENTITY_UNAVAILABLE:
        return _decision(
            ObjectState.UNKNOWN,
            "identity_unavailable",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=facts,
            detail="无法确认程序名称绑定的对象身份，不能默认一致",
        )
    if same_bound_path:
        return _decision(
            ObjectState.INVALID,
            "bound_path_object_replaced",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=facts,
            detail="原绑定路径上的对象已被替换：绑定已失效，需先恢复/澄清绑定",
        )
    return _unrelated(
        "different_object_identity",
        operation=operation,
        candidate_kind=candidate_kind,
        candidate_value=candidate_value,
        facts=facts,
    )


def _unbound_path_decision(
    limit_value: str,
    candidate_value: str,
    *,
    item: Mapping[str, Any],
    operation: str | None,
    effect: Any,
    workspace: Any,
    effect_base: Any,
    cache: dict | None,
    resolver: Callable[[str], ProgramResolution],
    candidate_kind: str,
    base_facts: Sequence[ObjectFact],
) -> ObjectDecision:
    """§3.2 未绑定 program_name + 具体对象候选：保存候选优先，事实不足失败关闭。

    R9-H12 删除“本次真实入口名不同 -> UNRELATED”的提前返回（H7/H8 旧语义），
    并删除“当前路径解析到别处 -> UNRELATED”（查找环境不是对象事实）。
    """
    facts = list(base_facts)
    saved = _saved_candidates(item)
    resolution = resolver(limit_value) if limit_value else ProgramResolution(name="")
    candidate_path = resolve_path(candidate_value, _coerce_base(effect_base)) or _clean(candidate_value)
    if resolution.path:
        facts.append(
            _fact(
                "entry_path",
                expected=resolution.path,
                observed=candidate_path,
                source="path_lookup",
                equal=_path_equal(candidate_path, resolution.path),
            )
        )
    # 1) 候选不存在：可靠无关（先判；不存在的对象不可能是被禁止对象）。
    if not _exists(candidate_path):
        facts.append(_fact("exists", observed="false", source="path_compare", equal=False))
        return _unrelated(
            "candidate_not_exists",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=facts,
        )
    # 2) 保存候选（纯数据）优先参与判断：路径/身份相关 -> UNBOUND，否则失败关闭。
    if saved:
        path_match = _saved_path_match(candidate_path, saved, effect_base=effect_base)
        if path_match is not None:
            facts.append(
                _fact(
                    "saved_candidate_match",
                    expected=_clean(path_match.get("path")),
                    observed=candidate_path,
                    source="path_compare",
                    equal=True,
                )
            )
            return _decision(
                ObjectState.UNBOUND,
                "binding_missing_saved_candidate",
                operation=operation,
                candidate_kind=candidate_kind,
                candidate_value=candidate_value,
                facts=facts,
                detail="本次对象与保存时记录的候选路径相同，但保存时没有形成对象绑定：需要先完成程序名称的专用恢复/澄清",
            )
        observed_for_saved = _observe_path(candidate_path, cache)
        identity_match = _saved_identity_match(observed_for_saved, saved)
        if identity_match is not None:
            facts.append(
                _fact(
                    "saved_candidate_identity",
                    expected=_clean(identity_match.get("identity")),
                    observed=observed_for_saved[0],
                    source="file_identity",
                    equal=True,
                )
            )
            return _decision(
                ObjectState.UNBOUND,
                "binding_missing_saved_candidate_identity",
                operation=operation,
                candidate_kind=candidate_kind,
                candidate_value=candidate_value,
                facts=facts,
                detail="本次对象与保存时记录的候选身份相同，但保存时没有形成对象绑定：需要先完成程序名称的专用恢复/澄清",
            )
        return _decision(
            ObjectState.UNKNOWN,
            "binding_missing_candidate_not_in_saved",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=facts,
            detail="保存时记录了候选但没有形成绑定；本次对象与保存候选都不匹配，不能排除硬链接/别名，需先澄清",
        )
    # 3) 无保存候选：只有身份事实能建立“相关”或“无关”的可靠结论。
    if not resolution.resolved or not resolution.path:
        entry = _effect_entry_name(effect)
        if entry and _entry_equal(entry, limit_value):
            return _decision(
                ObjectState.UNBOUND,
                "binding_missing_entry_same",
                operation=operation,
                candidate_kind=candidate_kind,
                candidate_value=candidate_value,
                facts=facts,
                detail="限制中的程序名称尚未可靠绑定到明确对象，本次调用的入口名与之相同：需要先完成程序名称的专用恢复/澄清",
            )
        return _decision(
            ObjectState.UNKNOWN,
            "binding_missing_lookup_unresolved",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=facts,
            detail="保存时没有形成对象绑定，当前也无法澄清程序名称对应的对象：不能静默放行",
        )
    observed = _observe_path(candidate_path, cache)
    if observed is None:
        return _decision(
            ObjectState.UNKNOWN,
            "binding_missing_candidate_unreadable",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=facts,
            detail="本次对象存在但文件身份无法读取：无法确认与程序名称的关系，不能默认无关",
        )
    comparison = compare_bound_identity(
        resolution.identity_key,
        resolution.birth_key,
        observed_identity=observed[0],
        observed_birth=observed[1],
    )
    facts.extend(comparison.facts)
    if comparison.state == IDENTITY_SAME:
        return _decision(
            ObjectState.UNBOUND,
            "binding_missing_same_object",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=facts,
            detail="限制中的程序名称当前解析到本次对象，但保存时没有形成对象绑定：需要先完成程序名称的专用恢复/澄清，普通批准不能解除",
        )
    if comparison.state in (IDENTITY_DIFFERENT_OBJECT, IDENTITY_DIFFERENT_LIFECYCLE):
        return _decision(
            ObjectState.UNKNOWN,
            "binding_missing_resolved_elsewhere",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=facts,
            detail="当前查找环境把限制名称解析到别的对象，但保存时没有形成绑定：查找环境事实不能证明无关（失败关闭）",
        )
    return _decision(
        ObjectState.UNKNOWN,
        "binding_missing_lifecycle_unverifiable",
        operation=operation,
        candidate_kind=candidate_kind,
        candidate_value=candidate_value,
        facts=facts,
        detail="程序名称解析对象与本次对象的生命周期无法确认，不能当作无关",
    )


def _program_name_decision(
    item: Mapping[str, Any],
    candidate_kind: str,
    candidate_value: str,
    *,
    operation: str | None,
    effect: Any,
    workspace: Any,
    effect_base: Any,
    resolver: Callable[[str], ProgramResolution],
    cache: dict | None,
) -> ObjectDecision:
    limit_value = _clean(item.get("value"))
    base_facts = _base_facts(operation, candidate_kind, candidate_value)
    base_facts.extend(_saved_candidate_facts(item))
    binding, binding_invalid = _binding_state(item)
    if binding_invalid:
        return _decision(
            ObjectState.UNKNOWN,
            "binding_incomplete",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=base_facts,
            detail="保存的程序名称绑定结构不完整：不能确认对象，需先恢复/澄清绑定",
        )
    entry_name = _effect_entry_name(effect)
    cand_entry = _candidate_entry_name(candidate_kind, candidate_value)
    if cand_entry is not None:
        return _name_branch(
            limit_value,
            cand_entry,
            binding=binding,
            item=item,
            operation=operation,
            cache=cache,
            resolver=resolver,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            base_facts=base_facts,
        )
    if binding is not None:
        primary = _bound_path_decision(
            binding,
            candidate_value,
            operation=operation,
            effect_base=effect_base,
            cache=cache,
            candidate_kind=candidate_kind,
            base_facts=base_facts,
        )
    else:
        primary = _unbound_path_decision(
            limit_value,
            candidate_value,
            item=item,
            operation=operation,
            effect=effect,
            workspace=workspace,
            effect_base=effect_base,
            cache=cache,
            resolver=resolver,
            candidate_kind=candidate_kind,
            base_facts=base_facts,
        )
    if entry_name:
        secondary = _name_branch(
            limit_value,
            entry_name,
            binding=binding,
            item=item,
            operation=operation,
            cache=cache,
            resolver=resolver,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            base_facts=base_facts,
        )
        return _merge_decisions(primary, secondary)
    return primary


# ---------------------------------------------------------------- 唯一入口


def _decide_object_impl(
    item: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    operation: str | None = None,
    effect: Any = None,
    workspace: Any = None,
    effect_base: Any = None,
    resolver: Callable[[str], ProgramResolution] = resolve_program_name,
    cache: dict | None = None,
) -> ObjectDecision:
    """内部实现：决定 state/basis/facts；object_kind/object_value 由公开入口回填。"""
    limit_kind = _clean(item.get("kind")).lower() or "path"
    limit_value = _clean(item.get("value"))
    candidate_kind = _clean(candidate.get("kind")).lower() or "path"
    candidate_value = _clean(candidate.get("value"))
    base_facts = _base_facts(operation, candidate_kind, candidate_value)

    # §3.1 操作范围先过滤（限制条目合并传入 operations 时生效；引擎层也会先过滤）。
    limit_operations = _operation_set(item.get("operations"))
    if limit_operations:
        normalized_operation = _normalize_operation(operation)
        if normalized_operation is not None and normalized_operation not in limit_operations:
            facts = list(base_facts)
            facts.append(
                _fact(
                    "operation_scope",
                    expected=",".join(sorted(limit_operations)),
                    observed=normalized_operation,
                    source="requirement_limit",
                    equal=False,
                )
            )
            return _unrelated(
                "operation_out_of_scope",
                operation=operation,
                candidate_kind=candidate_kind,
                candidate_value=candidate_value,
                facts=facts,
            )

    if not limit_value:
        return _decision(
            ObjectState.UNKNOWN,
            "limit_object_missing_value",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=base_facts,
            detail="限制对象缺少 value，无法核对",
        )
    if candidate_kind not in _CANDIDATE_KINDS:
        return _decision(
            ObjectState.UNKNOWN,
            "unsupported_candidate_kind",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=base_facts,
            detail="候选对象类别无法核对，不能当作无关",
        )
    if not candidate_value:
        return _decision(
            ObjectState.UNKNOWN,
            "candidate_missing_value",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=base_facts,
            detail="本次影响对象缺少 value，无法核对",
        )

    # §3.6 service（跨类别是可靠无关：URL/主机名不是文件系统对象）。
    if limit_kind == "service" or candidate_kind in _SERVICE_CANDIDATE_KINDS:
        if limit_kind != "service" or candidate_kind not in _SERVICE_CANDIDATE_KINDS:
            facts = list(base_facts)
            facts.append(_fact("limit_kind", observed=limit_kind, source="requirement_limit"))
            return _unrelated(
                "object_kind_out_of_scope",
                operation=operation,
                candidate_kind=candidate_kind,
                candidate_value=candidate_value,
                facts=facts,
            )
        matched, scoped = _service_matches(candidate_value, limit_value)
        if matched:
            return _decision(
                ObjectState.MATCH,
                "service_scope_same" if scoped else "service_same",
                operation=operation,
                candidate_kind=candidate_kind,
                candidate_value=candidate_value,
                facts=base_facts,
            )
        facts = list(base_facts)
        facts.append(
            _fact(
                "service_scope",
                expected=limit_value,
                observed=candidate_value,
                source="requirement_limit",
                equal=False,
            )
        )
        return _unrelated(
            "service_outside",
            operation=operation,
            candidate_kind=candidate_kind,
            candidate_value=candidate_value,
            facts=facts,
        )

    if limit_kind in _PATH_KINDS:
        name = _candidate_entry_name(candidate_kind, candidate_value)
        if name is not None:
            return _path_limit_name_decision(
                limit_kind,
                limit_value,
                name,
                operation=operation,
                workspace=workspace,
                effect_base=effect_base,
                cache=cache,
                resolver=resolver,
                candidate_kind=candidate_kind,
                candidate_value=candidate_value,
                base_facts=base_facts,
                saved_object_facts=_saved_object_facts(item),
            )
        return _path_limit_decision(
            limit_kind,
            limit_value,
            candidate_value,
            operation=operation,
            workspace=workspace,
            effect_base=effect_base,
            cache=cache,
            candidate_kind=candidate_kind,
            base_facts=base_facts,
            saved_object_facts=_saved_object_facts(item),
        )

    if limit_kind == "program":
        name = _candidate_entry_name(candidate_kind, candidate_value)
        if name is not None:
            return _program_limit_name_decision(
                limit_value,
                name,
                operation=operation,
                workspace=workspace,
                effect_base=effect_base,
                cache=cache,
                resolver=resolver,
                candidate_kind=candidate_kind,
                candidate_value=candidate_value,
                base_facts=base_facts,
                saved_object_facts=_saved_object_facts(item),
            )
        # 显式程序路径就是文件对象：按 §3.2 path 规则核对。
        return _path_limit_decision(
            "path",
            limit_value,
            candidate_value,
            operation=operation,
            workspace=workspace,
            effect_base=effect_base,
            cache=cache,
            candidate_kind=candidate_kind,
            base_facts=base_facts,
            saved_object_facts=_saved_object_facts(item),
        )

    if limit_kind == "program_name":
        return _program_name_decision(
            item,
            candidate_kind,
            candidate_value,
            operation=operation,
            effect=effect,
            workspace=workspace,
            effect_base=effect_base,
            resolver=resolver,
            cache=cache,
        )

    return _decision(
        ObjectState.UNKNOWN,
        "unsupported_limit_object_kind",
        operation=operation,
        candidate_kind=candidate_kind,
        candidate_value=candidate_value,
        facts=base_facts,
        detail="限制对象类别无法核对，不能当作无关",
    )


def decide_object(
    item: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    operation: str | None = None,
    effect: Any = None,
    workspace: Any = None,
    effect_base: Any = None,
    resolver: Callable[[str], ProgramResolution] = resolve_program_name,
    cache: dict | None = None,
) -> ObjectDecision:
    """唯一对象判定入口：限制对象条目 vs 本次真实影响对象。绝不返回布尔。

    object_kind/object_value 记录**限制对象**的类别与值（引擎的展示原因与审计按它
    判断"程序名称对象"）；本次候选对象的事实全部进入 facts（candidate 字段）。
    """
    decision = _decide_object_impl(
        item,
        candidate,
        operation=operation,
        effect=effect,
        workspace=workspace,
        effect_base=effect_base,
        resolver=resolver,
        cache=cache,
    )
    return ObjectDecision(
        state=decision.state,
        basis=decision.basis,
        operation=decision.operation,
        object_kind=_clean(item.get("kind")).lower() or "path",
        object_value=_clean(item.get("value")) or None,
        facts=decision.facts,
        detail=decision.detail,
    )


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
