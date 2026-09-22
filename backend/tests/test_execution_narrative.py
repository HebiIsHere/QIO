"""Execution Narrative：模型文案与工具事实分离（spec 2026-09-22）。"""
from __future__ import annotations

from agent.core.narrative import (
    NARRATIVE_KEY,
    Narrative,
    narrative_event_payload,
    parse_narrative,
    split_narrative_arguments,
)


def test_parse_narrative_accepts_known_kinds():
    out = parse_narrative({"kind": "announce", "text": "  我先确认审批链路。  "})
    assert out == Narrative(kind="announce", text="我先确认审批链路。")


def test_parse_narrative_rejects_unknown_kind_and_empty_text():
    assert parse_narrative({"kind": "speak", "text": "x"}) is None
    assert parse_narrative({"kind": "announce", "text": "   "}) is None
    assert parse_narrative("not-a-dict") is None
    assert parse_narrative(None) is None


def test_parse_narrative_keeps_explanation_only():
    out = parse_narrative({"explanation": "为了写入叙事记录，需要新增一个模块文件。"})
    assert out is not None
    assert out.silent is True
    assert out.text == ""
    assert out.explanation.startswith("为了写入叙事记录")


def test_parse_narrative_drops_unknown_keys_and_caps_length():
    out = parse_narrative(
        {
            "kind": "progress",
            "text": "甲" * 400,
            "explanation": "乙" * 500,
            "risk": "danger",
            "capabilities": ["写入文件：是"],
            "text_override": "骗你的",
        }
    )
    assert out is not None
    assert len(out.text) <= 120
    assert len(out.explanation) <= 200
    assert not hasattr(out, "risk")


def test_parse_narrative_redacts_secret_shaped_text():
    out = parse_narrative({"kind": "warning", "text": "api_key=sk-abcdef123456"})
    assert out is not None
    assert "sk-abcdef123456" not in out.text


def test_split_narrative_arguments_removes_reserved_key():
    clean, raw = split_narrative_arguments(
        {"path": "a.txt", NARRATIVE_KEY: {"kind": "announce", "text": "先看文件"}}
    )
    assert clean == {"path": "a.txt"}
    assert raw == {"kind": "announce", "text": "先看文件"}
    assert split_narrative_arguments({"path": "a.txt"}) == ({"path": "a.txt"}, None)


def test_narrative_event_payload_carries_system_provenance():
    payload = narrative_event_payload(
        "msg_1",
        "turn_1",
        Narrative(kind="announce", text="先看审批链路"),
        tool="grep_search",
        call_id="call_a",
        call_ids=["call_a", "call_b"],
        created_at="2026-09-22T09:41:09+00:00",
    )
    assert payload["narrative_id"] == "msg_1"
    assert payload["tool"] == "grep_search"
    assert payload["call_ids"] == ["call_a", "call_b"]
    assert payload["text"] == "先看审批链路"


# ---- 适配器：把 _qio 剥离成 ToolCall.narrative ---------------------------------


def test_native_adapter_strips_narrative_from_arguments():
    from agent.adapters.native import NativeAdapter

    class _Fn:
        name = "fs_read"
        arguments = '{"path": "a.txt", "_qio": {"kind": "announce", "text": "先读它"}}'

    class _Call:
        id = "call_1"
        function = _Fn()

    class _Msg:
        content = None
        tool_calls = [_Call()]

    class _Choice:
        message = _Msg()
        finish_reason = "tool_calls"

    class _Raw:
        choices = [_Choice()]
        usage = None

    completion = NativeAdapter(client=None, model="m")._to_completion(_Raw())
    call = completion.tool_calls[0]
    assert call.arguments == {"path": "a.txt"}
    assert call.narrative == {"kind": "announce", "text": "先读它"}


def test_text_adapter_parses_narrative_from_json_block():
    from agent.adapters.text import TextAdapter

    adapter = TextAdapter(client=None, model="m")
    parsed = adapter._parse(
        '```json\n{"tool_calls": [{"name": "fs_read", "arguments": '
        '{"path": "a.txt", "_qio": {"kind": "progress", "text": "继续核对"}}}]}\n```'
    )
    assert parsed is not None
    raw = parsed["tool_calls"][0]["arguments"]
    clean, narrative = split_narrative_arguments(raw)
    assert clean == {"path": "a.txt"}
    assert narrative["kind"] == "progress"


