# -*- coding: utf-8 -*-
"""F01：普通工具调用不得过早打开可回答阶段（A 角色定向测试）。

只证明**接线与分支衔接**（fake/mock provider，不联网），不声称真实模型准确率。

固定住的验收点（契约 §3.3 第 128-131 行）：

* 响应只有普通工具调用 / 过程说明时**不**打开 ANSWERABLE，保持 DECISION_OPEN；
* 普通检索期间零正式正文泄露（正文按既有过程区口径交付，含 interim=true）；
* 后续响应里的合理话题工具调用**不被当成迟到调用**（照常进入申请 / 执行）；
* 只有真正准备发布正式答案（无工具调用 / 已声明回答）且前提满足时才 ANSWERABLE，
  并保证 TOPIC_PHASE(answerable) 先于正式正文事件；
* 已声明回答 + 普通工具调用：不执行工具、按已声明回答收尾（既有语义保持）；
* 有未完成话题操作（keep_decision_open / 分步事实未齐）时不得发布正式正文。
"""

from __future__ import annotations

import asyncio

import pytest

from agent.adapters.fake import FakeStreamAdapter, ScriptedToolCall, StreamScript
from agent.api.bus import EventBus
from agent.api.events import EventType
from agent.core.loop import AgentLoop
from agent.services.topic_operation import TopicPhase
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
    """只读检索类普通工具（F01 场景里的「普通工具调用」）。"""

    name = "lookup"
    description = "fake read-only lookup"
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.executed = 0

    async def run(self, **kwargs):
        self.executed += 1
        return ToolResult(ok=True, content="查到的资料")


class FakeTopicBridge:
    """够用的桥替身：审批决定、阶段回调、上下文重建、操作身份。

    keep_open_on_after：after_tool 成功时回传 keep_decision_open=True（模拟
    查重冲突待确认 / 同版本等待合法重试的未完成操作）。
    stages_on_after：after_tool 成功 directive 里的分步事实（默认返回完整三步）。
    """

    def __init__(
        self,
        *,
        decisions=None,
        keep_open_on_after: bool = False,
        stages_on_after: dict | None = None,
        rebind_on_success: bool = True,
    ) -> None:
        self.loop = None
        self.decisions = list(decisions or [])
        self.requests: list = []
        self.rebuilds: list[str] = []
        self.keep_open_on_after = bool(keep_open_on_after)
        self.rebind_on_success = bool(rebind_on_success)
        self.stages_on_after = stages_on_after
        self.target: str | None = None
        self.events_len_at_request: int | None = None
        self._events_ref: list | None = None

    def set_phase(self, phase: TopicPhase) -> None:
        if self.loop is not None:
            self.loop.set_topic_phase(phase)

    async def before_tool(self, call):
        if call.name not in ("switch_topic", "create_topic"):
            return None
        self.requests.append(call)
        self.events_len_at_request = len(self._events_ref or [])
        self.set_phase(TopicPhase.AWAITING_CONFIRMATION)
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
        stages = (
            dict(self.stages_on_after)
            if self.stages_on_after is not None
            else {
                "tool_success": True,
                "navigation_applied": True,
                "turn_ownership": True,
            }
        )
        directive = {
            "operation_id": "op-r14-a-f01",
            "target_topic_id": self.target,
            "stages": stages,
            "keep_decision_open": self.keep_open_on_after,
        }
        if self.rebind_on_success and self.target:
            directive["rebind_topic_id"] = self.target
        return directive

    async def build_system(self, topic_id: str) -> str:
        self.rebuilds.append(topic_id)
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


def _phases(events: list) -> list[str]:
    return [
        str((getattr(event, "data", None) or {}).get("phase"))
        for event in events
        if getattr(event, "type", None) == EventType.TOPIC_PHASE
    ]


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


def _index_of(events: list, predicate) -> int | None:
    for index, event in enumerate(events):
        if predicate(event):
            return index
    return None


def _answerable_index(events: list) -> int | None:
    return _index_of(
        events,
        lambda event: getattr(event, "type", None) == EventType.TOPIC_PHASE
        and (event.data or {}).get("phase") == TopicPhase.ANSWERABLE.value,
    )


