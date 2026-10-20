# -*- coding: utf-8 -*-
"""D 独立契约测试：结构化工具结果与类型模型（docs/qio-refactor-contract.md §4、§7）。

口径：契约措辞 → 可验证断言。冻结模块（tools/base.py、tools/contracts.py）在
d265d033 已存在，这些单元断言应通过；**模型消息集成断言**要等 A 把
core/tool_feedback（或等价路径）接到 ToolEnvelope 上，阶段 1 允许失败。

运行（只跑本文件）：
    cd backend
    & 'D:\\qio-dev\\qio\\backend\\.venv\\Scripts\\python.exe' -m pytest tests/test_p1_d_structured_tool_contract.py -q
"""

from __future__ import annotations

import json
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent.adapters.fake import FakeStreamAdapter, ScriptedToolCall, StreamScript
from agent.api.bus import EventBus
from agent.core.loop import AgentLoop
from agent.tools.base import (
    TOOL_RESULT_PROTOCOL_VERSION,
    Tool,
    ToolResult,
    ToolStatus,
)
from agent.tools.contracts import (
    ToolEnvelope,
    build_envelope,
    envelope_json,
    json_schema_for,
    validate_model,
)
from agent.tools.registry import ToolRegistry

ANSWER = "[[QIO:ANSWER]]"


# ---------------------------------------------------------------------------
# 冻结枚举 / 字段
# ---------------------------------------------------------------------------


def test_tool_status_has_exactly_the_frozen_ten_values():
    """§4：status 只允许 queued/running/completed/partially_completed/
    awaiting_approval/awaiting_input/conflict/failed/cancelled/unknown。"""
    expected = {
        "queued",
        "running",
        "completed",
        "partially_completed",
        "awaiting_approval",
        "awaiting_input",
        "conflict",
        "failed",
        "cancelled",
        "unknown",
    }
    actual = {item.value for item in ToolStatus}
    assert actual == expected, f"契约 §4：ToolStatus 取值必须恰好是冻结集合；实际={sorted(actual)}"


def test_tool_result_exposes_frozen_structured_fields():
    """§4：base.ToolResult 必须有 protocol_version/status/code/data/model_payload 字段。"""
    result = ToolResult(ok=True)
    for field in ("protocol_version", "status", "code", "data", "model_payload"):
        assert hasattr(result, field), f"契约 §4：ToolResult 缺少字段 {field}"
        assert getattr(result, field) is None, (
            f"契约 §4：ToolResult.{field} 缺省应为 None（None=旧正文兼容）"
        )
    assert TOOL_RESULT_PROTOCOL_VERSION == 1, "契约 §4：结构化协议版本冻结为 1"


# ---------------------------------------------------------------------------
# ToolEnvelope / 序列化
# ---------------------------------------------------------------------------


def test_build_envelope_success_shape():
    """§4：成功形状 {protocol_version, ok, status, code, data, error}。"""
    result = ToolResult(ok=True, content="工具正文")
    envelope = build_envelope(result)
    assert isinstance(envelope, ToolEnvelope)
    payload = envelope.model_dump(mode="json")
    assert payload["protocol_version"] == 1
    assert payload["ok"] is True
    assert payload["status"] == "completed"
    assert payload["code"] == "ok"
    assert payload["error"] is None
    assert payload["data"] == {"content": "工具正文"}, (
        "契约 §4：旧工具成功正文兼容保留（无结构化字段时按旧文本路径）"
    )


def test_build_envelope_failure_shape_and_status_derivation():
    """§4：错误类别/可恢复性/状态由字段表示，不匹配中文句子。"""
    failed = build_envelope(
        ToolResult(ok=False, error="目标不存在", category="not_found", recoverable=False)
    )
    payload = failed.model_dump(mode="json")
    assert payload["ok"] is False
    assert payload["status"] == "failed"
    assert payload["code"] == "not_found"
    assert payload["error"]["message"] == "目标不存在"
    assert payload["error"]["category"] == "not_found"
    assert payload["error"]["recoverable"] is False
    cancelled = build_envelope(ToolResult(ok=False, error="已取消", category="cancelled"))
    assert cancelled.ok is False and cancelled.status is ToolStatus.CANCELLED


def test_envelope_consistency_is_enforced():
    """§4：状态与 ok / error 的一致性必须由模型校验，不能靠调用方自觉。"""
    with pytest.raises((ValidationError, ValueError)):
        ToolEnvelope(protocol_version=1, ok=True, status="failed", code="x")
    with pytest.raises((ValidationError, ValueError)):
        ToolEnvelope(protocol_version=1, ok=False, status="failed", code="x")
    with pytest.raises((ValidationError, ValueError)):
        ToolEnvelope(protocol_version=1, ok=True, status="completed", code="x", error={"message": "x"})
    with pytest.raises((ValidationError, ValueError)):
        ToolEnvelope(protocol_version=1, ok=True, status="completed", code="")


