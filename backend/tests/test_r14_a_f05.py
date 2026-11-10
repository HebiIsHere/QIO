# -*- coding: utf-8 -*-
"""F05：决策依据形成时冻结真实位置（A 角色定向测试）。

只证明接线与事实传递（fake provider，不联网），不声称真实模型准确率。

固定住的验收点（契约 §3.1）：

* `TurnContext` 在 begin 落实绑定 / build_context 写入用户消息之后、首次模型调用
  之前一次性写入 decision_nav_version / decision_topic_id / decision_fragment_id /
  decision_queued；
* 快照冻结后，用户后来的手动导航不改变它（旧模型结果不得覆盖用户新选择）；
* 同轮话题操作真实导航成功后，loop 用 C 的明确方法做因果更新（权威字段来自
  after_tool directive，不二次读取当前版本、不猜值）；
* 归属失败 / 未真实导航时不更新快照；
* 旧测试替身缺字段时用 getattr 兼容。
"""

from __future__ import annotations

import asyncio

from agent.adapters.fake import FakeStreamAdapter, ScriptedToolCall, StreamScript
from agent.api.bus import EventBus
from agent.api.events import EventType
from agent.config import Settings
from agent.core.loop import AgentLoop
from agent.core.turn import TurnContext
from agent.credentials.store import MemoryKeyring
from agent.services import navigation_version
from agent.services.app import AppContext
from agent.services.topic_operation import TopicPhase
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry

ANSWER_MARKER = "[[QIO:ANSWER]]"


def _app(tmp_path) -> AppContext:
    conn = connect(tmp_path / "r14_a_f05.db")
    apply_migrations(conn)
    app = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app.credentials._kr = MemoryKeyring()
    return app


def _topic(app: AppContext, name: str) -> str:
    return app.topics.nodes.create_topic(name).id


# ---------------------------------------------------------------------------
# 1) 快照：真实导航版本 / 绑定话题 / 绑定片段 / 排队事实
# ---------------------------------------------------------------------------


def test_snapshot_freezes_real_position_before_first_model_call(tmp_path):
    app = _app(tmp_path)
    topic = _topic(app, "快照话题")
    fragment = app.fragments.get_or_create_open(topic).id
    app.navigation.enter_topic(topic)  # 一次真实导航 → 推进版本
    version = navigation_version.current(app.conn)

    ctx = TurnContext(turn_id="turn_snapshot", message="问题")
    ctx.user_message_id = "msg_snapshot"
    ctx.bound_topic = topic
    ctx.bound_fragment_id = fragment
    app.turn_orchestrator._snapshot_decision_context(ctx)

    assert ctx.decision_nav_version == version
    assert ctx.decision_topic_id == topic
    assert ctx.decision_fragment_id == fragment
    assert ctx.decision_queued is False
    assert ctx.user_message_id == "msg_snapshot"


def test_snapshot_is_frozen_against_later_manual_navigation(tmp_path):
    app = _app(tmp_path)
    first = _topic(app, "第一话题")
    second = _topic(app, "第二话题")
    first_fragment = app.fragments.get_or_create_open(first).id
    app.fragments.get_or_create_open(second)
    app.navigation.enter_topic(first)

    ctx = TurnContext(turn_id="turn_frozen", message="问题")
    ctx.user_message_id = "msg_frozen"
    ctx.bound_topic = first
    ctx.bound_fragment_id = first_fragment
    app.turn_orchestrator._snapshot_decision_context(ctx)
    frozen = (
        ctx.decision_nav_version,
        ctx.decision_topic_id,
        ctx.decision_fragment_id,
    )

    # 用户在本轮请求还没返回时手动切走（另一个请求里的真实导航）。
    app.navigation.enter_topic(second)
    assert navigation_version.current(app.conn) != frozen[0]

    # 快照不会被「当前 Anchor」悄悄改写：它就是本轮判断依据的真实位置。
    assert (
        ctx.decision_nav_version,
        ctx.decision_topic_id,
        ctx.decision_fragment_id,
    ) == frozen


def test_queued_turn_is_marked_and_keeps_submit_binding(tmp_path):
    app = _app(tmp_path)
    submitted_topic = _topic(app, "提交时话题")
    other_topic = _topic(app, "别人后开的话题")
    submitted_fragment = app.fragments.get_or_create_open(submitted_topic).id
    app.fragments.get_or_create_open(other_topic)

    # 受理时前面还有主 turn：这一轮就是排队消息（事实来自 TurnManager 的创建状态）。
    ctx = TurnContext(
        turn_id="turn_queued",
        message="排队的问题",
        initial_topic=submitted_topic,
        current_topic=submitted_topic,
        status="queued",
    )
    assert ctx.decision_queued is True
    ctx.user_message_id = "msg_queued"
    ctx.bound_topic = submitted_topic
    ctx.bound_fragment_id = submitted_fragment
    # 排队期间用户去了别的话题：快照仍使用提交时的绑定（不随当前 Anchor 重绑）。
    app.navigation.enter_topic(other_topic)
    app.turn_orchestrator._snapshot_decision_context(ctx)

    assert ctx.decision_queued is True
    assert ctx.decision_topic_id == submitted_topic
    assert ctx.decision_fragment_id == submitted_fragment