def _first_formal_index(events: list) -> int | None:
    return _index_of(
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


def _make_loop(adapter, registry, bridge, events_ref: list | None = None) -> AgentLoop:
    loop = AgentLoop(
        adapter,
        registry,
        EventBus(),
        topic_application=bridge,
        turn_id="turn_r14_a_f01",
    )
    bridge.loop = loop
    bridge._events_ref = events_ref
    return loop


def _ordinary_call(call_id: str = "c1") -> ScriptedToolCall:
    return ScriptedToolCall(id=call_id, name="lookup", arguments={"query": "资料"})


def _switch_call(call_id: str = "c2", target: str = "topic-target") -> ScriptedToolCall:
    return ScriptedToolCall(
        id=call_id, name="switch_topic", arguments={"topic_id": target}
    )


# ---------------------------------------------------------------------------
# 1) 普通检索 → 话题工具：检索期间不打开 ANSWERABLE，话题工具不是迟到调用
# ---------------------------------------------------------------------------


async def test_ordinary_tool_call_keeps_decision_open_and_topic_call_is_not_late():
    adapter = FakeStreamAdapter(
        [
            StreamScript(text_chunks=["我先查一下资料。"], tool_calls=[_ordinary_call()]),
            StreamScript(tool_calls=[_switch_call()]),
            StreamScript(text=ANSWER_MARKER + "\n就在当前话题回答"),
        ]
    )
    registry, topic_tool, lookup_tool = _registry()
    bridge = FakeTopicBridge(decisions=[{"decision": "rejected"}])
    loop = _make_loop(adapter, registry, bridge)
    events = _record_events(loop.bus)
    bridge._events_ref = events

    result = await asyncio.wait_for(loop.run("先查资料，再决定要不要换话题"), timeout=15)

    # 普通检索真实执行了一次
    assert lookup_tool.executed == 1
    # 第二次响应里的话题工具**照常进入申请流程**（不是迟到调用）
    assert len(bridge.requests) == 1, "普通工具调用后的合理话题工具调用被当成迟到了"
    assert topic_tool.executed == 0, "用户拒绝后话题工具不得执行"
    assert not any(
        "answer_declared_with_tool_calls" == code for code in _warning_codes(events)
    ), "合理话题工具调用不应变成迟到工具警告"
    # 拒绝 → 在原话题回答（恰好一份正式回答）
    assert result.final_content == "就在当前话题回答"
    assert result.formal_answer is True
    formal = _assistant(events, interim=False)
    assert {item["content"] for item in formal} == {"就在当前话题回答"}
    # 普通检索正文只进过程区（interim=true），不是正式正文
    assert all(
        "我先查一下资料" not in (item.get("content") or "")
        for item in formal
    ), "普通检索的过程说明泄露进了正式回答区"
    assert any(
        "我先查一下资料" in (item.get("content") or "")
        for item in _assistant(events, interim=True)
    ), "普通工具轮的过程说明应进过程区"
    # ANSWERABLE 只能在话题决策处理之后才有（普通检索期间不得打开）。
    # 话题申请被拦截那一刻的事件数就是「普通检索结束、话题决策开始」的位置：
    # 若普通工具调用后就打开了 ANSWERABLE，它的下标会落在该位置之前。
    answerable_at = _answerable_index(events)
    assert answerable_at is not None
    request_at = bridge.events_len_at_request
    assert request_at is not None
    assert answerable_at > request_at, (
        "普通工具调用期间就打开了 ANSWERABLE（F01 旧问题）"
    )
    # 事件顺序：ANSWERABLE 先于正式正文
    formal_at = _first_formal_index(events)
    assert formal_at is not None and answerable_at < formal_at
    # 阶段序列首项必须是 decision_open
    assert _phases(events)[0] == TopicPhase.DECISION_OPEN.value


# ---------------------------------------------------------------------------
# 2) 普通检索 → 纯文本回答：仍按既有语义在检索之后一次性交付正式回答
# ---------------------------------------------------------------------------


async def test_plain_answer_after_ordinary_tool_call_is_published_once():
    adapter = FakeStreamAdapter(
        [
            StreamScript(text_chunks=["我先查一下。"], tool_calls=[_ordinary_call()]),
            StreamScript(text=ANSWER_MARKER + "\n查到的资料是……"),
        ]
    )
    registry, _, lookup_tool = _registry()
    bridge = FakeTopicBridge()
    loop = _make_loop(adapter, registry, bridge)
    events = _record_events(loop.bus)

    result = await asyncio.wait_for(loop.run("查一下资料再回答"), timeout=15)

    assert lookup_tool.executed == 1
    assert result.final_content == "查到的资料是……"
    formal = _assistant(events, interim=False)
    assert len(formal) == 1, "正式回答只允许一份"
    assert formal[0]["streaming"] is False, "决策期缓冲后一次性交付，不冒充流式"
    answerable_at = _answerable_index(events)
    formal_at = _first_formal_index(events)
    assert answerable_at is not None and formal_at is not None
    assert answerable_at < formal_at


# ---------------------------------------------------------------------------
# 3) 纯文本回答（无工具）：仍是一次性交付 + ANSWERABLE（既有 P1-02 语义保持）
# ---------------------------------------------------------------------------


async def test_plain_answer_without_tools_still_delivers_after_answerable():
    adapter = FakeStreamAdapter([StreamScript(text_chunks=[ANSWER_MARKER + "\n普通回答"])])
    registry, _, _ = _registry()
    bridge = FakeTopicBridge()
    loop = _make_loop(adapter, registry, bridge)
    events = _record_events(loop.bus)

    result = await asyncio.wait_for(loop.run("普通问题"), timeout=15)

    assert result.final_content == "普通回答"
    formal = _assistant(events, interim=False)
    assert len(formal) == 1 and formal[0]["content"] == "普通回答"
    assert _phases(events) == [
        TopicPhase.DECISION_OPEN.value,
        TopicPhase.ANSWERABLE.value,
    ]
    answerable_at = _answerable_index(events)
    formal_at = _first_formal_index(events)
    assert answerable_at is not None and formal_at is not None
    assert answerable_at < formal_at


# ---------------------------------------------------------------------------
# 4) 已声明回答 + 普通工具调用：不执行工具、按已声明回答收尾（既有语义保持）
# ---------------------------------------------------------------------------


async def test_declared_answer_with_ordinary_tool_calls_is_not_executed():
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                text=ANSWER_MARKER + "\n最终回答",
                tool_calls=[_ordinary_call()],
            )
        ]
    )
    registry, _, lookup_tool = _registry()
    bridge = FakeTopicBridge()
    loop = _make_loop(adapter, registry, bridge)
    events = _record_events(loop.bus)

    result = await asyncio.wait_for(loop.run("直接回答"), timeout=15)

    assert lookup_tool.executed == 0, "声明回答之后的工具调用不得执行"
    assert result.final_content == "最终回答"
    assert result.formal_answer is True
    assert "answer_declared_with_tool_calls" in _warning_codes(events)
    answerable_at = _answerable_index(events)
    formal_at = _first_formal_index(events)
    assert answerable_at is not None and formal_at is not None
    assert answerable_at < formal_at


