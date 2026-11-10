"""P1-02 正式回答发布边界（阶段门）定向测试。

用 fake/mock provider 证明接线与分支衔接（不声称真实模型准确率）：

* DECISION_OPEN 期间**不发布任何正式正文**：流式正文整段缓冲，没有
  interim=false delta、没有一次性回答、没有正式消息交付；
* 响应含话题工具调用 → 正文**整段丢弃**（不冒充回答，也不改名成过程说明）；
* 响应不含话题工具调用 → 一次性交付（streaming=false），随后本轮 ANSWERABLE，
  之后的模型调用恢复流式；
* 上下文准备失败 → 不发布正式回答、进入 RECOVERABLE_FAILURE、写恢复记录
  （P1-03，接缝的最小验证）。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent.adapters.fake import FakeStreamAdapter, ScriptedToolCall, StreamScript
from agent.api.bus import EventBus
from agent.api.events import EventType
from agent.core.loop import AgentLoop
from agent.services.topic_operation import TopicPhase, publishes_formal_answer
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry

ANSWER_MARKER = "[[QIO:ANSWER]]"


# ---------------------------------------------------------------------------
# 最小工具 / 桥 / 事件捕获（不碰数据库，只证明 loop 侧接线）
# ---------------------------------------------------------------------------


class _TopicTool(Tool):
    name = "switch_topic"
    description = "fake topic switch"
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.executed = 0

    async def run(self, **kwargs):
        self.executed += 1
        return ToolResult(ok=True, content="已切换到目标话题")


class _LookupTool(Tool):
    name = "lookup"
    description = "fake normal tool"
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.executed = 0

    async def run(self, **kwargs):
        self.executed += 1
        return ToolResult(ok=True, content="查到的资料")


class FakeTopicBridge:
    """够用的桥替身：审批决定、阶段回调、上下文重建、操作身份。"""

    def __init__(self, *, decisions=None, rebuild=None) -> None:
        self.loop = None
        self.decisions = list(decisions or [])
        self.requests: list = []
        self.rebuilds: list[str] = []
        self.set_phase_calls: list[TopicPhase] = []
        self.rebuild = rebuild
        self.target: str | None = None
        self.hold: asyncio.Event | None = None
        self.awaiting = asyncio.Event()

    def set_phase(self, phase: TopicPhase) -> None:
        self.set_phase_calls.append(phase)
        if self.loop is not None:
            self.loop.set_topic_phase(phase)

    async def before_tool(self, call):
        if call.name not in ("switch_topic", "create_topic"):
            return None
        self.requests.append(call)
        self.set_phase(TopicPhase.AWAITING_CONFIRMATION)
        self.awaiting.set()
        if self.hold is not None:
            await self.hold.wait()
        decision = self.decisions.pop(0) if self.decisions else {"decision": "rejected"}
        if decision.get("decision") != "approved":
            self.set_phase(TopicPhase.ANSWERABLE)
            return ToolResult(
                ok=False,
                content="用户拒绝了这个话题申请；请在当前话题回答，不要重复申请。",
            )
        self.set_phase(TopicPhase.EXECUTING)
        self.target = str(call.arguments.get("topic_id") or "")
        return None

    async def after_tool(self, call, result):
        if call.name not in ("switch_topic", "create_topic"):
            return None
        if not result.ok:
            self.set_phase(TopicPhase.ANSWERABLE)
            return None
        return {
            "rebind_topic_id": self.target,
            "operation_id": "op-p1r-b",
            "target_topic_id": self.target,
            "stages": {"tool_success": True, "navigation_applied": True},
        }

    async def build_system(self, topic_id: str) -> str:
        self.rebuilds.append(topic_id)
        if self.rebuild is not None:
            return await self.rebuild(topic_id)
        return f"SYS_FOR:{topic_id}"

    def current_operation(self):
        return None


def _registry() -> tuple[ToolRegistry, _TopicTool, _LookupTool]:
    topic = _TopicTool()
    lookup = _LookupTool()
    registry = ToolRegistry()
    registry.register(topic)
    registry.register(lookup)
    return registry, topic, lookup


def _record_events(bus: EventBus) -> list:
    events: list = []
    original = bus.publish

    async def spy(event):
        events.append(event)
        await original(event)

    bus.publish = spy  # type: ignore[method-assign]
    return events


def _assistant(events: list, *, interim: bool | None = None) -> list[dict]:
    out = []
    for event in events:
        if getattr(event, "type", None) != EventType.ASSISTANT:
            continue
        data = dict(getattr(event, "data", None) or {})
        if interim is not None and bool(data.get("interim")) != interim:
            continue
        out.append(data)
    return out


def _phases(events: list) -> list[str]:
    return [
        str((getattr(event, "data", None) or {}).get("phase"))
        for event in events
        if getattr(event, "type", None) == EventType.TOPIC_PHASE
    ]


def _first_index(events: list, predicate) -> int | None:
    for index, event in enumerate(events):
        if predicate(event):
            return index
    return None


def _answerable_index(events: list) -> int | None:
    return _first_index(
        events,
        lambda event: getattr(event, "type", None) == EventType.TOPIC_PHASE
        and (event.data or {}).get("phase") == TopicPhase.ANSWERABLE.value,
    )


def _first_formal_assistant_index(events: list) -> int | None:
    return _first_index(
        events,
        lambda event: getattr(event, "type", None) == EventType.ASSISTANT
        and (event.data or {}).get("interim") is False
        and str((event.data or {}).get("content") or "").strip(),
    )


def _warning_codes(events: list) -> list[str]:
    return [
        str((getattr(event, "data", None) or {}).get("code"))
        for event in events
        if getattr(event, "type", None) == EventType.WARNING
    ]


def _error_codes(events: list) -> list[str]:
    return [
        str((getattr(event, "data", None) or {}).get("code"))
        for event in events
        if getattr(event, "type", None) == EventType.ERROR
    ]


def _make_loop(adapter, registry, bridge) -> AgentLoop:
    return AgentLoop(
        adapter,
        registry,
        EventBus(),
        topic_application=bridge,
        turn_id="turn_p1r_b",
    )


def _switch_script(call_id: str, target: str = "topic-target") -> StreamScript:
    return StreamScript(
        text_chunks=["好的，我先确认一下。"],
        tool_calls=[
            ScriptedToolCall(
                id=call_id,
                name="switch_topic",
                arguments={"topic_id": target},
            )
        ],
    )


# ---------------------------------------------------------------------------
# 1) 决定之前：零正式正文（流式正文整段缓冲，丢弃而非改判）
# ---------------------------------------------------------------------------


async def test_no_formal_body_before_decision_and_topic_body_is_discarded():
    adapter = FakeStreamAdapter(
        [
            _switch_script("c1"),
            StreamScript(text=ANSWER_MARKER + "\n这是目标话题里的正式回答"),
        ]
    )
    registry, topic_tool, _ = _registry()
    bridge = FakeTopicBridge(decisions=[{"decision": "approved"}])
    loop = _make_loop(adapter, registry, bridge)
    bridge.loop = loop
    bridge.hold = asyncio.Event()
    events = _record_events(loop.bus)

    run = asyncio.create_task(loop.run("换到目标话题并回答"))
    try:
        await asyncio.wait_for(bridge.awaiting.wait(), timeout=5)
        # 决定之前：没有任何正式回答事件（含 delta），缓冲正文一个字都没发布
        assert _assistant(events, interim=False) == []
        assert all(
            "好的，我先确认一下" not in (item.get("content") or "")
            for item in _assistant(events)
        ), "含话题工具调用的正文必须整段丢弃（不得改名成过程说明）"
        assert _phases(events)[0] == TopicPhase.DECISION_OPEN.value
        assert TopicPhase.AWAITING_CONFIRMATION.value in _phases(events)
        bridge.hold.set()
        result = await asyncio.wait_for(run, timeout=15)
    finally:
        if not run.done():
            run.cancel()

    assert result.final_content == "这是目标话题里的正式回答"
    assert result.formal_answer is True
    assert loop.topic_phase is TopicPhase.ANSWERABLE
    assert topic_tool.executed == 1
    assert bridge.rebuilds == ["topic-target"]
    formal = _assistant(events, interim=False)
    # 流式回答的实时增量与收尾校准是同一个 delta_id 的累计快照：正文只算一份。
    assert {item["content"] for item in formal} == {"这是目标话题里的正式回答"}
    assert len({item["delta_id"] for item in formal}) == 1
    assert formal[-1]["streaming"] is False
    assert all(
        "好的，我先确认一下" not in (item.get("content") or "")
        for item in _assistant(events)
    )
    assert _phases(events)[-1] == TopicPhase.ANSWERABLE.value
    # 新建/切换目标后：system 整体换成目标上下文，但**不**复制原话题全部历史；
    # 原问题仍然只有一条（user 消息不重复、不新建轮次）。
    second_messages = adapter.requests[1]["messages"]
    # 末尾那条 system 是本次调用的回答协议提示（不写回对话记录），不属于历史。
    assert [m.role for m in second_messages[:4]] == ["system", "user", "assistant", "tool"]
    assert second_messages[0].content == "SYS_FOR:topic-target"
    assert [m.content for m in second_messages if m.role == "user"] == ["换到目标话题并回答"]
    assert len([m for m in second_messages if m.role == "user"]) == 1, "不得复制原话题全部历史"
    answerable_at = _answerable_index(events)
    formal_at = _first_formal_assistant_index(events)
    assert answerable_at is not None and formal_at is not None
    assert answerable_at < formal_at, "ANSWERABLE 必须先于正式正文事件"


# ---------------------------------------------------------------------------
# 2) 拒绝：工具不执行、原话题恰好一份回答、不标用户停止
# ---------------------------------------------------------------------------


async def test_rejected_topic_operation_answers_in_current_topic_exactly_once():
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                text_chunks=["这段先不能交付。"],
                tool_calls=[
                    ScriptedToolCall(
                        id="c1",
                        name="switch_topic",
                        arguments={"topic_id": "topic-target"},
                    )
                ],
            ),
            StreamScript(text=ANSWER_MARKER + "\n就在当前话题回答"),
        ]
    )
    registry, topic_tool, _ = _registry()
    bridge = FakeTopicBridge(decisions=[{"decision": "rejected"}])
    loop = _make_loop(adapter, registry, bridge)
    bridge.loop = loop
    events = _record_events(loop.bus)

    result = await asyncio.wait_for(loop.run("换个话题说这个"), timeout=15)

    assert result.final_content == "就在当前话题回答"
    assert result.cancelled is False
    assert result.stopped_by is None
    assert result.formal_answer is True
    assert topic_tool.executed == 0
    assert bridge.rebuilds == []
    formal = _assistant(events, interim=False)
    assert {item["content"] for item in formal} == {"就在当前话题回答"}
    assert len({item["delta_id"] for item in formal}) == 1
    assert all(
        "这段先不能交付" not in (item.get("content") or "")
        for item in _assistant(events)
    )
    assert loop.topic_phase is TopicPhase.ANSWERABLE


# ---------------------------------------------------------------------------
# 3) 不含话题工具调用：一次性交付（streaming=false）+ ANSWERABLE + 恢复流式
# ---------------------------------------------------------------------------


async def test_plain_answer_is_delivered_once_then_phase_answerable():
    adapter = FakeStreamAdapter(
        [StreamScript(text_chunks=[ANSWER_MARKER + "\n普通回答", "，补充一句"])]
    )
    registry, _, _ = _registry()
    bridge = FakeTopicBridge()
    loop = _make_loop(adapter, registry, bridge)
    bridge.loop = loop
    events = _record_events(loop.bus)

    result = await asyncio.wait_for(loop.run("普通问题"), timeout=15)

    assert result.final_content == "普通回答，补充一句"
    assert result.formal_answer is True
    assert loop.topic_phase is TopicPhase.ANSWERABLE
    formal = _assistant(events, interim=False)
    assert len(formal) == 1, "阶段门收口只允许一次性交付一份正式回答"
    assert formal[0]["streaming"] is False, "缓冲后一次性交付，不冒充流式"
    assert formal[0]["content"] == "普通回答，补充一句"
    assert _phases(events) == [
        TopicPhase.DECISION_OPEN.value,
        TopicPhase.ANSWERABLE.value,
    ]
    # 事件顺序：ANSWERABLE 必须先于正式正文（前端正文门只在可回答阶段放行，
    # 先发正文会被丢弃 —— 契约 §3 / §8.4）。
    answerable_at = _answerable_index(events)
    formal_at = _first_formal_assistant_index(events)
    assert answerable_at is not None and formal_at is not None
    assert answerable_at < formal_at


# ---------------------------------------------------------------------------
# 4) ANSWERABLE 之后：后续回答恢复流式
# ---------------------------------------------------------------------------


async def test_streaming_resumes_after_phase_answerable():
    adapter = FakeStreamAdapter(
        [
            _switch_script("c1"),
            StreamScript(
                text_chunks=[ANSWER_MARKER + "\n目标", "话题", "回答"]
            ),
        ]
    )
    registry, topic_tool, _ = _registry()
    bridge = FakeTopicBridge(decisions=[{"decision": "approved"}])
    loop = _make_loop(adapter, registry, bridge)
    bridge.loop = loop
    events = _record_events(loop.bus)

    result = await asyncio.wait_for(loop.run("换到目标话题再回答"), timeout=15)

    assert result.final_content == "目标话题回答"
    assert topic_tool.executed == 1
    streamed = [
        item
        for item in _assistant(events, interim=False)
        if item.get("streaming") is True
    ]
    assert streamed, "ANSWERABLE 之后的模型调用必须恢复流式"
    assert streamed[-1]["content"] == "目标话题回答"
    # 正式回答事件里只出现这一份正文（收尾快照是同一 delta_id 的校准，不多出别的内容）
    assert {item["content"] for item in _assistant(events, interim=False)} == {
        "目标话题回答"
    }


# ---------------------------------------------------------------------------
# 5) 上下文准备失败：可恢复失败、无正式回答、记录不丢（P1-03 接缝）
# ---------------------------------------------------------------------------


async def _failing_rebuild(topic_id: str) -> str:
    raise RuntimeError("topic context rebuild failed: boom")


async def test_context_prepare_failure_is_recoverable_and_publishes_nothing():
    from agent.services import topic_recovery

    adapter = FakeStreamAdapter(
        [
            _switch_script("c1"),
            StreamScript(text=ANSWER_MARKER + "\n不该出现的旧上下文回答"),
        ]
    )
    registry, topic_tool, _ = _registry()
    bridge = FakeTopicBridge(
        decisions=[{"decision": "approved"}], rebuild=_failing_rebuild
    )
    loop = _make_loop(adapter, registry, bridge)
    bridge.loop = loop
    events = _record_events(loop.bus)

    result = await asyncio.wait_for(loop.run("换到目标话题再回答"), timeout=15)

    assert result.final_content is None, "上下文准备失败不得发布任何回答"
    assert result.formal_answer is False
    assert loop.topic_phase is TopicPhase.RECOVERABLE_FAILURE
    assert _assistant(events, interim=False) == []
    assert "topic_context_prepare_failed" in _error_codes(events)

    error = [
        event.data
        for event in events
        if getattr(event, "type", None) == EventType.ERROR
        and (event.data or {}).get("code") == "topic_context_prepare_failed"
    ][0]
    assert error["recoverable"] is True
    assert error["topic_id"] == "topic-target"
    assert error["operation_id"] == "op-p1r-b"
    record = error["recovery"]
    assert record["failed_stage"] == "context_prepare"
    assert record["retryable"] is True
    # 可恢复失败阶段事件本身必须带恢复事实（前端重试入口靠 record_id）
    recovery_events = [
        event.data
        for event in events
        if getattr(event, "type", None) == EventType.TOPIC_PHASE
        and (event.data or {}).get("phase") == TopicPhase.RECOVERABLE_FAILURE.value
    ]
    assert recovery_events, "RECOVERABLE_FAILURE 必须是可观察阶段事件"
    assert recovery_events[-1]["recovery"]["record_id"] == record["record_id"]
    assert recovery_events[-1]["recovery"]["failed_stage"] == "context_prepare"
    assert recovery_events[-1]["recovery"]["retryable"] is True
    stored = topic_recovery.get_record(bridge, record["record_id"])
    assert stored is not None and stored["status"] == "pending"
    assert stored["topic_id"] == "topic-target"
    # 待重建记录没有被提前清空（恢复入口只重做这一步）
    assert loop._pending_topic_rebuilds == ["topic-target"]
    # 恢复失败不是「用户停止」；也没有把模型正文当旧上下文回答发出去
    assert result.cancelled is False
    assert result.stopped_by == "system"
    assert topic_tool.executed == 1


async def test_empty_rebuilt_context_is_recoverable_failure():
    from agent.services import topic_recovery

    async def empty_rebuild(topic_id: str) -> str:
        return "   "

    adapter = FakeStreamAdapter(
        [
            _switch_script("c1"),
            StreamScript(text=ANSWER_MARKER + "\n不该出现的旧上下文回答"),
        ]
    )
    registry, _, _ = _registry()
    bridge = FakeTopicBridge(
        decisions=[{"decision": "approved"}], rebuild=empty_rebuild
    )
    loop = _make_loop(adapter, registry, bridge)
    bridge.loop = loop
    events = _record_events(loop.bus)

    result = await asyncio.wait_for(loop.run("换到目标话题再回答"), timeout=15)

    assert result.formal_answer is False
    assert loop.topic_phase is TopicPhase.RECOVERABLE_FAILURE
    assert _assistant(events, interim=False) == []
    record = loop._topic_recovery
    assert record is not None and record["failed_stage"] == "context_prepare"
    assert topic_recovery.get_record(bridge, record["record_id"])["status"] == "pending"


# ---------------------------------------------------------------------------
# 6) 迟到的话题工具调用（ANSWERABLE 之后）：不执行、不改归属、可见警告
# ---------------------------------------------------------------------------


# F01（2026-11-10 第三轮）：普通工具调用**不**再打开 ANSWERABLE。因此
# 「普通检索 → 话题工具」是正常决策链，不是迟到调用；迟到只发生在已经
# ANSWERABLE（决策已解决 / 正式答案已发布）之后。
async def test_topic_tool_call_after_ordinary_retrieval_is_not_late():
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                text_chunks=["我先查一下资料。"],
                tool_calls=[
                    ScriptedToolCall(
                        id="c1", name="lookup", arguments={"query": "x"}
                    )
                ],
            ),
            StreamScript(
                tool_calls=[
                    ScriptedToolCall(
                        id="c2",
                        name="switch_topic",
                        arguments={"topic_id": "topic-target"},
                    )
                ],
            ),
            StreamScript(text=ANSWER_MARKER + "\n就在当前话题回答"),
        ]
    )
    registry, topic_tool, lookup_tool = _registry()
    bridge = FakeTopicBridge()
    loop = _make_loop(adapter, registry, bridge)
    bridge.loop = loop
    events = _record_events(loop.bus)

    result = await asyncio.wait_for(loop.run("先查资料再决定换不换话题"), timeout=15)

    assert lookup_tool.executed == 1
    assert len(bridge.requests) == 1, "普通工具调用之后的话题工具必须正常进入决策链"
    assert topic_tool.executed == 0, "用户拒绝后话题工具不得执行"
    assert result.final_content == "就在当前话题回答"
    assert not any(
        code == "answer_declared_with_tool_calls" for code in _warning_codes(events)
    ), "普通检索之后的话题工具不是迟到调用"
    assert loop.topic_phase is TopicPhase.ANSWERABLE


async def test_late_topic_tool_call_after_answerable_is_not_executed():
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                tool_calls=[
                    ScriptedToolCall(
                        id="c1", name="switch_topic", arguments={"topic_id": "topic-target"}
                    )
                ],
            ),
            StreamScript(
                text_chunks=[ANSWER_MARKER + "\n最终回答"],
                tool_calls=[
                    ScriptedToolCall(
                        id="c2",
                        name="switch_topic",
                        arguments={"topic_id": "topic-target"},
                    )
                ],
            ),
        ]
    )
    registry, topic_tool, _lookup = _registry()
    bridge = FakeTopicBridge()
    loop = _make_loop(adapter, registry, bridge)
    bridge.loop = loop
    events = _record_events(loop.bus)

    result = await asyncio.wait_for(loop.run("换个话题再回答"), timeout=15)

    assert result.final_content == "最终回答"
    assert topic_tool.executed == 0, "ANSWERABLE 之后迟到的导航请求不得执行"
    assert len(bridge.requests) == 1, "迟到的导航请求不再进入申请流程"
    assert bridge.rebuilds == []
    warnings = [
        event.data
        for event in events
        if getattr(event, "type", None) == EventType.WARNING
        and (event.data or {}).get("code") == "answer_declared_with_tool_calls"
    ]
    assert warnings, "迟到工具调用必须变成可见警告"
    assert loop.topic_phase is TopicPhase.ANSWERABLE


# ---------------------------------------------------------------------------
# 8) 回答生成失败（话题已切换、上下文已就绪）：单独恢复，不冒充整轮失败
# ---------------------------------------------------------------------------


class _AnswerFailsAdapter(FakeStreamAdapter):
    """第二次模型调用（生成回答）抛厂商故障。"""

    async def stream(self, messages, tools, **kwargs):
        if self.requests:
            raise RuntimeError("provider down while answering")
        async for delta in super().stream(messages, tools, **kwargs):
            yield delta


async def test_answer_failure_after_context_ready_is_separately_recoverable():
    from agent.services import topic_recovery

    adapter = _AnswerFailsAdapter([_switch_script("c1")])
    registry, topic_tool, _ = _registry()
    bridge = FakeTopicBridge(decisions=[{"decision": "approved"}])
    loop = _make_loop(adapter, registry, bridge)
    bridge.loop = loop
    events = _record_events(loop.bus)

    result = await asyncio.wait_for(loop.run("换到目标话题再回答"), timeout=15)

    assert result.final_content is None, "回答失败不得发布正式回答"
    assert result.formal_answer is False
    assert loop.topic_phase is TopicPhase.RECOVERABLE_FAILURE
    assert topic_tool.executed == 1
    assert bridge.rebuilds == ["topic-target"]
    errors = [
        event.data
        for event in events
        if getattr(event, "type", None) == EventType.ERROR
        and (event.data or {}).get("code") == "topic_answer_failed"
    ]
    assert errors, "回答失败必须可观察（topic_answer_failed）"
    recovery_events = [
        event.data
        for event in events
        if getattr(event, "type", None) == EventType.TOPIC_PHASE
        and (event.data or {}).get("phase") == TopicPhase.RECOVERABLE_FAILURE.value
    ]
    assert recovery_events
    assert recovery_events[-1]["recovery"]["failed_stage"] == "answer"
    record = errors[0]["recovery"]
    assert record["failed_stage"] == "answer"
    stored = topic_recovery.get_record(bridge, record["record_id"])
    assert stored is not None and stored["status"] == "pending"
    assert stored["failed_stage"] == "answer"
    assert stored["topic_id"] == "topic-target"
    assert _assistant(events, interim=False) == []

# ---------------------------------------------------------------------------
# 7) text 兼容档：正文是协议块时不得当作过程说明/正式回答展示
# ---------------------------------------------------------------------------


class _TextScriptedAdapter:
    """text 兼容档替身：content 是（可能带协议的）整段文本，tools=[] 时不解析。"""

    mode = "text"
    model = "fake-text"
    supports_stream = False

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, tools, **kwargs):
        self.calls += 1
        if self.calls == 1:
            content = json.dumps(
                {
                    "tool_calls": [
                        {"name": "lookup", "arguments": {"query": "x"}}
                    ]
                },
                ensure_ascii=False,
            )
            from agent.adapters.base import ChatMessage, Completion, ToolCall

            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content=content,
                    tool_calls=[ToolCall(id="c1", name="lookup", arguments={"query": "x"})],
                ),
                finish_reason="tool_calls",
            )
        from agent.adapters.base import ChatMessage, Completion

        return Completion(
            message=ChatMessage(
                role="assistant", content=ANSWER_MARKER + "\n文字档回答", tool_calls=None
            ),
            finish_reason="stop",
        )


async def test_text_mode_never_publishes_protocol_block_as_body():
    adapter = _TextScriptedAdapter()
    registry, _, lookup_tool = _registry()
    bridge = FakeTopicBridge()
    loop = _make_loop(adapter, registry, bridge)
    bridge.loop = loop
    events = _record_events(loop.bus)

    result = await asyncio.wait_for(loop.run("文字档问题"), timeout=15)

    assert result.final_content == "文字档回答"
    assert lookup_tool.executed == 1
    assert all(
        "tool_calls" not in (item.get("content") or "") for item in _assistant(events)
    ), "text 兼容档的协议块不得当作正文展示"
    formal = _assistant(events, interim=False)
    assert [item["content"] for item in formal] == ["文字档回答"]
