# -*- coding: utf-8 -*-
"""F06：归属失败阻止正式回答并进入可恢复失败（A 角色定向测试）。

只证明接线与分支衔接（fake provider，不联网），不声称真实模型准确率。

固定住的验收点（契约 §3.3 第 132-138 行）：

* 话题工具 / 导航已经成功，但归属搬迁失败（rebind 抛错 / 直接失败结果）或
  话题后置处理异常 → stages.failed_stage="ownership"、阶段 RECOVERABLE_FAILURE、
  写恢复记录（failed_stage="ownership"）、可观察 ERROR（code=topic_ownership_failed）、
  **零正式答案**（不发布正文、不再发起模型调用、不用旧 system 回答）；
* 非话题工具的普通后置异常保持既有容错（只 warning，不误标归属失败）；
* 话题工具本身失败（工具未成功）不冒充归属失败。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from agent.adapters.fake import FakeStreamAdapter, ScriptedToolCall, StreamScript
from agent.api.bus import EventBus
from agent.api.events import EventType
from agent.core.loop import AgentLoop
from agent.services import topic_recovery
from agent.services.topic_operation import TopicPhase
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry

ANSWER_MARKER = "[[QIO:ANSWER]]"


class _TopicTool(Tool):
    name = "switch_topic"
    description = "fake topic switch"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, ok: bool = True) -> None:
        self.executed = 0
        self.ok = ok

    async def run(self, **kwargs):
        self.executed += 1
        return ToolResult(ok=self.ok, content="已切换到目标话题" if self.ok else "",
                          error=None if self.ok else "switch failed")


class _NormalTool(Tool):
    name = "lookup"
    description = "fake normal tool"
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.executed = 0

    async def run(self, **kwargs):
        self.executed += 1
        return ToolResult(ok=True, content="资料")


class _Operation:
    """current_operation() 的最小事实替身。"""

    def __init__(self) -> None:
        self.operation_id = "op-r14-a-f06"
        self.param_version = 1
        self.base_nav_version = 7
        self.from_topic = "topic-old"
        self.target_topic_id = "topic-target"
        self.user_message_id = "msg-user-1"
        self.status = SimpleNamespace(value="executed")
        self.conflict = SimpleNamespace(value="none")


class _OwnershipFailingBridge:
    """话题工具成功、但归属搬迁失败的桥替身。

    mode:
      * after_raises —— after_tool 直接抛异常（后置回调异常注入）；
      * directive —— after_tool 返回带 ownership_failed 的 directive；
      * legacy_directive —— 旧形态：只有 replacement ToolResult(code=rebind_failed)
        与 stages.failed_stage="ownership"。
    """

    def __init__(self, mode: str = "after_raises", *, normal_after_raises: bool = False) -> None:
        self.mode = mode
        self.normal_after_raises = normal_after_raises
        self.loop = None
        # 真实事实：话题工具成功后 Anchor 已经落在目标话题（导航已发生），
        # 归属搬迁才失败 —— 目标话题可以从真实 Anchor 读到。
        self.app = SimpleNamespace(
            navigation=SimpleNamespace(
                anchors=SimpleNamespace(
                    get_active=lambda: SimpleNamespace(topic_id="topic-target")
                )
            )
        )
        self.ctx = SimpleNamespace(
            turn_id="turn_r14_a_f06",
            bound_topic="topic-old",
            bound_fragment_id="frag-old",
            user_message_id="msg-user-1",
        )
        self.rebuilds: list[str] = []

    def set_phase(self, phase: TopicPhase) -> None:
        if self.loop is not None:
            self.loop.set_topic_phase(phase)

    async def before_tool(self, call):
        if call.name not in ("switch_topic", "create_topic"):
            return None
        self.set_phase(TopicPhase.EXECUTING)
        return None

    async def after_tool(self, call, result):
        if call.name not in ("switch_topic", "create_topic"):
            if self.normal_after_raises:
                raise RuntimeError("normal tool after_tool boom")
            return None
        if self.mode == "after_raises":
            raise RuntimeError("rebind exploded")
        if self.mode == "directive":
            return {
                "operation_id": "op-r14-a-f06",
                "target_topic_id": "topic-target",
                "ownership_failed": True,
                "stages": {
                    "tool_success": True,
                    "navigation_applied": True,
                    "failed_stage": "ownership",
                    "failure": "rebind exploded",
                },
            }
        if self.mode == "legacy_directive":
            return {
                "operation_id": "op-r14-a-f06",
                "target_topic_id": "topic-target",
                "stages": {
                    "tool_success": True,
                    "navigation_applied": True,
                    "failed_stage": "ownership",
                },
                "result": ToolResult(
                    ok=False,
                    content="话题已变更，但本轮归属迁移失败",
                    error="话题已变更，但本轮归属迁移失败：rebind exploded",
                    code="rebind_failed",
                ),
            }
        return None

    async def build_system(self, topic_id: str) -> str:
        self.rebuilds.append(topic_id)
        return f"SYS_FOR:{topic_id}"

    def current_operation(self):
        return _Operation()


def _registry(topic_ok: bool = True):
    topic = _TopicTool(ok=topic_ok)
    normal = _NormalTool()
    registry = ToolRegistry()
    registry.register(topic)
    registry.register(normal)
    return registry, topic, normal


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


def _error_codes(events: list) -> list[str]:
    return [
        str((getattr(event, "data", None) or {}).get("code"))
        for event in events
        if getattr(event, "type", None) == EventType.ERROR
    ]


def _make_loop(adapter, registry, bridge) -> AgentLoop:
    loop = AgentLoop(
        adapter,
        registry,
        EventBus(),
        topic_application=bridge,
        turn_id="turn_r14_a_f06",
    )
    bridge.loop = loop
    return loop


def _switch_call(call_id: str = "c1") -> ScriptedToolCall:
    return ScriptedToolCall(
        id=call_id, name="switch_topic", arguments={"topic_id": "topic-target"}
    )


async def _run_failure(mode: str, *, topic_ok: bool = True):
    adapter = FakeStreamAdapter(
        [
            StreamScript(tool_calls=[_switch_call()]),
            StreamScript(text=ANSWER_MARKER + "\n不该出现的旧上下文回答"),
        ]
    )
    registry, topic_tool, _ = _registry(topic_ok=topic_ok)
    bridge = _OwnershipFailingBridge(mode)
    loop = _make_loop(adapter, registry, bridge)
    events = _record_events(loop.bus)
    result = await asyncio.wait_for(loop.run("换个话题再回答"), timeout=15)
    return result, loop, bridge, events, topic_tool, adapter


async def test_after_tool_exception_after_success_is_ownership_failure():
    result, loop, bridge, events, topic_tool, adapter = await _run_failure("after_raises")

    assert topic_tool.executed == 1, "话题工具本身已经成功执行过"
    assert adapter._index == 1, "归属失败后不得再发起模型调用（不用旧 system 回答）"
    assert loop.topic_phase is TopicPhase.RECOVERABLE_FAILURE
    assert loop.topic_stages is not None
    assert loop.topic_stages.failed_stage == "ownership"
    assert loop.topic_stages.tool_success is True
    assert _assistant(events, interim=False) == [], "归属失败必须零正式答案"
    assert result.final_content is None
    assert result.formal_answer is False
    assert result.stopped_by == "system"
    assert "topic_ownership_failed" in _error_codes(events)
    error = [
        event.data
        for event in events
        if getattr(event, "type", None) == EventType.ERROR
        and (event.data or {}).get("code") == "topic_ownership_failed"
    ][0]
    assert error["recoverable"] is True
    assert error["failed_stage"] == "ownership"
    assert error["operation_id"] == "op-r14-a-f06"
    recovery = error["recovery"]
    assert recovery["failed_stage"] == "ownership"
    assert recovery["retryable"] is True
    stored = topic_recovery.get_record(bridge.app, recovery["record_id"])
    assert stored is not None
    assert stored["status"] == "pending"
    assert stored["failed_stage"] == "ownership"
    assert stored["turn_id"] == "turn_r14_a_f06"
    assert stored["user_message_id"] == "msg-user-1"
    assert stored["operation_id"] == "op-r14-a-f06"
    assert stored["base_nav_version"] == 7
    assert stored["topic_id"] == "topic-target"
    assert bridge.rebuilds == [], "归属失败后不得重建目标上下文"


async def test_structured_ownership_failed_directive_blocks_answer():
    result, loop, bridge, events, _topic_tool, adapter = await _run_failure("directive")

    assert adapter._index == 1
    assert loop.topic_phase is TopicPhase.RECOVERABLE_FAILURE
    assert loop.topic_stages.failed_stage == "ownership"
    assert _assistant(events, interim=False) == []
    assert result.formal_answer is False and result.final_content is None
    assert "topic_ownership_failed" in _error_codes(events)


async def test_legacy_rebind_failed_result_is_recognised_as_ownership_failure():
    result, loop, bridge, events, _topic_tool, adapter = await _run_failure("legacy_directive")

    assert adapter._index == 1
    assert loop.topic_phase is TopicPhase.RECOVERABLE_FAILURE
    assert loop.topic_stages.failed_stage == "ownership"
    assert _assistant(events, interim=False) == []
    assert result.formal_answer is False
    assert "topic_ownership_failed" in _error_codes(events)


async def test_normal_tool_after_tool_exception_keeps_legacy_tolerance():
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                text_chunks=["我先查一下。"],
                tool_calls=[ScriptedToolCall(id="c1", name="lookup", arguments={})],
            ),
            StreamScript(text=ANSWER_MARKER + "\n正常回答"),
        ]
    )
    registry, _topic_tool, normal_tool = _registry()
    bridge = _OwnershipFailingBridge("after_raises", normal_after_raises=True)
    loop = _make_loop(adapter, registry, bridge)
    events = _record_events(loop.bus)

    result = await asyncio.wait_for(loop.run("查一下再回答"), timeout=15)

    assert normal_tool.executed == 1
    assert result.final_content == "正常回答", "非话题工具后置异常仍应容错回答"
    assert loop.topic_phase is TopicPhase.ANSWERABLE
    assert "topic_ownership_failed" not in _error_codes(events)
    assert any("后置处理失败" in item for item in result.warnings)


async def test_topic_tool_failure_is_not_misreported_as_ownership_failure():
    adapter = FakeStreamAdapter(
        [
            StreamScript(tool_calls=[_switch_call()]),
            StreamScript(text=ANSWER_MARKER + "\n在当前话题回答"),
        ]
    )
    registry, topic_tool, _ = _registry(topic_ok=False)
    bridge = _OwnershipFailingBridge("after_raises")
    loop = _make_loop(adapter, registry, bridge)
    events = _record_events(loop.bus)

    result = await asyncio.wait_for(loop.run("换个话题再回答"), timeout=15)

    assert topic_tool.executed == 1
    assert "topic_ownership_failed" not in _error_codes(events), (
        "工具本身失败不是归属失败（工具未成功，不能标 ownership）"
    )
    stages = loop.topic_stages
    assert stages is None or stages.failed_stage != "ownership"
    assert result.final_content == "在当前话题回答"
