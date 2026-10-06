"""真实流式（plan §2.1）：分类守卫、发布节奏、参数碎片、取消、错误、去重。

全部使用 fake provider / fake SSE 客户端，**不联网、不需要真实密钥**。
"""

from __future__ import annotations

import asyncio
import types
from typing import Any

import pytest

from agent.adapters.base import (
    STREAM_DONE,
    STREAM_TEXT,
    STREAM_TOOL_CALL,
    AdapterMode,
    BaseAdapter,
    ChatMessage,
    Completion,
    StreamDelta,
    ToolCall,
    ToolCallParseError,
    ToolSpec,
)
from agent.adapters.fake import FakeStreamAdapter, ScriptedToolCall, StreamScript
from agent.adapters.native import NativeAdapter
from agent.api.bus import EventBus, _Subscriber
from agent.api.events import EventType, make_event
from agent.core.loop import GUARD_MS, PUBLISH_CHARS, PUBLISH_MS, AgentLoop, _AssistantStream
from agent.tools.builtin import EchoTool
from agent.tools.registry import ToolRegistry

TOOLS = [
    ToolSpec(
        name="echo",
        description="Echoes text",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}},
    )
]


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(EchoTool())
    return registry


def _events(bus: EventBus, name: str) -> list[dict]:
    return [e.data for e in bus._history if e.type.value == name]


