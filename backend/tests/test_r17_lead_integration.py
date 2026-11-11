# -*- coding: utf-8 -*-
"""R17 · Lead 集成验收：旧排队轮次不得覆盖用户后来的新导航（P1-1）+ 普通回答统一身份（P2-4 接线）。

被测系统：真实 FastAPI create_app + 真实迁移 + 临时 SQLite + TestClient（真实 lifespan）+
fake provider（可计数、可挂受控闸门；绝不联网）。前端位置接受由 C/F 的 store 用例覆盖；
本文件断言后端权威 wire（runtime/state anchor、position_state.read、ANCHOR 事件）与消息归属。

覆盖用户任务书验收：
* A 提交排队 → 手动切 B → A 执行并完成：原问题/回答归属 A；前后端当前位置（权威 wire）仍为 B；
* 轮次结束前发生新导航：轮末不覆盖 B；回答仍归属 A；
* 普通正式回答接入统一 answer_key（接线级，依赖 A+D 合并后的 persist 调用）。
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
from agent.services import topic_recovery
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

ANSWER_BODY = "[[QIO:ANSWER]]\nlead 集成验收回答"


def _request_id(tag: str) -> str:
    return f"req-r17-lead-{tag}-{uuid.uuid4().hex}"


def _wait_until(predicate, *, timeout: float = 30.0, interval: float = 0.02) -> bool:
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
    model = "fake-r17-lead"
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
            await asyncio.to_thread(gate.wait, 60.0)
        return Completion(message=ChatMessage(role="assistant", content=ANSWER_BODY))


@contextmanager
def _backend(tmp_path: Path, gate_calls: tuple[int, ...] = ()):
    conn = connect(tmp_path / "r17_lead_integration.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path), conn)
    ctx = app.state.ctx
    ctx.credentials._kr = MemoryKeyring()
    gates = {index: threading.Event() for index in gate_calls}
    adapter = GatedAdapter(gates)

    async def _build_adapter():
        return adapter

    ctx.build_adapter = _build_adapter  # type: ignore[assignment]
    # 预测/派生的真实模型依赖去掉：本文件只验证位置与归属（确定性、不联网、不加载嵌入模型）。
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


def _diag(ctx, adapter, client) -> str:
    try:
        journal = [dict(row) for row in ctx.conn.execute(
            "SELECT turn_id, status, reason FROM turn_journal ORDER BY rowid DESC LIMIT 5"
        )]
    except Exception as exc:  # noqa: BLE001
        journal = [repr(exc)]
    try:
        snapshot = ctx.turns.snapshot()
    except Exception as exc:  # noqa: BLE001
        snapshot = repr(exc)
    return (
        "DIAG calls=%r journal=%r snapshot=%r anchor=%r"
        % (getattr(adapter, "calls", None), journal, snapshot, _anchor(client))
    )


def _wait_gate(event, *, timeout: float = 60.0, **diag) -> bool:
    ok = event.wait(timeout)
    if not ok:
        print("GATE TIMEOUT", _diag(**diag), flush=True)
    return ok


def _anchor(client: TestClient) -> dict:
    return client.get("/api/runtime/state").json()["anchor"]


def _anchor_events(ctx) -> list[dict]:
    return [
        dict(event.data or {})
        for event in list(ctx.bus._history)  # noqa: SLF001 - 与既有测试同口径读总线历史
        if getattr(event, "type", None) == EventType.ANCHOR
    ]


def _submit(client: TestClient, message: str, topic_id: str | None = None) -> dict:
    body: dict = {"message": message}
    if topic_id is not None:
        body["topic_id"] = topic_id
    response = client.post("/api/turns", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _message_rows(conn, turn_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, role, content, fragment_id, answer_key FROM messages "
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


def test_queued_turn_does_not_override_later_manual_navigation(tmp_path: Path) -> None:
    """A 提交排队 → 手动切 B → A 执行并完成：归属 A、位置 B。"""
    with _backend(tmp_path, gate_calls=(1, 2)) as (client, ctx, adapter, gates):
        topic_a = ctx.topics.nodes.create_topic("基线话题 A").id
        # 第一条用户消息在 A 开始执行（模型第 1 次调用被闸门挂住）。
        first = _submit(client, "甲：第一轮问题", topic_id=topic_a)
        assert _wait_gate(adapter.started[1], ctx=ctx, adapter=adapter, client=client), _diag(ctx, adapter, client)
        assert _anchor(client)["topic_id"] == topic_a, _anchor(client)
        topic_b = ctx.topics.nodes.create_topic("新导航话题 B").id
        # 第一条还在跑时再提交第二条：它已受理、按当时的 A 归属排队。
        second = _submit(client, "乙：排队问题", topic_id=topic_a)
        assert second.get("status") in ("queued", "accepted"), second
        assert first["turn_id"] != second["turn_id"]
        # 用户手动导航到 B（真实 HTTP，带 request_id + base_nav_version）。
        switch_body = _switch_topic(client, topic_b)
        assert switch_body["position"]["topic_id"] == topic_b
        switch_position_version = int(switch_body["position_version"])
        events_before_switch = len(_anchor_events(ctx))
        # 依次放行两条轮次的模型调用，等两条都真正收口。
        gates[1].set()
        assert adapter.started[2].wait(60.0), "排队轮没有开始执行（或没有进入模型调用）"
        gates[2].set()
        assert _wait_until(
            lambda: len([r for r in _message_rows(ctx.conn, second["turn_id"]) if r["role"] == "assistant"]) == 1,
            timeout=30.0,
        ), "排队轮没有完成正式回答"

        # 归属：用户消息与正式回答都在 A（受理时的归属），不在 B。
        for turn in (first, second):
            rows = _message_rows(ctx.conn, turn["turn_id"])
            user_rows = [r for r in rows if r["role"] == "user"]
            assistant_rows = [r for r in rows if r["role"] == "assistant"]
            assert len(user_rows) == 1, rows
            assert len(assistant_rows) == 1, rows
            for row in rows:
                assert _fragment_topic(ctx.conn, row["fragment_id"]) == topic_a, (
                    turn,
                    row,
                )
        # 位置：旧轮的轮首补锚 / 轮末收尾都不能把位置改回 A；权威 wire 仍为 B。
        position = ctx.position_state.read()
        assert position["topic_id"] == topic_b, position
        wire = _anchor(client)
        assert wire["topic_id"] == topic_b, wire
        assert int(wire["position_version"]) == switch_position_version, wire
        # 切换之后不再出现把 A 广播成当前位置的事件（旧轮迟到写入不得重新广播成功）。
        late_events = _anchor_events(ctx)[events_before_switch:]
        for event in late_events:
            event_position = event.get("position") or {}
            assert event_position.get("topic_id") != topic_a, event
        # P2-4 接线：普通正式回答带统一 answer_key（同轮 + 原用户消息）。
        for turn in (first, second):
            rows = _message_rows(ctx.conn, turn["turn_id"])
            assistant = next(row for row in rows if row["role"] == "assistant")
            user_message = next(row for row in rows if row["role"] == "user")
            expected = topic_recovery.answer_key_for(
                turn["turn_id"], user_message_id=user_message["id"]
            )
            assert assistant["answer_key"] == expected, (assistant, expected)


def test_navigation_before_turn_end_keeps_new_position(tmp_path: Path) -> None:
    """轮次结束前发生新导航：轮末不覆盖 B；回答仍按原归属写回 A。"""
    with _backend(tmp_path, gate_calls=(1, 2)) as (client, ctx, adapter, gates):
        topic_a = ctx.topics.nodes.create_topic("基线话题 A").id
        first = _submit(client, "甲：第一轮问题", topic_id=topic_a)
        assert adapter.started[1].wait(60.0)
        gates[1].set()
        topic_b = ctx.topics.nodes.create_topic("新导航话题 B").id
        second = _submit(client, "乙：第一轮还没结束就改导航", topic_id=topic_a)
        assert adapter.started[2].wait(60.0), "第二条消息没有开始执行"
        # 第二条消息已开始执行（模型挂住）时，用户导航到 B。
        switch_body = _switch_topic(client, topic_b)
        switch_position_version = int(switch_body["position_version"])
        events_before_switch = len(_anchor_events(ctx))
        gates[2].set()
        assert _wait_until(
            lambda: len([r for r in _message_rows(ctx.conn, second["turn_id"]) if r["role"] == "assistant"]) == 1,
            timeout=30.0,
        ), "轮次没有完成正式回答"
        rows = _message_rows(ctx.conn, second["turn_id"])
        assert [r["role"] for r in rows] == ["user", "assistant"], rows
        for row in rows:
            assert _fragment_topic(ctx.conn, row["fragment_id"]) == topic_a, row
        position = ctx.position_state.read()
        assert position["topic_id"] == topic_b, position
        assert int(position["position_version"]) == switch_position_version, position
        for event in _anchor_events(ctx)[events_before_switch:]:
            assert (event.get("position") or {}).get("topic_id") != topic_a, event
