from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from agent.adapters.base import AdapterMode, ChatMessage, ToolCall, ToolSpec
from agent.adapters.native import NativeAdapter
from agent.api.events import EventType, make_event
from agent.api.server import EventBus
from agent.core.loop import AgentLoop
from agent.tools.builtin import EchoTool
from agent.tools.registry import ToolRegistry

TOOLS = [
    ToolSpec(
        name="echo",
        description="Echoes text",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}},
    )
]


@dataclass
class FakeMessage:
    content: str | None
    tool_calls: list[Any] | None = None


# 冻结协议串（第五轮契约 §1.1；测试里写死字面量：契约改了就应当红）
ANSWER_MARKER = "[[QIO:ANSWER]]"


@dataclass
class FakeChoice:
    message: FakeMessage


@dataclass
class FakeCompletion:
    choices: list[FakeChoice]
    usage: Any = None


def _tc(tool_id: str, name: str, arguments: str) -> Any:
    return type(
        "TC",
        (),
        {"id": tool_id, "function": type("F", (), {"name": name, "arguments": arguments})()},
    )()


def _chunk(
    content: str | None = None,
    tool_calls: list[Any] | None = None,
    finish_reason: str | None = None,
) -> Any:
    """OpenAI 兼容的流式分片形状（delta / finish_reason）。"""
    return type(
        "Chunk",
        (),
        {
            "choices": [
                type(
                    "Choice",
                    (),
                    {
                        "delta": type(
                            "Delta", (), {"content": content, "tool_calls": tool_calls}
                        )(),
                        "finish_reason": finish_reason,
                    },
                )()
            ],
            "usage": None,
        },
    )()


def _tc_delta(tc: Any) -> Any:
    return type(
        "TCDelta",
        (),
        {"index": 0, "id": tc.id, "function": tc.function},
    )()


async def _stream_of(completion: FakeCompletion):
    """把一条整段响应变成流式分片（真 provider 走的就是这条路径）。"""
    for choice in completion.choices:
        message = choice.message
        if message.content:
            yield _chunk(content=message.content)
        for tc in message.tool_calls or []:
            yield _chunk(tool_calls=[_tc_delta(tc)])
        yield _chunk(finish_reason="tool_calls" if message.tool_calls else "stop")


class ScriptedClient:
    """Plays a script of raw responses, then falls back to plain text.

    `stream=True` 时返回异步分片（与真实 OpenAI 兼容端点一致）：整段响应被切成
    「正文 → 工具调用 → 结束」三片，AgentLoop 的真流式分支才能被真正走到。
    """

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls = 0
        # 每次请求的 kwargs（只读记录）：断言「工作调用带工具 / 回答调用不带工具」
        self.requests: list[dict[str, Any]] = []

    @property
    def chat(self) -> "ScriptedClient":
        return self

    @property
    def completions(self) -> "ScriptedClient":
        return self

    def _next_completion(self) -> FakeCompletion:
        if self.script:
            return self.script.pop(0)
        return FakeCompletion([FakeChoice(FakeMessage("final answer", None))])

    async def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        self.requests.append(dict(kwargs))
        completion = self._next_completion()
        if kwargs.get("stream"):
            return _stream_of(completion)
        return completion


def _make_loop(client: ScriptedClient, **loop_kwargs: Any):
    adapter = NativeAdapter(client=client, model="m1")
    registry = ToolRegistry()
    registry.register(EchoTool())
    bus = EventBus()
    loop = AgentLoop(adapter, registry, bus, **loop_kwargs)
    return loop, bus


async def test_plain_text_turn():
    client = ScriptedClient([])
    loop, _ = _make_loop(client)
    result = await loop.run("hello")
    assert result.phase.value == "done"
    assert result.final_content == "final answer"
    assert result.tool_calls_made == 0


async def test_single_tool_turn():
    client = ScriptedClient(
        [
            FakeCompletion([FakeChoice(FakeMessage(None, [_tc("c1", "echo", '{"text": "hi"}')]))]),
            # 契约 §1.1（第五轮）变更：角色由正文声明决定 —— 工具轮之后声明回答
            FakeCompletion([FakeChoice(FakeMessage(ANSWER_MARKER + "\nfinal answer", None))]),
        ]
    )
    loop, _ = _make_loop(client)
    result = await loop.run("say hi")
    assert result.tool_calls_made == 1
    assert result.final_content == "final answer"
    # 工具轮（带工具、真的调用工具）+ 声明回答 = 2 次（不再固定 +1）
    assert client.calls == 2
    assert [bool(r.get("tools")) for r in client.requests] == [True, True]


