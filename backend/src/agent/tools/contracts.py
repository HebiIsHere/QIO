"""Phase 1 冻结公共契约：结构化工具结果 + 类型模型 → JSON Schema。

两件事共用同一份类型定义：

1. 结构化工具结果（{protocol_version, ok, status, code, data, error}）：
   - 由程序自有类型标识（protocol_version / ToolEnvelope）识别，不从任意正文里
     猜同名 JSON 字段；
   - 完整证据放在 data 里，经 core/tool_feedback 序列化后真正进入模型消息。
2. 新工具与关键改造路径的参数模型：
   - 展示给模型的 JSON Schema 用 :func:`json_schema_for` 从 pydantic 模型生成；
   - 本地校验用同一个模型 :func:`validate_model`（真正执行 enum / oneOf /
     additionalProperties 约束；不再依赖 tools/schema.py 的轻量检查）。

本地校验与展示 schema 必须来自同一类型；不要手写第二套 schema。
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agent.tools.base import TOOL_RESULT_PROTOCOL_VERSION, ToolResult, ToolStatus

#: 成功语义的状态集合（ok=True）。
OK_STATUSES = frozenset(
    {
        ToolStatus.QUEUED,
        ToolStatus.RUNNING,
        ToolStatus.COMPLETED,
        ToolStatus.PARTIALLY_COMPLETED,
        ToolStatus.AWAITING_APPROVAL,
        ToolStatus.AWAITING_INPUT,
    }
)

#: 非成功语义的状态集合（ok=False）。
NOT_OK_STATUSES = frozenset(
    {
        ToolStatus.CONFLICT,
        ToolStatus.FAILED,
        ToolStatus.CANCELLED,
        ToolStatus.UNKNOWN,
    }
)


class ToolErrorPayload(BaseModel):
    """结构化失败说明：类别、可恢复性与原因为字段，不做中文句子匹配。"""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1)
    category: str | None = None
    recoverable: bool | None = None


class ToolEnvelope(BaseModel):
    """结构化工具结果的唯一模型（也就是交给模型的 JSON 形状）。"""

    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal[1] = TOOL_RESULT_PROTOCOL_VERSION
    ok: bool
    status: ToolStatus
    code: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)
    error: ToolErrorPayload | None = None

    @model_validator(mode="after")
    def _check_consistency(self) -> "ToolEnvelope":
        if self.ok and self.status not in OK_STATUSES:
            raise ValueError(f"ok=True 不允许 status={self.status.value}")
        if not self.ok and self.status not in NOT_OK_STATUSES:
            raise ValueError(f"ok=False 不允许 status={self.status.value}")
        if not self.ok and self.error is None:
            raise ValueError("非成功结果必须给出 error")
        if self.ok and self.error is not None:
            raise ValueError("成功结果不应带 error")
        return self


def _derive_status(result: ToolResult) -> ToolStatus:
    if result.status:
        try:
            return ToolStatus(str(result.status))
        except ValueError:
            return ToolStatus.UNKNOWN
    if result.ok:
        return ToolStatus.COMPLETED
    if result.category == "cancelled":
        return ToolStatus.CANCELLED
    return ToolStatus.FAILED


def _derive_code(result: ToolResult) -> str:
    """稳定机器代码：优先用工具给的 code，否则从失败类别派生（不读中文正文）。"""
    if result.code:
        return str(result.code)
    if result.ok:
        return "ok"
    if result.category:
        return str(result.category)
    return "failed"


def build_envelope(result: ToolResult) -> ToolEnvelope:
    """把一次工具结果转成结构化契约（同一路径既用于模型消息，也用于本地校验）。"""
    status = _derive_status(result)
    ok = status in OK_STATUSES
    data = dict(result.data or {})
    # 旧工具没有结构化字段：成功正文按旧口径兼容保留在 data.content。
    if not data and (result.content or "").strip():
        data = {"content": result.content}
    error = None
    if not ok:
        error = ToolErrorPayload(
            message=(result.error or result.content or "工具执行失败").strip()
            or "工具执行失败",
            category=result.category,
            recoverable=result.recoverable,
        )
    return ToolEnvelope(
        ok=ok,
        status=status,
        code=_derive_code(result),
        data=data,
        error=error,
    )


def envelope_json(result: ToolResult) -> str:
    """结构化结果交给模型的正文：完整 envelope 序列化，不截断序列化后的 JSON。"""
    if result.model_payload is not None:
        payload = ToolEnvelope.model_validate(result.model_payload)
    else:
        payload = build_envelope(result)
    return json.dumps(
        payload.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
    )


def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref[len("#/$defs/") :]
            return _inline_refs(defs[name], defs)
        return {key: _inline_refs(value, defs) for key, value in node.items() if key != "$defs"}
    if isinstance(node, list):
        return [_inline_refs(item, defs) for item in node]
    return node


def json_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """从 pydantic 模型生成可下发给模型的 JSON Schema（$defs 内联，无递归引用）。"""
    schema = model.model_json_schema()
    defs = dict(schema.get("$defs") or {})
    inlined = _inline_refs(schema, defs)
    if not isinstance(inlined, dict):  # pragma: no cover - pydantic 顶层总是 object
        raise TypeError("模型 JSON Schema 顶层必须是 object")
    inlined.setdefault("type", "object")
    return inlined


def validate_model(
    model: type[BaseModel], arguments: dict[str, Any]
) -> tuple[BaseModel | None, list[str]]:
    """用类型模型做本地校验；返回（值, 错误列表）。约束（enum/oneOf/额外字段）真正执行。"""
    try:
        return model.model_validate(dict(arguments or {})), []
    except ValidationError as exc:
        errors: list[str] = []
        for item in exc.errors():
            location = ".".join(str(part) for part in item.get("loc", ()))
            errors.append(f"{location}: {item.get('msg', 'invalid')}")
        return None, errors
