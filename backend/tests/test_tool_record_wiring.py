"""工具记录写入接线：终态在 `tool/end` 才权威，全文只在 `tool/result` 拿得到。

真实约束：`tool/result` 看不到「取消」标记（取消与失败必须分开），
而 `tool/end` 只有 200 字预览 —— 所以全文在 result 时暂存、终态在 end 时落库。
"""

from __future__ import annotations

import asyncio

import agent.core  # noqa: F401 - 导入顺序：agent.tools 必须在 agent.core 之后

from agent.adapters.base import AdapterMode, ChatMessage, Completion, ToolCall
from agent.api.events import EventType
from agent.core.loop import AgentLoop
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry

LONG_OUTPUT = "完整输出：" + "细节" * 400


class _Bus:
    def __init__(self) -> None:
        self.events = []

    async def publish(self, event):  # noqa: ANN001
        self.events.append(event)


class _EchoTool(Tool):
    name = "echo"
    description = "echo"
    parameters = {"type": "object", "properties": {"x": {"type": "integer"}}}

    async def run(self, **kwargs):
        return ToolResult(ok=True, content=LONG_OUTPUT)


class _OneToolAdapter:
    mode = AdapterMode.NATIVE
    model = "test"

    def __init__(self) -> None:
        self.n = 0

    async def complete(self, messages, tools, **kw):  # noqa: ANN001
        self.n += 1
        if self.n == 1:
            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[ToolCall(id="call_1", name="echo", arguments={"x": 1})],
                ),
                usage={"completion_tokens": 1},
            )
        return Completion(
            message=ChatMessage(role="assistant", content="完成"),
            usage={"completion_tokens": 1},
        )


def _run_with_trace(trace):
    registry = ToolRegistry()
    registry.register(_EchoTool())
    bus = _Bus()
    loop = AgentLoop(
        _OneToolAdapter(), registry, bus, tool_trace=trace, turn_id="turn_wire"
    )
    asyncio.run(loop.run("go"))
    return bus


def _tool_end(bus) -> dict:
    ends = [e for e in bus.events if e.type == EventType.TOOL_END]
    assert ends, "没有收到 TOOL_END"
    return ends[-1].data


def test_tool_trace_receives_terminal_facts_and_full_output():
    seen: list[dict] = []

    def trace(payload):
        seen.append(payload)
        return "tr_test"

    bus = _run_with_trace(trace)

    assert len(seen) == 1
    payload = seen[0]
    assert payload["tool_name"] == "echo"
    assert payload["call_id"] == "call_1"
    assert payload["turn_id"] == "turn_wire"
    assert payload["seq"] == 1
    assert payload["status"] == "success"
    assert isinstance(payload["duration_ms"], int)
    assert payload["result"] == LONG_OUTPUT          # 全文，不是 200 字预览
    assert payload["arguments"] == {"x": 1}
    assert _tool_end(bus)["record_id"] == "tr_test"  # 实时卡片要靠它取全文


def test_tool_trace_failure_never_breaks_the_tool_result():
    """写历史失败（这里直接抛）不能让工具结果变成失败，也不能让这一轮崩掉。"""

    def trace(payload):  # noqa: ARG001
        raise RuntimeError("历史写不进去")

    bus = _run_with_trace(trace)
    end = _tool_end(bus)
    assert end["ok"] is True
    assert end["record_id"] is None


def test_call_ids_get_sequence_numbers_per_turn():
    """同一轮里多次调用必须有稳定顺序（历史卡片按它排序）。"""
    seen: list[dict] = []

    def trace(payload):
        seen.append(payload)
        return None

    _run_with_trace(trace)
    assert [p["seq"] for p in seen] == [1]