def test_snapshot_survives_missing_navigation_version(tmp_path, monkeypatch):
    """版本读不到时如实 None（不猜数字），其余快照事实仍然写入。"""
    app = _app(tmp_path)
    topic = _topic(app, "无版本话题")
    fragment = app.fragments.get_or_create_open(topic).id

    def _boom(conn):  # noqa: ANN001
        raise RuntimeError("navigation_version table missing")

    monkeypatch.setattr(navigation_version, "current", _boom)
    ctx = TurnContext(turn_id="turn_no_nav", message="问题")
    ctx.user_message_id = "msg_no_nav"
    ctx.bound_topic = topic
    ctx.bound_fragment_id = fragment
    app.turn_orchestrator._snapshot_decision_context(ctx)

    assert ctx.decision_nav_version is None
    assert ctx.decision_topic_id == topic
    assert ctx.decision_fragment_id == fragment


# ---------------------------------------------------------------------------
# 2) 因果更新：同轮操作真实导航成功后，用 C 的明确方法更新快照
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


class _CausalBridge:
    """记录因果更新调用（C 的 note_decision_navigation 的替身）。"""

    def __init__(self, *, stages: dict | None = None) -> None:
        self.loop = None
        self.note_calls: list[dict] = []
        self.rebuilds: list[str] = []
        self.stages = (
            dict(stages)
            if stages is not None
            else {
                "tool_success": True,
                "navigation_applied": True,
                "turn_ownership": True,
            }
        )

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
            return None
        if not result.ok:
            return None
        directive = {
            "operation_id": "op-r14-a-f05",
            "topic_id": "topic-target",
            "target_topic_id": "topic-target",
            "fragment_id": "frag-target",
            "nav_version": 42,
            "stages": dict(self.stages),
            "keep_decision_open": False,
        }
        if self.stages.get("turn_ownership"):
            directive["rebind_topic_id"] = "topic-target"
        return directive

    async def build_system(self, topic_id: str) -> str:
        self.rebuilds.append(topic_id)
        return f"SYS_FOR:{topic_id}"

    def note_decision_navigation(self, *, topic_id, fragment_id=None, nav_version=None):
        self.note_calls.append(
            {"topic_id": topic_id, "fragment_id": fragment_id, "nav_version": nav_version}
        )
        return {}

    def current_operation(self):
        return None


def _registry() -> tuple[ToolRegistry, _TopicTool]:
    topic = _TopicTool()
    registry = ToolRegistry()
    registry.register(topic)
    return registry, topic


def _record_events(bus: EventBus) -> list:
    events: list = []
    original = bus.publish

    async def spy(event):  # noqa: ANN001
        events.append(event)
        await original(event)

    bus.publish = spy  # type: ignore[method-assign]
    return events


def _make_loop(adapter, registry, bridge) -> AgentLoop:
    loop = AgentLoop(
        adapter,
        registry,
        EventBus(),
        topic_application=bridge,
        turn_id="turn_r14_a_f05",
    )
    bridge.loop = loop
    return loop


def _switch_call(call_id: str = "c1") -> ScriptedToolCall:
    return ScriptedToolCall(
        id=call_id, name="switch_topic", arguments={"topic_id": "topic-target"}
    )


async def test_successful_navigation_updates_snapshot_with_authoritative_facts():
    adapter = FakeStreamAdapter(
        [
            StreamScript(tool_calls=[_switch_call()]),
            StreamScript(text=ANSWER_MARKER + "\n目标话题回答"),
        ]
    )
    registry, topic_tool = _registry()
    bridge = _CausalBridge()
    loop = _make_loop(adapter, registry, bridge)

    result = await asyncio.wait_for(loop.run("换到目标话题"), timeout=15)

    assert topic_tool.executed == 1
    assert result.final_content == "目标话题回答"
    assert bridge.note_calls == [
        {"topic_id": "topic-target", "fragment_id": "frag-target", "nav_version": 42}
    ], "同轮真实导航成功后必须用桥的权威字段做一次因果更新"


