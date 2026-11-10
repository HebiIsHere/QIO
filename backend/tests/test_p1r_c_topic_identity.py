"""P1R-C 定向测试：P1-04 申请绑定真实导航位置与版本 + P1-05 稳定 operation_id。

只证明程序事实（接线与分支衔接），不做自然语言判断，不调用真实模型：
* 申请 payload 保存 operation_id / turn_id / user_message_id / from_topic_id /
  from_fragment_id / base_nav_version / param_version；
* 同话题换片段、离开再返回、确认时起点变化 -> 旧申请失效且不执行（不写拒绝范围）；
* 仅编辑草稿 / 收起 / 与导航无关的状态变化 -> 申请仍可确认（不推进导航版本）；
* 工具实际写入前的 CAS：起点版本已变 -> nav_version_conflict，Anchor/话题零写入；
* 改名：模型初始 A -> 用户改成 B -> 工具查重冲突 -> 同 operation 同 param_version
  合法重试 -> 只创建一次，实际名称是 B；
* 参数版本变化 -> 旧批准不迁移，需要新申请；
* 已成功后的重复确认 / 重放 -> 返回既有结果，不重复创建。
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
    ConflictStatus,
    IntentSource,
    OperationStatus,
    TopicOperation,
    operation_scope,
)
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.approval import ApprovalResult

# 真实审批交互：关闭 conftest 对旧测试的「话题申请视为同意」兜底。
pytestmark = pytest.mark.topic_application


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(tmp_path: Path) -> AppContext:
    conn = connect(tmp_path / "p1r_c.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    yield ctx


class FakeApprovals:
    """可编程审批：decisions 顺序消费；manual=True 时等待 release()。"""

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
    turn_id = "turn-p1r-c"
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


def _make_bridge(app: AppContext, ctx: TurnContext, approvals: FakeApprovals):
    app.approvals = approvals
    captured: dict[str, list] = {"rebind": [], "systems": []}

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

    bridge = TopicApplicationBridge(app, ctx, rebind_turn=_rebind, rebuild_system=_rebuild)
    return bridge, captured


def _switch(call_id: str, topic_id: str) -> ToolCall:
    return ToolCall(id=call_id, name="switch_topic", arguments={"topic_id": topic_id})


def _create(call_id: str, name: str) -> ToolCall:
    return ToolCall(id=call_id, name="create_topic", arguments={"name": name})


async def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    loop_now = asyncio.get_event_loop().time()
    deadline = loop_now + timeout
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("等待条件超时")
        await asyncio.sleep(0.01)


def _anchor(app: AppContext) -> str | None:
    active = app.navigation.anchors.get_active()
    return active.topic_id if active is not None else None


def _close_fragment(app: AppContext, fragment_id: str) -> None:
    app.conn.execute(
        "UPDATE fragments SET closed_at = ?, boundary_reason = ? WHERE id = ?",
        ("2026-11-10T00:00:00+00:00", "test_close", fragment_id),
    )


# ---------------------------------------------------------------------------
# P1-04：起点快照 + 三处校验
# ---------------------------------------------------------------------------


async def test_payload_carries_full_start_snapshot(app: AppContext):
    old = _topic(app, "旧话题")
    target = _topic(app, "目标话题")
    app.fragments.get_or_create_open(target)
    ctx = _make_turn(app, topic_id=old)
    approvals = FakeApprovals(decisions=[{"decision": "rejected"}])
    bridge, _ = _make_bridge(app, ctx, approvals)

    await bridge.before_tool(_switch("c1", target))

    payload = approvals.requests[0]["payload"]
    assert payload["operation_id"].startswith("topop-")
    assert payload["turn_id"] == ctx.turn_id
    assert payload["user_message_id"] == ctx.user_message_id
    assert payload["from_topic_id"] == old
    assert payload["from_fragment_id"] == ctx.bound_fragment_id
    assert payload["base_nav_version"] == navigation_version.current(app.conn)
    assert payload["param_version"] == 1
    assert payload["target_topic_id"] == target
    assert payload["intent_source"] == "suggestion"


async def test_same_topic_fragment_change_invalidates_pending_application(app: AppContext):
    old = _topic(app, "旧话题")
    target = _topic(app, "目标话题")
    app.fragments.get_or_create_open(target)
    ctx = _make_turn(app, topic_id=old)
    approvals = FakeApprovals(decisions=[{"decision": "approved"}])
    approvals.manual = True
    bridge, captured = _make_bridge(app, ctx, approvals)

    run = asyncio.create_task(bridge.before_tool(_switch("c1", target)))
    await _wait_until(lambda: bool(approvals.requests))
    base = approvals.requests[0]["payload"]["base_nav_version"]

    # 用户在同一话题里改选历史片段（真实导航动作）：导航版本必须推进。
    old_fragment = app.bindings.binding_for(ctx.turn_id).fragment_id
    _close_fragment(app, old_fragment)
    app.navigation.register_continuation(old, old_fragment)
    assert navigation_version.current(app.conn) != base

    approvals.release("approved")
    result = await asyncio.wait_for(run, timeout=5)

    assert result is not None and result.ok is False
    assert result.code == "topic_application_stale"
    assert result.facts["why"] == "nav_version_changed"
    assert _anchor(app) == old, "旧申请不得执行，更不得改动用户的新位置"
    assert captured["rebind"] == [] and captured["systems"] == []
    assert getattr(ctx, "topic_refusal_scope", None) is None, "失效 ≠ 拒绝：不写拒绝范围"


async def test_leave_and_return_invalidates_old_application(app: AppContext):
    old = _topic(app, "旧话题")
    target = _topic(app, "目标话题")
    other = _topic(app, "用户看过的第三个话题")
    for topic in (target, other):
        app.fragments.get_or_create_open(topic)
    ctx = _make_turn(app, topic_id=old)
    approvals = FakeApprovals(decisions=[{"decision": "approved"}])
    approvals.manual = True
    bridge, _ = _make_bridge(app, ctx, approvals)

    run = asyncio.create_task(bridge.before_tool(_switch("c1", target)))
    await _wait_until(lambda: bool(approvals.requests))

    # 用户离开再返回：最终话题相同，但起点事实已经变了（版本不会相等）。
    app.navigation.enter_topic(other)
    app.navigation.enter_topic(old)
    assert _anchor(app) == old

    approvals.release("approved")
    result = await asyncio.wait_for(run, timeout=5)

    assert result is not None and result.ok is False
    assert result.code == "topic_application_stale"
    assert _anchor(app) == old
    assert getattr(ctx, "topic_refusal_scope", None) is None


async def test_draft_edit_or_collapse_does_not_invalidate_application(app: AppContext):
    old = _topic(app, "旧话题")
    target = _topic(app, "目标话题")
    app.fragments.get_or_create_open(target)
    ctx = _make_turn(app, topic_id=old)
    approvals = FakeApprovals(decisions=[{"decision": "approved"}])
    approvals.manual = True
    bridge, captured = _make_bridge(app, ctx, approvals)

    run = asyncio.create_task(bridge.before_tool(_switch("c1", target)))
    await _wait_until(lambda: bool(approvals.requests))
    base = navigation_version.current(app.conn)

    # 前端草稿编辑 / 仅收起弹窗 / 无关后台状态：不调用导航写入口，版本不动。
    await asyncio.sleep(0.05)
    assert navigation_version.current(app.conn) == base

    approvals.release("approved")
    assert await asyncio.wait_for(run, timeout=5) is None, "申请仍有效：放行原工具"
    call = _switch("c1", target)
    executed = await app.registry.execute(call)
    assert executed.ok is True, executed.error
    await bridge.after_tool(call, executed)
    assert _anchor(app) == target
    assert captured["rebind"] == [target]


async def test_confirmation_precheck_stale_does_not_execute(app: AppContext):
    old = _topic(app, "旧话题")
    target = _topic(app, "目标话题")
    app.fragments.get_or_create_open(target)
    ctx = _make_turn(app, topic_id=old)
    approvals = FakeApprovals(decisions=[{"decision": "stale"}])
    bridge, captured = _make_bridge(app, ctx, approvals)

    result = await bridge.before_tool(_switch("c1", target))

    assert result is not None and result.ok is False
    assert result.code == "topic_application_stale"
    assert result.facts["base_nav_version"] is not None
    assert _anchor(app) == old, "确认前校验失败不得执行"
    assert captured["rebind"] == []
    assert getattr(ctx, "topic_refusal_scope", None) is None
    operation = bridge.current_operation()
    assert operation is not None and operation.status is OperationStatus.STALE


async def test_tool_cas_blocks_write_when_start_changed(app: AppContext):
    old = _topic(app, "旧话题")
    target = _topic(app, "目标话题")
    app.fragments.get_or_create_open(target)
    turn_id = "turn-cas"

    operation = TopicOperation(
        operation_id="topop-cas",
        turn_id=turn_id,
        kind="topic_switch",
        intent_source=IntentSource.USER_REQUEST,
        params={"topic_id": target},
        route="direct",
        from_topic=old,
        base_nav_version=navigation_version.current(app.conn),
    )
    # 起点快照之后用户又导航了：版本与 operation 绑定值不再一致。
    app.navigation.enter_topic(old)
    before = navigation_version.current(app.conn)

    with operation_scope(operation):
        result = app.registry.get("switch_topic").run_sync(topic_id=target)

    assert result.ok is False
    assert result.code == "nav_version_conflict"
    assert _anchor(app) == old, "CAS 失败必须回滚 Anchor 写入"
    assert navigation_version.current(app.conn) == before, "失败不得推进版本"


# ---------------------------------------------------------------------------
# P1-05：改名 / 查重冲突 / 合法重试 / 参数版本
# ---------------------------------------------------------------------------


async def test_rename_then_conflict_retry_same_version_creates_once(app: AppContext):
    old = _topic(app, "旧话题")
    app.fragments.get_or_create_open(_topic(app, "项目计划"))  # 相似话题，制造查重冲突
    ctx = _make_turn(app, topic_id=old)
    tool = app.registry.get("create_topic")
    # 第一次批准带「用户改名」；第二次是工具内部查重冲突的二次审批。
    approvals = FakeApprovals(
        decisions=[
            {"decision": "approved", "edits": {"name": "项目计划2"}},
            {"decision": "approved"},
        ]
    )
    tool.approvals = approvals
    bridge, captured = _make_bridge(app, ctx, approvals)

    call1 = _create("c1", "模型起的初始名称")
    assert await bridge.before_tool(call1) is None, "申请批准后放行原工具"
    assert call1.arguments["name"] == "项目计划2", "用户改名后的真实名称写回本次调用"
    result1 = await app.registry.execute(call1)
    assert result1.ok is False and result1.code == "topic_create_conflict"
    directive1 = await bridge.after_tool(call1, result1)
    assert directive1["keep_decision_open"] is True, "查重冲突 ≠ 创建成功 ≠ 整轮结束"
    operation = bridge.current_operation()
    assert operation.conflict is ConflictStatus.CONFLICT_PENDING
    assert _topic_id_by_name(app, "项目计划2") is None, "冲突阶段不得先创建"

    # 同 operation + 同 param_version 的合法重试：进入工具内部二次审批并真正创建。
    call2 = _create("c2", "项目计划2")
    assert await bridge.before_tool(call2) is None
    result2 = await app.registry.execute(call2)
    assert result2.ok is True, result2.error
    directive2 = await bridge.after_tool(call2, result2)
    assert directive2["rebind_topic_id"] == _topic_id_by_name(app, "项目计划2")

    assert [item["kind"] for item in approvals.requests] == ["topic_create", "create_topic"]
    inner = approvals.requests[1]["payload"]
    assert inner["operation_id"] == operation.operation_id, "冲突二次审批绑定同一 operation"
    assert inner["param_version"] == operation.param_version
    rows = app.conn.execute(
        "SELECT id FROM nodes WHERE type = 'topic' AND name = ?", ("项目计划2",)
    ).fetchall()
    assert len(rows) == 1, "同 operation 同版本重试只能创建一次"
    assert _topic_id_by_name(app, "模型起的初始名称") is None
    assert captured["rebind"] == [rows[0]["id"]]


async def test_param_version_change_requires_new_application(app: AppContext):
    old = _topic(app, "旧话题")
    ctx = _make_turn(app, topic_id=old)
    approvals = FakeApprovals(decisions=[{"decision": "rejected"}, {"decision": "approved"}])
    bridge, _ = _make_bridge(app, ctx, approvals)

    refused = await bridge.before_tool(_create("c1", "话题甲"))
    assert refused is not None and refused.code.startswith("topic_application_")
    assert len(approvals.requests) == 1

    # 模型改了名称（参数版本变化）：旧拒绝/旧批准都不迁移，需要新申请。
    assert await bridge.before_tool(_create("c2", "话题乙")) is None
    assert len(approvals.requests) == 2, "参数版本变化必须建立新申请"
    assert approvals.requests[0]["payload"]["param_version"] == 1
    assert approvals.requests[1]["payload"]["param_version"] == 2
    assert approvals.requests[1]["payload"]["operation_id"] == (
        approvals.requests[0]["payload"]["operation_id"]
    ), "改名不换操作身份，只推进参数版本"

    executed = await app.registry.execute(_create("c2", "话题乙"))
    assert executed.ok is True, executed.error
    await bridge.after_tool(_create("c2", "话题乙"), executed)
    assert _topic_id_by_name(app, "话题乙") is not None
    assert _topic_id_by_name(app, "话题甲") is None


async def test_repeat_after_success_returns_existing_result_without_recreating(app: AppContext):
    old = _topic(app, "旧话题")
    ctx = _make_turn(app, topic_id=old)
    approvals = FakeApprovals(decisions=[{"decision": "approved"}])
    bridge, _ = _make_bridge(app, ctx, approvals)

    call1 = _create("c1", "只创建一次的话题")
    assert await bridge.before_tool(call1) is None
    first = await app.registry.execute(call1)
    assert first.ok is True
    await bridge.after_tool(call1, first)

    # 重复确认 / 网络重放 / 模型重试：同 operation 同 param_version 返回既有结果。
    replay = await bridge.before_tool(_create("c2", "只创建一次的话题"))
    assert replay is not None and replay.ok is True
    assert replay.content == first.content
    assert len(approvals.requests) == 1, "已成功的操作不得再次发起申请"
    rows = app.conn.execute(
        "SELECT COUNT(*) c FROM nodes WHERE type = 'topic' AND name = ?",
        ("只创建一次的话题",),
    ).fetchone()
    assert rows["c"] == 1, "重复确认不得重复创建"
