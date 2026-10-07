"""真实流式（第四轮契约 §1.1）：回答阶段协议、发布节奏、参数碎片、取消、错误、去重。

角色判据**只有**「这次调用带不带工具」：

* `tools=[...]` = 工作调用 → 正文是进度说明，进过程区（interim=true）；
* `tools=[]` = 回答调用 → 正文从**第一个可发布增量**起就是正式回答
  （interim=false、streaming=true），结束时再补一条同 delta_id 的
  `{streaming:false}` 累计快照做校准（**校准不是首次展示来源**）。

全部使用 fake provider / fake SSE 客户端，**不联网、不需要真实密钥**。
"""

from __future__ import annotations

import asyncio
import json
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
from agent.core.loop import PUBLISH_CHARS, PUBLISH_MS, AgentLoop, _AssistantStream
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


# ---- 发布节奏与角色边界（单元，plan §1.1）------------------------------------


def test_publish_cadence_constants_match_contract():
    assert PUBLISH_MS == 40
    assert PUBLISH_CHARS == 24


async def test_undeclared_text_is_held_until_the_call_ends():
    """未声明（无标记）的正文先不展示：有界缓冲 → 无工具调用 → 一次性交付（降级）。

    第五轮契约 §1.1 第 3 条：未声明的正文不能猜角色（猜成过程说明就会出现
    「过程区一份 + 回答区再生成一份」），先缓冲，调用结束再按工具调用有无落地。
    """
    events: list[dict] = []
    clock = [100.0]

    async def emit(payload: dict) -> None:
        events.append(payload)

    stream = _AssistantStream(emit, delta_id="dl_x_1", clock=lambda: clock[0])
    await stream.note_text("这是一段还没有声明的文字")
    assert events == []  # 未声明：先不展示
    assert stream.next_deadline() is None
    clock[0] += 0.05
    await stream.on_deadline()
    assert events == []
    await stream.finish(
        Completion(message=ChatMessage(role="assistant", content="这是一段还没有声明的文字"))
    )
    assert len(events) == 1
    assert events[0] == {
        "content": "这是一段还没有声明的文字",
        "interim": False,
        "streaming": False,
        "delta_id": "dl_x_1",
        "seq": 1,
        "stage_id": None,
        "call_ids": [],
        "role_evidence": "undeclared_answer",
    }
    assert stream.answer_text == "这是一段还没有声明的文字"
    assert stream.undeclared_answer_used is True


async def test_work_call_text_stays_in_process_area_and_is_never_promoted():
    """工作调用（出现工具调用）的正文按事实留在过程区：不提升、不搬动、不丢字。

    工具调用是「这条响应是工具轮」的直接证据：缓冲中的正文此刻按进度说明实时
    放行；调用结束时它留在过程区，等 flush_interim 补阶段信息。正式回答只能由
    **声明过的**正文产出（见 test_declared_answer_streams_...）。
    """
    events: list[dict] = []
    clock = [0.0]

    async def emit(payload: dict) -> None:
        events.append(payload)

    stream = _AssistantStream(emit, delta_id="dl_x_1", clock=lambda: clock[0])
    await stream.note_text("第一句。")
    assert events == []  # 还没出现工具调用：先缓冲（不猜角色）
    await stream.note_tool_call()  # 工具调用 → 工作调用 → 实时进过程区
    assert len(events) == 1
    assert events[0]["interim"] is True and events[0]["streaming"] is True
    assert events[0]["content"] == "第一句。"

    # 工作调用结束（这次请求了工具）：文字留在过程区，等阶段信息
    await stream.finish(
        Completion(
            message=ChatMessage(
                role="assistant",
                content="第一句。",
                tool_calls=[ToolCall(id="c1", name="echo", arguments={})],
            )
        )
    )
    assert len(events) == 1
    await stream.flush_interim(stage_id=None, call_ids=["c1"])
    closed = events[-1]
    assert closed["interim"] is True  # 留在过程区：不搬动、不删除
    assert closed["streaming"] is False  # 收尾（不再增长）
    assert closed["content"] == "第一句。"
    assert closed["delta_id"] == events[0]["delta_id"]
    assert closed["seq"] == events[0]["seq"] + 1
    assert closed["role_evidence"] is None  # 工作调用没有任何角色证据
    assert closed["stage_id"] is None and closed["call_ids"] == ["c1"]
    # 整个过程没有任何一条事件进过正式回答区，工作正文也没有变成回答
    assert all(e["interim"] for e in events)
    assert stream.answer_text == ""


async def test_answer_stream_publishes_answer_area_events_from_the_first_increment():
    """声明回答：标记不展示，正文从第一个可发布增量起实时进正式回答区。"""
    events: list[dict] = []
    clock = [0.0]

    async def emit(payload: dict) -> None:
        events.append(payload)

    stream = _AssistantStream(emit, delta_id="dl_x_2", clock=lambda: clock[0])
    await stream.note_text(ANSWER_MARKER + "\n第一句。")
    clock[0] += 0.05
    await stream.on_deadline()
    assert len(events) == 1
    assert events[-1]["interim"] is False and events[-1]["streaming"] is True
    assert events[-1]["content"] == "第一句。"  # 声明本身不展示
    assert events[-1]["role_evidence"] == "declared_answer"
    await stream.note_text("第二句。")
    await stream.finish(
        Completion(
            message=ChatMessage(role="assistant", content=ANSWER_MARKER + "\n第一句。第二句。")
        )
    )
    # 结束时的收尾校准：同一 delta_id、同一份累计文字
    assert events[-1]["interim"] is False and events[-1]["streaming"] is False
    assert events[-1]["content"] == "第一句。第二句。"
    assert events[-1]["delta_id"] == "dl_x_2"
    assert all(e["interim"] is False for e in events)
    assert stream.answer_text == "第一句。第二句。"


async def test_tool_round_text_stays_in_process_area_and_gets_stage_later():
    """工具轮：文字先实时进过程区，阶段就位后补 stage_id —— 永不移走、不重复。"""
    events: list[dict] = []
    clock = [0.0]

    async def emit(payload: dict) -> None:
        events.append(payload)

    stream = _AssistantStream(emit, delta_id="dl_x_1", clock=lambda: clock[0])
    await stream.note_text("这是一段完整的话，本来像正式回答。")
    assert events == []  # 未声明：先缓冲，绝不先当正式回答再移走

    # 正文之后才出现的工具调用：这条响应是工具轮 → 缓冲正文按进度说明实时放行
    await stream.note_tool_call()
    assert events[-1]["interim"] is True
    assert events[-1]["content"] == "这是一段完整的话，本来像正式回答。"
    assert all(e["interim"] is True for e in events)

    await stream.finish(
        Completion(
            message=ChatMessage(
                role="assistant",
                content="这是一段完整的话，本来像正式回答。",
                tool_calls=[ToolCall(id="c1", name="echo", arguments={})],
            )
        )
    )
    assert len(events) == 1  # 阶段还没就位：等 flush_interim 带阶段信息发
    await stream.flush_interim(stage_id="st_1_2", call_ids=["c1"])
    assert len(events) == 2
    assert events[-1]["interim"] is True and events[-1]["streaming"] is False
    assert events[-1]["content"] == events[0]["content"]  # 同一份文字：不重复、不改写
    assert events[-1]["delta_id"] == events[0]["delta_id"]
    assert events[-1]["stage_id"] == "st_1_2" and events[-1]["call_ids"] == ["c1"]
    assert events[-1]["role_evidence"] is None
    assert {e["delta_id"] for e in events} == {"dl_x_1"}
    assert [e["seq"] for e in events] == [1, 2]


async def test_tool_call_delta_before_any_text_classifies_as_process():
    """工具调用先于正文到达：这条响应从第一个正文增量起就是过程说明。"""
    events: list[dict] = []

    async def emit(payload: dict) -> None:
        events.append(payload)

    stream = _AssistantStream(emit, delta_id="dl_x_1")
    await stream.note_tool_call()
    await stream.note_text("我先读一下文件，然后再把结论整理出来，最后给你答复。")
    assert len(events) == 1
    assert events[0]["interim"] is True and events[0]["streaming"] is True


async def test_tool_round_without_text_publishes_nothing():
    """没有正文的工具轮不发空气泡（工具事实走 TOOL_*）。"""
    events: list[dict] = []

    async def emit(payload: dict) -> None:
        events.append(payload)

    stream = _AssistantStream(emit, delta_id="dl_x_2")
    await stream.note_tool_call()
    await stream.finish(None)
    await stream.flush_interim(stage_id="st_1_1", call_ids=["c9"])
    assert events == []


async def test_answer_from_start_streams_into_answer_area():
    """不带工具的显式回答调用：正文从第一个增量起就是正式回答（plan §1.1 第 4 条）。"""
    events: list[dict] = []
    clock = [0.0]

    async def emit(payload: dict) -> None:
        events.append(payload)

    stream = _AssistantStream(emit, delta_id="dl_x_2", clock=lambda: clock[0])
    await stream.note_text(ANSWER_MARKER + "\n直接回答你。")
    clock[0] += 0.05
    await stream.on_deadline()
    assert events[-1]["interim"] is False and events[-1]["streaming"] is True
    assert events[-1]["role_evidence"] == "declared_answer"
    await stream.finish(
        Completion(
            message=ChatMessage(role="assistant", content=ANSWER_MARKER + "\n直接回答你。")
        )
    )
    assert events[-1]["interim"] is False and events[-1]["streaming"] is False
    assert events[-1]["content"] == "直接回答你。"
    assert events[-1]["role_evidence"] == "declared_answer"
    assert {e["interim"] for e in events} == {False}
    assert all(ANSWER_MARKER not in e["content"] for e in events)


