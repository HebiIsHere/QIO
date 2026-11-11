# -*- coding: utf-8 -*-
"""R19-A：首次互动板提交的默认话题归属（P1）。

契约：docs/topic-unify-r19-contract-20261111.md §2.1 / §5 A1—A3。
基线红证据：D:\\qio-dev\\r19-evidence\\a\\baseline-red\\r19_a_baseline_red.log

被测系统：真实 FastAPI create_app + 真实迁移 + 临时 SQLite + TestClient（真实 lifespan）
+ fake provider（可计数、可挂受控闸门；绝不联网）。顺序由 threading 事件闸门控制，
不用 sleep 构造并发时序（_wait_until 只用于等待异步事实收口）。

覆盖：
* A1 无当前位置（无 active Anchor）时提交互动板 → 模型准备（begin 的 adapter_setup）
  被闸门暂停 → 用户手动切到话题 B → 放行：提问与回答都写入默认话题；权威位置保持 B；
  position_version 不推进；没有迟到 ANCHOR 把默认话题广播成当前位置；受理依据快照与
  turn.board_topic_id 在受理那一刻就固定为默认话题。
  修复前（基线）：受理快照 topic_id=None，begin 才用 app.current_topic() 兜底 —— 准备
  期间用户已切到 B，本轮被错误归到 B（本用例在基线上因此失败）。
* A2 无当前位置提交且无导航干扰：正常写入默认话题并补锚（既有行为不变）。
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

BOARD = "board_r19a"
ANSWER_BODY = "R19-A 默认话题归属验收回答"
ANSWER = "[[QIO:ANSWER]]\n" + ANSWER_BODY
GATE_TIMEOUT = 60.0


def _request_id(tag: str) -> str:
    return f"req-r19a-{tag}-{uuid.uuid4().hex}"


def _wait_until(predicate, *, timeout: float = 30.0, interval: float = 0.02) -> bool:
    """有界等待异步事实（不是用 sleep 构造并发顺序：顺序仍由事件闸门决定）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class CountingAdapter:
    """可计数 fake provider：只回同一份正式回答，绝不联网。"""

    mode = "native"
    model = "fake-r19-a"
    supports_stream = False
    protocol_in_prompt = False

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        return Completion(message=ChatMessage(role="assistant", content=ANSWER))