async def _wait_for(bus: EventBus, name: str, *, count: int = 1, timeout: float = 3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        found = _events(bus, name)
        if len(found) >= count:
            return found
        if loop.time() > deadline:
            kinds = [e.type.value for e in bus._history]
            raise AssertionError(f"等待 {name} 超时；已有事件：{kinds}")
        await asyncio.sleep(0.005)


# ---- adapter 契约 -----------------------------------------------------------


def test_base_adapter_does_not_support_streaming_by_default():
    class _Plain(BaseAdapter):
        mode = AdapterMode.TEXT

        def __init__(self) -> None:
            self.model = "plain"
            self.endpoint = None

        async def complete(self, messages, tools, *, temperature=None, max_tokens=None):
            return Completion(message=ChatMessage(role="assistant", content="hi"))

    adapter = _Plain()
    assert adapter.supports_stream is False

    async def _drain():
        async for _ in adapter.stream([], []):
            pass

    with pytest.raises(NotImplementedError):
        asyncio.run(_drain())


def test_text_adapter_declares_no_streaming():
    from agent.adapters.text import TextAdapter

    assert TextAdapter(client=object(), model="m").supports_stream is False


def test_native_and_anthropic_declare_streaming():
    from agent.adapters.anthropic import AnthropicAdapter

    assert NativeAdapter(client=object(), model="m").supports_stream is True
    adapter = AnthropicAdapter(api_key="k", model="m")
    assert adapter.supports_stream is True
    asyncio.run(adapter.close())


# ---- 分类守卫（单元）--------------------------------------------------------


def test_guard_constants_match_contract():
    assert GUARD_MS == 300
    assert PUBLISH_MS == 40
    assert PUBLISH_CHARS == 24


async def test_guard_window_expiry_classifies_answer():
    events: list[dict] = []
    clock = [100.0]

    async def emit(payload: dict) -> None:
        events.append(payload)

    stream = _AssistantStream(emit, delta_id="dl_x_1", guard_ms=300, clock=lambda: clock[0])
    await stream.note_text("你好")
    assert events == []  # 还在守卫缓冲里：分类没定就不出缓冲
    assert stream.next_deadline() == pytest.approx(100.3)
    clock[0] += 0.301
    await stream.on_deadline()
    assert len(events) == 1
    assert events[0] == {
        "content": "你好",
        "interim": False,
        "streaming": True,
        "delta_id": "dl_x_1",
        "seq": 1,
        # 正式回答不属于任何阶段（阶段只服务过程区）
        "stage_id": None,
        "call_ids": [],
    }
    # 定角色之后追加增量：直接进正式回答区，累计快照
    await stream.note_text("，世界")
    clock[0] += 0.05
    await stream.on_deadline()
    assert events[-1]["content"] == "你好，世界" and events[-1]["seq"] == 2


async def test_tool_call_delta_defers_until_stage_is_known():
    """工具轮的文字要等阶段就位后再发：否则会和 STAGE 说明并排成两个气泡。"""
    events: list[dict] = []

    async def emit(payload: dict) -> None:
        events.append(payload)

    stream = _AssistantStream(emit, delta_id="dl_x_1")
    await stream.note_text("我先读一下文件")
    await stream.note_tool_call()
    assert events == []  # 分类已定（interim），但还没有阶段信息 → 先不发
    # 也不该给发布定时器：到点也发不出去，只会让消费循环空转
    assert stream.next_deadline() is None
    await stream.finish(
        Completion(
            message=ChatMessage(
                role="assistant",
                content="我先读一下文件",
                tool_calls=[ToolCall(id="c1", name="echo", arguments={})],
            )
        ),
        defer_interim=True,
    )
    assert events == []
    await stream.flush_interim(stage_id="st_1_1", call_ids=["c1"])
    assert len(events) == 1
    assert events[0]["content"] == "我先读一下文件"
    assert events[0]["interim"] is True and events[0]["streaming"] is False
    assert events[0]["stage_id"] == "st_1_1" and events[0]["call_ids"] == ["c1"]
    # 没有正文的工具轮不发空气泡
    empty: list[dict] = []

    async def emit_empty(payload: dict) -> None:
        empty.append(payload)

    quiet = _AssistantStream(emit_empty, delta_id="dl_x_2")
    await quiet.note_tool_call()
    await quiet.finish(None, defer_interim=True)
    await quiet.flush_interim(stage_id="st_1_1", call_ids=["c9"])
    assert empty == []


async def test_late_tool_call_moves_text_from_answer_to_interim():
    events: list[dict] = []
    clock = [0.0]

    async def emit(payload: dict) -> None:
        events.append(payload)

    stream = _AssistantStream(emit, delta_id="dl_x_1", clock=lambda: clock[0])
    await stream.note_text("这是一段完整的话")
    await stream.note_text("，本来像正式回答。")
    clock[0] += 0.4
    await stream.on_deadline()
    assert events[-1]["interim"] is False
    # 唯一允许的改判：同一条 delta_id 的文字移到过程区，不重复、不撤回；
    # 改判事件等阶段就位后发出，带上同一个 stage_id。
    await stream.note_tool_call()
    await stream.flush_interim(stage_id="st_1_2", call_ids=["c1"])
    assert events[-1]["interim"] is True
    assert events[-1]["content"] == "这是一段完整的话，本来像正式回答。"
    assert events[-1]["delta_id"] == events[0]["delta_id"]
    assert events[-1]["stage_id"] == "st_1_2"
    assert all(e["delta_id"] == "dl_x_1" for e in events)


async def test_publish_cadence_merges_by_char_count():
    events: list[dict] = []

    async def emit(payload: dict) -> None:
        events.append(payload)

    clock = [0.0]
    stream = _AssistantStream(emit, delta_id="dl_x_1", clock=lambda: clock[0])
    # 先用守卫判成 answer（工具轮的文字是延后发布的，不参与节奏测试）
    await stream.note_text("开头")
    clock[0] += 0.4
    await stream.on_deadline()
    for _ in range(60):
        await stream.note_text("a")
    # 60 个单字符增量 → 按 ≥24 字符合并，绝不逐字符发
    assert len(events) == 3  # 1 条守卫放行 + 2 条按 24 字符合并
    assert [e["seq"] for e in events] == [1, 2, 3]
    assert events[-1]["content"] == "开头" + "a" * 48
    await stream.finish(None)
    assert events[-1]["streaming"] is False
    assert events[-1]["content"] == "开头" + "a" * 60
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(set(seqs))  # seq 单调、不重复、不回退


async def test_finish_uses_completion_text_when_no_delta_arrived():
    """供应商一次性给出整段（没有正文增量）时，不丢文字、也不假装是流式。"""
    events: list[dict] = []

    async def emit(payload: dict) -> None:
        events.append(payload)

    interim = Completion(
        message=ChatMessage(
            role="assistant",
            content="我先读一下文件。",
            tool_calls=[ToolCall(id="c1", name="echo", arguments={})],
        )
    )
    stream = _AssistantStream(emit, delta_id="dl_x_1")
    await stream.finish(interim)
    assert len(events) == 1
    assert events[0]["content"] == "我先读一下文件。"
    assert events[0]["interim"] is True and events[0]["streaming"] is False

    answer = Completion(message=ChatMessage(role="assistant", content="整段回答"))
    again: list[dict] = []

    async def emit2(payload: dict) -> None:
        again.append(payload)

    await _AssistantStream(emit2, delta_id="dl_x_2").finish(answer)
    assert len(again) == 1 and again[0]["content"] == "整段回答"
    assert again[0]["interim"] is False and again[0]["streaming"] is False


async def test_finish_without_any_text_publishes_nothing():
    events: list[dict] = []

    async def emit(payload: dict) -> None:
        events.append(payload)

    stream = _AssistantStream(emit, delta_id="dl_x_1")
    interim = Completion(
        message=ChatMessage(
            role="assistant", content=None, tool_calls=[ToolCall(id="c1", name="echo", arguments={})]
        )
    )
    await stream.finish(interim)
    assert events == []  # 没有正文就没有气泡，工具事实走 TOOL_*


# ---- native：真 SSE 增量与参数碎片组装 ---------------------------------------


def _ns(**kwargs: Any) -> Any:
    """属性在**实例**上的普通对象（SDK 的 pydantic 模型之外的一种真实形状）。"""
    return types.SimpleNamespace(**kwargs)


def _text_chunk(text: str) -> Any:
    delta = _ns(content=text, tool_calls=None)
    return _ns(choices=[_ns(delta=delta, finish_reason=None)], usage=None)


def _tc_chunk(
    index: int, call_id: str | None = None, name: str | None = None, arguments: str | None = None
) -> Any:
    function = _ns(name=name, arguments=arguments)
    fragment = _ns(index=index, id=call_id, function=function)
    delta = _ns(content=None, tool_calls=[fragment])
    return _ns(choices=[_ns(delta=delta, finish_reason=None)], usage=None)


def _finish_chunk(reason: str) -> Any:
    delta = _ns(content=None, tool_calls=None)
    return _ns(choices=[_ns(delta=delta, finish_reason=reason)], usage=None)


def _usage_chunk(inp: int, out: int) -> Any:
    usage = _ns(prompt_tokens=inp, completion_tokens=out, total_tokens=inp + out)
    return _ns(choices=[], usage=usage)


class _FakeOpenAIClient:
    """openai SDK 形状：chat.completions.create(stream=True) → 异步分片。"""

    def __init__(self, chunks: list[Any], *, reject_stream_options: bool = False) -> None:
        self.chunks = list(chunks)
        self.reject_stream_options = reject_stream_options
        self.requests: list[dict] = []

    @property
    def chat(self) -> "_FakeOpenAIClient":
        return self

    @property
    def completions(self) -> "_FakeOpenAIClient":
        return self

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if self.reject_stream_options and "stream_options" in kwargs:
            raise TypeError("unexpected keyword argument stream_options")
        chunks = self.chunks

        async def _iter():
            for chunk in chunks:
                yield chunk

        return _iter()


async def _deltas(adapter: NativeAdapter, messages=None, tools=None) -> list[StreamDelta]:
    out: list[StreamDelta] = []
    msgs = messages or [ChatMessage(role="user", content="hi")]
    async for delta in adapter.stream(msgs, tools or TOOLS):
        out.append(delta)
    return out


async def test_native_stream_yields_text_in_order_and_assembles_fragments():
    client = _FakeOpenAIClient(
        [
            _text_chunk("我先看"),
            _text_chunk("两个文件。"),
            _tc_chunk(0, call_id="call_1", name="read_file", arguments='{"pa'),
            _tc_chunk(0, arguments='th": "a.txt", "limit": '),
            _tc_chunk(0, arguments="50}"),
            _finish_chunk("tool_calls"),
            _usage_chunk(11, 7),
        ]
    )
    deltas = await _deltas(NativeAdapter(client=client, model="m1"))

    assert [d.text for d in deltas if d.kind == STREAM_TEXT] == ["我先看", "两个文件。"]
    tool_deltas = [d for d in deltas if d.kind == STREAM_TOOL_CALL]
    assert len(tool_deltas) == 3
    assert all(d.text == "" for d in tool_deltas)  # 碎片参数不当正文
    done = deltas[-1]
    assert done.kind == STREAM_DONE and done.completion is not None
    assert done.completion.message.content == "我先看两个文件。"
    calls = done.completion.tool_calls or []
    assert len(calls) == 1
    assert calls[0].id == "call_1" and calls[0].name == "read_file"
    assert calls[0].arguments == {"path": "a.txt", "limit": 50}
    assert done.completion.usage is not None and done.completion.usage.output_tokens == 7
    assert done.completion.finish_reason == "tool_calls"
    assert client.requests[0]["stream"] is True
    assert client.requests[0]["stream_options"] == {"include_usage": True}


async def test_native_stream_broken_tool_arguments_never_produce_a_call():
    client = _FakeOpenAIClient(
        [
            _tc_chunk(0, call_id="call_1", name="read_file", arguments='{"path": "a.txt"'),
            _finish_chunk("tool_calls"),
        ]
    )
    with pytest.raises(ToolCallParseError):
        await _deltas(NativeAdapter(client=client, model="m1"))


async def test_native_stream_falls_back_when_stream_options_rejected():
    client = _FakeOpenAIClient([_text_chunk("ok")], reject_stream_options=True)
    deltas = await _deltas(NativeAdapter(client=client, model="m1"))
    assert [d.text for d in deltas if d.kind == STREAM_TEXT] == ["ok"]
    assert len(client.requests) == 2  # 第一次被拒 → 去掉 stream_options 重试一次
    assert "stream_options" in client.requests[0]
    assert "stream_options" not in client.requests[1]
    assert deltas[-1].completion is not None and deltas[-1].completion.usage is None


# ---- anthropic：SSE 事件解析 -------------------------------------------------


class _FakeResponse:
    def __init__(self, lines: list[str], status_code: int = 200) -> None:
        self._lines = lines
        self.status_code = status_code

    async def aread(self) -> bytes:
        return b"bad request body"

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamContext:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeHttpxClient:
    """httpx.AsyncClient.stream 的形状（只有流式路径用到）。"""

    def __init__(self, lines: list[str], status_code: int = 200) -> None:
        self.lines = lines
        self.status_code = status_code
        self.payloads: list[dict] = []

    def stream(self, method: str, url: str, json: Any = None) -> _FakeStreamContext:
        self.payloads.append(json)
        return _FakeStreamContext(_FakeResponse(self.lines, self.status_code))

    async def aclose(self) -> None:
        return None


ANTHROPIC_SSE = [
    "event: message_start",
    'data: {"type":"message_start","message":{"usage":{"input_tokens":12},"stop_reason":null}}',
    "event: content_block_start",
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
    "event: content_block_delta",
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"先看"}}',
    "event: content_block_delta",
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"一下"}}',
    "event: content_block_start",
    'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_1","name":"read_file"}}',
    "event: content_block_delta",
    'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"path\\":"}}',
    "event: content_block_delta",
    'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"\\"a.txt\\"}"}}',
    "event: message_delta",
    'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":7}}',
    "event: message_stop",
    'data: {"type":"message_stop"}',
]


