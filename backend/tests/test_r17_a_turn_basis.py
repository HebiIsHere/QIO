# -*- coding: utf-8 -*-
"""R17-A：轮次受理依据 —— 旧排队轮次不得覆盖用户后来的新导航（P1-1）。

契约：docs/topic-unify-r17-contract-20261111.md §2.1（冻结接口）与 §5 验收矩阵 A1—A5。
本文件先红后绿（基线红证据：D:\\qio-dev\\r17-evidence\\a\\baseline_p1_1_repro.py/.log）：

* A1 A 提交排队 → 用户手动导航到话题 B → A 执行完成：回答仍归属 A，当前位置仍是 B，
  A 轮次期间不广播任何「A 成为位置」的 ANCHOR（基线的 begin 补锚会用临时读到的
  当前版本通过 CAS，把 Anchor 从 B 改回 A）。
* A2 离开再返回：旧轮轮末推进不得覆盖用户返回的位置，也没有多余位置推进/广播。
* A3 同话题改选历史接续：旧轮不得落实/取消新选择，pending 保持新的选择。
* A4 轮次结束前（模型闸门期间）发生新导航：轮末不覆盖 B，回答仍归属 A。
* A5 无导航干扰的正常轮：轮首补锚 / 轮末推进 / 容量自动分段行为保持。

时序全部用 asyncio.Event 闸门控制（holder 轮占住 active，fake 模型进入即 set），
不用 sleep；模型一律 fake/scripted provider，数据库一律临时 SQLite（真实迁移）。
"""

from __future__ import annotations

import asyncio

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.api.events import EventType
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.core.turn import TurnManager
from agent.graph.anchors import AnchorService
from agent.services.app import AppContext
from agent.services.predict import TopicPrediction
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

ANSWER = "[[QIO:ANSWER]]\n排队轮次的正式回答"
#: 回答区协议标记在最终正文里会被去掉；落库/断言用正文本身。
ANSWER_BODY = "排队轮次的正式回答"
TIMEOUT = 60


# ---------------------------------------------------------------------------
# 装置：真实 AppContext + 临时 SQLite + fake scripted adapter + 事件闸门
# ---------------------------------------------------------------------------


def _app(tmp_path) -> AppContext:
    conn = connect(tmp_path / "r17_a_turn_basis.db")
    apply_migrations(conn)
    app = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app.credentials._kr = MemoryKeyring()
    return app


def _topic(app: AppContext, name: str) -> str:
    return app.topics.nodes.create_topic(name).id


def _open_fragment(app: AppContext, topic_id: str) -> str:
    return app.fragments.get_or_create_open(topic_id).id


def _seed_history(app: AppContext, topic_id: str) -> tuple[str, str, str]:
    """话题的固定布局：两段已封存的历史片段 + 一个开放片段（历史必须先封存）。"""
    hist1 = app.fragments.create_child(topic_id, relation_type="normal")
    app.fragments.seal(hist1, reason="r17_a_test_history")
    hist2 = app.fragments.create_child(topic_id, relation_type="normal")
    app.fragments.seal(hist2, reason="r17_a_test_history")
    open_fragment = _open_fragment(app, topic_id)
    return open_fragment, hist1, hist2


def _anchor(app: AppContext):
    return AnchorService(app.conn).get_active()


def _position_version(app: AppContext) -> int:
    return app.position_state.current_position_version()


def _nav_version(app: AppContext) -> int:
    from agent.services.navigation_version import current as current_nav

    return int(current_nav(app.conn))


def _anchor_events(app: AppContext) -> list[dict]:
    return [
        event.data
        for event in list(app.bus._history)  # noqa: SLF001 - 与 test_r16_a 同一只读取法
        if event.type == EventType.ANCHOR
    ]


class ScriptedAdapter:
    """非流式 fake adapter：第 N 次调用进入闸门等释放；绝不联网。"""

    mode = "native"
    model = "fake-r17-a"
    supports_stream = False

    def __init__(self, gates: int = 0, text: str = ANSWER) -> None:
        self.text = text
        self.calls = 0
        self.entered = [asyncio.Event() for _ in range(gates)]
        self.release = [asyncio.Event() for _ in range(gates)]

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        index = self.calls
        self.calls += 1
        if index < len(self.entered):
            self.entered[index].set()
            await self.release[index].wait()
        return Completion(
            message=ChatMessage(role="assistant", content=self.text),
            finish_reason="stop",
        )


