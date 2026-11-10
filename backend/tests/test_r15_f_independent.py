# -*- coding: utf-8 -*-
"""R15 F 独立验收（R1/R3/R4）：不拷贝实现者测试，独立构造复现与断言。

角色 F：独立复现 + 交叉评审。本文件只通过**冻结的共享契约**与公开服务接口
（AppContext.run_turn / TopicNavigationService / POST /api/anchor /
topic_recovery.retry_topic_answer / 真实 SQLite）观察用户可见事实，不复用
A/B/D 的测试代码。

覆盖：
* R1（契约 §3）：装配期间手动导航改选历史 → 旧模型结果的 switch_topic 必须
  stale：快照早于 build_context、旧操作零执行、用户新位置/新待接续保留、
  回答仍按原绑定归属保存；对照组的正常导航与快照字段；
* R3（契约 §5）：真实 FastAPI + 临时 SQLite 的 HTTP 重放：重复同 request_id、
  A→B→重放A、取消后重放、消费后重放、重新选择、request_id 参数冲突；断言
  Anchor / pending 来源 / 意图状态 / 导航版本 / 片段数（不能只看 HTTP 码）；
* R4（契约 §6）：fake 模型 + 受控时钟（monkeypatch topic_recovery._now_utc）与
  冻结测试缝 heartbeat_wait：同 answer_key 双记录并发、同 record 并发、
  活任务超租约、失效实例接管、旧执行者迟到成功/失败、只补存、接管与保存竞争；
  断言模型调用次数 / 答案行数 / answer_id / 执行世代。

时序一律用事件/屏障/受控时钟，不用 sleep 制造通过；外部模型一律 fake。
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from agent.adapters.base import ChatMessage, Completion, ToolCall
from agent.api.bus import EventBus
from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.graph.anchors import AnchorService
from agent.services import topic_recovery
from agent.services.app import AppContext
from agent.services.navigation_version import current as nav_current
from agent.services.topic_operation import TopicStages
from agent.storage.db import connect
from agent.storage.instance_registry import InstanceRegistry
from agent.storage.migrate import apply_migrations

ANSWER_MARKER = "[[QIO:ANSWER]]"


def _app(tmp_path) -> AppContext:
    conn = connect(tmp_path / "r15_f_independent.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


def _topic(app: AppContext, name: str) -> str:
    return app.topics.nodes.create_topic(name).id


def _open_fragment(app: AppContext, topic_id: str) -> str:
    return app.fragments.get_or_create_open(topic_id).id


def _seal_historic(app: AppContext, topic_id: str) -> tuple[str, str]:
    """把当前开放片段封存成历史，再建新的开放片段；返回 (历史, 新开放)。"""
    historic = _open_fragment(app, topic_id)
    app.fragments.seal(historic, reason="r15f_historic")
    return historic, _open_fragment(app, topic_id)


def _assistant_rows(app: AppContext) -> list:
    return app.conn.execute(
        "SELECT id, content, fragment_id, turn_id FROM messages WHERE role = 'assistant' "
        "ORDER BY rowid"
    ).fetchall()


def _install_adapter(app: AppContext, adapter) -> None:
    async def _build():
        return adapter

    app.build_adapter = _build


class _ScriptedAdapter:
    """按脚本出牌：list[ToolCall] 表示工具调用，str 表示最终回答文本。"""

    mode = "native"
    model = "fake-r15-f-scripted"
    protocol_in_prompt = False

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        index = self.calls
        self.calls += 1
        item = self.responses[index] if index < len(self.responses) else self.responses[-1]
        if isinstance(item, str):
            return Completion(
                message=ChatMessage(role="assistant", content=ANSWER_MARKER + "\n" + item)
            )
        return Completion(
            message=ChatMessage(role="assistant", content=None, tool_calls=list(item))
        )


class _AssemblyGate:
    """把 build_context 的真实内部步骤（话题预判）变成可释放的装配屏障。"""

    def __init__(self, order: list[str] | None = None) -> None:
        self.arrived = asyncio.Event()
        self.release = asyncio.Event()
        self.order: list[str] = order if order is not None else []

    def install(self, app: AppContext, monkeypatch, topic_hint=None) -> "_AssemblyGate":  # noqa: ANN001
        from agent.services.predict import TopicPrediction

        async def _fake_predict(ctx, message, topic):  # noqa: ANN001
            self.order.append("assembly")
            self.arrived.set()
            await self.release.wait()
            return TopicPrediction(
                main_topic_id=topic_hint or topic, backend_used="rules"
            )

        monkeypatch.setattr(app.turn_orchestrator, "_predict_topics", _fake_predict)
        return self

    def install_plain(self, app: AppContext, monkeypatch, topic_hint=None) -> "_AssemblyGate":  # noqa: ANN001
        """不暂停的装配 fake（对照组仍走同一个真实步骤）。"""
        from agent.services.predict import TopicPrediction

        gate = self

        async def _fake_predict(ctx, message, topic):  # noqa: ANN001
            gate.order.append("assembly")
            return TopicPrediction(
                main_topic_id=topic_hint or topic, backend_used="rules"
            )

        monkeypatch.setattr(app.turn_orchestrator, "_predict_topics", _fake_predict)
        return self


def _spy_snapshot(app: AppContext, monkeypatch, order: list[str] | None = None) -> dict:
    """包住快照函数：记录调用顺序与落盘字段（不改变实现行为）。"""
    captured: dict = {"order": order if order is not None else [], "ctx": None, "calls": 0}
    original = app.turn_orchestrator._snapshot_decision_context

    def _spy(ctx):  # noqa: ANN001
        captured["calls"] += 1
        captured["order"].append("snapshot")
        original(ctx)
        captured["ctx"] = ctx
        captured["snapshot"] = {
            "nav_version": getattr(ctx, "decision_nav_version", None),
            "topic_id": getattr(ctx, "decision_topic_id", None),
            "fragment_id": getattr(ctx, "decision_fragment_id", None),
        }

    monkeypatch.setattr(app.turn_orchestrator, "_snapshot_decision_context", _spy)
    return captured


def _switch_call(topic_id: str, call_id: str = "c1") -> ToolCall:
    return ToolCall(
        id=call_id,
        name="switch_topic",
        arguments={"topic_id": topic_id, "intent": "user_request"},
    )


# ===========================================================================
# R1：快照必须早于异步上下文装配（契约 §3.3）
# ===========================================================================


async def test_r1_reselection_inside_same_topic_during_assembly_invalidates_stale_switch(
    tmp_path, monkeypatch
):
    """主场景：装配期间同话题改选历史 → 旧依据的 switch_topic 不得执行。"""
    app = _app(tmp_path)
    topic_a = _topic(app, "甲话题")
    topic_b = _topic(app, "乙话题")
    _open_fragment(app, topic_b)
    historic, open_a = _seal_historic(app, topic_a)
    app.navigation.enter_topic(topic_a, fragment_id=open_a)
    base_version = nav_current(app.conn)

    adapter = _ScriptedAdapter([[_switch_call(topic_b)], "旧依据下的正式回答"])
    _install_adapter(app, adapter)
    order: list[str] = []
    gate = _AssemblyGate(order).install(app, monkeypatch)
    captured = _spy_snapshot(app, monkeypatch, order)

    task = asyncio.create_task(app.run_turn("换到乙话题", topic_id=topic_a))
    await asyncio.wait_for(gate.arrived.wait(), 30)
    # 装配期间：用户在同一话题内改选历史片段（真实导航写入口，推进版本）。
    app.navigation.enter_topic(topic_a, fragment_id=historic, relate=False)
    user_version = nav_current(app.conn)
    assert user_version == base_version + 1, "改选历史必须推进一次导航版本"
    gate.release.set()

    result = await asyncio.wait_for(task, 60)

    anchor = AnchorService(app.conn).get_active()
    assert anchor is not None and anchor.topic_id == topic_a, (
        "旧依据的 switch_topic 不得覆盖用户的新选择（Anchor 必须留在甲话题）"
    )
    assert nav_current(app.conn) == user_version, "旧操作不得推进导航版本"
    assert result["ok"] is True, "用户浏览不等于取消原问题：正式回答仍要完成"
    rows = _assistant_rows(app)
    assert rows, "本轮正式回答必须保存"
    assert rows[-1]["fragment_id"] == open_a, (
        "正式回答必须按本轮原绑定片段归属保存（不能被旧导航带到乙话题）"
    )
    assert "旧依据下的正式回答" in (rows[-1]["content"] or "")
    # 快照早于装配、内容是装配前的位置
    assert captured["calls"] == 1
    assert captured["order"] == ["snapshot", "assembly"], (
        f"快照必须早于上下文装配：{captured['order']}"
    )
    assert captured["snapshot"]["nav_version"] == base_version
    assert captured["snapshot"]["topic_id"] == topic_a
    assert captured["snapshot"]["fragment_id"] == open_a


async def test_r1_leave_and_return_during_assembly_invalidates_stale_switch(tmp_path, monkeypatch):
    """装配期间真实导航离开话题再返回（版本推进）→ 旧操作 stale。"""
    app = _app(tmp_path)
    topic_a = _topic(app, "甲话题")
    topic_b = _topic(app, "乙话题")
    _open_fragment(app, topic_b)
    _historic, open_a = _seal_historic(app, topic_a)
    app.navigation.enter_topic(topic_a, fragment_id=open_a)
    base_version = nav_current(app.conn)

    adapter = _ScriptedAdapter([[_switch_call(topic_b)], "离开又回来时的回答"])
    _install_adapter(app, adapter)
    order: list[str] = []
    gate = _AssemblyGate(order).install(app, monkeypatch)
    captured = _spy_snapshot(app, monkeypatch, order)

    task = asyncio.create_task(app.run_turn("换到乙话题", topic_id=topic_a))
    await asyncio.wait_for(gate.arrived.wait(), 30)
    app.navigation.enter_topic(topic_b)  # 切走
    app.navigation.enter_topic(topic_a)  # 再返回（恢复该话题位置）
    user_version = nav_current(app.conn)
    assert user_version == base_version + 2
    gate.release.set()

    result = await asyncio.wait_for(task, 60)
    anchor = AnchorService(app.conn).get_active()
    assert anchor is not None and anchor.topic_id == topic_a, "旧操作不得把位置留到乙话题"
    assert nav_current(app.conn) == user_version
    assert result["ok"] is True
    rows = _assistant_rows(app)
    assert rows and rows[-1]["fragment_id"] == open_a
    assert captured["snapshot"]["nav_version"] == base_version


async def test_r1_pending_continuation_change_during_assembly_invalidates_stale_switch(
    tmp_path, monkeypatch
):
    """装配期间 register_continuation 改选另一历史片段 → 旧操作 stale、新选择保留。"""
    app = _app(tmp_path)
    topic_a = _topic(app, "甲话题")
    topic_b = _topic(app, "乙话题")
    _open_fragment(app, topic_b)
    first_historic, _middle_open = _seal_historic(app, topic_a)
    second_historic, _open = _seal_historic(app, topic_a)

    first = app.navigation.register_continuation(
        topic_a, first_historic, request_id="r15f-pre"
    )
    assert first["changed"] is True

    adapter = _ScriptedAdapter([[_switch_call(topic_b)], "改选待接续后的回答"])
    _install_adapter(app, adapter)
    order: list[str] = []
    gate = _AssemblyGate(order).install(app, monkeypatch)
    captured = _spy_snapshot(app, monkeypatch, order)

    task = asyncio.create_task(app.run_turn("换到乙话题", topic_id=topic_a))
    await asyncio.wait_for(gate.arrived.wait(), 30)
    changed = app.navigation.register_continuation(
        topic_a, second_historic, request_id="r15f-change"
    )
    assert changed["changed"] is True, "改选另一段历史必须是一次真实位置变化"
    user_version = nav_current(app.conn)
    gate.release.set()

    result = await asyncio.wait_for(task, 60)
    anchor = AnchorService(app.conn).get_active()
    assert anchor is not None and anchor.topic_id == topic_a
    pending = app.bindings.peek_intent()
    assert pending is not None and pending.source_fragment_id == second_historic, (
        "装配期间用户的新待接续选择必须保留，不能被旧操作顶掉"
    )
    assert nav_current(app.conn) == user_version, "旧操作不得推进导航版本"
    assert result["ok"] is True, "正式回答仍要按原绑定归属完成（不整轮停止）"
    rows = _assistant_rows(app)
    assert rows, "正式回答必须保存"
    # 本轮真实绑定来自 begin 的 h1 落实（新片段 D），回答必须归属 D 而不是旧操作目标。
    intents = [row for row in app.conn.execute(
        "SELECT resolved_fragment_id FROM continuation_intents WHERE intent_id = ?",
        (first["intent_id"],),
    )]
    assert intents and intents[0]["resolved_fragment_id"], "begin 应已落实 h1 到真实片段"
    assert rows[-1]["fragment_id"] == intents[0]["resolved_fragment_id"]
    snapshot = captured["snapshot"]
    assert snapshot["nav_version"] is not None
    assert snapshot["topic_id"] == topic_a


async def test_r1_control_group_normal_switch_executes_and_updates_causally(tmp_path, monkeypatch):
    """对照组：装配期间无导航变化 → 快照版本=当前版本，操作正常执行。"""
    app = _app(tmp_path)
    topic_a = _topic(app, "甲话题")
    topic_b = _topic(app, "乙话题")
    _open_fragment(app, topic_a)
    _open_fragment(app, topic_b)
    app.navigation.enter_topic(topic_a)
    base_version = nav_current(app.conn)

    adapter = _ScriptedAdapter([[_switch_call(topic_b)], "目标话题里的正式回答"])
    _install_adapter(app, adapter)
    order: list[str] = []
    _AssemblyGate(order).install_plain(app, monkeypatch)
    captured = _spy_snapshot(app, monkeypatch, order)

    result = await asyncio.wait_for(app.run_turn("换到乙话题", topic_id=topic_a), 60)

    assert result["ok"] is True
    anchor = AnchorService(app.conn).get_active()
    assert anchor is not None and anchor.topic_id == topic_b, "无导航变化时正常切换必须执行"
    assert nav_current(app.conn) == base_version + 1, "一次真实导航只推进一次版本"
    assert captured["snapshot"]["nav_version"] == base_version
    assert captured["order"] == ["snapshot", "assembly"], (
        f"快照必须早于上下文装配：{captured['order']}"
    )
    rows = _assistant_rows(app)
    assert rows and "目标话题里的正式回答" in (rows[-1]["content"] or "")


async def test_r1_snapshot_fields_and_verification_state(tmp_path, monkeypatch):
    """快照字段真实写入；装配后复核状态 fresh（契约 §3.2.2/§3.2.3）。"""
    app = _app(tmp_path)
    topic_a = _topic(app, "甲话题")
    open_a = _open_fragment(app, topic_a)
    app.navigation.enter_topic(topic_a, fragment_id=open_a)
    base_version = nav_current(app.conn)

    adapter = _ScriptedAdapter(["直接回答"])
    _install_adapter(app, adapter)
    order: list[str] = []
    _AssemblyGate(order).install_plain(app, monkeypatch)
    captured = _spy_snapshot(app, monkeypatch, order)

    verify = getattr(app.turn_orchestrator, "_verify_decision_basis", None)
    assert callable(verify), "契约 §3.2.3 要求提供 _verify_decision_basis(ctx) 装配后复核"
    original_verify = verify
    states: list[str] = []

    def _verify_spy(ctx):  # noqa: ANN001
        result = original_verify(ctx)
        states.append(str(getattr(ctx, "decision_basis_state", "")))
        return result

    monkeypatch.setattr(app.turn_orchestrator, "_verify_decision_basis", _verify_spy)

    result = await asyncio.wait_for(app.run_turn("普通问题", topic_id=topic_a), 60)
    assert result["ok"] is True
    ctx = captured["ctx"]
    assert ctx is not None, "快照必须被调用"
    assert ctx.turn_id
    assert getattr(ctx, "decision_turn_id", None) == ctx.turn_id, "快照必须记录本轮 turn"
    user_message_id = getattr(ctx, "user_message_id", None)
    assert user_message_id, "本轮用户消息必须落库（build_context 之后）"
    assert getattr(ctx, "decision_user_message_id", None) == user_message_id, (
        "快照必须记录原用户消息 id"
    )
    assert hasattr(ctx, "decision_intent_id") and hasattr(ctx, "decision_intent_version"), (
        "契约 §3.2.2 要求 decision_intent_id / decision_intent_version 字段"
    )
    assert ctx.decision_intent_id is None and ctx.decision_intent_version is None
    assert ctx.decision_topic_id == topic_a
    assert ctx.decision_fragment_id == open_a
    assert ctx.decision_nav_version == base_version
    assert states and states[-1] == "fresh", f"无导航变化时复核状态应为 fresh：{states}"
    assert captured["order"] == ["snapshot", "assembly"]


async def test_r1_verification_reports_unknown_when_navigation_version_unreadable(
    tmp_path, monkeypatch
):
    """装配后复核读不到导航版本 → unknown（不伪装成 fresh）。"""
    app = _app(tmp_path)
    topic_a = _topic(app, "甲话题")
    open_a = _open_fragment(app, topic_a)
    app.navigation.enter_topic(topic_a, fragment_id=open_a)

    adapter = _ScriptedAdapter(["直接回答"])
    _install_adapter(app, adapter)
    _AssemblyGate().install_plain(app, monkeypatch)
    captured = _spy_snapshot(app, monkeypatch)

    from agent.services import navigation_version

    def _boom(conn):  # noqa: ANN001
        raise RuntimeError("navigation_version 表不可读")

    monkeypatch.setattr(navigation_version, "current", _boom)
    result = await asyncio.wait_for(app.run_turn("普通问题", topic_id=topic_a), 60)
    assert result["ok"] is True
    ctx = captured["ctx"]
    assert getattr(ctx, "decision_nav_version", None) is None, "读不到版本时快照如实 None"
    assert getattr(ctx, "decision_basis_state", None) == "unknown", (
        "版本读不到必须如实 unknown，不得伪装 fresh/superseded"
    )


# ===========================================================================
# R3：历史旧请求不能绕过服务层失效结果（契约 §5.3，真实 HTTP）
# ===========================================================================


@pytest.fixture()
def api_client(tmp_path):
    conn = connect(tmp_path / "r15_f_r3.db")
    apply_migrations(conn)
    settings = Settings(data_dir=tmp_path)
    app = create_app(settings, conn)
    ctx: AppContext = app.state.ctx
    ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as client:
        yield client


def _r3_ctx(client: TestClient) -> AppContext:
    return client.app.state.ctx


def _seed_closed(ctx: AppContext, topic_id: str, fragment_id: str, text: str) -> str:
    now = datetime.now(timezone.utc).isoformat()
    ctx.conn.execute(
        "INSERT INTO fragments (id, topic_id, summary, created_at, closed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (fragment_id, topic_id, text, now, now),
    )
    ctx.conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
        "VALUES (?, ?, 'user', ?, 'text', ?)",
        (fragment_id + "_m", fragment_id, text, now),
    )
    ctx.conn.commit()
    return fragment_id


def _post_continue(client: TestClient, topic_id: str, fragment_id: str, request_id=None):
    body = {
        "topic_id": topic_id,
        "fragment_id": fragment_id,
        "continue_from_history": True,
    }
    if request_id is not None:
        body["request_id"] = request_id
    return client.post("/api/anchor", json=body)


def _r3_facts(ctx: AppContext) -> dict:
    anchor = AnchorService(ctx.conn).get_active()
    pending = ctx.bindings.peek_intent()
    return {
        "anchor_topic": anchor.topic_id if anchor else None,
        "anchor_fragment": anchor.fragment_id if anchor else None,
        "pending_intent": pending.intent_id if pending else None,
        "pending_source": pending.source_fragment_id if pending else None,
        "pending_version": pending.version if pending else None,
        "nav_version": nav_current(ctx.conn),
        "fragments": ctx.conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"],
    }


def _intent_state(ctx: AppContext, request_id: str):
    intent = ctx.bindings.intent_by_request(request_id)
    return intent.state if intent is not None else None


def _r3_error_payload(resp) -> dict:  # noqa: ANN001
    body = resp.json()
    if isinstance(body, dict) and isinstance(body.get("detail"), dict):
        return body["detail"]
    return body if isinstance(body, dict) else {}


def test_r3_duplicate_same_request_is_zero_write(api_client):
    """A→重复 A（同 request_id）：200 replay、零写入零推进。"""
    ctx = _r3_ctx(api_client)
    topic = _topic(ctx, "甲话题")
    fragment = _seed_closed(ctx, topic, "r3f_a1", "历史 A1")

    first = _post_continue(api_client, topic, fragment, "r3-req-a")
    assert first.status_code == 200, first.text
    facts_after_first = _r3_facts(ctx)

    second = _post_continue(api_client, topic, fragment, "r3-req-a")
    assert second.status_code == 200, second.text
    assert _r3_facts(ctx) == facts_after_first, "同 request_id 幂等重放必须零位置写入"
    assert _intent_state(ctx, "r3-req-a") == "registered"


def test_r3_a_then_b_then_replay_a_is_rejected_inactive(api_client):
    """A→B→重放 A：第三次 409 inactive；Anchor/pending/版本/意图全部保持 B 的事实。"""
    ctx = _r3_ctx(api_client)
    topic_a = _topic(ctx, "甲话题")
    topic_b = _topic(ctx, "乙话题")
    frag_a = _seed_closed(ctx, topic_a, "r3f_a1", "历史 A1")
    frag_b = _seed_closed(ctx, topic_b, "r3f_b1", "历史 B1")

    assert _post_continue(api_client, topic_a, frag_a, "r3-req-a").status_code == 200
    facts_a = _r3_facts(ctx)
    assert _post_continue(api_client, topic_b, frag_b, "r3-req-b").status_code == 200
    facts_b = _r3_facts(ctx)
    assert facts_b["anchor_topic"] == topic_b
    assert facts_b["pending_source"] == frag_b
    assert facts_b["nav_version"] == facts_a["nav_version"] + 1, "真实改选只推进一次"
    assert _intent_state(ctx, "r3-req-a") == "replaced"
    assert _intent_state(ctx, "r3-req-b") == "registered"

    replay = _post_continue(api_client, topic_a, frag_a, "r3-req-a")
    assert replay.status_code == 409, replay.text
    payload = _r3_error_payload(replay)
    assert payload.get("ok") is False
    assert payload.get("conflict") is True
    assert payload.get("code") == "continuation_request_inactive"
    assert _r3_facts(ctx) == facts_b, "失效旧请求重放必须零写入（位置/接续/版本/片段数）"
    assert _intent_state(ctx, "r3-req-a") == "replaced"
    assert _intent_state(ctx, "r3-req-b") == "registered"


def test_r3_replay_after_cancel_is_rejected(api_client):
    """A 取消后重放 A：409 inactive；Anchor/pending/版本均不变。"""
    ctx = _r3_ctx(api_client)
    topic_a = _topic(ctx, "甲话题")
    frag_a = _seed_closed(ctx, topic_a, "r3f_a1", "历史 A1")

    assert _post_continue(api_client, topic_a, frag_a, "r3-req-cancel").status_code == 200
    cancelled = api_client.post("/api/anchor/continue/cancel")
    assert cancelled.status_code == 200
    facts_cancelled = _r3_facts(ctx)

    replay = _post_continue(api_client, topic_a, frag_a, "r3-req-cancel")
    assert replay.status_code == 409, replay.text
    payload = _r3_error_payload(replay)
    assert payload.get("code") == "continuation_request_inactive"
    assert _r3_facts(ctx) == facts_cancelled
    assert _intent_state(ctx, "r3-req-cancel") == "cancelled"


def test_r3_replay_after_consume_is_idempotent_replay(api_client):
    """A 已消费后重放 A（无更新选择）：200 replay、零写入零推进。"""
    ctx = _r3_ctx(api_client)
    topic_a = _topic(ctx, "甲话题")
    frag_a = _seed_closed(ctx, topic_a, "r3f_a1", "历史 A1")

    assert _post_continue(api_client, topic_a, frag_a, "r3-req-used").status_code == 200
    intent = ctx.bindings.intent_by_request("r3-req-used")
    assert intent is not None
    applied = ctx.navigation.apply_continuation(topic_a, intent.intent_id)
    ctx.bindings.consume_intent(intent.intent_id)
    facts_consumed = _r3_facts(ctx)
    assert facts_consumed["anchor_fragment"] == applied.fragment_id

    replay = _post_continue(api_client, topic_a, frag_a, "r3-req-used")
    assert replay.status_code == 200, replay.text
    assert _r3_facts(ctx) == facts_consumed, "已消费的幂等重放必须零写入"


def test_r3_user_reselects_a_after_b_advances_once(api_client):
    """B 之后用户重新主动选择 A（新 request_id）：200 changed、版本 +1。"""
    ctx = _r3_ctx(api_client)
    topic_a = _topic(ctx, "甲话题")
    topic_b = _topic(ctx, "乙话题")
    frag_a = _seed_closed(ctx, topic_a, "r3f_a1", "历史 A1")
    frag_b = _seed_closed(ctx, topic_b, "r3f_b1", "历史 B1")

    assert _post_continue(api_client, topic_a, frag_a, "r3-req-a").status_code == 200
    assert _post_continue(api_client, topic_b, frag_b, "r3-req-b").status_code == 200
    facts_b = _r3_facts(ctx)

    reselect = _post_continue(api_client, topic_a, frag_a, "r3-req-new-a")
    assert reselect.status_code == 200, reselect.text
    facts_new = _r3_facts(ctx)
    assert facts_new["anchor_topic"] == topic_a
    assert facts_new["anchor_fragment"] == frag_a
    assert facts_new["pending_source"] == frag_a
    assert facts_new["nav_version"] == facts_b["nav_version"] + 1
    assert _intent_state(ctx, "r3-req-b") == "replaced"
    assert _intent_state(ctx, "r3-req-new-a") == "registered"
    assert facts_new["fragments"] == facts_b["fragments"], "点击路径不得创建片段"


def test_r3_request_id_reused_with_different_params_is_conflict(api_client):
    """同一 request_id 用于不同 (topic, fragment) → 409 conflict、零写入。"""
    ctx = _r3_ctx(api_client)
    topic_a = _topic(ctx, "甲话题")
    topic_b = _topic(ctx, "乙话题")
    frag_a = _seed_closed(ctx, topic_a, "r3f_a1", "历史 A1")
    frag_b = _seed_closed(ctx, topic_b, "r3f_b1", "历史 B1")

    assert _post_continue(api_client, topic_a, frag_a, "r3-req-shared").status_code == 200
    facts_a = _r3_facts(ctx)

    conflict = _post_continue(api_client, topic_b, frag_b, "r3-req-shared")
    assert conflict.status_code == 409, conflict.text
    payload = _r3_error_payload(conflict)
    assert payload.get("code") == "continuation_request_conflict"
    assert _r3_facts(ctx) == facts_a, "参数冲突必须零写入"
    intent = ctx.bindings.intent_by_request("r3-req-shared")
    assert intent is not None and intent.topic_id == topic_a


def test_r3_plain_anchor_does_not_create_continuation(api_client):
    """普通（非 continue）POST /api/anchor 行为不变：进入话题、不登记接续。"""
    ctx = _r3_ctx(api_client)
    topic_a = _topic(ctx, "甲话题")
    _open_fragment(ctx, topic_a)
    before = _r3_facts(ctx)
    resp = api_client.post("/api/anchor", json={"topic_id": topic_a})
    assert resp.status_code == 200, resp.text
    facts = _r3_facts(ctx)
    assert facts["anchor_topic"] == topic_a
    assert facts["pending_intent"] is None
    assert facts["nav_version"] == before["nav_version"] + 1


# ===========================================================================
# R4：逻辑答案执行权、租约续期与写入权收口（契约 §6.4）
# ===========================================================================


class _Clock:
    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=float(seconds))


def _freeze_lease_clock(monkeypatch, clock: _Clock) -> None:
    """契约 §6.2.5：租约时间唯一来源是模块级 _now_utc（测试 monkeypatch 它）。"""
    assert hasattr(topic_recovery, "_now_utc"), (
        "契约 §6.2.5 要求 topic_recovery 提供模块级 _now_utc() 作为租约时间唯一来源"
    )
    monkeypatch.setattr(topic_recovery, "_now_utc", lambda: clock.now)


def _install_r4(app: AppContext, adapter) -> None:
    class _AnswerMessages:
        async def prepare_topic_answer_messages(self, *, turn_id, topic_id, user_message_id):
            return [ChatMessage(role="user", content="原问题")]

    app.turn_orchestrator = _AnswerMessages()  # type: ignore[assignment]
    _install_adapter(app, adapter)


class _RecoveryScope:
    """第二个执行者/实例身份（共享同一 SQLite 与实例登记表）。"""

    def __init__(self, app: AppContext, adapter, instance_id: str) -> None:
        self.conn = app.conn
        self.instances = app.instances
        self.instance_id = instance_id
        self._adapter = adapter

        class _AnswerMessages:
            async def prepare_topic_answer_messages(self, *, turn_id, topic_id, user_message_id):
                return [ChatMessage(role="user", content="原问题")]

        self.turn_orchestrator = _AnswerMessages()

    async def build_adapter(self):
        return self._adapter


class _R4Adapter:
    mode = "native"
    model = "fake-r15-f-r4"
    protocol_in_prompt = False

    def __init__(self, text: str = "恢复后的唯一回答") -> None:
        self.text = text
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False
        self.fail = False

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        self.started.set()
        if self.block:
            await self.release.wait()
        if self.fail:
            raise RuntimeError("fake 模型通道失败")
        return Completion(
            message=ChatMessage(role="assistant", content=self.text)
        )


class _HeartbeatGate:
    """heartbeat_wait 测试缝：每次续租前阻塞，测试用事件控制节奏（不用 sleep）。"""

    def __init__(self) -> None:
        self.calls = 0
        self._cond = asyncio.Condition()
        self._allow = asyncio.Event()

    async def wait(self) -> None:
        async with self._cond:
            self.calls += 1
            self._cond.notify_all()
        await self._allow.wait()
        self._allow.clear()

    def allow_next(self) -> None:
        self._allow.set()

    async def wait_calls(self, count: int) -> None:
        async with self._cond:
            await self._cond.wait_for(lambda: self.calls >= count)


class _BlockingHeartbeat:
    async def wait(self) -> None:
        await asyncio.Event().wait()  # 永不续租：让租约自然到期（受控时钟推进）


def _seed_recovery_target(app: AppContext) -> tuple[str, str, str]:
    topic_id = _topic(app, "恢复目标话题")
    fragment_id = _open_fragment(app, topic_id)
    message_id, _ = app.memory.append_message(
        topic_id=topic_id,
        role="user",
        content="恢复的原问题",
        content_type="text",
        model="fake",
        turn_id="turn_r15f",
    )
    return topic_id, fragment_id, message_id


def _make_recovery_record(app: AppContext, topic_id: str, message_id: str, **overrides):
    payload = {
        "turn_id": "turn_r15f",
        "topic_id": topic_id,
        "user_message_id": message_id,
        "failed_stage": "answer",
        "message": "回答阶段失败",
        "stages": TopicStages(tool_success=True, navigation_applied=True, turn_ownership=True),
    }
    payload.update(overrides)
    return topic_recovery.record_topic_failure(app, **payload)


def _answer_rows(conn: sqlite3.Connection) -> list:
    return conn.execute(
        "SELECT id, content FROM messages WHERE role = 'assistant' AND answer_key IS NOT NULL "
        "ORDER BY rowid"
    ).fetchall()


def _runs_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'topic_answer_runs'"
    ).fetchone()
    return row is not None


def _run_row(conn: sqlite3.Connection, answer_key: str):
    if not _runs_table_exists(conn):
        return None
    return conn.execute(
        "SELECT answer_key, status, generation, owner, claim_until FROM topic_answer_runs "
        "WHERE answer_key = ?",
        (answer_key,),
    ).fetchone()


def _execution_generation(conn: sqlite3.Connection, record_id: str) -> int | None:
    columns = [row[1] for row in conn.execute("PRAGMA table_info(topic_recovery_records)")]
    if "execution_generation" not in columns:
        return None
    row = conn.execute(
        "SELECT execution_generation FROM topic_recovery_records WHERE record_id = ?",
        (record_id,),
    ).fetchone()
    return None if row is None or row[0] is None else int(row[0])


async def test_r4_same_answer_key_two_records_generate_once(tmp_path):
    """同 answer_key、不同 record 并发：只生成一次；等待方 202；最终一份答案。"""
    app = _app(tmp_path)
    topic_id, _fragment, message_id = _seed_recovery_target(app)
    adapter = _R4Adapter()
    adapter.block = True
    _install_r4(app, adapter)

    first = _make_recovery_record(app, topic_id, message_id, record_id="r15f_rec_a")
    second = _make_recovery_record(app, topic_id, message_id, record_id="r15f_rec_b")
    assert first["answer_key"] == second["answer_key"]
    answer_key = first["answer_key"]

    task_first = asyncio.create_task(topic_recovery.retry_topic_answer(app, "r15f_rec_a"))
    await asyncio.wait_for(adapter.started.wait(), 20)
    result_second = await asyncio.wait_for(
        topic_recovery.retry_topic_answer(app, "r15f_rec_b"), 20
    )
    assert result_second.get("code") == "recovery_in_progress", (
        "同一逻辑答案的第二个执行者必须结构化 202，不得再调模型"
    )
    assert int(result_second.get("status_code") or 0) == 202
    assert adapter.calls == 1, f"同一 answer_key 只允许一次模型调用，实际 {adapter.calls}"

    adapter.release.set()
    result_first = await asyncio.wait_for(task_first, 30)
    assert result_first.get("ok") is True and result_first.get("answered") is True

    rows = _answer_rows(app.conn)
    assert len(rows) == 1, "最终只允许一份正式答案"
    record_a = topic_recovery.get_record(app, "r15f_rec_a")
    record_b = topic_recovery.get_record(app, "r15f_rec_b")
    assert record_a["status"] == "answered" and record_b["status"] == "answered"
    assert record_a["answer_id"] == record_b["answer_id"] == rows[0]["id"]
    run = _run_row(app.conn, answer_key)
    assert run is not None, "契约 §6.2.1 要求 topic_answer_runs 记录逻辑答案执行权"
    assert str(run["status"]) in ("answered", "completed")


async def test_r4_same_record_concurrent_generates_once(tmp_path):
    """同 record 并发：第二个请求 202 处理中，模型只调用一次。"""
    app = _app(tmp_path)
    topic_id, _fragment, message_id = _seed_recovery_target(app)
    adapter = _R4Adapter()
    adapter.block = True
    _install_r4(app, adapter)
    record = _make_recovery_record(app, topic_id, message_id)
    record_id = record["record_id"]

    task_first = asyncio.create_task(topic_recovery.retry_topic_answer(app, record_id))
    await asyncio.wait_for(adapter.started.wait(), 20)
    result_second = await asyncio.wait_for(topic_recovery.retry_topic_answer(app, record_id), 20)
    assert result_second.get("code") == "recovery_in_progress"
    assert adapter.calls == 1

    adapter.release.set()
    result_first = await asyncio.wait_for(task_first, 30)
    assert result_first.get("answered") is True
    assert len(_answer_rows(app.conn)) == 1
    assert topic_recovery.get_record(app, record_id)["status"] == "answered"


async def test_r4_live_task_heartbeat_prevents_takeover_beyond_lease(tmp_path, monkeypatch):
    """活任务运行超过原租约周期（受控时钟 + 心跳续租）：第二个请求仍 202、模型仍 1 次。"""
    app = _app(tmp_path)
    clock = _Clock()
    _freeze_lease_clock(monkeypatch, clock)
    topic_id, _fragment, message_id = _seed_recovery_target(app)
    adapter = _R4Adapter()
    adapter.block = True
    _install_r4(app, adapter)
    record = _make_recovery_record(app, topic_id, message_id)
    record_id = record["record_id"]
    heartbeat = _HeartbeatGate()

    task_first = asyncio.create_task(
        topic_recovery.retry_topic_answer(
            app,
            record_id,
            lease_seconds=10.0,
            heartbeat_interval=0.0,
            heartbeat_wait=heartbeat.wait,
        )
    )
    await asyncio.wait_for(adapter.started.wait(), 20)
    # 第 1 次续租（t0）把租约延到 t0+10；推进时钟到 t0+9 做第 2 次续租（延到
    # t0+19）；再推进到 t0+11 —— 已超过最初 claim 的 t0+10，但续租后仍有效。
    await asyncio.wait_for(heartbeat.wait_calls(1), 20)
    heartbeat.allow_next()
    await asyncio.wait_for(heartbeat.wait_calls(2), 20)
    clock.advance(9.0)
    heartbeat.allow_next()
    await asyncio.wait_for(heartbeat.wait_calls(3), 20)
    clock.advance(2.0)

    result_second = await asyncio.wait_for(topic_recovery.retry_topic_answer(app, record_id), 20)
    assert result_second.get("code") == "recovery_in_progress", (
        "活任务靠心跳续租，超过原租约也不能被新请求再次生成"
    )
    assert adapter.calls == 1

    adapter.release.set()
    result_first = await asyncio.wait_for(task_first, 30)
    assert result_first.get("answered") is True
    assert len(_answer_rows(app.conn)) == 1


async def test_r4_confirmed_dead_instance_takeover_succeeds(tmp_path, monkeypatch):
    """真正失效实例（显式退出）的到期租约可被接管：世代递增、只生成一次。"""
    app = _app(tmp_path)
    clock = _Clock()
    _freeze_lease_clock(monkeypatch, clock)
    topic_id, _fragment, message_id = _seed_recovery_target(app)
    record = _make_recovery_record(app, topic_id, message_id)
    record_id = record["record_id"]
    answer_key = record["answer_key"]

    dead_registry = InstanceRegistry(
        app.conn, "qio_r15f_dead", pid=999_999, pid_alive=lambda pid: False
    )
    dead_registry.start()
    dead_registry.mark_clean_exit()
    claimed = topic_recovery.claim_record(
        app.conn, record_id, owner="qio_r15f_dead|legacy-owner", lease_seconds=10.0
    )
    assert claimed is not None, "前置：旧执行者领取了记录"
    clock.advance(11.0)  # 租约到期

    adapter = _R4Adapter("失效实例接管后的回答")
    _install_r4(app, adapter)
    result = await asyncio.wait_for(
        topic_recovery.retry_topic_answer(app, record_id, lease_seconds=10.0), 30
    )
    assert result.get("ok") is True and result.get("answered") is True, (
        "确认失效实例的到期租约必须能被接管并完成回答"
    )
    assert adapter.calls == 1
    rows = _answer_rows(app.conn)
    assert len(rows) == 1
    run = _run_row(app.conn, answer_key)
    assert run is not None, "接管必须写 topic_answer_runs 执行权"
    assert int(run["generation"]) >= 1
    record_after = topic_recovery.get_record(app, record_id)
    assert record_after["status"] == "answered"
    generation = _execution_generation(app.conn, record_id)
    assert generation is not None, "契约 §6.3 要求 topic_recovery_records.execution_generation"
    assert generation >= 1


async def test_r4_alive_owner_expired_lease_is_not_taken_over(tmp_path, monkeypatch):
    """租约到期但旧实例仍活着（判 True）→ 不得接管，返回处理中、不调模型。

    真实执行者（当前实例）已领取执行权并在等模型；心跳被测试门挡住不续租，
    受控时钟越过租约。此时第二个请求到达：旧实例仍活着（InstanceRegistry
    owner_alive=True）→ 只能 202，不得因「时间到了」重新生成。
    """
    app = _app(tmp_path)
    clock = _Clock()
    _freeze_lease_clock(monkeypatch, clock)
    topic_id, _fragment, message_id = _seed_recovery_target(app)
    adapter = _R4Adapter()
    adapter.block = True
    _install_r4(app, adapter)
    record = _make_recovery_record(app, topic_id, message_id)
    record_id = record["record_id"]
    heartbeat = _HeartbeatGate()

    task_first = asyncio.create_task(
        topic_recovery.retry_topic_answer(
            app,
            record_id,
            lease_seconds=10.0,
            heartbeat_interval=3600.0,
            heartbeat_wait=heartbeat.wait,
        )
    )
    await asyncio.wait_for(adapter.started.wait(), 20)
    await asyncio.wait_for(heartbeat.wait_calls(1), 20)  # 心跳被门挡住，不续租
    clock.advance(11.0)  # 租约时间到期，但实例心跳仍新鲜（owner_alive=True）

    result = await asyncio.wait_for(
        topic_recovery.retry_topic_answer(app, record_id, lease_seconds=10.0), 30
    )
    assert result.get("code") == "recovery_in_progress", (
        "实例存活时不得因租约时间到期就接管（活任务靠心跳，不靠延长超时）"
    )
    assert adapter.calls == 1, "未接管时不得发起第二次模型调用"

    adapter.release.set()
    result_first = await asyncio.wait_for(task_first, 30)
    assert result_first.get("answered") is True
    assert len(_answer_rows(app.conn)) == 1


async def test_r4_late_success_after_takeover_is_superseded(tmp_path, monkeypatch):
    """旧执行者迟到成功：接管后不得写答案/改记录，返回 superseded。"""
    app = _app(tmp_path)
    clock = _Clock()
    _freeze_lease_clock(monkeypatch, clock)
    topic_id, _fragment, message_id = _seed_recovery_target(app)
    record = _make_recovery_record(app, topic_id, message_id)
    record_id = record["record_id"]
    answer_key = record["answer_key"]

    old_adapter = _R4Adapter("旧执行者的迟到回答")
    old_adapter.block = True
    _install_r4(app, old_adapter)
    blocker = _BlockingHeartbeat()
    task_old = asyncio.create_task(
        topic_recovery.retry_topic_answer(
            app,
            record_id,
            lease_seconds=10.0,
            heartbeat_interval=0.0,
            heartbeat_wait=blocker.wait,
        )
    )
    await asyncio.wait_for(old_adapter.started.wait(), 20)
    old_run = _run_row(app.conn, answer_key)
    assert old_run is not None
    old_generation = int(old_run["generation"])

    # 旧执行者所在实例被确认退出 → 新实例按契约接管（世代 +1）。
    app.instances.mark_clean_exit()
    clock.advance(11.0)
    new_adapter = _R4Adapter("接管者的正式回答")
    new_scope = _RecoveryScope(app, new_adapter, "qio_r15f_new")
    result_new = await asyncio.wait_for(
        topic_recovery.retry_topic_answer(new_scope, record_id, lease_seconds=10.0), 30
    )
    assert result_new.get("ok") is True and result_new.get("answered") is True
    new_run = _run_row(app.conn, answer_key)
    assert new_run is not None and int(new_run["generation"]) > old_generation

    old_adapter.release.set()
    result_old = await asyncio.wait_for(task_old, 30)
    assert result_old.get("ok") is False
    assert result_old.get("code") == "recovery_execution_superseded", (
        f"失去执行权的旧任务必须如实返回 superseded：{result_old}"
    )
    assert result_old.get("superseded") is True
    rows = _answer_rows(app.conn)
    assert len(rows) == 1, "旧执行者不得补写第二份答案"
    record_after = topic_recovery.get_record(app, record_id)
    assert record_after["status"] == "answered"
    assert record_after["answer_id"] == result_new.get("answer_id") == rows[0]["id"]


async def test_r4_late_failure_does_not_corrupt_new_execution(tmp_path, monkeypatch):
    """旧执行者迟到失败：不得把新任务的记录改成失败、不得清新租约。"""
    app = _app(tmp_path)
    clock = _Clock()
    _freeze_lease_clock(monkeypatch, clock)
    topic_id, _fragment, message_id = _seed_recovery_target(app)
    record = _make_recovery_record(app, topic_id, message_id)
    record_id = record["record_id"]

    old_adapter = _R4Adapter()
    old_adapter.block = True
    old_adapter.fail = True
    _install_r4(app, old_adapter)
    blocker = _BlockingHeartbeat()
    task_old = asyncio.create_task(
        topic_recovery.retry_topic_answer(
            app,
            record_id,
            lease_seconds=10.0,
            heartbeat_interval=0.0,
            heartbeat_wait=blocker.wait,
        )
    )
    await asyncio.wait_for(old_adapter.started.wait(), 20)

    app.instances.mark_clean_exit()
    clock.advance(11.0)
    new_adapter = _R4Adapter("接管中的新执行者")
    new_adapter.block = True
    new_scope = _RecoveryScope(app, new_adapter, "qio_r15f_new_fail")
    task_new = asyncio.create_task(
        topic_recovery.retry_topic_answer(new_scope, record_id, lease_seconds=10.0)
    )
    await asyncio.wait_for(new_adapter.started.wait(), 20)

    old_adapter.release.set()
    result_old = await asyncio.wait_for(task_old, 30)
    assert result_old.get("ok") is False
    assert result_old.get("code") == "recovery_execution_superseded", (
        "失去执行权的旧任务迟到失败必须按 CAS 放弃写入"
    )
    record_mid = topic_recovery.get_record(app, record_id)
    assert record_mid["status"] == "pending", "旧执行者失败不得把新任务记录改成失败"
    assert "qio_r15f_new_fail" in str(record_mid.get("claim_owner") or ""), (
        "旧执行者失败不得清掉新执行者的租约"
    )
    assert not record_mid.get("answer_text")
    assert _answer_rows(app.conn) == []

    new_adapter.release.set()
    result_new = await asyncio.wait_for(task_new, 30)
    assert result_new.get("answered") is True
    assert len(_answer_rows(app.conn)) == 1
    assert topic_recovery.get_record(app, record_id)["status"] == "answered"


async def test_r4_save_failure_stashes_and_retry_only_persists(tmp_path):
    """生成成功但保存失败：正文暂存；再次重试只补存、不重新调用模型。"""
    app = _app(tmp_path)
    topic_id, _fragment, message_id = _seed_recovery_target(app)
    adapter = _R4Adapter("已生成但保存失败的正文")
    _install_r4(app, adapter)
    record = _make_recovery_record(app, topic_id, message_id)
    record_id = record["record_id"]

    # 用真实 SQLite 触发器制造一次「答案写入失败」（不依赖实现内部函数名）。
    app.conn.execute(
        "CREATE TRIGGER r15f_block_answer BEFORE INSERT ON messages "
        "WHEN NEW.answer_key IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'r15f blocked answer insert'); END"
    )
    app.conn.commit()
    first = await asyncio.wait_for(topic_recovery.retry_topic_answer(app, record_id), 30)
    assert first.get("ok") is False
    assert first.get("code") == "recovery_persist_failed", f"保存失败必须如实：{first}"
    assert adapter.calls == 1
    record_after_fail = topic_recovery.get_record(app, record_id)
    assert record_after_fail["status"] == "pending"
    assert record_after_fail["answer_text"] == "已生成但保存失败的正文"
    assert _answer_rows(app.conn) == []

    app.conn.execute("DROP TRIGGER r15f_block_answer")
    app.conn.commit()
    second = await asyncio.wait_for(topic_recovery.retry_topic_answer(app, record_id), 30)
    assert second.get("ok") is True and second.get("answered") is True
    assert adapter.calls == 1, "补存不得重新生成（模型调用次数必须保持 1）"
    rows = _answer_rows(app.conn)
    assert len(rows) == 1
    assert rows[0]["content"] == "已生成但保存失败的正文"
    assert topic_recovery.get_record(app, record_id)["status"] == "answered"


async def test_r4_takeover_racing_old_save_writes_exactly_one_answer(tmp_path, monkeypatch):
    """接管与旧执行者保存竞争：保存事务必须复核执行权，最终只有一份答案。"""
    app = _app(tmp_path)
    clock = _Clock()
    _freeze_lease_clock(monkeypatch, clock)
    topic_id, _fragment, message_id = _seed_recovery_target(app)
    record = _make_recovery_record(app, topic_id, message_id)
    record_id = record["record_id"]

    real_generate = getattr(topic_recovery, "_generate_answer", None)
    assert callable(real_generate), (
        "本用例需要「模型返回后、保存前」的受控屏障（内部 _generate_answer 钩子）"
    )
    return_gate = {"entered": asyncio.Event(), "release": asyncio.Event()}
    old_adapter = _R4Adapter("旧执行者已生成、尚未保存的正文")
    _install_r4(app, old_adapter)
    blocker = _BlockingHeartbeat()

    async def _gated_generate(adapter, messages):  # noqa: ANN001
        answer = await real_generate(adapter, messages)
        if adapter is old_adapter:
            return_gate["entered"].set()
            await return_gate["release"].wait()
        return answer

    monkeypatch.setattr(topic_recovery, "_generate_answer", _gated_generate)

    task_old = asyncio.create_task(
        topic_recovery.retry_topic_answer(
            app,
            record_id,
            lease_seconds=10.0,
            heartbeat_interval=0.0,
            heartbeat_wait=blocker.wait,
        )
    )
    await asyncio.wait_for(return_gate["entered"].wait(), 30)

    # 旧实例确认退出 + 租约到期 → 新执行权接管；新执行者的模型调用被屏障挡住。
    app.instances.mark_clean_exit()
    clock.advance(11.0)
    new_adapter = _R4Adapter("接管者的唯一答案")
    new_adapter.block = True
    new_scope = _RecoveryScope(app, new_adapter, "qio_r15f_race_new")
    blocker_new = _BlockingHeartbeat()
    task_new = asyncio.create_task(
        topic_recovery.retry_topic_answer(
            new_scope,
            record_id,
            lease_seconds=10.0,
            heartbeat_interval=0.0,
            heartbeat_wait=blocker_new.wait,
        )
    )
    await asyncio.wait_for(new_adapter.started.wait(), 20)

    # 现在放行旧执行者的保存：它已失去执行权，必须放弃写入。
    return_gate["release"].set()
    result_old = await asyncio.wait_for(task_old, 30)
    assert result_old.get("ok") is False
    assert result_old.get("code") == "recovery_execution_superseded"
    assert _answer_rows(app.conn) == [], "旧执行者的迟到保存不得写入答案"
    record_mid = topic_recovery.get_record(app, record_id)
    assert record_mid["status"] == "pending"
    assert "qio_r15f_race_new" in str(record_mid.get("claim_owner") or "")

    new_adapter.release.set()
    result_new = await asyncio.wait_for(task_new, 30)
    assert result_new.get("answered") is True
    rows = _answer_rows(app.conn)
    assert len(rows) == 1
    assert rows[0]["content"] == "接管者的唯一答案"
    assert topic_recovery.get_record(app, record_id)["answer_id"] == rows[0]["id"]