# ---------------------------------------------------------------------------
# 5) 未完成话题操作（keep_decision_open）：不得发布正式正文
# ---------------------------------------------------------------------------


async def test_unfinished_topic_operation_blocks_formal_answer():
    adapter = FakeStreamAdapter(
        [
            StreamScript(tool_calls=[_switch_call()]),
            StreamScript(text=ANSWER_MARKER + "\n不该出现的回答"),
        ]
    )
    registry, topic_tool, _ = _registry()
    bridge = FakeTopicBridge(
        decisions=[{"decision": "approved"}],
        keep_open_on_after=True,
        rebind_on_success=False,
    )
    loop = _make_loop(adapter, registry, bridge)
    events = _record_events(loop.bus)

    result = await asyncio.wait_for(loop.run("换个话题再回答"), timeout=15)

    assert topic_tool.executed == 1, "话题工具本身真实执行过（不是拒绝）"
    assert _assistant(events, interim=False) == [], "未完成话题操作时不得发布正式正文"
    assert result.final_content is None
    assert result.formal_answer is False
    assert loop.topic_phase is not TopicPhase.ANSWERABLE


# ---------------------------------------------------------------------------
# 6) 分步事实未齐（工具成功但归属/上下文未完成）：同样不得发布正式正文
# ---------------------------------------------------------------------------


async def test_incomplete_topic_stages_block_formal_answer():
    adapter = FakeStreamAdapter(
        [
            StreamScript(tool_calls=[_switch_call()]),
            StreamScript(text=ANSWER_MARKER + "\n不该出现的回答"),
        ]
    )
    registry, topic_tool, _ = _registry()
    bridge = FakeTopicBridge(
        decisions=[{"decision": "approved"}],
        stages_on_after={"tool_success": True, "navigation_applied": True},
        rebind_on_success=False,
    )
    loop = _make_loop(adapter, registry, bridge)
    events = _record_events(loop.bus)

    result = await asyncio.wait_for(loop.run("换个话题再回答"), timeout=15)

    assert topic_tool.executed == 1
    assert _assistant(events, interim=False) == [], "分步事实未齐时不得发布正式正文"
    assert result.formal_answer is False