def _install_adapter(app: AppContext, monkeypatch, adapter: ScriptedAdapter) -> None:
    async def _fake_build():
        return adapter

    monkeypatch.setattr(app, "build_adapter", _fake_build)


def _force_prediction(app: AppContext, monkeypatch) -> None:
    async def _fake_predict(turn_ctx, message, topic):  # noqa: ANN001
        return TopicPrediction(main_topic_id=topic, backend_used="rules")

    monkeypatch.setattr(app.turn_orchestrator, "_predict_topics", _fake_predict)


def _submit_turn(app: AppContext, message: str, topic_id: str):
    """模拟生产入口：提交时把「受理那一刻」的位置依据一起冻结进 basis（契约 §2.1）。"""
    position = app.position_state.read()
    pending = app.bindings.peek_intent()
    basis = {
        "topic_id": topic_id,
        "fragment_id": position["fragment_id"],
        "intent_id": pending.intent_id if pending is not None else None,
        "intent_version": pending.version if pending is not None else None,
        "nav_version": position["nav_version"],
        "position_version": position["position_version"],
    }
    return app.turns.submit(
        message,
        topic_id,
        intent_id=pending.intent_id if pending is not None else None,
        basis=basis,
    )


async def _wait_turn(app: AppContext, ctx) -> dict:
    result = await asyncio.wait_for(app.turns.wait(ctx.turn_id), timeout=TIMEOUT)
    assert result is not None, "turn 没有结果"
    return result


async def _start_holder(app: AppContext, adapter: ScriptedAdapter, topic_id: str):
    """占位轮：让 active 被占用（后续提交因此真的是 queued），并进入模型闸门。"""
    holder = app.turns.submit("占位问题", topic_id)
    await asyncio.wait_for(adapter.entered[0].wait(), timeout=20)
    return holder


async def _release_holder(app: AppContext, adapter: ScriptedAdapter, holder) -> dict:
    adapter.release[0].set()
    return await _wait_turn(app, holder)


def _turn_rows(app: AppContext, turn_id: str) -> list:
    return app.conn.execute(
        "SELECT m.role, m.content, m.fragment_id, f.topic_id AS topic_id "
        "FROM messages m LEFT JOIN fragments f ON f.id = m.fragment_id "
        "WHERE m.turn_id = ? ORDER BY m.created_at, m.rowid",
        (turn_id,),
    ).fetchall()


def _commands(app: AppContext, turn_id: str) -> list[dict]:
    rows = app.conn.execute(
        "SELECT command_kind, status, position_version, nav_version "
        "FROM navigation_commands WHERE turn_id = ? ORDER BY rowid",
        (turn_id,),
    ).fetchall()
    return [dict(row) for row in rows]


# ===========================================================================
# 0) 冻结接口：TurnManager.submit/reserve 记录受理依据；旧调用方行为不变
# ===========================================================================


