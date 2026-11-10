"""话题申请执行桥的流程测试（契约 §2.3 / §4 第 1、4、5、6、7、10、12 项）。

用 fake adapter + fake approvals + 真实 AppContext 的 SQLite 话题/片段/绑定设施，
走 AgentLoop 的真实工具管线（ToolRegistry / TOOL 事件 / 预算 / 护栏），只把模型
与「用户点按钮」换成脚本。不联网、不用真实 Key。

固定住的验收点：
* 未确认前：话题工具不执行（DB 副作用不存在）、正式回答不泄露；
* 批准后：原工具真实执行、本轮归属搬到目标话题、system 用目标话题重建、
  正式回答只出现一次；
* 拒绝 / 超时 / stale：工具不执行、原话题形成正式回答、不标用户停止；
* create 用用户改过的名称；相似冲突的内层 create_topic 审批保留；
* 工具失败 / 归属迁移失败状态真实（不假装成功）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agent.adapters.base import ChatMessage, Completion, ToolCall
from agent.adapters.native import NativeAdapter
from agent.api.bus import EventBus
from agent.api.events import EventType
from agent.config import Settings
from agent.core.loop import AgentLoop
from agent.core.turn import TurnContext
from agent.credentials.store import MemoryKeyring
from agent.services import navigation_version, topic_recovery
from agent.services.app import AppContext
from agent.services.topic_application import TopicApplicationBridge
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.approval import ApprovalResult
from agent.tools.base import Tool, ToolResult

# 本文件验证话题申请的真实审批交互：关闭 conftest 的旧测试自动批准兜底。
pytestmark = pytest.mark.topic_application

ANSWER_MARKER = "[[QIO:ANSWER]]"


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------

@pytest.fixture()
def app(tmp_path) -> AppContext:
    conn = connect(tmp_path / "topic_app.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


def _topic(app: AppContext, name: str) -> str:
    return app.topics.nodes.create_topic(name).id


def _topic_id_by_name(app: AppContext, name: str) -> str | None:
    row = app.conn.execute(
        "SELECT id FROM nodes WHERE type = ? AND name = ?", ("topic", name)
    ).fetchone()
    return row["id"] if row is not None else None


def _message_fragment(app: AppContext, message_id: str) -> str | None:
    row = app.conn.execute(
        "SELECT fragment_id FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    return row["fragment_id"] if row is not None else None


def _binding(app: AppContext, turn_id: str):
    return app.bindings.binding_for(turn_id)


class FakeApprovals:
    """可编程的审批服务：记录申请、按脚本返回决定（可手动放行）。"""

    def __init__(self, *, decisions: list[dict] | None = None, on_request=None) -> None:
        self.decisions = list(decisions or [])
        self.on_request = on_request
        self.requests: list[dict] = []
        self.executions: list[dict] = []
        self.manual = False
        self.awaiting = asyncio.Event()
        self._release = asyncio.Event()

    async def request(self, kind: str, payload: dict) -> ApprovalResult:
        self.requests.append({"kind": kind, "payload": json.loads(json.dumps(payload))})
        if self.on_request is not None:
            await self.on_request(kind, payload)
        if self.manual:
            self.awaiting.set()
            await self._release.wait()
            self._release.clear()
            self.awaiting.clear()
        item = self.decisions.pop(0) if self.decisions else {"decision": "rejected"}
        return ApprovalResult(
            str(item.get("approval_id") or f"appr_fake_{len(self.requests)}"),
            str(item.get("decision") or "rejected"),
            edits=item.get("edits"),
        )

    async def record_legacy_execution(
        self, approval_id: str, *, ok: bool, error: str | None = None,
        summary: str | None = None,
    ) -> dict | None:
        self.executions.append(
            {"approval_id": approval_id, "ok": bool(ok), "error": error, "summary": summary}
        )
        return {"phase": "executed" if ok else "failed"}

    def release(self, decision: str = "approved", edits: dict | None = None) -> None:
        self.decisions.append({"decision": decision, "edits": edits})
        self._release.set()


class ScriptedAdapter:
    """非流式假 adapter：按脚本返回（正文, 工具调用）。"""

    mode = "native"
    model = "fake-topic-scripted"
    supports_stream = False

    def __init__(self, script: list[tuple[str | None, list[ToolCall] | None]]) -> None:
        self.script = list(script)
        self.calls = 0
        self.requests: list[dict] = []

    async def complete(self, messages, tools, **kwargs):
        self.calls += 1
        # 每次请求都留一份快照：后续循环会继续往 messages 里追加。
        self.requests.append({"messages": list(messages), "tools": list(tools or [])})
        if self.script:
            content, calls = self.script.pop(0)
        else:
            content, calls = (ANSWER_MARKER + "\n收尾回答", None)
        return Completion(
            message=ChatMessage(role="assistant", content=content, tool_calls=calls),
            finish_reason="tool_calls" if calls else "stop",
        )


# ---- 流式假客户端（与 tests/test_loop.py 同一形状）----------------------

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
    return type("TCDelta", (), {"index": 0, "id": tc.id, "function": tc.function})()


async def _stream_of(text: str | None, calls: list[Any] | None):
    if text:
        yield _chunk(content=text)
    for call in calls or []:
        yield _chunk(tool_calls=[_tc_delta(_tc(call[0], call[1], call[2]))])
    yield _chunk(finish_reason="tool_calls" if calls else "stop")


class ScriptedStreamClient:
    """流式假客户端：脚本项 = (正文, [(id, name, arguments_json), ...])。"""

    def __init__(self, script: list[tuple[str | None, list[Any] | None]]) -> None:
        self.script = list(script)
        self.calls = 0
        self.requests: list[dict] = []

    @property
    def chat(self) -> "ScriptedStreamClient":
        return self

    @property
    def completions(self) -> "ScriptedStreamClient":
        return self

    async def create(self, **kwargs):
        self.calls += 1
        self.requests.append(dict(kwargs))
        if self.script:
            text, calls = self.script.pop(0)
        else:
            text, calls = (ANSWER_MARKER + "\n收尾回答", None)
        return _stream_of(text, calls)


# ---------------------------------------------------------------------------
# 组装：真实 AppContext + bridge + loop
# ---------------------------------------------------------------------------

def _prepare_turn(app: AppContext, *, message: str, topic_id: str):
    fragment = app.fragments.get_or_create_open(topic_id)
    app.navigation.enter_topic(topic_id)
    turn_id = "turn_topic_app"
    ctx = TurnContext(turn_id=turn_id, message=message)
    message_id, _closed = app.memory.append_message(
        topic_id=topic_id,
        role="user",
        content=message,
        content_type="text",
        model="fake",
        turn_id=turn_id,
    )
    app.bindings.record_binding(turn_id, topic_id, fragment_id=fragment.id)
    ctx.user_message_id = message_id
    ctx.current_topic = topic_id
    ctx.bound_topic = topic_id
    ctx.bound_fragment_id = fragment.id
    return ctx, fragment.id


def _make_bridge(
    app: AppContext,
    ctx: TurnContext,
    approvals: FakeApprovals,
    *,
    rebind_fail: bool = False,
    system_prefix: str = "SYS_FOR",
):
    # bridge 从 app.approvals 取审批通道（生产里就是 AppContext 的真服务）。
    app.approvals = approvals
    captured: dict[str, list] = {"rebind": [], "systems": []}

    async def _rebind(topic_id: str) -> str:
        captured["rebind"].append(topic_id)
        if rebind_fail:
            raise RuntimeError("fake rebind failed")
        binding = app.bindings.binding_for(ctx.turn_id)
        target = app.fragments.get_or_create_open(topic_id)
        app.bindings.rebind_topic_from_tool_nav(ctx.turn_id, topic_id)
        if ctx.user_message_id:
            app.memory.move_message(ctx.user_message_id, to_fragment_id=target.id)
        app.bindings.record_binding(
            ctx.turn_id,
            topic_id,
            fragment_id=target.id,
            intent_id=getattr(binding, "intent_id", None),
            intent_version=getattr(ctx, "bound_intent_version", None),
        )
        ctx.bound_topic = topic_id
        ctx.bound_fragment_id = target.id
        app.navigation.enter_topic(topic_id)
        return topic_id

    async def _rebuild(topic_id: str) -> str:
        captured["systems"].append(topic_id)
        return f"{system_prefix}:{topic_id}"

    bridge = TopicApplicationBridge(app, ctx, rebind_turn=_rebind, rebuild_system=_rebuild)
    return bridge, captured


def _initial_messages(message: str, system: str = "SYS_FOR:old") -> list[ChatMessage]:
    return [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content=message),
    ]


def _make_loop(app: AppContext, adapter, approvals: FakeApprovals, bridge) -> AgentLoop:
    return AgentLoop(
        adapter,
        app.registry,
        app.bus,
        approvals=approvals,
        topic_application=bridge,
        turn_id="turn_topic_app",
    )


def _record_events(app: AppContext) -> list:
    events: list = []
    original = app.bus.publish

    async def spy(event):
        events.append(event)
        await original(event)

    app.bus.publish = spy
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


async def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("等待条件超时")
        await asyncio.sleep(0.01)


def _switch_call(
    call_id: str, topic_id: str, *, intent: str | None = None
) -> ToolCall:
    arguments: dict = {"topic_id": topic_id, "reason": "这段内容看起来属于该话题"}
    if intent is not None:
        arguments["intent"] = intent
    return ToolCall(id=call_id, name="switch_topic", arguments=arguments)


def _create_call(call_id: str, name: str, *, intent: str | None = None) -> ToolCall:
    arguments: dict = {"name": name, "reason": "这是一个独立的新话题"}
    if intent is not None:
        arguments["intent"] = intent
    return ToolCall(id=call_id, name="create_topic", arguments=arguments)


# ---------------------------------------------------------------------------
# 1) 批准已有话题：确认后才执行、上下文重建、回答归目标话题
# ---------------------------------------------------------------------------

async def test_switch_approved_executes_after_decision_and_rebuilds_context(app: AppContext):
    old_topic = _topic(app, "旧话题")
    target_topic = _topic(app, "目标话题")
    old_fragment = app.fragments.get_or_create_open(old_topic).id
    target_fragment = app.fragments.get_or_create_open(target_topic).id
    question = "我们换成目标话题聊吧"
    ctx, old_fragment = _prepare_turn(app, message=question, topic_id=old_topic)
    adapter = ScriptedAdapter([
        ("好的，我先确认一下。", [_switch_call("c1", target_topic)]),
        (ANSWER_MARKER + "\n这是目标话题里的正式回答", None),
    ])
    approvals = FakeApprovals(decisions=[{"decision": "approved"}])
    approvals.manual = True
    bridge, captured = _make_bridge(app, ctx, approvals)
    loop = _make_loop(app, adapter, approvals, bridge)
    events = _record_events(app)

    run = asyncio.create_task(
        loop.run(question, initial_messages=_initial_messages(question))
    )
    try:
        await _wait_until(lambda: bool(approvals.requests))
        # ---- 决定之前：工具未执行、没有正式回答泄露 ----
        assert approvals.requests[0]["kind"] == "topic_switch"
        payload = approvals.requests[0]["payload"]
        assert payload["action"] == "switch"
        assert payload["from_topic_id"] == old_topic
        assert payload["target_topic_id"] == target_topic
        assert payload["source"] == "model"
        assert question not in json.dumps(payload, ensure_ascii=False), "载荷不得带原问题"
        assert app.navigation.anchors.get_active().topic_id == old_topic, "工具不得先执行"
        assert _binding(app, ctx.turn_id).topic_id == old_topic
        assert _message_fragment(app, ctx.user_message_id) == old_fragment
        assert _assistant(events, interim=False) == [], "决定前不得出现正式回答事件"
        assert captured["systems"] == []

        approvals.release("approved")
        result = await asyncio.wait_for(run, timeout=15)
    finally:
        if not run.done():
            run.cancel()

    # ---- 批准之后：真实执行 + 归属/上下文/回答 ----
    assert result.final_content == "这是目标话题里的正式回答"
    assert result.phase.value == "done"
    assert app.navigation.anchors.get_active().topic_id == target_topic
    binding = _binding(app, ctx.turn_id)
    assert binding.topic_id == target_topic
    assert binding.fragment_id == target_fragment
    assert _message_fragment(app, ctx.user_message_id) == target_fragment
    assert app.conn.execute(
        "SELECT COUNT(*) c FROM messages WHERE content = ?", (question,)
    ).fetchone()["c"] == 1, "原问题只能存一份"
    assert captured["rebind"] == [target_topic]
    assert captured["systems"] == [target_topic]
    # 第二次模型请求：system 已换成目标话题，user 消息仍在且只有一条
    second = adapter.requests[1]["messages"]
    assert second[0].role == "system"
    assert second[0].content == f"SYS_FOR:{target_topic}"
    user_messages = [m for m in second if m.role == "user"]
    assert [m.content for m in user_messages] == [question]
    # 正式回答事件只出现一次（且只在决定之后）
    final_events = _assistant(events, interim=False)
    assert len(final_events) == 1
    assert final_events[0]["content"] == "这是目标话题里的正式回答"


# ---------------------------------------------------------------------------
# 2) 拒绝：工具不执行、原话题回答、不标用户停止、不重复申请
# ---------------------------------------------------------------------------

async def test_switch_rejected_answers_in_current_topic_without_stopping_turn(app: AppContext):
    old_topic = _topic(app, "旧话题")
    target_topic = _topic(app, "目标话题")
    app.fragments.get_or_create_open(target_topic)
    question = "帮我换个话题说这个"
    ctx, old_fragment = _prepare_turn(app, message=question, topic_id=old_topic)
    adapter = ScriptedAdapter([
        (None, [_switch_call("c1", target_topic)]),
        (ANSWER_MARKER + "\n就在当前话题回答", None),
    ])
    approvals = FakeApprovals(decisions=[{"decision": "rejected"}])
    bridge, captured = _make_bridge(app, ctx, approvals)
    loop = _make_loop(app, adapter, approvals, bridge)
    events = _record_events(app)

    result = await asyncio.wait_for(
        loop.run(question, initial_messages=_initial_messages(question)), timeout=15
    )

    assert result.final_content == "就在当前话题回答"
    assert result.cancelled is False
    assert result.stopped_by is None, "拒绝话题申请不是用户停止整轮"
    assert result.phase.value == "done"
    assert app.navigation.anchors.get_active().topic_id == old_topic
    assert _binding(app, ctx.turn_id).topic_id == old_topic
    assert _message_fragment(app, ctx.user_message_id) == old_fragment
    assert captured["rebind"] == [] and captured["systems"] == []
    # 第二次请求仍在原话题上下文；tool 结果明确要求不要重复申请
    second = adapter.requests[1]["messages"]
    assert second[0].content == "SYS_FOR:old"
    tool_messages = [m for m in second if m.role == "tool"]
    assert tool_messages, "被拒绝的调用必须有 tool 结果"
    assert "不要" in tool_messages[-1].content
    assert "当前话题" in tool_messages[-1].content
    # 拒绝不是工具失败：不得写进工具失败警告；也不触发无进展审批
    assert not any("switch_topic failed" in w for w in result.warnings)
    assert not any("无进展" in w for w in result.warnings)
    # 拒绝范围是 A 可读的证据
    guard = app.topic_application_state
    assert guard.refused_attempts and guard.refused_attempts[-1]["reason"] == "rejected"
    scope = getattr(ctx, "topic_refusal_scope", None)
    assert scope is not None and scope["user_message_id"] == ctx.user_message_id
    assert scope["fragment_id"] == old_fragment
    # 审批历史里没有「执行」记录（工具没执行）
    assert approvals.executions == []


# ---------------------------------------------------------------------------
# 3) create：用户改过的名称生效（edits）
# ---------------------------------------------------------------------------

async def test_create_approved_with_edited_name_creates_the_edited_topic(app: AppContext):
    old_topic = _topic(app, "旧话题")
    question = "把这段整理成一个新话题吧"
    ctx, _old_fragment = _prepare_turn(app, message=question, topic_id=old_topic)
    adapter = ScriptedAdapter([
        (None, [_create_call("c1", "模型起的新话题名")]),
        (ANSWER_MARKER + "\n新话题里的回答", None),
    ])
    approvals = FakeApprovals(
        decisions=[{"decision": "approved", "edits": {"name": "用户改过的名字"}}]
    )
    bridge, captured = _make_bridge(app, ctx, approvals)
    loop = _make_loop(app, adapter, approvals, bridge)

    result = await asyncio.wait_for(
        loop.run(question, initial_messages=_initial_messages(question)), timeout=15
    )

    created_id = _topic_id_by_name(app, "用户改过的名字")
    assert created_id is not None, "create 必须使用用户批准的名称"
    assert _topic_id_by_name(app, "模型起的新话题名") is None
    assert _binding(app, ctx.turn_id).topic_id == created_id
    assert captured["rebind"] == [created_id]
    assert captured["systems"] == [created_id]
    second = adapter.requests[1]["messages"]
    assert second[0].content == f"SYS_FOR:{created_id}"
    # 工具确实收到了校正后的参数（工具消息与工具事件同源）
    tool_messages = [m for m in second if m.role == "tool"]
    assert any("用户改过的名字" in (m.content or "") for m in tool_messages)
    assert result.final_content == "新话题里的回答"


# ---------------------------------------------------------------------------
# 4) 拒绝之后同轮重复申请被程序拦截（不重复弹、工具不执行）
# ---------------------------------------------------------------------------

async def test_repeated_switch_after_rejection_is_late_and_not_executed(app: AppContext):
    old_topic = _topic(app, "旧话题")
    target_topic = _topic(app, "目标话题")
    app.fragments.get_or_create_open(target_topic)
    question = "换到目标话题吧"
    ctx, _ = _prepare_turn(app, message=question, topic_id=old_topic)
    adapter = ScriptedAdapter([
        (None, [_switch_call("c1", target_topic)]),
        (None, [_switch_call("c2", target_topic)]),
        (ANSWER_MARKER + "\n在当前话题回答", None),
    ])
    approvals = FakeApprovals(decisions=[{"decision": "rejected"}])
    bridge, captured = _make_bridge(app, ctx, approvals)
    loop = _make_loop(app, adapter, approvals, bridge)

    result = await asyncio.wait_for(
        loop.run(question, initial_messages=_initial_messages(question)), timeout=15
    )

    assert len(approvals.requests) == 1, "同一轮同一申请不得弹第二次"
    assert adapter.calls == 3
    assert result.final_content == "在当前话题回答"
    assert app.navigation.anchors.get_active().topic_id == old_topic
    # 拒绝 → 立即 ANSWERABLE，本轮归属固定：再次请求导航属**迟到**请求，
    # 按既有 _warn_late_tool_calls 口径不执行，不再进入申请/查重流程。
    reasons = [item["reason"] for item in app.topic_application_state.refused_attempts]
    assert reasons == ["rejected"], "迟到的导航请求不进入申请流程（不记 duplicate）"
    assert captured["rebind"] == []
    assert any("不执行这些调用" in w for w in result.warnings), "迟到请求必须可观察"


# ---------------------------------------------------------------------------
# 5) stale：等待期间用户手动导航 → 不执行、按拒绝在原话题回答
# ---------------------------------------------------------------------------

async def test_navigation_while_waiting_makes_application_stale(app: AppContext):
    old_topic = _topic(app, "旧话题")
    target_topic = _topic(app, "目标话题")
    other_topic = _topic(app, "用户手动点开的话题")
    app.fragments.get_or_create_open(target_topic)
    app.fragments.get_or_create_open(other_topic)
    question = "换成目标话题说"
    ctx, old_fragment = _prepare_turn(app, message=question, topic_id=old_topic)

    async def navigate_during_wait(kind, payload):
        # 模拟另一个请求里的用户导航：active anchor 变了，但本轮 binding 没变。
        app.navigation.enter_topic(other_topic)

    adapter = ScriptedAdapter([
        (None, [_switch_call("c1", target_topic)]),
        (ANSWER_MARKER + "\n在原话题回答", None),
    ])
    approvals = FakeApprovals(
        decisions=[{"decision": "approved"}], on_request=navigate_during_wait
    )
    bridge, captured = _make_bridge(app, ctx, approvals)
    loop = _make_loop(app, adapter, approvals, bridge)

    result = await asyncio.wait_for(
        loop.run(question, initial_messages=_initial_messages(question)), timeout=15
    )

    # 不执行、不覆盖用户的新选择
    assert app.navigation.anchors.get_active().topic_id == other_topic
    assert _binding(app, ctx.turn_id).topic_id == old_topic
    assert _message_fragment(app, ctx.user_message_id) == old_fragment
    assert captured["rebind"] == [] and captured["systems"] == []
    assert result.final_content == "在原话题回答"
    # P1-04：申请绑定真实导航版本快照（不是等待前检查一次就当一直有效）。
    payload = approvals.requests[0]["payload"]
    assert payload["base_nav_version"] is not None
    assert navigation_version.current(app.conn) != payload["base_nav_version"], (
        "用户手动导航必须推进导航版本，旧申请的起点因此不再匹配"
    )
    # 失效 ≠ 拒绝：不写拒绝范围（不污染「用户拒绝过」的证据）。
    reasons = [item["reason"] for item in app.topic_application_state.refused_attempts]
    assert "stale" not in reasons
    assert getattr(ctx, "topic_refusal_scope", None) is None
    # 批准过但没执行：审批历史必须如实收口成失败（不假装执行成功）
    assert approvals.executions and approvals.executions[-1]["ok"] is False


# ---------------------------------------------------------------------------
# 6) 超时 / 过期：**不是**用户主动拒绝（F10），不执行、不标用户停止、照常回答
# ---------------------------------------------------------------------------

async def test_timeout_is_not_treated_as_user_refusal_and_answers_in_current_topic(
    app: AppContext,
):
    old_topic = _topic(app, "旧话题")
    target_topic = _topic(app, "目标话题")
    app.fragments.get_or_create_open(target_topic)
    question = "换话题"
    ctx, _ = _prepare_turn(app, message=question, topic_id=old_topic)
    adapter = ScriptedAdapter([
        (None, [_switch_call("c1", target_topic)]),
        (ANSWER_MARKER + "\n超时后原话题回答", None),
    ])
    approvals = FakeApprovals(decisions=[{"decision": "timeout"}])
    bridge, captured = _make_bridge(app, ctx, approvals)
    loop = _make_loop(app, adapter, approvals, bridge)

    result = await asyncio.wait_for(
        loop.run(question, initial_messages=_initial_messages(question)), timeout=15
    )

    assert result.final_content == "超时后原话题回答"
    assert result.formal_answer is True
    assert result.cancelled is False and result.stopped_by is None
    assert result.stop_reason_code == "none"
    assert app.navigation.anchors.get_active().topic_id == old_topic
    assert captured["rebind"] == [], "超时后话题工具不得执行"
    # F10：超时 **不是** 主动拒绝 —— 不写拒绝证据、不污染「用户拒绝过」的范围，
    # 也不伪装成用户拒绝的文案；本轮照常在原话题回答。
    reasons = [item["reason"] for item in app.topic_application_state.refused_attempts]
    assert "timeout" not in reasons
    assert getattr(ctx, "topic_refusal_scope", None) is None
    assert not any("over timeout" in w for w in result.warnings)
    second = adapter.requests[1]["messages"]
    tool_messages = [m for m in second if m.role == "tool"]
    assert tool_messages, "被拦截的超时调用必须有真实工具结果"
    feedback = tool_messages[-1].content or ""
    assert "超时" in feedback
    assert "不是用户拒绝" in feedback, "超时不得被描述成用户拒绝"
    assert "topic_application_timeout" in feedback or "没有执行" in feedback


# ---------------------------------------------------------------------------
# 7) 一次性交付路径：正文 + 话题工具同时返回 → 决定前不得出现正式回答
# ---------------------------------------------------------------------------

async def test_declared_answer_with_topic_tool_body_is_discarded_until_decision(
    app: AppContext,
):
    old_topic = _topic(app, "旧话题")
    target_topic = _topic(app, "目标话题")
    app.fragments.get_or_create_open(target_topic)
    question = "换到目标话题并回答"
    ctx, _ = _prepare_turn(app, message=question, topic_id=old_topic)
    adapter = ScriptedAdapter([
        (ANSWER_MARKER + "\n这段先不能交付", [_switch_call("c1", target_topic)]),
        (ANSWER_MARKER + "\n真正的回答", None),
    ])
    approvals = FakeApprovals(decisions=[{"decision": "approved"}])
    approvals.manual = True
    bridge, _captured = _make_bridge(app, ctx, approvals)
    loop = _make_loop(app, adapter, approvals, bridge)
    events = _record_events(app)

    run = asyncio.create_task(
        loop.run(question, initial_messages=_initial_messages(question))
    )
    try:
        await _wait_until(lambda: bool(approvals.requests))
        assert _assistant(events, interim=False) == [], "决定前不得出现正式回答"
        # 阶段门：响应含话题工具调用 → 正文整段丢弃（不能当回答，也**不得**
        # 改名成过程说明发出去 —— 旧「先发后改判」路径已删除）。
        assert all(
            "这段先不能交付" not in (d.get("content") or "")
            for d in _assistant(events)
        ), "含话题工具调用的响应正文必须整段丢弃"
        approvals.release("approved")
        result = await asyncio.wait_for(run, timeout=15)
    finally:
        if not run.done():
            run.cancel()

    assert result.final_content == "真正的回答"
    final_events = _assistant(events, interim=False)
    # 流式收尾是同一 delta_id 的累计快照：正文只算一份。
    assert {d["content"] for d in final_events} == {"真正的回答"}
    assert len({d["delta_id"] for d in final_events}) == 1
    assert all("这段先不能交付" not in d.get("content", "") for d in final_events)
    assert all(
        "这段先不能交付" not in (d.get("content") or "")
        for d in _assistant(events)
    )


# ---------------------------------------------------------------------------
# 8) 流式：先文字后工具调用 —— 决定前没有正式回答 delta
# ---------------------------------------------------------------------------

async def test_streamed_preamble_before_topic_tool_never_becomes_answer(app: AppContext):
    old_topic = _topic(app, "旧话题")
    target_topic = _topic(app, "目标话题")
    app.fragments.get_or_create_open(target_topic)
    question = "先说明再换话题"
    ctx, _ = _prepare_turn(app, message=question, topic_id=old_topic)
    client = ScriptedStreamClient([
        (
            "好的，我先确认一下。",
            [("c1", "switch_topic", json.dumps({"topic_id": target_topic, "reason": "换话题"}))],
        ),
        (ANSWER_MARKER + "\n流式生成的正式回答", None),
    ])
    adapter = NativeAdapter(client=client, model="m1")
    approvals = FakeApprovals(decisions=[{"decision": "approved"}])
    approvals.manual = True
    bridge, _captured = _make_bridge(app, ctx, approvals)
    loop = _make_loop(app, adapter, approvals, bridge)
    events = _record_events(app)

    run = asyncio.create_task(
        loop.run(question, initial_messages=_initial_messages(question))
    )
    try:
        await _wait_until(lambda: bool(approvals.requests))
        assert _assistant(events, interim=False) == [], "决定前不得有 answer delta"
        approvals.release("approved")
        result = await asyncio.wait_for(run, timeout=20)
    finally:
        if not run.done():
            run.cancel()

    assert result.final_content == "流式生成的正式回答"
    final_events = _assistant(events, interim=False)
    # 流式收尾会带同一 delta_id 的累计快照：按内容去重后只能有一份正式回答
    assert {d["content"] for d in final_events} == {"流式生成的正式回答"}
    assert len(final_events) >= 1


# ---------------------------------------------------------------------------
# 9) 流程内失败：归属迁移失败 → 工具结果如实失败，不假装成功
# ---------------------------------------------------------------------------

async def test_rebind_failure_is_reported_as_failure_not_success(app: AppContext):
    old_topic = _topic(app, "旧话题")
    target_topic = _topic(app, "目标话题")
    app.fragments.get_or_create_open(target_topic)
    question = "换话题"
    ctx, _ = _prepare_turn(app, message=question, topic_id=old_topic)
    adapter = ScriptedAdapter([
        (None, [_switch_call("c1", target_topic)]),
        (ANSWER_MARKER + "\n仍然给出回答", None),
    ])
    approvals = FakeApprovals(decisions=[{"decision": "approved"}])
    bridge, captured = _make_bridge(app, ctx, approvals, rebind_fail=True)
    loop = _make_loop(app, adapter, approvals, bridge)

    events = _record_events(app)
    result = await asyncio.wait_for(
        loop.run(question, initial_messages=_initial_messages(question)), timeout=15
    )

    # F06：工具本身执行了（用户批准的操作不回滚），但归属迁移失败 = 本轮归属事实
    # 不可信：进入可恢复失败，不重建上下文、不用旧 system 发布正式回答。
    assert app.navigation.anchors.get_active().topic_id == target_topic
    assert _binding(app, ctx.turn_id).topic_id == old_topic
    assert captured["systems"] == [], "归属失败后不得重建目标上下文"
    assert adapter.calls == 1, "归属失败后不得再发起模型调用"
    assert result.final_content is None, "归属失败必须零正式答案"
    assert result.formal_answer is False
    assert loop.topic_phase.value == "recoverable_failure"
    assert loop.topic_stages is not None and loop.topic_stages.failed_stage == "ownership"
    assert any("失败" in w for w in result.warnings)
    # 审批历史：工具执行成功但迁移失败，有一句真实说明（不是裸的「已执行」）
    assert approvals.executions[-1]["ok"] is True
    assert "迁移失败" in (approvals.executions[-1]["summary"] or "")
    # 可观察错误 + 恢复记录（failed_stage=ownership，可重试）
    errors = [
        event.data
        for event in events
        if getattr(event, "type", None) == EventType.ERROR
        and (event.data or {}).get("code") == "topic_ownership_failed"
    ]
    assert errors, "归属失败必须发出可观察 ERROR(topic_ownership_failed)"
    recovery = errors[0]["recovery"]
    assert recovery["failed_stage"] == "ownership" and recovery["retryable"] is True
    stored = topic_recovery.get_record(app, recovery["record_id"])
    assert stored is not None and stored["status"] == "pending"
    assert stored["failed_stage"] == "ownership"
    assert stored["turn_id"] == ctx.turn_id
    assert stored["user_message_id"] == ctx.user_message_id
    assert stored["topic_id"] == target_topic


# ---------------------------------------------------------------------------
# 10) 相似冲突：内层 create_topic 二次审批保留（不跳过）
# ---------------------------------------------------------------------------

async def test_create_duplicate_keeps_inner_create_topic_approval(app: AppContext):
    existing_topic = _topic(app, "项目计划")
    app.fragments.get_or_create_open(existing_topic)
    question = "新建话题记录一下"
    ctx, _ = _prepare_turn(app, message=question, topic_id=existing_topic)
    tool = app.registry.get("create_topic")
    assert tool is not None
    adapter = ScriptedAdapter([
        (None, [_create_call("c1", "项目计划2")]),
        (None, [_create_call("c2", "项目计划2")]),
        (ANSWER_MARKER + "\n冲突处理后的回答", None),
    ])
    approvals = FakeApprovals(
        decisions=[
            {"decision": "approved"},
            {"decision": "approved"},
        ]
    )
    tool.approvals = approvals
    bridge, _captured = _make_bridge(app, ctx, approvals)
    loop = _make_loop(app, adapter, approvals, bridge)

    result = await asyncio.wait_for(
        loop.run(question, initial_messages=_initial_messages(question)), timeout=20
    )

    kinds = [item["kind"] for item in approvals.requests]
    assert kinds == ["topic_create", "create_topic"], (
        "查重失败后的重试必须进入工具自己的 create_topic 二次审批"
    )
    created_id = _topic_id_by_name(app, "项目计划2")
    assert created_id is not None, "第二次（人工批准冲突后）应当真的创建"
    assert _binding(app, ctx.turn_id).topic_id == created_id
    assert result.final_content == "冲突处理后的回答"


# ---------------------------------------------------------------------------
# 11) 用户本轮已明确要求：不弹普通申请，但仍真实执行/搬归属/重建上下文
# ---------------------------------------------------------------------------

async def test_explicit_user_switch_skips_approval_but_still_rebinds(app: AppContext):
    old_topic = _topic(app, "旧话题")
    target_topic = _topic(app, "目标话题")
    app.fragments.get_or_create_open(target_topic)
    question = "切换到目标话题"
    ctx, _ = _prepare_turn(app, message=question, topic_id=old_topic)
    # 模拟 begin() 已按用户明确导航把本轮绑定到目标话题（contract §1.3）。
    target_fragment = app.fragments.get_or_create_open(target_topic).id
    app.navigation.enter_topic(target_topic)
    app.bindings.rebind_topic_from_tool_nav(ctx.turn_id, target_topic)
    app.bindings.record_binding(ctx.turn_id, target_topic, fragment_id=target_fragment)
    app.memory.move_message(ctx.user_message_id, to_fragment_id=target_fragment)
    ctx.bound_topic = target_topic
    ctx.bound_fragment_id = target_fragment

    # P1-01：语义来源唯一受控通道 = 本轮真实 tool_call 的 intent 参数。
    adapter = ScriptedAdapter([
        (None, [_switch_call("c1", target_topic, intent="user_request")]),
        (ANSWER_MARKER + "\n明确导航后的回答", None),
    ])
    approvals = FakeApprovals()
    bridge, captured = _make_bridge(app, ctx, approvals)
    loop = _make_loop(app, adapter, approvals, bridge)

    result = await asyncio.wait_for(
        loop.run(question, initial_messages=_initial_messages(question)), timeout=15
    )

    assert approvals.requests == [], "本轮用户已明确要求，不再弹普通申请"
    assert result.final_content == "明确导航后的回答"
    assert captured["systems"] == [target_topic], "仍要按目标话题重建 system"
    second = adapter.requests[1]["messages"]
    assert second[0].content == f"SYS_FOR:{target_topic}"


async def test_explicit_user_creation_skips_approval_but_still_creates_and_rebinds(
    app: AppContext,
):
    old_topic = _topic(app, "旧话题")
    question = "新建一个话题叫「用户说的新话题」"
    ctx, _ = _prepare_turn(app, message=question, topic_id=old_topic)
    # P1-01：直接执行只由本轮 tool_call 的 intent 声明，不再读轮前 explicit 提示。
    adapter = ScriptedAdapter([
        (None, [_create_call("c1", "用户说的新话题", intent="user_request")]),
        (ANSWER_MARKER + "\n新话题里的回答", None),
    ])
    approvals = FakeApprovals()
    bridge, captured = _make_bridge(app, ctx, approvals)
    loop = _make_loop(app, adapter, approvals, bridge)

    result = await asyncio.wait_for(
        loop.run(question, initial_messages=_initial_messages(question)), timeout=15
    )

    assert approvals.requests == [], "新建的查重/冲突审批仍由工具内部处理，但普通申请不弹"
    created_id = _topic_id_by_name(app, "用户说的新话题")
    assert created_id is not None
    assert _binding(app, ctx.turn_id).topic_id == created_id
    assert captured["rebind"] == [created_id]
    assert captured["systems"] == [created_id]
    assert result.final_content == "新话题里的回答"


# ---------------------------------------------------------------------------
# 12) 失败事实分开：工具失败 / 回答失败不得互相冒充
# ---------------------------------------------------------------------------

async def test_switch_tool_failure_after_approval_is_reported_as_tool_failure(
    app: AppContext,
):
    old_topic = _topic(app, "旧话题")
    target_topic = _topic(app, "目标话题")
    app.fragments.get_or_create_open(target_topic)
    question = "换到目标话题"
    ctx, _ = _prepare_turn(app, message=question, topic_id=old_topic)

    # 批准后、真正执行时工具失败（等价于目标在等待期间被删除）：注入失败的执行。
    tool = app.registry.get("switch_topic")
    assert tool is not None

    async def failing_run(**kwargs):  # noqa: ANN003
        return ToolResult(ok=False, error="模拟工具执行失败")

    tool.run = failing_run
    adapter = ScriptedAdapter([
        (None, [_switch_call("c1", target_topic)]),
        (ANSWER_MARKER + "\n目标消失后仍在原话题回答", None),
    ])
    approvals = FakeApprovals(decisions=[{"decision": "approved"}])
    bridge, captured = _make_bridge(app, ctx, approvals)
    loop = _make_loop(app, adapter, approvals, bridge)

    result = await asyncio.wait_for(
        loop.run(question, initial_messages=_initial_messages(question)), timeout=15
    )

    # 工具真实失败：不搬归属、不重建；失败记进工具侧而不是回答侧
    assert captured["rebind"] == [] and captured["systems"] == []
    assert _binding(app, ctx.turn_id).topic_id == old_topic
    assert result.final_content == "目标消失后仍在原话题回答"
    assert any("switch_topic failed" in w for w in result.warnings)
    # 审批历史：批准了但执行失败，必须如实写 failed（不是 executed）
    assert approvals.executions and approvals.executions[-1]["ok"] is False
    # 模型看到的是工具失败消息（不是因为工具没执行）
    second = adapter.requests[1]["messages"]
    tool_messages = [m for m in second if m.role == "tool"]
    assert tool_messages and "失败" in tool_messages[-1].content


async def test_answer_failure_after_switch_does_not_rollback_the_topic(app: AppContext):
    old_topic = _topic(app, "旧话题")
    target_topic = _topic(app, "目标话题")
    app.fragments.get_or_create_open(target_topic)
    question = "换到目标话题再回答"
    ctx, _ = _prepare_turn(app, message=question, topic_id=old_topic)

    class AnswerFails(ScriptedAdapter):
        async def complete(self, messages, tools, **kwargs):
            if self.calls >= 1:
                self.calls += 1
                raise RuntimeError("provider down while answering")
            return await super().complete(messages, tools, **kwargs)

    adapter = AnswerFails([(None, [_switch_call("c1", target_topic)])])
    approvals = FakeApprovals(decisions=[{"decision": "approved"}])
    bridge, _captured = _make_bridge(app, ctx, approvals)
    loop = _make_loop(app, adapter, approvals, bridge)

    # P1-03：回答失败是可恢复失败（不再上抛整轮失败）：不发布正式回答，保留恢复
    # 记录，单独记 failed_stage="answer"；已经成功执行的话题变更不回滚。
    result = await asyncio.wait_for(
        loop.run(question, initial_messages=_initial_messages(question)), timeout=15
    )

    from agent.services import topic_recovery

    assert result.final_content is None
    assert result.formal_answer is False
    assert result.cancelled is False
    assert app.navigation.anchors.get_active().topic_id == target_topic
    assert _binding(app, ctx.turn_id).topic_id == target_topic
    assert approvals.executions and approvals.executions[-1]["ok"] is True
    recovery = loop._topic_recovery
    assert recovery is not None and recovery["failed_stage"] == "answer"
    stored = topic_recovery.get_record(app, recovery["record_id"])
    assert stored is not None
    assert stored["status"] == "pending"
    assert stored["failed_stage"] == "answer"
    assert stored["topic_id"] == target_topic


# ---------------------------------------------------------------------------
# 13) F01：普通检索 → 话题工具 → 确认执行 → 目标上下文 → 正式回答（完整链路）
# ---------------------------------------------------------------------------


class _LookupTool(Tool):
    """只读检索类普通工具（F01 场景里的「普通工具调用」）。"""

    name = "lookup"
    description = "fake read-only lookup"
    parameters = {"type": "object", "properties": {"query": {"type": "string"}}}

    def __init__(self) -> None:
        self.executed = 0

    async def run(self, **kwargs):
        self.executed += 1
        return ToolResult(ok=True, content="查到的资料")


def _phase_names(events: list) -> list[str]:
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


async def test_ordinary_retrieval_then_topic_flow_full_chain(app: AppContext):
    old_topic = _topic(app, "旧话题")
    target_topic = _topic(app, "目标话题")
    target_fragment = app.fragments.get_or_create_open(target_topic).id
    question = "先查一下资料，再换到目标话题回答"
    ctx, old_fragment = _prepare_turn(app, message=question, topic_id=old_topic)
    lookup = _LookupTool()
    app.registry.register(lookup)
    adapter = ScriptedAdapter(
        [
            ("我先查一下资料。", [ToolCall(id="r1", name="lookup", arguments={"query": "x"})]),
            (None, [_switch_call("c1", target_topic)]),
            (ANSWER_MARKER + "\n这是目标话题里的正式回答", None),
        ]
    )
    approvals = FakeApprovals(decisions=[{"decision": "approved"}])
    bridge, captured = _make_bridge(app, ctx, approvals)
    loop = _make_loop(app, adapter, approvals, bridge)
    events = _record_events(app)

    result = await asyncio.wait_for(
        loop.run(question, initial_messages=_initial_messages(question)), timeout=20
    )

    # 普通检索真实执行过一次；后续话题工具正常进入申请（不是迟到调用）
    assert lookup.executed == 1
    assert len(approvals.requests) == 1, "普通检索之后的话题工具必须正常进入申请流程"
    assert captured["rebind"] == [target_topic]
    assert captured["systems"] == [target_topic]
    assert not any(
        code == "answer_declared_with_tool_calls" for code in _warning_codes(events)
    )
    # 归属与回答都落到目标话题
    assert _binding(app, ctx.turn_id).topic_id == target_topic
    assert _binding(app, ctx.turn_id).fragment_id == target_fragment
    assert _message_fragment(app, ctx.user_message_id) == target_fragment
    assert result.final_content == "这是目标话题里的正式回答"
    assert result.formal_answer is True

    # 普通检索的正文只进过程区（零正式正文泄露）
    formal = _assistant(events, interim=False)
    assert {item["content"] for item in formal} == {"这是目标话题里的正式回答"}
    assert all(
        "我先查一下资料" not in (item.get("content") or "") for item in formal
    )
    assert any(
        "我先查一下资料" in (item.get("content") or "")
        for item in _assistant(events, interim=True)
    ), "普通工具轮的过程说明应进过程区"
    # 事件顺序：TOPIC_PHASE(answerable) 必须先于正式正文
    phases = _phase_names(events)
    assert phases[0] == "decision_open"
    assert "answerable" in phases
    answerable_at = _first_index(
        events,
        lambda event: getattr(event, "type", None) == EventType.TOPIC_PHASE
        and (event.data or {}).get("phase") == "answerable",
    )
    formal_at = _first_index(
        events,
        lambda event: getattr(event, "type", None) == EventType.ASSISTANT
        and (event.data or {}).get("interim") is False
        and str((event.data or {}).get("content") or "").strip(),
    )
    assert answerable_at is not None and formal_at is not None
    assert answerable_at < formal_at


# ---------------------------------------------------------------------------
# 14) F06：归属失败的三种注入 → 零正式答案 + 可见恢复记录
# ---------------------------------------------------------------------------


async def test_move_user_message_failure_is_ownership_failure(app: AppContext):
    old_topic = _topic(app, "旧话题")
    target_topic = _topic(app, "目标话题")
    app.fragments.get_or_create_open(target_topic)
    question = "换到目标话题再回答"
    ctx, _ = _prepare_turn(app, message=question, topic_id=old_topic)
    adapter = ScriptedAdapter(
        [
            (None, [_switch_call("c1", target_topic)]),
            (ANSWER_MARKER + "\n不该出现的回答", None),
        ]
    )
    approvals = FakeApprovals(decisions=[{"decision": "approved"}])
    app.approvals = approvals
    captured: dict[str, list] = {"rebind": [], "systems": []}

    async def _rebuild(topic_id: str) -> str:
        captured["systems"].append(topic_id)
        return f"SYS_FOR:{topic_id}"

    async def _rebind(topic_id: str) -> str:
        captured["rebind"].append(topic_id)
        # 生产里的归属搬迁是事务化的：任何一步失败都整体回滚（绑定保持旧值）。
        return await app.turn_orchestrator.rebind_turn_to_topic(ctx, topic_id)

    bridge = TopicApplicationBridge(
        app, ctx, rebind_turn=_rebind, rebuild_system=_rebuild
    )
    loop = _make_loop(app, adapter, approvals, bridge)
    events = _record_events(app)

    def _move_boom(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise RuntimeError("move user message failed")

    app.memory.move_message = _move_boom

    result = await asyncio.wait_for(
        loop.run(question, initial_messages=_initial_messages(question)), timeout=15
    )

    # 工具与导航已经真实发生（不回滚），但归属搬迁失败：本轮归属事实不可信
    assert app.navigation.anchors.get_active().topic_id == target_topic
    assert _binding(app, ctx.turn_id).topic_id == old_topic
    assert captured["systems"] == [], "归属失败后不得装配目标上下文"
    assert adapter.calls == 1, "归属失败后不得再发起模型调用"
    assert result.final_content is None and result.formal_answer is False
    assert _assistant(events, interim=False) == []
    assert loop.topic_phase.value == "recoverable_failure"
    assert loop.topic_stages is not None and loop.topic_stages.failed_stage == "ownership"
    assert "topic_ownership_failed" in _error_codes(events)
    recovery = loop._topic_recovery
    assert recovery is not None and recovery["failed_stage"] == "ownership"
    stored = topic_recovery.get_record(app, recovery["record_id"])
    assert stored is not None and stored["status"] == "pending"
    assert stored["topic_id"] == target_topic
    assert stored["user_message_id"] == ctx.user_message_id


async def test_after_tool_exception_after_success_is_ownership_failure(
    app: AppContext, monkeypatch
):
    old_topic = _topic(app, "旧话题")
    target_topic = _topic(app, "目标话题")
    app.fragments.get_or_create_open(target_topic)
    question = "换到目标话题再回答"
    ctx, _ = _prepare_turn(app, message=question, topic_id=old_topic)
    adapter = ScriptedAdapter(
        [
            (None, [_switch_call("c1", target_topic)]),
            (ANSWER_MARKER + "\n不该出现的回答", None),
        ]
    )
    approvals = FakeApprovals(decisions=[{"decision": "approved"}])
    bridge, captured = _make_bridge(app, ctx, approvals)
    loop = _make_loop(app, adapter, approvals, bridge)
    events = _record_events(app)
    original_after = bridge.after_tool

    async def _boom(call, result):  # noqa: ANN001
        if call.name == "switch_topic" and getattr(result, "ok", False):
            raise RuntimeError("topic post-processing boom")
        return await original_after(call, result)

    monkeypatch.setattr(bridge, "after_tool", _boom)

    result = await asyncio.wait_for(
        loop.run(question, initial_messages=_initial_messages(question)), timeout=15
    )

    assert app.navigation.anchors.get_active().topic_id == target_topic
    assert _binding(app, ctx.turn_id).topic_id == old_topic
    assert captured["rebind"] == []
    assert captured["systems"] == []
    assert adapter.calls == 1
    assert result.final_content is None and result.formal_answer is False
    assert _assistant(events, interim=False) == []
    errors = [
        event.data
        for event in events
        if getattr(event, "type", None) == EventType.ERROR
        and (event.data or {}).get("code") == "topic_ownership_failed"
    ]
    assert errors, "话题后置处理异常必须收口成可观察的归属失败"
    assert errors[0]["failed_stage"] == "ownership"
    stored = topic_recovery.get_record(app, errors[0]["recovery"]["record_id"])
    assert stored is not None and stored["failed_stage"] == "ownership"


async def test_normal_tool_after_tool_exception_keeps_tolerance(
    app: AppContext, monkeypatch
):
    topic = _topic(app, "普通话题")
    fragment = app.fragments.get_or_create_open(topic)
    app.navigation.enter_topic(topic)
    question = "查一下资料再回答"
    turn_id = "turn_topic_app"
    ctx = TurnContext(turn_id=turn_id, message=question)
    message_id, _ = app.memory.append_message(
        topic_id=topic, role="user", content=question, model="fake", turn_id=turn_id
    )
    app.bindings.record_binding(turn_id, topic, fragment_id=fragment.id)
    ctx.user_message_id = message_id
    ctx.current_topic = topic
    ctx.bound_topic = topic
    ctx.bound_fragment_id = fragment.id
    lookup = _LookupTool()
    app.registry.register(lookup)
    adapter = ScriptedAdapter(
        [
            (None, [ToolCall(id="r1", name="lookup", arguments={"query": "x"})]),
            (ANSWER_MARKER + "\n正常回答", None),
        ]
    )
    approvals = FakeApprovals()
    bridge, _captured = _make_bridge(app, ctx, approvals)
    loop = _make_loop(app, adapter, approvals, bridge)
    events = _record_events(app)
    original_after = bridge.after_tool

    async def _boom(call, result):  # noqa: ANN001
        if call.name == "lookup":
            raise RuntimeError("normal post-processing boom")
        return await original_after(call, result)

    monkeypatch.setattr(bridge, "after_tool", _boom)

    result = await asyncio.wait_for(
        loop.run(question, initial_messages=_initial_messages(question)), timeout=15
    )

    assert lookup.executed == 1
    assert result.final_content == "正常回答", "非话题工具的普通后置异常仍应容错"
    assert loop.topic_phase.value == "answerable"
    assert "topic_ownership_failed" not in _error_codes(events)
    assert any("后置处理失败" in item for item in result.warnings)

