# -*- coding: utf-8 -*-
"""用户要求解析（R9-G，契约 docs/r9g-contracts.md §1、§3）。

每个用户 turn 开始时，用**主模型**判断这条新消息与已有 active 用户要求的语义关系：
    new      —— 新目标/新任务，不是对已有要求的补充/修正/撤回；
    amend    —— 对某条已有要求的补充/修正（目标、对象、范围、方式调整）；
    withdraw —— 撤回/取消某条已有要求。
并抽取用户明确表达的结构化限制（operations / objects / reason）。

职责边界：
* 只做语义理解，**不产生任何执行许可**；是否允许某个具体操作仍由授权审查/固定规则决定。
* 解析失败（无模型/超时/异常/输出非法/目标校验不过）→ RequirementResolutionUnavailable，
  调用方按"新要求"继续并标记 requirement_resolution_status="unavailable"；
  后端**不**补关键词、不猜测用户限制（契约 §1、§2、§3）。
* 目标要求必须来自调用方给出的有限候选（id/原话摘录/版本/时间），且版本一致；
  模型不能凭自己的记忆编造 target。
* 提示词单一来源：agent/prompts.py 的 REQUIREMENT_RESOLUTION_* 常量。

Lead 接线点（契约 §3）：
* 每个用户 turn 开始调用 ModelRequirementResolver.resolve(...)（不因模式不同而跳过）；
* 成功后 RequirementLedger.apply_resolution(outcome, message_id=..., turn_id=...,
  message=用户原话) 落实并写 requirement_resolutions 审计；失败调用
  RequirementLedger.record_resolution_failure(...)；
* 把得到的 RequirementBinding 放进 contextvar，供 engine/工具/子智能体/审批使用。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from enum import Enum
from string import Template
from typing import Any, Callable, Protocol, Sequence

from agent.adapters.base import ChatMessage
from agent.prompts import (
    LIMIT_EXTRACTION_INSTRUCTION,
    LIMIT_EXTRACTION_SCHEMA_VERSION,
    LIMIT_EXTRACTION_SYSTEM,
    REQUIREMENT_RESOLUTION_INSTRUCTION,
    REQUIREMENT_RESOLUTION_SCHEMA_VERSION,
    REQUIREMENT_RESOLUTION_SYSTEM,
)

logger = logging.getLogger(__name__)

#: 解析调用默认超时（秒）。超时一律视为不可用，不做关键词兜底。
DEFAULT_TIMEOUT_SECONDS = 20.0

MAX_MESSAGE_CHARS = 6000
MAX_CANDIDATES = 12
MAX_CANDIDATE_TEXT_CHARS = 300
MAX_CORRECTIONS = 8
MAX_CORRECTION_CHARS = 1000
MAX_LIMITS = 12
MAX_LIMIT_OPERATIONS = 8
MAX_LIMIT_OBJECTS = 8
MAX_LIMIT_REASON_CHARS = 300
MAX_REASON_CHARS = 500
MAX_TARGET_CHARS = 200
MAX_CONTEXT_ITEMS = 12
MAX_CONTEXT_JSON_CHARS = 2000

#: 结构化限制里允许出现的稳定操作名（契约 §3 limits.operations）。
ALLOWED_LIMIT_OPERATIONS = frozenset(
    {
        "read",
        "write",
        "create",
        "delete",
        "execute",
        "shell",
        "install",
        "egress",
        "system_change",
        "internal_data",
        "tool_register",
        "subagent",
    }
)

#: 结构化限制里允许的对象类别（与 ReviewObject.kind 对齐，另加 record/other 表示后端可核对对象）。
LIMIT_OBJECT_KINDS = frozenset(
    {"path", "directory", "program", "service", "material", "record", "other"}
)

RELATION_VALUES = frozenset({"new", "amend", "withdraw"})

#: JSON 输出解析用的代码围栏识别（只解析模型输出格式，不参与授权判断）。
_FENCE_MARK = chr(96) * 3
_FENCE_RE = re.compile(
    _FENCE_MARK + r"(?:json)?\s*(.*?)" + _FENCE_MARK, re.IGNORECASE | re.DOTALL
)


class RequirementRelation(str, Enum):
    NEW = "new"
    AMEND = "amend"
    WITHDRAW = "withdraw"


class RequirementResolutionUnavailable(Exception):
    """要求解析不可用：模型缺失/超时/异常/输出非法/目标校验不通过。

    调用方必须按"新要求"继续并标记 unresolved；不得改用关键词/句式兜底
    （契约 §1、§2、§3）。
    """


@dataclass(frozen=True)
class ResolutionOutcome:
    """一次要求解析的结果（模型语义判断 + 后端校验后的规范形式）。"""

    relation: RequirementRelation
    target_requirement_id: str | None
    corrections: tuple[str, ...]
    limits: tuple[dict, ...]
    reason: str
    model: str
    prompt_version: str
    #: 模型解析时看到的目标要求版本（R9-G §3 版本绑定）。
    #: amend/withdraw 必须给出并与候选版本一致；new 为 None。
    target_version: int | None = None
    #: R9-H §6：amend 时用户明确解除的限制（结构与 limits 条目相同）。
    #: 默认空表示"没有解除任何限制"；非 amend 时必须为空，不得借清空假装修正。
    removed_limits: tuple[dict, ...] = ()

    def as_dict(self) -> dict:
        return {
            "relation": self.relation.value,
            "target_requirement_id": self.target_requirement_id,
            "target_version": self.target_version,
            "corrections": list(self.corrections),
            "limits": [dict(item) for item in self.limits],
            "removed_limits": [dict(item) for item in self.removed_limits],
            "reason": self.reason,
            "model": self.model,
            "prompt_version": self.prompt_version,
        }


class RequirementResolver(Protocol):
    async def resolve(
        self,
        *,
        message: str,
        message_id: str,
        turn_id: str,
        candidates: list[dict],
    ) -> ResolutionOutcome: ...


# ---------------------------------------------------------------------------
# 输入组装与渲染（后端负责，模型不能选择性提交）
# ---------------------------------------------------------------------------


def _clip(value: object, limit: int, *, marker: str = "…（已截断）") -> str:
    text = "" if value is None else str(value)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(marker))
    return text[:keep] + marker


def _safe_json_value(value: Any, *, max_chars: int, depth: int = 0) -> Any:
    if depth > 4:
        return "[层级过深]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _clip(value, max_chars)
    if isinstance(value, dict):
        return {
            str(key)[:80]: _safe_json_value(item, max_chars=max_chars, depth=depth + 1)
            for key, item in list(value.items())[:32]
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            _safe_json_value(item, max_chars=max_chars, depth=depth + 1)
            for item in list(value)[:32]
        ]
    return _clip(value, max_chars)


def normalize_candidate(candidate: Any) -> dict | None:
    """把候选要求规范成模型可见的最小结构（只含 id/版本/原话摘录/时间/状态/对象）。"""
    if not isinstance(candidate, dict):
        return None
    requirement_id = str(candidate.get("requirement_id") or "").strip()
    if not requirement_id:
        return None
    try:
        version = int(candidate.get("version"))
    except (TypeError, ValueError):
        return None
    if version < 1:
        return None
    text = str(
        candidate.get("text")
        or candidate.get("text_excerpt")
        or candidate.get("original_text")
        or ""
    ).strip()
    payload: dict = {
        "requirement_id": requirement_id,
        "version": version,
        "text_excerpt": _clip(text, MAX_CANDIDATE_TEXT_CHARS),
        "created_at": _clip(candidate.get("created_at"), 60),
        "updated_at": _clip(candidate.get("updated_at"), 60),
        "status": _clip(candidate.get("status") or "active", 40),
        "parent_id": (
            None if candidate.get("parent_id") is None else str(candidate.get("parent_id"))[:MAX_TARGET_CHARS]
        ),
        "topic_id": (
            None if candidate.get("topic_id") is None else str(candidate.get("topic_id"))[:MAX_TARGET_CHARS]
        ),
    }
    objects = candidate.get("objects")
    if isinstance(objects, (list, tuple)) and objects:
        payload["objects"] = [
            _safe_json_value(item, max_chars=500) for item in list(objects)[:MAX_LIMIT_OBJECTS]
        ]
    return payload


def _normalized_candidates(candidates: Sequence[dict] | None) -> list[dict]:
    result: list[dict] = []
    for item in list(candidates or ())[:MAX_CANDIDATES]:
        candidate = normalize_candidate(item)
        if candidate is not None:
            result.append(candidate)
    return result


def build_resolution_payload(
    *,
    message: object,
    message_id: object,
    turn_id: object,
    candidates: Sequence[dict] | None,
    context: Sequence[dict] | None = (),
) -> dict:
    """组装要求解析输入（唯一入口，模型无法选择性提交）。"""
    return {
        "prompt_version": REQUIREMENT_RESOLUTION_SCHEMA_VERSION,
        "message": {
            "message_id": None if message_id is None else str(message_id)[:200],
            "turn_id": None if turn_id is None else str(turn_id)[:200],
            "text": _clip(message, MAX_MESSAGE_CHARS),
        },
        "candidates": _normalized_candidates(candidates),
        "related_context": [
            _safe_json_value(item, max_chars=MAX_CONTEXT_JSON_CHARS)
            for item in list(context or ())[:MAX_CONTEXT_ITEMS]
        ],
    }


def render_resolution_messages(
    *,
    message: object,
    message_id: object,
    turn_id: object,
    candidates: Sequence[dict] | None,
    context: Sequence[dict] | None = (),
) -> list[ChatMessage]:
    payload = build_resolution_payload(
        message=message,
        message_id=message_id,
        turn_id=turn_id,
        candidates=candidates,
        context=context,
    )
    payload_text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    instruction = Template(REQUIREMENT_RESOLUTION_INSTRUCTION).substitute(payload=payload_text)
    return [
        ChatMessage(role="system", content=REQUIREMENT_RESOLUTION_SYSTEM),
        ChatMessage(role="user", content=instruction),
    ]


# ---------------------------------------------------------------------------
# 输出解析与校验
# ---------------------------------------------------------------------------


def _invalid(message: str) -> RequirementResolutionUnavailable:
    return RequirementResolutionUnavailable("要求解析输出无效：" + message)


def _extract_json_object(raw_text: object) -> str:
    text = "" if raw_text is None else str(raw_text).strip()
    if not text:
        raise RequirementResolutionUnavailable("要求解析模型没有返回内容")
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise RequirementResolutionUnavailable("要求解析模型没有返回 JSON 对象")
    return text[start : end + 1]


def _optional_str(data: dict, key: str, *, max_chars: int) -> str:
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _invalid(key + " 必须是字符串")
    return _clip(value.strip(), max_chars)


def _parse_limits(data: dict, *, key: str = "limits", label: str | None = None) -> tuple[dict, ...]:
    """解析结构化限制数组（limits / removed_limits 共用同一结构）。"""
    name = label or key
    value = data.get(key, [])
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _invalid(name + " 必须是对象数组")
    result: list[dict] = []
    for item in value[:MAX_LIMITS]:
        if not isinstance(item, dict):
            raise _invalid(name + " 每一项必须是对象")
        operations_raw = item.get("operations", [])
        if not isinstance(operations_raw, list):
            raise _invalid(name + ".operations 必须是字符串数组")
        operations: list[str] = []
        for op in operations_raw[:MAX_LIMIT_OPERATIONS]:
            if not isinstance(op, str):
                raise _invalid(name + ".operations 只能包含字符串")
            text = op.strip().lower()
            if text:
                operations.append(text)
        objects_raw = item.get("objects", [])
        if not isinstance(objects_raw, list):
            raise _invalid(name + ".objects 必须是数组")
        objects: list[dict] = []
        for obj in objects_raw[:MAX_LIMIT_OBJECTS]:
            if isinstance(obj, str):
                text = obj.strip()
                if text:
                    objects.append({"kind": "other", "value": text})
                continue
            if not isinstance(obj, dict):
                raise _invalid(name + ".objects 每一项必须是对象或字符串")
            kind = str(obj.get("kind") or "other").strip().lower()
            object_value = str(obj.get("value") or "").strip()
            if not object_value:
                raise _invalid(name + ".objects 每一项必须有非空 value")
            objects.append({"kind": kind, "value": object_value})
        reason = _optional_str(item, "reason", max_chars=MAX_LIMIT_REASON_CHARS)
        result.append(
            {"operations": sorted(set(operations)), "objects": objects, "reason": reason}
        )
    return tuple(result)


def parse_resolution_outcome(
    raw_text: object,
    *,
    model: str | None = None,
    prompt_version: str = REQUIREMENT_RESOLUTION_SCHEMA_VERSION,
) -> ResolutionOutcome:
    """把模型原始文本解析成 ResolutionOutcome（只做结构解析；语义/目标校验在 validate）。"""
    payload = _extract_json_object(raw_text)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RequirementResolutionUnavailable("要求解析输出不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise _invalid("顶层必须是 JSON 对象")
    relation_raw = data.get("relation")
    if not isinstance(relation_raw, str) or not relation_raw.strip():
        raise _invalid("缺少 relation")
    try:
        relation = RequirementRelation(relation_raw.strip().lower())
    except ValueError as exc:
        raise _invalid("relation 只能是 new/amend/withdraw") from exc
    target_raw = data.get("target_requirement_id")
    if target_raw is None or target_raw == "":
        target_requirement_id = None
    elif isinstance(target_raw, str):
        target_requirement_id = target_raw.strip()[:MAX_TARGET_CHARS] or None
    else:
        raise _invalid("target_requirement_id 必须是字符串或 null")
    target_version_raw = data.get("target_version")
    if target_version_raw is None:
        target_version = None
    elif isinstance(target_version_raw, bool) or not isinstance(target_version_raw, int):
        raise _invalid("target_version 必须是整数或 null")
    else:
        target_version = int(target_version_raw)
    corrections_raw = data.get("corrections", [])
    if corrections_raw is None:
        corrections_raw = []
    if not isinstance(corrections_raw, list):
        raise _invalid("corrections 必须是字符串数组")
    corrections: list[str] = []
    for item in corrections_raw[:MAX_CORRECTIONS]:
        if not isinstance(item, str):
            raise _invalid("corrections 只能包含字符串")
        text = item.strip()
        if text:
            corrections.append(_clip(text, MAX_CORRECTION_CHARS))
    limits = _parse_limits(data, key="limits")
    removed_limits = _parse_limits(data, key="removed_limits")
    reason = _optional_str(data, "reason", max_chars=MAX_REASON_CHARS)
    return ResolutionOutcome(
        relation=relation,
        target_requirement_id=target_requirement_id,
        corrections=tuple(corrections),
        limits=limits,
        reason=reason,
        model=str(model or "")[:200],
        prompt_version=str(prompt_version or REQUIREMENT_RESOLUTION_SCHEMA_VERSION)[:100],
        target_version=target_version,
        removed_limits=removed_limits,
    )


def _normalize_object_key(kind: object, value: object) -> tuple[str, str]:
    object_kind = str(kind or "other").strip().lower() or "other"
    text = str(value or "").strip()
    if object_kind in ("path", "directory", "program"):
        try:
            text = os.path.normcase(os.path.normpath(text))
        except (OSError, ValueError):  # pragma: no cover - 极端输入
            text = text.lower()
    elif object_kind == "service":
        text = text.lower()
    return object_kind, text


def _allowed_object_keys(values: Sequence[Any] | None) -> tuple[set[tuple[str, str]], set[str]]:
    typed: set[tuple[str, str]] = set()
    bare: set[str] = set()
    for item in list(values or ()):
        if isinstance(item, dict):
            kind = item.get("kind")
            value = item.get("value")
            if value is None and isinstance(item.get("path"), str):
                kind = "path"
                value = item.get("path")
            if value is not None and str(value).strip():
                normalized = _normalize_object_key(kind, value)
                typed.add(normalized)
                # 后端可解析对象按"值"也认一次：模型可能把同一个 D:/proj 写成 path 或
                # directory，这是可核对的同一对象，不应因为类别措辞不同让整个解析不可用。
                bare.add(normalized[1])
                continue
        text = str(item or "").strip()
        if text:
            bare.add(text)
    return typed, bare


def _candidate_object_keys(candidates: Sequence[dict]) -> tuple[set[tuple[str, str]], set[str]]:
    values: list[Any] = []
    for candidate in candidates:
        for item in candidate.get("objects") or ():
            values.append(item)
    return _allowed_object_keys(values)


def _clean_limit_entries(
    entries: Sequence[dict],
    *,
    label: str,
    typed_allowed: set[tuple[str, str]],
    bare_allowed: set[str],
    require_known_objects: bool = True,
) -> tuple[dict, ...]:
    """校验并规范化结构化限制条目（limits / removed_limits / 历史提取共用）。

    只做事实核对：operations 必须是稳定操作名；objects.kind 合法。语义由模型判断，
    不做词句匹配。

    require_known_objects=False 用于两种"只收紧、不产生授权"的场景：
    * removed_limits：解除限制只是放宽，解除对象可能来自旧要求、当前候选里没有；
    * 历史原话提取：旧要求里的对象不一定能对上本轮后端可解析对象。
    这两类对象即使写错也不会产生许可，执行前仍按真实对象核对；因此只做结构校验，
    避免把合法历史限制误判成 Unavailable。
    """
    cleaned: list[dict] = []
    for limit in list(entries)[:MAX_LIMITS]:
        if not isinstance(limit, dict):
            raise RequirementResolutionUnavailable(label + " 每一项必须是对象")
        operations = [
            str(item).strip().lower()
            for item in (limit.get("operations") or ())
            if str(item).strip()
        ]
        if not operations:
            raise RequirementResolutionUnavailable(label + " 缺少 operations")
        unknown = [item for item in operations if item not in ALLOWED_LIMIT_OPERATIONS]
        if unknown:
            raise RequirementResolutionUnavailable(
                label + " 含未知操作：" + ", ".join(sorted(set(unknown))[:6])
            )
        objects_raw = limit.get("objects") or ()
        if not isinstance(objects_raw, (list, tuple)):
            raise RequirementResolutionUnavailable(label + ".objects 必须是数组")
        objects: list[dict] = []
        for item in list(objects_raw)[:MAX_LIMIT_OBJECTS]:
            if isinstance(item, dict):
                kind = str(item.get("kind") or "other").strip().lower()
                value = str(item.get("value") or "").strip()
            else:
                kind = "other"
                value = str(item or "").strip()
            if not value:
                raise RequirementResolutionUnavailable(label + ".objects 含空对象")
            if kind not in LIMIT_OBJECT_KINDS:
                raise RequirementResolutionUnavailable(label + ".objects.kind 非法：" + kind[:40])
            typed_key = _normalize_object_key(kind, value)
            if require_known_objects and (typed_allowed or bare_allowed):
                if (
                    typed_key not in typed_allowed
                    and typed_key[1] not in bare_allowed
                    and str(value) not in bare_allowed
                ):
                    raise RequirementResolutionUnavailable(
                        label + " 引用了候选/后端可解析对象之外的标识：" + value[:120]
                    )
            objects.append({"kind": kind, "value": value})
        reason = str(limit.get("reason") or "").strip()[:MAX_LIMIT_REASON_CHARS]
        cleaned.append({"operations": sorted(set(operations)), "objects": objects, "reason": reason})
    return tuple(cleaned)


def validate_resolution(
    outcome: ResolutionOutcome,
    *,
    candidates: Sequence[dict] | None,
    allowed_objects: Sequence[Any] | None = (),
) -> ResolutionOutcome:
    """后端校验模型解析结果（R9-G 契约 §3）。

    * relation 合法；reason 非空；
    * AMEND/WITHDRAW 必须给出候选里存在、版本一致的 target（new 不得带 target）；
    * limits 结构合法，operations 是稳定操作名；objects 必须来自候选对象或调用方
      给出的后端可解析对象（没有提供任何可解析对象时只做结构校验——限制本身只收紧、
      不产生授权，最终执行前仍会核对真实对象）；
    * 不落推理全文；任何不通过都抛 RequirementResolutionUnavailable。
    """
    if not isinstance(outcome, ResolutionOutcome):
        raise RequirementResolutionUnavailable("要求解析结果结构非法（不是 ResolutionOutcome）")
    if not isinstance(outcome.relation, RequirementRelation):
        raise RequirementResolutionUnavailable("要求解析结果的 relation 非法")
    if not str(outcome.reason or "").strip():
        raise RequirementResolutionUnavailable("要求解析结果缺少 reason（依据）")

    candidate_map: dict[str, dict] = {}
    for item in _normalized_candidates(candidates):
        candidate_map[item["requirement_id"]] = item

    corrections = tuple(
        str(item).strip()[:MAX_CORRECTION_CHARS]
        for item in (outcome.corrections or ())
        if str(item).strip()
    )[:MAX_CORRECTIONS]
    limits = tuple(dict(item) for item in (outcome.limits or ()) if isinstance(item, dict))[:MAX_LIMITS]
    removed_limits = tuple(
        dict(item) for item in (getattr(outcome, "removed_limits", ()) or ()) if isinstance(item, dict)
    )[:MAX_LIMITS]

    typed_allowed, bare_allowed = _allowed_object_keys(allowed_objects)
    candidate_typed, candidate_bare = _candidate_object_keys(list(candidate_map.values()))
    typed_allowed |= candidate_typed
    bare_allowed |= candidate_bare

    cleaned_limits = _clean_limit_entries(
        limits, label="limits", typed_allowed=typed_allowed, bare_allowed=bare_allowed
    )
    cleaned_removed = _clean_limit_entries(
        removed_limits,
        label="removed_limits",
        typed_allowed=typed_allowed,
        bare_allowed=bare_allowed,
        require_known_objects=False,  # 解除限制只放宽；对象可能来自旧要求
    )
    if cleaned_removed and outcome.relation != RequirementRelation.AMEND:
        raise RequirementResolutionUnavailable("只有 amend 可以解除已有要求上的限制")

    if outcome.relation == RequirementRelation.NEW:
        if outcome.target_requirement_id:
            raise RequirementResolutionUnavailable("new 不得指定 target_requirement_id")
        if outcome.target_version is not None:
            raise RequirementResolutionUnavailable("new 不得指定 target_version")
        target_requirement_id = None
        target_version = None
    else:
        target = str(outcome.target_requirement_id or "").strip()
        if not target:
            raise RequirementResolutionUnavailable(
                outcome.relation.value + " 必须给出 target_requirement_id"
            )
        candidate = candidate_map.get(target)
        if candidate is None:
            raise RequirementResolutionUnavailable("target_requirement_id 不在有限候选内：" + target[:120])
        if str(candidate.get("status") or "active") != "active":
            raise RequirementResolutionUnavailable("target 用户要求不是 active，不能修正/撤回")
        version = outcome.target_version
        if not isinstance(version, int) or isinstance(version, bool):
            raise RequirementResolutionUnavailable(outcome.relation.value + " 必须给出 target_version")
        if int(version) != int(candidate["version"]):
            raise RequirementResolutionUnavailable(
                "target 版本与候选不一致（请求=%r，候选=%r）" % (version, candidate["version"])
            )
        target_requirement_id = target
        target_version = int(version)

    return ResolutionOutcome(
        relation=outcome.relation,
        target_requirement_id=target_requirement_id,
        corrections=corrections,
        limits=tuple(cleaned_limits),
        reason=str(outcome.reason).strip()[:MAX_REASON_CHARS],
        model=str(outcome.model or "")[:200],
        prompt_version=str(outcome.prompt_version or REQUIREMENT_RESOLUTION_SCHEMA_VERSION)[:100],
        target_version=target_version,
        removed_limits=cleaned_removed,
    )


# ---------------------------------------------------------------------------
# R9-H §6：历史要求原话 → 结构化限制（重新理解，不解析展平文字）
# ---------------------------------------------------------------------------


def build_limit_extraction_payload(
    *,
    text: object,
    requirement_id: str | None = None,
    version: int | None = None,
) -> dict:
    """组装历史限制提取输入（唯一入口；只喂用户原话与来源标识，不喂展平文字）。"""
    payload: dict = {
        "prompt_version": LIMIT_EXTRACTION_SCHEMA_VERSION,
        "text": _clip(text, MAX_MESSAGE_CHARS),
        "requirement_id": None if requirement_id is None else str(requirement_id)[:MAX_TARGET_CHARS],
        "version": None if version is None else int(version),
    }
    return payload


def render_limit_extraction_messages(
    *,
    text: object,
    requirement_id: str | None = None,
    version: int | None = None,
) -> list[ChatMessage]:
    payload_text = json.dumps(
        build_limit_extraction_payload(text=text, requirement_id=requirement_id, version=version),
        ensure_ascii=False,
        indent=2,
        default=str,
    )
    instruction = Template(LIMIT_EXTRACTION_INSTRUCTION).substitute(payload=payload_text)
    return [
        ChatMessage(role="system", content=LIMIT_EXTRACTION_SYSTEM),
        ChatMessage(role="user", content=instruction),
    ]


def parse_extracted_limits(
    raw_text: object, *, allowed_objects: Sequence[Any] | None = ()
) -> tuple[dict, ...]:
    """解析并规范化模型返回的历史限制（结构/操作名/对象来源核对；不做关键词判断）。

    返回空元组表示模型语义判断"用户原话没有明确限制"——这是合法结果；
    任何结构非法/对象不可核对都抛 RequirementResolutionUnavailable，由调用方标 unresolved。
    """
    payload = _extract_json_object(raw_text)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RequirementResolutionUnavailable("历史限制提取输出不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise RequirementResolutionUnavailable("历史限制提取输出顶层必须是 JSON 对象")
    limits = _parse_limits(data, key="limits")
    typed_allowed, bare_allowed = _allowed_object_keys(allowed_objects)
    return _clean_limit_entries(
        limits, label="limits", typed_allowed=typed_allowed, bare_allowed=bare_allowed
    )


# ---------------------------------------------------------------------------
# 模型解析器
# ---------------------------------------------------------------------------


def _completion_text(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    message = getattr(result, "message", None)
    content = getattr(message, "content", None)
    if content is None and hasattr(result, "content"):
        content = getattr(result, "content")
    return "" if content is None else str(content)


def _sanitize_error(exc: BaseException) -> str:
    try:
        from agent.trace.redact import sanitize_error_text

        return sanitize_error_text(str(exc), limit=300)
    except Exception:  # noqa: BLE001
        return "[redacted]"


class ModelRequirementResolver:
    """用用户已配置的主模型解析"新消息与已有要求的关系"。

    构造参数：
    * adapter_factory：无参可等待可调用，返回 BaseAdapter | None（生产传 ctx.build_adapter）；
    * model_call：可选，直接替换模型调用（接收 ChatMessage 列表，返回文本）；
    * timeout_seconds：整体超时，默认 20 秒；
    * context_provider：可选，返回有限相关上下文（只作为数据展示）；
    * allowed_objects_provider：可选，返回后端可解析/已核实的对象列表，
      用于校验 limits.objects 不引用未知对象。

    安全约束：单次 complete、tools=[]；不新建 Agent、不暴露工具/文件/设置；不写库；
    任何异常/非法输出 → RequirementResolutionUnavailable（不关键词兜底）。
    """

    def __init__(
        self,
        *,
        adapter_factory: Callable[[], Any] | None,
        model_call: Callable[[list[ChatMessage]], Any] | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        context_provider: Callable[[], Sequence[dict]] | None = None,
        allowed_objects_provider: Callable[[], Sequence[Any]] | None = None,
    ) -> None:
        self.adapter_factory = adapter_factory
        self.model_call = model_call
        try:
            self.timeout_seconds = max(0.0, float(timeout_seconds))
        except (TypeError, ValueError):
            self.timeout_seconds = DEFAULT_TIMEOUT_SECONDS
        self.context_provider = context_provider
        self.allowed_objects_provider = allowed_objects_provider
        # 审计来源：app/requirements 写入 prompt_version / source 时使用（不猜）。
        self.prompt_version = REQUIREMENT_RESOLUTION_SCHEMA_VERSION
        self.limit_extraction_prompt_version = LIMIT_EXTRACTION_SCHEMA_VERSION

    async def resolve(
        self,
        *,
        message: str,
        message_id: str,
        turn_id: str,
        candidates: list[dict],
    ) -> ResolutionOutcome:
        started = time.monotonic()
        context = self._context()
        allowed_objects = self._allowed_objects()
        try:
            messages = render_resolution_messages(
                message=message,
                message_id=message_id,
                turn_id=turn_id,
                candidates=candidates,
                context=context,
            )
            raw_text, model = await asyncio.wait_for(
                self._call_model(messages), timeout=self.timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            raise RequirementResolutionUnavailable(
                "要求解析模型超时（超过 " + str(self.timeout_seconds) + " 秒），按新要求继续"
            ) from exc
        except RequirementResolutionUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - 模型异常不能变成解析成功
            raise RequirementResolutionUnavailable(
                "要求解析模型调用失败：" + _sanitize_error(exc)
            ) from exc
        try:
            outcome = parse_resolution_outcome(raw_text, model=model)
            validated = validate_resolution(
                outcome, candidates=candidates, allowed_objects=allowed_objects
            )
        except RequirementResolutionUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - 校验器异常同样视为不可用
            raise RequirementResolutionUnavailable(
                "要求解析结果校验失败：" + _sanitize_error(exc)
            ) from exc
        logger.debug(
            "要求解析完成 relation=%s target=%s latency_ms=%d",
            validated.relation.value,
            validated.target_requirement_id,
            int((time.monotonic() - started) * 1000),
        )
        return validated

    async def extract_limits(
        self,
        text: object,
        *,
        requirement_id: str | None = None,
        version: int | None = None,
    ) -> tuple[dict, ...]:
        """R9-H §6：从历史用户原话重新理解结构化限制（单次主模型调用，tools 为空）。

        * 只喂用户原话与来源标识；**不解析** R9-G 之前的展平文字、不做关键词判断；
        * 输出结构必须与 ResolutionOutcome.limits 条目一致；结构非法/超时/异常 →
          RequirementResolutionUnavailable（调用方标 unresolved，绝不把 legacy 当无限制）；
        * 返回空元组 = 模型语义判断"原话没有明确限制"。
        """
        try:
            messages = render_limit_extraction_messages(
                text=text, requirement_id=requirement_id, version=version
            )
            raw_text, _model = await asyncio.wait_for(
                self._call_model(messages), timeout=self.timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            raise RequirementResolutionUnavailable(
                "历史限制提取模型超时（超过 " + str(self.timeout_seconds) + " 秒），标记 unresolved"
            ) from exc
        except RequirementResolutionUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - 模型异常不能变成"没有限制"
            raise RequirementResolutionUnavailable(
                "历史限制提取模型调用失败：" + _sanitize_error(exc)
            ) from exc
        try:
            # 历史原话里的对象是旧要求的事实，当前后端对象清单不一定覆盖；
            # 提取结果只用于"限制"（只收紧、不产生授权），因此只做结构校验。
            return parse_extracted_limits(raw_text, allowed_objects=())
        except RequirementResolutionUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - 校验器异常同样视为不可用
            raise RequirementResolutionUnavailable(
                "历史限制提取结果校验失败：" + _sanitize_error(exc)
            ) from exc

    def _context(self) -> list[dict]:
        provider = self.context_provider
        if provider is None:
            return []
        try:
            values = provider()
        except Exception:  # noqa: BLE001 - 上下文失败不能变成解析失败
            logger.warning("要求解析上下文读取失败（按空上下文继续）", exc_info=True)
            return []
        return [dict(item) for item in list(values or ())[:MAX_CONTEXT_ITEMS] if isinstance(item, dict)]

    def _allowed_objects(self) -> list[Any]:
        provider = self.allowed_objects_provider
        if provider is None:
            return []
        try:
            values = provider()
        except Exception:  # noqa: BLE001 - 对象清单失败时退回结构校验（限制只收紧不授权）
            logger.warning("要求解析对象清单读取失败（只做结构校验）", exc_info=True)
            return []
        return [item for item in list(values or ())[: MAX_LIMITS * MAX_LIMIT_OBJECTS]]

    async def _call_model(self, messages: list[ChatMessage]) -> tuple[str, str | None]:
        if self.model_call is not None:
            result = self.model_call(messages)
            if inspect.isawaitable(result):
                result = await result
            return _completion_text(result), None
        factory = self.adapter_factory
        if factory is None:
            raise RequirementResolutionUnavailable(
                "没有可用的主模型配置，无法解析用户要求（按新要求继续）"
            )
        adapter = factory()
        if inspect.isawaitable(adapter):
            adapter = await adapter
        if adapter is None:
            raise RequirementResolutionUnavailable(
                "没有可用的主模型配置，无法解析用户要求（按新要求继续）"
            )
        model_name = getattr(adapter, "model", None)
        completion = await adapter.complete(messages, tools=[])
        return _completion_text(completion), (None if model_name is None else str(model_name))


__all__ = [
    "ALLOWED_LIMIT_OPERATIONS",
    "DEFAULT_TIMEOUT_SECONDS",
    "LIMIT_OBJECT_KINDS",
    "ModelRequirementResolver",
    "RELATION_VALUES",
    "RequirementRelation",
    "RequirementResolutionUnavailable",
    "RequirementResolver",
    "ResolutionOutcome",
    "build_limit_extraction_payload",
    "build_resolution_payload",
    "normalize_candidate",
    "parse_extracted_limits",
    "parse_resolution_outcome",
    "render_limit_extraction_messages",
    "render_resolution_messages",
    "validate_resolution",
];
