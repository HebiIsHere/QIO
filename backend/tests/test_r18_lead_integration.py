# -*- coding: utf-8 -*-
"""R18 · Lead 集成验收：互动板提交的受理依据（P1）+ 普通回答保存失败的暂存/恢复记录（P2）。

被测系统：真实 FastAPI create_app + 真实迁移 + 临时 SQLite + TestClient（真实 lifespan）+
fake provider（可计数、可挂受控闸门；绝不联网）。断言后端权威 wire（/api/runtime/state、
position_state.read、ANCHOR 事件、messages 归属、topic_answer_runs/topic_recovery_records）
与真实 HTTP 行为，不依赖 A/B 的内部函数名。

覆盖用户任务书验收：
* P1：A（受理话题）提交互动板内容并被排队 → 手动切到 B → 放行旧轮：问题/回答归属 A，
  当前位置保持 B，旧轮不推进 position_version，也不广播 A 为当前位置。
* P2：普通回答生成成功但保存失败（真实 HTTP + fake 模型注入一次写失败）→ 解除故障后
  从现有 retry-answer 入口重试：只补存一条正式回答、模型调用次数不增加、原问题不重复写入、
  恢复记录正确结束；重复重试返回同一份正式回答。
"""
from __future__ import annotations

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
from agent.services import topic_recovery
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

ANSWER_BODY = "[[QIO:ANSWER]]\nR18 集成验收回答"
BOARD_ID = "board_r18_lead"


def _request_id(tag: str) -> str:
    return f"req-r18-lead-{tag}-{uuid.uuid4().hex}"