def test_text_adapter_tool_call_carries_narrative():
    import asyncio

    from agent.adapters.text import TextAdapter

    class _Choice:
        message = type("M", (), {"content": '{"tool_calls": [{"name": "fs_read", "arguments": {"path": "a.txt", "_qio": {"kind": "announce", "text": "先读它"}}}]}'})()

    class _Raw:
        choices = [_Choice()]
        usage = None

    class _Client:
        class chat:  # noqa: N801 - 模拟 openai 客户端形状
            class completions:  # noqa: N801
                @staticmethod
                async def create(**kwargs):
                    return _Raw()

    adapter = TextAdapter(client=_Client(), model="m")
    completion = asyncio.run(adapter.complete([], []))
    call = completion.tool_calls[0]
    assert call.arguments == {"path": "a.txt"}
    assert call.narrative == {"kind": "announce", "text": "先读它"}


def test_registry_specs_declare_narrative_field():
    from agent.tools.base import Tool, ToolResult
    from agent.tools.registry import ToolRegistry

    class _Echo(Tool):
        name = "narrative_probe"
        description = "echo"
        parameters = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}

        async def run(self, **kwargs):
            return ToolResult(ok=True, content="ok")

    reg = ToolRegistry()
    reg.register(_Echo())
    spec = reg.specs()[0]
    assert NARRATIVE_KEY in spec.parameters["properties"]
    assert spec.parameters["required"] == ["q"]


# ---- 审批 explanation：模型只能补"为什么"，事实字段不变 --------------------------


class _ApprovalStub:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []

    async def request(self, kind: str, payload: dict, **kwargs) -> object:
        from agent.tools.approval import ApprovalResult

        self.requests.append((kind, dict(payload)))
        return ApprovalResult("appr_1", "approved")


class _RecordingBus:
    """记录 SSE 事件，并对审批请求立即应答（用它跑真实的 ApprovalService）。"""

    def __init__(self) -> None:
        self.events: list[object] = []
        self.service = None

    async def publish(self, event) -> None:
        import asyncio

        self.events.append(event)
        if getattr(event.type, "value", "") == "APPROVAL_REQUIRED":
            approval_id = event.data["approval"]["approval_id"]
            asyncio.get_running_loop().create_task(
                self.service.respond(approval_id, "approved")
            )


class _NeedsApproval:
    """构造工具类的小工厂：requires_approval 的普通工具。"""

    @staticmethod
    def build(name: str = "fs_write"):
        from agent.tools.base import Tool, ToolResult

        class _Write(Tool):
            parameters = {"type": "object", "properties": {"path": {"type": "string"}}}
            requires_approval = True

            async def run(self, **kwargs):
                return ToolResult(ok=True, content="written")

        _Write.name = name
        _Write.description = "write"
        return _Write()


async def test_approval_payload_gets_model_explanation():
    from agent.adapters.base import ToolCall
    from agent.tools.approval import ApprovalService
    from agent.tools.registry import ToolRegistry

    bus = _RecordingBus()
    approvals = ApprovalService(bus)
    bus.service = approvals
    reg = ToolRegistry(approvals=approvals)
    reg.register(_NeedsApproval.build())
    result = await reg.execute(
        ToolCall(
            id="c1",
            name="fs_write",
            arguments={"path": "a.txt"},
            narrative={
                "kind": "announce",
                "text": "写入叙事模块",
                "explanation": "为了让过程说明可恢复。",
            },
        )
    )
    assert result.ok
    event = next(e for e in bus.events if e.type.value == "APPROVAL_REQUIRED")
    assert event.data["approval"]["kind"] == "tool_execution"
    payload = event.data["approval"]["payload"]
    assert payload["explanation"] == "为了让过程说明可恢复。"
    # 事实字段仍是系统生成的，且没有混进模型给的键
    assert payload["description"] == "想修改当前项目中的一个文件"
    assert payload["arguments"] == {"path": "a.txt"}
    assert payload["access"] == ["写入：a.txt"]


