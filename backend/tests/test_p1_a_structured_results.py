"""Phase 1 A：结构化工具结果（data 进模型消息）+ 类型化参数（schema 与校验同源）。"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent.adapters.base import ChatMessage, ToolCall, ToolSpec
from agent.adapters.fake import FakeStreamAdapter, ScriptedToolCall, StreamScript
from agent.api.bus import EventBus
from agent.core import tool_feedback
from agent.core.loop import AgentLoop
from agent.tools.base import TOOL_RESULT_PROTOCOL_VERSION, Tool, ToolResult
from agent.tools.contracts import json_schema_for
from agent.tools.registry import ToolRegistry


class _StructuredTool(Tool):
    name = "structured_rows"
    description = "返回结构化证据"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs):
        return ToolResult(
            ok=True,
            protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
            code="ok",
            data={"rows": [{"id": 1, "text": "证据一"}], "source": "db"},
        )


def test_structured_success_envelope_carries_data():
    result = ToolResult(
        ok=True,
        protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
        code="ok",
        data={"rows": [{"id": 7}], "note": "完整证据"},
    )
    text = tool_feedback.message(result, tool_name="structured_rows", call_id="c1")
    payload = json.loads(text)
    assert payload["protocol_version"] == 1
    assert payload["ok"] is True
    assert payload["status"] == "completed"
    assert payload["code"] == "ok"
    assert payload["data"]["rows"] == [{"id": 7}]
    assert payload["data"]["note"] == "完整证据"
    assert payload["error"] is None


def test_structured_failure_uses_fields_not_chinese_sentences():
    result = ToolResult(
        ok=False,
        protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
        status="conflict",
        code="version_conflict",
        data={"expected": 3, "actual": 4},
        error="版本已变化",
        category="conflict",
        recoverable=True,
    )
    payload = json.loads(tool_feedback.message(result))
    assert payload["ok"] is False
    assert payload["status"] == "conflict"
    assert payload["code"] == "version_conflict"
    assert payload["error"]["message"] == "版本已变化"
    assert payload["error"]["recoverable"] is True
    assert payload["data"] == {"expected": 3, "actual": 4}


def test_explicit_model_payload_is_used_as_is():
    result = ToolResult(
        ok=True,
        model_payload={
            "protocol_version": 1,
            "ok": True,
            "status": "partially_completed",
            "code": "partial",
            "data": {"done": 2, "total": 3},
            "error": None,
        },
    )
    payload = json.loads(tool_feedback.message(result))
    assert payload["status"] == "partially_completed"
    assert payload["data"] == {"done": 2, "total": 3}


def test_legacy_text_results_stay_compatible():
    # 旧工具：成功正文原样，不强行改造成 JSON
    ok = ToolResult(ok=True, content="原样结果")
    assert tool_feedback.message(ok) == "原样结果"
    # 旧失败：仍给出中文事实（不是空正文）
    failed = ToolResult(ok=False, error="ZeroDivisionError: division by zero")
    text = tool_feedback.message(failed, tool_name="calc", call_id="c9")
    assert "失败" in text and "ZeroDivisionError" in text and "c9" in text


async def test_structured_data_reaches_the_model_message():
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                tool_calls=[
                    ScriptedToolCall(id="c1", name="structured_rows", arguments={})
                ]
            ),
            StreamScript(text="[[QIO:ANSWER]]\n完成"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_StructuredTool())
    result = await AgentLoop(
        adapter, registry, EventBus(), turn_id="turn_p1a_structured"
    ).run("给我证据")

    assert result.final_content == "完成"
    tool_messages = [
        m
        for m in adapter.requests[-1]["messages"]
        if getattr(m, "role", None) == "tool"
    ]
    assert tool_messages, "结构化结果没有回填给模型"
    payload = json.loads(tool_messages[-1].content or "")
    assert payload["protocol_version"] == 1
    assert payload["data"]["rows"] == [{"id": 1, "text": "证据一"}]
    assert payload["data"]["source"] == "db"


# ---- 类型化参数：展示 schema 与本地校验同源 ------------------------------


class _Kind(str, Enum):
    alpha = "alpha"
    beta = "beta"


class _TypedArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    kind: _Kind
    count: int = Field(default=1, ge=1, le=5)


class _TypedTool(Tool):
    name = "typed_tool"
    description = "类型化参数工具"
    parameters = {"type": "object", "properties": {}}  # 展示 schema 应从 arg_model 生成
    arg_model = _TypedArgs

    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []

    async def run(self, **kwargs):
        self.runs.append(dict(kwargs))
        return ToolResult(ok=True, content="ok")


def test_specs_schema_comes_from_pydantic_model():
    registry = ToolRegistry()
    registry.register(_TypedTool())
    spec = registry.specs()[0]
    expected = json_schema_for(_TypedArgs)
    assert spec.parameters["properties"]["name"] == expected["properties"]["name"]
    assert spec.parameters["properties"]["kind"] == expected["properties"]["kind"]
    assert spec.parameters["properties"]["kind"]["enum"] == ["alpha", "beta"]
    assert spec.parameters["additionalProperties"] is False
    assert set(spec.parameters["required"]) == {"name", "kind"}
    # 叙事信封仍然注入，且说明来自 prompts
    assert "_qio" in spec.parameters["properties"]
    assert (
        spec.parameters["properties"]["_qio"]["description"]
        == prompts_narrative_description()
    )


def prompts_narrative_description() -> str:
    from agent import prompts

    return prompts.narrative_field_description()


async def test_invalid_typed_arguments_return_structured_failure_and_do_not_run():
    tool = _TypedTool()
    registry = ToolRegistry()
    registry.register(tool)

    # 枚举非法
    result = await registry.execute(
        ToolCall(id="c1", name="typed_tool", arguments={"name": "x", "kind": "gamma"})
    )
    assert not result.ok
    assert result.protocol_version == TOOL_RESULT_PROTOCOL_VERSION
    assert result.status == "failed"
    assert result.code == "invalid_arguments"
    assert result.data and result.data["errors"]
    assert tool.runs == []

    payload = json.loads(tool_feedback.message(result, tool_name="typed_tool"))
    assert payload["code"] == "invalid_arguments"
    assert payload["status"] == "failed"
    assert payload["data"]["errors"]

    # 额外字段（additionalProperties=false）
    result = await registry.execute(
        ToolCall(
            id="c2",
            name="typed_tool",
            arguments={"name": "x", "kind": "alpha", "extra": 1},
        )
    )
    assert result.code == "invalid_arguments"
    assert tool.runs == []


async def test_valid_typed_arguments_run_once_with_validated_values():
    tool = _TypedTool()
    registry = ToolRegistry()
    registry.register(tool)
    result = await registry.execute(
        ToolCall(id="c3", name="typed_tool", arguments={"name": "x", "kind": "alpha"})
    )
    assert result.ok
    assert len(tool.runs) == 1
    assert tool.runs[0]["name"] == "x"
    assert tool.runs[0]["kind"] in (_Kind.alpha, "alpha")
    assert tool.runs[0]["count"] == 1