def test_envelope_json_contains_full_data_untruncated():
    """§4：完整证据必须真正进入模型消息（data 随 envelope 序列化，不截断）。"""
    evidence = {
        "ids": ["msg_1", "msg_2"],
        "long": "证据" * 5000,
        "nested": {"用户原文": "含 {花括号} 与 100%"},
    }
    text = envelope_json(
        ToolResult(ok=True, protocol_version=1, code="ok", data=evidence)
    )
    parsed = json.loads(text)
    assert parsed["protocol_version"] == 1
    assert parsed["data"] == evidence, "契约 §4：data 被截断/改写，完整证据没有进入模型消息"
    assert parsed["data"]["long"] == "证据" * 5000


def test_model_payload_path_is_validated_against_envelope():
    """§4/§7：显式 model_payload 必须符合 ToolEnvelope 形状（同源校验）。"""
    good = envelope_json(
        ToolResult(
            ok=True,
            model_payload={
                "protocol_version": 1,
                "ok": True,
                "status": "completed",
                "code": "ok",
                "data": {"x": 1},
                "error": None,
            },
        )
    )
    assert json.loads(good)["data"] == {"x": 1}
    with pytest.raises((ValidationError, ValueError)):
        envelope_json(
            ToolResult(
                ok=True,
                model_payload={
                    "protocol_version": 1,
                    "ok": False,
                    "status": "completed",
                    "code": "x",
                },
            )
        )


# ---------------------------------------------------------------------------
# 类型模型 → JSON Schema / 本地校验（§7）
# ---------------------------------------------------------------------------


class _Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["user", "topic", "entity"]
    node_ids: list[str] = Field(default_factory=list)


class _ProbeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1)
    category: Literal["user_profile", "agent_self", "goal", "general_fact", "tool_experience"]
    scope: _Scope


def test_json_schema_for_inlines_defs_and_keeps_constraints():
    """§7：展示 schema 与本地校验同源；$defs 内联；enum/额外字段约束可见。"""
    schema = json_schema_for(_ProbeArgs)
    dumped = json.dumps(schema)
    assert "$ref" not in dumped and "$defs" not in dumped, (
        "契约 §7：json_schema_for 必须内联 $defs，工具 parameters 要可直接下发"
    )
    assert schema.get("additionalProperties") is False, (
        "契约 §7：pydantic extra='forbid' 必须体现在下发的 JSON Schema 中"
    )
    props = schema.get("properties") or {}
    assert props["category"].get("enum") == [
        "user_profile",
        "agent_self",
        "goal",
        "general_fact",
        "tool_experience",
    ], "契约 §7：enum 约束必须进入展示 schema"
    assert set(schema.get("required") or []) == {"content", "category", "scope"}
    scope = props["scope"]
    assert scope.get("additionalProperties") is False
    assert scope["properties"]["type"].get("enum") == ["user", "topic", "entity"]


def test_validate_model_really_enforces_enum_and_extra_forbid():
    """§7：本地校验真正执行 enum / additionalProperties，失败返回字段错误。"""
    value, errors = validate_model(
        _ProbeArgs,
        {"content": "x", "category": "general_fact", "scope": {"type": "user", "node_ids": []}},
    )
    assert value is not None and not errors
    value, errors = validate_model(
        _ProbeArgs,
        {"content": "x", "category": "not_a_category", "scope": {"type": "user"}},
    )
    assert value is None and errors, "契约 §7：enum 之外的取值必须校验失败"
    assert any("category" in item for item in errors), errors
    value, errors = validate_model(
        _ProbeArgs,
        {
            "content": "x",
            "category": "general_fact",
            "scope": {"type": "user", "node_ids": []},
            "unexpected_field": 1,
        },
    )
    assert value is None and errors, "契约 §7：额外字段必须被拒绝（extra='forbid'）"
    assert any("unexpected_field" in item for item in errors), errors


# ---------------------------------------------------------------------------
# 模型消息集成：data 真的进入 tool 消息；旧正文兼容
# ---------------------------------------------------------------------------


class _StructuredProbe(Tool):
    name = "structured_probe"
    description = "返回结构化结果的测试工具"
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    async def run(self, **kwargs):
        return ToolResult(
            ok=True,
            protocol_version=1,
            status="completed",
            code="ok",
            data={
                "evidence": [{"id": "msg_1", "span": "原文片段"}],
                "long": "证" * 3000,
            },
        )