async def test_publish_cadence_merges_by_char_count():
    events: list[dict] = []

    async def emit(payload: dict) -> None:
        events.append(payload)

    clock = [0.0]
    stream = _AssistantStream(emit, delta_id="dl_x_1", clock=lambda: clock[0])
    # 声明回答：标记本身不展示，声明之后的正文按发布节奏合并
    await stream.note_text(ANSWER_MARKER + "\n开头")
    for _ in range(60):
        await stream.note_text("a")
    # 62 个字符 → 按 ≥24 字符合并，绝不逐字符发
    assert len(events) == 2
    assert [e["seq"] for e in events] == [1, 2]
    assert events[-1]["content"] == "开头" + "a" * 46
    await stream.finish(None)
    assert events[-1]["streaming"] is False
    assert events[-1]["content"] == "开头" + "a" * 60
    # 声明回答 → 全部进正式回答区；声明本身不出现
    assert all(e["interim"] is False for e in events)
    assert all(e["role_evidence"] == "declared_answer" for e in events)
    assert all(ANSWER_MARKER not in e["content"] for e in events)
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
    # 工具轮的正文等**阶段就位**后交付：没有阶段就没有归位信息，先不发
    assert events == []
    await stream.flush_interim(stage_id=None, call_ids=["c1"])
    assert len(events) == 1
    assert events[0]["content"] == "我先读一下文件。"
    assert events[0]["interim"] is True and events[0]["streaming"] is False
    assert events[0]["call_ids"] == ["c1"]
    # 工具轮的正文不是正式回答：没有任何角色证据
    assert events[0]["role_evidence"] is None

    # 声明回答、整段返回：正文是正式回答（标记不展示）
    answer = Completion(
        message=ChatMessage(role="assistant", content=ANSWER_MARKER + "\n整段回答")
    )
    again: list[dict] = []

    async def emit2(payload: dict) -> None:
        again.append(payload)

    await _AssistantStream(emit2, delta_id="dl_x_2").finish(answer)
    assert len(again) == 1 and again[0]["content"] == "整段回答"
    assert again[0]["interim"] is False and again[0]["streaming"] is False
    assert again[0]["role_evidence"] == "declared_answer"

    # 未声明、整段返回、且没有工具调用：正式回答（降级路径，一次性交付）
    undeclared = Completion(message=ChatMessage(role="assistant", content="整段回答"))
    work: list[dict] = []

    async def emit3(payload: dict) -> None:
        work.append(payload)

    await _AssistantStream(emit3, delta_id="dl_x_3").finish(undeclared)
    assert len(work) == 1 and work[0]["content"] == "整段回答"
    assert work[0]["interim"] is False and work[0]["streaming"] is False
    assert work[0]["role_evidence"] == "undeclared_answer"


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


async def test_answer_after_a_tool_round_streams_live_from_the_first_increment():
    """工具轮之后的回答：正文声明 → 从**第一个可发布增量**起实时进正式回答区。

    第五轮契约 §1.1：角色只看正文声明，不看这次调用带不带工具 —— 工具列表非空时
    同样成立。声明之后的第一段回答**早于这次调用结束**就出现在回答区（真流式）。
    """
    hold = asyncio.Event()
    adapter = FakeStreamAdapter(
        [
            # ① 工作轮：带工具、真的调用了工具（正文是过程说明）
            StreamScript(
                text="我先看一下。",
                tool_calls=[ScriptedToolCall(id="c1", name="echo", arguments={"text": "hi"})],
            ),
            # ② 声明回答：真流式；第一段之后**暂停**（provider 还没结束）
            StreamScript(
                text_chunks=[ANSWER_MARKER + "\n", "第一段回答", "第二段回答"],
                hold=hold,
                hold_after=2,
            ),
        ]
    )
    bus = EventBus()
    loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
    task = asyncio.create_task(loop.run("hi"))

    early: dict | None = None
    deadline = asyncio.get_running_loop().time() + 5
    while not task.done() and asyncio.get_running_loop().time() < deadline:
        for payload in _events(bus, "ASSISTANT"):
            if payload["interim"] is False and payload["content"].strip():
                early = payload
                break
        if early is not None:
            break
        await asyncio.sleep(0.005)

    try:
        assert early is not None, "正式回答必须在 provider 结束前就出现在回答区"
        assert not task.done(), "不能等整段响应结束才展示正式回答"
        assert early["content"] == "第一段回答"  # 声明本身不展示
        assert early["streaming"] is True and early["interim"] is False
        assert early["delta_id"] == "dl_1_2"  # 第二次调用
        assert early["role_evidence"] == "declared_answer"
        # 成本：工具轮 + 声明回答，一共 2 次调用；两次都带全套工具（角色由声明决定）
        assert len(adapter.requests) == 2
        assert [t.name for t in adapter.requests[0]["tools"]] != []
        assert [t.name for t in adapter.requests[1]["tools"]] != []
        # 工作轮的正文按事实留在过程区：不搬动、不删除、也不会出现在回答区
        work_notes = [e for e in _events(bus, "ASSISTANT") if e["interim"]]
        assert work_notes and work_notes[0]["content"] == "我先看一下。"
        assert all("我先看一下。" not in (e["content"] or "") for e in _events(bus, "ASSISTANT") if not e["interim"])
    finally:
        hold.set()

    result = await asyncio.wait_for(task, 3)
    assert result.final_content == "第一段回答第二段回答"
    finals = [e for e in _events(bus, "ASSISTANT") if e["interim"] is False]
    # 回答调用结束时的收尾校准：同一 delta_id、同一份累计文字、streaming=false
    assert finals[-1]["streaming"] is False
    assert finals[-1]["content"] == "第一段回答第二段回答"
    assert finals[-1]["delta_id"] == "dl_1_2"
    assert all(e["delta_id"] == "dl_1_2" for e in finals)
    contents = [e["content"] for e in finals]
    for earlier, later in zip(contents, contents[1:]):
        assert later.startswith(earlier), "累计快照不得回退/重打"
    seqs = [e["seq"] for e in finals]
    assert seqs == sorted(set(seqs))  # seq 单调、不重复、不回退
    # 回答调用结束前已经有正式回答内容（校准不是首次展示来源）
    assert finals[0]["streaming"] is True


async def test_tool_round_keeps_interim_text_and_executes_assembled_arguments():
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                text="我先读一下文件。",
                tool_calls=[ScriptedToolCall(id="c1", name="echo", arguments={"text": "hi"})],
            ),
            # 声明回答：正式回答（声明本身不展示）
            StreamScript(text=ANSWER_MARKER + "\n完成"),
        ]
    )
    bus = EventBus()
    loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
    result = await loop.run("hi")

    assert result.tool_calls_made == 1 and result.final_content == "完成"
    # 成本如实（第五轮 §1.1）：工具轮 + 声明回答，一共 2 次调用（不再固定 +1）
    assert len(adapter.requests) == 2
    assert [t.name for t in adapter.requests[1]["tools"]] != []  # 角色由声明决定
    assistant = _events(bus, "ASSISTANT")
    assert any(e["interim"] is True and e["content"] == "我先读一下文件。" for e in assistant)
    # 过程区文字带上了这一批的 call_ids，且排在 TOOL_START 之前（阶段/归属先就位）；
    # 工具轮的正文**永不**出现在正式回答区（审计问题 2）。
    kinds = [e.type.value for e in bus._history]
    interim = [e for e in assistant if e["interim"]]
    # 过程区两条：工具调用出现时的实时放行 + 阶段就位后的累计快照（同一 delta_id）
    assert len(interim) == 2
    assert all(e["content"] == "我先读一下文件。" for e in interim)
    assert interim[0]["streaming"] is True and interim[-1]["streaming"] is False
    assert interim[-1]["call_ids"] == ["c1"]
    assert {e["delta_id"] for e in interim} == {"dl_1_1"}
    assert all(not e["interim"] for e in assistant if e not in interim)
    assert not any(
        e["content"] == "我先读一下文件。" and not e["interim"] for e in assistant
    )
    assert kinds.index("ASSISTANT") < kinds.index("TOOL_START")
    # 参数碎片只在 adapter 内组装：执行时拿到的是合法 JSON，碎片从未作为正文出现
    starts = _events(bus, "TOOL_START")
    assert starts[0]["arguments"] == {"text": "hi"}
    assert starts[0]["stage_id"] is None  # 没有阶段提供者时如实为 null
    assert all(d["stage_id"] is None for d in _events(bus, "TOOL_END"))
    assert all('"text"' not in e["content"] for e in assistant)
    assert "tool_calls" not in " ".join(e["content"] for e in assistant)


async def test_one_second_late_tool_call_never_shows_answer_then_moves_it():
    """审计问题 2 的原样复现：正文先到，工具调用 1 秒后才到。

    旧行为：守卫窗口（300ms）到期就把正文当正式回答显示，工具调用到达后再移回过程区。
    新契约：没有任何文字从答案区移走 —— 一个字都不许先进答案区。
    """
    said = "这是一段完整的说明，本来会先当答案展示。"
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                text_chunks=["这是一段完整的说明", "，本来会先当答案展示。"],
                tool_calls=[ScriptedToolCall(id="c1", name="echo", arguments={"text": "hi"})],
                gap_ms=1000,
            ),
            StreamScript(text=ANSWER_MARKER + "\n完成"),  # 声明回答
        ]
    )
    bus = EventBus()
    loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
    task = asyncio.create_task(loop.run("hi"))
    # 等第一段正文出现：这一刻工具调用还没到（旧代码会在这里把它当正式回答）
    await _wait_for(bus, "ASSISTANT", count=1)
    first = _events(bus, "ASSISTANT")[0]
    assert first["interim"] is True
    assert first["streaming"] is True
    result = await asyncio.wait_for(task, 10)

    assert result.final_content == "完成"
    assistant = _events(bus, "ASSISTANT")
    tool_round = [e for e in assistant if e["delta_id"] == "dl_1_1"]
    assert tool_round
    # 工具轮的文字**全程**只在过程区（没有任何一条 interim=false）
    assert all(e["interim"] is True for e in tool_round), tool_round
    assert tool_round[-1]["content"] == said
    assert tool_round[-1]["call_ids"] == ["c1"]
    assert tool_round[-1]["streaming"] is False  # 收尾快照
    # 正式回答只属于**声明过的那次调用**；工具轮的正文一个字都没进回答区。
    # 声明回答会发两条：未到发布阈值的增量先补一条 streaming=true，再一条收尾校准。
    answers = [e for e in assistant if not e["interim"]]
    assert {e["delta_id"] for e in answers} == {"dl_1_2"}
    assert answers[0]["streaming"] is True and answers[-1]["streaming"] is False
    assert all(said not in (e["content"] or "") for e in answers)