async def test_approval_keeps_existing_explanation():
    """工具自己带了 explanation（如工具创建提案）时，模型文案不得覆盖。"""
    from agent.adapters.base import ToolCall
    from agent.tools.approval import ApprovalService
    from agent.tools.base import Tool, ToolResult
    from agent.tools.registry import ToolRegistry

    bus = _RecordingBus()
    approvals = ApprovalService(bus)
    bus.service = approvals

    class _Inner(Tool):
        name = "dev_submit_tool"
        description = "submit"
        parameters = {"type": "object", "properties": {}}

        async def run(self, **kwargs):
            await self.approvals.request(
                "tool_create", {"name": "x", "explanation": "提案自带的说明"}
            )
            return ToolResult(ok=True, content="ok")

    reg = ToolRegistry(approvals=approvals)
    tool = _Inner()
    tool.approvals = approvals
    reg.register(tool)
    await reg.execute(
        ToolCall(
            id="c1",
            name="dev_submit_tool",
            arguments={},
            narrative={"explanation": "模型想覆盖的说明"},
        )
    )
    event = next(e for e in bus.events if e.type.value == "APPROVAL_REQUIRED")
    payload = event.data["approval"]["payload"]
    assert payload["explanation"] == "提案自带的说明"


async def test_approval_without_narrative_still_works():
    from agent.adapters.base import ToolCall
    from agent.tools.approval import ApprovalService
    from agent.tools.registry import ToolRegistry

    bus = _RecordingBus()
    approvals = ApprovalService(bus)
    bus.service = approvals
    reg = ToolRegistry(approvals=approvals)
    reg.register(_NeedsApproval.build())
    result = await reg.execute(
        ToolCall(id="c1", name="fs_write", arguments={"path": "a.txt"})
    )
    assert result.ok
    event = next(e for e in bus.events if e.type.value == "APPROVAL_REQUIRED")
    assert event.data["approval"]["kind"] == "tool_execution"
    payload = event.data["approval"]["payload"]
    assert str(payload.get("explanation") or "") == ""


async def test_parallel_calls_do_not_leak_explanation():
    """并行调用各自持有自己的叙事：一个带 explanation，一个不带。"""
    import asyncio

    from agent.adapters.base import ToolCall
    from agent.tools.approval import ApprovalService
    from agent.tools.registry import ToolRegistry

    bus = _RecordingBus()
    approvals = ApprovalService(bus)
    bus.service = approvals
    reg = ToolRegistry(approvals=approvals)
    reg.register(_NeedsApproval.build("tool_a"))
    reg.register(_NeedsApproval.build("tool_b"))
    await asyncio.gather(
        reg.execute(
            ToolCall(
                id="c1",
                name="tool_a",
                arguments={"path": "a.txt"},
                narrative={"explanation": "A 需要说明"},
            )
        ),
        reg.execute(
            ToolCall(
                id="c2",
                name="tool_b",
                arguments={"path": "b.txt"},
                narrative={"explanation": "B 需要说明"},
            )
        ),
        reg.execute(ToolCall(id="c3", name="tool_a", arguments={"path": "c.txt"})),
    )
    by_call = {}
    for event in [e for e in bus.events if e.type.value == "APPROVAL_REQUIRED"]:
        payload = event.data["approval"]["payload"]
        by_call.setdefault(str(payload.get("arguments")), payload)
    assert by_call["{'path': 'a.txt'}"]["explanation"] == "A 需要说明"
    assert by_call["{'path': 'b.txt'}"]["explanation"] == "B 需要说明"
    assert str(by_call["{'path': 'c.txt'}"].get("explanation") or "") == ""


async def test_narrative_never_reaches_tool_arguments():
    """_qio 在 adapter 层就被剥离：工具收到的 kwargs 里没有它。"""
    from agent.adapters.base import ToolCall
    from agent.tools.base import Tool, ToolResult
    from agent.tools.registry import ToolRegistry

    seen: list[dict] = []

    class _Echo(Tool):
        name = "echo"
        description = "echo"
        parameters = {"type": "object", "properties": {"q": {"type": "string"}}}

        async def run(self, **kwargs):
            seen.append(dict(kwargs))
            return ToolResult(ok=True, content="ok")

    reg = ToolRegistry()
    reg.register(_Echo())
    await reg.execute(
        ToolCall(
            id="c1",
            name="echo",
            arguments={"q": "hi"},
            narrative={"kind": "announce", "text": "打招呼"},
        )
    )
    assert seen == [{"q": "hi"}]


