"""R14-C / F05（执行侧）：话题操作继承决策快照，不在工具返回时补当前版本。

契约 §3.1：TurnContext 在 begin 建立判断上下文时冻结 turn_nav_version / topic /
fragment / user_message；TopicOperation 的 base_nav_version / from_topic /
from_fragment 必须继承该快照。工具返回时用当前版本补快照，会让旧模型结果覆盖
用户后来的手动选择（等待申请期间用户切走 / 返回 / 同话题换片段）。

同时验证 C 提供给 A 的因果更新方法：同轮自己成功导航后，A 调
note_decision_navigation(topic_id, fragment_id, nav_version) 把快照写成真实结果，
不推进导航版本；后续同轮操作以新快照为起点。
"""

from __future__ import annotations

import asyncio
import json
import types
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
from agent.services.topic_operation import DecisionStatus
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.approval import ApprovalResult

pytestmark = pytest.mark.topic_application


@pytest.fixture()
def app(tmp_path: Path) -> AppContext:
    conn = connect(tmp_path / "r14c_f05.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    yield ctx


class FakeApprovals:
    def __init__(self, *, decisions: list[dict] | None = None) -> None:
        self.decisions = list(decisions or [])
        self.requests: list[dict] = []
        self.executions: list[dict] = []

    async def request(self, kind: str, payload: dict) -> ApprovalResult:
        self.requests.append({"kind": kind, "payload": json.loads(json.dumps(payload))})
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


def _topic(app: AppContext, name: str) -> str:
    return app.topics.nodes.create_topic(name).id


def _make_turn(app: AppContext, *, topic_id: str, message: str = "问题"):
    fragment = app.fragments.get_or_create_open(topic_id)
    turn_id = "turn-r14c-f05"
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
    return ctx


def _make_bridge(app: AppContext, ctx, approvals: FakeApprovals):
    app.approvals = approvals
    captured: dict[str, list] = {"rebind": [], "systems": []}

    async def _rebind(topic_id: str) -> str:
        captured["rebind"].append(topic_id)
        app.bindings.rebind_topic_from_tool_nav(ctx.turn_id, topic_id)
        target = app.fragments.get_or_create_open(topic_id)
        if ctx.user_message_id:
            app.memory.move_message(ctx.user_message_id, to_fragment_id=target.id)
        app.bindings.record_binding(ctx.turn_id, topic_id, fragment_id=target.id)
        ctx.bound_topic = topic_id
        ctx.bound_fragment_id = target.id
        return topic_id

    async def _rebuild(topic_id: str) -> str:
        captured["systems"].append(topic_id)
        return f"SYS:{topic_id}"

    return TopicApplicationBridge(
        app, ctx, rebind_turn=_rebind, rebuild_system=_rebuild
    ), captured


def _snapshot(ctx, *, nav_version: int, topic_id: str | None, fragment_id: str | None):
    ctx.decision_nav_version = nav_version
    ctx.decision_topic_id = topic_id
    ctx.decision_fragment_id = fragment_id


# ---------------------------------------------------------------------------
# 1) 决策形成后用户又导航：操作继承快照（旧起点），确认前复核判定失效
# ---------------------------------------------------------------------------


def test_operation_inherits_decision_snapshot_not_later_navigation(app: AppContext):
    old = _topic(app, "旧话题")
    target = _topic(app, "目标话题")
    other = _topic(app, "用户后来去的话题")
    app.fragments.get_or_create_open(target)
    ctx = _make_turn(app, topic_id=old)
    old_fragment = ctx.bound_fragment_id
    snapshot_version = navigation_version.current(app.conn)
    _snapshot(ctx, nav_version=snapshot_version, topic_id=old, fragment_id=old_fragment)
    # 用户在本轮判断形成之后手动切走（真实导航：版本推进、Anchor 改走）。
    app.navigation.enter_topic(other)
    assert navigation_version.current(app.conn) != snapshot_version

    approvals = FakeApprovals(decisions=[{"decision": "approved"}])
    bridge, captured = _make_bridge(app, ctx, approvals)

    async def scenario():
        call = ToolCall(id="c1", name="switch_topic", arguments={"topic_id": target})
        result = await bridge.before_tool(call)
        payload = approvals.requests[0]["payload"]
        assert payload["base_nav_version"] == snapshot_version, "base 必须来自决策快照"
        assert payload["from_topic_id"] == old, "from_topic 必须来自决策快照"
        assert payload["from_fragment_id"] == old_fragment
        # 用户批准后、执行前复核：真实版本已变 → 失效；不执行、不覆盖用户新选择。
        assert payload["base_nav_version"] != navigation_version.current(app.conn)
        assert result is not None and result.ok is False
        assert result.code == "topic_application_stale"
        assert result.facts["base_nav_version"] == snapshot_version
        assert result.facts["current_nav_version"] != snapshot_version

    asyncio.run(scenario())
    assert captured["rebind"] == []
    assert app.navigation.anchors.get_active().topic_id == other, "不得覆盖用户的新选择"


def test_confirm_precheck_after_snapshot_change_returns_stale(app: AppContext):
    old = _topic(app, "旧话题")
    target = _topic(app, "目标话题")
    other = _topic(app, "用户后来去的话题")
    app.fragments.get_or_create_open(target)
    ctx = _make_turn(app, topic_id=old)
    snapshot_version = navigation_version.current(app.conn)
    _snapshot(
        ctx,
        nav_version=snapshot_version,
        topic_id=old,
        fragment_id=ctx.bound_fragment_id,
    )
    # 等待确认期间用户手动切走：版本推进。
    app.navigation.enter_topic(other)
    approvals = FakeApprovals(decisions=[{"decision": "approved"}])
    bridge, captured = _make_bridge(app, ctx, approvals)

    async def scenario():
        result = await bridge.before_tool(
            ToolCall(id="c1", name="switch_topic", arguments={"topic_id": target})
        )
        assert result is not None and result.ok is False
        assert result.code == "topic_application_stale"
        assert result.facts["base_nav_version"] == snapshot_version
        assert result.facts["current_nav_version"] != snapshot_version
        assert getattr(ctx, "topic_refusal_scope", None) is None, "失效 ≠ 拒绝"

    asyncio.run(scenario())
    assert captured["rebind"] == []
    assert app.navigation.anchors.get_active().topic_id == other


# ---------------------------------------------------------------------------
# 2) 无快照的旧路径：退回既有「当前值」口径（子任务 / 旧测试替身不破坏）
# ---------------------------------------------------------------------------


def test_old_path_without_snapshot_keeps_current_value_behavior(app: AppContext):
    old = _topic(app, "旧话题")
    target = _topic(app, "目标话题")
    app.fragments.get_or_create_open(target)
    ctx = _make_turn(app, topic_id=old)
    # 旧替身：没有 decision_* 字段。
    legacy_ctx = types.SimpleNamespace(
        turn_id=ctx.turn_id,
        message=ctx.message,
        user_message_id=ctx.user_message_id,
        current_topic=old,
        bound_topic=old,
        bound_fragment_id=ctx.bound_fragment_id,
    )
    assert not hasattr(legacy_ctx, "decision_nav_version")
    current = navigation_version.current(app.conn)
    approvals = FakeApprovals(decisions=[{"decision": "rejected"}])
    bridge, _captured = _make_bridge(app, legacy_ctx, approvals)

    async def scenario():
        await bridge.before_tool(
            ToolCall(id="c1", name="switch_topic", arguments={"topic_id": target})
        )
        payload = approvals.requests[0]["payload"]
        assert payload["base_nav_version"] == current
        assert payload["from_topic_id"] == old
        assert payload["from_fragment_id"] == ctx.bound_fragment_id

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 3) 因果更新：同轮自己成功导航 → 快照写成真实结果、不推进版本、后续操作继承
# ---------------------------------------------------------------------------