async def _anthropic_stream(lines: list[str], status_code: int = 200):
    from agent.adapters.anthropic import AnthropicAdapter

    adapter = AnthropicAdapter(api_key="k", model="m")
    fake = _FakeHttpxClient(lines, status_code)
    adapter._client = fake  # 只替换传输层：SSE 解析逻辑是真的
    deltas = []
    try:
        async for delta in adapter.stream([ChatMessage(role="user", content="hi")], TOOLS):
            deltas.append(delta)
    finally:
        await adapter.close()
    return deltas, fake


async def test_anthropic_stream_parses_sse_and_assembles_tool_input():
    deltas, fake = await _anthropic_stream(ANTHROPIC_SSE)
    assert [d.text for d in deltas if d.kind == STREAM_TEXT] == ["先看", "一下"]
    notices = [d for d in deltas if d.kind == STREAM_TOOL_CALL]
    assert notices and notices[0].call_id == "toolu_1" and notices[0].name == "read_file"
    done = deltas[-1]
    assert done.kind == STREAM_DONE and done.completion is not None
    assert done.completion.message.content == "先看一下"
    calls = done.completion.tool_calls or []
    assert len(calls) == 1 and calls[0].arguments == {"path": "a.txt"}
    assert done.completion.finish_reason == "tool_use"
    usage = done.completion.usage
    assert usage is not None and usage.input_tokens == 12 and usage.output_tokens == 7
    assert fake.payloads[0]["stream"] is True