async def test_anthropic_path_follows_the_same_role_rules():
    """Anthropic SSE → AgentLoop：同一套内容角色协议（声明 → 回答区，标记不展示）。"""
    from agent.adapters.anthropic import AnthropicAdapter

    lines = [
        'data: {"type":"message_start","message":{"usage":{"input_tokens":12}}}',
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta",'
        '"text":"[[QIO:ANSWER]]\\n先看"}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"一下"}}',
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":7}}',
        'data: {"type":"message_stop"}',
    ]
    adapter = AnthropicAdapter(api_key="k", model="m")
    fake = _FakeHttpxClient(lines)
    adapter._client = fake
    bus = EventBus()
    loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
    try:
        result = await loop.run("hi")
    finally:
        await adapter.close()

    assert result.final_content == "先看一下"  # 声明本身不展示
    assistant = _events(bus, "ASSISTANT")
    assert assistant
    # 声明回答：正文从第一段起就进正式回答区，结束再补一条收尾校准
    answer = [e for e in assistant if e["delta_id"] == "dl_1_1"]
    assert answer and all(e["interim"] is False for e in answer)
    assert answer[0]["streaming"] is True
    assert answer[-1]["streaming"] is False
    assert answer[-1]["content"] == "先看一下"
    assert answer[-1]["role_evidence"] == "declared_answer"
    assert all("[[QIO:ANSWER]]" not in e["content"] for e in answer)
    # 成本如实：合规直接回答只需 1 次调用（不再固定 +1）
    assert len(fake.payloads) == 1
    assert fake.payloads[0]["tools"]


# ---- 回答阶段：工作调用不再请求工具 → 一次 tools=[] 的回答调用（§1.1）--------------


async def test_tool_phase_without_text_gets_one_fallback_call():
    """整轮完全没有回答内容 → 最多一次兜底调用（tools=[]）要求最终回答。"""
    adapter = FakeStreamAdapter(
        [
            # 1) 工具轮：一个字都没说，只调用工具
            StreamScript(
                tool_calls=[ScriptedToolCall(id="c1", name="echo", arguments={"text": "hi"})]
            ),
            # 2) 工具阶段收尾的调用：同样没有正文
            StreamScript(text=""),
            # 3) 兜底调用（tools=[]）：模型声明了回答
            StreamScript(text=ANSWER_MARKER + "\n这是正式回答"),
        ]
    )
    bus = EventBus()
    loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
    result = await loop.run("hi")

    assert result.tool_calls_made == 1
    assert result.final_content == "这是正式回答"
    # 唯一允许的额外调用：3 次模型调用，最后一次**不带工具**
    assert len(adapter.requests) == 3
    assert [bool(r["tools"]) for r in adapter.requests] == [True, True, False]
    assert result.iterations_used == 3  # 兜底调用如实计入用量
    answers = [e for e in _events(bus, "ASSISTANT") if not e["interim"]]
    assert answers and answers[-1]["content"] == "这是正式回答"
    assert answers[-1]["delta_id"] == "dl_1_3"
    assert all(a["role_evidence"] == "declared_answer" for a in answers)
    # 前两次调用一个字都没有 → 不伪造过程区气泡；声明回答发两条（增量 + 收尾校准）
    published = _events(bus, "ASSISTANT")
    assert [e["content"] for e in published] == ["这是正式回答", "这是正式回答"]
    assert published[0]["streaming"] is True and published[-1]["streaming"] is False


async def test_tool_free_answer_call_happens_at_most_once_per_turn():
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                tool_calls=[ScriptedToolCall(id="c1", name="echo", arguments={"text": "hi"})]
            ),
            StreamScript(text=""),
            StreamScript(text=""),  # 补的那次也没有正文 → 不再补第二次
        ]
    )
    bus = EventBus()
    result = await AgentLoop(adapter, _registry(), bus, turn_id="turn_1").run("hi")
    assert len(adapter.requests) == 3
    # 一个字都没有就如实说明，不编一个回答
    assert "没有产生回答" in (result.final_content or "")


async def test_work_call_without_text_still_gets_the_answer_call():
    """常规路径：工作调用没有正文、也没有请求工具 → 仍然要发起回答调用。"""
    adapter = FakeStreamAdapter([StreamScript(text=""), StreamScript(text="")])
    result = await AgentLoop(adapter, _registry(), EventBus(), turn_id="turn_1").run("hi")
    assert len(adapter.requests) == 2
    assert [bool(r["tools"]) for r in adapter.requests] == [True, False]
    # 一个字都没有就如实说明，不编一个回答
    assert "没有产生回答" in (result.final_content or "")


async def test_loop_without_any_tool_gets_a_single_call():
    """没有任何可用工具时同样只调用一次：未声明的正文就是本轮回答（降级路径）。"""
    adapter = FakeStreamAdapter([StreamScript(text="直接回答")])
    bus = EventBus()
    result = await AgentLoop(adapter, ToolRegistry(), bus, turn_id="turn_1").run("hi")
    assert len(adapter.requests) == 1
    assert [r["tools"] for r in adapter.requests] == [[]]
    assert result.final_content == "直接回答"
    # 未声明 → 一次性交付到正式回答区（不冒充流式），不重新生成
    published = _events(bus, "ASSISTANT")
    assert len(published) == 1
    assert published[0]["interim"] is False and published[0]["streaming"] is False
    assert published[0]["content"] == "直接回答"
    assert published[0]["role_evidence"] == "undeclared_answer"


async def test_stage_id_is_attached_to_tool_events_when_provider_exists():
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                text="先读文件。",
                tool_calls=[ScriptedToolCall(id="c1", name="echo", arguments={"text": "hi"})],
            ),
            StreamScript(text=""),  # 工作调用收尾：没有请求工具
            StreamScript(text="完成"),  # 回答调用
        ]
    )
    bus = EventBus()
    loop = AgentLoop(
        adapter, _registry(), bus, turn_id="turn_1", stage_id_provider=lambda: "st_1_1"
    )
    await loop.run("hi")
    assert _events(bus, "TOOL_START")[0]["stage_id"] == "st_1_1"
    assert _events(bus, "TOOL_END")[0]["stage_id"] == "st_1_1"
    # 工作调用的正文与同批工具、同一条 STAGE 说明共用一个阶段标识
    interim = [e for e in _events(bus, "ASSISTANT") if e["interim"]]
    tool_round = [e for e in interim if e["delta_id"] == "dl_1_1"]
    assert tool_round and tool_round[-1]["stage_id"] == "st_1_1"
    assert tool_round[-1]["call_ids"] == ["c1"]
    # 正式回答属于回答调用，不进任何阶段
    answer = [e for e in _events(bus, "ASSISTANT") if not e["interim"]]
    assert answer and all(e["stage_id"] is None for e in answer)


async def test_cancel_mid_stream_keeps_confirmed_answer_text():
    """取消时已经显示的**正式回答**文字不丢、不回退（声明回答的真流式路径）。"""
    hold = asyncio.Event()
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                text_chunks=[ANSWER_MARKER + "\n已经确认的一段"],
                hold=hold,
                hold_after=1,
            )
        ]
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
    assert published[0]["content"] == "已经确认的一段"  # 声明不展示
    # 已经进入正式回答区的文字保留在原地：不搬动、不删除、不回退
    assert published[0]["interim"] is False and published[0]["streaming"] is True
    assert published[-1]["streaming"] is False  # 收尾快照：状态由 TURN_END 给出
    assert published[-1]["content"] == "已经确认的一段"
    assert published[-1]["interim"] is False
    assert published[-1]["role_evidence"] == "declared_answer"
    assert result.final_content is None  # 取消不落成「正常回答」


async def test_cancel_with_undeclared_text_releases_it_to_the_process_area():
    """取消时未声明的缓冲正文放行到过程区：不猜角色、不丢字。"""
    events: list[dict] = []

    async def emit(payload: dict) -> None:
        events.append(payload)

    stream = _AssistantStream(emit, delta_id="dl_x_1")
    await stream.note_text("还没声明的一段")
    assert events == []  # 未声明：先缓冲
    await stream.finish(None)  # 取消 / 断流
    assert len(events) == 1
    assert events[0]["content"] == "还没声明的一段"
    assert events[0]["interim"] is True and events[0]["streaming"] is False
    assert events[0]["role_evidence"] is None
    assert stream.answer_text == ""


async def test_cancel_after_tool_round_call_keeps_the_text_in_the_process_area():
    """取消落在「模型已返回工具调用、工具还没开始」这一刻：模型说明不能丢。"""
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                text="我先读一下文件，然后再把结论整理出来。",
                tool_calls=[ScriptedToolCall(id="c1", name="echo", arguments={"text": "hi"})],
            )
        ]
    )
    bus = EventBus()
    state = {"cancelled": False}
    loop = AgentLoop(
        adapter, _registry(), bus, turn_id="turn_1", is_cancelled=lambda: state["cancelled"]
    )
    original_plan = loop._plan

    async def _plan_then_stop(messages, tools=None, *, hint=None):
        completion = await original_plan(messages, tools, hint=hint)
        state["cancelled"] = True  # 模型刚返回，用户就按了停止
        return completion

    loop._plan = _plan_then_stop  # type: ignore[method-assign]
    result = await asyncio.wait_for(loop.run("hi"), 3)

    assert result.cancelled is True
    assert len(adapter.requests) == 1  # 取消之后不再发起新的模型调用
    interim = [e for e in _events(bus, "ASSISTANT") if e["interim"]]
    assert interim, "已确认的过程说明必须保留"
    assert interim[-1]["content"] == "我先读一下文件，然后再把结论整理出来。"
    assert interim[-1]["streaming"] is False  # 收尾快照：不再增长
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
    assert published[0]["interim"] is True  # 失败：没有定论，留在过程区
    assert published[-1]["streaming"] is False
    assert published[-1]["interim"] is True


