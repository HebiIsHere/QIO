# -*- coding: utf-8 -*-
"""R18-A：互动板提交的受理依据（P1）—— 排队中的互动提交轮不得覆盖用户后来的新导航。

契约：docs/topic-unify-r18-contract-20261111.md §2.1 / §5 A1—A3。
基线红证据：D:\\qio-dev\\r18-evidence\\a\\baseline-red\\r18_a_baseline_red.log

被测系统：真实 FastAPI create_app + 真实迁移 + 临时 SQLite + TestClient（真实 lifespan）
+ fake gated provider（可计数、可挂受控闸门；绝不联网）。顺序由 threading 事件闸门控制，
不用 sleep 构造并发时序（_wait_until 只用于等待异步事实收口）。

覆盖：
* A1 互动板提交在普通轮执行期间受理（排队）→ 手动导航到话题 B → 放行旧轮：
  互动轮的用户消息与正式回答仍在受理话题 A；权威位置仍是 B；没有把 A 广播成当前位置的
  迟到 ANCHOR；position_version 保持切换后的值；turn.board_topic_id 取受理快照话题。
  修复前（基线）：accept_turn 没有传 basis，begin 补锚用**实时** position_version 做 CAS，
  会把位置从 B 改回 A 并广播 ANCHOR —— 本用例在基线上因此失败。
* A2 无导航干扰的互动板提交仍在受理话题正常完成，收尾推进/广播行为不变。
"""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

from agent.adapters.base import ChatMessage, Completion
from agent.api.events import EventType
from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.interactive import models
from agent.services.predict import TopicPrediction
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

BOARD = "board_r18a"
ANSWER_BODY = "R18-A 互动受理依据验收回答"
ANSWER = "[[QIO:ANSWER]]\n" + ANSWER_BODY
GATE_TIMEOUT = 60.0


def _request_id(tag: str) -> str:
    return f"req-r18a-{tag}-{uuid.uuid4().hex}"


