"""R14-C / F02：直接要求 ≠ 执行成功；冲突 / 可重试失败后同版本可合法重试。

最小失败场景（旧行为，修复前会红）：
* 主动要求新建（intent=user_request）第一次进入原工具遇到相似话题冲突后，
  同 operation + 同 param_version 的重试被 direct 缓存当成「本轮已处理过」拦下，
  合法重试永远进不了具体冲突确认，话题无法创建；
* 直接执行失败（含可重试失败 / 冲突确认超时）后同版本重试同样被拦。

本文件只证明程序事实（不调用真实模型）：决定层 / 执行层分开记录，重试只在
retryable_failure / conflict_pending 允许，成功后重放只返回既有结果，失效不写拒绝。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent.adapters.base import ToolCall
from agent.api.bus import EventBus
from agent.config import Settings
from agent.core.turn import TurnContext
from agent.credentials.store import MemoryKeyring
from agent.services import navigation_version
from agent.services.app import AppContext
from agent.services.topic_application import TopicApplicationBridge
from agent.services.topic_operation import (
    AttemptStatus,
    ConflictStatus,
    DecisionStatus,
    OperationStatus,
)
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.approval import ApprovalResult

pytestmark = pytest.mark.topic_application


@pytest.fixture()
def app(tmp_path: Path) -> AppContext:
    conn = connect(tmp_path / "r14c_f02.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    yield ctx


class FakeApprovals:
    """可编程审批：decisions 顺序消费；manual=True 时等待 release()。"""

    def __init__(self, *, decisions: list[dict] | None = None) -> None:
        self.decisions = list(decisions or [])
        self.requests: list[dict] = []
        self.executions: list[dict] = []
        self.manual = False
        self.awaiting = asyncio.Event()
        self._release = asyncio.Event()

    async def request(self, kind: str, payload: dict) -> ApprovalResult:
        self.requests.append({"kind": kind, "payload": json.loads(json.dumps(payload))})
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
        self, approval_id, *, ok, error=None, summary=None
    ) -> dict | None:
        self.executions.append({"approval_id": approval_id, "ok": bool(ok)})
        return {"phase": "executed" if ok else "failed"}

    def release(self, decision: str = "approved", edits: dict | None = None) -> None:
        self.decisions.append({"decision": decision, "edits": edits})
        self._release.set()


def _topic(app: AppContext, name: str) -> str:
    return app.topics.nodes.create_topic(name).id


def _topic_id_by_name(app: AppContext, name: str) -> str | None:
    row = app.conn.execute(
        "SELECT id FROM nodes WHERE type = 'topic' AND name = ?", (name,)
    ).fetchone()
    return row["id"] if row is not None else None


def _make_turn(app: AppContext, *, topic_id: str, message: str = "问题"):
    fragment = app.fragments.get_or_create_open(topic_id)
    turn_id = "turn-r14c-f02"
    ctx = TurnContext(turn_id=turn_id, message=message)
    message_id, _ = app.memory.append_message(
        topic_id=topic_id,
        role="user",
        content=message,
        content_type="text",
        model="fake",
        turn_id=turn_id,
    )
    app.navigation.enter_topic(topic_id)
    app.bindings.record_binding(turn_id, topic_id, fragment_id=fragment.id)
    ctx.user_message_id = message_id
    ctx.current_topic = topic_id
    ctx.bound_topic = topic_id
    ctx.bound_fragment_id = fragment.id
    # F05 决策快照：本轮判断依据形成时的真实位置（A 在生产 begin 里写）。
    ctx.decision_nav_version = navigation_version.current(app.conn)
    ctx.decision_topic_id = topic_id
    ctx.decision_fragment_id = fragment.id
    return ctx


def _make_bridge(app: AppContext, ctx: TurnContext, approvals: FakeApprovals):
    app.approvals = approvals
    captured: dict[str, list] = {"rebind": [], "systems": [], "phases": []}

    async def _rebind(topic_id: str) -> str:
        captured["rebind"].append(topic_id)
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
        )
        ctx.bound_topic = topic_id
        ctx.bound_fragment_id = target.id
        return topic_id

    async def _rebuild(topic_id: str) -> str:
        captured["systems"].append(topic_id)
        return f"SYS:{topic_id}"

    bridge = TopicApplicationBridge(
        app,
        ctx,
        rebind_turn=_rebind,
        rebuild_system=_rebuild,
        set_phase=lambda phase: captured["phases"].append(phase),
    )
    return bridge, captured


def _switch(call_id: str, topic_id: str, *, intent: str | None = None) -> ToolCall:
    arguments: dict = {"topic_id": topic_id}
    if intent is not None:
        arguments["intent"] = intent
    return ToolCall(id=call_id, name="switch_topic", arguments=arguments)


def _create(call_id: str, name: str, *, intent: str | None = None) -> ToolCall:
    arguments: dict = {"name": name}
    if intent is not None:
        arguments["intent"] = intent
    return ToolCall(id=call_id, name="create_topic", arguments=arguments)


async def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    loop_now = asyncio.get_event_loop().time()
    deadline = loop_now + timeout
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("等待条件超时")
        await asyncio.sleep(0.01)


def _conflict_setup(app: AppContext):
    """相似名话题制造 create 的查重冲突；返回 (ctx, bridge, captured, tool)。"""
    old = _topic(app, "旧话题")
    _topic(app, "项目计划")  # 相似话题
    ctx = _make_turn(app, topic_id=old)
    approvals = FakeApprovals()
    bridge, captured = _make_bridge(app, ctx, approvals)
    tool = app.registry.get("create_topic")
    tool.approvals = approvals
    return ctx, bridge, captured, tool, approvals


# ---------------------------------------------------------------------------
# 1) 主动要求新建 → 相似冲突 → 同版本重试必须能进入具体冲突确认并只建一次
# ---------------------------------------------------------------------------


def test_direct_create_conflict_retry_is_allowed_and_creates_once(app: AppContext):
    _ctx, bridge, captured, _tool, approvals = _conflict_setup(app)
    approvals.decisions = [{"decision": "approved"}]

    async def scenario():
        call1 = _create("c1", "项目计划2", intent="user_request")
        # 主动要求：直接放行原工具（不弹普通申请）
        assert await bridge.before_tool(call1) is None
        first = await app.registry.execute(call1)
        assert first.ok is False and first.code == "topic_create_conflict"
        directive1 = await bridge.after_tool(call1, first)
        assert directive1["keep_decision_open"] is True
        operation = bridge.current_operation()
        assert operation.decided is DecisionStatus.APPROVED
        assert operation.attempt is AttemptStatus.CONFLICT_PENDING
        assert operation.conflict is ConflictStatus.CONFLICT_PENDING

        # 最小失败场景：旧实现在这里返回 duplicate（direct 缓存拦截），话题永远建不出来。
        call2 = _create("c2", "项目计划2", intent="user_request")
        assert await bridge.before_tool(call2) is None, "同版本冲突重试必须再次进入原工具"
        second = await app.registry.execute(call2)
        assert second.ok is True, second.error
        await bridge.after_tool(call2, second)

    asyncio.run(scenario())

    rows = app.conn.execute(
        "SELECT id FROM nodes WHERE type='topic' AND name='项目计划2'"
    ).fetchall()
    assert len(rows) == 1, "同 operation 同版本重试只能创建一次"
    assert approvals.requests and approvals.requests[0]["kind"] == "create_topic"
    assert bridge.current_operation().attempt is AttemptStatus.SUCCEEDED
    assert captured["rebind"] == [rows[0]["id"]]


# ---------------------------------------------------------------------------
# 2) 冲突确认超时 ≠ 拒绝：同版本可重试，不写拒绝证据
# ---------------------------------------------------------------------------


def test_direct_conflict_timeout_is_retryable_not_a_refusal(app: AppContext):
    _ctx, bridge, _captured, _tool, approvals = _conflict_setup(app)
    refunds_before = list(bridge.guard.refused_attempts)
    # 第一次冲突确认超时；第二次重试时用户同意。
    approvals.decisions = [{"decision": "timeout"}, {"decision": "approved"}]

    async def full():
        call1 = _create("c1", "项目计划2", intent="user_request")
        assert await bridge.before_tool(call1) is None
        first = await app.registry.execute(call1)
        assert first.code == "topic_create_conflict"
        await bridge.after_tool(call1, first)

        call2 = _create("c2", "项目计划2", intent="user_request")
        assert await bridge.before_tool(call2) is None
        timed = await app.registry.execute(call2)
        assert timed.ok is False and timed.code == "topic_create_conflict_timeout"
        directive = await bridge.after_tool(call2, timed)
        assert directive["keep_decision_open"] is True, "超时不是终态，允许同版本重试"
        operation = bridge.current_operation()
        assert operation.decided is DecisionStatus.APPROVED, "超时不得写成用户拒绝"
        assert operation.attempt is AttemptStatus.RETRYABLE_FAILURE
        assert operation.conflict is ConflictStatus.CONFLICT_PENDING
        assert bridge.guard.refused_attempts == refunds_before, "超时不得写拒绝证据"

        call3 = _create("c3", "项目计划2", intent="user_request")
        assert await bridge.before_tool(call3) is None
        created = await app.registry.execute(call3)
        assert created.ok is True, created.error
        await bridge.after_tool(call3, created)

    asyncio.run(full())
    rows = app.conn.execute(
        "SELECT id FROM nodes WHERE type='topic' AND name='项目计划2'"
    ).fetchall()
    assert len(rows) == 1
    assert [item["kind"] for item in approvals.requests] == ["create_topic", "create_topic"]


# ---------------------------------------------------------------------------
# 3) 具体冲突被拒绝 = 终态：同版本不再重复弹，也不重新执行
# ---------------------------------------------------------------------------


def test_direct_conflict_declined_is_terminal(app: AppContext):
    _ctx, bridge, _captured, _tool, approvals = _conflict_setup(app)
    approvals.decisions = [{"decision": "rejected"}]

    async def scenario():
        call1 = _create("c1", "项目计划2", intent="user_request")
        assert await bridge.before_tool(call1) is None
        first = await app.registry.execute(call1)
        assert first.code == "topic_create_conflict"
        await bridge.after_tool(call1, first)

        call2 = _create("c2", "项目计划2", intent="user_request")
        assert await bridge.before_tool(call2) is None
        declined = await app.registry.execute(call2)
        assert declined.code == "topic_create_conflict_declined"
        directive = await bridge.after_tool(call2, declined)
        assert directive["keep_decision_open"] is False
        operation = bridge.current_operation()
        assert operation.decided is DecisionStatus.REFUSED
        assert operation.attempt is AttemptStatus.TERMINAL_FAILURE
        assert operation.conflict is ConflictStatus.CONFLICT_DECLINED

        # 同版本重放：返回既有终态结果，不再弹确认、不再执行工具。
        calls = len(approvals.requests)
        replay = await bridge.before_tool(_create("c3", "项目计划2", intent="user_request"))
        assert replay is not None and replay.code == "topic_create_conflict_declined"
        assert len(approvals.requests) == calls, "同版本不得重复弹冲突确认"

    asyncio.run(scenario())
    assert _topic_id_by_name(app, "项目计划2") is None


# ---------------------------------------------------------------------------
# 4) 成功后同键重放只返回既有结果（不重复执行、不重复创建）
# ---------------------------------------------------------------------------


def test_successful_direct_replay_returns_existing_result(app: AppContext):
    old = _topic(app, "旧话题")
    target = _topic(app, "目标话题")
    app.fragments.get_or_create_open(target)
    ctx = _make_turn(app, topic_id=old)
    approvals = FakeApprovals()
    bridge, captured = _make_bridge(app, ctx, approvals)

    def run():
        call1 = _switch("c1", target, intent="user_request")
        assert asyncio.run(bridge.before_tool(call1)) is None
        first = asyncio.run(app.registry.execute(call1))
        assert first.ok is True
        asyncio.run(bridge.after_tool(call1, first))

        # 同一 operation + 同一 param_version 重放：直接返回既有结果，零执行。
        replay = asyncio.run(bridge.before_tool(_switch("c2", target, intent="user_request")))
        assert replay is not None and replay.ok is True
        assert replay.content == first.content
        assert asyncio.run(bridge.after_tool(_switch("c2", target, intent="user_request"), replay)) is None

    run()
    assert captured["rebind"] == [target]
    assert ctx.bound_topic == target


# ---------------------------------------------------------------------------
# 5) 起点已变后的重试 = 失效（stale）终态：不执行、不写拒绝证据
# ---------------------------------------------------------------------------


def test_retry_after_navigation_change_is_stale_not_refusal(app: AppContext):
    _ctx, bridge, captured, _tool, approvals = _conflict_setup(app)
    approvals.decisions = [{"decision": "approved"}]

    async def scenario():
        call1 = _create("c1", "项目计划2", intent="user_request")
        assert await bridge.before_tool(call1) is None
        first = await app.registry.execute(call1)
        assert first.code == "topic_create_conflict"
        await bridge.after_tool(call1, first)

        # 等待冲突确认期间，用户真实导航到别的话题（导航版本推进）：
        other = _topic(app, "用户后来去的话题")
        app.navigation.enter_topic(other)

        refusals_before = list(bridge.guard.refused_attempts)
        call2 = _create("c2", "项目计划2", intent="user_request")
        assert await bridge.before_tool(call2) is None
        stale = await app.registry.execute(call2)
        assert stale.ok is False and stale.code == "nav_version_conflict"
        directive = await bridge.after_tool(call2, stale)
        assert directive["keep_decision_open"] is False
        operation = bridge.current_operation()
        assert operation.decided is DecisionStatus.STALE
        assert operation.attempt is AttemptStatus.TERMINAL_FAILURE
        assert operation.status is OperationStatus.STALE
        assert bridge.guard.refused_attempts == refusals_before, "失效 ≠ 拒绝：不写拒绝证据"

        # 再重放同版本：返回既有失效终态，不重新执行。
        replay = await bridge.before_tool(_create("c3", "项目计划2", intent="user_request"))
        assert replay is not None and replay.ok is False
        assert replay.code == "nav_version_conflict"
        assert captured["rebind"] == []

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 6) 建议 → 批准 → 成功 → 重放（申请路线的成功重放只返回既有结果）
# ---------------------------------------------------------------------------


def test_application_approval_success_replay_is_idempotent(app: AppContext):
    old = _topic(app, "旧话题")
    target = _topic(app, "目标话题")
    app.fragments.get_or_create_open(target)
    ctx = _make_turn(app, topic_id=old)
    approvals = FakeApprovals(decisions=[{"decision": "approved"}])
    bridge, captured = _make_bridge(app, ctx, approvals)

    async def scenario():
        call1 = _switch("c1", target)
        assert await bridge.before_tool(call1) is None
        first = await app.registry.execute(call1)
        assert first.ok is True
        await bridge.after_tool(call1, first)

        replay = await bridge.before_tool(_switch("c2", target))
        assert replay is not None and replay.ok is True
        assert replay.content == first.content
        assert len(approvals.requests) == 1, "成功后重放不得重复发起申请"
        assert await bridge.after_tool(_switch("c2", target), replay) is None

    asyncio.run(scenario())
    operation = bridge.current_operation()
    assert operation.decided is DecisionStatus.APPROVED
    assert operation.attempt is AttemptStatus.SUCCEEDED
    assert captured["rebind"] == [target]