async def test_plain_adapter_exception_is_not_disguised_as_a_provider_error():
    """没有走适配器错误分类的异常**不得**被包装成厂商故障（Lead 裁决 2026-10-06）。

    provider_error 的含义是「厂商/传输路径失败」。适配器或解析自己出 bug 时把它
    报成厂商故障，是对用户撒谎 —— 所以这里断言异常**原样**上抛（类型与文字都不变），
    由上层按类名如实归到 internal_error。
    """
    from agent.adapters import errors as adapter_errors
    from agent.adapters.errors import ProviderError

    class _BugAdapter(BaseAdapter):
        mode = AdapterMode.TEXT
        supports_stream = False

        def __init__(self) -> None:
            self.model = "bug"
            self.endpoint = None

        async def complete(self, messages, tools, **kwargs):
            raise RuntimeError("解析响应时炸了：choices 字段缺失")

    bus = EventBus()
    loop = AgentLoop(_BugAdapter(), _registry(), bus, turn_id="turn_1")
    with pytest.raises(RuntimeError) as excinfo:
        await loop.run("hi")

    assert not isinstance(excinfo.value, ProviderError)  # 不冒充厂商故障
    assert type(excinfo.value).__name__ == "RuntimeError"
    assert "choices 字段缺失" in str(excinfo.value)  # 原文如实保留
    assert _events(bus, "ERROR")[0]["code"] == "planning_failed"

    # 对照：已经归一化的适配器错误保持自己的类型（它就是厂商故障）
    normalized = FakeStreamAdapter(
        [
            StreamScript(
                text_chunks=["前半段"],
                error=adapter_errors.NetworkError("连接断了"),
                error_after=1,
            )
        ]
    )
    with pytest.raises(adapter_errors.NetworkError):
        await AgentLoop(normalized, _registry(), EventBus(), turn_id="turn_2").run("hi")


async def test_adapter_without_streaming_delivers_declared_and_undeclared_answers():
    """不支持流式：整段交付；声明被识别，未声明走降级路径（都**不假装流式**）。"""
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                text="我先看一下。",
                tool_calls=[ScriptedToolCall(id="c1", name="echo", arguments={"text": "hi"})],
            ),
            StreamScript(text=ANSWER_MARKER + "\n不支持流式也能回答"),
        ],
        stream_supported=False,
    )
    bus = EventBus()
    loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
    result = await loop.run("hi")
    assert result.final_content == "不支持流式也能回答"
    published = _events(bus, "ASSISTANT")
    assert len(published) == 2
    # 工作轮：进度说明（过程区）
    assert published[0]["interim"] is True and published[0]["streaming"] is False
    assert published[0]["content"] == "我先看一下。"
    assert published[0]["delta_id"] == "dl_1_1" and published[0]["seq"] == 1
    assert published[0]["role_evidence"] is None
    # 声明回答：整段交付（声明不展示、不假装流式）
    assert published[1]["streaming"] is False and published[1]["interim"] is False
    assert published[1]["content"] == "不支持流式也能回答"
    assert published[1]["delta_id"] == "dl_1_2" and published[1]["seq"] == 1
    assert published[1]["stage_id"] is None and published[1]["call_ids"] == []
    assert published[1]["role_evidence"] == "declared_answer"
    assert [r["stream"] for r in adapter.requests] == [False, False]  # 没有假装流式

    # 未声明 + 没有工具调用：一次性交付到正式回答区（降级路径），不重新生成
    undeclared = FakeStreamAdapter(
        [StreamScript(text="没声明的整段回答")], stream_supported=False
    )
    bus2 = EventBus()
    result2 = await AgentLoop(undeclared, _registry(), bus2, turn_id="turn_2").run("hi")
    assert result2.final_content == "没声明的整段回答"
    published2 = _events(bus2, "ASSISTANT")
    assert len(published2) == 1
    assert published2[0]["interim"] is False and published2[0]["streaming"] is False
    assert published2[0]["role_evidence"] == "undeclared_answer"
    assert len(undeclared.requests) == 1


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
    # 一次调用：整段返回（降级）；未声明的正文就是本轮回答，不重新生成
    assert adapter.calls == 1
    published = _events(bus, "ASSISTANT")
    assert len(published) == 1
    assert all(e["streaming"] is False for e in published)
    assert published[0]["interim"] is False
    assert published[0]["role_evidence"] == "undeclared_answer"
    assert any("不支持实时生成" in w for w in result.warnings)


# ---- 回答阶段协议：阶段提示与各档位下发（§1.1）--------------------------------


async def test_content_role_protocol_is_sent_with_every_call():
    """内容角色协议随**每次**调用发出（native 档作为最后一条 system 消息），可断言。"""
    from agent.prompts import ANSWER_MARKER as PROTOCOL_MARKER
    from agent.prompts import CONTENT_ROLE_PROTOCOL

    # 协议语义三件事必须写在文本里（措辞可改，语义不能少）
    assert PROTOCOL_MARKER in CONTENT_ROLE_PROTOCOL
    assert "进度说明" in CONTENT_ROLE_PROTOCOL
    assert "开头" in CONTENT_ROLE_PROTOCOL
    assert "工具" in CONTENT_ROLE_PROTOCOL  # 声明之后不要再请求工具

    adapter = FakeStreamAdapter(
        [
            StreamScript(
                tool_calls=[ScriptedToolCall(id="c1", name="echo", arguments={"text": "hi"})]
            ),
            StreamScript(text=ANSWER_MARKER + "\n正式回答"),
        ]
    )
    bus = EventBus()
    result = await AgentLoop(adapter, _registry(), bus, turn_id="turn_1").run("hi")

    assert result.final_content == "正式回答"
    assert len(adapter.requests) == 2
    for request in adapter.requests:
        messages = request["messages"]
        assert messages[-1].role == "system"
        assert messages[-1].content == CONTENT_ROLE_PROTOCOL
        # 协议只发给这一次调用，不写回对话记录
        assert not any(m.content == CONTENT_ROLE_PROTOCOL for m in messages[:-1])


async def test_text_tier_carries_the_protocol_in_its_own_system_prompt():
    """text 兼容档：协议拼进它自己的 system prompt，循环不再重复注入（同一份措辞）。"""
    from agent.adapters.text import TextAdapter
    from agent.prompts import CONTENT_ROLE_PROTOCOL

    text_adapter = TextAdapter(client=object(), model="m")
    prompt = text_adapter.build_system_prompt(TOOLS)
    assert CONTENT_ROLE_PROTOCOL in prompt
    assert prompt.count(CONTENT_ROLE_PROTOCOL) == 1
    assert text_adapter.protocol_in_prompt is True

    class _TextOnce(BaseAdapter):
        mode = AdapterMode.TEXT
        supports_stream = False
        protocol_in_prompt = True

        def __init__(self) -> None:
            self.model = "text"
            self.endpoint = None
            self.requests: list[list[ChatMessage]] = []

        async def complete(self, messages, tools, **kwargs):
            self.requests.append(list(messages))
            return Completion(
                message=ChatMessage(role="assistant", content=ANSWER_MARKER + "\n回答")
            )

    adapter = _TextOnce()
    result = await AgentLoop(adapter, _registry(), EventBus(), turn_id="turn_1").run("hi")
    assert result.final_content == "回答"
    assert len(adapter.requests) == 1
    assert not any(m.content == CONTENT_ROLE_PROTOCOL for m in adapter.requests[0])


async def test_text_compat_declared_answer_ignores_late_tool_blocks():
    """text 兼容档与 native / anthropic 一致：声明回答之后请求的工具不执行（可见警告）。"""
    from agent.tools.base import Tool, ToolResult

    class _CountingEcho(Tool):
        name = "echo"
        description = "echo"
        parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

        def __init__(self) -> None:
            self.runs = 0

        async def run(self, **kwargs):
            self.runs += 1
            return ToolResult(ok=True, content="echo:%s" % kwargs.get("text", ""))

    class _TextAdapter(BaseAdapter):
        mode = AdapterMode.TEXT
        supports_stream = False

        def __init__(self) -> None:
            self.model = "text"
            self.endpoint = None
            self.calls: list[list[str]] = []

        async def complete(self, messages, tools, **kwargs):
            self.calls.append([t.name for t in tools])
            if len(self.calls) == 1:
                # 工作调用：text 档的正文是工具协议块，adapter 已把它解析成 tool_calls
                # （content 仍是原文，但**不得**被当成正文展示）。
                return Completion(
                    message=ChatMessage(
                        role="assistant",
                        content=json.dumps(
                            {"tool_calls": [{"name": "echo", "arguments": {"text": "hi"}}]}
                        ),
                        tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "hi"})],
                    )
                )
            # 声明回答：模型违反「声明之后不要再请求工具」，仍回了一个工具调用
            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content=ANSWER_MARKER + "\n这是正式回答",
                    tool_calls=[ToolCall(id="c9", name="echo", arguments={"text": "again"})],
                )
            )

    registry = ToolRegistry()
    tool = _CountingEcho()
    registry.register(tool)
    adapter = _TextAdapter()
    bus = EventBus()
    loop = AgentLoop(adapter, registry, bus, turn_id="turn_1")
    result = await loop.run("hi")

    # 两次调用都带全套工具（角色只看声明）；迟到的工具调用**不执行**
    assert adapter.calls == [["echo"], ["echo"]]
    assert tool.runs == 1
    assert result.final_content == "这是正式回答"
    assert len(_events(bus, "TOOL_START")) == 1  # 只执行了工作轮那一次
    assert any("工具" in w for w in result.warnings)
    warnings = _events(bus, "WARNING")
    assert any(w["code"] == "answer_declared_with_tool_calls" for w in warnings)
    # 声明不展示；text 档的 JSON 协议块也不当作正文
    assistant = _events(bus, "ASSISTANT")
    assert all(ANSWER_MARKER not in (e["content"] or "") for e in assistant)
    assert all("tool_calls" not in (e["content"] or "") for e in assistant)
    answers = [e for e in assistant if not e["interim"]]
    assert answers and answers[-1]["content"] == "这是正式回答"
    assert answers[-1]["role_evidence"] == "declared_answer"


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