def test_causal_navigation_update_from_success_directive(app: AppContext):
    old = _topic(app, "旧话题")
    target = _topic(app, "目标话题")
    app.fragments.get_or_create_open(target)
    ctx = _make_turn(app, topic_id=old)
    snapshot_version = navigation_version.current(app.conn)
    _snapshot(ctx, nav_version=snapshot_version, topic_id=old, fragment_id=ctx.bound_fragment_id)

    approvals = FakeApprovals(
        decisions=[{"decision": "approved"}, {"decision": "approved"}]
    )
    bridge, _captured = _make_bridge(app, ctx, approvals)

    async def scenario():
        call = ToolCall(id="c1", name="switch_topic", arguments={"topic_id": target})
        assert await bridge.before_tool(call) is None
        executed = await app.registry.execute(call)
        assert executed.ok is True
        directive = await bridge.after_tool(call, executed)
        # 权威导航结果随 directive 回传（真实目标 + 写入后的版本），不是猜值。
        assert directive["topic_id"] == target
        assert directive["nav_version"] == navigation_version.current(app.conn)
        assert directive["nav_version"] == snapshot_version + 1

        version_before = navigation_version.current(app.conn)
        snapshot = bridge.note_decision_navigation(
            directive["topic_id"],
            directive.get("fragment_id"),
            directive["nav_version"],
        )
        assert snapshot["topic_id"] == target
        assert ctx.decision_topic_id == target
        assert ctx.decision_nav_version == directive["nav_version"]
        assert navigation_version.current(app.conn) == version_before, "因果更新不推进导航版本"
        # 幂等：同值重复调用无副作用。
        again = bridge.note_decision_navigation(
            directive["topic_id"],
            directive.get("fragment_id"),
            directive["nav_version"],
        )
        assert again == snapshot
        assert navigation_version.current(app.conn) == version_before

        # 后续同轮新操作以新快照为起点。
        create = ToolCall(id="c2", name="create_topic", arguments={"name": "下一件事"})
        assert await bridge.before_tool(create) is None
        payload = approvals.requests[1]["payload"]
        assert payload["from_topic_id"] == target
        assert payload["base_nav_version"] == directive["nav_version"]

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 4) 同轮连续导航：新 param_version 继承因果更新后的快照（不拿旧版本 CAS）
# ---------------------------------------------------------------------------