def _wait_until(predicate, *, timeout: float = 45.0, interval: float = 0.02) -> bool:
    """有界等待异步事实（不是用 sleep 构造并发顺序：顺序仍由事件闸门决定）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class GatedAdapter:
    """第 N 次调用可挂起，直到测试放行；记录调用次数。"""

    mode = "native"
    model = "fake-r18-lead"
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
            await __import__("asyncio").to_thread(gate.wait, 90.0)
        return Completion(message=ChatMessage(role="assistant", content=ANSWER_BODY))


@contextmanager
def _backend(tmp_path: Path, gate_calls: tuple[int, ...] = ()):
    conn = connect(tmp_path / "r18_lead_integration.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path), conn)
    ctx = app.state.ctx
    ctx.credentials._kr = MemoryKeyring()
    gates = {index: threading.Event() for index in gate_calls}
    adapter = GatedAdapter(gates)

    async def _build_adapter():
        return adapter

    ctx.build_adapter = _build_adapter  # type: ignore[assignment]
    # 预测/派生的真实模型依赖去掉：本文件只验证位置、归属与恢复接线（确定性、不联网）。
    from agent.services.predict import TopicPrediction

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


def _anchor(client: TestClient) -> dict:
    return client.get("/api/runtime/state").json()["anchor"]


def _anchor_events(ctx) -> list[dict]:
    return [
        dict(event.data or {})
        for event in list(ctx.bus._history)  # noqa: SLF001 - 与既有测试同口径读总线历史
        if getattr(event, "type", None) == EventType.ANCHOR
    ]


def _submit_turn(client: TestClient, message: str, topic_id: str) -> dict:
    response = client.post("/api/turns", json={"message": message, "topic_id": topic_id})
    assert response.status_code == 200, response.text
    return response.json()


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


def _messages(conn, turn_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, role, content, content_type, fragment_id, answer_key FROM messages "
        "WHERE turn_id = ? ORDER BY created_at, id",
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


def _create_board_with_note(client: TestClient) -> None:
    created = client.post("/api/interactive/boards", json={"boardId": BOARD_ID})
    assert created.status_code == 200, created.text
    card = models.new_card("text", "互动板验收说明", checked=True)
    state = {
        "boardId": BOARD_ID,
        "seq": 0,
        "updatedAt": models.now_iso(),
        "cards": [card],
        "groups": [],
        "links": [],
        "selection": [],
    }
    saved = client.put(
        f"/api/interactive/boards/{BOARD_ID}/state", json={"state": state}
    )
    assert saved.status_code == 200, saved.text


def _recovery_records(client: TestClient) -> list[dict]:
    response = client.get("/api/recovery/records")
    if response.status_code != 200:
        return []
    return list(response.json().get("records") or [])


def _run_row(conn, answer_key: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM topic_answer_runs WHERE answer_key = ?", (answer_key,)
    ).fetchone()
    return dict(row) if row is not None else None


def _diag(ctx, adapter, client) -> str:
    try:
        journal = [dict(row) for row in ctx.conn.execute(
            "SELECT turn_id, status, reason FROM turn_journal ORDER BY rowid DESC LIMIT 6"
        )]
    except Exception as exc:  # noqa: BLE001
        journal = [repr(exc)]
    try:
        snapshot = ctx.turns.snapshot()
    except Exception as exc:  # noqa: BLE001
        snapshot = repr(exc)
    return "DIAG calls=%r journal=%r snapshot=%r anchor=%r" % (
        getattr(adapter, "calls", None), journal, snapshot, _anchor(client)
    )


# ── P1：互动板提交排队 → 手动切 B → 旧轮放行，不覆盖新位置 ──────────────


def test_r18_p1_interactive_submission_queued_does_not_override_later_navigation(
    tmp_path: Path,
) -> None:
    with _backend(tmp_path, gate_calls=(1, 2)) as (client, ctx, adapter, gates):
        topic_a = ctx.topics.nodes.create_topic("R18 A 受理话题").id
        # 第一条普通轮开始执行（模型第 1 次调用被闸门挂住）——互动提交在它后面排队。
        first = _submit_turn(client, "甲：先占住执行槽的问题", topic_a)
        assert adapter.started[1].wait(90.0), _diag(ctx, adapter, client)
        assert _anchor(client)["topic_id"] == topic_a, _anchor(client)

        _create_board_with_note(client)
        submitted = client.post(
            f"/api/interactive/boards/{BOARD_ID}/submissions", json={}
        )
        assert submitted.status_code == 200, submitted.text
        submission_body = submitted.json()
        assert submission_body["status"] == "succeeded", submission_body
        board_turn_id = submission_body["turnId"]
        assert board_turn_id and board_turn_id != first["turn_id"], submission_body
        assert submission_body["delivery"]["turnId"] == board_turn_id

        # 用户手动导航到话题 B（真实 HTTP，带 request_id + base_nav_version）。
        topic_b = ctx.topics.nodes.create_topic("R18 B 新导航话题").id
        switch_body = _switch_topic(client, topic_b)
        assert switch_body["position"]["topic_id"] == topic_b
        switch_position_version = int(switch_body["position_version"])
        events_before_switch = len(_anchor_events(ctx))

        # 依次放行两条轮次，等互动提交轮真正完成。
        gates[1].set()
        assert adapter.started[2].wait(90.0), _diag(ctx, adapter, client)
        gates[2].set()
        assert _wait_until(
            lambda: len(
                [row for row in _messages(ctx.conn, board_turn_id) if row["role"] == "assistant"]
            )
            == 1,
            timeout=45.0,
        ), "互动提交轮没有完成正式回答：" + _diag(ctx, adapter, client)

        # 归属：互动提交轮的用户消息与正式回答都在受理话题 A。
        rows = _messages(ctx.conn, board_turn_id)
        user_rows = [row for row in rows if row["role"] == "user"]
        assistant_rows = [row for row in rows if row["role"] == "assistant"]
        assert len(user_rows) == 1 and len(assistant_rows) == 1, rows
        assert user_rows[0]["content_type"] == "board_submission", user_rows[0]
        for row in rows:
            assert _fragment_topic(ctx.conn, row["fragment_id"]) == topic_a, (row, topic_a)

        # 位置：旧轮轮首补锚 / 轮末推进都不得把位置从 B 改回 A。
        position = ctx.position_state.read()
        assert position["topic_id"] == topic_b, position
        wire = _anchor(client)
        assert wire["topic_id"] == topic_b, wire
        assert int(wire["position_version"]) == switch_position_version, wire
        # 切换之后不再出现把 A 广播成当前位置的事件。
        late_events = _anchor_events(ctx)[events_before_switch:]
        for event in late_events:
            event_position = event.get("position") or {}
            assert event_position.get("topic_id") != topic_a, event


# ── P2：普通回答保存失败 → 现有入口重试只补存，不重复调模型/写问题 ────────


def test_r18_p2_normal_answer_save_failure_recovered_via_retry_answer(tmp_path, monkeypatch) -> None:
    with _backend(tmp_path) as (client, ctx, adapter, _gates):
        topic_a = ctx.topics.nodes.create_topic("R18 P2 话题").id
        real_insert = topic_recovery._insert_normal_answer_message
        attempts = {"n": 0}

        def _flaky_insert(conn, **kwargs):  # noqa: ANN001, ANN003
            attempts["n"] += 1
            raise RuntimeError("注入：普通回答保存失败（Lead 集成验收）")

        monkeypatch.setattr(topic_recovery, "_insert_normal_answer_message", _flaky_insert)
        turn = _submit_turn(client, "乙：普通回答保存失败后重试的问题", topic_a)
        turn_id = turn["turn_id"]

        assert _wait_until(
            lambda: any(
                record.get("turn_id") == turn_id for record in _recovery_records(client)
            ),
            timeout=45.0,
        ), "保存失败后没有出现待恢复记录：" + _diag(ctx, adapter, client)
        assert _wait_until(
            lambda: not (ctx.turns.snapshot().get("active") or []),
            timeout=45.0,
        ), "保存失败的轮次没有收口：" + _diag(ctx, adapter, client)
        assert attempts["n"] == 1 and adapter.calls == 1, (attempts, adapter.calls)

        rows_after_failure = _messages(ctx.conn, turn_id)
        user_rows = [row for row in rows_after_failure if row["role"] == "user"]
        assistant_rows = [row for row in rows_after_failure if row["role"] == "assistant"]
        assert len(user_rows) == 1, rows_after_failure
        assert assistant_rows == [], "保存失败不得留下半截正式回答"
        answer_key = topic_recovery.answer_key_for(
            turn_id, user_message_id=user_rows[0]["id"]
        )
        run = _run_row(ctx.conn, answer_key)
        assert run is not None, "保存失败必须建立共享暂存（topic_answer_runs 行）"
        assert run.get("phase") == "staged" and run.get("status") == "pending", run
        assert run.get("staged_answer"), run

        # 解除故障：恢复真实保存函数后，从**现有**「重试回答」入口补存。
        monkeypatch.setattr(topic_recovery, "_insert_normal_answer_message", real_insert)
        record = next(
            record for record in _recovery_records(client) if record.get("turn_id") == turn_id
        )
        record_id = record["record_id"]
        assert record.get("state_class") == "answer_pending", record
        assert record.get("failed_stage") == "answer", record

        retried = client.post(f"/api/recovery/records/{record_id}/retry-answer")
        assert retried.status_code == 200, retried.text
        result = retried.json()
        assert result.get("ok") is True and result.get("answered") is True, result
        answer_id = result["answer_id"]

        rows = _messages(ctx.conn, turn_id)
        user_rows = [row for row in rows if row["role"] == "user"]
        assistant_rows = [row for row in rows if row["role"] == "assistant"]
        assert len(user_rows) == 1, "原问题不得重复写入：" + repr(rows)
        assert len(assistant_rows) == 1, "只能补存一条正式回答：" + repr(rows)
        assert assistant_rows[0]["id"] == answer_id
        assert "R18 集成验收回答" in assistant_rows[0]["content"]
        assert assistant_rows[0]["answer_key"] == answer_key
        assert adapter.calls == 1, "补存不得再调用模型"

        stored = client.get(f"/api/recovery/records/{record_id}").json()
        assert stored.get("status") == "answered", stored
        assert stored.get("answer_id") == answer_id, stored
        run = _run_row(ctx.conn, answer_key)
        assert run.get("status") == "answered" and run.get("phase") == "answered", run
        assert run.get("staged_answer") == "" and run.get("staged_at") is None, run

        # 重复重试：返回同一份正式回答，不新增消息、不新增模型调用。
        again = client.post(f"/api/recovery/records/{record_id}/retry-answer")
        assert again.status_code == 200, again.text
        assert again.json().get("answer_id") == answer_id, again.json()
        rows = _messages(ctx.conn, turn_id)
        assert len([row for row in rows if row["role"] == "assistant"]) == 1
        assert adapter.calls == 1