async def test_submit_and_reserve_record_frozen_basis_fields():
    seen: dict = {}

    async def runner(ctx) -> None:
        seen[ctx.turn_id] = ctx
        ctx.result = {"ok": True}

    manager = TurnManager(runner=runner)
    try:
        basis = {
            "topic_id": "topic_x",
            "fragment_id": "frag_1",
            "intent_id": "intent_1",
            "intent_version": 3,
            "nav_version": 7,
            "position_version": 5,
        }
        ctx = manager.submit("问题", "topic_x", intent_id="intent_1", basis=basis)
        await asyncio.wait_for(manager.wait(ctx.turn_id), timeout=10)
        # 受理依据逐键复制（按受理那一刻的真实值，不得事后重读）。
        assert ctx.submit_basis_topic_id == "topic_x"
        assert ctx.submit_basis_fragment_id == "frag_1"
        assert ctx.submit_basis_intent_id == "intent_1"
        assert ctx.submit_basis_intent_version == 3
        assert ctx.submit_basis_nav_version == 7
        assert ctx.submit_basis_position_version == 5
        # CAS 链从受理依据起链（这是一个**有默认值**的字段，旧替身用 getattr 兼容）。
        assert ctx.position_basis_version == 5
        assert ctx.position_basis_nav_version == 7
        assert ctx.position_basis_superseded is False

        # basis=None 的旧调用方：所有新字段保持默认值，旧行为不变。
        old = manager.submit("旧调用方", "topic_x")
        await asyncio.wait_for(manager.wait(old.turn_id), timeout=10)
        assert old.submit_basis_topic_id is None
        assert old.submit_basis_position_version is None
        assert old.submit_basis_nav_version is None
        assert old.position_basis_version is None
        assert old.position_basis_nav_version is None
        assert old.position_basis_superseded is False

        # reserve 同样是受理入口：basis 必须一起冻结。
        reserved = manager.reserve(
            "预留问题", "topic_x", prepare_id="prep_1", basis=dict(basis)
        )
        assert reserved.submit_basis_position_version == 5
        assert reserved.position_basis_version == 5
        manager.abandon(reserved, reason="test_cleanup")
    finally:
        await manager.shutdown()


# ===========================================================================
# A1：A 提交排队 → 手动导航 B → A 执行完成：回答归 A，当前位置仍 B
# ===========================================================================


async def test_a1_queued_turn_does_not_override_new_navigation(tmp_path, monkeypatch):
    app = _app(tmp_path)
    topic_a = _topic(app, "原话题A")
    fragment_a = _open_fragment(app, topic_a)
    topic_b = _topic(app, "用户后来导航的话题B")
    fragment_b = _open_fragment(app, topic_b)
    topic_h = _topic(app, "占位轮次话题H")
    _open_fragment(app, topic_h)

    adapter = ScriptedAdapter(gates=1)
    _install_adapter(app, monkeypatch, adapter)
    _force_prediction(app, monkeypatch)

    # 占位轮先跑起来（active 被占住），A 的提交才是真正的 queued。
    holder = await _start_holder(app, adapter, topic_h)
    assert _position_version(app) == 1  # 占位轮轮首补锚（无 anchor → H）
    basis_version = _position_version(app)

    queued = _submit_turn(app, "A 的原问题", topic_a)
    assert queued.status == "queued", "占位轮还在跑，这一轮必须真的排队"
    assert queued.submit_basis_position_version == basis_version
    assert queued.position_basis_version == basis_version

    # 受理之后、执行之前：用户手动导航到 B（明确落到 B 的当前开放片段）。
    app.navigation.enter_topic(topic_b, fragment_id=fragment_b)
    anchor_b = _anchor(app)
    assert anchor_b.topic_id == topic_b
    assert anchor_b.fragment_id == fragment_b
    assert _position_version(app) == basis_version + 1
    position_after_nav = app.position_state.read()
    events_mark = len(_anchor_events(app))

    await _release_holder(app, adapter, holder)
    result = await _wait_turn(app, queued)
    assert result.get("ok") is True, result

    # 1) 回答（用户消息 + 正式回答）仍按受理归属写在 A（话题 + A 的开放片段）。
    rows = _turn_rows(app, queued.turn_id)
    user_rows = [row for row in rows if row["role"] == "user"]
    answer_rows = [row for row in rows if row["role"] == "assistant"]
    assert len(user_rows) == 1 and len(answer_rows) == 1, [dict(r) for r in rows]
    assert user_rows[0]["topic_id"] == topic_a
    assert answer_rows[0]["topic_id"] == topic_a
    assert user_rows[0]["fragment_id"] == fragment_a
    assert answer_rows[0]["fragment_id"] == fragment_a
    assert answer_rows[0]["content"] == ANSWER_BODY

    # 2) 最终位置仍是 B：旧轮的轮首补锚被依据链 CAS 拒绝，不得改回 A。
    final = app.position_state.read()
    assert final["topic_id"] == topic_b
    assert final["fragment_id"] == fragment_b
    assert final["position_version"] == position_after_nav["position_version"]
    # 3) A 轮次期间不广播任何「A 成为位置」的 ANCHOR（零位置副作用）。
    new_events = _anchor_events(app)[events_mark:]
    assert new_events == [], [(e.get("topic_id"), e.get("position_version")) for e in new_events]
    # 4) 事实可观察：依据链被标记为 superseded（不洗白、不重读当前位置）。
    assert queued.position_basis_superseded is True