# ---- SSE 事件：NARRATIVE 是"关键事件"（不可静默丢弃） ---------------------------


def test_narrative_event_is_registered_and_critical():
    from agent.api.bus import CRITICAL_EVENTS
    from agent.api.events import EventType, make_event, sse_format

    assert EventType.NARRATIVE.value == "NARRATIVE"
    assert EventType.NARRATIVE in CRITICAL_EVENTS
    text = sse_format(make_event(EventType.NARRATIVE, {"text": "先确认链路"}))
    assert "event: NARRATIVE" in text
    assert "先确认链路" in text


# ---- AgentLoop：一批工具最多一条叙事，执行前发出，批次结束结算 ------------------


def _http_events(chunks: list[str]) -> list[dict]:
    import json

    out: list[dict] = []
    for line in chunks:
        for part in line.splitlines():
            if part.startswith("data: "):
                out.append(json.loads(part[6:]))
    return out


class _LoopAdapter:
    mode = "native"
    model = "m"


def _narrative_tool():
    from agent.tools.base import Tool, ToolResult

    class _Echo(Tool):
        name = "echo"
        description = "echo"
        parameters = {"type": "object", "properties": {"q": {"type": "string"}}}
        is_concurrency_safe = True

        async def run(self, **kwargs):
            return ToolResult(ok=True, content="ok")

    return _Echo()


async def _collect_events(bus, expected: int, coro) -> list[dict]:
    import asyncio

    chunks: list[str] = []

    async def consume() -> None:
        async for chunk in bus.stream():
            chunks.append(chunk)
            if len(_http_events(chunks)) >= expected:
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await coro
    await asyncio.wait_for(task, timeout=5)
    return _http_events(chunks)


async def test_loop_emits_one_narrative_before_tool_batch():
    from agent.adapters.base import ToolCall
    from agent.api.bus import EventBus
    from agent.api.events import EventType, make_event
    from agent.core.loop import AgentLoop
    from agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(_narrative_tool())
    bus = EventBus()
    seen: list[dict] = []

    async def sink(turn_id, narrative, call, call_ids):
        seen.append(
            {
                "turn_id": turn_id,
                "kind": narrative.kind,
                "text": narrative.text,
                "call_id": call.id,
                "call_ids": list(call_ids),
            }
        )
        # 真实实现里 sink = AppContext._on_narrative：落库后广播 NARRATIVE
        await bus.publish(
            make_event(
                EventType.NARRATIVE,
                {
                    "narrative_id": "msg_1",
                    "turn_id": turn_id,
                    "kind": narrative.kind,
                    "text": narrative.text,
                    "call_ids": list(call_ids),
                },
            )
        )
        return "msg_1"

    loop = AgentLoop(
        _LoopAdapter(), registry, bus, turn_id="turn_1", narrative_sink=sink
    )
    calls = [
        ToolCall(
            id="c1",
            name="echo",
            arguments={"q": "a"},
            narrative={"kind": "announce", "text": "我先确认审批链路。"},
        ),
        ToolCall(id="c2", name="echo", arguments={"q": "b"}),
        ToolCall(id="c3", name="echo", arguments={"q": "c"}),
    ]
    events = await _collect_events(bus, expected=7, coro=loop._dispatch_tool_calls(calls))

    # 只发布一次叙事；携带整批 call_ids（前端据此收纳抽屉）
    assert len(seen) == 1
    assert seen[0]["kind"] == "announce"
    assert seen[0]["call_ids"] == ["c1", "c2", "c3"]

    types = [e["type"] for e in events]
    assert types[0] == "NARRATIVE"
    assert types.index("NARRATIVE") < types.index("TOOL_START")
    assert types.count("TOOL_START") == 3
    assert types.count("TOOL_END") == 3