async def test_budget_stops_runaway_loop():
    # 参数每次不同（结果也就不同）→ 不触发「无进展暂停」，验的是预算这条兜底
    client = ScriptedClient(
        [
            FakeCompletion([FakeChoice(FakeMessage(None, [_tc(f"c{i}", "echo", '{"text": "x%d"}' % i)]))])
            for i in range(50)
        ]
    )
    loop, _ = _make_loop(client, max_iterations=3)
    result = await loop.run("loop")
    assert result.phase.value == "stopped"
    assert result.iterations_used == 3


async def test_budget_stop_emits_warning_event():
    """预算耗尽停止时须发出 WARNING，前端不再静默无输出。"""
    client = ScriptedClient(
        [
            FakeCompletion([FakeChoice(FakeMessage(None, [_tc(f"c{i}", "echo", '{"text": "x%d"}' % i)]))])
            for i in range(50)
        ]
    )
    loop, bus = _make_loop(client, max_iterations=3)

    collected: list[str] = []

    async def consumer():
        async for chunk in bus.stream():
            collected.append(chunk)

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.05)
    await loop.run("loop")
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    joined = "\n".join(collected)
    assert "event: WARNING" in joined
    assert "budget" in joined or "迭代" in joined or "token" in joined


async def test_force_continue_overrides_budget():
    client = ScriptedClient(
        [
            FakeCompletion([FakeChoice(FakeMessage(None, [_tc(f"c{i}", "echo", '{"text": "x%d"}' % i)]))])
            for i in range(50)
        ]
    )
    loop, _ = _make_loop(client, max_iterations=3, force_continue=True)
    result = await loop.run("loop")
    assert result.phase.value == "done"  # ran past the budget to completion


async def test_unknown_tool_failure_isolated_and_warns():
    client = ScriptedClient(
        [
            FakeCompletion([FakeChoice(FakeMessage(None, [_tc("c1", "ghost", "{}")]))]),
            # 声明回答：这一轮的警告只应该有一条（工具失败），不带协议降级的噪声
            FakeCompletion([FakeChoice(FakeMessage(ANSWER_MARKER + "\nfinal answer", None))]),
        ]
    )
    loop, _ = _make_loop(client)
    result = await loop.run("call ghost")
    assert result.tool_calls_made == 1
    assert len(result.warnings) == 1
    assert "ghost" in result.warnings[0]
    assert result.phase.value == "done"


async def test_turn_events_emitted():
    client = ScriptedClient([])
    loop, bus = _make_loop(client)

    collected: list[str] = []

    async def consumer():
        async for chunk in bus.stream():
            collected.append(chunk)

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.05)
    await loop.run("hello")
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    joined = "\n".join(collected)
    # turn 生命周期事件已上移：本循环只发 USAGE / WARNING / ERROR / TOOL_* / ASSISTANT，
    # TURN_START / TURN_END 由 core/turn.py 的 TurnManager 单独负责（含子 agent 隔离）。
    assert "TURN_START" not in joined
    assert "TURN_END" not in joined
    assert "USAGE" in joined
async def test_interim_assistant_event_emitted_for_native_commentary():
    client = ScriptedClient(
        [FakeCompletion([FakeChoice(FakeMessage("我先查一下仓库", [_tc("c1", "echo", '{"text": "hi"}')]))])]
    )
    loop, bus = _make_loop(client)

    collected: list[str] = []

    async def consumer():
        async for chunk in bus.stream():
            collected.append(chunk)

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.05)
    await loop.run("查一下仓库")
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    joined = "\n".join(collected)
    assert "event: ASSISTANT" in joined
    assert "我先查一下仓库" in joined
    assert "\"interim\":true" in joined or "\"interim\": true" in joined
    # interim 文本先到，收尾的 USAGE 后到；TURN_END 不再由本循环发出
    assert joined.index("我先查一下仓库") < joined.index("USAGE")


async def test_plain_text_turn_streams_answer_in_the_answer_area():
    """契约 §1.1：声明的正文从第一个可发布增量起实时进正式回答区（标记不展示）。"""
    client = ScriptedClient(
        [FakeCompletion([FakeChoice(FakeMessage(ANSWER_MARKER + "\nfinal answer", None))])]
    )
    loop, bus = _make_loop(client)

    collected: list[str] = []

    async def consumer():
        async for chunk in bus.stream():
            collected.append(chunk)

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.05)
    await loop.run("hello")
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    joined = "\n".join(collected)
    # 声明回答：正文进正式回答区（interim=false），声明本身不展示
    assert "event: ASSISTANT" in joined
    assert "final answer" in joined
    assert "\"interim\": false" in joined or "\"interim\":false" in joined
    assert "\"interim\": true" not in joined and "\"interim\":true" not in joined
    assert "[[QIO:ANSWER]]" not in joined
    # 合规直接回答只需 1 次调用（不再有固定的回答调用）
    assert client.calls == 1
    assert client.requests[0].get("tools")  # 角色由声明决定，不看带不带工具
