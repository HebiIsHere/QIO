"""共同对象判定：限制对象 vs 本次操作真实对象的唯一结论（R9-H11 §2）。

为什么需要独立模块：
* 旧实现用布尔返回值衔接对象判定；"绑定缺失/绑定失效/事实不足"都退化为
  False（=无关），随后被模式规则放行（历史无绑定仍可执行/读取；替换后旧硬
  链接失去保护；编号复用被放行）。
* 本模块只回答一个问题：**本次操作的真实对象与这条限制的对象是什么关系**，
  返回五态结构（MATCH/UNRELATED/UNBOUND/INVALID/UNKNOWN）+ 可审计 facts，
  绝不返回布尔。原因文字只用于说明，不参与判定。

五态含义：
* MATCH     有效匹配：保存身份/生命周期与候选一致，或路径/入口结构化匹配。
* UNRELATED 有可靠依据证明无关（唯一允许按模式继续的状态）。
* UNBOUND   绑定缺失：明确禁止但保存时未形成对象绑定，需专用恢复/澄清。
* INVALID   绑定失效：原绑定路径对象被替换/删除，或编号相同但生命周期不同。
* UNKNOWN   事实不足，无法可靠判断（失败关闭，不能当无关）。

约束：
* 生命周期只来自文件身份（birth_key）；禁止用内容摘要/mtime/名称相似/调用方
  自报代替。
* 不按名称字符串/basename/后缀猜测相关性；入口名只做平台规范化后的完全相等。
* 澄清查找（resolve_program_name）只读、不持久化、不补建绑定、不改变历史 JSON。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


class ObjectState(str, Enum):
    MATCH = "match"
    UNRELATED = "unrelated"
    UNBOUND = "unbound"
    INVALID = "invalid"
    UNKNOWN = "unknown"


#: 决策码（追加；授权行为只认结构化码，不解析中文 reason）。
DECISION_BINDING_INVALIDATED = "program_binding_invalidated"
DECISION_OBJECT_UNCONFIRMED = "object_state_unconfirmed"

_PATH_KINDS = frozenset({"path", "path_prefix", "directory"})

_MAX_FACT_TEXT = 300


def _clip(value: object, limit: int = _MAX_FACT_TEXT) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


@dataclass(frozen=True)
class ObjectFact:
    """一条可审计的结构化事实（expected/observed/source/equal）。"""

    field: str
    expected: str | None = None
    observed: str | None = None
    source: str = ""
    equal: bool | None = None

    def as_dict(self) -> dict:
        return {
            "field": str(self.field),
            "expected": _clip(self.expected),
            "observed": _clip(self.observed),
            "source": str(self.source or ""),
            "equal": self.equal,
        }


@dataclass(frozen=True)
class IdentityComparison:
    """共同身份契约的结果（匹配与失效检查共用，禁止各分支自己比字段）。"""

    state: str  # same / different_object / different_lifecycle / lifecycle_unverifiable / identity_unavailable
    facts: tuple[ObjectFact, ...] = ()

    def as_dict(self) -> dict:
        return {"state": self.state, "facts": [item.as_dict() for item in self.facts]}


@dataclass(frozen=True)
class ProgramResolution:
    """程序名称只读澄清结果；不持久化、不补建绑定。"""

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
    """限制对象与本次对象的唯一判定结果。"""

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
        return {
            "state": self.state.value,
            "basis": str(self.basis or ""),
            "operation": self.operation,
            "object_kind": self.object_kind,
            "object_value": _clip(self.object_value),
            "facts": [item.as_dict() for item in self.facts],
            "detail": _clip(self.detail),
        }


@dataclass(frozen=True)
class LimitAssessment:
    """一次操作对当前限制集合的聚合结果。"""

    decisions: tuple[ObjectDecision, ...] = ()
    reason: str = ""
    decision_code: str | None = None

    @property
    def conflict(self) -> bool:
        return any(
            item.matched or item.needs_recovery or item.fail_closed
            for item in self.decisions
        )

    @property
    def matched(self) -> bool:
        return any(item.matched for item in self.decisions)

    @property
    def needs_recovery(self) -> bool:
        return any(item.needs_recovery for item in self.decisions)

    @property
    def fail_closed(self) -> bool:
        return any(item.fail_closed for item in self.decisions)

    def as_dict(self) -> dict:
        return {
            "conflict": self.conflict,
            "reason": _clip(self.reason),
            "decision_code": self.decision_code,
            "decisions": [item.as_dict() for item in self.decisions],
        }


# ---------------------------------------------------------------- 路径与身份


def _norm_text(value: object) -> str:
    text = str(value or "").strip()
    return os.path.normcase(text) if hasattr(os, "normcase") else text


def resolve_path(value: object, base: Any) -> str | None:
    """规范化绝对路径（expanduser+resolve）；不可解析返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            if base is None:
                return None
            candidate = Path(str(base)) / candidate
        return str(candidate.resolve())
    except (OSError, ValueError):
        return None


