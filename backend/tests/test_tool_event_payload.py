"""TOOL_START / TOOL_END 的载荷契约（第三阶段 spec 第 32~34、97~98 条）。

前端靠 `call_id` 把「开始执行」与「执行结束」更新到**同一张卡**上：

* 缺 `call_id` → 会被当成两次不同的调用，用户看到两张卡（重复卡）；
* 缺 `duration_ms` → 卡片只能显示状态，不能显示耗时。

这两个字段是协议的硬要求，不能只在文档里写着。
"""

from __future__ import annotations

import asyncio
import json

from agent.adapters.base import AdapterMode, ToolCall
from agent.api.bus import EventBus
from agent.core.loop import AgentLoop
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry


class _SlowEchoTool(Tool):
    name = "echo"
    description = "回声"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

    def present_call(self, arguments: dict) -> dict:
        return {"title": "回声测试", "summary": str(arguments.get("text") or "")}

    async def run(self, **kwargs) -> ToolResult:
        await asyncio.sleep(0.05)
        return ToolResult(ok=True, content="pong")


class _Adapter:
    mode = AdapterMode.NATIVE
    model = "t"


def _http_events(chunks: list[str]) -> list[dict]:
    out: list[dict] = []
    for line in chunks:
        for part in line.splitlines():
            if part.startswith("data: "):
                out.append(json.loads(part[6:]))
    return out


async def test_tool_start_and_end_share_call_id_and_end_has_duration():
    registry = ToolRegistry()
    registry.register(_SlowEchoTool())
    bus = EventBus()
    loop = AgentLoop(_Adapter(), registry, bus, turn_id="turn_x")

    chunks: list[str] = []

    async def consume() -> None:
        async for chunk in bus.stream():
            chunks.append(chunk)
            if len(_http_events(chunks)) >= 2:
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await loop._dispatch_tool_calls([ToolCall(id="call_1", name="echo", arguments={"text": "hi"})])
    await asyncio.wait_for(task, timeout=5)

    events = _http_events(chunks)
    start = next(e for e in events if e["type"] == "TOOL_START")
    end = next(e for e in events if e["type"] == "TOOL_END")

    assert start["data"]["call_id"] == "call_1"
    assert end["data"]["call_id"] == "call_1"
    # 开始执行时就有中文标题与摘要：卡片不必等结束才说人话
    assert start["data"]["presentation"]["title"] == "回声测试"
    assert isinstance(end["data"]["duration_ms"], int)
    assert end["data"]["duration_ms"] >= 0


async def test_tool_end_reports_failure_with_call_id():
    class _BoomTool(Tool):
        name = "boom"
        description = "boom"
        parameters = {"type": "object", "properties": {}}

        async def run(self, **kwargs) -> ToolResult:
            return ToolResult(ok=False, error="工具「boom」未执行（用户已拒绝）")

    registry = ToolRegistry()
    registry.register(_BoomTool())
    bus = EventBus()
    loop = AgentLoop(_Adapter(), registry, bus, turn_id="turn_y")

    chunks: list[str] = []

    async def consume() -> None:
        async for chunk in bus.stream():
            chunks.append(chunk)
            if len(_http_events(chunks)) >= 2:
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await loop._dispatch_tool_calls([ToolCall(id="call_2", name="boom", arguments={})])
    await asyncio.wait_for(task, timeout=5)

    end = next(e for e in _http_events(chunks) if e["type"] == "TOOL_END")
    assert end["data"]["call_id"] == "call_2"
    assert end["data"]["ok"] is False
    assert "未执行" in end["data"]["error"]