# ---- 内容角色协议（第五轮 §1.1）：角色由模型声明，不由调用类型猜 -----------------

# 冻结协议串（测试里写死字面量：契约改了就应当红）
ANSWER_MARKER = "[[QIO:ANSWER]]"


def _answer_area(bus: EventBus) -> list[dict]:
    return [e for e in _events(bus, "ASSISTANT") if e["interim"] is False]


def _process_area(bus: EventBus) -> list[dict]:
    return [e for e in _events(bus, "ASSISTANT") if e["interim"] is True]


async def test_undeclared_full_answer_is_not_duplicated_and_not_regenerated():
    """反例（今天必红）：模型第一次就写出完整答案、没有声明、工具列表非空。

    旧行为：工作调用正文进过程区 → 结束无 tool_calls → 再发一次 tools=[] 重写一遍
    → 过程区与正式回答区各一份。新协议：未声明正文先有界缓冲 → 调用结束没有工具
    调用 → **一次性**交付到正式回答区（降级路径），不重新生成、过程区不留副本。
    """
    answer = "这是完整答案，模型没有发声明。"
    adapter = FakeStreamAdapter([StreamScript(text=answer), StreamScript(text=answer)])
    bus = EventBus()
    loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
    result = await loop.run("hi")

    assert result.final_content == answer
    assert len(adapter.requests) == 1, "不得为同一段答案再发一次调用（禁止重复生成）"
    assert [bool(r["tools"]) for r in adapter.requests] == [True]  # 唯一一次调用带工具
    events = _events(bus, "ASSISTANT")
    assert [e["content"] for e in events] == [answer]
    # 降级路径：一次性交付，不冒充流式
    assert events[0]["interim"] is False and events[0]["streaming"] is False
    assert events[0]["role_evidence"] == "undeclared_answer"
    # 过程区没有这份完整答案的副本
    assert not [e for e in _process_area(bus) if answer in (e["content"] or "")]


async def test_declaration_with_a_long_body_in_one_chunk_is_recognised():
    """反例（第六轮问题二，今天必红）：合法声明 + 长正文落在**同一个大分块**里。

    旧实现把「累计收到的正文长度」当成「声明是否有效」的证据（len(probe) > 32 →
    未声明）：整段被当未声明 —— 回答区在调用结束前是空的，结束后连声明一起泄漏。
    修正版：_probe 只保存识别控制前缀所需的部分，任何超出部分都是正文；匹配成功后
    同一分块里剩下的正文**立即**交给回答流（实时发布）。
    """
    body = "这是声明之后的长正文，必须实时进正式回答区。" * 3  # 60 字符
    chunk = ANSWER_MARKER + "\n" + body
    hold = asyncio.Event()
    adapter = FakeStreamAdapter(
        [StreamScript(text_chunks=[chunk], hold=hold, hold_after=1)]
    )
    bus = EventBus()
    loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
    task = asyncio.create_task(loop.run("直接回答我"))
    try:
        await asyncio.sleep(0.2)
        published = _events(bus, "ASSISTANT")
        assert published, (
            "合法声明 + 长正文同一分块：正文必须已经实时进回答区（这里为空 = 被当成未声明）"
        )
        assert not task.done(), "provider 还开着（hold）：这一刻正文就该可见"
        assert published[0]["interim"] is False and published[0]["streaming"] is True
        assert published[0]["content"] == body
        assert ANSWER_MARKER not in published[0]["content"]
        assert published[0]["role_evidence"] == "declared_answer"
    finally:
        hold.set()
    result = await asyncio.wait_for(task, 3)

    assert result.final_content == body
    events = _events(bus, "ASSISTANT")
    assert all(ANSWER_MARKER not in (e["content"] or "") for e in events), (
        "声明泄漏到正文里了",
        [e["content"] for e in events],
    )
    assert all(e["interim"] is False for e in events)
    assert len(adapter.requests) == 1


@pytest.mark.parametrize("split_at", list(range(0, len(ANSWER_MARKER) + 2)))
async def test_declaration_split_at_any_position_with_a_long_body(split_at):
    """分块无关性：声明在**每个位置**被拆开、后面跟着长正文，结果必须完全一致。"""
    body = "正文很长的一段内容，用来验证分块无关性。" * 2
    text = ANSWER_MARKER + "\n" + body
    chunks = [c for c in (text[:split_at], text[split_at:]) if c]
    adapter = FakeStreamAdapter([StreamScript(text_chunks=chunks)])
    bus = EventBus()
    loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
    result = await loop.run("直接回答我")

    assert result.final_content == body
    events = _events(bus, "ASSISTANT")
    assert events, "一个字都没有展示"
    assert all(ANSWER_MARKER not in (e["content"] or "") for e in events), (
        "声明泄漏",
        [e["content"] for e in events],
    )
    assert not _process_area(bus), "声明回答不该出现在过程区"
    assert len(adapter.requests) == 1


async def test_undeclared_long_answer_is_delivered_once_without_regeneration(
    tmp_path, monkeypatch
):
    """反例（第六轮问题三，今天必红）：未声明 262,165 字符完整答案、不请求工具。

    旧实现：字符数超过 256*1024 即改判工作调用 → 放行到过程区；调用结束没有可交付
    回答内容 → _maybe_fallback 再生成一次 → 2 次调用、过程区与回答区各一份。
    修正版：上限只管理资源（内存 + 临时暂存），不改角色；结束后一次性交付回答区。
    """
    monkeypatch.setenv("APPDATA", str(tmp_path))
    answer = "长" * 262_165
    # 分两块到达：第一块定角色（未声明），第二块让缓冲越过旧上限 —— 旧实现正是在
    # _feed 里把「超过 256*1024 字符」改判成工作调用，于是过程区一份 + 再生成一份。
    adapter = FakeStreamAdapter(
        [StreamScript(text_chunks=[answer[:1], answer[1:]]), StreamScript(text=answer)]
    )
    bus = EventBus()
    loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
    result = await loop.run("hi")

    assert len(adapter.requests) == 1, "不得为同一段答案再发一次调用（禁止重复生成）"
    assert result.final_content == answer
    events = _events(bus, "ASSISTANT")
    assert len(events) == 1, [e["content"][:20] for e in events]
    assert events[0]["content"] == answer
    assert events[0]["interim"] is False and events[0]["streaming"] is False
    assert events[0]["role_evidence"] == "undeclared_answer"
    assert _process_area(bus) == [], "过程区不得留副本"