async def test_loop_emits_single_narrative_when_every_call_carries_one():
    from agent.adapters.base import ToolCall
    from agent.api.bus import EventBus
    from agent.api.events import EventType, make_event
    from agent.core.loop import AgentLoop
    from agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(_narrative_tool())
    bus = EventBus()
    seen: list[str] = []

    async def sink(turn_id, narrative, call, call_ids):
        seen.append(narrative.text)
        await bus.publish(
            make_event(EventType.NARRATIVE, {"narrative_id": "msg_1", "text": narrative.text})
        )
        return "msg_1"

    loop = AgentLoop(
        _LoopAdapter(), registry, bus, turn_id="turn_1", narrative_sink=sink
    )
    calls = [
        ToolCall(
            id=f"c{i}",
            name="echo",
            arguments={"q": str(i)},
            narrative={"kind": "announce", "text": f"第 {i} 步"},
        )
        for i in (1, 2, 3)
    ]
    await _collect_events(bus, expected=7, coro=loop._dispatch_tool_calls(calls))
    assert seen == ["第 1 步"]


async def test_loop_stays_silent_without_narrative():
    from agent.adapters.base import ToolCall
    from agent.api.bus import EventBus
    from agent.core.loop import AgentLoop
    from agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(_narrative_tool())
    bus = EventBus()
    calls_to_sink: list[str] = []

    async def sink(turn_id, narrative, call, call_ids):
        calls_to_sink.append(narrative.text)
        return "msg_1"

    loop = AgentLoop(
        _LoopAdapter(), registry, bus, turn_id="turn_1", narrative_sink=sink
    )
    calls = [
        ToolCall(id="c1", name="echo", arguments={"q": "a"}),
        ToolCall(id="c2", name="echo", arguments={"q": "b"}),
    ]
    events = await _collect_events(bus, expected=4, coro=loop._dispatch_tool_calls(calls))
    assert calls_to_sink == []
    assert [e["type"] for e in events] == ["TOOL_START", "TOOL_END", "TOOL_START", "TOOL_END"]


async def test_loop_settles_narrative_with_real_results():
    from agent.adapters.base import ToolCall
    from agent.api.bus import EventBus
    from agent.api.events import EventType, make_event
    from agent.core.loop import AgentLoop
    from agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(_narrative_tool())
    bus = EventBus()
    settled: list[dict] = []

    async def sink(turn_id, narrative, call, call_ids):
        await bus.publish(
            make_event(EventType.NARRATIVE, {"narrative_id": "msg_42", "text": narrative.text})
        )
        return "msg_42"

    async def settler(narrative_id, results, calls, facts):
        settled.append(
            {
                "narrative_id": narrative_id,
                "ok": {c.id: results[c.id].ok for c in calls},
                "status": facts["c1"]["status"],
            }
        )

    loop = AgentLoop(
        _LoopAdapter(),
        registry,
        bus,
        turn_id="turn_1",
        narrative_sink=sink,
        narrative_settler=settler,
    )
    calls = [
        ToolCall(
            id="c1",
            name="echo",
            arguments={"q": "a"},
            narrative={"kind": "announce", "text": "我先查一下。"},
        )
    ]
    await _collect_events(bus, expected=3, coro=loop._dispatch_tool_calls(calls))
    assert settled[0]["narrative_id"] == "msg_42"
    assert settled[0]["ok"] == {"c1": True}
    assert settled[0]["status"] == "success"


async def test_narrative_sink_failure_does_not_break_tools():
    """叙事只是表达：它失败绝不能影响工具执行与 TOOL_END。"""
    from agent.adapters.base import ToolCall
    from agent.api.bus import EventBus
    from agent.core.loop import AgentLoop
    from agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(_narrative_tool())
    bus = EventBus()

    async def sink(turn_id, narrative, call, call_ids):
        raise RuntimeError("narrative storage down")

    loop = AgentLoop(
        _LoopAdapter(), registry, bus, turn_id="turn_1", narrative_sink=sink
    )
    calls = [
        ToolCall(
            id="c1",
            name="echo",
            arguments={"q": "a"},
            narrative={"kind": "announce", "text": "我先查一下。"},
        )
    ]
    events = await _collect_events(bus, expected=2, coro=loop._dispatch_tool_calls(calls))
    assert [e["type"] for e in events] == ["TOOL_START", "TOOL_END"]
    assert events[1]["data"]["ok"] is True