async def test_anthropic_stream_reports_provider_error_event():
    from agent.adapters import errors as adapter_errors

    lines = [
        'data: {"type":"message_start","message":{"usage":{"input_tokens":3}}}',
        'data: {"type":"error","error":{"type":"overloaded_error","message":"服务器忙"}}',
    ]
    with pytest.raises(adapter_errors.ProviderInternalError):
        await _anthropic_stream(lines)


async def test_anthropic_stream_rejects_http_error_before_any_text():
    from agent.adapters import errors as adapter_errors

    with pytest.raises(adapter_errors.InvalidToolCall):
        await _anthropic_stream([], status_code=400)


# ---- AgentLoop：真流式端到端 -------------------------------------------------


async def test_answer_is_visible_before_provider_finishes():
    hold = asyncio.Event()
    adapter = FakeStreamAdapter(
        [StreamScript(text_chunks=["第一段回答", "第二段回答"], hold=hold, hold_after=1)]
    )
    bus = EventBus()
    loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
    task = asyncio.create_task(loop.run("hi"))
    try:
        published = await _wait_for(bus, "ASSISTANT", count=1)
        # provider 还没结束，前端已经拿到非空回答（验收第 3 条）
        assert not task.done()
        assert published[0]["content"] == "第一段回答"
        assert published[0]["streaming"] is True and published[0]["interim"] is False
        assert published[0]["delta_id"] == "dl_1_1" and published[0]["seq"] == 1
        assert adapter.requests[0]["stream"] is True
    finally:
        hold.set()
    result = await asyncio.wait_for(task, 3)
    published = _events(bus, "ASSISTANT")
    assert result.final_content == "第一段回答第二段回答"  # 全文只做校准，不追加
    assert published[-1]["streaming"] is False
    assert published[-1]["content"] == "第一段回答第二段回答"
    seqs = [e["seq"] for e in published]
    assert seqs == sorted(set(seqs))  # seq 单调、不重复、不回退
    assert all(e["delta_id"] == "dl_1_1" for e in published)