async def test_undeclared_memory_limit_is_measured_in_utf8_bytes(tmp_path, monkeypatch):
    """缓冲上限按 **UTF-8 字节** 计量，超出部分落临时暂存（旧实现按字符数，中文差 3 倍）。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    memory_limit = 256 * 1024
    answer = "汉" * 87_382  # 262,146 字节 > 256 KiB，但字符数远小于上限
    assert len(answer) < memory_limit < len(answer.encode("utf-8"))
    hold = asyncio.Event()
    adapter = FakeStreamAdapter(
        [StreamScript(text_chunks=[answer[:1], answer[1:]], hold=hold, hold_after=2)]
    )
    bus = EventBus()
    loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
    spill_dir = tmp_path / "qio" / "tmp"
    task = asyncio.create_task(loop.run("hi"))
    try:
        await asyncio.sleep(0.3)  # provider 卡在 hold：暂存文件此刻应该已经在
        assert not task.done()
        assert spill_dir.is_dir(), "超出内存上限的正文必须落到 <data_dir>/tmp 暂存"
        assert list(spill_dir.glob("*.spill")), "暂存文件没有创建（上限按 UTF-8 字节算）"
    finally:
        hold.set()
    result = await asyncio.wait_for(task, 5)

    assert result.final_content == answer
    assert len(adapter.requests) == 1
    events = _events(bus, "ASSISTANT")
    assert len(events) == 1 and events[0]["content"] == answer
    assert events[0]["role_evidence"] == "undeclared_answer"
    # 收尾后不留暂存文件（内存 + 暂存按序拼回，一个字都不少）
    assert not list(spill_dir.glob("*.spill")), "收尾后暂存文件没有清理"


async def test_declaration_recognition_is_chunking_invariant():
    """分块无关性（契约 §1.2 核心不变量）：同一文本，无论怎样拆分/合并，结果完全一致。

    覆盖：整段一块、一字符一块、空块混入、确定性随机、声明单独一块、声明拆两半、
    小写声明 + CRLF、中文 + 代码块 + 表格 + 长英文。
    """
    import random

    body = (
        "第一段中文。\n\n"
        "```python\nprint('hi')\n```\n\n"
        "| 列 | 值 |\n| - | - |\n| 1 | 2 |\n\n"
        "Long English tail: " + "lorem ipsum " * 8
    )
    text = ANSWER_MARKER + "\n" + body
    rng = random.Random(20261008)
    random_chunks: list[str] = []
    index = 0
    while index < len(text):
        size = rng.randint(1, 9)
        random_chunks.append(text[index : index + size])
        index += size
    empty_mixed: list[str] = []
    for i, char in enumerate(text):
        if i % 7 == 0:
            empty_mixed.append("")
        empty_mixed.append(char)
    strategies = {
        "整段一块": [text],
        "一字符一块": list(text),
        "空块混入": empty_mixed,
        "确定性随机": random_chunks,
        "声明单独一块": [ANSWER_MARKER, "\n" + body],
        "声明拆两半": [text[:7], text[7:]],
        "小写声明+CRLF": ["[[qio:answer]]\r\n", body],
    }
    for seq, (name, chunks) in enumerate(strategies.items()):
        adapter = FakeStreamAdapter(
            [StreamScript(text_chunks=[c for c in chunks if c] or [""])]
        )
        bus = EventBus()
        result = await AgentLoop(
            adapter, _registry(), bus, turn_id=f"turn_inv_{seq}"
        ).run("hi")
        events = _events(bus, "ASSISTANT")
        shown = "".join(str(e["content"] or "") for e in events)
        assert result.final_content == body, (name, (result.final_content or "")[:40])
        assert "[[qio" not in shown.lower(), (name, shown[:80])
        assert all(e["interim"] is False for e in events), (name, [e["interim"] for e in events])
        assert not _process_area(bus), name
        assert len(adapter.requests) == 1, name


@pytest.mark.parametrize("unit", ["ascii", "cjk"])
async def test_memory_threshold_boundaries_spill_only_beyond_the_byte_limit(
    tmp_path, monkeypatch, unit
):
    """阈值前 / 等号 / 阈值后：只有 **UTF-8 字节数超过** 256 KiB 才落暂存。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    limit = 256 * 1024
    char, per_char = ("a", 1) if unit == "ascii" else ("汉", 3)
    equal_chars = limit // per_char  # ASCII 恰好等号；CJK 取「还放得下」的最大字符数
    spill_dir = tmp_path / "qio" / "tmp"
    for seq, (label, count) in enumerate(
        [("阈值前", equal_chars - 1), ("等号", equal_chars), ("阈值后", equal_chars + 1)]
    ):
        answer = char * count
        expect_spill = len(answer.encode("utf-8")) > limit
        hold = asyncio.Event()
        adapter = FakeStreamAdapter(
            [StreamScript(text_chunks=[answer[:1], answer[1:]], hold=hold, hold_after=2)]
        )
        bus = EventBus()
        loop = AgentLoop(adapter, _registry(), bus, turn_id=f"turn_th_{unit}_{seq}")
        task = asyncio.create_task(loop.run("hi"))
        try:
            await asyncio.sleep(0.25)
            spilled = bool(list(spill_dir.glob("*.spill")))
            assert spilled is expect_spill, (
                f"{unit} {label}：字节数 {len(answer.encode('utf-8'))} vs 上限 {limit}",
                spilled,
            )
        finally:
            hold.set()
        result = await asyncio.wait_for(task, 5)
        assert result.final_content == answer, (unit, label)
        assert len(adapter.requests) == 1, (unit, label)
        events = _events(bus, "ASSISTANT")
        assert len(events) == 1 and events[0]["content"] == answer, (unit, label)
        assert events[0]["role_evidence"] == "undeclared_answer"
        assert not list(spill_dir.glob("*.spill")), (unit, label, "收尾后暂存未清理")


async def test_spill_write_failure_is_reported_honestly(tmp_path, monkeypatch):
    """暂存不可用：如实报告（可见 WARNING + 截断事实进交付内容），不改角色、不丢字不重发。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from agent.core import answer_buffer as buffer_module

    def _boom(self, data):  # noqa: ANN001 - 模拟磁盘写失败
        raise OSError("disk full (test)")

    monkeypatch.setattr(buffer_module.AnswerBuffer, "_write_sync", _boom)
    answer = "汉" * 90_000  # 270,000 字节 > 256 KiB → 必须走暂存
    adapter = FakeStreamAdapter(
        [StreamScript(text_chunks=[answer[:1], answer[1:]]), StreamScript(text=answer)]
    )
    bus = EventBus()
    result = await AgentLoop(adapter, _registry(), bus, turn_id="turn_spill_fail").run("hi")

    assert len(adapter.requests) == 1, "暂存失败也不得再生成一次"
    text = result.final_content or ""
    assert text.startswith("汉" * 87_381), "内存里的部分必须完整交付"
    assert "截断" in text, "截断事实必须写进交付内容"
    assert _process_area(bus) == [], "过程区不得留副本"
    warnings = _events(bus, "WARNING")
    assert [w["code"] for w in warnings] == ["answer_truncated"], warnings
    assert any("截断" in w for w in result.warnings)


async def test_spill_hard_limit_truncates_and_reports(tmp_path, monkeypatch):
    """达到暂存硬上限：如实截断 + 可见 WARNING + 截断事实写进交付内容（绝不无界增长）。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from agent.core import answer_buffer as buffer_module

    monkeypatch.setattr(buffer_module, "UNDECLARED_SPILL_LIMIT", 1024)
    answer = "汉" * 90_000  # 内存 256 KiB + 暂存 1 KiB 之后必须停下并如实报告
    adapter = FakeStreamAdapter(
        [StreamScript(text_chunks=[answer[:1], answer[1:]])]
    )
    bus = EventBus()
    result = await AgentLoop(adapter, _registry(), bus, turn_id="turn_spill_cap").run("hi")

    assert len(adapter.requests) == 1
    text = result.final_content or ""
    delivered = text.split("\n\n—— 系统事实")[0]
    assert len(delivered.encode("utf-8")) <= 256 * 1024 + 1024 + 8, len(
        delivered.encode("utf-8")
    )
    assert "截断" in text
    assert [w["code"] for w in _events(bus, "WARNING")] == ["answer_truncated"]
    assert _process_area(bus) == []


async def test_stream_break_with_spilled_text_releases_it_and_cleans_up(tmp_path, monkeypatch):
    """断流：缓冲（内存 + 暂存）按序完整放行到过程区，暂存文件清理，不猜角色。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from agent.adapters import errors as adapter_errors

    answer = "汉" * 90_000
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                text_chunks=[answer[:1], answer[1:]],
                error=adapter_errors.NetworkError("连接断了"),
                error_after=2,
            )
        ]
    )
    bus = EventBus()
    spill_dir = tmp_path / "qio" / "tmp"
    with pytest.raises(adapter_errors.NetworkError):
        await AgentLoop(adapter, _registry(), bus, turn_id="turn_spill_break").run("hi")

    process = _process_area(bus)
    assert "".join(str(e["content"] or "") for e in process) == answer, "断流不得丢字"
    assert _answer_area(bus) == [], "断流不猜角色：不进正式回答区"
    assert not list(spill_dir.glob("*.spill")), "断流后暂存文件必须清理"


async def test_stale_spill_files_are_cleaned_before_the_first_spill(tmp_path, monkeypatch):
    """启动清理：新进程第一次要写暂存前，先把上一进程残留的 .spill 清掉。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    spill_dir = tmp_path / "qio" / "tmp"
    spill_dir.mkdir(parents=True)
    stale = spill_dir / "dl_previous_process.spill"
    stale.write_text("上一进程残留", encoding="utf-8")

    answer = "汉" * 90_000
    adapter = FakeStreamAdapter([StreamScript(text_chunks=[answer[:1], answer[1:]])])
    result = await AgentLoop(
        adapter, _registry(), EventBus(), turn_id="turn_stale"
    ).run("hi")

    assert result.final_content == answer
    assert not stale.exists(), "陈旧暂存文件必须在第一次写暂存前清理"
    assert not list(spill_dir.glob("*.spill"))


