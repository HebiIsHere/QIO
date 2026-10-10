"""B 独立验证：流式结束语义（审计 F06 / 冻结契约 C2）。

**反例（基线应为红）**：假厂商只发送已声明回答的一部分后干净收束 SSE、没有
finish_reason，系统仍给出 done / stop_reason_code=none（被当成正常完成）。

验证层级（不是直接构造 Completion）：本地真实 HTTP/SSE 假厂商 →
真 NativeAdapter / AnthropicAdapter → AgentLoop → 事件链，再把 TurnResult 喂给
TurnManager 断言 TURN_END 的结束语义。

运行：cd backend; uv run --frozen pytest tests/test_acc_b_stream_end.py -q
"""

from __future__ import annotations

import contextlib
import threading

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.core.loop import AgentLoop, LoopPhase
from agent.core.turn import TurnManager
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry

from _acc_b_provider import AccSseProvider

DECL = "[[QIO:ANSWER]]"


# ---- 装置 -------------------------------------------------------------------


@pytest.fixture()
def provider():
    server = AccSseProvider(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


class _EchoTool(Tool):
    name = "echo"
    description = "echo（验证用）"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}
    is_concurrency_safe = True

    def __init__(self) -> None:
        self.seen: list[dict] = []

    async def run(self, **kwargs):
        self.seen.append(dict(kwargs))
        return ToolResult(ok=True, content="echo:%s" % kwargs.get("text", ""))


def _registry() -> tuple[ToolRegistry, _EchoTool]:
    registry = ToolRegistry()
    tool = _EchoTool()
    registry.register(tool)
    return registry, tool


def _events(bus: EventBus, name: str) -> list[dict]:
    return [e.data for e in bus._history if e.type.value == name]


@contextlib.asynccontextmanager
async def _native_loop(server: AccSseProvider, steps: list[dict], registry: ToolRegistry):
    from openai import AsyncOpenAI

    from agent.adapters.native import NativeAdapter

    server.set(steps)
    client = AsyncOpenAI(
        base_url="http://127.0.0.1:%d/v1" % server.server_port,
        api_key="sk-acc-b-fake-0001",
        timeout=15.0,
    )
    try:
        bus = EventBus()
        loop = AgentLoop(
            NativeAdapter(client=client, model="acc-b-native"), registry, bus, turn_id="turn_accb"
        )
        yield loop, bus
    finally:
        await client.close()


@contextlib.asynccontextmanager
async def _anthropic_loop(server: AccSseProvider, steps: list[dict], registry: ToolRegistry):
    from agent.adapters.anthropic import AnthropicAdapter

    server.set(steps)
    adapter = AnthropicAdapter(
        api_key="sk-acc-b-fake-0001",
        model="acc-b-anthropic",
        endpoint="http://127.0.0.1:%d/v1" % server.server_port,
    )
    try:
        bus = EventBus()
        loop = AgentLoop(adapter, registry, bus, turn_id="turn_accb_an")
        yield loop, bus
    finally:
        await adapter.close()


async def _turn_end_for(result) -> dict:
    """把真实的 loop TurnResult 喂给 TurnManager，取 TURN_END（只读映射，不伪造流）。"""
    events: list[tuple[str, dict]] = []

    async def emitter(name: str, data: dict) -> None:
        events.append((name, data))

    async def runner(ctx) -> None:
        ctx.final_content = result.final_content
        ctx.result = {"ok": True, "turn": result.__dict__}

    manager = TurnManager(runner=runner, emitter=emitter)
    ctx = manager.submit("回答我")
    await manager.wait(ctx.turn_id, timeout=5)
    await manager.shutdown()
    ends = [data for name, data in events if name == "TURN_END"]
    assert ends, "没有发出 TURN_END"
    return ends[-1]


# ---- 1. 正常完整流是对照 -------------------------------------------------------


async def test_complete_sse_stream_stops_as_none(provider):
    registry, _tool = _registry()
    async with _native_loop(provider, [{"chunks": [DECL + "\n", "完整回答。"]}], registry) as (
        loop,
        bus,
    ):
        result = await loop.run("回答我")

    assert result.final_content == "完整回答。"
    assert result.phase == LoopPhase.DONE
    assert result.stop_reason_code == "none"
    assert _events(bus, "WARNING") == [], [w["code"] for w in _events(bus, "WARNING")]


async def test_complete_anthropic_stream_with_message_stop_is_none(provider):
    registry, _tool = _registry()
    async with _anthropic_loop(provider, [{"chunks": [DECL + "\n", "完整回答。"]}], registry) as (
        loop,
        bus,
    ):
        result = await loop.run("回答我")

    assert result.final_content == "完整回答。"
    assert result.stop_reason_code == "none"


# ---- 2. 无结束标记的不完整 EOF：保留正文、如实标未完成 ----------------------------


