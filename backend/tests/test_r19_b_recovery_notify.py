# -*- coding: utf-8 -*-
"""R19-B：普通回答保存失败后通知前端刷新恢复清单（R19 契约 §2.2 / §5 B1—B4）。

缺口（基线 d1f8376d）：R18-B 已在 save_normal_answer 失败时同事务写共享暂存 +
pending 恢复记录，但**没有任何通知**：已打开且清单为空的页面只有刷新 / 重连之后
才看得见「重试回答」入口。

本文件从契约行为出发（真实 HTTP + 真实迁移 + 临时 SQLite + fake provider；不联网，
不按真实时间等模型）：

* B1 保存失败且记录提交成功 → **事务提交之后**发布 RECOVERY_INBOX_CHANGED，
  payload 带真实 record_id / turn_id / topic_id / kind / failed_stage / action；
  普通成功轮不发布该事件。
* B1b 事件类型已注册，且必须归入总线关键事件（背压时不能静默丢）。
* B2 查不到已提交 pending 记录（暂存 / 记录没写成功、被活跃执行者持有）→ 不发。
* B2b 通知本身失败（bus.publish 抛错）不得掩盖原异常、不改变轮次结局。
* B0 find_pending_by_answer_key 是只读查询：pending 返回、无 / answered 返回 None。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from agent.adapters.fake import FakeStreamAdapter, StreamScript
from agent.api.events import EventType
from agent.api.server import create_app
from agent.credentials.store import MemoryKeyring
from agent.services import topic_recovery

DECL = "[[QIO:ANSWER]]\n"
BODY = "R19-B 正文：保存失败后前端必须看到重试入口"
QUESTION = "请直接回答这个普通问题"
FAILURE = "注入：普通回答保存失败"
EVENT_TYPE = "RECOVERY_INBOX_CHANGED"
RECOVERY_TABLE = "topic_recovery_records"
RUN_TABLE = "topic_answer_runs"
TURN = "turn_r19_b_direct"
TIMEOUT = 60.0


# ── 装置：真实 app + fake provider + 权威表读取 ────────────────────────


@pytest.fixture()
def app(db_conn, settings):
    application = create_app(settings, db_conn)
    application.state.ctx.credentials._kr = MemoryKeyring()
    return application


def _ctx(application):
    return application.state.ctx


def _client(application):
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://qio.test",
        timeout=120.0,
    )


def _install_fake_adapter(application) -> FakeStreamAdapter:
    adapter = FakeStreamAdapter([StreamScript(text=DECL + BODY)])
    ctx = _ctx(application)

    async def _build():
        return adapter

    ctx.build_adapter = _build  # type: ignore[method-assign]
    return adapter


def _events(application, kind: str) -> list[dict]:
    return [
        event.data
        for event in list(_ctx(application).bus._history)  # noqa: SLF001 - 与既有 HTTP 用例同一只读取法
        if event.type.value == kind
    ]


async def _wait_turn_end(application, turn_id: str, timeout: float = TIMEOUT) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for data in _events(application, "TURN_END"):
            if str(data.get("turn_id")) == str(turn_id):
                return data
        await asyncio.sleep(0.02)
    raise AssertionError("TURN_END 没有到达（turn_id=%s）" % turn_id)


async def _start_turn(client, message: str = QUESTION) -> tuple[str, dict]:
    resp = await client.post("/api/turns", json={"message": message})
    assert resp.status_code == 200, resp.text[:500]
    payload = resp.json()
    assert payload.get("accepted") is True, payload
    return str(payload["turn_id"]), payload


async def _list_topic_items(client) -> list[dict]:
    resp = await client.get("/api/recovery/records")
    assert resp.status_code == 200, resp.text[:500]
    return [
        item
        for item in resp.json().get("records") or []
        if item.get("kind") == topic_recovery.RECOVERY_KIND
    ]


def _rows(ctx, sql: str, params=()) -> list[dict]:
    return [dict(row) for row in ctx.conn.execute(sql, params)]


def _db_path(ctx) -> str:
    row = ctx.conn.execute("PRAGMA database_list").fetchone()
    value = row["file"] if hasattr(row, "keys") else row[2]
    return str(value)


def _committed_row(ctx, record_id: str) -> dict | None:
    """用**独立连接**读权威表：只有已提交事务的行才可见（验证「提交后才发事件」）。"""
    probe = sqlite3.connect(_db_path(ctx), timeout=10.0)
    try:
        probe.row_factory = sqlite3.Row
        row = probe.execute(
            f"SELECT * FROM {RECOVERY_TABLE} WHERE record_id = ?", (str(record_id),)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        probe.close()


@pytest.fixture()
def failing_insert(monkeypatch):
    """注入一次普通回答写入失败（保存失败路径的触发点，与 R18-B 相同）。"""
    state = {"calls": 0}

    def _boom(conn, **kwargs):  # noqa: ANN001, ANN003
        state["calls"] += 1
        raise RuntimeError(FAILURE)

    monkeypatch.setattr(topic_recovery, "_insert_normal_answer_message", _boom)
    return state


@pytest.fixture()
def published_events(app, monkeypatch):
    """把总线 publish 包一层：记录本类型事件，并保持其它事件照常发布。"""
    ctx = _ctx(app)
    real_publish = ctx.bus.publish
    seen: list[dict] = []

    async def _wrapped(event):
        if event.type.value == EVENT_TYPE:
            seen.append(dict(event.data))
        await real_publish(event)

    monkeypatch.setattr(ctx.bus, "publish", _wrapped)
    return seen


# ── B0：只读 helper（查不到 / answered 都返回 None）────────────────────


def test_b0_find_pending_by_answer_key_is_readonly(app):
    ctx = _ctx(app)
    topic_id = ctx.topics.nodes.create_topic("R19-B helper 话题").id
    record = topic_recovery.record_topic_failure(
        ctx,
        turn_id=TURN,
        topic_id=topic_id,
        failed_stage="answer",
        message="helper 只读查询",
        user_message_id="msg_r19_b_helper",
    )
    answer_key = topic_recovery.answer_key_for(
        TURN, user_message_id="msg_r19_b_helper"
    )
    assert record["answer_key"] == answer_key, record

    found = topic_recovery.find_pending_by_answer_key(ctx, answer_key)
    assert found is not None and found["record_id"] == record["record_id"], found
    assert topic_recovery.find_pending_by_answer_key(ctx, "0" * 64) is None
    assert topic_recovery.find_pending_by_answer_key(ctx, "") is None

    topic_recovery.mark_answered(ctx, record["record_id"], "已收口")
    assert (
        topic_recovery.find_pending_by_answer_key(ctx, answer_key) is None
    ), "answered 记录不得再作为「待恢复」返回"


# ── B1：保存失败 → 提交后发事件，payload 带真实记录事实 ───────────────


async def test_b1_event_published_after_committed_pending_record(
    app, failing_insert, published_events, monkeypatch
):
    ctx = _ctx(app)
    _install_fake_adapter(app)
    commit_checked: list[dict] = []
    real_publish = ctx.bus.publish

    async def _wrapped(event):
        if event.type.value == EVENT_TYPE:
            data = dict(event.data)
            committed = _committed_row(ctx, str(data.get("record_id") or ""))
            assert committed is not None, (
                "RECOVERY_INBOX_CHANGED 必须在恢复记录事务提交之后发布"
                "（独立连接读不到未提交行）"
            )
            assert committed["status"] == "pending", committed
            commit_checked.append(committed)
        await real_publish(event)

    monkeypatch.setattr(ctx.bus, "publish", _wrapped)

    async with _client(app) as client:
        turn_id, _accept = await _start_turn(client)
        end = await _wait_turn_end(app, turn_id)
        assert end.get("status") == "failed", end
        assert failing_insert["calls"] == 1, failing_insert

        records = _rows(ctx, f"SELECT * FROM {RECOVERY_TABLE} ORDER BY created_at, record_id")
        pending = [
            row
            for row in records
            if row["status"] == "pending" and row["turn_id"] == turn_id
        ]
        assert len(pending) == 1, "保存失败必须已提交恰好一条 pending 记录：%s" % pending
        record = pending[0]

        assert len(published_events) == 1, published_events
        payload = published_events[0]
        assert payload["record_id"] == record["record_id"], payload
        assert record["record_id"].startswith("trec_"), record
        assert payload["turn_id"] == turn_id, payload
        assert payload["topic_id"] == record["topic_id"], payload
        assert payload["kind"] == topic_recovery.RECOVERY_KIND, payload
        assert payload["failed_stage"] == "answer", payload
        assert payload["action"] == topic_recovery.ACTION_RETRY_ANSWER, payload
        assert len(commit_checked) == 1, commit_checked

        # 同一事实在权威清单里也可见（前端刷新后真的出现「重试回答」）
        items = await _list_topic_items(client)
        assert [item["record_id"] for item in items] == [record["record_id"]], items

        # 事件之后轮次才收口：TURN_END 是 TurnManager 的唯一出口
        ends = [
            data
            for data in _events(app, "TURN_END")
            if str(data.get("turn_id")) == turn_id
        ]
        assert ends and ends[0]["status"] == "failed", ends


async def test_b1b_event_type_registered_and_critical():
    from agent.api.bus import CRITICAL_EVENTS, MERGEABLE_EVENTS

    assert EventType.RECOVERY_INBOX_CHANGED.value == EVENT_TYPE
    assert (
        EventType.RECOVERY_INBOX_CHANGED in CRITICAL_EVENTS
    ), "清单变化通知丢了会让页面看不到重试入口：背压时不能静默丢（关键事件）"
    assert EventType.RECOVERY_INBOX_CHANGED not in MERGEABLE_EVENTS


async def test_b1c_successful_turn_does_not_publish_event(
    app, published_events
):
    _install_fake_adapter(app)
    async with _client(app) as client:
        turn_id, _accept = await _start_turn(client)
        end = await _wait_turn_end(app, turn_id)
        assert end.get("status") == "completed", end

    assert published_events == [], "普通成功轮不得发「恢复清单已变化」事件"


# ── B2：查不到已提交 pending 记录 → 不发 ───────────────────────────────


async def test_b2_no_committed_record_no_event(
    app, failing_insert, published_events, monkeypatch
):
    ctx = _ctx(app)
    _install_fake_adapter(app)
    # 暂存 / 记录没有写成功（例如被另一活跃执行者持有 / 表不可用）：失败路径返回 False
    monkeypatch.setattr(
        topic_recovery, "_stash_normal_answer_failure", lambda *a, **k: False
    )

    async with _client(app) as client:
        turn_id, _accept = await _start_turn(client)
        end = await _wait_turn_end(app, turn_id)
        assert end.get("status") == "failed", end

    assert published_events == [], "查不到已提交 pending 记录时不得发事件"
    assert _rows(ctx, f"SELECT * FROM {RECOVERY_TABLE}") == []
    assert _rows(ctx, f"SELECT * FROM {RUN_TABLE}") == []


# ── B2b：通知失败不得掩盖原异常 / 不改变轮次结局 ──────────────────────


async def test_b2b_notification_failure_does_not_mask_original_exception(
    app, failing_insert, monkeypatch
):
    ctx = _ctx(app)
    _install_fake_adapter(app)
    real_publish = ctx.bus.publish

    async def _flaky(event):
        if event.type.value == EVENT_TYPE:
            raise RuntimeError("注入：通知总线失败")
        await real_publish(event)

    monkeypatch.setattr(ctx.bus, "publish", _flaky)

    async with _client(app) as client:
        turn_id, _accept = await _start_turn(client)
        end = await _wait_turn_end(app, turn_id)
        assert end.get("status") == "failed", end
        # 原异常照常抛出：轮次失败原因仍是「保存失败」，不是通知失败
        assert FAILURE in str(end.get("error") or ""), end

    assert _events(app, EVENT_TYPE) == []
    record = [
        row
        for row in _rows(ctx, f"SELECT * FROM {RECOVERY_TABLE}")
        if row["status"] == "pending"
    ]
    assert len(record) == 1, "通知失败不得回滚已提交的暂存 / 记录"
