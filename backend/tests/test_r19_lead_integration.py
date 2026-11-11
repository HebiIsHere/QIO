# -*- coding: utf-8 -*-
"""R19 · Lead 集成验收：首次互动板提交的默认话题归属（P1）+ 保存失败后的恢复入口通知（P2）。

被测系统：真实 FastAPI create_app + 真实迁移 + 临时 SQLite + TestClient（真实 lifespan）+
fake provider（可计数、可挂受控闸门；绝不联网）。只断言 HTTP 行为、权威位置 wire、消息归属、
事件总线历史与恢复记录表；不依赖 A/B 内部实现细节。

覆盖用户任务书验收：
* P1：没有当前位置时提交互动板，暂停模型准备，手动切到 B，再放行：提问和回答都写入默认话题，
  当前位置保持 B，旧轮不推进 position_version，也不发布把当前位置改回默认话题的事件。
* P2：普通回答保存失败（真实 HTTP + fake 模型注入一次写失败）且暂存/记录提交成功 → 后端发出
  RECOVERY_INBOX_CHANGED（record_id 指向真实 pending 记录）→ 解除故障后从既有 retry-answer 入口
  补存：只一条正式回答、模型调用次数不增、原问题不重复、记录 answered；重复重试同一 answer_id。
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
from agent.services import topic_recovery
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

ANSWER_BODY = "[[QIO:ANSWER]]\nR19 集成验收回答"
BOARD_ID = "board_r19_lead"


def _request_id(tag: str) -> str:
    return f"req-r19-lead-{tag}-{uuid.uuid4().hex}"


def _wait_until(predicate, *, timeout: float = 45.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class FakeAdapter:
    mode = "native"
    model = "fake-r19-lead"
    supports_stream = False
    protocol_in_prompt = False

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        return Completion(message=ChatMessage(role="assistant", content=ANSWER_BODY))


@contextmanager
def _backend(tmp_path: Path, *, gate_first_adapter_build: bool = False):
    conn = connect(tmp_path / "r19_lead_integration.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path), conn)
    ctx = app.state.ctx
    ctx.credentials._kr = MemoryKeyring()
    adapter = FakeAdapter()
    build_entered = threading.Event()
    build_release = threading.Event()
    if not gate_first_adapter_build:
        build_release.set()
    build_calls = {"n": 0}

    async def _build_adapter():
        build_calls["n"] += 1
        if build_calls["n"] == 1 and gate_first_adapter_build:
            build_entered.set()
            await asyncio.to_thread(build_release.wait, 90.0)
        return adapter

    ctx.build_adapter = _build_adapter  # type: ignore[assignment]
    from agent.services.predict import TopicPrediction

    async def _fake_predict(turn_ctx, message, topic):  # noqa: ANN001
        return TopicPrediction(main_topic_id=topic, backend_used="rules")

    ctx.turn_orchestrator._predict_topics = _fake_predict  # type: ignore[assignment]
    ctx.turn_orchestrator._schedule_derived_work = lambda *a, **k: None  # type: ignore[assignment]
    try:
        with TestClient(app) as client:
            yield client, ctx, adapter, (build_entered, build_release)
    finally:
        build_release.set()
        conn.close()


def _anchor(client: TestClient) -> dict:
    return client.get("/api/runtime/state").json()["anchor"]


def _anchor_events(ctx) -> list[dict]:
    return [
        dict(event.data or {})
        for event in list(ctx.bus._history)  # noqa: SLF001 - 与既有测试同口径读总线历史
        if getattr(event, "type", None) == EventType.ANCHOR
    ]


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
    card = models.new_card("text", "R19 互动板验收说明", checked=True)
    state = {
        "boardId": BOARD_ID,
        "seq": 0,
        "updatedAt": models.now_iso(),
        "cards": [card],
        "groups": [],
        "links": [],
        "selection": [],
    }
    saved = client.put(f"/api/interactive/boards/{BOARD_ID}/state", json={"state": state})
    assert saved.status_code == 200, saved.text


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


# ── P1：无当前位置时互动板提交归默认话题，不覆盖后来导航 ─────────────────


def test_r19_p1_first_interactive_submission_uses_default_topic(tmp_path: Path) -> None:
    with _backend(tmp_path, gate_first_adapter_build=True) as (client, ctx, adapter, gates):
        build_entered, build_release = gates
        # 先确认：没有任何当前位置。
        assert ctx.position_state.read()["topic_id"] is None

        _create_board_with_note(client)
        submitted = client.post(
            f"/api/interactive/boards/{BOARD_ID}/submissions", json={}
        )
        assert submitted.status_code == 200, submitted.text
        body = submitted.json()
        assert body["status"] == "succeeded", body
        board_turn_id = body["turnId"]
        assert board_turn_id, body

        # 受理时按现有规则解析出的默认话题（此时还没有第二个话题）。
        topics = list(ctx.topics.nodes.list_topics())
        assert len(topics) == 1, [topic.id for topic in topics]
        default_topic_id = topics[0].id

        # 模型准备被暂停：用户手动导航到话题 B。
        assert build_entered.wait(90.0), "模型准备没有开始（无法暂停）"
        topic_b = ctx.topics.nodes.create_topic("R19 B 新导航话题").id
        switch_body = _switch_topic(client, topic_b)
        assert switch_body["position"]["topic_id"] == topic_b
        switch_position_version = int(switch_body["position_version"])
        events_before_switch = len(_anchor_events(ctx))
        build_release.set()

        assert _wait_until(
            lambda: len(
                [row for row in _messages(ctx.conn, board_turn_id) if row["role"] == "assistant"]
            )
            == 1,
            timeout=45.0,
        ), "互动提交轮没有完成正式回答"

        rows = _messages(ctx.conn, board_turn_id)
        user_rows = [row for row in rows if row["role"] == "user"]
        assistant_rows = [row for row in rows if row["role"] == "assistant"]
        assert len(user_rows) == 1 and len(assistant_rows) == 1, rows
        for row in rows:
            assert _fragment_topic(ctx.conn, row["fragment_id"]) == default_topic_id, (
                row,
                default_topic_id,
            )

        # 位置：旧轮不覆盖用户后来的 B。
        position = ctx.position_state.read()
        assert position["topic_id"] == topic_b, position
        wire = _anchor(client)
        assert wire["topic_id"] == topic_b, wire
        assert int(wire["position_version"]) == switch_position_version, wire
        late_events = _anchor_events(ctx)[events_before_switch:]
        for event in late_events:
            event_position = event.get("position") or {}
            assert event_position.get("topic_id") != default_topic_id, event


def test_r19_p1_first_interactive_submission_without_navigation_completes(tmp_path: Path) -> None:
    with _backend(tmp_path) as (client, ctx, adapter, _gates):
        assert ctx.position_state.read()["topic_id"] is None
        _create_board_with_note(client)
        submitted = client.post(
            f"/api/interactive/boards/{BOARD_ID}/submissions", json={}
        )
        assert submitted.status_code == 200, submitted.text
        body = submitted.json()
        assert body["status"] == "succeeded", body
        board_turn_id = body["turnId"]
        assert _wait_until(
            lambda: len(
                [row for row in _messages(ctx.conn, board_turn_id) if row["role"] == "assistant"]
            )
            == 1,
            timeout=45.0,
        ), "互动提交轮没有完成正式回答"
        topics = list(ctx.topics.nodes.list_topics())
        assert len(topics) == 1, [topic.id for topic in topics]
        default_topic_id = topics[0].id
        rows = _messages(ctx.conn, board_turn_id)
        assert {row["role"] for row in rows} == {"user", "assistant"}, rows
        for row in rows:
            assert _fragment_topic(ctx.conn, row["fragment_id"]) == default_topic_id, row
        # 正常路径仍会补锚到默认话题（既有行为）。
        assert _anchor(client)["topic_id"] == default_topic_id


# ── P2：保存失败提交成功后通知前端，既有入口重试只补存 ───────────────────


def test_r19_p2_save_failure_emits_recovery_inbox_changed(tmp_path, monkeypatch) -> None:
    with _backend(tmp_path) as (client, ctx, adapter, _gates):
        topic_a = ctx.topics.nodes.create_topic("R19 P2 话题").id
        real_insert = topic_recovery._insert_normal_answer_message
        attempts = {"n": 0}

        def _flaky_insert(conn, **kwargs):  # noqa: ANN001, ANN003
            attempts["n"] += 1
            raise RuntimeError("注入：普通回答保存失败（R19 Lead 集成验收）")

        monkeypatch.setattr(topic_recovery, "_insert_normal_answer_message", _flaky_insert)
        response = client.post(
            "/api/turns", json={"message": "乙：R19 保存失败后恢复入口的问题", "topic_id": topic_a}
        )
        assert response.status_code == 200, response.text
        turn_id = response.json()["turn_id"]

        assert _wait_until(
            lambda: any(record.get("turn_id") == turn_id for record in _recovery_records(client)),
            timeout=45.0,
        ), "保存失败后没有出现待恢复记录：" + repr(_recovery_records(client))
        assert _wait_until(
            lambda: not (ctx.turns.snapshot().get("active") or []), timeout=45.0
        ), "保存失败的轮次没有收口"
        assert attempts["n"] == 1 and adapter.calls == 1, (attempts, adapter.calls)

        # 后端通知：事务提交后发出的既有事件（新类型），record_id 指向真实 pending 记录。
        notify_events = [
            event
            for event in list(ctx.bus._history)  # noqa: SLF001
            if getattr(event, "type", None) == EventType.RECOVERY_INBOX_CHANGED
        ]
        assert notify_events, "保存失败提交成功后没有发出 RECOVERY_INBOX_CHANGED"
        notify = dict(notify_events[-1].data or {})
        rows_after_failure = _messages(ctx.conn, turn_id)
        user_rows = [row for row in rows_after_failure if row["role"] == "user"]
        assert len(user_rows) == 1
        answer_key = topic_recovery.answer_key_for(
            turn_id, user_message_id=user_rows[0]["id"]
        )
        pending = ctx.conn.execute(
            "SELECT record_id, status FROM topic_recovery_records WHERE answer_key = ?",
            (answer_key,),
        ).fetchall()
        assert pending, "事件发出时恢复记录必须已经提交"
        assert notify.get("record_id") == str(pending[0]["record_id"]), (notify, dict(pending[0]))
        assert notify.get("turn_id") == turn_id, notify
        assert notify.get("failed_stage") == "answer", notify
        assert notify.get("action") == "retry_answer", notify

        monkeypatch.setattr(topic_recovery, "_insert_normal_answer_message", real_insert)
        record_id = str(pending[0]["record_id"])
        retried = client.post(f"/api/recovery/records/{record_id}/retry-answer")
        assert retried.status_code == 200, retried.text
        result = retried.json()
        assert result.get("ok") is True and result.get("answered") is True, result
        answer_id = result["answer_id"]
        rows = _messages(ctx.conn, turn_id)
        assert len([row for row in rows if row["role"] == "user"]) == 1
        assistant_rows = [row for row in rows if row["role"] == "assistant"]
        assert len(assistant_rows) == 1 and assistant_rows[0]["id"] == answer_id, rows
        assert adapter.calls == 1, "补存不得再调用模型"
        stored = client.get(f"/api/recovery/records/{record_id}").json()
        assert stored.get("status") == "answered" and stored.get("answer_id") == answer_id, stored
        run = _run_row(ctx.conn, answer_key)
        assert run.get("status") == "answered" and run.get("phase") == "answered", run
        assert run.get("staged_answer") == "" and run.get("staged_at") is None, run
        again = client.post(f"/api/recovery/records/{record_id}/retry-answer")
        assert again.status_code == 200, again.text
        assert again.json().get("answer_id") == answer_id
        assert adapter.calls == 1