async def test_loop_skips_interim_when_batch_has_narrative():
    """合并规则：这一批工具带了叙事，就不再推 interim 气泡（同一阶段只留一种过程表达）。"""
    from agent.adapters.base import ChatMessage, Completion, ToolCall
    from agent.api.bus import EventBus
    from agent.api.events import EventType
    from agent.core.loop import AgentLoop
    from agent.tools.registry import ToolRegistry

    class _EchoAdapter:
        mode = "native"
        model = "m"

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return Completion(
                    message=ChatMessage(
                        role="assistant",
                        content="我先看几个文件再说。",
                        tool_calls=[
                            ToolCall(
                                id="c1",
                                name="echo",
                                arguments={"q": "a"},
                                narrative={"kind": "announce", "text": "我先确认审批链路。"},
                            )
                        ],
                    )
                )
            return Completion(message=ChatMessage(role="assistant", content="完成"))

    registry = ToolRegistry()
    registry.register(_narrative_tool())
    bus = EventBus()
    events: list[dict] = []

    async def sink(turn_id, narrative, call, call_ids):
        events.append({"narrative": narrative.text})
        return "msg_1"

    loop = AgentLoop(_EchoAdapter(), registry, bus, turn_id="turn_1", narrative_sink=sink)
    await loop.run("hi")

    published = [e.type for e in bus._history]
    # 有叙事 → 没有 interim 的 ASSISTANT；叙事本身照常
    assert EventType.ASSISTANT not in published
    assert events == [{"narrative": "我先确认审批链路。"}]


async def test_loop_keeps_interim_when_batch_has_no_narrative():
    """没有叙事时，模型的前置说明仍然以 interim 气泡显示（不丢信息）。"""
    from agent.adapters.base import ChatMessage, Completion, ToolCall
    from agent.api.bus import EventBus
    from agent.api.events import EventType
    from agent.core.loop import AgentLoop
    from agent.tools.registry import ToolRegistry

    class _EchoAdapter:
        mode = "native"
        model = "m"

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return Completion(
                    message=ChatMessage(
                        role="assistant",
                        content="我先看几个文件再说。",
                        tool_calls=[ToolCall(id="c1", name="echo", arguments={"q": "a"})],
                    )
                )
            return Completion(message=ChatMessage(role="assistant", content="完成"))

    registry = ToolRegistry()
    registry.register(_narrative_tool())
    loop = AgentLoop(_EchoAdapter(), registry, EventBus(), turn_id="turn_1")
    await loop.run("hi")

    interim = [
        e for e in loop.bus._history if e.type == EventType.ASSISTANT and e.data.get("interim")
    ]
    assert len(interim) == 1
    assert interim[0].data["content"] == "我先看几个文件再说。"


# ---- AppContext：先落库、再广播；批次结束补写系统调用摘要 ------------------------


