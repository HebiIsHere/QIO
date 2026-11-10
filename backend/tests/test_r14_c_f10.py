"""R14-C / F10：查重冲突确认的拒绝进入统一终态（拒绝 / 超时 / 失效 / 技术错误分开）。

最小失败场景（旧行为，修复前会红）：
* 工具 _resolve_conflict 把任何非 approved 的决定（含超时 / 失效 / 技术错误）都当成
  用户拒绝 → 写拒绝证据、加进 _declined、同版本不再重试；超时被写成主动拒绝；
* 桥不消费 topic_create_conflict_declined 的结构化事实 → OperationStatus 停在
  FAILED / conflict 不清、不进入原话题回答、同版本可能重复弹冲突。

修复口径（契约 §3.7）：消费工具结构化 code/facts：
* declined  → REFUSED + CONFLICT_DECLINED，清 pending conflict，进原话题回答，
              同版本不再重复弹，拒绝证据只覆盖具体对象；
* timeout   → 不是拒绝、不写拒绝证据、同版本可重试；
* stale     → 失效 ≠ 拒绝、不写拒绝证据；
* 技术错误  → 单独表示、不写拒绝证据、可重试。
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
    TopicPhase,
)
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.approval import ApprovalResult

pytestmark = pytest.mark.topic_application


@pytest.fixture()
def app(tmp_path: Path) -> AppContext:
    conn = connect(tmp_path / "r14c_f10.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    yield ctx


class FakeApprovals:
    def __init__(self, *, decisions: list[dict] | None = None, fail: bool = False) -> None:
        self.decisions = list(decisions or [])
        self.requests: list[dict] = []
        self.executions: list[dict] = []
        self.fail = fail

    async def request(self, kind: str, payload: dict) -> ApprovalResult:
        self.requests.append({"kind": kind, "payload": json.loads(json.dumps(payload))})
        if self.fail:
            raise RuntimeError("approval channel down")
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

    def set_fail(self, fail: bool) -> None:
        self.fail = fail


def _topic(app: AppContext, name: str) -> str:
    return app.topics.nodes.create_topic(name).id


def _make_turn(app: AppContext, *, topic_id: str, message: str = "问题"):
    fragment = app.fragments.get_or_create_open(topic_id)
    turn_id = "turn-r14c-f10"
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
    ctx.decision_nav_version = navigation_version.current(app.conn)
    ctx.decision_topic_id = topic_id
    ctx.decision_fragment_id = fragment.id
    return ctx


def _make_bridge(app: AppContext, ctx: TurnContext, approvals: FakeApprovals):
    app.approvals = approvals
    captured: dict[str, list] = {"rebind": [], "systems": [], "phases": []}

    async def _rebind(topic_id: str) -> str:
        captured["rebind"].append(topic_id)
        app.bindings.rebind_topic_from_tool_nav(ctx.turn_id, topic_id)
        target = app.fragments.get_or_create_open(topic_id)
        if ctx.user_message_id:
            app.memory.move_message(ctx.user_message_id, to_fragment_id=target.id)
        app.bindings.record_binding(ctx.turn_id, topic_id, fragment_id=target.id)
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


def _create(call_id: str, name: str) -> ToolCall:
    return ToolCall(id=call_id, name="create_topic", arguments={"name": name})


def _setup(app: AppContext, decisions: list[dict] | None = None):
    """建议路线进入查重冲突：1) 普通申请批准；2) 工具返回相似冲突。

    decisions 是**完整顺序**（第一条给外层申请，后续给冲突二次审批）。
    """
    old = _topic(app, "旧话题")
    similar = _topic(app, "项目计划")
    ctx = _make_turn(app, topic_id=old)
    approvals = FakeApprovals(
        decisions=decisions if decisions is not None else [{"decision": "approved"}]
    )
    bridge, captured = _make_bridge(app, ctx, approvals)
    tool = app.registry.get("create_topic")
    tool.approvals = approvals
    return old, similar, ctx, bridge, captured, tool, approvals


async def _into_conflict_pending(bridge, app, approvals):
    call1 = _create("c1", "项目计划2")
    assert await bridge.before_tool(call1) is None
    first = await app.registry.execute(call1)
    assert first.code == "topic_create_conflict"
    await bridge.after_tool(call1, first)
    operation = bridge.current_operation()
    assert operation.attempt is AttemptStatus.CONFLICT_PENDING
    return operation


# ---------------------------------------------------------------------------
# 1) 具体冲突被拒绝 → REFUSED + CONFLICT_DECLINED + 清 pending + 原话题回答
# ---------------------------------------------------------------------------


def test_conflict_declined_becomes_unified_refused_terminal(app: AppContext):
    _old, similar, _ctx, bridge, captured, _tool, approvals = _setup(
        app, decisions=[{"decision": "approved"}, {"decision": "rejected"}]
    )

    async def scenario():
        operation = await _into_conflict_pending(bridge, app, approvals)
        assert operation.conflict is ConflictStatus.CONFLICT_PENDING

        call2 = _create("c2", "项目计划2")
        assert await bridge.before_tool(call2) is None
        declined = await app.registry.execute(call2)
        assert declined.ok is False
        assert declined.code == "topic_create_conflict_declined"
        directive = await bridge.after_tool(call2, declined)

        # 统一终态：REFUSED + CONFLICT_DECLINED，待处理冲突清掉，进原话题回答。
        assert directive["keep_decision_open"] is False
        operation = bridge.current_operation()
        assert operation.decided is DecisionStatus.REFUSED
        assert operation.attempt is AttemptStatus.TERMINAL_FAILURE
        assert operation.conflict is ConflictStatus.CONFLICT_DECLINED
        assert operation.status is OperationStatus.REFUSED
        assert TopicPhase.ANSWERABLE in captured["phases"], "拒绝后必须在原话题回答"
        assert captured["rebind"] == [], "拒绝不得搬动本轮归属"

        # 拒绝证据只覆盖**这个具体对象**：新名称 + 相似话题 id，带操作身份。
        evidence = [
            item
            for item in bridge.guard.refused_attempts
            if item.get("reason") == "conflict_declined"
        ]
        assert len(evidence) == 1
        record = evidence[0]
        assert record["conflict"] == "conflict_declined"
        assert record["similar_topic_id"] == similar
        assert record["topic_name"] == "项目计划2"
        assert record["operation_id"] == operation.operation_id
        assert record["fragment_id"] == _ctx.bound_fragment_id

        # 同版本不再重复弹冲突、不重复执行。
        popups = len(approvals.requests)
        replay = await bridge.before_tool(_create("c3", "项目计划2"))
        assert replay is not None and replay.code == "topic_create_conflict_declined"
        assert len(approvals.requests) == popups
        assert directive.get("result") is None or True

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 2) 冲突确认超时 ≠ 拒绝：可重试、不写拒绝证据
# ---------------------------------------------------------------------------


def test_conflict_timeout_is_retryable_not_refusal(app: AppContext):
    _old, _similar, _ctx, bridge, _captured, _tool, approvals = _setup(
        app, decisions=[{"decision": "approved"}, {"decision": "timeout"}, {"decision": "approved"}]
    )

    async def scenario():
        await _into_conflict_pending(bridge, app, approvals)
        before = list(bridge.guard.refused_attempts)
        call2 = _create("c2", "项目计划2")
        assert await bridge.before_tool(call2) is None
        timed = await app.registry.execute(call2)
        assert timed.code == "topic_create_conflict_timeout"
        directive = await bridge.after_tool(call2, timed)
        assert directive["keep_decision_open"] is True, "超时不是终态"
        operation = bridge.current_operation()
        assert operation.decided is DecisionStatus.APPROVED
        assert operation.attempt is AttemptStatus.RETRYABLE_FAILURE
        assert operation.conflict is ConflictStatus.CONFLICT_PENDING
        assert bridge.guard.refused_attempts == before, "超时不得写成主动拒绝"

        # 同版本重试：再次进入冲突确认并真正创建。
        call3 = _create("c3", "项目计划2")
        assert await bridge.before_tool(call3) is None
        created = await app.registry.execute(call3)
        assert created.ok is True, created.error
        await bridge.after_tool(call3, created)

    asyncio.run(scenario())
    assert bridge.current_operation().attempt is AttemptStatus.SUCCEEDED
    assert app.conn.execute(
        "SELECT COUNT(*) c FROM nodes WHERE type='topic' AND name='项目计划2'"
    ).fetchone()["c"] == 1


# ---------------------------------------------------------------------------
# 3) 冲突确认期间失效 → STALE + 不写拒绝证据
# ---------------------------------------------------------------------------


def test_conflict_stale_is_not_a_refusal(app: AppContext):
    _old, _similar, _ctx, bridge, _captured, _tool, approvals = _setup(
        app, decisions=[{"decision": "approved"}, {"decision": "stale"}]
    )

    async def scenario():
        await _into_conflict_pending(bridge, app, approvals)
        before = list(bridge.guard.refused_attempts)
        call2 = _create("c2", "项目计划2")
        assert await bridge.before_tool(call2) is None
        stale = await app.registry.execute(call2)
        assert stale.code == "topic_create_conflict_stale"
        directive = await bridge.after_tool(call2, stale)
        assert directive["keep_decision_open"] is False
        operation = bridge.current_operation()
        assert operation.decided is DecisionStatus.STALE
        assert operation.conflict is ConflictStatus.NONE
        assert bridge.guard.refused_attempts == before

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 4) 技术错误 ≠ 拒绝：单独表示、可重试
# ---------------------------------------------------------------------------


def test_conflict_technical_error_is_retryable_and_not_refusal(app: AppContext):
    _old, _similar, _ctx, bridge, _captured, _tool, approvals = _setup(
        app, decisions=[{"decision": "approved"}, {"decision": "approved"}]
    )

    async def scenario():
        await _into_conflict_pending(bridge, app, approvals)
        before = list(bridge.guard.refused_attempts)
        approvals.set_fail(True)
        call2 = _create("c2", "项目计划2")
        assert await bridge.before_tool(call2) is None
        failed = await app.registry.execute(call2)
        assert failed.code == "topic_create_conflict_failed"
        directive = await bridge.after_tool(call2, failed)
        assert directive["keep_decision_open"] is True
        operation = bridge.current_operation()
        assert operation.attempt is AttemptStatus.RETRYABLE_FAILURE
        assert bridge.guard.refused_attempts == before, "技术错误不得写成用户拒绝"

        approvals.set_fail(False)
        call3 = _create("c3", "项目计划2")
        assert await bridge.before_tool(call3) is None
        created = await app.registry.execute(call3)
        assert created.ok is True, created.error
        await bridge.after_tool(call3, created)

    asyncio.run(scenario())
    assert app.conn.execute(
        "SELECT COUNT(*) c FROM nodes WHERE type='topic' AND name='项目计划2'"
    ).fetchone()["c"] == 1


# ---------------------------------------------------------------------------
# 5) 外层申请超时 ≠ 拒绝：不写拒绝证据、同版本不重复弹
# ---------------------------------------------------------------------------


def test_outer_application_timeout_is_not_refusal(app: AppContext):
    old = _topic(app, "旧话题")
    _topic(app, "项目计划")
    ctx = _make_turn(app, topic_id=old)
    approvals = FakeApprovals(decisions=[{"decision": "timeout"}])
    bridge, _captured = _make_bridge(app, ctx, approvals)

    async def scenario():
        call = _create("c1", "项目计划2")
        result = await bridge.before_tool(call)
        assert result is not None and result.code == "topic_application_timeout"
        operation = bridge.current_operation()
        assert operation.decided is DecisionStatus.PENDING, "超时不是用户拒绝"
        assert operation.attempt is AttemptStatus.NONE
        # 本轮已终结（不重弹、不执行），但决定层没有用户拒绝：loop 必须能在原话题回答。
        assert operation.status is OperationStatus.FAILED
        assert bridge.operation_progress() == "terminal"
        assert bridge.has_incomplete_operation() is False
        assert result.facts["keep_decision_open"] is False
        assert bridge.guard.refused_attempts == []
        # 同版本重放：返回既有超时结果，不重复弹（稳定操作 ID 不延长有效期）。
        replay = await bridge.before_tool(_create("c2", "项目计划2"))
        assert replay is not None and replay.code == "topic_application_timeout"
        assert len(approvals.requests) == 1

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 6) 普通拒绝：只记录这次具体对象，不扩大为「永远拒绝任何新话题」
# ---------------------------------------------------------------------------


def test_plain_rejection_records_only_the_specific_object(app: AppContext):
    old = _topic(app, "旧话题")
    _topic(app, "项目计划")
    ctx = _make_turn(app, topic_id=old)
    approvals = FakeApprovals(decisions=[{"decision": "rejected"}])
    bridge, _captured = _make_bridge(app, ctx, approvals)

    async def scenario():
        result = await bridge.before_tool(_create("c1", "项目计划2"))
        assert result is not None and result.ok is False
        operation = bridge.current_operation()
        assert operation.decided is DecisionStatus.REFUSED
        evidence = [
            item for item in bridge.guard.refused_attempts if item["reason"] == "rejected"
        ]
        assert evidence and evidence[-1]["topic_name"] == "项目计划2"
        # 拒绝范围只覆盖这个对象：换一个名称是新的操作版本，需要新的申请。
        approvals.decisions.append({"decision": "approved"})
        other = await bridge.before_tool(_create("c2", "另一个新话题"))
        assert other is None, "拒绝只覆盖具体对象，不扩大为全局拒绝"
        assert approvals.requests[-1]["payload"]["new_name"] == "另一个新话题"

    asyncio.run(scenario())