async def test_tool_round_keeps_interim_text_and_executes_assembled_arguments():
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                text="我先读一下文件。",
                tool_calls=[ScriptedToolCall(id="c1", name="echo", arguments={"text": "hi"})],
            ),
            StreamScript(text="完成"),
        ]
    )
    bus = EventBus()
    loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
    result = await loop.run("hi")

    assert result.tool_calls_made == 1 and result.final_content == "完成"
    assistant = _events(bus, "ASSISTANT")
    assert any(e["interim"] is True and e["content"] == "我先读一下文件。" for e in assistant)
    # 过程区文字带上了这一批的 call_ids，且排在 TOOL_START 之前（阶段/归属先就位）；
    # 同一段文字**只发一次**（延后发布不能与一次性补发重复）。
    kinds = [e.type.value for e in bus._history]
    interim = [e for e in assistant if e["interim"]]
    assert len(interim) == 1
    assert interim[0]["content"] == "我先读一下文件。"
    assert interim[0]["call_ids"] == ["c1"]
    assert all(not e["interim"] for e in assistant if e is not interim[0])
    assert kinds.index("ASSISTANT") < kinds.index("TOOL_START")
    # 参数碎片只在 adapter 内组装：执行时拿到的是合法 JSON，碎片从未作为正文出现
    starts = _events(bus, "TOOL_START")
    assert starts[0]["arguments"] == {"text": "hi"}
    assert starts[0]["stage_id"] is None  # 没有阶段提供者时如实为 null
    assert all(d["stage_id"] is None for d in _events(bus, "TOOL_END"))
    assert all('"text"' not in e["content"] for e in assistant)
    assert "tool_calls" not in " ".join(e["content"] for e in assistant)