def _app_ctx(tmp_path):
    from agent.api.bus import EventBus
    from agent.config import Settings
    from agent.services.app import AppContext
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations

    conn = connect(tmp_path / "narrative.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    topic = ctx.topics.nodes.create_topic("执行叙事层").id
    fragment = ctx.fragments.get_or_create_open(topic).id
    ctx.bindings.record_binding("turn_1", topic, fragment_id=fragment)
    return ctx, topic, fragment


async def test_narrative_is_persisted_then_broadcast(tmp_path):
    from agent.adapters.base import ToolCall
    from agent.core.narrative import Narrative, parse_narrative

    ctx, topic, fragment = _app_ctx(tmp_path)
    narrative = parse_narrative({"kind": "announce", "text": "我先确认审批链路。"})
    assert narrative is not None
    call = ToolCall(id="c1", name="echo", arguments={"q": "a"})

    message_id = await ctx._on_narrative("turn_1", narrative, call, ["c1"])
    assert message_id

    row = ctx.conn.execute(
        "SELECT role, content, content_type, turn_id, raw, fragment_id FROM messages WHERE id = ?",
        (message_id,),
    ).fetchone()
    assert row["role"] == "assistant"
    assert row["content_type"] == "narrative"
    assert row["content"] == "我先确认审批链路。"
    assert row["turn_id"] == "turn_1"
    assert row["fragment_id"] == fragment
    import json

    raw = json.loads(row["raw"])
    assert raw["narrative"]["kind"] == "announce"
    assert raw["narrative"]["tool"] == "echo"
    assert raw["calls"] == []

    events = [e for e in ctx.bus._history if e.type.value == "NARRATIVE"]
    assert len(events) == 1
    assert events[0].data["narrative_id"] == message_id
    assert events[0].data["call_ids"] == ["c1"]


async def test_narrative_without_binding_only_broadcasts(tmp_path):
    """拿不到本轮绑定时不落库，但也不报错、不写脏数据。"""
    from agent.adapters.base import ToolCall
    from agent.core.narrative import parse_narrative

    ctx, topic, fragment = _app_ctx(tmp_path)
    narrative = parse_narrative({"kind": "progress", "text": "继续核对。"})
    assert narrative is not None
    call = ToolCall(id="c9", name="echo", arguments={})
    out = await ctx._on_narrative("turn_unknown", narrative, call, ["c9"])
    assert out is None
    count = ctx.conn.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE content_type = 'narrative'"
    ).fetchone()["c"]
    assert count == 0


async def test_settle_writes_system_call_summary(tmp_path):
    import json

    from agent.adapters.base import ToolCall
    from agent.core.narrative import parse_narrative
    from agent.tools.base import ToolResult

    ctx, topic, fragment = _app_ctx(tmp_path)
    narrative = parse_narrative(
        {"kind": "announce", "text": "我先确认链路。", "explanation": "写入叙事模块需要你确认。"}
    )
    assert narrative is not None
    call = ToolCall(id="c1", name="echo", arguments={"q": "a"}, narrative={})
    message_id = await ctx._on_narrative("turn_1", narrative, call, ["c1"])

    results = {"c1": ToolResult(ok=True, content="ok")}
    facts = {"c1": {"status": "success", "duration_ms": 210, "error": None}}
    await ctx._settle_narrative(message_id, results, [call], facts)

    row = ctx.conn.execute("SELECT raw FROM messages WHERE id = ?", (message_id,)).fetchone()
    raw = json.loads(row["raw"])
    assert raw["calls"] == [
        {
            "call_id": "c1",
            "tool": "echo",
            "title": raw["calls"][0]["title"],
            "status": "success",
            "error": None,
            "duration_ms": 210,
        }
    ]
    # 系统摘要里不出现模型文案
    assert "我先确认链路" not in json.dumps(raw["calls"], ensure_ascii=False)


def test_narrative_rows_do_not_count_toward_fragment_capacity(tmp_path):
    ctx, topic, fragment = _app_ctx(tmp_path)
    before = ctx.fragments.content_tokens(fragment)
    ctx.memory.append_message(
        topic_id=topic,
        role="assistant",
        content="我先确认审批请求从后端到前端的完整路径。",
        content_type="narrative",
        fragment_id=fragment,
        turn_id="turn_1",
    )
    assert ctx.fragments.content_tokens(fragment) == before
    # 普通消息照常计入
    ctx.memory.append_message(
        topic_id=topic, role="user", content="普通消息", fragment_id=fragment
    )
    assert ctx.fragments.content_tokens(fragment) > before


def test_active_turn_narratives_returns_system_view(tmp_path):
    ctx, topic, fragment = _app_ctx(tmp_path)
    ctx.memory.append_message(
        topic_id=topic,
        role="assistant",
        content="我先确认链路。",
        content_type="narrative",
        fragment_id=fragment,
        turn_id="turn_1",
        raw={
            "narrative": {"kind": "announce", "tool": "echo", "call_id": "c1"},
            "calls": [{"call_id": "c1", "tool": "echo", "status": "success", "duration_ms": 5}],
        },
    )
    out = ctx.active_turn_narratives("turn_1")
    assert len(out) == 1
    assert out[0]["kind"] == "announce"
    assert out[0]["text"] == "我先确认链路。"
    assert out[0]["calls"][0]["tool"] == "echo"
    # 子任务的叙事不属于主对话
    assert ctx.active_turn_narratives("subagent:task_x") == []