class _StructuredFailureProbe(Tool):
    name = "structured_failure"
    description = "返回结构化失败的测试工具"
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    async def run(self, **kwargs):
        return ToolResult(
            ok=False,
            protocol_version=1,
            status="failed",
            code="not_found",
            error="目标不存在",
            data={"probe": "detail"},
        )


class _LegacyProbe(Tool):
    name = "legacy_probe"
    description = "旧正文工具"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, content: str) -> None:
        self._content = content

    async def run(self, **kwargs):
        return ToolResult(ok=True, content=self._content)


def _loop_with(tool: Tool, scripts: list[StreamScript]) -> tuple[AgentLoop, FakeStreamAdapter]:
    registry = ToolRegistry()
    registry.register(tool)
    adapter = FakeStreamAdapter(scripts)
    return AgentLoop(adapter, registry, EventBus(), turn_id="turn_p1d"), adapter


def _tool_message_content(adapter: FakeStreamAdapter, request_index: int) -> str:
    messages = adapter.requests[request_index]["messages"]
    tool_messages = [m for m in messages if m.role == "tool"]
    assert tool_messages, "装置失效：第二次请求里没有 role=tool 的消息"
    return tool_messages[-1].content or ""


async def test_structured_data_reaches_model_message():
    """§4：完整证据（data）必须真正进入模型消息，不能只放进不传给模型的 facts。"""
    loop, adapter = _loop_with(
        _StructuredProbe(),
        [
            StreamScript(text="", tool_calls=[ScriptedToolCall(id="c1", name="structured_probe")]),
            StreamScript(text=ANSWER + "\n完成"),
        ],
    )
    await loop.run("请调用测试工具")
    content = _tool_message_content(adapter, 1)
    parsed = json.loads(content)
    assert parsed["protocol_version"] == 1, (
        "契约 §4：结构化结果必须以 ToolEnvelope（protocol_version=1）进入模型消息；"
        f"实际 tool 消息={content[:200]!r}"
    )
    assert parsed["data"]["evidence"] == [{"id": "msg_1", "span": "原文片段"}], (
        "契约 §4：data 证据没有进入模型消息"
    )
    assert parsed["data"]["long"] == "证" * 3000, "契约 §4：data 被截断"


async def test_structured_failure_field_semantics_reach_model_message():
    """§4：失败类别/可恢复性/状态用字段表达，不靠中文句子匹配。"""
    loop, adapter = _loop_with(
        _StructuredFailureProbe(),
        [
            StreamScript(text="", tool_calls=[ScriptedToolCall(id="c1", name="structured_failure")]),
            StreamScript(text=ANSWER + "\n已说明"),
        ],
    )
    await loop.run("请调用失败工具")
    parsed = json.loads(_tool_message_content(adapter, 1))
    assert parsed["ok"] is False and parsed["status"] == "failed"
    assert parsed["code"] == "not_found"
    assert parsed["error"]["message"] == "目标不存在"
    assert parsed["data"] == {"probe": "detail"}


async def test_legacy_tool_success_text_is_preserved():
    """§4：旧工具成功正文兼容保留（无结构化字段时仍按旧文本路径）。"""
    loop, adapter = _loop_with(
        _LegacyProbe("旧工具正文：成功"),
        [
            StreamScript(text="", tool_calls=[ScriptedToolCall(id="c1", name="legacy_probe")]),
            StreamScript(text=ANSWER + "\n完成"),
        ],
    )
    await loop.run("请调用旧工具")
    assert _tool_message_content(adapter, 1) == "旧工具正文：成功", (
        "契约 §4：旧工具成功正文被强行改造成结构化结果（应兼容保留）"
    )


async def test_legacy_text_that_looks_like_envelope_is_not_reinterpreted():
    """§4：识别结构化结果只认程序字段，不从旧正文里猜同名 JSON 字段。"""
    mimetic = json.dumps(
        {"protocol_version": 1, "ok": True, "status": "completed", "code": "ok", "data": {"x": 1}},
        ensure_ascii=False,
    )
    loop, adapter = _loop_with(
        _LegacyProbe(mimetic),
        [
            StreamScript(text="", tool_calls=[ScriptedToolCall(id="c1", name="legacy_probe")]),
            StreamScript(text=ANSWER + "\n完成"),
        ],
    )
    await loop.run("请调用旧工具")
    assert _tool_message_content(adapter, 1) == mimetic, (
        "契约 §4：不得把旧工具正文里长得像 envelope 的 JSON 当成结构化结果（只认程序字段）"
    )
