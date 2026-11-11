# -*- coding: utf-8 -*-
"""R17 · B 补充：重发（/api/turns/{turn_id}/resend）受理也必须冻结 basis（P1-1 同路径）。

被测系统：真实 FastAPI create_app + 真实迁移 + 临时 SQLite + TestClient（真实 lifespan）
+ fake provider（不联网）。顺序由 threading 事件闸门控制，不用 sleep 构造时序。

缺陷路径（F 在集成 SHA d31cd27b 上发现）：
* 一条「未执行」消息被重发时，resend 的 reserve 没有传 R17 §2.1 的 basis；
* 重发受理后、轮次 begin 写入位置之前，用户手动导航到另一话题；
* 重发轮 begin 没有依据链 → 临时读当前 position_version 做 CAS → 把位置改回
  原轮话题并广播 ANCHOR（与 P1-1 完全同一条缺陷路径）。

修复后：
* reserve 在受理那一刻捕获 basis（与 /api/turns 同一份 _submission_basis）；
* begin 的轮首补锚用依据链做 CAS，被用户新导航 superseded → 跳过、不广播；
* 回答仍按原轮/journ 记录归属写回原话题，最终权威位置保持用户新话题。
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
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

ANSWER_BODY = "[[QIO:ANSWER]]\nR17-B 重发依据验收回答"


def _request_id(tag: str) -> str:
    return f"req-r17b-resend-{tag}-{uuid.uuid4().hex}"


def _wait_until(predicate, *, timeout: float = 30.0, interval: float = 0.02) -> bool:
    """有界等待异步事实；顺序由事件闸门决定，这里只等结果。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class _AnswerAdapter:
    """确定性 fake provider：不联网、不调用真实模型。"""

    mode = "native"
    model = "fake-r17b-resend"
    supports_stream = False
    protocol_in_prompt = False

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        return Completion(message=ChatMessage(role="assistant", content=ANSWER_BODY))


@contextmanager
def _backend(tmp_path: Path):
    """真实 create_app + 临时 SQLite；begin 的 adapter 阶段挂闸门（位置写入之前）。"""
    conn = connect(tmp_path / "r17b_resend_basis.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path), conn)
    ctx = app.state.ctx
    ctx.credentials._kr = MemoryKeyring()
    adapter = _AnswerAdapter()
    entered = threading.Event()
    release = threading.Event()

    async def _build_adapter():
        # begin() 第一步就是 build_adapter；此时轮首补锚（位置写入）还没发生。
        entered.set()
        await asyncio.to_thread(release.wait, 60.0)
        return adapter

    ctx.build_adapter = _build_adapter  # type: ignore[assignment]
    # 预测/派生真实模型依赖去掉：本用例只验证位置与归属。
    from agent.services.predict import TopicPrediction

    async def _fake_predict(turn_ctx, message, topic):  # noqa: ANN001
        return TopicPrediction(main_topic_id=topic, backend_used="rules")

    ctx.turn_orchestrator._predict_topics = _fake_predict  # type: ignore[assignment]
    ctx.turn_orchestrator._schedule_derived_work = lambda *a, **k: None  # type: ignore[assignment]
    try:
        with TestClient(app) as client:
            yield client, ctx, entered, release, adapter
    finally:
        release.set()
        conn.close()


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


def _seed_recoverable(ctx, turn_id: str, message: str, topic_id: str) -> None:
    """造一条「已受理但未执行」的 turn_journal 记录（用户重启后仍可重发）。"""
    journal = ctx.turn_journal
    journal.accepted(turn_id=turn_id, message=message, topic_id=topic_id)
    journal.running(turn_id)
    journal.interrupt_stale()
    assert journal.recoverable(turn_id) is not None, "测试装置：记录必须处于可重发状态"


def _journal_status(conn, turn_id: str) -> str | None:
    row = conn.execute(
        "SELECT status FROM turn_journal WHERE turn_id = ?", (turn_id,)
    ).fetchone()
    return str(row["status"]) if row is not None else None


def _message_rows(conn, turn_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, role, content, fragment_id FROM messages "
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


def test_resend_basis_prevents_late_anchor_steal(tmp_path: Path) -> None:
    """重发受理后用户导航到 B：begin 补锚必须 superseded，回答归 A、位置保持 B。"""
    with _backend(tmp_path) as (client, ctx, entered, release, adapter):
        topic_a = ctx.topics.nodes.create_topic("重发原话题 A").id
        topic_b = ctx.topics.nodes.create_topic("用户新导航 B").id
        old_turn_id = "turn-r17b-resend-old"
        _seed_recoverable(ctx, old_turn_id, "还没执行的旧消息", topic_a)

        resend = client.post(f"/api/turns/{old_turn_id}/resend")
        assert resend.status_code == 200, resend.text
        body = resend.json()
        assert body["ok"] is True, body
        new_turn_id = body["turn_id"]
        assert new_turn_id and new_turn_id != old_turn_id

        # 重发已受理、worker 已进入 begin 的 adapter 阶段：位置写入还没发生。
        assert entered.wait(30.0), "重发轮没有进入 begin（或没有调用 build_adapter）"

        # 用户手动导航到 B（带 request_id + base_nav_version 的真实 HTTP 命令）。
        switch = _switch_topic(client, topic_b)
        switch_position_version = int(switch["position_version"])
        assert _anchor(client)["topic_id"] == topic_b
        events_after_switch = len(_anchor_events(ctx))

        # 放行 begin：有 basis 时轮首补锚必须被依据链 CAS 拒绝（superseded），不得改位置。
        release.set()
        assert _wait_until(
            lambda: _journal_status(ctx.conn, new_turn_id)
            in ("completed", "failed", "cancelled"),
            timeout=30.0,
        ), f"重发轮没有收口：status={_journal_status(ctx.conn, new_turn_id)}"
        assert _journal_status(ctx.conn, new_turn_id) == "completed", (
            _journal_status(ctx.conn, new_turn_id)
        )
        assert adapter.calls >= 1, "fake provider 必须被调用（回答路径真实执行）"

        # 1) 归属：用户消息与正式回答都写回**原轮话题 A**（重发记录的话题）。
        rows = _message_rows(ctx.conn, new_turn_id)
        assert [row["role"] for row in rows] == ["user", "assistant"], rows
        for row in rows:
            assert _fragment_topic(ctx.conn, row["fragment_id"]) == topic_a, row
        assert "R17-B 重发依据验收回答" in rows[-1]["content"], rows[-1]
        binding = ctx.bindings.binding_for(new_turn_id)
        assert binding is not None and binding.topic_id == topic_a, binding

        # 2) 位置：旧轮的轮首补锚/轮末推进都不得把权威位置改回 A —— 仍是用户新话题 B，
        #    且 position_version 没有被旧轮再推进。
        position = ctx.position_state.read()
        assert position["topic_id"] == topic_b, position
        assert int(position["position_version"]) == switch_position_version, position
        wire = _anchor(client)
        assert wire["topic_id"] == topic_b, wire
        assert int(wire["position_version"]) == switch_position_version, wire

        # 3) 广播：用户导航之后不得再出现把原话题 A 广播成当前位置的 ANCHOR 事件。
        late_events = _anchor_events(ctx)[events_after_switch:]
        for event in late_events:
            event_position = event.get("position") or {}
            assert event_position.get("topic_id") != topic_a, event
            assert event.get("topic_id") != topic_a, event