# ===========================================================================
# A2：离开再返回：旧轮轮末推进不得覆盖用户返回的位置
# ===========================================================================


async def test_a2_leave_and_return_keeps_user_returned_position(tmp_path, monkeypatch):
    app = _app(tmp_path)
    topic_a = _topic(app, "原话题A")
    fragment_open, hist1, _hist2 = _seed_history(app, topic_a)
    topic_b = _topic(app, "用户临时离开的话题B")
    _open_fragment(app, topic_b)
    topic_h = _topic(app, "占位轮次话题H")
    _open_fragment(app, topic_h)
    # 用户当前明确停在 A 的历史片段上（本轮受理时的真实位置）。
    app.navigation.enter_topic(topic_a, fragment_id=hist1)
    assert _anchor(app).fragment_id == hist1

    adapter = ScriptedAdapter(gates=1)
    _install_adapter(app, monkeypatch, adapter)
    _force_prediction(app, monkeypatch)

    holder = await _start_holder(app, adapter, topic_h)
    basis_version = _position_version(app)
    queued = _submit_turn(app, "A 的原问题", topic_a)
    assert queued.status == "queued"
    assert queued.submit_basis_position_version == basis_version

    # 受理之后、执行之前：用户离开 A 去 B，再返回 A（恢复 hist1）。
    app.navigation.enter_topic(topic_b)
    app.navigation.enter_topic(topic_a)
    returned_anchor = _anchor(app)
    assert returned_anchor.topic_id == topic_a
    assert returned_anchor.fragment_id == hist1, "返回后应恢复用户保存的位置"
    position_after_return = app.position_state.read()
    events_mark = len(_anchor_events(app))

    await _release_holder(app, adapter, holder)
    result = await _wait_turn(app, queued)
    assert result.get("ok") is True, result

    # 回答仍归属 A（开放片段），但轮末的 advance_anchor 被依据链 CAS 拒绝。
    rows = _turn_rows(app, queued.turn_id)
    answers = [row for row in rows if row["role"] == "assistant"]
    assert len(answers) == 1 and answers[0]["topic_id"] == topic_a
    assert answers[0]["fragment_id"] == fragment_open

    final = app.position_state.read()
    assert final["topic_id"] == topic_a
    assert final["fragment_id"] == hist1, "旧轮轮末推进覆盖了用户返回的位置"
    assert final["position_version"] == position_after_return["position_version"], (
        "旧轮不得产生多余的位置推进"
    )
    new_events = _anchor_events(app)[events_mark:]
    assert new_events == [], [(e.get("topic_id"), e.get("fragment_id")) for e in new_events]
    assert queued.position_basis_superseded is True


# ===========================================================================
# A3：同话题改选历史接续：旧轮不得落实/取消新选择，pending 保持新选择
# ===========================================================================


