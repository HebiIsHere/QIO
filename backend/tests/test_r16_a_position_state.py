# -*- coding: utf-8 -*-
"""R16-A 定向测试：完整位置状态、两类版本与统一变更入口（契约 §2—§5/§14）。

只证明程序事实：迁移对象、两类版本职责、转换表每个 kind 的版本/Anchor/pending 结果、
单事务原子性与回滚、命令身份幂等/冲突/CAS、auto_segment 不清旧申请、
apply_continuation 不推进 nav_version、事件与快照同源。

全部用真实 AppContext + 临时 SQLite（真实迁移），不调用真实模型。
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent.api.bus import EventBus
from agent.api.events import EventType
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.graph.anchors import AnchorService
from agent.memory.fragment import new_id
from agent.services import navigation_version
from agent.services.app import AppContext
from agent.services.binding import TurnBindingService
from agent.services.position_state import (
    ALL_KINDS,
    KIND_APPLY_CONTINUATION,
    KIND_AUTO_SEGMENT,
    KIND_CANCEL_CONTINUATION,
    KIND_CREATE_TOPIC,
    KIND_ENTER_TOPIC,
    KIND_INTERNAL_REFINE,
    KIND_OPEN_FRAGMENT,
    KIND_SELECT_HISTORY,
    NAV_DECISION_KINDS,
    NavCommandIdentity,
    PositionStateService,
    PositionTransition,
    param_digest,
)
from agent.services.turn_orchestrator import BOUNDARY_MODE_KEY
from agent.storage.db import connect
from agent.storage.migrate import REQUIRED_OBJECTS, apply_migrations
from agent.storage.schema import COMPENSATION_VERSION, MIGRATIONS, SCHEMA_VERSION


# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "r16a.db")
    apply_migrations(conn)
    app = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app.credentials._kr = MemoryKeyring()
    return app


def _topic(app: AppContext, name: str = "话题") -> str:
    return app.topics.nodes.create_topic(name).id


def _seed_fragment(
    app: AppContext,
    topic_id: str,
    *,
    closed: bool = True,
    marker: str = "a",
    title: str | None = None,
) -> str:
    fragment_id = new_id("frag")
    now = _now()
    app.conn.execute(
        "INSERT INTO fragments (id, topic_id, summary, summary_version, created_at, closed_at) "
        "VALUES (?, ?, ?, 1, ?, ?)",
        (fragment_id, topic_id, "摘要", now, now if closed else None),
    )
    app.conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
        "VALUES (?, ?, 'user', ?, 'text', ?)",
        (new_id("msg"), fragment_id, f"原文-{marker}", now),
    )
    if closed:
        app.conn.execute(
            "INSERT INTO memory_index (id, fragment_id, topic_id, entity_ids, keywords, title, "
            "token_estimate, created_at) VALUES (?, ?, ?, '[]', '[]', ?, 10, ?)",
            (new_id("idx"), fragment_id, topic_id, title or f"标题-{marker}", now),
        )
    app.conn.commit()
    return fragment_id


def _nav(app: AppContext) -> int:
    return int(navigation_version.current(app.conn))


def _pos(app: AppContext) -> int:
    return app.position_state.current_position_version()


def _anchor(app: AppContext):
    return AnchorService(app.conn).get_active()


def _events(app: AppContext) -> list[dict]:
    return [
        event.data
        for event in list(app.bus._history)  # noqa: SLF001 - 直接读总线历史，避免再开 SSE
        if event.type == EventType.ANCHOR
    ]


def _stmts(version: int) -> list[str]:
    return [str(s) for (target, stmts) in MIGRATIONS if target == version for s in stmts]


def _request_transition(
    kind: str,
    *,
    request_id: str,
    topic: str,
    fragment: str | None = None,
    base_nav_version: int = 0,
    **kwargs,
) -> PositionTransition:
    return PositionTransition(
        kind=kind,
        source=f"manual.{kind}",
        identity=NavCommandIdentity.request(request_id),
        initiator="user",
        target_topic_id=topic,
        target_fragment_id=fragment,
        base_nav_version=base_nav_version,
        **kwargs,
    )


# ===========================================================================
# 1) 迁移 83/84 + 必需对象
# ===========================================================================


def test_migration_83_84_registered_and_required_objects() -> None:
    assert SCHEMA_VERSION == COMPENSATION_VERSION == 84 == max(t for t, _ in MIGRATIONS)
    for table in ("position_state", "navigation_commands"):
        assert table in REQUIRED_OBJECTS["tables"]
    for column in (
        "topic_answer_runs.phase",
        "topic_answer_runs.staged_answer",
        "topic_answer_runs.staged_at",
        "topic_answer_runs.staged_model",
    ):
        assert column in REQUIRED_OBJECTS["columns"]
    for index in ("idx_navigation_commands_identity", "idx_navigation_commands_created"):
        assert index in REQUIRED_OBJECTS["indexes"]

    migration = " ".join(_stmts(83)).lower()
    assert "create table if not exists position_state" in migration
    assert "create table if not exists navigation_commands" in migration
    assert "insert or ignore into position_state" in migration
    assert "alter table topic_answer_runs add column phase" in migration
    assert "alter table topic_answer_runs add column staged_answer" in migration
    assert "update topic_answer_runs set phase" in migration

    compensation = " ".join(_stmts(COMPENSATION_VERSION)).lower()
    for needle in (
        "create table if not exists position_state",
        "create table if not exists navigation_commands",
        "create unique index if not exists idx_navigation_commands_identity",
        "alter table topic_answer_runs add column staged_answer",
        "alter table topic_answer_runs add column staged_model",
    ):
        assert needle in compensation


def test_migration_module_apply_is_idempotent(tmp_path) -> None:
    from agent.storage import migration_r16_position_state as migration

    conn = connect(tmp_path / "m83.db")
    try:
        conn.execute(
            "CREATE TABLE topic_answer_runs (answer_key TEXT PRIMARY KEY, status TEXT NOT NULL, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        first = migration.apply(conn)
        assert first, "首次应用必须真的执行建对象语句"
        assert conn.execute("SELECT COUNT(*) FROM position_state").fetchone()[0] == 1
        row = conn.execute(
            "SELECT position_version, db_identity FROM position_state WHERE id = 1"
        ).fetchone()
        assert int(row[0]) == 0 and row[1] == ""
        assert conn.execute("SELECT COUNT(*) FROM navigation_commands").fetchone()[0] == 0
        # 重复应用：无操作、不重复改数据。
        assert migration.apply(conn) == []
    finally:
        conn.close()


def test_startup_ensure_identity_rebuilds_version_on_db_change(ctx: AppContext) -> None:
    assert ctx.position_state.ensure_identity() is False, "同一身份的启动期对齐是幂等的"
    topic = _topic(ctx)
    ctx.position_state.apply(
        _request_transition(KIND_ENTER_TOPIC, request_id="req-ident", topic=topic)
    )
    assert _pos(ctx) == 1
    # 库被整体替换：身份变化 → 只能由启动期权威重建把 position_version 归零并采用新身份。
    ctx.conn.execute(
        "UPDATE settings SET value = 'another-db-instance' WHERE key = 'db.instance_id'"
    )
    ctx.conn.commit()
    assert ctx.position_state.ensure_identity() is True
    assert _pos(ctx) == 0
    assert ctx.position_state.read()["position_identity"] == "another-db-instance"
    # 命令历史保留，重放返回既有 result + 当前 position/新 identity（不标 superseded）。
    replay = ctx.position_state.apply(
        _request_transition(KIND_ENTER_TOPIC, request_id="req-ident", topic=topic)
    )
    assert replay.replay is True and replay.status == "applied"
    assert replay.position["position_identity"] == "another-db-instance"


# ===========================================================================
# 2) 两类版本：职责严格分开
# ===========================================================================


def test_enter_topic_advances_both_versions_and_replay_is_zero_write(ctx: AppContext) -> None:
    topic = _topic(ctx)
    transition = _request_transition(KIND_ENTER_TOPIC, request_id="req-1", topic=topic)
    first = ctx.position_state.apply(transition)
    assert first.status == "applied"
    assert first.changed is True and first.replay is False
    assert first.command_id == "request:req-1" == first.identity_key
    assert first.nav_version == 1 and first.position_version == 1
    assert first.position["topic_id"] == topic
    assert first.position["position_identity"] == ctx.position_state.read()["position_identity"]

    before = ctx.position_state.read()
    replay = ctx.position_state.apply(transition)
    assert replay.replay is True and replay.changed is False
    assert replay.status == "applied" and replay.inactive is False
    assert replay.position == before, "replay 返回当前只读 position"
    assert replay.nav_version == 1 and replay.position_version == 1
    assert _nav(ctx) == 1 and _pos(ctx) == 1, "重放零版本推进"
    assert ctx.position_state.lookup("request:req-1")["status"] == "applied"
    assert (
        ctx.conn.execute("SELECT COUNT(*) FROM navigation_commands").fetchone()[0] == 1
    ), "重放不新增命令行"


def test_enter_topic_same_position_is_not_a_real_change(ctx: AppContext) -> None:
    topic = _topic(ctx)
    ctx.position_state.apply(
        _request_transition(KIND_ENTER_TOPIC, request_id="req-first", topic=topic)
    )
    second = ctx.position_state.apply(
        _request_transition(
            KIND_ENTER_TOPIC,
            request_id="req-second",
            topic=topic,
            base_nav_version=1,
        )
    )
    assert second.status == "applied" and second.changed is False
    assert second.nav_version == 1 and second.position_version == 1


def test_internal_refine_advances_position_only(ctx: AppContext) -> None:
    topic = _topic(ctx)
    frag_a = _seed_fragment(ctx, topic, marker="a")
    frag_b = _seed_fragment(ctx, topic, marker="b")
    ctx.position_state.apply(
        _request_transition(
            KIND_ENTER_TOPIC, request_id="req-refine-0", topic=topic, fragment=frag_a
        )
    )
    transition = PositionTransition(
        kind=KIND_INTERNAL_REFINE,
        source="turn.advance_anchor",
        identity=NavCommandIdentity.internal(
            "internal_refine", f"turn-refine:post_turn:{frag_b}"
        ),
        initiator="internal",
        target_topic_id=topic,
        target_fragment_id=frag_b,
        expected_position_version=1,
        turn_id="turn-refine",
    )
    refined = ctx.position_state.apply(transition)
    assert refined.status == "applied" and refined.changed is True
    assert refined.nav_version == 1, "内部细化不推进导航决定版本"
    assert refined.position_version == 2
    assert refined.position["fragment_id"] == frag_b

    # 同身份同参数重放：零写入、零推进。
    replay = ctx.position_state.apply(transition)
    assert replay.replay is True and replay.changed is False
    assert replay.nav_version == 1 and replay.position_version == 2


def test_apply_continuation_nav_zero_position_plus_one(ctx: AppContext) -> None:
    topic = _topic(ctx)
    source = _seed_fragment(ctx, topic, closed=True)
    registered = ctx.position_state.apply(
        PositionTransition(
            kind=KIND_SELECT_HISTORY,
            source="tool.continue_from_fragment",
            identity=NavCommandIdentity.operation("op-cont", 1),
            initiator="tool",
            pending="register",
            pending_topic_id=topic,
            pending_fragment_id=source,
            nav_decision=None,
        )
    )
    assert registered.status == "applied" and registered.changed is True
    assert registered.nav_version == 1, "select_history 真实选择改变推进一次决定版本"
    assert registered.position_version == 1
    assert registered.result["state"] == "registered"
    intent_id = registered.result["intent_id"]
    assert intent_id

    child_id = ctx.fragments.create_child(
        topic,
        source_fragment_id=source,
        relation_type="history_reopen",
        boundary_reason="history_continuation",
    )
    applied = ctx.position_state.apply(
        PositionTransition(
            kind=KIND_APPLY_CONTINUATION,
            source="turn.apply_continuation",
            identity=NavCommandIdentity.internal(
                "apply_continuation", f"turn-apply:{intent_id}"
            ),
            initiator="internal",
            target_topic_id=topic,
            target_fragment_id=child_id,
            expected_position_version=1,
            pending="resolve",
            pending_intent_id=intent_id,
            turn_id="turn-apply",
            causal={"sealed_fragment_id": None},
        )
    )
    assert applied.status == "applied" and applied.changed is True
    assert applied.nav_version == 1, "apply_continuation 不推进 nav_version（决定在登记时已计）"
    assert applied.position_version == 2
    assert TurnBindingService(ctx.conn).peek_intent() is None
    resolved = TurnBindingService(ctx.conn).intent_by_id(intent_id)
    assert resolved is not None and resolved.state == "resolved"
    assert resolved.resolved_fragment_id == child_id


def test_auto_segment_advances_position_and_keeps_pending(ctx: AppContext) -> None:
    topic = _topic(ctx)
    hist = _seed_fragment(ctx, topic, closed=True)
    open_fragment = _seed_fragment(ctx, topic, closed=False, marker="open")
    ctx.position_state.apply(
        _request_transition(
            KIND_ENTER_TOPIC,
            request_id="req-seg-0",
            topic=topic,
            fragment=open_fragment,
        )
    )
    ctx.position_state.apply(
        PositionTransition(
            kind=KIND_SELECT_HISTORY,
            source="tool.continue_from_fragment",
            identity=NavCommandIdentity.operation("op-seg", 1),
            initiator="tool",
            pending="register",
            pending_topic_id=topic,
            pending_fragment_id=hist,
        )
    )
    assert ctx.bindings.peek_intent() is not None
    before_nav, before_pos = _nav(ctx), _pos(ctx)

    # 调用方（orchestrator）真实封存之后才调用 auto_segment；causal 记 sealed_fragment_id。
    ctx.memory_lifecycle.seal_fragment(topic, reason="capacity")
    result = ctx.position_state.apply(
        PositionTransition(
            kind=KIND_AUTO_SEGMENT,
            source="capacity.segment",
            identity=NavCommandIdentity.internal(
                "auto_segment", f"turn-seg:{open_fragment}"
            ),
            initiator="internal",
            expected_position_version=before_pos,
            causal={"sealed_fragment_id": open_fragment},
            turn_id="turn-seg",
        )
    )
    assert result.status == "applied" and result.changed is True
    assert result.nav_version == before_nav, "自动分段不推进导航决定版本"
    assert result.position_version == before_pos + 1, "开放片段封存是真实位置状态变化"
    assert result.result["sealed_fragment_id"] == open_fragment
    assert ctx.bindings.peek_intent() is not None, "auto_segment 不得清掉旧申请"


def test_open_fragment_cancels_stale_pending_and_advances_versions(ctx: AppContext) -> None:
    topic = _topic(ctx)
    hist = _seed_fragment(ctx, topic, closed=True)
    open_fragment = _seed_fragment(ctx, topic, closed=False, marker="open")
    ctx.position_state.apply(
        PositionTransition(
            kind=KIND_SELECT_HISTORY,
            source="tool.continue_from_fragment",
            identity=NavCommandIdentity.operation("op-open", 1),
            initiator="tool",
            pending="register",
            pending_topic_id=topic,
            pending_fragment_id=hist,
        )
    )
    intent = ctx.bindings.peek_intent()
    assert intent is not None
    before_nav, before_pos = _nav(ctx), _pos(ctx)

    opened = ctx.position_state.apply(
        PositionTransition(
            kind=KIND_OPEN_FRAGMENT,
            source="manual.open_fragment",
            identity=NavCommandIdentity.request("req-open"),
            initiator="user",
            target_topic_id=topic,
            target_fragment_id=open_fragment,
            base_nav_version=before_nav,
        )
    )
    assert opened.status == "applied" and opened.changed is True
    assert opened.nav_version == before_nav + 1, "取消旧 pending 属于真实导航决定（§14.5）"
    assert opened.position_version == before_pos + 1
    assert ctx.bindings.peek_intent() is None, "open_fragment 结束不再适用的旧 registered"
    assert ctx.bindings.intent_by_id(intent.intent_id).state == "cancelled"
    assert _anchor(ctx).fragment_id == open_fragment


def test_cancel_continuation_does_not_advance_nav(ctx: AppContext) -> None:
    topic = _topic(ctx)
    hist = _seed_fragment(ctx, topic, closed=True)
    ctx.position_state.apply(
        PositionTransition(
            kind=KIND_SELECT_HISTORY,
            source="tool.continue_from_fragment",
            identity=NavCommandIdentity.operation("op-cancel", 1),
            initiator="tool",
            pending="register",
            pending_topic_id=topic,
            pending_fragment_id=hist,
        )
    )
    intent = ctx.bindings.peek_intent()
    assert intent is not None
    before_nav, before_pos = _nav(ctx), _pos(ctx)
    cancelled = ctx.position_state.apply(
        PositionTransition(
            kind=KIND_CANCEL_CONTINUATION,
            source="manual.cancel_continuation",
            identity=NavCommandIdentity.request("req-cancel"),
            initiator="user",
            pending="cancel",
            pending_intent_id=intent.intent_id,
        )
    )
    assert cancelled.status == "applied" and cancelled.changed is True
    assert cancelled.nav_version == before_nav, "§14.9：取消不是新的导航选择，不推进 nav_version"
    assert cancelled.position_version == before_pos + 1
    assert cancelled.result["cancelled"] is True
    assert cancelled.result["cancelled_intent_id"] == intent.intent_id
    assert ctx.bindings.peek_intent() is None


def test_select_history_tool_registration_moves_no_anchor(ctx: AppContext) -> None:
    topic_a = _topic(ctx, "话题A")
    topic_b = _topic(ctx, "话题B")
    hist = _seed_fragment(ctx, topic_b, closed=True)
    ctx.position_state.apply(
        _request_transition(KIND_ENTER_TOPIC, request_id="req-select-0", topic=topic_a)
    )
    before_pos = _pos(ctx)
    result = ctx.position_state.apply(
        PositionTransition(
            kind=KIND_SELECT_HISTORY,
            source="tool.continue_from_fragment",
            identity=NavCommandIdentity.operation("op-select", 1),
            initiator="tool",
            pending="register",
            pending_topic_id=topic_b,
            pending_fragment_id=hist,
        )
    )
    assert result.status == "applied" and result.changed is True
    assert _anchor(ctx).topic_id == topic_a, "工具登记路径不动 Anchor（§14.5）"
    assert result.nav_version == 2, "真实选择改变推进一次"
    assert result.position_version == before_pos + 1, "pending 出现是真实位置状态变化"
    assert result.result["intent_id"]


def test_create_topic_transition_creates_topic_and_advances_both(ctx: AppContext) -> None:
    result = ctx.position_state.apply(
        PositionTransition(
            kind=KIND_CREATE_TOPIC,
            source="tool.create_topic",
            identity=NavCommandIdentity.operation("op-create", 1),
            initiator="tool",
            new_topic_name="新话题",
            base_nav_version=0,
        )
    )
    assert result.status == "applied" and result.changed is True
    assert result.nav_version == 1 and result.position_version == 1
    topic_id = result.result["new_topic_id"]
    assert result.result["new_name"] == "新话题"
    assert _anchor(ctx).topic_id == topic_id


# ===========================================================================
# 3) 命令身份：CAS 失效 / 冲突 / 拒绝行
# ===========================================================================


def test_stale_base_nav_supersedes_with_zero_write_and_rejection_row(ctx: AppContext) -> None:
    topic_a = _topic(ctx, "话题A")
    topic_b = _topic(ctx, "话题B")
    ctx.position_state.apply(
        _request_transition(KIND_ENTER_TOPIC, request_id="req-base", topic=topic_a)
    )
    before_anchor = _anchor(ctx)
    before_nav, before_pos = _nav(ctx), _pos(ctx)

    stale = ctx.position_state.apply(
        _request_transition(
            KIND_ENTER_TOPIC,
            request_id="req-stale",
            topic=topic_b,
            base_nav_version=0,
        )
    )
    assert stale.status == "superseded"
    assert stale.code == "navigation_command_stale"
    assert stale.inactive is True and stale.conflict is False
    assert stale.changed is False
    assert stale.result["expected_nav_version"] == 0
    assert stale.result["actual_nav_version"] == before_nav
    assert _anchor(ctx) == before_anchor, "CAS 失效零位置写入"
    assert (_nav(ctx), _pos(ctx)) == (before_nav, before_pos)

    row = ctx.position_state.lookup("request:req-stale")
    assert row is not None and row["status"] == "superseded"
    # 重放被拒身份：返回既有拒绝事实，零写入、不重新执行。
    replay = ctx.position_state.apply(
        _request_transition(
            KIND_ENTER_TOPIC,
            request_id="req-stale",
            topic=topic_b,
            base_nav_version=0,
        )
    )
    assert replay.replay is True and replay.inactive is True
    assert replay.code == "navigation_command_stale"
    assert (_nav(ctx), _pos(ctx)) == (before_nav, before_pos)
    assert _anchor(ctx) == before_anchor


def test_same_identity_different_params_is_conflict_without_new_row(ctx: AppContext) -> None:
    topic_a = _topic(ctx, "话题A")
    topic_b = _topic(ctx, "话题B")
    first = ctx.position_state.apply(
        _request_transition(KIND_ENTER_TOPIC, request_id="req-conflict", topic=topic_a)
    )
    assert first.status == "applied"
    before = (ctx.position_state.read(), _anchor(ctx))
    rows = ctx.conn.execute("SELECT COUNT(*) FROM navigation_commands").fetchone()[0]

    conflict = ctx.position_state.apply(
        _request_transition(KIND_ENTER_TOPIC, request_id="req-conflict", topic=topic_b)
    )
    assert conflict.status == "conflict" and conflict.conflict is True
    assert conflict.code == "navigation_command_conflict"
    assert conflict.command_id == "request:req-conflict"
    assert conflict.position == before[0], "返回既有 command_id + 当前 position"
    assert ctx.position_state.read() == before[0]
    assert _anchor(ctx) == before[1]
    assert (
        ctx.conn.execute("SELECT COUNT(*) FROM navigation_commands").fetchone()[0] == rows
    ), "I05 冲突零写入、不新增行"


def test_stale_expected_position_version_supersedes_internal(ctx: AppContext) -> None:
    topic = _topic(ctx)
    frag = _seed_fragment(ctx, topic, closed=False, marker="target")
    ctx.position_state.apply(
        _request_transition(KIND_ENTER_TOPIC, request_id="req-pos", topic=topic)
    )
    before = (ctx.position_state.read(), _anchor(ctx))
    stale = ctx.position_state.apply(
        PositionTransition(
            kind=KIND_INTERNAL_REFINE,
            source="turn.advance_anchor",
            identity=NavCommandIdentity.internal(
                "internal_refine", f"turn-pos:post_turn:{frag}"
            ),
            initiator="internal",
            target_topic_id=topic,
            target_fragment_id=frag,
            expected_position_version=0,
        )
    )
    assert stale.status == "superseded"
    assert stale.code == "position_state_stale"
    assert stale.result["expected_position_version"] == 0
    assert stale.result["actual_position_version"] == 1
    assert ctx.position_state.read() == before[0]
    assert _anchor(ctx) == before[1]


def test_identity_validation_and_internal_expected_version() -> None:
    import pytest

    with pytest.raises(ValueError):
        NavCommandIdentity(kind="request", identity_key="operation:bad:1")
    with pytest.raises(ValueError):
        NavCommandIdentity(kind="internal")  # internal 必须显式 identity_key
    assert (
        NavCommandIdentity.operation("op-1", 2).identity_key == "operation:op-1:2"
    )
    assert NavCommandIdentity.request("req-9").identity_key == "request:req-9"
    internal = NavCommandIdentity.internal("auto_segment", "turn-1:frag-1")
    assert internal.identity_key == "internal:auto_segment:turn-1:frag-1"
    assert internal.command_kind == "auto_segment"

    service = PositionStateService.__new__(PositionStateService)
    with pytest.raises(ValueError):
        service._validate(
            PositionTransition(
                kind=KIND_ENTER_TOPIC,
                source="x",
                identity=NavCommandIdentity.internal("internal_refine", "turn:begin:f"),
                initiator="internal",
                target_topic_id="topic",
            )
        )
    with pytest.raises(ValueError):
        service._validate(
            PositionTransition(
                kind=KIND_AUTO_SEGMENT,
                source="x",
                identity=NavCommandIdentity.internal("internal_refine", "turn:begin:f"),
                initiator="internal",
                expected_position_version=1,
            )
        )


def test_param_digest_is_precise_and_excludes_cas(tmp_path) -> None:
    from agent.services.position_state import param_digest as digest

    base = PositionTransition(
        kind=KIND_ENTER_TOPIC,
        source="manual.enter_topic",
        identity=NavCommandIdentity.request("req-x"),
        target_topic_id="topic-a",
        target_fragment_id="frag-a",
        base_nav_version=1,
    )
    same_other_cas = PositionTransition(
        kind=KIND_ENTER_TOPIC,
        source="manual.enter_topic",
        identity=NavCommandIdentity.request("req-x"),
        target_topic_id="topic-a",
        target_fragment_id="frag-a",
        base_nav_version=99,
    )
    other_target = PositionTransition(
        kind=KIND_ENTER_TOPIC,
        source="manual.enter_topic",
        identity=NavCommandIdentity.request("req-x"),
        target_topic_id="topic-b",
        target_fragment_id="frag-a",
    )
    assert digest(base) == digest(same_other_cas), "CAS 前提不参与执行参数摘要"
    assert digest(base) != digest(other_target), "实际执行参数变化必须改变摘要"


# ===========================================================================
# 4) 薄包装：旧函数名/返回形状 + 新冻结字段
# ===========================================================================


def test_wrapper_enter_topic_returns_new_frozen_fields(ctx: AppContext) -> None:
    topic = _topic(ctx)
    result = ctx.navigation.enter_topic(
        topic, identity=NavCommandIdentity.request("req-wrap-enter")
    )
    assert result.topic_id == topic
    assert result.changed is True and result.replay is False
    assert result.command_id == "request:req-wrap-enter"
    assert result.nav_version == 1 and result.position_version == 1
    assert result.position is not None and result.position["topic_id"] == topic
    assert ctx.position_state.lookup("request:req-wrap-enter")["status"] == "applied"

    replay = ctx.navigation.enter_topic(
        topic, identity=NavCommandIdentity.request("req-wrap-enter")
    )
    assert replay.replay is True and replay.changed is False
    assert _nav(ctx) == 1 and _pos(ctx) == 1


def test_wrapper_enter_topic_restore_semantics(ctx: AppContext) -> None:
    topic = _topic(ctx)
    other = _topic(ctx, "另一个")
    hist = _seed_fragment(ctx, topic, closed=True)
    ctx.navigation.enter_topic(
        topic, fragment_id=hist, identity=NavCommandIdentity.request("req-restore-1")
    )
    ctx.navigation.enter_topic(other, identity=NavCommandIdentity.request("req-restore-2"))
    restored = ctx.navigation.enter_topic(
        topic, identity=NavCommandIdentity.request("req-restore-3")
    )
    assert restored.fragment_id == hist, "restore_position=True 恢复该话题保存位置"
    latest = ctx.navigation.enter_topic(
        topic, restore_position=False, identity=NavCommandIdentity.request("req-restore-4")
    )
    assert latest.fragment_id is None, "显式进入最新位置不恢复旧位置"


def test_wrapper_click_continuation_moves_anchor_and_position(ctx: AppContext) -> None:
    topic = _topic(ctx)
    hist = _seed_fragment(ctx, topic, closed=True)
    _seed_fragment(ctx, topic, closed=False, marker="open")
    before_nav, before_pos = _nav(ctx), _pos(ctx)

    payload = ctx.navigation.register_and_position_continuation(
        topic,
        hist,
        request_id="req-click",
        identity=NavCommandIdentity.request("req-click"),
    )
    assert payload["changed"] is True and payload["replay"] is False
    assert payload["topic_id"] == topic and payload["fragment_id"] == hist
    assert payload["historic"] is True
    assert payload["intent_id"] and payload["state"] == "registered"
    assert payload["nav_version"] == before_nav + 1
    assert payload["position_version"] == before_pos + 1
    assert payload["position"]["fragment_id"] == hist
    assert _anchor(ctx).fragment_id == hist, "点击路径 Anchor 落历史片段"

    replay = ctx.navigation.register_and_position_continuation(
        topic,
        hist,
        request_id="req-click",
        identity=NavCommandIdentity.request("req-click"),
    )
    assert replay["changed"] is False and replay["replay"] is True
    assert replay["intent_id"] == payload["intent_id"]
    assert (_nav(ctx), _pos(ctx)) == (before_nav + 1, before_pos + 1)


def test_wrapper_tool_registration_does_not_move_anchor(ctx: AppContext) -> None:
    topic_a = _topic(ctx, "话题A")
    topic_b = _topic(ctx, "话题B")
    hist = _seed_fragment(ctx, topic_b, closed=True)
    ctx.navigation.enter_topic(topic_a, identity=NavCommandIdentity.request("req-tool-0"))
    before_nav, before_pos = _nav(ctx), _pos(ctx)

    payload = ctx.navigation.register_continuation(
        topic_b,
        hist,
        identity=NavCommandIdentity.operation("op-tool", 1),
    )
    assert payload["changed"] is True
    assert payload["intent_id"] and payload["state"] == "registered"
    assert _anchor(ctx).topic_id == topic_a, "工具登记路径不动 Anchor"
    assert payload["nav_version"] == before_nav + 1
    assert _pos(ctx) == before_pos + 1, "pending 出现推进 position_version"
    assert _events(ctx) == [], "工具登记路径不广播位置事件（由 orchestrator/调用方决定）"


def test_wrapper_apply_continuation_nav_zero_and_resolves(ctx: AppContext) -> None:
    topic = _topic(ctx)
    hist = _seed_fragment(ctx, topic, closed=True)
    payload = ctx.navigation.register_continuation(
        topic, hist, identity=NavCommandIdentity.operation("op-apply", 1)
    )
    intent_id = payload["intent_id"]
    nav_after_register, pos_after_register = _nav(ctx), _pos(ctx)
    result = ctx.navigation.apply_continuation(topic, intent_id, turn_id="turn-apply-wrap")
    assert result.created_fragment_id is not None
    assert result.fragment_id == result.created_fragment_id
    assert result.source_fragment_id == hist
    assert _nav(ctx) == nav_after_register, "apply_continuation 不推进 nav_version"
    assert _pos(ctx) >= pos_after_register + 1
    assert TurnBindingService(ctx.conn).peek_intent() is None
    assert TurnBindingService(ctx.conn).intent_by_id(intent_id).state == "resolved"
    # 幂等重试：不重复建片段、不推进版本。
    fragments = ctx.conn.execute("SELECT COUNT(*) FROM fragments").fetchone()[0]
    retried = ctx.navigation.apply_continuation(topic, intent_id, turn_id="turn-apply-wrap")
    assert retried.fragment_id == result.fragment_id
    assert ctx.conn.execute("SELECT COUNT(*) FROM fragments").fetchone()[0] == fragments
    assert (_nav(ctx), _pos(ctx)) == (nav_after_register, _pos(ctx))


def test_apply_continuation_atomic_rollback_on_stale_cas(ctx: AppContext) -> None:
    topic = _topic(ctx)
    source = _seed_fragment(ctx, topic, closed=True)
    other = _topic(ctx, "另一个话题")
    payload = ctx.navigation.register_continuation(
        topic, source, identity=NavCommandIdentity.operation("op-atomic", 1)
    )
    intent_id = payload["intent_id"]
    version_at_register = payload["nav_version"]

    # 用户之后又真实导航：旧依据失效（同时按 §14.5 清除旧 registered 意图）。
    ctx.navigation.enter_topic(other, identity=NavCommandIdentity.request("req-atomic-other"))
    anchor_before = _anchor(ctx)
    fragments_before = ctx.conn.execute("SELECT COUNT(*) FROM fragments").fetchone()[0]
    nav_before, pos_before = _nav(ctx), _pos(ctx)

    from agent.services.navigation_version import NavigationVersionConflict

    with pytest.raises(NavigationVersionConflict):
        ctx.navigation.apply_continuation(
            topic, intent_id, expected_nav_version=version_at_register
        )

    assert ctx.conn.execute("SELECT COUNT(*) FROM fragments").fetchone()[0] == fragments_before, (
        "CAS 失效必须整体回滚（不允许留下已建片段）"
    )
    assert _anchor(ctx) == anchor_before
    assert (_nav(ctx), _pos(ctx)) == (nav_before, pos_before)


# ===========================================================================
# 5) 事件与快照同源（真实 AppContext + 临时 SQLite）
# ===========================================================================


async def test_anchor_event_and_runtime_snapshot_share_complete_position(ctx: AppContext) -> None:
    topic = _topic(ctx)
    hist = _seed_fragment(ctx, topic, closed=True)
    payload = ctx.navigation.register_and_position_continuation(
        topic,
        hist,
        request_id="req-event",
        identity=NavCommandIdentity.request("req-event"),
    )
    await ctx._publish_anchor_event(
        position=payload["position"], command_id=payload["command_id"]
    )
    events = _events(ctx)
    assert events, "真实 applied 命令必须广播一次 ANCHOR"
    event = events[-1]
    snapshot = ctx.position_state.read()

    assert event["position"] == snapshot, "事件携带的 position 必须与快照整份同源"
    for key in (
        "topic_id",
        "topic_name",
        "fragment_id",
        "fragment_title",
        "historic",
        "pending_intent_id",
        "pending_intent_version",
        "pending_source_fragment_id",
        "pending_source_title",
        "nav_version",
        "position_version",
        "position_identity",
    ):
        assert event[key] == snapshot[key], key
    assert event["command_id"] == "request:req-event"
    assert event["position_version"] == payload["position_version"]
    assert event["instance_id"] == ctx.instance_id
    # 回退路径：不给 position 时用 read() 生成同一份权威状态。
    await ctx._publish_anchor_event()
    assert _events(ctx)[-1]["position"] == ctx.position_state.read()


async def test_tool_replay_and_zero_change_do_not_broadcast(ctx: AppContext) -> None:
    from agent.tools.base import ToolResult

    before = len(_events(ctx))
    await ctx._on_tool_anchor_result(
        {"tool": "switch_topic", "result": ToolResult(ok=True, content="ok", facts={"replay": True})}
    )
    await ctx._on_tool_anchor_result(
        {
            "tool": "switch_topic",
            "result": ToolResult(ok=True, content="ok", facts={"changed": False}),
        }
    )
    assert len(_events(ctx)) == before, "重放 / 无真实变化零广播"


# ===========================================================================
# 6) orchestrator：auto_segment / boundary 两条转换分开记账
# ===========================================================================


async def test_post_turn_auto_segment_advances_position_version(ctx: AppContext, monkeypatch) -> None:
    topic = _topic(ctx)
    ctx.fragments.max_turns = 1
    ctx.memory.append_message(topic_id=topic, role="user", content="问题")
    ctx.memory.append_message(topic_id=topic, role="assistant", content="回答")
    fragment = ctx.fragments.open_fragment(topic)
    ctx.position_state.apply(
        _request_transition(
            KIND_ENTER_TOPIC, request_id="req-post", topic=topic, fragment=fragment.id
        )
    )
    # 登记中的接续（旧申请）不得被自动分段清掉。
    hist = _seed_fragment(ctx, topic, closed=True, marker="hist")
    ctx.navigation.register_continuation(
        topic, hist, identity=NavCommandIdentity.operation("op-post", 1)
    )
    before_pos, before_nav = _pos(ctx), _nav(ctx)
    before_events = len(_events(ctx))

    async def _noop_refresh(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(ctx.turn_orchestrator, "_refresh_topic_vector", _noop_refresh)
    monkeypatch.setattr(ctx.turn_orchestrator, "_schedule_derived_work", lambda *a, **k: None)

    fake_ctx = SimpleNamespace(trace=None)
    plan = SimpleNamespace(needs_consolidation=False)
    await ctx.turn_orchestrator.post_turn(fake_ctx, adapter=None, plan=plan, final_topic=topic)

    assert _pos(ctx) == before_pos + 1, "auto_segment 一律推进 position_version"
    assert _nav(ctx) == before_nav, "auto_segment 不推进 nav_version"
    assert ctx.bindings.peek_intent() is not None, "auto_segment 不清旧申请"
    events = _events(ctx)
    assert len(events) == before_events + 1, "内部推进必须产生可被前端接受的位置事件"
    assert events[-1]["position_version"] == before_pos + 1
    assert events[-1]["position"] == ctx.position_state.read()


def test_boundary_split_accounts_auto_segment_and_refine_separately(ctx: AppContext) -> None:
    topic = _topic(ctx)
    ctx.settings_store.set(BOUNDARY_MODE_KEY, "enabled")
    ctx.fragments.max_turns = 10
    ctx.fragments.max_tokens = 0
    ctx.memory.append_message(topic_id=topic, role="user", content="先做方案设计")
    ctx.memory.append_message(topic_id=topic, role="assistant", content="方案如下……")
    fragment = ctx.fragments.open_fragment(topic)
    ctx.position_state.apply(
        _request_transition(
            KIND_ENTER_TOPIC, request_id="req-boundary", topic=topic, fragment=fragment.id
        )
    )
    before_pos, before_nav = _pos(ctx), _nav(ctx)
    fake_ctx = SimpleNamespace(trace=None, message="设计确定了，开始实现", bound_fragment_id=fragment.id)

    ctx.turn_orchestrator._apply_boundary_policy(fake_ctx, topic, fragment.id)

    child = ctx.fragments.open_fragment(topic)
    assert child is not None and child.id != fragment.id
    assert fake_ctx.bound_fragment_id == child.id
    assert _pos(ctx) == before_pos + 2, "封存 + 新建子片段按 auto_segment + internal_refine 两条转换分别记账"
    assert _nav(ctx) == before_nav, "边界策略不得伪装成用户导航决定"
    assert _anchor(ctx).fragment_id == child.id
    rows = ctx.conn.execute(
        "SELECT command_kind, status FROM navigation_commands ORDER BY created_at, command_kind"
    ).fetchall()
    kinds = {row["command_kind"] for row in rows}
    assert {"auto_segment", "internal_refine"} <= kinds
    for row in rows:
        if row["command_kind"] in ("auto_segment", "internal_refine"):
            assert row["status"] == "applied"

# ===========================================================================
# 7) F 独立评审回归（Lead 转达）：select_history 点击移动 Anchor 必须推进 nav
# ===========================================================================


def test_click_same_history_after_tool_registration_advances_nav(ctx: AppContext) -> None:
    """工具先登记同一历史 H（move_anchor=False）→ 用户点击 H（move_anchor=True, base=nav）。

    §14.5：点击路径 Anchor 落历史片段，即使 pending 来源相同，Anchor 真实移动也是
    真实导航决定 → nav_version +1；工具登记路径本身不动 Anchor、不额外推进。
    """
    topic = _topic(ctx)
    hist = _seed_fragment(ctx, topic, closed=True, marker="same")
    tool = ctx.navigation.register_continuation(
        topic, hist, identity=NavCommandIdentity.operation("op-same", 1)
    )
    assert tool["changed"] is True and tool["state"] == "registered"
    assert _anchor(ctx) is None, "工具登记路径不动 Anchor"
    nav_after_tool, pos_after_tool = _nav(ctx), _pos(ctx)

    clicked = ctx.navigation.register_and_position_continuation(
        topic,
        hist,
        request_id="req-click-same",
        identity=NavCommandIdentity.request("req-click-same"),
        expected_nav_version=nav_after_tool,
    )
    assert clicked["changed"] is True, "Anchor 真实移动 = 真实导航决定"
    assert clicked["fragment_id"] == hist
    assert _anchor(ctx).fragment_id == hist, "点击路径 Anchor 落历史片段"
    assert _nav(ctx) == nav_after_tool + 1, "点击移动 Anchor 必须推进 nav_version（§14.5）"
    assert _pos(ctx) == pos_after_tool + 1
    assert clicked["position"]["fragment_id"] == hist
    rows = ctx.conn.execute(
        "SELECT status FROM navigation_commands WHERE identity_key = ?",
        ("request:req-click-same",),
    ).fetchall()
    assert [row["status"] for row in rows] == ["applied"]


def test_apply_rejects_forged_identity_prefix_with_zero_write(ctx: AppContext) -> None:
    """§14.8：apply 自己复查 kind 与 identity_key 前缀，object.__new__ 绕过构造也必须拒绝。"""
    import pytest

    topic = _topic(ctx)
    forged = object.__new__(NavCommandIdentity)
    object.__setattr__(forged, "kind", "request")
    object.__setattr__(forged, "identity_key", "operation:forged:1")
    object.__setattr__(forged, "request_id", None)
    object.__setattr__(forged, "operation_id", None)
    object.__setattr__(forged, "param_version", None)
    object.__setattr__(forged, "internal_scope", None)
    before = ctx.position_state.read()
    with pytest.raises(ValueError):
        ctx.position_state.apply(
            PositionTransition(
                kind=KIND_ENTER_TOPIC,
                source="manual.enter_topic",
                identity=forged,
                initiator="user",
                target_topic_id=topic,
            )
        )
    assert ctx.position_state.read() == before
    assert ctx.conn.execute("SELECT COUNT(*) FROM navigation_commands").fetchone()[0] == 0


def test_legacy_anonymous_navigation_does_not_clear_registered_intent(ctx: AppContext) -> None:
    """兼容边界：旧调用方无身份合成的 anonymous internal 位置写入不是「真实导航决定」，
    不得误清用户等待中的接续申请（生产新路径必须传真实身份才触发清除规则）。"""
    topic_a = _topic(ctx, "话题A")
    topic_b = _topic(ctx, "话题B")
    hist = _seed_fragment(ctx, topic_b, closed=True)
    ctx.navigation.register_continuation(
        topic_b, hist, identity=NavCommandIdentity.operation("op-legacy", 1)
    )
    intent = ctx.bindings.peek_intent()
    assert intent is not None
    ctx.navigation.enter_topic(topic_a)  # 旧调用方：无 identity → anonymous internal
    assert _anchor(ctx).topic_id == topic_a
    kept = ctx.bindings.peek_intent()
    assert kept is not None and kept.intent_id == intent.intent_id, (
        "anonymous 兼容位置写入不得清掉真实用户的等待申请"
    )