async def test_full_turn_persists_narrative_and_call_summary(tmp_path, monkeypatch):
    """端到端：一轮真实 turn 里模型带 `_qio`，叙事落库并补写系统调用摘要。"""
    import json

    from agent.adapters.base import ChatMessage, Completion, ToolCall

    ctx, topic, fragment = _app_ctx(tmp_path)
    # echo 是内置工具，直接用真实注册表跑完整一轮

    class _Adapter:
        mode = "native"
        model = "fake-narrative"

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return Completion(
                    message=ChatMessage(
                        role="assistant",
                        content=None,
                        tool_calls=[
                            ToolCall(
                                id="c1",
                                name="echo",
                                arguments={"text": "hi"},
                                narrative={
                                    "kind": "announce",
                                    "text": "我先确认审批链路。",
                                    "explanation": "写入叙事模块需要你确认。",
                                },
                            )
                        ],
                    )
                )
            return Completion(message=ChatMessage(role="assistant", content="完成"))

    async def fake_build():
        return _Adapter()

    monkeypatch.setattr(ctx, "build_adapter", fake_build)
    result = await ctx.run_turn("继续实现", topic_id=topic)
    assert result["ok"] is True

    rows = ctx.conn.execute(
        "SELECT id, content, raw FROM messages WHERE content_type = 'narrative'"
    ).fetchall()
    assert len(rows) == 1
    raw = json.loads(rows[0]["raw"])
    assert rows[0]["content"] == "我先确认审批链路。"
    assert raw["narrative"]["kind"] == "announce"
    assert raw["calls"][0]["tool"] == "echo"
    assert raw["calls"][0]["status"] == "success"
    assert isinstance(raw["calls"][0]["duration_ms"], int)
    assert ctx.active_turn_narratives(
        ctx.conn.execute(
            "SELECT turn_id FROM messages WHERE content_type = 'narrative'"
        ).fetchone()["turn_id"]
    )


# ---- 恢复路径：runtime state 与历史分页都要能拿到叙事 ---------------------------


def test_runtime_state_carries_narratives(db_conn, settings):
    from fastapi.testclient import TestClient

    from agent.api.server import create_app

    app = create_app(settings, db_conn)
    ctx = app.state.ctx
    ctx.active_turn_narratives = lambda turn_id=None: [
        {
            "narrative_id": "msg_1",
            "turn_id": "turn_1",
            "kind": "announce",
            "text": "我先确认链路。",
            "calls": [],
            "created_at": "2026-09-22T09:41:09+00:00",
        }
    ]
    with TestClient(app) as client:
        state = client.get("/api/runtime/state").json()
    assert state["narratives"][0]["narrative_id"] == "msg_1"
    assert state["narratives"][0]["kind"] == "announce"


def test_session_context_returns_narrative_raw(db_conn, settings):
    import json

    from fastapi.testclient import TestClient

    from agent.api.server import create_app

    app = create_app(settings, db_conn)
    ctx = app.state.ctx
    topic = ctx.topics.nodes.create_topic("执行叙事层").id
    fragment = ctx.fragments.get_or_create_open(topic).id
    ctx.navigation.enter_topic(topic, fragment_id=fragment)
    ctx.memory.append_message(
        topic_id=topic,
        role="assistant",
        content="我先确认审批链路。",
        content_type="narrative",
        fragment_id=fragment,
        turn_id="turn_1",
        raw={
            "narrative": {"kind": "announce", "tool": "echo", "call_id": "c1"},
            "calls": [{"call_id": "c1", "tool": "echo", "status": "success", "duration_ms": 5}],
        },
    )
    with TestClient(app) as client:
        payload = client.get("/api/session/context").json()
    msg = next(m for m in payload["messages"] if m["content_type"] == "narrative")
    raw = json.loads(msg["raw"])
    assert raw["narrative"]["kind"] == "announce"
    assert raw["calls"][0]["status"] == "success"