@contextmanager
def _backend(tmp_path: Path, *, gated_build: bool = False):
    conn = connect(tmp_path / "r19_a_default_topic.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path), conn)
    ctx = app.state.ctx
    ctx.credentials._kr = MemoryKeyring()
    adapter = CountingAdapter()
    gate = {
        "started": threading.Event(),
        "release": threading.Event(),
        "calls": 0,
    }

    async def _build_adapter():
        gate["calls"] += 1
        if gated_build and gate["calls"] == 1:
            # 第一次（begin 的 adapter_setup）挂住：话题归属尚未解析。
            gate["started"].set()
            await asyncio.to_thread(gate["release"].wait, GATE_TIMEOUT)
        return adapter

    ctx.build_adapter = _build_adapter  # type: ignore[assignment]

    async def _fake_predict(turn_ctx, message, topic):  # noqa: ANN001
        return TopicPrediction(main_topic_id=topic, backend_used="rules")

    ctx.turn_orchestrator._predict_topics = _fake_predict  # type: ignore[assignment]
    ctx.turn_orchestrator._schedule_derived_work = lambda *a, **k: None  # type: ignore[assignment]
    try:
        with TestClient(app) as client:
            yield client, ctx, adapter, gate
    finally:
        gate["release"].set()
        conn.close()


# ---------------------------------------------------------------------------
# 工具（与 R18-A 既有用例同口径：真实 HTTP + 权威 wire）
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


def _switch_topic(client: TestClient, topic_id: str) -> dict:
    base = int(_anchor(client)["nav_version"])
    response = client.post(
        "/api/anchor",
        json={
            "topic_id": topic_id,
            "fragment_id": None,
            "request_id": _request_id("switch"),
            "base_nav_version": base,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["changed"] is True, body
    return body


def _message_rows(conn, turn_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, role, content, content_type, fragment_id FROM messages"
        " WHERE turn_id = ? ORDER BY created_at, id",
        (turn_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _fragment_topic(conn, fragment_id: str | None) -> str | None:
    if not fragment_id:
        return None
    row = conn.execute(
        "SELECT topic_id FROM fragments WHERE id = ?", (fragment_id,)
    ).fetchone()
    return str(row["topic_id"]) if row is not None else None


def _turn_status(conn, turn_id: str) -> str | None:
    row = conn.execute(
        "SELECT status FROM turn_journal WHERE turn_id = ?", (turn_id,)
    ).fetchone()
    return str(row["status"]) if row is not None else None


def _wait_turn_done(conn, turn_id: str, *, timeout: float = 30.0) -> None:
    assert _wait_until(
        lambda: _turn_status(conn, turn_id) in ("completed", "failed", "cancelled"),
        timeout=timeout,
    ), f"轮次没有收口：status={_turn_status(conn, turn_id)}"
    assert _turn_status(conn, turn_id) == "completed", _turn_status(conn, turn_id)


def _save_board(client: TestClient) -> None:
    created = client.post("/api/interactive/boards", json={"boardId": BOARD})
    assert created.status_code == 200, created.text
    card = models.new_card("text", "R19-A 互动板提交内容", checked=True)
    card["id"] = "note_r19a"
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
        json={"state": state, "reason": "r19_a_test"},
    )
    assert response.status_code == 200, response.text


def _submit_board(client: TestClient):
    return client.post(f"/api/interactive/boards/{BOARD}/submissions", json={})


@contextmanager
def _capture_turn_submit(ctx):
    """抓取受理路径创建的那一个 TurnContext 与交给 TurnManager 的 basis。"""
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


def _assert_no_late_anchor(position_topic: str, events: list[dict]) -> None:
    leaked = [
        event
        for event in events
        if (event.get("position") or {}).get("topic_id") == position_topic
        or event.get("topic_id") == position_topic
    ]
    assert leaked == [], leaked


# ===========================================================================
# A1：无当前位置提交互动板 → 暂停模型准备 → 手动切 B → 放行
# ===========================================================================


def test_a1_first_board_submission_without_position_keeps_default_topic(
    tmp_path: Path,
) -> None:
    with _backend(tmp_path, gated_build=True) as (client, ctx, adapter, gate):
        # 初始没有任何 active Anchor（当前位置为空）。
        assert ctx.position_state.read()["topic_id"] is None
        # 按既有兜底语义拿默认话题（无话题时创建；只读解析，不写位置）。
        default_topic = ctx.current_topic()
        assert default_topic
        assert ctx.position_state.read()["topic_id"] is None
        _save_board(client)
        topic_b = ctx.topics.nodes.create_topic("用户准备期间导航到的话题 B").id
        assert int(_anchor(client)["nav_version"]) == 0

        with _capture_turn_submit(ctx) as captured:
            submitted = _submit_board(client)
        assert submitted.status_code == 200, submitted.text
        body = submitted.json()
        assert body["status"] == "succeeded", body
        turn_id = body["turnId"]
        assert turn_id, body

        # begin 的模型准备被闸门挂住：此刻话题归属还没有解析。
        assert gate["started"].wait(GATE_TIMEOUT), "模型准备没有被闸门挂住"

        # 用户准备期间手动导航到 B（真实 HTTP，带 request_id + base_nav_version）。
        switch = _switch_topic(client, topic_b)
        switch_position_version = int(switch["position_version"])
        assert _anchor(client)["topic_id"] == topic_b
        events_after_switch = len(_anchor_events(ctx))

        gate["release"].set()
        _wait_turn_done(ctx.conn, turn_id)

        # 归属：互动提交轮的用户消息与正式回答都在默认话题。
        rows = _message_rows(ctx.conn, turn_id)
        assert [row["role"] for row in rows] == ["user", "assistant"], rows
        assert rows[0]["content_type"] == "board_submission", rows[0]
        for row in rows:
            assert _fragment_topic(ctx.conn, row["fragment_id"]) == default_topic, (
                row,
                default_topic,
            )
        assert ANSWER_BODY in rows[-1]["content"], rows[-1]
        assert adapter.calls == 1, adapter.calls

        # 位置：只读解析默认话题 / 旧轮补锚都不得把权威位置从 B 改回默认话题，
        # position_version 保持切换后的值（不被旧轮推进）。
        position = ctx.position_state.read()
        assert position["topic_id"] == topic_b, position
        assert int(position["position_version"]) == switch_position_version, position
        wire = _anchor(client)
        assert wire["topic_id"] == topic_b, wire
        assert int(wire["position_version"]) == switch_position_version, wire

        # 广播：切换之后不得再出现把默认话题广播成当前位置的迟到 ANCHOR。
        _assert_no_late_anchor(default_topic, _anchor_events(ctx)[events_after_switch:])

        # 受理那一刻冻结的事实：basis 与 turn 的 topic 都是默认话题（不是事后兜底）。
        basis = captured.get("basis")
        assert basis is not None, "互动板受理必须冻结依据快照"
        assert basis["topic_id"] == default_topic, basis
        turn = captured["turn"]
        assert turn.turn_id == turn_id
        assert captured["topic_id"] == default_topic
        assert turn.board_topic_id == default_topic
        assert turn.submit_basis_topic_id == default_topic


# ===========================================================================
# A2：无当前位置提交、无导航干扰 —— 正常写入默认话题并补锚（既有行为）
# ===========================================================================


def test_a2_first_board_submission_without_navigation_completes_in_default_topic(
    tmp_path: Path,
) -> None:
    with _backend(tmp_path) as (client, ctx, adapter, gate):
        del gate
        assert ctx.position_state.read()["topic_id"] is None
        default_topic = ctx.current_topic()
        assert default_topic
        _save_board(client)

        with _capture_turn_submit(ctx) as captured:
            submitted = _submit_board(client)
        assert submitted.status_code == 200, submitted.text
        body = submitted.json()
        assert body["status"] == "succeeded", body
        turn_id = body["turnId"]
        assert turn_id, body
        _wait_turn_done(ctx.conn, turn_id)

        # 问题/回答正常写入默认话题。
        rows = _message_rows(ctx.conn, turn_id)
        assert [row["role"] for row in rows] == ["user", "assistant"], rows
        for row in rows:
            assert _fragment_topic(ctx.conn, row["fragment_id"]) == default_topic, (
                row,
                default_topic,
            )
        assert ANSWER_BODY in rows[-1]["content"], rows[-1]
        assert adapter.calls == 1, adapter.calls

        # 既有补锚行为不变：轮首/轮末把权威位置落到默认话题（不要求具体推进次数）。
        position = ctx.position_state.read()
        assert position["topic_id"] == default_topic, position
        wire = _anchor(client)
        assert wire["topic_id"] == default_topic, wire

        basis = captured.get("basis")
        assert basis is not None, "互动板受理必须冻结依据快照"
        assert basis["topic_id"] == default_topic, basis
        turn = captured["turn"]
        assert turn.board_topic_id == default_topic
