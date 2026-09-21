"""TOOL_END 丢失之后，工具卡的最终结果仍然要能恢复。

真实缺陷：主 Turn 里 Tool X 开始执行（前端显示「运行中」），随后 X 实际上已经
success / failed / cancelled，但 `TOOL_END` 因为事件总线过载 / 断线 / replay 缺失
没有送到前端。RESYNC 之后前端只知道「X 不在 active tools 里」，
于是显示「结果未收到」—— 而服务器其实知道结果。

这一组测试锁住事实来源：**权威状态在发送实时事件之前/同步写好**，
事件丢不丢都不影响 snapshot 恢复出终态。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from agent.adapters.base import AdapterMode, ChatMessage, Completion, ToolCall
from agent.api.server import create_app
from agent.config import Settings
from agent.core.loop import AgentLoop
from agent.core.tool_state import ToolExecutionState
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry


class _RecordingBus:
    """只记录事件；`drop` 里的类型模拟「这条通知没送到客户端」。"""

    def __init__(self, drop: tuple[str, ...] = ()) -> None:
        self.events: list[tuple[str, dict]] = []
        self._drop = set(drop)

    async def publish(self, event) -> None:  # noqa: ANN001
        event_type = getattr(getattr(event, "type", ""), "value", getattr(event, "type", ""))
        if event_type in self._drop:
            return
        self.events.append((str(event_type), dict(getattr(event, "data", {}))))

    def of(self, event_type: str) -> list[dict]:
        return [data for name, data in self.events if name == event_type]


class _EchoTool(Tool):
    name = "echo"
    description = "回声"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs) -> ToolResult:  # noqa: ANN003
        return ToolResult(ok=True, content="pong")


class _BoomTool(Tool):
    name = "boom"
    description = "失败"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs) -> ToolResult:  # noqa: ANN003
        return ToolResult(ok=False, error="boom: 目标文件不存在")


class _BlockingTool(Tool):
    name = "blocking"
    description = "阻塞"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, started: asyncio.Event) -> None:
        self._started = started

    async def run(self, **kwargs) -> ToolResult:  # noqa: ANN003
        self._started.set()
        await asyncio.sleep(30)
        return ToolResult(ok=True, content="never")


class _OneCallAdapter:
    mode = AdapterMode.NATIVE
    model = "test"

    def __init__(self, tool: str, call_id: str = "call_1") -> None:
        self._tool = tool
        self._call_id = call_id
        self.calls = 0

    async def complete(self, messages, tools, **kw):  # noqa: ANN001, ANN003
        self.calls += 1
        if self.calls == 1:
            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[ToolCall(id=self._call_id, name=self._tool, arguments={})],
                )
            )
        return Completion(message=ChatMessage(role="assistant", content="完成"))


def _loop(tool: Tool, bus, state: ToolExecutionState, *, turn_id: str = "turn_1", call_id: str = "call_1"):
    registry = ToolRegistry()
    registry.register(tool)
    loop = AgentLoop(
        _OneCallAdapter(tool.name, call_id), registry, bus, turn_id=turn_id, tool_state=state
    )
    return loop


async def test_tool_success_is_authoritative_even_when_the_end_event_is_lost():
    state = ToolExecutionState()
    bus = _RecordingBus(drop=("TOOL_END",))
    await _loop(_EchoTool(), bus, state).run("go")

    assert bus.of("TOOL_END") == [], "这条通知确实没送到客户端"

    (record,) = state.snapshot(active_turn_id="turn_1")
    assert record["tool_call_id"] == "call_1"
    assert record["tool_name"] == "echo"
    assert record["status"] == "success"
    assert record["turn_id"] == "turn_1"
    assert record["ended_at"]


async def test_tool_failure_keeps_status_and_error_summary():
    state = ToolExecutionState()
    await _loop(_BoomTool(), _RecordingBus(), state).run("go")

    (record,) = state.snapshot(active_turn_id="turn_1")
    assert record["status"] == "failed"
    assert "不存在" in (record["error_summary"] or "")


async def test_tool_end_event_carries_the_terminal_status():
    """实时事件自己也要说清结局，前端不必靠 ok 猜「取消 vs 失败」。"""
    bus = _RecordingBus()
    await _loop(_EchoTool(), bus, ToolExecutionState()).run("go")
    assert bus.of("TOOL_END")[0]["status"] == "success"

    bus = _RecordingBus()
    await _loop(_BoomTool(), bus, ToolExecutionState()).run("go")
    assert bus.of("TOOL_END")[0]["status"] == "failed"


async def test_cancelled_tool_is_recorded_as_cancelled_not_failed():
    state = ToolExecutionState()
    started = asyncio.Event()
    loop = _loop(_BlockingTool(started), _RecordingBus(), state)

    run = asyncio.create_task(loop._dispatch_tool_calls([ToolCall(id="call_1", name="blocking", arguments={})]))
    await started.wait()
    assert state.snapshot(active_turn_id="turn_1")[0]["status"] == "running"

    loop.cancel()
    results = await run
    assert results["call_1"].ok is False

    (record,) = state.snapshot(active_turn_id="turn_1")
    assert record["status"] == "cancelled", "取消是独立语义，不能被并进 failed"


async def test_active_tools_still_report_only_what_is_really_running():
    state = ToolExecutionState()
    started = asyncio.Event()
    loop = _loop(_BlockingTool(started), _RecordingBus(), state, call_id="call_live")

    run = asyncio.create_task(loop._dispatch_tool_calls([ToolCall(id="call_live", name="blocking", arguments={})]))
    await started.wait()

    assert [t["tool_call_id"] for t in loop.active_tools()] == ["call_live"]

    loop.cancel()
    await run
    assert loop.active_tools() == []


async def test_state_is_shared_across_loops_so_an_earlier_turn_stays_recoverable():
    """进程级状态：后一轮开始时，前一轮的终态不会被覆盖掉。"""
    state = ToolExecutionState()
    await _loop(_EchoTool(), _RecordingBus(), state, turn_id="turn_1", call_id="call_1").run("go")
    await _loop(_BoomTool(), _RecordingBus(), state, turn_id="turn_2", call_id="call_2").run("go")

    records = {r["tool_call_id"]: r for r in state.snapshot(active_turn_id="turn_2")}
    assert records["call_1"]["status"] == "success"
    assert records["call_2"]["status"] == "failed"


# ---------------------------------------------------------------------------
# runtime snapshot：TOOL_END 丢失后的恢复入口
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(db_conn, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def test_runtime_snapshot_carries_terminal_tool_state_after_the_event_is_lost(client):
    """服务器知道结果 → 前端就必须能恢复成 success / failed / cancelled。"""
    ctx = client.app.state.ctx
    ctx.tool_state.start("turn_a", "call_1", "fs_read")
    ctx.tool_state.start("turn_a", "call_2", "run_cmd")
    ctx.tool_state.start("turn_a", "call_3", "web_search")
    ctx.tool_state.finish("turn_a", "call_1", "success")
    ctx.tool_state.finish("turn_a", "call_2", "failed", error="命令退出码 1")
    ctx.tool_state.finish("turn_a", "call_3", "cancelled")

    body = client.get("/api/runtime/state").json()
    tools = {t["tool_call_id"]: t for t in body["tools"]}

    assert tools["call_1"]["status"] == "success"
    assert tools["call_2"]["status"] == "failed"
    assert tools["call_2"]["error_summary"] == "命令退出码 1"
    assert tools["call_3"]["status"] == "cancelled"
    assert tools["call_1"]["turn_id"] == "turn_a"


def test_runtime_snapshot_hides_subagent_internal_tools(client):
    """Subagent 只恢复 Task 级状态：内部工具的明细不进主 snapshot。"""
    ctx = client.app.state.ctx
    ctx.tool_state.start("subagent:task_1", "call_inner", "memory_search")
    ctx.tool_state.finish("subagent:task_1", "call_inner", "success")

    assert client.get("/api/runtime/state").json()["tools"] == []


def test_runtime_snapshot_does_not_claim_a_stale_running_tool(client):
    """Turn 已经不在、记录还停在 running：只能报 unknown，不能报「还在跑」。"""
    ctx = client.app.state.ctx
    ctx.tool_state.start("turn_gone", "call_1", "fs_read")

    (tool,) = client.get("/api/runtime/state").json()["tools"]
    assert tool["status"] == "unknown"