def test_same_turn_second_navigation_uses_causal_snapshot(app: AppContext):
    old = _topic(app, "旧话题")
    first_target = _topic(app, "第一站")
    second_target = _topic(app, "第二站")
    for topic in (first_target, second_target):
        app.fragments.get_or_create_open(topic)
    ctx = _make_turn(app, topic_id=old)
    snapshot_version = navigation_version.current(app.conn)
    _snapshot(ctx, nav_version=snapshot_version, topic_id=old, fragment_id=ctx.bound_fragment_id)
    approvals = FakeApprovals()
    bridge, captured = _make_bridge(app, ctx, approvals)

    async def scenario():
        first = ToolCall(
            id="c1", name="switch_topic", arguments={"topic_id": first_target, "intent": "user_request"}
        )
        assert await bridge.before_tool(first) is None
        executed = await app.registry.execute(first)
        assert executed.ok is True, executed.error
        directive = await bridge.after_tool(first, executed)
        # A 在 loop 里做因果更新（同轮自己成功导航，不推进版本）。
        bridge.note_decision_navigation(
            directive["topic_id"], directive.get("fragment_id"), directive["nav_version"]
        )
        version_after_first = navigation_version.current(app.conn)
        assert directive["nav_version"] == version_after_first

        # 第二个操作（同 kind、新参数版本）：继承新快照，不应拿旧版本 CAS。
        second = ToolCall(
            id="c2", name="switch_topic", arguments={"topic_id": second_target, "intent": "user_request"}
        )
        assert await bridge.before_tool(second) is None
        operation = bridge.current_operation()
        assert operation.param_version == 2
        assert operation.base_nav_version == version_after_first
        assert operation.from_topic == first_target
        executed_second = await app.registry.execute(second)
        assert executed_second.ok is True, executed_second.error
        await bridge.after_tool(second, executed_second)

    asyncio.run(scenario())
    assert app.navigation.anchors.get_active().topic_id == second_target
    assert captured["rebind"] == [first_target, second_target]


def test_same_turn_second_navigation_without_causal_update_is_stale(app: AppContext):
    """最小失败场景：不做因果更新时，第二次导航拿旧 base → 工具 CAS 如实失败。"""
    old = _topic(app, "旧话题")
    first_target = _topic(app, "第一站")
    second_target = _topic(app, "第二站")
    for topic in (first_target, second_target):
        app.fragments.get_or_create_open(topic)
    ctx = _make_turn(app, topic_id=old)
    _snapshot(
        ctx,
        nav_version=navigation_version.current(app.conn),
        topic_id=old,
        fragment_id=ctx.bound_fragment_id,
    )
    bridge, _captured = _make_bridge(app, ctx, FakeApprovals())

    async def scenario():
        first = ToolCall(
            id="c1", name="switch_topic", arguments={"topic_id": first_target, "intent": "user_request"}
        )
        assert await bridge.before_tool(first) is None
        executed = await app.registry.execute(first)
        assert executed.ok is True
        await bridge.after_tool(first, executed)
        # 故意不调用 note_decision_navigation：快照仍是旧版本。
        second = ToolCall(
            id="c2", name="switch_topic", arguments={"topic_id": second_target, "intent": "user_request"}
        )
        assert await bridge.before_tool(second) is None
        blocked = await app.registry.execute(second)
        assert blocked.ok is False and blocked.code == "nav_version_conflict"
        await bridge.after_tool(second, blocked)
        assert bridge.current_operation().decided is DecisionStatus.STALE

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 5) 排队事实只作标记，不参与运算
# ---------------------------------------------------------------------------


def test_queued_flag_is_fact_only(app: AppContext):
    old = _topic(app, "旧话题")
    target = _topic(app, "目标话题")
    app.fragments.get_or_create_open(target)
    ctx = _make_turn(app, topic_id=old)
    snapshot_version = navigation_version.current(app.conn)
    _snapshot(ctx, nav_version=snapshot_version, topic_id=old, fragment_id=ctx.bound_fragment_id)
    ctx.decision_queued = True
    approvals = FakeApprovals(decisions=[{"decision": "rejected"}])
    bridge, _captured = _make_bridge(app, ctx, approvals)

    async def scenario():
        await bridge.before_tool(
            ToolCall(id="c1", name="switch_topic", arguments={"topic_id": target})
        )
        payload = approvals.requests[0]["payload"]
        assert payload["base_nav_version"] == snapshot_version
        assert payload["from_topic_id"] == old

    asyncio.run(scenario())