async def test_stage_id_is_attached_to_tool_events_when_provider_exists():
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                text="先读文件。",
                tool_calls=[ScriptedToolCall(id="c1", name="echo", arguments={"text": "hi"})],
            ),
            StreamScript(text="完成"),
        ]
    )
    bus = EventBus()
    loop = AgentLoop(
        adapter, _registry(), bus, turn_id="turn_1", stage_id_provider=lambda: "st_1_1"
    )
    await loop.run("hi")
    assert _events(bus, "TOOL_START")[0]["stage_id"] == "st_1_1"
    assert _events(bus, "TOOL_END")[0]["stage_id"] == "st_1_1"
    # 流式过程区的文字与同批工具、同一条 STAGE 说明共用一个阶段标识
    interim = [e for e in _events(bus, "ASSISTANT") if e["interim"]]
    assert interim and interim[-1]["stage_id"] == "st_1_1"
    assert interim[-1]["call_ids"] == ["c1"]


async def test_cancel_mid_stream_keeps_confirmed_text():
    hold = asyncio.Event()
    adapter = FakeStreamAdapter(
        [StreamScript(text_chunks=["已经确认的一段"], hold=hold, hold_after=1)]
    )
    bus = EventBus()
    loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
    task = asyncio.create_task(loop.run("hi"))
    # 先在流还开着（provider 卡在 hold）时取消：取消必须能立刻中断这次等待
    await _wait_for(bus, "ASSISTANT", count=1)
    loop.cancel()
    result = await asyncio.wait_for(task, 3)
    hold.set()

    assert result.cancelled is True and result.phase.value == "stopped"
    published = _events(bus, "ASSISTANT")
    assert published[0]["content"] == "已经确认的一段"
    assert published[-1]["streaming"] is False  # 收尾快照：状态由 TURN_END 给出
    assert published[-1]["content"] == "已经确认的一段"
    assert result.final_content is None  # 取消不落成「正常回答」