async def test_failed_ownership_does_not_update_decision_snapshot():
    adapter = FakeStreamAdapter([StreamScript(tool_calls=[_switch_call()])])
    registry, topic_tool = _registry()
    bridge = _CausalBridge(
        stages={
            "tool_success": True,
            "navigation_applied": False,
            "turn_ownership": False,
        }
    )
    # 归属/导航未完成：不产生 directive 的 rebind，也没有 ownership_failed 标记 ——
    # 这类事实不得被当成「真实导航成功」去更新快照。
    bridge.after_tool_orig = bridge.after_tool

    async def _after_tool(call, result):
        directive = await bridge.after_tool_orig(call, result)
        if isinstance(directive, dict):
            directive.pop("rebind_topic_id", None)
        return directive

    bridge.after_tool = _after_tool
    loop = _make_loop(adapter, registry, bridge)

    await asyncio.wait_for(loop.run("换到目标话题"), timeout=15)

    assert bridge.note_calls == [], "未真实导航 / 未完成归属时不得更新决策快照"

# ---------------------------------------------------------------------------
# 3) 真实一轮：begin 快照被操作继承；成功导航后因果更新到真实结果
# ---------------------------------------------------------------------------


async def test_full_turn_uses_snapshot_and_updates_causally(tmp_path, monkeypatch):
    from agent.adapters.base import ChatMessage, Completion, ToolCall
    from agent.services.topic_application import TopicApplicationBridge
    from agent.tools.approval import ApprovalResult

    app = _app(tmp_path)
    old_topic = _topic(app, "旧话题")
    target_topic = _topic(app, "目标话题")
    old_fragment = app.fragments.get_or_create_open(old_topic).id
    app.fragments.get_or_create_open(target_topic)
    app.navigation.enter_topic(old_topic)
    base_version = navigation_version.current(app.conn)

    snapshots: list[tuple] = []
    original_snapshot = app.turn_orchestrator._snapshot_decision_context

    def _spy_snapshot(ctx):  # noqa: ANN001
        original_snapshot(ctx)
        snapshots.append(
            (ctx.decision_nav_version, ctx.decision_topic_id, ctx.decision_fragment_id)
        )

    monkeypatch.setattr(app.turn_orchestrator, "_snapshot_decision_context", _spy_snapshot)

    note_calls: list[dict] = []
    original_note = TopicApplicationBridge.note_decision_navigation

    def _spy_note(self, topic_id, fragment_id=None, nav_version=None):  # noqa: ANN001
        result = original_note(
            self, topic_id, fragment_id=fragment_id, nav_version=nav_version
        )
        note_calls.append(dict(result))
        return result

    monkeypatch.setattr(
        TopicApplicationBridge, "note_decision_navigation", _spy_note
    )

    payloads: list[dict] = []

    async def _fake_request(kind, payload):  # noqa: ANN001
        payloads.append(dict(payload))
        return ApprovalResult(f"appr_{len(payloads)}", "approved")

    monkeypatch.setattr(app.approvals, "request", _fake_request)

    class _ScriptedAdapter:
        mode = "native"
        model = "fake-r14-a-f05-turn"

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
            self.calls += 1
            if self.calls == 1:
                return Completion(
                    message=ChatMessage(
                        role="assistant",
                        content=None,
                        tool_calls=[
                            ToolCall(
                                id="c1",
                                name="switch_topic",
                                arguments={"topic_id": target_topic},
                            )
                        ],
                    )
                )
            return Completion(
                message=ChatMessage(
                    role="assistant", content=ANSWER_MARKER + "\n目标话题回答"
                )
            )

    adapter = _ScriptedAdapter()

    async def _fake_build():
        return adapter

    monkeypatch.setattr(app, "build_adapter", _fake_build)

    async def _fake_predict(turn_ctx, message, topic):  # noqa: ANN001
        from agent.services.predict import TopicPrediction

        return TopicPrediction(main_topic_id=old_topic, backend_used="rules")

    monkeypatch.setattr(app.turn_orchestrator, "_predict_topics", _fake_predict)

    result = await app.run_turn("换到目标话题并回答", topic_id=old_topic)

    assert result["ok"] is True
    assert snapshots and snapshots[0] == (base_version, old_topic, old_fragment), (
        "begin/build_context 之后必须先冻结真实位置快照"
    )
    assert payloads, "话题操作必须建立申请"
    payload = payloads[0]
    assert payload["from_topic_id"] == old_topic
    assert payload["from_fragment_id"] == old_fragment
    assert payload["base_nav_version"] == base_version, (
        "操作的起点必须继承决策快照，而不是工具返回时读当前版本"
    )
    assert app.navigation.anchors.get_active().topic_id == target_topic
    assert note_calls, "同轮真实导航成功后必须做因果更新"
    assert note_calls[-1]["topic_id"] == target_topic
    assert note_calls[-1]["nav_version"] == navigation_version.current(app.conn)

