"""R14-C / F07：精确执行参数决定批准版本（查重归一化只作证据）。

最小失败场景（旧行为，修复前会红）：
* normalize_topic_name 把空格 / 全角空格 / 引号 / 冒号全去掉并用于 canonical_params，
  于是「Data Science」与「DataScience」共享同一个 param_version / 批准身份 ——
  模型把用户已批准的名称改成另一个真实名称时，旧批准会被直接复用，等于无确认创建；
* 用户弹窗编辑确认的最终名称没有和模型自行改名区分开。

修复口径：execution_params（只外层 strip）决定 param_version / 批准身份；
dedup_key（宽松归一化）只作查重证据。用户编辑确认的最终名称写入同一操作新版本，
不再重复问；模型后来自行改名推进版本、旧批准失效。
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
    TopicOperation,
    dedup_key,
    execution_params,
    param_digest,
)
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.approval import ApprovalResult

pytestmark = pytest.mark.topic_application


@pytest.fixture()
def app(tmp_path: Path) -> AppContext:
    conn = connect(tmp_path / "r14c_f07.db")
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


def _topic_id_by_name(app: AppContext, name: str) -> str | None:
    row = app.conn.execute(
        "SELECT id FROM nodes WHERE type = 'topic' AND name = ?", (name,)
    ).fetchone()
    return row["id"] if row is not None else None


def _make_turn(app: AppContext, *, topic_id: str, message: str = "问题"):
    fragment = app.fragments.get_or_create_open(topic_id)
    turn_id = "turn-r14c-f07"
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
        app.fragments.get_or_create_open(topic_id)
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


# ---------------------------------------------------------------------------
# 1) 执行参数 vs 查重归一化：显式分开，宽松归一化只作证据
# ---------------------------------------------------------------------------


def test_execution_params_keep_inner_characters_while_dedup_normalizes():
    base = execution_params("topic_create", {"name": "  Data Science  "})
    assert base == {"name": "Data Science"}, "只去掉外层空白，内部空格是执行身份的一部分"

    variants = [
        "DataScience",
        "Data Science",
        "Data　Science",  # 全角空格
        'Data "Science"',
        "Data：Science",
    ]
    digests = {param_digest("topic_create", {"name": name}) for name in variants}
    assert len(digests) == len(variants), "任何实际存储会变化的字符都必须算参数变化"

    dedups = {dedup_key("topic_create", {"name": name}) for name in variants}
    assert len(dedups) == 1, "查重归一化只作证据：宽松形相同"

    assert execution_params("topic_switch", {"topic_id": " t1 "}) == {"topic_id": "t1"}


def test_update_params_advances_version_on_inner_change_and_resets_state():
    operation = TopicOperation(
        operation_id="topop-f07",
        turn_id="turn-f07",
        kind="topic_create",
        params={"name": "Data Science"},
    )
    operation.decide(DecisionStatus.APPROVED)
    operation.begin_attempt()
    operation.conflict = ConflictStatus.CONFLICT_PENDING

    assert operation.update_params({"name": "Data Science"}) is False, "参数不变不推进版本"
    assert operation.param_version == 1
    assert operation.decided is DecisionStatus.APPROVED

    assert operation.update_params({"name": "DataScience"}) is True, "内部空格变化 = 参数变化"
    assert operation.param_version == 2
    assert operation.decided is DecisionStatus.NONE, "新版本 = 新身份，旧批准失效"
    assert operation.attempt is AttemptStatus.NONE
    assert operation.conflict is ConflictStatus.NONE
    assert operation.dedup_name == "DataScience"

    assert operation.update_params({"name": "Data　Science"}) is True, "全角空格也是实际字符"
    assert operation.param_version == 3


# ---------------------------------------------------------------------------
# 2) 旧版本的批准不能迁移到新版本：改名后必须重新申请
# ---------------------------------------------------------------------------


def test_model_name_change_requires_new_application(app: AppContext):
    old = _topic(app, "旧话题")
    ctx = _make_turn(app, topic_id=old)
    approvals = FakeApprovals(
        decisions=[{"decision": "approved"}, {"decision": "approved"}]
    )
    bridge, _captured = _make_bridge(app, ctx, approvals)

    async def scenario():
        call1 = _create("c1", "Data Science")
        assert await bridge.before_tool(call1) is None
        assert approvals.requests[0]["payload"]["new_name"] == "Data Science"
        assert approvals.requests[0]["payload"]["param_version"] == 1
        # 模拟工具还没执行，模型下一轮自行把名称改成另一个实际名称：
        call2 = _create("c2", "DataScience")
        assert await bridge.before_tool(call2) is None, "新版本需要新的申请（批准后放行）"
        assert len(approvals.requests) == 2, "旧版本的批准不得迁移到新版本"
        assert approvals.requests[1]["payload"]["new_name"] == "DataScience"
        assert approvals.requests[1]["payload"]["param_version"] == 2
        assert approvals.requests[1]["payload"]["operation_id"] == (
            approvals.requests[0]["payload"]["operation_id"]
        ), "改名不换操作身份，只推进参数版本"

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 3) 用户弹窗编辑确认的最终名称 → 同一操作新版本，执行用该名称且不再重复问
# ---------------------------------------------------------------------------


def test_user_edited_name_becomes_new_version_and_is_executed_once(app: AppContext):
    old = _topic(app, "旧话题")
    ctx = _make_turn(app, topic_id=old)
    approvals = FakeApprovals(
        decisions=[{"decision": "approved", "edits": {"name": "DataScience"}}]
    )
    bridge, captured = _make_bridge(app, ctx, approvals)

    async def scenario():
        call = _create("c1", "Data Science")
        assert await bridge.before_tool(call) is None
        assert call.arguments["name"] == "DataScience", "执行用用户确认的最终名称"
        operation = bridge.current_operation()
        assert operation.param_version == 2, "用户确认的最终名称写入同一操作新版本"
        finished = await app.registry.execute(call)
        assert finished.ok is True, finished.error
        await bridge.after_tool(call, finished)
        # 同一版本重放：返回既有结果，不再发起第二次普通申请。
        replay = await bridge.before_tool(_create("c2", "DataScience"))
        assert replay is not None and replay.ok is True
        assert len(approvals.requests) == 1

    asyncio.run(scenario())
    assert _topic_id_by_name(app, "DataScience") is not None
    assert _topic_id_by_name(app, "Data Science") is None, "不得无确认创建另一个真实名称"
    assert captured["rebind"]


# ---------------------------------------------------------------------------
# 4) 用户确认 A 后模型又改回 B：旧批准（v2）不能借给 v3
# ---------------------------------------------------------------------------


def test_model_change_after_user_edit_invalidates_previous_approval(app: AppContext):
    old = _topic(app, "旧话题")
    ctx = _make_turn(app, topic_id=old)
    approvals = FakeApprovals(
        decisions=[
            {"decision": "approved", "edits": {"name": "DataScience"}},
            {"decision": "rejected"},
        ]
    )
    bridge, _captured = _make_bridge(app, ctx, approvals)

    async def scenario():
        call1 = _create("c1", "Data Science")
        assert await bridge.before_tool(call1) is None
        assert bridge.current_operation().param_version == 2
        # 模型（不是用户）又自行改回 "Data Science"：推进到 v3，旧批准不迁移。
        refused = await bridge.before_tool(_create("c2", "Data Science"))
        assert refused is not None and refused.ok is False
        assert len(approvals.requests) == 2
        assert approvals.requests[1]["payload"]["param_version"] == 3
        assert bridge.current_operation().decided is DecisionStatus.REFUSED

    asyncio.run(scenario())
    assert _topic_id_by_name(app, "DataScience") is None
    assert _topic_id_by_name(app, "Data Science") is None


# ---------------------------------------------------------------------------
# 5) A+C 接缝回归（F 发现）：v1 冲突待确认 → v2 改名 → 拒绝新版本
# ---------------------------------------------------------------------------


def test_new_version_refusal_clears_old_conflict_and_keep_fact(app: AppContext):
    """旧 v1 的 conflict/KEEP 事实不得影响 v2 的终态（否则本轮永不发正式回答）。"""
    old = _topic(app, "旧话题")
    _topic(app, "DataScience")  # 相似名：制造 v1 查重冲突
    ctx = _make_turn(app, topic_id=old)
    approvals = FakeApprovals(decisions=[{"decision": "approved"}])
    bridge, _captured = _make_bridge(app, ctx, approvals)
    app.registry.get("create_topic").approvals = approvals

    async def scenario():
        # v1：建议路线申请被批准 → 工具返回相似冲突 → keep_decision_open=True
        call1 = _create("c1", "Data Science")
        assert await bridge.before_tool(call1) is None
        conflict = await app.registry.execute(call1)
        assert conflict.code == "topic_create_conflict"
        directive = await bridge.after_tool(call1, conflict)
        assert directive["keep_decision_open"] is True
        operation = bridge.current_operation()
        assert operation.param_version == 1
        assert bridge.has_incomplete_operation() is True

        # v2：模型把实际名称改成另一个实际名称（同 operation 新 param_version）
        # → 新申请；用户拒绝新版本。
        approvals.decisions = [{"decision": "rejected"}]
        call2 = _create("c2", "DataScience")
        refused = await bridge.before_tool(call2)
        assert refused is not None and refused.ok is False
        operation = bridge.current_operation()
        assert operation.param_version == 2, "改名推进参数版本"
        assert operation.decided is DecisionStatus.REFUSED
        assert operation.conflict is ConflictStatus.NONE, "旧 v1 冲突不得留在 v2 终态"
        assert operation.attempt is AttemptStatus.TERMINAL_FAILURE
        # 桥回传给 loop 的终态事实：旧批次的 keep 不得再拦住正式回答。
        assert refused.facts["keep_decision_open"] is False
        assert refused.facts["operation_progress"] == "terminal"
        assert bridge.has_incomplete_operation() is False
        assert bridge.operation_progress() == "terminal"

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 6) 参数不变的重试仍是同一版本（合法重试，不推进版本）
# ---------------------------------------------------------------------------


def test_same_exact_name_retry_keeps_param_version(app: AppContext):
    old = _topic(app, "旧话题")
    ctx = _make_turn(app, topic_id=old)
    approvals = FakeApprovals(decisions=[{"decision": "approved"}])
    bridge, _captured = _make_bridge(app, ctx, approvals)

    async def scenario():
        call1 = _create("c1", "同一个话题名")
        assert await bridge.before_tool(call1) is None
        operation = bridge.current_operation()
        version = operation.param_version
        # 外层空白不同但执行参数相同：同一版本，不推进参数版本、不重复发起申请。
        # （第一次已放行等待执行，因此这里得到「执行中」而不是再次放行 —— 不重复执行。）
        replay = await bridge.before_tool(_create("c2", "  同一个话题名  "))
        assert replay is not None and replay.code == "topic_application_executing"
        assert bridge.current_operation().param_version == version
        assert len(approvals.requests) == 1, "执行参数相同的重试不推进版本、不重复申请"

    asyncio.run(scenario())