async def test_partial_sse_without_finish_reason_is_incomplete(provider):
    registry, _tool = _registry()
    async with _native_loop(
        provider,
        [{"chunks": [DECL + "\n", "前两句。", "第二句。"], "abort": True}],
        registry,
    ) as (loop, bus):
        result = await loop.run("回答我")

    # 已确认正文必须保留（不丢字）
    assert result.final_content == "前两句。第二句。", result.final_content
    # 但过程必须如实表示未完成
    assert result.stop_reason_code == "incomplete_stream", result.stop_reason_code
    assert result.stopped_by == "system"
    warnings = _events(bus, "WARNING")
    assert any(w["code"] == "incomplete_stream" for w in warnings), warnings

    end = await _turn_end_for(result)
    assert end["reason_code"] == "incomplete_stream", end
    assert "retry" in end["actions"], end["actions"]
    assert end["final_content"] == "前两句。第二句。"


async def test_incomplete_anthropic_stream_without_message_stop(provider):
    registry, _tool = _registry()
    async with _anthropic_loop(
        provider, [{"chunks": [DECL + "\n", "半句回答"], "abort": True}], registry
    ) as (loop, bus):
        result = await loop.run("回答我")

    assert result.final_content == "半句回答", result.final_content
    assert result.stop_reason_code == "incomplete_stream", result.stop_reason_code


async def test_usage_only_stream_is_incomplete_not_done(provider):
    registry, _tool = _registry()
    async with _native_loop(provider, [{"usage_only": True}], registry) as (loop, bus):
        result = await loop.run("回答我")

    assert result.stop_reason_code == "incomplete_stream", result.stop_reason_code
    assert _events(bus, "WARNING") and any(
        w["code"] == "incomplete_stream" for w in _events(bus, "WARNING")
    )


async def test_empty_sse_stream_is_incomplete_not_done(provider):
    registry, _tool = _registry()
    async with _native_loop(provider, [{"abort": True}], registry) as (loop, _bus):
        result = await loop.run("回答我")

    assert result.stop_reason_code == "incomplete_stream", result.stop_reason_code


# ---- 3. 传输错误：归一化失败，不伪装完成 ------------------------------------------


async def test_transport_break_mid_stream_fails_as_provider_error(provider):
    from agent.adapters.errors import ProviderError

    registry, _tool = _registry()
    async with _native_loop(
        provider, [{"chunks": ["半句"], "hard_abort": True}], registry
    ) as (loop, bus):
        with pytest.raises(ProviderError):
            await loop.run("回答我")

    # 已收到的文字保留在过程区（不丢字）
    assistant = _events(bus, "ASSISTANT")
    assert any("半句" in str(e.get("content") or "") for e in assistant), assistant
    error = _events(bus, "ERROR")
    assert error, "传输错误必须有可见 ERROR"


async def test_unexpected_transport_break_maps_to_provider_error_on_turn_end(provider):
    from agent.adapters.errors import ProviderError

    registry, _tool = _registry()
    async with _native_loop(
        provider, [{"chunks": ["半句"], "hard_abort": True}], registry
    ) as (loop, _bus):
        with pytest.raises(ProviderError) as excinfo:
            await loop.run("回答我")

    events: list[tuple[str, dict]] = []

    async def emitter(name: str, data: dict) -> None:
        events.append((name, data))

    async def runner(ctx) -> None:
        raise excinfo.value

    manager = TurnManager(runner=runner, emitter=emitter)
    ctx = manager.submit("回答我")
    await manager.wait(ctx.turn_id, timeout=5)
    await manager.shutdown()
    end = [data for name, data in events if name == "TURN_END"][-1]
    assert end["status"] == "failed"
    assert end["reason_code"] == "provider_error", end
    assert "retry" in end["actions"], end["actions"]


# ---- 4. 工具调用未结束：绝不执行未确认的调用 --------------------------------------


async def test_unfinished_tool_call_is_never_executed(provider):
    registry, tool = _registry()
    steps = [
        {
            "tool_chunks": [
                {"id": "call_unfinished", "name": "echo", "args_fragments": ['{"text": "hi"}']}
            ],
            "abort": True,
        }
    ]
    async with _native_loop(provider, steps, registry) as (loop, _bus):
        result = await loop.run("调用一次工具")

    assert tool.seen == [], ("未结束的工具调用绝不能执行", tool.seen)
    assert result.tool_calls_made == 0
    assert result.stop_reason_code == "incomplete_stream", result.stop_reason_code


# ---- 5. 合法长度截断：单独的原因代码 --------------------------------------------


async def test_length_truncation_is_reported_as_length_limit(provider):
    registry, _tool = _registry()
    async with _native_loop(
        provider, [{"chunks": [DECL + "\n", "被长度截断的正文"], "finish": "length"}], registry
    ) as (loop, bus):
        result = await loop.run("回答我")

    assert result.final_content == "被长度截断的正文", result.final_content
    assert result.stop_reason_code == "length_limit", result.stop_reason_code
    warnings = _events(bus, "WARNING")
    assert any(w["code"] == "length_limit" for w in warnings), warnings


async def test_anthropic_max_tokens_truncation_is_length_limit(provider):
    registry, _tool = _registry()
    async with _anthropic_loop(
        provider,
        [{"chunks": [DECL + "\n", "被长度截断的正文"], "finish": "max_tokens"}],
        registry,
    ) as (loop, _bus):
        result = await loop.run("回答我")

    assert result.final_content == "被长度截断的正文", result.final_content
    assert result.stop_reason_code == "length_limit", result.stop_reason_code