def same_path_text(left: object, right: object) -> bool:
    """分隔符/大小写/尾斜杠规范化后的文本相等（不可解析时的唯一可靠依据）。"""

    def _norm(value: object) -> str:
        text = str(value or "").strip().replace("\\", "/").rstrip("/")
        try:
            return os.path.normcase(text)
        except (TypeError, ValueError):  # pragma: no cover - 极端输入
            return text

    a = _norm(left)
    b = _norm(right)
    return bool(a) and a == b


def _canonical_path(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        resolved = os.path.realpath(text)
        return os.path.normcase(os.path.normpath(resolved)) if os.name == "nt" else os.path.normpath(resolved)
    except (OSError, ValueError):
        return None


def _path_equal(left: object, right: object) -> bool:
    a = _canonical_path(left)
    b = _canonical_path(right)
    return bool(a) and bool(b) and a == b


def _is_within(candidate: str, target: str) -> bool:
    try:
        left = Path(candidate)
        right = Path(target)
        return left == right or left.is_relative_to(right)
    except (OSError, ValueError):
        return False


def _is_bare_entry(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if "/" in text or "\\" in text:
        return False
    if os.name == "nt" and ":" in text:
        return False
    return True


def _entry_key(value: object) -> str:
    text = str(value or "").strip()
    return os.path.normcase(text) if hasattr(os, "normcase") else text


def _entry_equal(left: object, right: object) -> bool:
    a = _entry_key(left)
    b = _entry_key(right)
    return bool(a) and a == b


def _exists(path: object) -> bool:
    try:
        return os.path.exists(str(path))
    except (OSError, ValueError):
        return False


def _identity_of(path: object, cache: dict | None = None) -> tuple[str, str | None] | None:
    """(identity_key, birth_key)；不可读返回 None（调用方按不可核对处理）。"""
    key = None
    if cache is not None:
        key = "id:" + str(_canonical_path(path) or path)
        if key in cache:
            return cache[key]
    result: tuple[str, str | None] | None
    try:
        from agent.services.file_identity import identity_of

        found = identity_of(Path(str(path)))
        if found is None:
            result = None
        else:
            birth = getattr(found, "birth_key", None)
            result = (str(found.key), str(birth) if birth else None)
    except Exception:  # noqa: BLE001 - 身份不可读按不可核对（调用方不猜测）
        result = None
    if cache is not None and key is not None:
        cache[key] = result
    return result


def resolve_program_name(name: str) -> ProgramResolution:
    """当前 PATH 只读查找（澄清用）：不写库、不补建绑定、不改变历史 JSON。"""
    text = str(name or "").strip()
    if not text:
        return ProgramResolution(name=text, error="empty_name")
    try:
        import shutil

        found = shutil.which(text)
    except Exception:  # noqa: BLE001 - 查找失败只表示"当前无法澄清"
        found = None
    if not found:
        return ProgramResolution(name=text, error="not_found")
    try:
        resolved = os.path.realpath(found)
        identity = _identity_of(resolved)
        if identity is None:
            return ProgramResolution(
                name=text, path=resolved, error="identity_unavailable"
            )
        return ProgramResolution(
            name=text,
            path=resolved,
            identity_key=identity[0],
            birth_key=identity[1],
            error="",
        )
    except (OSError, ValueError):  # pragma: no cover - 极端路径
        return ProgramResolution(name=text, error="resolve_failed")


def compare_bound_identity(
    bound_identity: str | None,
    bound_birth: str | None,
    *,
    candidate_path: str | None = None,
    observed_identity: str | None = None,
    observed_birth: str | None = None,
    cache: dict | None = None,
) -> IdentityComparison:
    """共同身份契约：identity + 生命周期一起核对，匹配/失效检查共用。"""
    facts: list[ObjectFact] = []
    bound_identity = str(bound_identity or "").strip() or None
    bound_birth = str(bound_birth or "").strip() or None
    if observed_identity is None and candidate_path is not None:
        observed = _identity_of(candidate_path, cache)
        if observed is None:
            facts.append(
                ObjectFact("identity_key", bound_identity, None, "file_identity", None)
            )
            return IdentityComparison("identity_unavailable", tuple(facts))
        observed_identity, observed_birth = observed
    observed_identity = str(observed_identity or "").strip() or None
    observed_birth = str(observed_birth or "").strip() or None
    if not bound_identity or not observed_identity:
        facts.append(
            ObjectFact(
                "identity_key",
                bound_identity,
                observed_identity,
                "file_identity",
                None,
            )
        )
        return IdentityComparison("identity_unavailable", tuple(facts))
    same_identity = bound_identity == observed_identity
    facts.append(
        ObjectFact(
            "identity_key", bound_identity, observed_identity, "file_identity", same_identity
        )
    )
    if not same_identity:
        return IdentityComparison("different_object", tuple(facts))
    if bound_birth and observed_birth:
        same_birth = bound_birth == observed_birth
        facts.append(
            ObjectFact("birth_key", bound_birth, observed_birth, "file_identity", same_birth)
        )
        return IdentityComparison("same" if same_birth else "different_lifecycle", tuple(facts))
    if bound_birth and not observed_birth:
        facts.append(ObjectFact("birth_key", bound_birth, None, "file_identity", None))
        return IdentityComparison("lifecycle_unverifiable", tuple(facts))
    # 保存时没有生命周期依据：只能按 identity 匹配（weak lifecycle，fact 明示）。
    facts.append(ObjectFact("birth_key", None, observed_birth, "program_binding", None))
    return IdentityComparison("same", tuple(facts))


def decision_code_for(state: ObjectState, object_kind: str | None) -> str | None:
    if state is ObjectState.INVALID:
        return DECISION_BINDING_INVALIDATED
    if state is ObjectState.UNKNOWN:
        return DECISION_OBJECT_UNCONFIRMED
    if state is ObjectState.UNBOUND:
        # 名称对象未绑定沿用既有专用码；其它对象类型同样需要恢复/澄清。
        return "program_name_requires_object_binding"
    return None


# ---------------------------------------------------------------- 判定


def _decision(
    state: ObjectState,
    basis: str,
    *,
    operation: str | None,
    object_kind: str | None,
    object_value: str | None,
    facts: Sequence[ObjectFact] = (),
    detail: str = "",
) -> ObjectDecision:
    return ObjectDecision(
        state=state,
        basis=basis,
        operation=operation,
        object_kind=object_kind,
        object_value=object_value,
        facts=tuple(facts),
        detail=detail,
    )


def _requested_entry(effect: Any) -> str:
    detail = getattr(effect, "detail", {}) if effect is not None else {}
    detail = detail if isinstance(detail, Mapping) else {}
    execution = detail.get("execution") if isinstance(detail.get("execution"), Mapping) else {}
    for raw in (
        detail.get("requested_program"),
        execution.get("requested_program"),
        execution.get("program"),
    ):
        text = str(raw or "").strip()
        if text:
            return text
    return ""


def _service_matches(candidate: object, limit: object) -> bool:
    left = str(candidate or "").strip().lower()
    right = str(limit or "").strip().lower()
    if not left or not right:
        return False
    return left == right or left.split("/")[0] == right.split("/")[0]


def _path_limit_decision(
    limit_kind: str,
    limit_value: str,
    cand_value: str,
    *,
    operation: str | None,
    workspace: Any,
    effect_base: Any,
    cache: dict | None,
) -> ObjectDecision:
    common = dict(
        operation=operation, object_kind=limit_kind, object_value=limit_value
    )
    limit_base = workspace if workspace is not None else effect_base
    target = resolve_path(limit_value, limit_base)
    candidate = resolve_path(cand_value, effect_base)
    facts = [
        ObjectFact(
            "path",
            _clip(target) or _clip(limit_value),
            _clip(candidate) or _clip(cand_value),
            "path_compare",
            _path_equal(candidate, target) if candidate and target else None,
        )
    ]
    if candidate is None or target is None:
        if same_path_text(cand_value, limit_value):
            return _decision(
                ObjectState.MATCH, "path_text_same", facts=facts, **common
            )
        return _decision(
            ObjectState.UNKNOWN,
            "path_unresolvable",
            facts=facts,
            detail="路径无法可靠规范化（基准不可确定）",
            **common,
        )
    if limit_kind in ("path_prefix", "directory"):
        if _is_within(candidate, target):
            return _decision(
                ObjectState.MATCH, "path_prefix_contains", facts=facts, **common
            )
        return _decision(
            ObjectState.UNRELATED, "path_prefix_outside", facts=facts, **common
        )
    if _path_equal(candidate, target):
        return _decision(ObjectState.MATCH, "path_same", facts=facts, **common)
    target_identity = _identity_of(target, cache)
    if target_identity is None:
        facts.append(
            ObjectFact("identity_key", None, None, "file_identity", None)
        )
        if not _exists(target):
            # 路径型限制没有保存身份；限制路径本身已不存在时不能把无关路径
            # 扩大成限制（也不做全盘别名扫描）。
            return _decision(
                ObjectState.UNRELATED, "path_target_missing", facts=facts, **common
            )
        return _decision(
            ObjectState.UNKNOWN,
            "path_target_identity_unavailable",
            facts=facts,
            **common,
        )
    if not _exists(candidate):
        return _decision(
            ObjectState.UNRELATED, "path_candidate_missing", facts=facts, **common
        )
    comparison = compare_bound_identity(
        target_identity[0],
        target_identity[1],
        candidate_path=candidate,
        cache=cache,
    )
    facts.extend(comparison.facts)
    if comparison.state == "same":
        return _decision(
            ObjectState.MATCH, "path_hardlink_same_object", facts=facts, **common
        )
    if comparison.state == "different_object":
        return _decision(
            ObjectState.UNRELATED, "path_different_object", facts=facts, **common
        )
    if comparison.state == "different_lifecycle":
        return _decision(
            ObjectState.UNRELATED, "path_different_lifecycle", facts=facts, **common
        )
    return _decision(
        ObjectState.UNKNOWN,
        "path_identity_unverifiable",
        facts=facts,
        **common,
    )


def _bound_object_decision(
    binding: Mapping[str, Any],
    cand_value: str,
    *,
    common: dict,
    cache: dict | None,
    entry: str,
    limit_value: str,
) -> ObjectDecision:
    """候选是具体程序/文件对象时，与保存绑定身份+生命周期核对。"""
    bound_identity = str(binding.get("identity") or "").strip()
    bound_birth = str(binding.get("birth_key") or "").strip() or None
    bound_path = str(binding.get("path") or "").strip()
    if not bound_identity:
        # 结构不完整的绑定按"绑定缺失"处理（由调用方走向 UNBOUND 分支）。
        return _decision(
            ObjectState.UNKNOWN,
            "binding_malformed",
            detail="保存的绑定缺少身份字段",
            **common,
        )
    facts: list[ObjectFact] = []
    if bound_path:
        same_bound_path = _path_equal(cand_value, bound_path)
        facts.append(
            ObjectFact("path", bound_path, cand_value, "program_binding", same_bound_path)
        )
    else:
        same_bound_path = False
    if not _exists(cand_value):
        if same_bound_path:
            return _decision(
                ObjectState.INVALID,
                "bound_object_missing",
                facts=facts,
                detail="原绑定路径上的对象已不存在",
                **common,
            )
        return _decision(
            ObjectState.UNRELATED, "candidate_missing", facts=facts, **common
        )
    comparison = compare_bound_identity(
        bound_identity, bound_birth, candidate_path=cand_value, cache=cache
    )
    facts.extend(comparison.facts)
    if comparison.state == "same":
        return _decision(
            ObjectState.MATCH, "binding_identity_same", facts=facts, **common
        )
    if comparison.state == "different_lifecycle":
        return _decision(
            ObjectState.INVALID,
            "lifecycle_changed_same_identity",
            facts=facts,
            detail="编号相同但生命周期不同：属于另一个生命周期对象",
            **common,
        )
    if comparison.state == "lifecycle_unverifiable":
        return _decision(
            ObjectState.UNKNOWN,
            "lifecycle_unverifiable",
            facts=facts,
            detail="保存了生命周期依据但当前无法取得，不能默认一致",
            **common,
        )
    if comparison.state == "identity_unavailable":
        return _decision(
            ObjectState.UNKNOWN, "identity_unavailable", facts=facts, **common
        )
    # different_object
    if same_bound_path:
        return _decision(
            ObjectState.INVALID,
            "bound_path_object_replaced",
            facts=facts,
            detail="原绑定路径上的对象已被替换",
            **common,
        )
    if entry and _is_bare_entry(entry) and _entry_equal(entry, limit_value):
        return _decision(
            ObjectState.INVALID,
            "entry_name_same_object_differs",
            facts=facts,
            detail="同一来源名当前解析到另一个对象",
            **common,
        )
    return _decision(
        ObjectState.UNRELATED, "different_object_identity", facts=facts, **common
    )


def _program_name_decision(
    item: Mapping[str, Any],
    cand_kind: str,
    cand_value: str,
    *,
    operation: str | None,
    effect: Any,
    resolver: Callable[[str], ProgramResolution],
    cache: dict | None,
) -> ObjectDecision:
    limit_value = str(item.get("value") or "").strip()
    common = dict(
        operation=operation, object_kind="program_name", object_value=limit_value
    )
    raw_binding = item.get("program_binding")
    binding = raw_binding if isinstance(raw_binding, Mapping) else None
    entry = _requested_entry(effect)
    cand_is_entry = cand_kind == "program_name" or (
        cand_kind == "program" and _is_bare_entry(cand_value)
    )
    if cand_is_entry:
        if binding and str(binding.get("identity") or "").strip():
            resolution = resolver(cand_value)
            if resolution.resolved:
                comparison = compare_bound_identity(
                    str(binding.get("identity") or ""),
                    str(binding.get("birth_key") or "").strip() or None,
                    observed_identity=resolution.identity_key,
                    observed_birth=resolution.birth_key,
                    cache=cache,
                )
                facts = list(comparison.facts)
                if comparison.state == "same":
                    return _decision(
                        ObjectState.MATCH,
                        "entry_name_same_object_same",
                        facts=facts,
                        **common,
                    )
                if comparison.state in ("different_object", "different_lifecycle"):
                    return _decision(
                        ObjectState.INVALID,
                        "entry_name_same_object_differs",
                        facts=facts,
                        **common,
                    )
                if comparison.state == "lifecycle_unverifiable":
                    return _decision(
                        ObjectState.UNKNOWN,
                        "lifecycle_unverifiable",
                        facts=facts,
                        **common,
                    )
            # 同一来源名但当前无法解析：按绑定失效处理（不能当无关）。
            if _entry_equal(cand_value, limit_value):
                return _decision(
                    ObjectState.INVALID,
                    "entry_name_same_unresolvable",
                    detail="同一来源名当前无法解析到保存对象",
                    **common,
                )
            return _decision(
                ObjectState.UNRELATED, "different_entry_name", **common
            )
        # 绑定缺失（UNBOUND 来源）
        if _entry_equal(cand_value, limit_value):
            return _decision(
                ObjectState.UNBOUND, "binding_missing_entry_same", **common
            )
        resolution = resolver(cand_value)
        bound_res = resolver(limit_value) if limit_value else ProgramResolution(name="")
        if resolution.resolved and bound_res.resolved:
            if resolution.identity_key == bound_res.identity_key:
                return _decision(
                    ObjectState.UNBOUND,
                    "binding_missing_entry_alias",
                    **common,
                )
        return _decision(ObjectState.UNRELATED, "different_entry_name", **common)
    # 候选是具体对象（程序/文件）
    if binding and str(binding.get("identity") or "").strip():
        return _bound_object_decision(
            binding,
            cand_value,
            common=common,
            cache=cache,
            entry=entry,
            limit_value=limit_value,
        )
    resolution = resolver(limit_value) if limit_value else ProgramResolution(name="")
    if resolution.resolved and resolution.path:
        facts = [
            ObjectFact("path", resolution.path, cand_value, "path_lookup", _path_equal(cand_value, resolution.path)),
            ObjectFact(
                "identity_key",
                resolution.identity_key,
                None,
                "path_lookup",
                None,
            ),
        ]
        if not _exists(cand_value):
            return _decision(
                ObjectState.UNRELATED,
                "binding_missing_candidate_missing",
                facts=facts,
                **common,
            )
        observed = _identity_of(cand_value, cache)
        if observed is None:
            return _decision(
                ObjectState.UNKNOWN,
                "binding_missing_identity_unavailable",
                facts=facts,
                **common,
            )
        same_identity = observed[0] == resolution.identity_key
        facts.append(
            ObjectFact(
                "identity_key",
                resolution.identity_key,
                observed[0],
                "file_identity",
                same_identity,
            )
        )
        if same_identity:
            return _decision(
                ObjectState.UNBOUND,
                "binding_missing_same_object",
                facts=facts,
                detail="名称当前解析到本次对象，但保存时没有形成绑定",
                **common,
            )
        return _decision(
            ObjectState.UNRELATED,
            "binding_missing_resolved_elsewhere",
            facts=facts,
            **common,
        )
    # 当前无法澄清名称：只有"不同裸入口名"是可靠无关；其余失败关闭。
    if entry and _is_bare_entry(entry) and not _entry_equal(entry, limit_value):
        return _decision(ObjectState.UNRELATED, "different_entry_name", **common)
    if not _exists(cand_value) and not _path_equal(cand_value, ""):
        return _decision(
            ObjectState.UNRELATED,
            "binding_missing_candidate_missing",
            **common,
        )
    return _decision(
        ObjectState.UNKNOWN,
        "binding_missing_lookup_unresolved",
        detail="保存时没有绑定对象，当前也无法可靠澄清",
        **common,
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
    """唯一对象判定入口：限制对象条目 vs 本次影响对象。绝不返回布尔。"""
    limit_kind = str(item.get("kind") or "path").strip().lower()
    limit_value = str(item.get("value") or "").strip()
    cand_kind = str(candidate.get("kind") or "path").strip().lower()
    cand_value = str(candidate.get("value") or "").strip()
    if not limit_value:
        return _decision(
            ObjectState.UNKNOWN,
            "limit_object_empty",
            operation=operation,
            object_kind=limit_kind,
            object_value=limit_value,
        )
    if limit_kind == "service" or cand_kind == "service":
        if limit_kind != "service" or cand_kind != "service":
            return _decision(
                ObjectState.UNRELATED,
                "object_kind_mismatch",
                operation=operation,
                object_kind=limit_kind,
                object_value=limit_value,
            )
        matched = _service_matches(cand_value, limit_value)
        return _decision(
            ObjectState.MATCH if matched else ObjectState.UNRELATED,
            "service_same" if matched else "service_different",
            operation=operation,
            object_kind=limit_kind,
            object_value=limit_value,
        )
    if limit_kind in _PATH_KINDS:
        if cand_kind == "service":
            return _decision(
                ObjectState.UNRELATED,
                "object_kind_mismatch",
                operation=operation,
                object_kind=limit_kind,
                object_value=limit_value,
            )
        return _path_limit_decision(
            limit_kind,
            limit_value,
            cand_value,
            operation=operation,
            workspace=workspace,
            effect_base=effect_base,
            cache=cache,
        )
    if limit_kind == "program":
        if cand_kind == "program_name" or (
            cand_kind == "program" and _is_bare_entry(cand_value)
        ):
            common = dict(
                operation=operation, object_kind="program", object_value=limit_value
            )
            if not _exists(limit_value) and not _path_equal(limit_value, ""):
                # 显式程序路径限制但目标当前不可读：按路径文本/身份尽力核对。
                pass
            target = resolve_path(limit_value, workspace if workspace is not None else effect_base)
            resolution = resolver(cand_value)
            if target is None:
                return _decision(
                    ObjectState.UNKNOWN, "program_limit_path_unresolvable", **common
                )
            if not resolution.resolved:
                return _decision(
                    ObjectState.UNKNOWN,
                    "program_entry_unresolvable",
                    detail="入口名当前无法解析到对象",
                    **common,
                )
            target_identity = _identity_of(target, cache)
            if target_identity is None:
                if _path_equal(resolution.path, target):
                    return _decision(
                        ObjectState.MATCH, "entry_name_same_path", **common
                    )
                if not _exists(target):
                    return _decision(
                        ObjectState.UNKNOWN,
                        "program_limit_target_missing",
                        **common,
                    )
                return _decision(
                    ObjectState.UNKNOWN,
                    "program_limit_identity_unavailable",
                    **common,
                )
            comparison = compare_bound_identity(
                target_identity[0],
                target_identity[1],
                observed_identity=resolution.identity_key,
                observed_birth=resolution.birth_key,
                cache=cache,
            )
            if comparison.state == "same":
                return _decision(
                    ObjectState.MATCH,
                    "entry_resolves_to_limit_path",
                    facts=comparison.facts,
                    **common,
                )
            if comparison.state in ("different_object", "different_lifecycle"):
                return _decision(
                    ObjectState.UNRELATED,
                    "entry_resolves_elsewhere",
                    facts=comparison.facts,
                    **common,
                )
            return _decision(
                ObjectState.UNKNOWN,
                "entry_identity_unverifiable",
                facts=comparison.facts,
                **common,
            )
        return _path_limit_decision(
            "path",
            limit_value,
            cand_value,
            operation=operation,
            workspace=workspace,
            effect_base=effect_base,
            cache=cache,
        )
    if limit_kind == "program_name":
        return _program_name_decision(
            item,
            cand_kind,
            cand_value,
            operation=operation,
            effect=effect,
            resolver=resolver,
            cache=cache,
        )
    return _decision(
        ObjectState.UNKNOWN,
        "unknown_limit_object_kind",
        operation=operation,
        object_kind=limit_kind,
        object_value=limit_value,
    )


__all__ = [
    "DECISION_BINDING_INVALIDATED",
    "DECISION_OBJECT_UNCONFIRMED",
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