async def test_non_streaming_long_undeclared_answer_is_delivered_once(tmp_path, monkeypatch):
    """整段响应（不支持流式）里的未声明长正文：一次性交付、1 次调用、暂存清理。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    answer = "汉" * 90_000
    adapter = FakeStreamAdapter([StreamScript(text=answer)], stream_supported=False)
    bus = EventBus()
    result = await AgentLoop(
        adapter, _registry(), bus, turn_id="turn_whole_long"
    ).run("hi")

    assert result.final_content == answer
    assert len(adapter.requests) == 1
    events = _events(bus, "ASSISTANT")
    assert len(events) == 1
    assert events[0]["interim"] is False and events[0]["streaming"] is False
    assert events[0]["role_evidence"] == "undeclared_answer"
    assert not list((tmp_path / "qio" / "tmp").glob("*.spill"))


async def test_undeclared_answer_is_invisible_until_the_call_ends():
    """未声明正文在**调用结束前不得出现在任何容器里**，结束后只出现在正式回答区一次。

    契约 §1.1 修正版第 2、3 条：前缀不匹配**不等于**工作调用 —— 不得因为前缀不匹配
    就立刻放进过程区；未声明正文先有界缓冲，调用结束无工具调用时一次性交付到正式
    回答区（同一 delta_id、interim=false、streaming=false、role_evidence=undeclared_answer）。
    """
    answer = "结论：这个问题的答案是 42。"
    hold = asyncio.Event()
    adapter = FakeStreamAdapter(
        [StreamScript(text_chunks=[answer], hold=hold, hold_after=1)]
    )
    bus = EventBus()
    loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
    task = asyncio.create_task(loop.run("直接回答我"))

    # 等流真的开起来（provider 卡在 hold）：这段时间里任何容器都不该有正文
    await asyncio.sleep(0.15)
    assert not task.done(), "provider 还开着，这一轮不该结束"
    assert _events(bus, "ASSISTANT") == [], (
        "未声明的正文在调用结束前就出现在容器里了（过程区也不行）",
        _events(bus, "ASSISTANT"),
    )
    assert _process_area(bus) == [] and _answer_area(bus) == []

    hold.set()
    result = await asyncio.wait_for(task, 3)

    # 结束后：只出现在正式回答区一次（一次性交付，不冒充流式）
    events = _events(bus, "ASSISTANT")
    assert len(events) == 1, events
    assert events[0]["content"] == answer
    assert events[0]["interim"] is False and events[0]["streaming"] is False
    assert events[0]["role_evidence"] == "undeclared_answer"
    assert _process_area(bus) == [], "过程区不得留副本"
    assert result.final_content == answer
    assert len(adapter.requests) == 1, "不得为同一段答案再发一次调用"


async def test_declared_answer_streams_live_and_costs_one_call():
    """合法直接回答：声明被识别（标记不展示）、正文实时进回答区、整轮 1 次调用。"""
    hold = asyncio.Event()
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                text_chunks=[ANSWER_MARKER[:7], ANSWER_MARKER[7:] + "\n", "第一段回答。", "第二段回答。"],
                hold=hold,
                hold_after=3,
            )
        ]
    )
    bus = EventBus()
    loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
    task = asyncio.create_task(loop.run("直接回答我"))
    try:
        published = await _wait_for(bus, "ASSISTANT", count=1)
        # provider 还没结束，正式回答区已经有字（真流式，且不是收尾校准）
        assert not task.done()
        assert published[0]["interim"] is False and published[0]["streaming"] is True
        assert published[0]["content"] == "第一段回答。"
        assert published[0]["role_evidence"] == "declared_answer"
        assert ANSWER_MARKER not in published[0]["content"]
    finally:
        hold.set()
    result = await asyncio.wait_for(task, 3)

    assert result.final_content == "第一段回答。第二段回答。"
    assert len(adapter.requests) == 1, "合规直接回答只需 1 次调用"
    events = _events(bus, "ASSISTANT")
    assert all(e["interim"] is False for e in events)
    assert events[-1]["streaming"] is False  # 收尾校准
    assert events[-1]["content"] == "第一段回答。第二段回答。"
    assert not _process_area(bus)


async def test_work_note_then_tool_then_declared_answer():
    """合法过程说明 + 工具 + 声明回答：过程区一份、回答区一份，互不重复。"""
    answer = "这是读完之后给出的正式回答。"
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                text="我先读一下文件。",
                tool_calls=[ScriptedToolCall(id="c1", name="echo", arguments={"text": "hi"})],
            ),
            StreamScript(text=f"{ANSWER_MARKER}\n{answer}"),
        ]
    )
    bus = EventBus()
    loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
    result = await loop.run("hi")

    assert result.tool_calls_made == 1
    assert result.final_content == answer
    assert len(adapter.requests) == 2
    work = _process_area(bus)
    answers = _answer_area(bus)
    assert any(e["content"] == "我先读一下文件。" for e in work)
    assert answers and answers[-1]["content"] == answer
    assert all(ANSWER_MARKER not in (e["content"] or "") for e in work + answers)
    assert not [e for e in work if answer in (e["content"] or "")], "答案不得在过程区留副本"


async def test_late_tool_call_after_declaration_is_not_executed_and_warns():
    """声明之后的迟到工具调用：不执行 + 可见警告，按已声明的回答收尾。"""
    answer = "这是正式回答，工具不会再执行。"
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                text=f"{ANSWER_MARKER}\n{answer}",
                tool_calls=[ScriptedToolCall(id="c1", name="echo", arguments={"text": "hi"})],
            )
        ]
    )
    bus = EventBus()
    loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
    result = await loop.run("hi")

    assert result.tool_calls_made == 0, "声明之后的工具调用不得执行"
    assert result.final_content == answer
    assert len(adapter.requests) == 1
    assert _events(bus, "TOOL_START") == []
    warnings = _events(bus, "WARNING")
    assert warnings, "必须给出可见警告，不能静默忽略工具调用"
    assert any("工具" in str(w.get("message") or "") for w in warnings), warnings


async def test_turn_without_any_answer_gets_at_most_one_fallback_call():
    """兜底：整轮完全没有回答内容 → 最多一次 tools=[] 调用要求最终回答。"""
    answer = "兜底给出的最终回答。"
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                tool_calls=[ScriptedToolCall(id="c1", name="echo", arguments={"text": "hi"})]
            ),
            StreamScript(text=""),  # 工作调用：一个字都没有
            StreamScript(text=f"{ANSWER_MARKER}\n{answer}"),  # 兜底调用（tools=[]）
        ]
    )
    bus = EventBus()
    loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
    result = await loop.run("hi")

    assert result.tool_calls_made == 1
    assert result.final_content == answer
    assert [bool(r["tools"]) for r in adapter.requests] == [True, True, False]
    assert len(adapter.requests) == 3, "兜底只允许一次"
    assert _answer_area(bus)[-1]["content"] == answer


async def test_marker_not_at_the_start_is_treated_as_undeclared():
    """非法声明（不在开头）：按未声明处理 —— 不猜测、不改写、不重复生成。"""
    text = "先说明一下。" + ANSWER_MARKER + "后面的字不算声明。"
    adapter = FakeStreamAdapter([StreamScript(text=text), StreamScript(text=text)])
    bus = EventBus()
    loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
    result = await loop.run("hi")

    # 声明不在开头 → 非法：不当作声明（标记原样留在正文里，不剥离、不改写）
    assert result.final_content == text
    assert ANSWER_MARKER in (result.final_content or "")
    # 未声明的正文一次性交付到正式回答区（降级路径）：不重新生成、过程区不留副本
    assert len(adapter.requests) == 1, "不得为同一段文字重复生成"
    answers = _answer_area(bus)
    assert answers and answers[-1]["content"] == text
    assert answers[-1]["role_evidence"] == "undeclared_answer"
    assert _process_area(bus) == []


async def test_undeclared_text_with_a_tool_call_goes_to_the_process_area():
    """未声明正文 + 工具调用：缓冲文字是过程说明，放行到过程区（不丢字）。"""
    note = "我先看一下文件。" + ANSWER_MARKER  # 非法声明，按未声明处理
    answer = "这是正式回答。"
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                text=note,
                tool_calls=[ScriptedToolCall(id="c1", name="echo", arguments={"text": "hi"})],
            ),
            StreamScript(text=f"{ANSWER_MARKER}\n{answer}"),
        ]
    )
    bus = EventBus()
    loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
    result = await loop.run("hi")

    assert result.tool_calls_made == 1
    assert result.final_content == answer
    work = _process_area(bus)
    assert any(e["content"] == note for e in work), "过程说明不得丢字"
    assert not [e for e in work if answer in (e["content"] or "")]


# ---- 明确的厂商错误：不得被 SDK 自动重试换成一个假答案（第五轮复核 ②）--------


async def test_provider_error_on_the_answer_call_is_not_retried_into_a_fake_answer():
    """回答调用的明确错误（500）必须原样上抛，且**只发一个请求**。

    openai SDK 默认 max_retries=2：5xx 会被静默重试，我们看到的会是重试后那一次
    的结果 —— 有状态假厂商的下一个脚本步骤会被当成成功返回，「厂商错误」因此
    变成一个假答案。适配器必须关掉 SDK 的自动重试。
    """
    import httpx
    from openai import AsyncOpenAI

    from agent.adapters.errors import ProviderError

    requests: list[int] = []

    def handler(request: Any) -> Any:
        requests.append(len(requests) + 1)
        if len(requests) == 1:
            # 工作调用：SSE 200，但零增量（这次调用没有输出）
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=b""
            )
        return httpx.Response(
            500, json={"error": {"message": "stream-aborted", "type": "verify"}}
        )

    client = AsyncOpenAI(
        api_key="sk-test-not-a-real-key",
        base_url="http://fake-provider.test/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_retries=2,  # 故意打开：适配器必须自己关掉它
    )
    adapter = NativeAdapter(client=client, model="m1")
    bus = EventBus()
    loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
    try:
        with pytest.raises(ProviderError):
            await loop.run("hi")
    finally:
        await client.close()

    assert requests == [1, 2], ("明确的厂商错误不得被 SDK 重试", requests)
    assert _events(bus, "ASSISTANT") == [], "失败的回答调用不得编出一个正式回答"


async def test_anthropic_and_text_tiers_do_not_retry_provider_errors():
    """anthropic / text 档同样不在明确错误上重试：各只发一个请求、原样上抛。"""
    from agent.adapters import errors as adapter_errors
    from agent.adapters.anthropic import AnthropicAdapter
    from agent.adapters.text import TextAdapter

    # anthropic：500 → 归一化成供应商错误，且只有一次请求
    adapter = AnthropicAdapter(api_key="k", model="m")
    fake = _FakeHttpxClient([], status_code=500)
    adapter._client = fake
    try:
        with pytest.raises(adapter_errors.ProviderError):
            async for _ in adapter.stream([ChatMessage(role="user", content="hi")], TOOLS):
                pass
    finally:
        await adapter.close()
    assert len(fake.payloads) == 1

    # text 档：complete() 抛错 → 归一化成供应商错误，且只有一次请求
    class _CountingTextClient:
        def __init__(self) -> None:
            self.calls = 0

        @property
        def chat(self) -> "_CountingTextClient":
            return self

        @property
        def completions(self) -> "_CountingTextClient":
            return self

        async def create(self, **kwargs: Any) -> Any:
            self.calls += 1
            raise RuntimeError("provider returned 500")

    text_client = _CountingTextClient()
    text_adapter = TextAdapter(client=text_client, model="m")
    with pytest.raises(adapter_errors.ProviderError):
        await text_adapter.complete([ChatMessage(role="user", content="hi")], TOOLS)
    assert text_client.calls == 1


# ---- 兼容性：服务忽略 stream=true（真实 SDK 复现 install e2e 的失败）-------------


def _whole_json(*, tool_call: bool) -> str:
    """一个只回整段 JSON 的 OpenAI 兼容服务（scripts/e2e_fake_provider.py 的形状）。"""
    message: dict = {"role": "assistant", "content": "整段回答", "tool_calls": None}
    finish = "stop"
    if tool_call:
        message = {
            "role": "assistant",
            "content": "我先读一下。",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "echo", "arguments": '{"text": "hi"}'},
                }
            ],
        }
        finish = "tool_calls"
    return json.dumps(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 1,
            "model": "m",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
        }
    )


class _StreamIgnoringProvider:
    """假装忽略 stream=true 的服务：**每个请求**都回 application/json 整段。

    与 scripts/e2e_fake_provider.py 同形：不区分流式，按请求顺序把整段结果发出去
    （真实 SDK 对这种响应会得到一条零 chunk 的流且不报错）。
    """

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self._last: str | None = None
        self.requests: list[dict] = []

    def handler(self, request: Any) -> Any:
        import httpx

        payload = json.loads(request.content.decode() or "{}")
        self.requests.append(payload)
        body = self._replies.pop(0) if self._replies else self._last
        self._last = body
        return httpx.Response(
            200, content=(body or "").encode(), headers={"content-type": "application/json"}
        )


class _SseProvider:
    """同一个 SDK、同一套请求形状，但真的按 SSE 回（对照用例）。"""

    def __init__(self, chunks: list[dict]) -> None:
        self._chunks = chunks
        self.requests: list[dict] = []

    def handler(self, request: Any) -> Any:
        import httpx

        self.requests.append(json.loads(request.content.decode() or "{}"))
        lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in self._chunks]
        lines.append("data: [DONE]\n\n")
        return httpx.Response(
            200,
            content="".join(lines).encode(),
            headers={"content-type": "text/event-stream"},
        )


def _real_openai_client(provider: Any) -> Any:
    """真实 openai SDK + httpx.MockTransport（不联网）：复现线上的兼容性路径。"""
    import httpx
    from openai import AsyncOpenAI

    return AsyncOpenAI(
        api_key="sk-test-not-a-real-key",
        base_url="http://fake-provider.test/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(provider.handler)),
    )


def _sse_chunk(content: str | None = None, finish: str | None = None) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": finish}],
    }


async def test_provider_that_ignores_stream_still_runs_tools_and_delivers_the_answer():
    """install e2e 的真实回归：服务只回整段 JSON，QIO 不能因此变成「没有工具调用」。

    有 with_raw_response 的客户端（真实 SDK）先看 Content-Type：不是 SSE 就把整段
    JSON 当结果 —— 每次模型调用**只发一个请求**（否则按请求消费脚本的假厂商会被
    打乱）。回退到 complete() 只在看不到 Content-Type 时才发生。
    第五轮 §1.1 起不再有固定的回答调用：这里一共 2 个请求（工具轮 + 整段回答）。
    """
    provider = _StreamIgnoringProvider(
        [_whole_json(tool_call=True), _whole_json(tool_call=False)]
    )
    client = _real_openai_client(provider)
    adapter = NativeAdapter(client=client, model="m1")
    bus = EventBus()
    loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
    try:
        result = await loop.run("hi")
    finally:
        await client.close()

    assert result.tool_calls_made == 1  # 工具调用没有被「零增量流」吃掉
    assert result.final_content == "整段回答"
    # 每次模型调用只发一个请求：请求仍带 stream=true（我们并不知道对方会不会流式），
    # 但响应体被直接当作这次调用的整段结果 —— 2 次调用（工具轮 + 未声明的整段回答）
    assert [bool(p.get("stream")) for p in provider.requests] == [True, True]
    # 如实告知：一轮一条 WARNING（该模型路径不支持实时生成）
    warnings = _events(bus, "WARNING")
    assert [w["code"] for w in warnings] == ["streaming_unsupported"]
    assert "不支持实时生成" in warnings[0]["message"]
    assert any("不支持实时生成" in w for w in result.warnings)
    # 正文以一次性 streaming=false 交付：不假装流式；未声明 → 降级路径如实标记
    assistant = _events(bus, "ASSISTANT")
    assert assistant and all(e["streaming"] is False for e in assistant)
    answer = [e for e in assistant if not e["interim"]]
    assert answer and answer[-1]["content"] == "整段回答"
    assert answer[-1]["role_evidence"] == "undeclared_answer"
    assert _events(bus, "TOOL_START")[0]["arguments"] == {"text": "hi"}


async def test_real_sse_provider_streams_without_any_fallback():
    """对照：真的按 SSE 回时，一次调用只发一个请求，事件是流式增量。"""
    provider = _SseProvider(
        [_sse_chunk("流式"), _sse_chunk("回答"), _sse_chunk(None, "stop")]
    )
    client = _real_openai_client(provider)
    adapter = NativeAdapter(client=client, model="m1")
    try:
        # 1) 适配器层：真的收到了增量（不是零 chunk，也就不会被判成降级）
        deltas = await _deltas(adapter)
        assert [d.text for d in deltas if d.kind == STREAM_TEXT] == ["流式", "回答"]
        assert deltas[-1].completion is not None
        assert deltas[-1].completion.message.content == "流式回答"

        # 2) 循环层：一次调用只发一个请求，没有回退、没有 WARNING
        bus = EventBus()
        loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
        result = await loop.run("hi")
    finally:
        await client.close()

    assert result.final_content == "流式回答"
    # 1 次适配器直连 + 1 次循环调用（未声明的正文就是回答：不重新生成、没有回退）
    assert [bool(p.get("stream")) for p in provider.requests] == [True, True]
    # 真 SSE 路径没有警告：未声明的降级如实记录在事件字段里（role_evidence），
    # 不进 WARNING（契约只要求「迟到工具调用」发可见警告）
    assert _events(bus, "WARNING") == []
    assistant = _events(bus, "ASSISTANT")
    assert len(assistant) == 1
    assert assistant[0]["streaming"] is False and assistant[0]["interim"] is False
    assert assistant[0]["content"] == "流式回答"
    assert assistant[0]["delta_id"] == "dl_1_1"
    assert assistant[0]["role_evidence"] == "undeclared_answer"


def _load_e2e_fake_provider():
    """按文件路径加载 CI 用的那个假厂商（它只回整段 JSON、忽略 stream=true）。"""
    import importlib.util
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "qio_e2e_fake_provider", repo_root / "scripts" / "e2e_fake_provider.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_real_e2e_fake_provider_round_trip_still_runs_tools():
    """对着 scripts/e2e_fake_provider.py 真跑一轮：这就是 install e2e 的那条路径。"""
    import threading

    from openai import AsyncOpenAI

    module = _load_e2e_fake_provider()
    server = module.FakeProvider(("127.0.0.1", 0))
    server.script.set(
        [
            # ① 工作轮：先说明再调用工具
            {"text": "我先读一下。", "tool": "echo", "args": {"text": "hi"}},
            # ② 声明回答：正式回答（声明本身不展示）
            {"text": "[[QIO:ANSWER]]\n完成"},
        ]
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = AsyncOpenAI(
        api_key="sk-fake-e2e-not-a-real-key",
        base_url=f"http://127.0.0.1:{server.server_port}/v1",
        timeout=10.0,
    )
    try:
        adapter = NativeAdapter(client=client, model="fake-model")
        bus = EventBus()
        loop = AgentLoop(adapter, _registry(), bus, turn_id="turn_1")
        result = await loop.run("hi")
    finally:
        await client.close()
        server.shutdown()
        server.server_close()

    assert result.tool_calls_made == 1  # 工具调用不再被「零增量流」吃掉
    assert result.final_content == "完成"
    # 假厂商只回整段 JSON：每次模型调用 1 个请求 —— 工具轮 + 声明回答，
    # 脚本队列不会被多打的请求打乱（install e2e 的假厂商脚本要按同一口径准备）
    assert len(server.log) == 2
    warnings = _events(bus, "WARNING")
    assert [w["code"] for w in warnings] == ["streaming_unsupported"]
    assistant = _events(bus, "ASSISTANT")
    assert assistant and all(e["streaming"] is False for e in assistant)


def _sse_tool_chunk(
    index: int,
    *,
    call_id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ]
                },
                "finish_reason": None,
            }
        ],
    }


async def test_real_sse_provider_assembles_fragmented_tool_arguments():
    """真实 SDK + 真 SSE（raw 路径）：参数碎片只在 adapter 里拼成合法 JSON。"""
    provider = _SseProvider(
        [
            _sse_tool_chunk(0, call_id="call_1", name="echo", arguments='{"te'),
            _sse_tool_chunk(0, arguments='xt": "hi"}'),
            _sse_chunk(None, "tool_calls"),
        ]
    )
    client = _real_openai_client(provider)
    adapter = NativeAdapter(client=client, model="m1")
    try:
        deltas = await _deltas(adapter)
    finally:
        await client.close()

    assert any(d.kind == STREAM_TOOL_CALL for d in deltas)
    assert not any(d.kind == STREAM_TEXT for d in deltas)  # 碎片不当正文
    done = deltas[-1]
    assert done.kind == STREAM_DONE and done.completion is not None
    calls = done.completion.tool_calls or []
    assert len(calls) == 1 and calls[0].arguments == {"text": "hi"}
    assert done.completion.finish_reason == "tool_calls"
    assert len(provider.requests) == 1  # 仍是一次调用，没有被整段降级


async def test_native_stream_with_zero_chunks_declares_unsupported():
    from agent.adapters.errors import UnsupportedCapability

    client = _FakeOpenAIClient([])  # 一条分片都没有：就是「忽略 stream」的形状
    with pytest.raises(UnsupportedCapability):
        await _deltas(NativeAdapter(client=client, model="m1"))


async def test_anthropic_stream_with_non_sse_body_declares_unsupported():
    from agent.adapters.errors import UnsupportedCapability

    # 整段 JSON 而不是 SSE：一行 data: 都没有，解析结果为空
    lines = ['{"id":"msg_1","type":"message","content":[{"type":"text","text":"整段"}],"stop_reason":"end_turn"}']
    with pytest.raises(UnsupportedCapability):
        await _anthropic_stream(lines)


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