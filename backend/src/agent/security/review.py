# -*- coding: utf-8 -*-
"""受限模式自然语言授权的模型审查（R9-F，契约 docs/r9f-contracts.md §3.2、§4.3）。

职责边界（本模块只做这些，不接 engine/gateway）：
1. 由**后端**组装审查输入：用户原话+来源、修正/限制、具体操作、后端核实过的对象
   （路径/身份/来源/接收服务）、既有授权与批准/拒绝记录；文件/网页/工具结果内容
   只作为**不可信材料**单独标注（untrusted_material），不混入用户要求。
2. 用用户已配置的**主模型**做一次普通 complete（tools 为空，不新建 Agent，不暴露
   工具/文件/设置能力），超时/异常/非法 JSON 一律抛 ReviewUnavailable（绝不默认允许）。
3. 后端 validate_outcome 校验：结构合法；requirement_id 真实且要求仍 active；
   objects/operations 是请求子集；ALLOW 必须覆盖全部目标并带 limits/purpose；
   不得把一次操作扩成长期/更大范围；reason 非空。另有只收紧、不放宽的确定性兜底
   （否定/条件/引用材料里的话不得成为 ALLOW 依据）。
4. 提供 operation_fingerprint（含对象身份与模式）作为复用缓存键；提供
   ReviewAuditLedger 落 authorization_review_audit（模型/提示词版本/耗时/错误）。

提示词单一来源：agent/prompts.py 的 AUTHORIZATION_REVIEW_* 常量；本模块不写第二套。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from string import Template
from typing import TYPE_CHECKING, Any, Callable, Protocol, Sequence

from agent.adapters.base import ChatMessage
from agent.prompts import (
    AUTHORIZATION_REVIEW_INSTRUCTION,
    AUTHORIZATION_REVIEW_SCHEMA_VERSION,
    AUTHORIZATION_REVIEW_SYSTEM,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注，避免模块循环导入
    from agent.security.requirements import UserRequirement

logger = logging.getLogger(__name__)

#: 触发模型审查的模式（read_only / unrestricted 不走模型审查，见契约 §2）。
REVIEW_MODE = "restricted"

#: 允许进入审查的操作类型（ReviewOperation.kind）。
REVIEWABLE_OPERATION_KINDS = frozenset(
    {"write", "create", "delete", "execute", "shell", "install", "egress", "system_change"}
)

#: 允许的审查对象类型（ReviewObject.kind）。
OBJECT_KINDS = frozenset({"path", "directory", "program", "service", "material"})

#: 写/删类操作必须带后端核实过的具体对象，不能空对象 ALLOW。
OBJECT_REQUIRED_OPERATION_KINDS = frozenset({"write", "create", "delete"})

MAX_REASON_CHARS = 500
MAX_TEXT_CHARS = 6000
MAX_CORRECTION_CHARS = 1000
MAX_LIMIT_CHARS = 300
MAX_PURPOSE_CHARS = 500
MAX_OBJECTS = 20
MAX_LIMITS = 20
MAX_CORRECTIONS = 8
MAX_CONTEXT_ITEMS = 12
MAX_CONTEXT_JSON_CHARS = 2000
MAX_UNTRUSTED_ITEMS = 8
MAX_UNTRUSTED_CHARS = 4000
MAX_SUMMARY_CHARS = 1000

#: 审查超时默认值（秒）。慢模型应按调用方配置调整，但超时一律不可用而非允许。
DEFAULT_TIMEOUT_SECONDS = 20.0


class ReviewUnavailable(Exception):
    """审查不可用：模型缺失/超时/异常/输出结构非法/校验不通过。

    调用方（Gateway/engine）必须把 ReviewUnavailable 映射为 APPROVE 或 DENY，
    **不得**映射为 ALLOW（契约 §2.3 c：不默认允许，不自动切模式）。
    """


# ---------------------------------------------------------------------------
# 公共数据模型（契约 §3.2；ReviewRequest 只增加契约 §3.2 文本要求单独标注的
# untrusted_material 可选字段，其余与契约一致）
# ---------------------------------------------------------------------------


class ReviewDecision(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass(frozen=True)
class ReviewObject:
    kind: str  # path | directory | program | service | material
    value: str
    identity_key: str | None = None
    file_origin: str | None = None


@dataclass(frozen=True)
class ReviewOperation:
    kind: str  # write | create | delete | execute | shell | install | egress | system_change
    objects: tuple[ReviewObject, ...]
    program: str | None = None
    argv: tuple[str, ...] = ()
    cwd: str | None = None
    script_sha256: str | None = None
    network_hosts: tuple[str, ...] = ()
    summary: str = ""


@dataclass(frozen=True)
class ReviewRequest:
    requirement: "UserRequirement"
    corrections: tuple[str, ...]
    limits: tuple[str, ...]
    mode: str
    operation: ReviewOperation
    related_grants: tuple[dict, ...]
    prior_decisions: tuple[dict, ...]
    approvals: tuple[dict, ...]
    user_constraints: tuple[str, ...]
    #: 契约 §3.2 模型调用规范要求单独标注的不可信材料（文件/网页/工具结果内容）。
    #: 只作为数据展示，永远不能增加授权。
    untrusted_material: tuple[dict, ...] = ()


@dataclass(frozen=True)
class ReviewOutcome:
    decision: ReviewDecision
    reason: str
    requirement_id: str
    allowed_operations: tuple[str, ...]
    objects: tuple[ReviewObject, ...]
    limits: tuple[str, ...]
    purpose: str


class AuthorizationReviewer(Protocol):
    async def review(self, request: ReviewRequest) -> ReviewOutcome: ...


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
    """把任意后端记录转成有界、可 JSON 序列化的只读结构。"""
    if depth > 4:
        return "[层级过深]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _clip(value, max_chars)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:32]:
            result[str(key)[:80]] = _safe_json_value(item, max_chars=max_chars, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            _safe_json_value(item, max_chars=max_chars, depth=depth + 1)
            for item in list(value)[:32]
        ]
    return _clip(value, max_chars)


def _bounded_str_list(values: object, *, max_items: int, max_chars: int) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    return [_clip(item, max_chars) for item in list(values)[:max_items] if str(item or "").strip()]


def _object_payload(item: Any) -> dict:
    return {
        "kind": str(getattr(item, "kind", "") or ""),
        "value": _clip(getattr(item, "value", ""), 1000),
        "identity_key": (
            None if getattr(item, "identity_key", None) is None else str(item.identity_key)
        ),
        "file_origin": (
            None if getattr(item, "file_origin", None) is None else str(item.file_origin)
        ),
    }


def _untrusted_payload(item: Any) -> dict:
    if not isinstance(item, dict):
        return {"source": "unknown", "label": "", "content": _clip(item, MAX_UNTRUSTED_CHARS)}
    source = item.get("source") or item.get("kind") or "unknown"
    label = item.get("label") or item.get("name") or item.get("path") or ""
    content = item.get("content")
    if content is None:
        content = item.get("text") or item.get("excerpt") or ""
    return {
        "source": _clip(source, 100),
        "label": _clip(label, 300),
        "content": _clip(content, MAX_UNTRUSTED_CHARS),
    }


def build_review_payload(request: ReviewRequest) -> dict:
    """组装审查输入（唯一入口）。所有字段由后端提供，模型无法选择性提交。"""
    requirement = getattr(request, "requirement", None)
    requirement_payload: dict | None
    if requirement is None:
        requirement_payload = None
    else:
        provenance = getattr(requirement, "provenance", None) or {}
        requirement_payload = {
            "requirement_id": str(getattr(requirement, "requirement_id", "") or ""),
            "text": _clip(getattr(requirement, "text", ""), MAX_TEXT_CHARS),
            "provenance": _safe_json_value(provenance, max_chars=500),
            "parent_id": (
                None if getattr(requirement, "parent_id", None) is None else str(requirement.parent_id)
            ),
            "status": str(getattr(requirement, "status", "") or ""),
            "limits": _bounded_str_list(
                getattr(requirement, "limits", ()) or (), max_items=MAX_LIMITS, max_chars=MAX_LIMIT_CHARS
            ),
        }
    operation = request.operation
    operation_payload = {
        "kind": str(operation.kind),
        "objects": [_object_payload(item) for item in list(operation.objects)[:MAX_OBJECTS]],
        "program": None if operation.program is None else _clip(operation.program, 1000),
        "argv": [_clip(item, 500) for item in list(operation.argv)[:32]],
        "cwd": None if operation.cwd is None else _clip(operation.cwd, 1000),
        "script_sha256": (
            None if operation.script_sha256 is None else _clip(operation.script_sha256, 128)
        ),
        "network_hosts": [
            _clip(item, 300) for item in list(operation.network_hosts)[:16] if str(item or "").strip()
        ],
        "summary": _clip(operation.summary, MAX_SUMMARY_CHARS),
    }
    return {
        "prompt_version": AUTHORIZATION_REVIEW_SCHEMA_VERSION,
        "mode": str(request.mode),
        "requirement": requirement_payload,
        "corrections": _bounded_str_list(
            request.corrections, max_items=MAX_CORRECTIONS, max_chars=MAX_CORRECTION_CHARS
        ),
        "user_limits": _bounded_str_list(
            request.limits, max_items=MAX_LIMITS, max_chars=MAX_LIMIT_CHARS
        ),
        "user_constraints": _bounded_str_list(
            request.user_constraints, max_items=MAX_LIMITS, max_chars=MAX_LIMIT_CHARS
        ),
        "operation": operation_payload,
        "related_grants": [
            _safe_json_value(item, max_chars=MAX_CONTEXT_JSON_CHARS)
            for item in list(request.related_grants)[:MAX_CONTEXT_ITEMS]
        ],
        "prior_decisions": [
            _safe_json_value(item, max_chars=MAX_CONTEXT_JSON_CHARS)
            for item in list(request.prior_decisions)[:MAX_CONTEXT_ITEMS]
        ],
        "approvals": [
            _safe_json_value(item, max_chars=MAX_CONTEXT_JSON_CHARS)
            for item in list(request.approvals)[:MAX_CONTEXT_ITEMS]
        ],
        "untrusted_material": [
            _untrusted_payload(item) for item in list(request.untrusted_material)[:MAX_UNTRUSTED_ITEMS]
        ],
    }


def review_payload_json(request: ReviewRequest) -> str:
    """审查输入的稳定 JSON 文本（审计摘要也用它计算，确保走同一份输入）。"""
    return json.dumps(build_review_payload(request), ensure_ascii=False, sort_keys=True, default=str)


def review_request_digest(request: ReviewRequest) -> str:
    """审查请求摘要（sha256）；审计只落摘要与结果，不落模型推理全文与材料原文。"""
    return hashlib.sha256(review_payload_json(request).encode("utf-8")).hexdigest()


def render_review_messages(request: ReviewRequest) -> list[ChatMessage]:
    """渲染 system + user 两条消息；提示词来自 agent/prompts.py 单一来源。"""
    payload_text = json.dumps(build_review_payload(request), ensure_ascii=False, indent=2, default=str)
    instruction = Template(AUTHORIZATION_REVIEW_INSTRUCTION).substitute(payload=payload_text)
    return [
        ChatMessage(role="system", content=AUTHORIZATION_REVIEW_SYSTEM),
        ChatMessage(role="user", content=instruction),
    ]


def operation_fingerprint(operation: ReviewOperation, *, mode: str) -> str:
    """操作指纹 = 复用缓存键的实质内容（契约：对象身份 + 模式都要参与）。

    包含：操作类型、对象（kind/规范化值/身份键/来源）、程序、argv、cwd、脚本摘要、
    网络接收方、模式。任何一项变化都会得到不同指纹 → 不能复用旧授权记录。
    """
    canonical = {
        "mode": str(mode),
        "kind": str(operation.kind),
        "objects": sorted(
            [
                {
                    "kind": str(item.kind),
                    "value": _normalize_object_value(str(item.kind), str(item.value)),
                    "identity_key": None if item.identity_key is None else str(item.identity_key),
                    "file_origin": None if item.file_origin is None else str(item.file_origin),
                }
                for item in operation.objects
            ],
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
        ),
        "program": None if operation.program is None else os.path.normcase(os.path.normpath(str(operation.program))),
        "argv": [str(item) for item in operation.argv],
        "cwd": None if operation.cwd is None else os.path.normcase(os.path.normpath(str(operation.cwd))),
        "script_sha256": None if operation.script_sha256 is None else str(operation.script_sha256),
        "network_hosts": sorted({str(item).strip().lower() for item in operation.network_hosts if str(item).strip()}),
    }
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 确定性兜底（只收紧、不放宽；ALLOW 必须同时通过模型判定与这里的检查）
# ---------------------------------------------------------------------------

_ACTION_VERBS: dict[str, tuple[str, ...]] = {
    "write": (
        "修改", "编辑", "改动", "改", "写入", "写", "保存", "覆盖", "重写", "替换", "更新",
        "整理", "修复", "补全", "添加", "追加", "合并", "汇总", "格式化",
        "modify", "edit", "write", "save", "overwrite", "update", "fix", "rewrite", "replace",
    ),
    "create": (
        "创建", "新建", "生成", "建立", "制作", "输出",
        "create", "generate", "make", "new",
    ),
    "delete": (
        "删除", "删掉", "删", "移除", "清理", "清空", "移到回收站",
        "delete", "remove", "erase", "clear", "trash",
    ),
    "execute": (
        "执行", "运行", "跑", "测试", "构建", "编译", "检查", "诊断", "验证",
        "run", "execute", "launch", "test", "build", "compile", "check", "diagnose",
    ),
    "shell": ("命令行", "终端", "命令", "shell", "powershell", "cmd", "terminal"),
    "install": ("安装", "装依赖", "装", "依赖", "install", "pip", "npm", "uv"),
    "egress": (
        "发送", "发给", "外发", "上传", "发布", "分享", "传给", "同步到",
        "send", "upload", "publish", "share", "post", "sync",
    ),
    "system": (
        "设置", "配置", "系统", "注册表", "环境变量", "权限",
        "setting", "settings", "configure", "config", "registry", "system", "environment",
    ),
}

_FAMILIES_FOR_OPERATION: dict[str, tuple[str, ...]] = {
    "write": ("write",),
    "create": ("write", "create"),
    "delete": ("delete",),
    "execute": ("execute",),
    "shell": ("execute", "shell"),
    "install": ("install", "system"),
    "egress": ("egress",),
    "system_change": ("system",),
}

_REFUSAL_PREFIXES = ("不要", "不用", "不必", "无需", "禁止", "不准", "不得", "不能", "不可", "请勿", "不允许", "别")

_CONDITIONAL_RE = re.compile(
    r"(?:如果|假如|假设|要是|万一|若是|倘若|除非|是否|能不能|可不可以|行不行|行吗|好吗|要不要|需不需要)"
)
_CONDITIONAL_EN_RE = re.compile(
    r"\b(?:if|whether|unless|maybe|perhaps|can you|could you|would you|should i)\b", re.IGNORECASE
)
_QUESTION_TAIL_RE = re.compile(r"(?:吗|呢)\s*[。.!！~～]*$")

# 引号/代码块识别用程序拼接，避免在正则源码里出现字面反引号。
_BACKTICK = chr(96)
_QUOTED_RE = re.compile(
    "|".join(
        [
            _BACKTICK * 3 + r".*?" + _BACKTICK * 3,
            _BACKTICK + r"[^" + _BACKTICK + r"]*" + _BACKTICK,
            r'"[^"\\]*(?:\\.[^"\\]*)*"',
            r"'[^'\\]*(?:\\.[^'\\]*)*'",
            r"“[^”]*”",
            r"‘[^’]*’",
            r"「[^」]*」",
            r"『[^』]*』",
        ]
    ),
    re.DOTALL,
)
_QUOTE_MARKS = "\"'“”‘’「」『』" + _BACKTICK
_GENERIC_REFUSAL_RE = re.compile(
    r"(?:不要|别|请勿|不准|禁止|不得)\s*(?:动|碰|操作|开始|执行|进行|下手|做)"
)
_FENCE_RE = re.compile(
    _BACKTICK * 3 + r"(?:json)?\s*(.*?)" + _BACKTICK * 3, re.IGNORECASE | re.DOTALL
)


def _family_verbs(operation_kind: str) -> tuple[str, ...]:
    verbs: list[str] = []
    for family in _FAMILIES_FOR_OPERATION.get(str(operation_kind), ()):
        verbs.extend(_ACTION_VERBS.get(family, ()))
    return tuple(verbs)


def _verb_pattern(verb: str) -> str:
    if verb.isascii():
        return r"\b" + re.escape(verb) + r"\b"
    return re.escape(verb)


def _text_mentions_operation(text: str, operation: ReviewOperation) -> bool:
    for verb in _family_verbs(operation.kind):
        if re.search(_verb_pattern(verb), text, re.IGNORECASE):
            return True
    return False


#: 「不要改其他内容 / 不要删除其他文件」是对**范围外**对象的限制，不是否定本次操作；
#: 否定词后紧跟这些词时不算阻断本次 ALLOW（否则会把合法的"别动其他部分"误判为拒绝）。
_REFUSAL_EXEMPT_TAILS = ("其他", "其它", "别的", "其余", "之外", "无关", "别处")


def _refusal_is_scope_limit(match: "re.Match[str]") -> bool:
    tail = match.string[match.end() : match.end() + 8].lstrip(" 　：:，,。；;、")
    return tail.startswith(_REFUSAL_EXEMPT_TAILS)


def _has_explicit_refusal(text: str, operation: ReviewOperation) -> bool:
    """用户原话里是否明确否定本次（或同类）操作。

    「不要改 X 的其他内容」「别动其他文件」属于范围限制，不阻断对 X 本身的授权；
    真正否定操作本身的句式（如"不要修改 X"）才返回 True。
    """
    verbs = _family_verbs(operation.kind)
    if verbs:
        alt = "|".join(_verb_pattern(verb) for verb in sorted(verbs, key=len, reverse=True))
        prefix = "|".join(_REFUSAL_PREFIXES)
        pattern = re.compile(
            r"(?:" + prefix + r")\s*(?:再|去|帮我|给我|随便|直接|立刻|马上)?\s*(?:" + alt + r")",
            re.IGNORECASE,
        )
        for match in pattern.finditer(text):
            if not _refusal_is_scope_limit(match):
                return True
    if operation.kind != "write":
        for match in _GENERIC_REFUSAL_RE.finditer(text):
            if not _refusal_is_scope_limit(match):
                return True
    return False


def _has_conditional_or_question(text: str) -> bool:
    if not text.strip():
        return False
    if _CONDITIONAL_RE.search(text) or _CONDITIONAL_EN_RE.search(text):
        return True
    if "？" in text or "?" in text:
        return True
    return bool(_QUESTION_TAIL_RE.search(text.strip()))


def _has_quote_marks(text: str) -> bool:
    return any(mark in text for mark in _QUOTE_MARKS)


def _strip_quoted(text: str) -> str:
    return _QUOTED_RE.sub(" ", text)


def deterministic_allow_guard(request: ReviewRequest) -> str | None:
    """ALLOW 的确定性兜底：返回中文原因表示必须拒绝这次 ALLOW；None 表示不额外阻断。

    这些规则只**收紧**、不放宽：即使模型误判为 allow，以下情况也不会被接受。
    * 用户原话明确否定本次操作（如"不要修改 X"）；
    * 疑问句 / 条件句 / 假设（条件无法由后端核实）；
    * 存在引用材料/不可信材料时，去掉引号内文字后用户原话没有明确要求这次操作
      （材料里的"允许/请执行"不构成授权）。
    """
    requirement = getattr(request, "requirement", None)
    if requirement is None:
        return "没有可依据的用户要求"
    text = str(getattr(requirement, "text", "") or "")
    corrections = " ".join(str(item) for item in (request.corrections or ()) if str(item).strip())
    combined = text + "\n" + corrections if corrections else text
    if not combined.strip():
        return "用户没有留下可依据的原话"
    if _has_explicit_refusal(combined, request.operation):
        return "用户原话包含对这次操作的明确否定（如“不要修改/删除”），不能解释为授权"
    if _has_conditional_or_question(combined):
        return "用户原话是疑问句/条件句/假设，条件无法由后端核实，不构成明确授权"
    if request.untrusted_material or _has_quote_marks(combined):
        bare = _strip_quoted(combined)
        if not _text_mentions_operation(bare, request.operation):
            return "引用/材料中的表述不是用户授权；去掉引用内容后，用户原话没有明确要求这次操作"
    return None


# ---------------------------------------------------------------------------
# 输出解析与校验
# ---------------------------------------------------------------------------


def _invalid(message: str) -> ReviewUnavailable:
    return ReviewUnavailable("授权审查输出无效：" + message)


def _extract_json_object(raw_text: object) -> str:
    text = "" if raw_text is None else str(raw_text).strip()
    if not text:
        raise ReviewUnavailable("授权审查模型没有返回内容")
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ReviewUnavailable("授权审查模型没有返回 JSON 对象")
    return text[start : end + 1]


def _optional_str(data: dict, key: str, *, max_chars: int) -> str:
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _invalid(key + " 必须是字符串")
    return _clip(value, max_chars)


def _string_list(data: dict, key: str, *, max_items: int, max_chars: int) -> tuple[str, ...]:
    value = data.get(key, [])
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _invalid(key + " 必须是字符串数组")
    result: list[str] = []
    for item in value[:max_items]:
        if not isinstance(item, str):
            raise _invalid(key + " 只能包含字符串")
        text = item.strip()
        if text:
            result.append(_clip(text, max_chars))
    return tuple(result)


def _parse_objects(data: dict) -> tuple[ReviewObject, ...]:
    value = data.get("objects", [])
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _invalid("objects 必须是数组")
    objects: list[ReviewObject] = []
    seen: set[tuple[str, str]] = set()
    for item in value[:MAX_OBJECTS]:
        if not isinstance(item, dict):
            raise _invalid("objects 的每一项必须是对象")
        kind = item.get("kind")
        obj_value = item.get("value")
        if not isinstance(kind, str) or not isinstance(obj_value, str) or not obj_value.strip():
            raise _invalid("objects 每一项都必须有非空的 kind/value 字符串")
        kind = kind.strip().lower()
        if kind not in OBJECT_KINDS:
            raise _invalid("objects.kind 非法：" + kind[:40])
        identity_key = item.get("identity_key")
        if identity_key is not None and not isinstance(identity_key, str):
            raise _invalid("objects.identity_key 必须是字符串或 null")
        file_origin = item.get("file_origin")
        if file_origin is not None and not isinstance(file_origin, str):
            raise _invalid("objects.file_origin 必须是字符串或 null")
        obj = ReviewObject(
            kind=kind,
            value=obj_value.strip(),
            identity_key=identity_key.strip() if isinstance(identity_key, str) else None,
            file_origin=file_origin.strip() if isinstance(file_origin, str) else None,
        )
        key = (_normalize_object_value(kind, obj.value), str(obj.identity_key or ""))
        if key in seen:
            continue
        seen.add(key)
        objects.append(obj)
    return tuple(objects)


def parse_review_outcome(raw_text: object) -> ReviewOutcome:
    """把模型原始文本解析为 ReviewOutcome（只做结构解析，安全校验在 validate_outcome）。

    任何结构问题（非 JSON / 字段类型错 / decision 非法 / 对象类型非法）都抛
    ReviewUnavailable —— 不猜测、不修补、不默认允许。
    """
    payload = _extract_json_object(raw_text)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ReviewUnavailable("授权审查输出不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise _invalid("顶层必须是 JSON 对象")
    decision_raw = data.get("decision")
    if not isinstance(decision_raw, str) or not decision_raw.strip():
        raise _invalid("缺少 decision")
    try:
        decision = ReviewDecision(decision_raw.strip().lower())
    except ValueError as exc:
        raise _invalid("decision 只能是 allow/confirm/deny") from exc
    requirement_id = data.get("requirement_id")
    if not isinstance(requirement_id, str) or not requirement_id.strip():
        raise _invalid("缺少 requirement_id")
    reason = _optional_str(data, "reason", max_chars=MAX_REASON_CHARS)
    purpose = _optional_str(data, "purpose", max_chars=MAX_PURPOSE_CHARS)
    allowed_operations = _string_list(data, "allowed_operations", max_items=20, max_chars=100)
    limits = _string_list(data, "limits", max_items=MAX_LIMITS, max_chars=MAX_LIMIT_CHARS)
    objects = _parse_objects(data)
    return ReviewOutcome(
        decision=decision,
        reason=reason,
        requirement_id=requirement_id.strip(),
        allowed_operations=allowed_operations,
        objects=objects,
        limits=limits,
        purpose=purpose,
    )


def _normalize_object_value(kind: str, value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    kind = str(kind or "").strip().lower()
    if kind in ("path", "directory", "program"):
        try:
            return os.path.normcase(os.path.normpath(text))
        except (OSError, ValueError):  # pragma: no cover - 极端输入
            return text.lower()
    if kind == "service":
        return text.lower()
    return text


def _objects_match(outcome_obj: ReviewObject, request_obj: ReviewObject) -> bool:
    """模型给出的对象是否就是请求里后端核实过的那个对象（不允许改写）。"""
    if str(outcome_obj.kind).strip().lower() != str(request_obj.kind).strip().lower():
        return False
    if _normalize_object_value(outcome_obj.kind, outcome_obj.value) != _normalize_object_value(
        request_obj.kind, request_obj.value
    ):
        return False
    # 身份/来源以请求（后端核实）为准：模型可以省略，但不能给出不同值或凭空补造。
    if outcome_obj.identity_key is not None:
        if request_obj.identity_key is None or str(outcome_obj.identity_key) != str(
            request_obj.identity_key
        ):
            return False
    if outcome_obj.file_origin is not None:
        if request_obj.file_origin is None or str(outcome_obj.file_origin) != str(
            request_obj.file_origin
        ):
            return False
    return True


def _canonical_matched_objects(
    outcome_objects: Sequence[ReviewObject], request_objects: Sequence[ReviewObject]
) -> tuple[tuple[ReviewObject, ...], list[ReviewObject]]:
    """把模型对象映射回请求对象；返回 (匹配到的请求对象, 无法匹配的模型对象)。"""
    matched: list[ReviewObject] = []
    used: set[int] = set()
    unmatched: list[ReviewObject] = []
    for outcome_obj in outcome_objects:
        found: int | None = None
        for index, request_obj in enumerate(request_objects):
            if index in used:
                continue
            if _objects_match(outcome_obj, request_obj):
                found = index
                break
        if found is None:
            # 模型偶尔会重复列出同一个对象：只要它确实是请求里的对象，按重复忽略，
            # 不因此把有效输出判成"新增对象"；真正不在请求里的对象仍然拒绝。
            if any(_objects_match(outcome_obj, request_obj) for request_obj in request_objects):
                continue
            unmatched.append(outcome_obj)
            continue
        used.add(found)
        matched.append(request_objects[found])
    return tuple(matched), unmatched


def _validate_request_shape(request: ReviewRequest) -> None:
    if not hasattr(request, "requirement") or not hasattr(request, "operation"):
        raise ReviewUnavailable("授权审查请求结构不完整")
    requirement = request.requirement
    if not getattr(requirement, "requirement_id", ""):
        raise ReviewUnavailable("授权审查请求缺少真实用户要求，不能进行审查")
    if not isinstance(request.operation, ReviewOperation):
        raise ReviewUnavailable("授权审查请求的操作结构非法")
    if str(request.mode or "") != REVIEW_MODE:
        raise ReviewUnavailable("只有受限模式才允许模型授权审查（当前模式：" + str(request.mode) + "）")
    for item in request.operation.objects:
        if not isinstance(item, ReviewObject):
            raise ReviewUnavailable("授权审查请求的对象结构非法")
    for field_name in ("corrections", "limits", "user_constraints"):
        values = getattr(request, field_name, ())
        # 契约字段类型是 tuple；实际接线从 DB/服务层组装时允许 list（只读取，不改原对象）。
        if not isinstance(values, (tuple, list)) or any(not isinstance(item, str) for item in values):
            raise ReviewUnavailable("授权审查请求的 " + field_name + " 必须是字符串序列")


def validate_outcome(outcome: ReviewOutcome, request: ReviewRequest) -> ReviewOutcome:
    """后端校验模型审查结果（契约 §3.2）。

    校验项：
    * 结构合法（ReviewOutcome / decision / 字段类型）；
    * requirement_id 真实存在（当前要求或其父要求），且要求仍为 active；
    * 只在受限模式、合法操作类型上生效；
    * objects / allowed_operations 是请求集合的子集，对象身份与来源以请求为准；
    * ALLOW 覆盖全部目标对象、allowed_operations 只含本次操作类型、必须有 limits/purpose；
    * 否定/条件/引用材料等确定性兜底；
    * reason 非空。
    任何不通过都抛 ReviewUnavailable（调用方不得把它当 ALLOW）。
    """
    _validate_request_shape(request)
    if not isinstance(outcome, ReviewOutcome):
        raise ReviewUnavailable("授权审查结果结构非法（不是 ReviewOutcome）")
    if not isinstance(outcome.decision, ReviewDecision):
        raise ReviewUnavailable("授权审查结果的 decision 非法")
    requirement = request.requirement
    valid_ids = {str(requirement.requirement_id)}
    parent_id = getattr(requirement, "parent_id", None)
    if parent_id:
        valid_ids.add(str(parent_id))
    if str(outcome.requirement_id or "") not in valid_ids:
        raise ReviewUnavailable("授权审查结果引用了不存在的用户要求，不能作为授权依据")
    if str(getattr(requirement, "status", "")) != "active":
        raise ReviewUnavailable("用户要求已失效（status=" + str(getattr(requirement, "status", "")) + "），审查结果不能使用")
    operation = request.operation
    if str(operation.kind) not in REVIEWABLE_OPERATION_KINDS:
        raise ReviewUnavailable("不支持的审查操作类型：" + str(operation.kind))
    if not str(outcome.reason or "").strip():
        raise ReviewUnavailable("授权审查结果缺少 reason（依据）")
    if outcome.decision == ReviewDecision.ALLOW:
        return _validate_allow(outcome, request)
    return _validate_soft(outcome, request)


def _validate_allow(outcome: ReviewOutcome, request: ReviewRequest) -> ReviewOutcome:
    requirement = request.requirement
    operation = request.operation
    guard = deterministic_allow_guard(request)
    if guard:
        raise ReviewUnavailable("授权审查结果被后端确定性规则否决：" + guard)

    request_objects = list(operation.objects)
    outcome_objects = list(outcome.objects)
    if request_objects:
        matched, unmatched = _canonical_matched_objects(outcome_objects, request_objects)
        if unmatched:
            raise ReviewUnavailable(
                "ALLOW 新增或改写了后端未核实的对象（只能原样引用请求对象）"
            )
        if len(matched) != len(request_objects):
            raise ReviewUnavailable("ALLOW 没有覆盖全部目标对象（不能只授权其中一部分）")
        canonical_objects = tuple(request_objects)
    else:
        if str(operation.kind) in OBJECT_REQUIRED_OPERATION_KINDS:
            raise ReviewUnavailable("ALLOW 缺少后端核实过的具体对象")
        canonical_objects = ()

    allowed_operations = [str(item).strip().lower() for item in outcome.allowed_operations if str(item).strip()]
    if not allowed_operations:
        raise ReviewUnavailable("ALLOW 必须列出 allowed_operations")
    if set(allowed_operations) - {str(operation.kind)}:
        raise ReviewUnavailable("ALLOW 把一次操作扩大成了其它操作类型")
    limits = tuple(str(item).strip() for item in outcome.limits if str(item).strip())
    if not limits:
        raise ReviewUnavailable("ALLOW 必须给出 limits（这次允许的边界）")
    purpose = str(outcome.purpose or "").strip()
    if not purpose:
        raise ReviewUnavailable("ALLOW 必须给出 purpose（这次操作的目的）")
    return ReviewOutcome(
        decision=ReviewDecision.ALLOW,
        reason=str(outcome.reason).strip(),
        requirement_id=str(requirement.requirement_id),
        allowed_operations=tuple(sorted(set(allowed_operations))),
        objects=canonical_objects,
        limits=limits,
        purpose=purpose,
    )


def _validate_soft(outcome: ReviewOutcome, request: ReviewRequest) -> ReviewOutcome:
    requirement = request.requirement
    operation = request.operation
    request_objects = list(operation.objects)
    matched, unmatched = _canonical_matched_objects(list(outcome.objects), request_objects)
    if unmatched:
        raise ReviewUnavailable("审查结果包含了请求之外的对象（越过了请求集合）")
    allowed_operations = [str(item).strip().lower() for item in outcome.allowed_operations if str(item).strip()]
    if set(allowed_operations) - {str(operation.kind)}:
        raise ReviewUnavailable("审查结果把操作类型扩大到了请求之外")
    limits = tuple(str(item).strip() for item in outcome.limits if str(item).strip())
    return ReviewOutcome(
        decision=outcome.decision,
        reason=str(outcome.reason).strip(),
        requirement_id=str(requirement.requirement_id),
        allowed_operations=tuple(sorted(set(allowed_operations))),
        objects=matched,
        limits=limits,
        purpose=str(outcome.purpose or "").strip(),
    )


# ---------------------------------------------------------------------------
# 审查审计（authorization_review_audit，迁移 55）
# ---------------------------------------------------------------------------


def _sanitize_error(exc: BaseException) -> str:
    try:
        from agent.trace.redact import sanitize_error_text

        return sanitize_error_text(str(exc), limit=300)
    except Exception:  # noqa: BLE001 - 脱敏失败时宁可少写
        return "[redacted]"


class ReviewAuditLedger:
    """审查审计持久化（只落摘要/结果/模型与提示词版本/耗时/错误，不落推理全文）。"""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def record(
        self,
        *,
        request_digest: str,
        outcome: str,
        requirement_id: str | None = None,
        model: str | None = None,
        prompt_version: str | None = AUTHORIZATION_REVIEW_SCHEMA_VERSION,
        decision: str | None = None,
        reason: str | None = None,
        latency_ms: int | None = None,
        error: str | None = None,
    ) -> str:
        audit_id = "revaudit_" + uuid.uuid4().hex[:16]
        self.conn.execute(
            "INSERT INTO authorization_review_audit"
            " (id, requirement_id, request_digest, model, prompt_version, outcome,"
            "  decision, reason, latency_ms, error, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                audit_id,
                None if requirement_id is None else str(requirement_id),
                str(request_digest or ""),
                None if model is None else str(model)[:200],
                None if prompt_version is None else str(prompt_version)[:100],
                str(outcome or "unavailable"),
                None if decision is None else str(decision),
                None if reason is None else _clip(reason, MAX_REASON_CHARS),
                None if latency_ms is None else int(latency_ms),
                None if error is None else _clip(error, 300),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return audit_id

    def recent(self, *, requirement_id: str | None = None, limit: int = 50) -> list[dict]:
        count = max(1, min(int(limit), 500))
        if requirement_id:
            rows = self.conn.execute(
                "SELECT * FROM authorization_review_audit WHERE requirement_id = ?"
                " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (str(requirement_id), count),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM authorization_review_audit ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (count,),
            ).fetchall()
        return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# 模型审查器
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


class ModelAuthorizationReviewer:
    """用用户已配置的主模型做一次受限模式授权审查。

    构造参数（契约 §3.2）：
    * adapter_factory：无参可等待可调用，返回 BaseAdapter | None；生产接线传
      ctx.build_adapter（复用主模型/凭据/预算/用量归因），测试可注入 FakeAdapter；
    * model_call：可选，直接替换模型调用（接收 ChatMessage 列表，返回文本）；
    * timeout_seconds：整体超时，默认 20 秒；
    * audit：可选 ReviewAuditLedger，用于把结果写入 authorization_review_audit。

    安全约束（冻结）：
    * 每次审查单独一次 adapter.complete(messages, tools=[])；不新建 Agent，
      不暴露工具/文件/设置能力；不写库、不改模式；
    * 超时/异常/无适配器/非法 JSON/校验不过 → ReviewUnavailable，绝不默认允许；
    * 提示词只来自 agent/prompts.py。
    """

    def __init__(
        self,
        *,
        adapter_factory: Callable[[], Any] | None,
        model_call: Callable[[list[ChatMessage]], Any] | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        audit: ReviewAuditLedger | None = None,
    ) -> None:
        self.adapter_factory = adapter_factory
        self.model_call = model_call
        try:
            self.timeout_seconds = max(0.0, float(timeout_seconds))
        except (TypeError, ValueError):
            self.timeout_seconds = DEFAULT_TIMEOUT_SECONDS
        self.audit = audit

    async def review(self, request: ReviewRequest) -> ReviewOutcome:
        started = time.monotonic()
        digest = review_request_digest(request)
        requirement_id = str(getattr(getattr(request, "requirement", None), "requirement_id", "") or "")
        model: str | None = None
        try:
            messages = render_review_messages(request)
            raw_text, model = await asyncio.wait_for(
                self._call_model(messages), timeout=self.timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            error = "授权审查模型超时（超过 " + str(self.timeout_seconds) + " 秒），本次不默认允许"
            self._write_audit(
                digest=digest, requirement_id=requirement_id, outcome="unavailable",
                model=model, latency_ms=_elapsed_ms(started), error=error,
            )
            raise ReviewUnavailable(error) from exc
        except ReviewUnavailable as exc:
            self._write_audit(
                digest=digest, requirement_id=requirement_id, outcome="unavailable",
                model=model, latency_ms=_elapsed_ms(started), error=_sanitize_error(exc),
            )
            raise
        except Exception as exc:  # noqa: BLE001 - 任何模型异常都不能变成允许
            error = "授权审查模型调用失败：" + _sanitize_error(exc)
            self._write_audit(
                digest=digest, requirement_id=requirement_id, outcome="unavailable",
                model=model, latency_ms=_elapsed_ms(started), error=error,
            )
            raise ReviewUnavailable(error) from exc
        try:
            outcome = parse_review_outcome(raw_text)
            validated = validate_outcome(outcome, request)
        except ReviewUnavailable as exc:
            self._write_audit(
                digest=digest, requirement_id=requirement_id, outcome="unavailable",
                model=model, latency_ms=_elapsed_ms(started), error=_sanitize_error(exc),
            )
            raise
        except Exception as exc:  # noqa: BLE001 - 校验器自身异常同样视为不可用
            error = "授权审查结果校验失败：" + _sanitize_error(exc)
            self._write_audit(
                digest=digest, requirement_id=requirement_id, outcome="unavailable",
                model=model, latency_ms=_elapsed_ms(started), error=error,
            )
            raise ReviewUnavailable(error) from exc
        self._write_audit(
            digest=digest,
            requirement_id=requirement_id,
            outcome=validated.decision.value,
            decision=validated.decision.value,
            reason=validated.reason,
            model=model,
            latency_ms=_elapsed_ms(started),
        )
        return validated

    async def _call_model(self, messages: list[ChatMessage]) -> tuple[str, str | None]:
        if self.model_call is not None:
            result = self.model_call(messages)
            if inspect.isawaitable(result):
                result = await result
            return _completion_text(result), None
        factory = self.adapter_factory
        if factory is None:
            raise ReviewUnavailable("没有可用的主模型配置，无法完成授权审查（请先在设置中配置模型）")
        adapter = factory()
        if inspect.isawaitable(adapter):
            adapter = await adapter
        if adapter is None:
            raise ReviewUnavailable("没有可用的主模型配置，无法完成授权审查（请先在设置中配置模型）")
        model_name = getattr(adapter, "model", None)
        completion = await adapter.complete(messages, tools=[])
        return _completion_text(completion), (None if model_name is None else str(model_name))

    def _write_audit(
        self,
        *,
        digest: str,
        requirement_id: str,
        outcome: str,
        model: str | None = None,
        decision: str | None = None,
        reason: str | None = None,
        latency_ms: int | None = None,
        error: str | None = None,
    ) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record(
                request_digest=digest,
                requirement_id=requirement_id or None,
                outcome=outcome,
                model=model,
                decision=decision,
                reason=reason,
                latency_ms=latency_ms,
                error=error,
            )
        except Exception:  # noqa: BLE001 - 审计失败不能把有效判定变成不可用
            logger.warning("写入授权审查审计失败（不影响本次判定）", exc_info=True)


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


__all__ = [
    "AuthorizationReviewer",
    "DEFAULT_TIMEOUT_SECONDS",
    "ModelAuthorizationReviewer",
    "OBJECT_KINDS",
    "REVIEWABLE_OPERATION_KINDS",
    "REVIEW_MODE",
    "ReviewAuditLedger",
    "ReviewDecision",
    "ReviewObject",
    "ReviewOperation",
    "ReviewOutcome",
    "ReviewRequest",
    "ReviewUnavailable",
    "build_review_payload",
    "deterministic_allow_guard",
    "operation_fingerprint",
    "parse_review_outcome",
    "render_review_messages",
    "review_payload_json",
    "review_request_digest",
    "validate_outcome",
];