async def test_a3_same_topic_reselection_keeps_new_pending_intent(tmp_path, monkeypatch):
    app = _app(tmp_path)
    topic_a = _topic(app, "原话题A")
    _fragment_open, hist1, hist2 = _seed_history(app, topic_a)
    topic_h = _topic(app, "占位轮次话题H")
    _open_fragment(app, topic_h)

    # 用户先点「从 hist1 继续」（点击路径：登记 + 落位）。
    first = app.navigation.register_and_position_continuation(topic_a, hist1)
    intent1 = app.bindings.peek_intent()
    assert intent1 is not None and intent1.intent_id == first["intent_id"]
    assert intent1.source_fragment_id == hist1
    assert _anchor(app).fragment_id == hist1

    adapter = ScriptedAdapter(gates=1)
    _install_adapter(app, monkeypatch, adapter)
    _force_prediction(app, monkeypatch)

    holder = await _start_holder(app, adapter, topic_h)
    basis_version = _position_version(app)
    queued = _submit_turn(app, "A 的原问题", topic_a)
    assert queued.status == "queued"
    assert queued.submit_basis_intent_id == intent1.intent_id
    assert queued.submit_basis_intent_version == intent1.version
    assert queued.submit_basis_position_version == basis_version

    # 受理之后、执行之前：同话题改选另一段历史（旧登记被 replaced）。
    second = app.navigation.register_and_position_continuation(topic_a, hist2)
    intent2 = app.bindings.peek_intent()
    assert intent2 is not None and intent2.intent_id == second["intent_id"]
    assert intent2.intent_id != intent1.intent_id
    assert intent2.state == "registered"
    assert intent2.source_fragment_id == hist2
    assert intent2.version == intent1.version + 1
    assert app.bindings.intent_by_id(intent1.intent_id).state == "replaced"
    anchor_after_reselect = _anchor(app)
    assert anchor_after_reselect.fragment_id == hist2
    position_after_reselect = app.position_state.read()

    await _release_holder(app, adapter, holder)
    result = await _wait_turn(app, queued)
    assert result.get("ok") is True, result

    # 新选择必须原样保持：旧轮既不落实它、也不取消它。
    pending = app.bindings.peek_intent()
    assert pending is not None
    assert pending.intent_id == intent2.intent_id
    assert pending.state == "registered"
    assert pending.source_fragment_id == hist2
    final = app.position_state.read()
    assert final["fragment_id"] == hist2
    assert final["position_version"] == position_after_reselect["position_version"]
    assert final["pending_intent_id"] == intent2.intent_id

    # 回答仍归属 A（话题），落库不改变 pending。
    rows = _turn_rows(app, queued.turn_id)
    answers = [row for row in rows if row["role"] == "assistant"]
    assert len(answers) == 1 and answers[0]["topic_id"] == topic_a
    assert queued.position_basis_superseded is True


# ===========================================================================
# A4：轮次结束前（模型闸门期间）新导航：轮末不覆盖 B，回答仍归属 A
# ===========================================================================


async def test_a4_navigation_during_model_gate_keeps_new_position(tmp_path, monkeypatch):
    app = _app(tmp_path)
    topic_a = _topic(app, "原话题A")
    fragment_a = _open_fragment(app, topic_a)
    topic_b = _topic(app, "模型生成期间切到的话题B")
    fragment_b = _open_fragment(app, topic_b)
    app.navigation.enter_topic(topic_a)
    assert _anchor(app).topic_id == topic_a

    adapter = ScriptedAdapter(gates=1)
    _install_adapter(app, monkeypatch, adapter)
    _force_prediction(app, monkeypatch)

    ctx = _submit_turn(app, "A 的原问题", topic_a)
    await asyncio.wait_for(adapter.entered[0].wait(), timeout=20)
    # 模型闸门期间用户导航到 B（明确落到 B 的当前开放片段）。
    app.navigation.enter_topic(topic_b, fragment_id=fragment_b)
    position_after_nav = app.position_state.read()
    assert position_after_nav["topic_id"] == topic_b
    events_mark = len(_anchor_events(app))

    adapter.release[0].set()
    result = await _wait_turn(app, ctx)
    assert result.get("ok") is True, result

    final = app.position_state.read()
    assert final["topic_id"] == topic_b
    assert final["fragment_id"] == fragment_b
    assert final["position_version"] == position_after_nav["position_version"]
    new_events = _anchor_events(app)[events_mark:]
    assert new_events == [], [(e.get("topic_id"), e.get("position_version")) for e in new_events]

    rows = _turn_rows(app, ctx.turn_id)
    answers = [row for row in rows if row["role"] == "assistant"]
    assert len(answers) == 1
    assert answers[0]["topic_id"] == topic_a
    assert answers[0]["fragment_id"] == fragment_a
    assert ctx.position_basis_superseded is True


# ===========================================================================
# A5：无导航干扰的正常轮 —— 轮首补锚 / 轮末推进 / 容量自动分段行为保持
# ===========================================================================


