# -*- coding: utf-8 -*-
"""P1R-F 独立回归：P1-02 阶段事件与正式正文的**相对顺序**（跨端契约）。

只证明接线与事件顺序，不声称真实模型准确率。

契约依据：
* 契约 §3：DECISION_OPEN 期间不得发布任何正式正文；响应不含话题工具调用 →
  交付缓冲正文（一次性 streaming=false），“随后本轮进入 ANSWERABLE”；
* 契约 §8.4：前端只有后端进入可回答阶段才展示正式回答，且不得靠隐藏已泄露事件充数。
两条同时成立时，事件流里的顺序必须是
    TOPIC_PHASE(decision_open) → TOPIC_PHASE(answerable) → ASSISTANT(interim=false)
否则前端正文门（frontend/src/stores/events.ts 的 acceptsTopicFormalAnswer）会丢弃
该轮唯一的正式回答。

本文件是 F 的独立验证（不改实现者测试）：当前 B 的实现若仍是
“先 ASSISTANT(interim=false) 再开 answerable”，本文件第 1 条会红，修好后转绿。

运行：
    cd backend; .venv\\Scripts\\python.exe -m pytest tests/test_p1r_f_phase_order.py -q
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


class _SwitchTool(Tool):
    name = "switch_topic"
    description = "fake topic switch"
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.executed = 0

    async def run(self, **kwargs):  # noqa: ANN003
        self.executed += 1
        return ToolResult(ok=True, content="已切换")


class _Bridge:
    """最小桥替身：只提供 loop 会调用的阶段/审批/重建接口（不碰数据库）。"""

    def __init__(self, *, decisions: list[dict] | None = None) -> None:
        self.loop: AgentLoop | None = None
        self.decisions = list(decisions or [])
        self.requests: list = []
        self.target: str | None = None

    def set_phase(self, phase: TopicPhase) -> None:
        if self.loop is not None:
            self.loop.set_topic_phase(phase)

    async def before_tool(self, call):  # noqa: ANN001
        if call.name not in ("switch_topic", "create_topic"):
            return None
        self.requests.append(call)
        self.set_phase(TopicPhase.AWAITING_CONFIRMATION)
        decision = self.decisions.pop(0) if self.decisions else {"decision": "rejected"}
        if decision.get("decision") != "approved":
            self.set_phase(TopicPhase.ANSWERABLE)
            return ToolResult(ok=False, content="用户拒绝了这个话题申请")
        self.set_phase(TopicPhase.EXECUTING)
        self.target = str(call.arguments.get("topic_id") or "")
        return None

    async def after_tool(self, call, result):  # noqa: ANN001
        if call.name not in ("switch_topic", "create_topic"):
            return None
        if not result.ok:
            self.set_phase(TopicPhase.ANSWERABLE)
            return None
        return {
            "rebind_topic_id": self.target,
            "operation_id": "op-f-order",
            "target_topic_id": self.target,
            "stages": {"tool_success": True, "navigation_applied": True},
        }

    async def build_system(self, topic_id: str) -> str:
        return "SYS_FOR:" + topic_id

    def current_operation(self):  # noqa: ANN201
        return None


def _events(bus: EventBus) -> list:
    captured: list = []
    original = bus.publish

    async def spy(event):  # noqa: ANN001
        captured.append(event)
        await original(event)

    bus.publish = spy  # type: ignore[method-assign]
    return captured


def _kind_events(events: list, kind: EventType, **match) -> list[dict]:
    out: list[dict] = []
    for event in events:
        if getattr(event, "type", None) != kind:
            continue
        data = dict(getattr(event, "data", None) or {})
        if all(data.get(key) == value for key, value in match.items()):
            out.append(data)
    return out


def _index_of(events: list, predicate) -> int | None:  # noqa: ANN001
    for index, event in enumerate(events):
        if predicate(event):
            return index
    return None


def _make_loop(adapter, registry, bridge) -> AgentLoop:  # noqa: ANN001
    return AgentLoop(
        adapter,
        registry,
        EventBus(),
        topic_application=bridge,
        turn_id="turn_f_order",
    )


async def test_plain_answer_is_published_after_phase_answerable():
    """不含话题工具调用的 gated 回答：必须先把阶段推进到 answerable，再发正式正文。"""
    adapter = FakeStreamAdapter(
        [StreamScript(text_chunks=[ANSWER_MARKER + "\n普通回答", "，补充"])]
    )
    registry = ToolRegistry()
    bridge = _Bridge()
    loop = _make_loop(adapter, registry, bridge)
    bridge.loop = loop
    events = _events(loop.bus)

    result = await asyncio.wait_for(loop.run("普通问题"), timeout=15)

    assert result.formal_answer is True
    answer_index = _index_of(
        events,
        lambda event: getattr(event, "type", None) == EventType.ASSISTANT
        and (getattr(event, "data", None) or {}).get("interim") is False,
    )
    answerable_index = _index_of(
        events,
        lambda event: getattr(event, "type", None) == EventType.TOPIC_PHASE
        and (getattr(event, "data", None) or {}).get("phase") == TopicPhase.ANSWERABLE.value,
    )
    assert answer_index is not None, "gated 普通回答必须有一份 interim=false 正式正文事件"
    assert answerable_index is not None, "必须发出 TOPIC_PHASE(answerable)"
    assert answerable_index < answer_index, (
        "事件顺序错误：TOPIC_PHASE(answerable) 必须在正式回答之前发出。"
        "否则前端正文门（acceptsTopicFormalAnswer 只在 answerable 放行）会丢弃该轮"
        "唯一的正式回答（契约 §3 + §8.4）。当前顺序：answerable=#%d, "
        "interim=false ASSISTANT=#%d；事件=%s"
        % (
            answerable_index,
            answer_index,
            [
                (
                    getattr(event, "type", None).value
                    if getattr(event, "type", None)
                    else None,
                    (getattr(event, "data", None) or {}).get("phase")
                    or (getattr(event, "data", None) or {}).get("interim"),
                )
                for event in events
            ],
        )
    )


async def test_topic_tool_response_text_is_not_published_at_all():
    """含话题工具调用的响应：正文整段丢弃（既不能当回答，也不能改名成过程说明）。"""
    pre_tool_text = "我先确认一下再切换。"
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                text_chunks=[pre_tool_text],
                tool_calls=[
                    ScriptedToolCall(
                        id="c1",
                        name="switch_topic",
                        arguments={"topic_id": "topic-target"},
                    )
                ],
            ),
            StreamScript(text_chunks=[ANSWER_MARKER + "\n原话题回答"]),
        ]
    )
    registry = ToolRegistry()
    registry.register(_SwitchTool())
    bridge = _Bridge(decisions=[{"decision": "rejected"}])
    loop = _make_loop(adapter, registry, bridge)
    bridge.loop = loop
    events = _events(loop.bus)

    result = await asyncio.wait_for(loop.run("先切过去再回答"), timeout=15)

    published = [
        dict(getattr(event, "data", None) or {})
        for event in events
        if getattr(event, "type", None) == EventType.ASSISTANT
        and pre_tool_text in str((getattr(event, "data", None) or {}).get("content") or "")
    ]
    assert published == [], (
        "含话题工具调用的响应正文被发布了（契约 §3 要求整段丢弃，不得改名成过程说明）：%s"
        % published
    )
    # 拒绝后：在原话题恰好一份**逻辑**正式回答。
    # 允许同一 delta_id 的「流式累计快照 + 收尾校准」两条事件（前端按 delta_id 就地归位），
    # 但不允许出现第二份不同内容 / 不同 delta 的正式回答。
    formal = _kind_events(events, EventType.ASSISTANT, interim=False)
    assert formal, "拒绝后必须在原话题给出正式回答"
    assert {str(item.get("content") or "") for item in formal} == {"原话题回答"}, formal
    if len(formal) > 1:
        assert len({str(item.get("delta_id") or "") for item in formal}) == 1, (
            "多份正式回答事件必须只是同一 delta_id 的累计快照校准，不得是两次回答：%s" % formal
        )
    assert bridge.requests, "话题工具调用应当经过申请桥"
    assert result.formal_answer is True