async def test_same_batch_sequential_navigation_uses_causal_snapshot(tmp_path, monkeypatch):
    """同一批两个 switch_topic：第一次真实导航后快照因果更新，第二次以新快照为起点。

    这验证 F05 的因果更新链路（A 写快照 → C 的 note 方法 → 下一个操作继承）；
    跨**模型响应**的连续导航受 P1-02「answerable 后迟到导航不执行」约束，不在本用例。
    """
    from agent.adapters.base import ChatMessage, Completion, ToolCall
    from agent.services.topic_application import TopicApplicationBridge

    app = _app(tmp_path)
    old_topic = _topic(app, "旧话题")
    first_topic = _topic(app, "目标话题一")
    second_topic = _topic(app, "目标话题二")
    for topic_id in (first_topic, second_topic):
        app.fragments.get_or_create_open(topic_id)
    app.navigation.enter_topic(old_topic)

    note_calls: list[dict] = []
    original_note = TopicApplicationBridge.note_decision_navigation

    def _spy_note(self, topic_id, fragment_id=None, nav_version=None):  # noqa: ANN001
        result = original_note(
            self, topic_id, fragment_id=fragment_id, nav_version=nav_version
        )
        note_calls.append(dict(result))
        return result

    monkeypatch.setattr(
        TopicApplicationBridge, "note_decision_navigation", _spy_note
    )

    class _ScriptedAdapter:
        mode = "native"
        model = "fake-r14-a-f05-batch"

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
            self.calls += 1
            if self.calls == 1:
                return Completion(
                    message=ChatMessage(
                        role="assistant",
                        content=None,
                        tool_calls=[
                            ToolCall(
                                id="c1",
                                name="switch_topic",
                                arguments={
                                    "topic_id": first_topic,
                                    "intent": "user_request",
                                },
                            ),
                            ToolCall(
                                id="c2",
                                name="switch_topic",
                                arguments={
                                    "topic_id": second_topic,
                                    "intent": "user_request",
                                },
                            ),
                        ],
                    )
                )
            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content=ANSWER_MARKER + "\n在目标话题二里的正式回答",
                )
            )

    adapter = _ScriptedAdapter()

    async def _fake_build():
        return adapter

    monkeypatch.setattr(app, "build_adapter", _fake_build)

    async def _fake_predict(turn_ctx, message, topic):  # noqa: ANN001
        from agent.services.predict import TopicPrediction

        return TopicPrediction(main_topic_id=old_topic, backend_used="rules")

    monkeypatch.setattr(app.turn_orchestrator, "_predict_topics", _fake_predict)

    result = await app.run_turn("本批先换到话题一再换到话题二", topic_id=old_topic)

    assert result["ok"] is True
    assert app.navigation.anchors.get_active().topic_id == second_topic, (
        "同批第二次导航应基于第一次成功后的新快照执行"
    )
    assert [call["topic_id"] for call in note_calls][:2] == [first_topic, second_topic]
    assert note_calls[0]["nav_version"] != note_calls[1]["nav_version"], (
        "第二次真实导航发生在第一次之后，快照版本必须推进"
    )
    assert result["turn"]["final_content"] == "在目标话题二里的正式回答"

async def test_success_directive_missing_causal_facts_is_observable():
    """成功 directive 缺 navigation_applied/turn_ownership 时不静默：可观察 WARNING。"""
    adapter = FakeStreamAdapter([StreamScript(tool_calls=[_switch_call()])])
    registry, _topic_tool = _registry()
    bridge = _CausalBridge(stages={"tool_success": True})
    bridge.after_tool_orig = bridge.after_tool

    async def _after_tool(call, result):  # noqa: ANN001
        directive = await bridge.after_tool_orig(call, result)
        if isinstance(directive, dict):
            directive.pop("rebind_topic_id", None)
        return directive

    bridge.after_tool = _after_tool
    loop = _make_loop(adapter, registry, bridge)
    events = _record_events(loop.bus)

    await asyncio.wait_for(loop.run("换到目标话题"), timeout=15)

    assert bridge.note_calls == [], "缺分步事实时不得猜值更新快照"
    codes = [
        str((getattr(event, "data", None) or {}).get("code"))
        for event in events
        if getattr(event, "type", None) == EventType.WARNING
    ]
    assert "topic_causal_update_missing" in codes, (
        "因果更新条件不满足时必须留可观察 WARNING，不能静默"
    )