async def test_a5a_normal_turn_still_anchors_topic_on_begin(tmp_path, monkeypatch):
    app = _app(tmp_path)
    topic_a = _topic(app, "新话题A")
    fragment_a = app.fragments.open_fragment(topic_a)
    assert fragment_a is None, "话题还没有开放片段"
    adapter = ScriptedAdapter()
    _install_adapter(app, monkeypatch, adapter)
    _force_prediction(app, monkeypatch)
    assert app.position_state.read()["topic_id"] is None
    basis_version = _position_version(app)

    ctx = _submit_turn(app, "第一轮问题", topic_a)
    result = await _wait_turn(app, ctx)
    assert result.get("ok") is True, result

    # 轮首补锚真实生效：位置从「无」推进到 A，并广播了同源 ANCHOR。
    final = app.position_state.read()
    assert final["topic_id"] == topic_a
    open_id = app.fragments.open_fragment(topic_a).id
    assert final["fragment_id"] == open_id
    # 轮首补锚 +1、轮末推进（None → 开放片段）+1。
    assert final["position_version"] == basis_version + 2
    events = _anchor_events(app)
    assert any(event["topic_id"] == topic_a for event in events), "轮首补锚必须广播 ANCHOR"
    assert events[-1]["position_version"] == final["position_version"]
    assert events[-1]["position"] == final
    # 依据链按本回合自己的命令结果推进（没有被标记 superseded）。
    assert ctx.position_basis_version == final["position_version"]
    assert ctx.position_basis_superseded is False


async def test_a5b_normal_turn_still_advances_anchor_at_turn_end(tmp_path, monkeypatch):
    app = _app(tmp_path)
    topic_a = _topic(app, "原话题A")
    fragment_open, hist1, _hist2 = _seed_history(app, topic_a)
    app.navigation.enter_topic(topic_a, fragment_id=hist1)
    assert _anchor(app).fragment_id == hist1

    adapter = ScriptedAdapter()
    _install_adapter(app, monkeypatch, adapter)
    _force_prediction(app, monkeypatch)
    basis_version = _position_version(app)

    ctx = _submit_turn(app, "从历史位置继续的问题", topic_a)
    result = await _wait_turn(app, ctx)
    assert result.get("ok") is True, result

    # 轮末推进保持：位置从用户选中的历史片段推进到本轮真实片段（开放片段）。
    final = app.position_state.read()
    assert final["topic_id"] == topic_a
    assert final["fragment_id"] == fragment_open
    assert final["position_version"] == basis_version + 1
    events = _anchor_events(app)
    assert events[-1]["position_version"] == final["position_version"]
    assert events[-1]["fragment_id"] == fragment_open
    assert ctx.position_basis_version == final["position_version"]


async def test_a5c_normal_turn_still_auto_segments_and_keeps_chain(tmp_path, monkeypatch):
    app = _app(tmp_path)
    topic_a = _topic(app, "容量分段话题")
    fragment_open = _open_fragment(app, topic_a)
    app.navigation.enter_topic(topic_a, fragment_id=fragment_open)
    app.settings_store.set("fragment.max_turns", "1")

    adapter = ScriptedAdapter()
    _install_adapter(app, monkeypatch, adapter)
    _force_prediction(app, monkeypatch)
    monkeypatch.setattr(
        app.turn_orchestrator, "_schedule_derived_work", lambda *args, **kwargs: None
    )

    async def _noop_refresh(*args, **kwargs):
        return None

    monkeypatch.setattr(app.turn_orchestrator, "_refresh_topic_vector", _noop_refresh)
    basis_version = _position_version(app)

    ctx = _submit_turn(app, "这一轮会触发容量分段", topic_a)
    result = await _wait_turn(app, ctx)
    assert result.get("ok") is True, result

    # 自动分段保持：本轮答案让开放片段达到 1 轮上限 → 封存 + auto_segment 推进位置。
    final = app.position_state.read()
    assert final["topic_id"] == topic_a
    assert final["position_version"] == basis_version + 1
    commands = _commands(app, ctx.turn_id)
    kinds = {row["command_kind"] for row in commands}
    assert "auto_segment" in kinds, commands
    auto = [row for row in commands if row["command_kind"] == "auto_segment"]
    assert auto[0]["status"] == "applied"
    assert auto[0]["position_version"] == basis_version + 1
    # 依据链只按本回合自己的命令结果推进。
    assert ctx.position_basis_version == final["position_version"]
    assert ctx.position_basis_superseded is False
    assert any(
        event["position_version"] == basis_version + 1 for event in _anchor_events(app)
    )