async def test_stream_error_keeps_confirmed_text_and_emits_error():
    from agent.adapters import errors as adapter_errors

    adapter = FakeStreamAdapter(
        [
            StreamScript(
                text_chunks=["前半段"],
                error=adapter_errors.NetworkError("连接断了"),
                error_after=1,
            )
        ]
    )
    bus = EventBus()
    loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
    with pytest.raises(adapter_errors.NetworkError):
        await loop.run("hi")

    errors = _events(bus, "ERROR")
    assert errors and errors[0]["code"] == "planning_failed"
    published = _events(bus, "ASSISTANT")
    assert published and published[0]["content"] == "前半段"
    assert published[-1]["streaming"] is False


async def test_adapter_without_streaming_gets_one_shot_assistant():
    adapter = FakeStreamAdapter(
        [StreamScript(text="不支持流式也能回答")], stream_supported=False
    )
    bus = EventBus()
    loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
    result = await loop.run("hi")
    assert result.final_content == "不支持流式也能回答"
    published = _events(bus, "ASSISTANT")
    assert len(published) == 1
    assert published[0]["streaming"] is False and published[0]["interim"] is False
    assert published[0]["delta_id"] == "dl_1_1" and published[0]["seq"] == 1
    assert published[0]["stage_id"] is None and published[0]["call_ids"] == []
    assert adapter.requests[0]["stream"] is False  # 没有假装流式


class _LyingAdapter(BaseAdapter):
    """声明支持流式但没有实现：必须整段降级，而不是假装流式。"""

    mode = AdapterMode.NATIVE
    supports_stream = True

    def __init__(self) -> None:
        self.model = "liar"
        self.endpoint = None
        self.calls = 0

    async def complete(self, messages, tools, *, temperature=None, max_tokens=None):
        self.calls += 1
        return Completion(message=ChatMessage(role="assistant", content="整段回答"))

    async def stream(self, messages, tools, *, temperature=None, max_tokens=None):
        raise NotImplementedError("not implemented")
        yield  # pragma: no cover


async def test_adapter_claiming_streaming_but_not_implementing_it_degrades():
    adapter = _LyingAdapter()
    bus = EventBus()
    loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
    result = await loop.run("hi")

    assert result.final_content == "整段回答"
    assert adapter.calls == 1
    published = _events(bus, "ASSISTANT")
    assert len(published) == 1 and published[0]["streaming"] is False
    assert any("不支持实时生成" in w for w in result.warnings)


# ---- 去重与合并键 -----------------------------------------------------------


def test_bus_merge_key_includes_delta_id():
    subscriber = _Subscriber(32)

    def offer(delta_id: str | None, content: str) -> None:
        subscriber.offer(
            make_event(
                EventType.ASSISTANT,
                {"turn_id": "t1", "delta_id": delta_id, "content": content},
            )
        )

    offer("dl_a", "1")
    offer("dl_a", "12")
    offer("dl_b", "x")
    items = list(subscriber._items)
    assert [i.data["content"] for i in items] == ["12", "x"]  # 同一 delta 只留最新
    # 没有 delta_id 的累计事件（USAGE）保持旧的 (type, turn_id) 合并行为
    subscriber.offer(make_event(EventType.USAGE, {"turn_id": "t1", "output_tokens": 1}))
    subscriber.offer(make_event(EventType.USAGE, {"turn_id": "t1", "output_tokens": 2}))
    usages = [i for i in subscriber._items if i.type == EventType.USAGE]
    assert len(usages) == 1 and usages[0].data["output_tokens"] == 2


def test_stage_is_a_critical_event():
    from agent.api.bus import CRITICAL_EVENTS

    assert EventType.STAGE in CRITICAL_EVENTS
    assert EventType.ASSISTANT not in CRITICAL_EVENTS  # 仍是可合并事件


def test_assistant_delta_payload_shape():
    event = make_event(
        EventType.ASSISTANT,
        {
            "turn_id": "t1",
            "delta_id": "dl_x_1",
            "seq": 3,
            "content": "ab",
            "interim": False,
            "streaming": True,
        },
    )
    assert event.data["delta_id"] == "dl_x_1" and event.data["seq"] == 3