def _wait_until(predicate, *, timeout: float = 30.0, interval: float = 0.02) -> bool:
    """有界等待异步事实（不是用 sleep 构造并发顺序：顺序仍由事件闸门决定）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class GatedAdapter:
    """第 N 次调用进入后可挂起，直到测试放行；记录调用次数。绝不联网。"""

    mode = "native"
    model = "fake-r18-a"
    supports_stream = False
    protocol_in_prompt = False

    def __init__(self, gates: dict[int, threading.Event] | None = None) -> None:
        self.calls = 0
        self.started = {index: threading.Event() for index in (gates or {})}
        self.gates = dict(gates or {})

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        index = self.calls
        gate = self.gates.get(index)
        if gate is not None:
            self.started[index].set()
            await asyncio.to_thread(gate.wait, GATE_TIMEOUT)
        return Completion(message=ChatMessage(role="assistant", content=ANSWER))


@contextmanager
def _backend(tmp_path: Path, gate_calls: tuple[int, ...] = ()):
    conn = connect(tmp_path / "r18_a_interactive_basis.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path), conn)
    ctx = app.state.ctx
    ctx.credentials._kr = MemoryKeyring()
    gates = {index: threading.Event() for index in gate_calls}
    adapter = GatedAdapter(gates)

    async def _build_adapter():
        return adapter

    ctx.build_adapter = _build_adapter  # type: ignore[assignment]

    async def _fake_predict(turn_ctx, message, topic):  # noqa: ANN001
        return TopicPrediction(main_topic_id=topic, backend_used="rules")

    ctx.turn_orchestrator._predict_topics = _fake_predict  # type: ignore[assignment]
    ctx.turn_orchestrator._schedule_derived_work = lambda *a, **k: None  # type: ignore[assignment]
    try:
        with TestClient(app) as client:
            yield client, ctx, adapter, gates
    finally:
        for gate in gates.values():
            gate.set()
        conn.close()


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _anchor(client: TestClient) -> dict:
    response = client.get("/api/runtime/state")
    assert response.status_code == 200, response.text
    return response.json()["anchor"]


def _anchor_events(ctx) -> list[dict]:
    return [
        dict(event.data or {})
        for event in list(ctx.bus._history)  # noqa: SLF001 - 与既有测试同口径读总线历史
        if getattr(event, "type", None) == EventType.ANCHOR
    ]


def _switch_topic(
    client: TestClient, topic_id: str, *, fragment_id: str | None = None
) -> dict:
    base = int(_anchor(client)["nav_version"])
    body = {
        "topic_id": topic_id,
        "fragment_id": fragment_id,
        "request_id": _request_id("switch"),
        "base_nav_version": base,
    }
    response = client.post("/api/anchor", json=body)
    assert response.status_code == 200, response.text
    switched = response.json()
    assert switched["changed"] is True, switched
    return switched


def _message_rows(conn, turn_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, role, content, fragment_id FROM messages"
        " WHERE turn_id = ? ORDER BY created_at, id",
        (turn_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _turn_status(conn, turn_id: str) -> str | None:
    row = conn.execute(
        "SELECT status FROM turn_journal WHERE turn_id = ?", (turn_id,)
    ).fetchone()
    return str(row["status"]) if row is not None else None


def _wait_turn_done(conn, turn_id: str, *, timeout: float = 30.0) -> None:
    """等到轮次真正收口（终态包含轮末位置推进），避免只等到消息落库就断言位置。"""
    assert _wait_until(
        lambda: _turn_status(conn, turn_id) in ("completed", "failed", "cancelled"),
        timeout=timeout,
    ), f"轮次没有收口：status={_turn_status(conn, turn_id)}"
    assert _turn_status(conn, turn_id) == "completed", _turn_status(conn, turn_id)


def _fragment_topic(conn, fragment_id: str | None) -> str | None:
    if not fragment_id:
        return None
    row = conn.execute(
        "SELECT topic_id FROM fragments WHERE id = ?", (fragment_id,)
    ).fetchone()
    return str(row["topic_id"]) if row is not None else None


def _save_board(client: TestClient) -> None:
    """创建互动板面并保存一张已勾选的文字卡（提交才有内容、状态才会 succeeded）。"""
    created = client.post("/api/interactive/boards", json={"boardId": BOARD})
    assert created.status_code == 200, created.text
    card = models.new_card("text", "R18-A 互动板提交内容", checked=True)
    card["id"] = "note_r18a"
    state = {
        "boardId": BOARD,
        "seq": 0,
        "updatedAt": models.now_iso(),
        "cards": [card],
        "groups": [],
        "links": [],
        "selection": [],
    }
    response = client.put(
        f"/api/interactive/boards/{BOARD}/state",
        json={"state": state, "reason": "r18_a_test"},
    )
    assert response.status_code == 200, response.text


def _submit_board(client: TestClient):
    return client.post(f"/api/interactive/boards/{BOARD}/submissions", json={})


def _submit_normal_turn(client: TestClient, message: str, topic_id: str) -> dict:
    response = client.post(
        "/api/turns", json={"message": message, "topic_id": topic_id}
    )
    assert response.status_code == 200, response.text
    return response.json()


@contextmanager
def _capture_turn_submit(ctx):
    """抓取受理路径创建的那一个 TurnContext 与交给 TurnManager 的 basis。

    accept_turn → ctx.turns.submit(...) 是受理的唯一写入口；这里只在测试里加一个
    旁路探针（不改生产代码），用来断言「轮次原话题 / 受理快照」这两个契约事实。
    """
    captured: dict = {}
    original = ctx.turns.submit

    def _spy(message, topic_id=None, **kwargs):  # noqa: ANN001
        turn = original(message, topic_id, **kwargs)
        captured["turn"] = turn
        captured["topic_id"] = topic_id
        captured["basis"] = kwargs.get("basis")
        return turn

    ctx.turns.submit = _spy
    try:
        yield captured
    finally:
        ctx.turns.submit = original


# ===========================================================================
# A1：互动板提交排队 → 手动切 B → 旧轮放行（可观测缺陷）
# ===========================================================================


def test_a1_queued_interactive_submission_keeps_later_navigation(tmp_path: Path) -> None:
    with _backend(tmp_path, gate_calls=(1, 2)) as (client, ctx, adapter, gates):
        topic_a = ctx.topics.nodes.create_topic("互动受理原话题 A").id
        topic_b = ctx.topics.nodes.create_topic("用户后来导航的话题 B").id
        _save_board(client)

        # 1) 先发起一条普通轮并让模型第 1 次调用挂住（正在执行）。
        first = _submit_normal_turn(client, "甲：普通轮占位问题", topic_a)
        assert adapter.started[1].wait(GATE_TIMEOUT), "普通轮没有进入模型调用"
        assert _anchor(client)["topic_id"] == topic_a

        # 2) 普通轮执行期间提交互动板：受理、排队（模型此时不会被调用）。
        with _capture_turn_submit(ctx) as captured:
            response = _submit_board(client)
        assert response.status_code == 200, response.text
        submission = response.json()
        assert submission["status"] == "succeeded", submission
        interactive_turn_id = submission.get("turnId")
        assert interactive_turn_id, submission
        assert interactive_turn_id != first["turn_id"], submission
        turn = captured["turn"]
        assert turn.turn_id == interactive_turn_id
        # 提交—轮绑定与原话题同一事实（受理话题 A）。
        assert turn.board_topic_id == topic_a

        # 3) 受理之后、互动轮执行之前：用户手动导航到 B（真实 HTTP，带 request_id +
        #    base_nav_version）。
        switch = _switch_topic(client, topic_b)
        switch_position_version = int(switch["position_version"])
        assert _anchor(client)["topic_id"] == topic_b
        events_after_switch = len(_anchor_events(ctx))

        # 4) 放行普通轮（模型 1），互动轮随即开始执行（模型 2 挂住）再放行。
        gates[1].set()
        assert adapter.started[2].wait(GATE_TIMEOUT), "互动受理轮没有开始执行"
        gates[2].set()
        _wait_turn_done(ctx.conn, interactive_turn_id)

        # 5) 归属：用户消息与正式回答都在受理话题 A（片段 topic = A）。
        rows = _message_rows(ctx.conn, interactive_turn_id)
        assert [row["role"] for row in rows] == ["user", "assistant"], rows
        for row in rows:
            assert _fragment_topic(ctx.conn, row["fragment_id"]) == topic_a, row
        assert ANSWER_BODY in rows[-1]["content"], rows[-1]

        # 6) 位置：旧轮的轮首补锚被受理依据链 CAS 拒绝，权威位置仍是 B，
        #    position_version 保持切换后的值（不被旧轮推进）。
        position = ctx.position_state.read()
        assert position["topic_id"] == topic_b, position
        assert int(position["position_version"]) == switch_position_version, position
        wire = _anchor(client)
        assert wire["topic_id"] == topic_b, wire
        assert int(wire["position_version"]) == switch_position_version, wire

        # 7) 广播：切换之后不得再出现把 A 广播成当前位置的 ANCHOR（迟到补锚不得广播）。
        late_events = _anchor_events(ctx)[events_after_switch:]
        leaked = [
            event
            for event in late_events
            if (event.get("position") or {}).get("topic_id") == topic_a
            or event.get("topic_id") == topic_a
        ]
        assert leaked == [], leaked

        # 8) 事实可观察：依据链被如实标记 superseded（不是静默通过）。
        assert turn.position_basis_superseded is True


# ===========================================================================
# A1-b：受理那一刻冻结与 /api/turns 完全相同的依据快照（契约 §2.1 冻结接缝）
# ===========================================================================


def test_a1b_acceptance_freezes_shared_submission_basis_snapshot(tmp_path: Path) -> None:
    with _backend(tmp_path) as (client, ctx, adapter, gates):
        del gates
        topic_a = ctx.topics.nodes.create_topic("受理快照话题 A").id
        ctx.fragments.get_or_create_open(topic_a)
        _switch_topic(client, topic_a)
        _save_board(client)
        accepted_position = ctx.position_state.read()

        with _capture_turn_submit(ctx) as captured:
            response = _submit_board(client)
        assert response.status_code == 200, response.text
        submission = response.json()
        assert submission["status"] == "succeeded", submission
        turn_id = submission.get("turnId")
        assert turn_id, submission

        # 受理依据 = 与 server._submission_basis 同一份只读快照（键集冻结，不得新增键）。
        basis = captured.get("basis")
        assert basis == {
            "topic_id": topic_a,
            "fragment_id": accepted_position["fragment_id"],
            "intent_id": None,
            "intent_version": None,
            "nav_version": int(accepted_position["nav_version"]),
            "position_version": int(accepted_position["position_version"]),
        }, basis
        # 轮次原话题与 board_topic_id 取同一份受理快照（不再两次读、两套事实）。
        assert captured["topic_id"] == topic_a
        turn = captured["turn"]
        assert turn.turn_id == turn_id
        assert turn.board_topic_id == topic_a
        assert turn.submit_basis_topic_id == topic_a
        assert turn.submit_basis_fragment_id == accepted_position["fragment_id"]
        assert turn.submit_basis_position_version == int(
            accepted_position["position_version"]
        )
        assert turn.submit_basis_nav_version == int(accepted_position["nav_version"])
        # 互动板提交不是「落实接续」：不消耗待接续选择（intent_id 仍按现行为不传）。
        assert turn.intent_id is None

        # 收口（含轮末位置推进），避免在 backend 关闭时还有轮次在跑。
        _wait_turn_done(ctx.conn, turn_id)


# ===========================================================================
# A3：受理依据读取失败时如实降级（有日志、不伪造快照），不改变既有受理语义
# ===========================================================================


def test_a3_basis_read_failure_degrades_honestly(tmp_path: Path, monkeypatch, caplog) -> None:
    with _backend(tmp_path) as (client, ctx, adapter, gates):
        del gates
        _save_board(client)
        topic_a = ctx.topics.nodes.create_topic("降级路径话题 A3").id
        ctx.fragments.get_or_create_open(topic_a)
        _switch_topic(client, topic_a)

        from agent.services import turn_basis as turn_basis_module

        def _broken(_ctx):  # noqa: ANN001
            raise RuntimeError("position_state.read() unavailable (r18-a test injection)")

        monkeypatch.setattr(turn_basis_module, "submission_basis", _broken)
        with caplog.at_level("WARNING", logger="agent.api.interactive_store"):
            with _capture_turn_submit(ctx) as captured:
                response = _submit_board(client)

        # 读取失败不改变既有受理语义（不静默返 500），但必须留下可核对的日志事实，
        # 且绝不伪造一份「已冻结」的依据快照。
        assert response.status_code == 200, response.text
        submission = response.json()
        assert submission["status"] == "succeeded", submission
        assert submission.get("turnId"), submission
        assert any(
            "互动提交受理依据读取失败" in record.getMessage()
            for record in caplog.records
        ), caplog.text
        assert captured.get("basis") is None, "读取失败时不得伪造依据快照"
        turn = captured["turn"]
        assert turn.submit_basis_topic_id is None
        assert turn.submit_basis_position_version is None
        # 降级轮仍如实走完（旧路径），不会留下永远执行不了的排队轮。
        _wait_turn_done(ctx.conn, submission["turnId"])


# ===========================================================================
# A2：无导航干扰 —— 互动板提交仍在受理话题正常完成，收尾推进行为不变
# ===========================================================================


def test_a2_interactive_submission_without_navigation_completes_in_accepted_topic(
    tmp_path: Path,
) -> None:
    with _backend(tmp_path) as (client, ctx, adapter, gates):
        del gates
        _save_board(client)
        topic_a = ctx.topics.nodes.create_topic("互动受理话题 A2").id
        open_fragment_id = ctx.fragments.get_or_create_open(topic_a).id
        # 用户当前明确停在 A（受理话题），没有后续导航干扰。
        _switch_topic(client, topic_a)
        assert _anchor(client)["topic_id"] == topic_a
        accepted_position = ctx.position_state.read()
        events_before = len(_anchor_events(ctx))

        with _capture_turn_submit(ctx) as captured:
            response = _submit_board(client)
        assert response.status_code == 200, response.text
        submission = response.json()
        assert submission["status"] == "succeeded", submission
        turn_id = submission.get("turnId")
        assert turn_id, submission
        basis = captured.get("basis")
        assert basis is not None, "互动板受理必须冻结依据快照（不能按旧路径临时读当前版本）"
        assert basis["topic_id"] == topic_a, basis
        turn = captured["turn"]
        assert turn.board_topic_id == topic_a

        _wait_turn_done(ctx.conn, turn_id)
        assert adapter.calls == 1, adapter.calls

        # 正常在受理话题 A 完成：两条消息都在 A 的开放片段。
        rows = _message_rows(ctx.conn, turn_id)
        assert [row["role"] for row in rows] == ["user", "assistant"], rows
        for row in rows:
            assert _fragment_topic(ctx.conn, row["fragment_id"]) == topic_a, row
        assert rows[0]["fragment_id"] == open_fragment_id, rows[0]
        assert ANSWER_BODY in rows[-1]["content"], rows[-1]

        # 收尾行为不变：轮末推进到本轮真实片段，position_version 在真实变化时 +1 并广播。
        final = ctx.position_state.read()
        assert final["topic_id"] == topic_a, final
        assert final["fragment_id"] == open_fragment_id, final
        assert int(final["position_version"]) == int(
            accepted_position["position_version"]
        ) + 1, final
        new_events = _anchor_events(ctx)[events_before:]
        assert new_events, "轮末推进必须广播同源 ANCHOR"
        last_event = new_events[-1]
        assert (last_event.get("position") or {}).get("topic_id") == topic_a, last_event
        assert (last_event.get("position") or {}).get("fragment_id") == open_fragment_id, (
            last_event
        )
        assert int((last_event.get("position") or {}).get("position_version")) == int(
            final["position_version"]
        ), last_event
        # 正常路径依据链没有被拒绝。
        assert turn.position_basis_superseded is False