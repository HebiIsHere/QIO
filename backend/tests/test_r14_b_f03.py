"""R14-B / F03：恢复执行与答案保存后端幂等（持久化 / 租约 / 唯一答案 / 事务收口）。

先建红场景（旧实现会失败）：
* 恢复记录只在 app 内存 dict（进程重启即丢）；
* retry_topic_answer 无原子领取/租约（两个并发请求都会生成）；
* 没有唯一逻辑答案身份（answer_key），重复恢复回调会写两份正文；
* answer 写入与记录 answered 不是同一事务；
* 生成成功但保存失败会重新生成。

模型一律 fake（asyncio 事件控制交错，不用 sleep/真实网络）；SQLite 用真实临时库。
外部模型调用**不保证 exactly-once**（崩溃在生成与暂存之间可能再调一次模型），
用例只证明「至多一份正式答案 + 不重复落库」。
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services import topic_recovery
from agent.services.app import AppContext
from agent.services.topic_operation import TopicStages
from agent.services.topic_recovery import (
    TopicContextPrepareError,
    answer_key_for,
    claim_record,
    record_topic_failure,
    retry_topic_answer,
)
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

ANSWER_MARKER = "[[QIO:ANSWER]]"
TABLE = "topic_recovery_records"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "r14_b_f03.db"


def _new_app(db_path, tmp_path) -> AppContext:
    conn = connect(db_path)
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


@pytest.fixture()
def app(db_path, tmp_path) -> AppContext:
    return _new_app(db_path, tmp_path)


class FakeAdapter:
    mode = "native"
    model = "fake-recovery"
    protocol_in_prompt = False

    def __init__(self, *, text: str = ANSWER_MARKER + "\n恢复后的回答", error=None) -> None:
        self.text = text
        self.error = error
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()  # 默认不阻塞；需要交错时 clear()
        self.requests: list = []

    async def complete(self, messages, tools, **kwargs):
        self.calls += 1
        self.requests.append({"messages": list(messages), "tools": list(tools or [])})
        self.started.set()
        await self.release.wait()
        if self.error is not None:
            raise self.error
        return Completion(message=ChatMessage(role="assistant", content=self.text))


class FakeOrchestrator:
    def __init__(self, *, messages=None, error: Exception | None = None) -> None:
        self.messages = messages
        self.error = error
        self.calls: list = []

    async def prepare_topic_answer_messages(self, *, turn_id, topic_id, user_message_id):
        self.calls.append(
            {"turn_id": turn_id, "topic_id": topic_id, "user_message_id": user_message_id}
        )
        if self.error is not None:
            raise self.error
        if self.messages is not None:
            return list(self.messages)
        return [
            ChatMessage(role="system", content="SYS_FOR:" + str(topic_id)),
            ChatMessage(role="user", content="原问题"),
        ]


def _wire(app: AppContext, orchestrator=None, adapter=None) -> None:
    if orchestrator is not None:
        app.turn_orchestrator = orchestrator  # type: ignore[assignment]
    if adapter is not None:
        async def _build_adapter():
            return adapter

        app.build_adapter = _build_adapter  # type: ignore[method-assign]


def _prepare(app: AppContext, *, topic_name: str = "目标话题", turn_id: str = "turn_r"):
    topic_id = app.topics.nodes.create_topic(topic_name).id
    fragment = app.fragments.get_or_create_open(topic_id)
    message_id, _ = app.memory.append_message(
        topic_id=topic_id,
        role="user",
        content="原问题",
        content_type="text",
        model="fake",
        turn_id=turn_id,
    )
    return topic_id, fragment.id, message_id


def _make_record(app: AppContext, topic_id: str, message_id: str, **overrides):
    payload = {
        "turn_id": "turn_r",
        "topic_id": topic_id,
        "user_message_id": message_id,
        "operation_id": "op-1",
        "param_version": 1,
        "failed_stage": "context_prepare",
        "message": "话题已切换，回答准备失败",
        "stages": TopicStages(context_ready=True),
    }
    payload.update(overrides)
    return record_topic_failure(app, **payload)


def _assistant_rows(app: AppContext) -> list:
    return app.conn.execute(
        "SELECT id, fragment_id, content, answer_key FROM messages WHERE role = 'assistant'"
    ).fetchall()


def _row(app: AppContext, record_id: str):
    return app.conn.execute(
        f"SELECT * FROM {TABLE} WHERE record_id = ?", (record_id,)
    ).fetchone()


# ---------------------------------------------------------------------------
# A. 持久化与重启
# ---------------------------------------------------------------------------


async def test_record_is_persisted_and_visible_after_restart(app, db_path, tmp_path):
    topic_id, _fragment, message_id = _prepare(app)
    record = _make_record(app, topic_id, message_id)

    row = _row(app, record["record_id"])
    assert row is not None, "恢复记录必须落 SQLite（旧实现只在 app 内存 dict）"
    assert row["status"] == "pending"
    assert row["kind"] == "topic_answer_recovery"
    assert row["user_message_id"] == message_id
    assert row["attempt_count"] == 0

    # 旧内存 dict 不再是权威来源
    memory_store = getattr(app, topic_recovery.TOPIC_RECOVERY_ATTR, None) or {}
    assert record["record_id"] not in memory_store

    # 「进程重启」：同一数据库新造 AppContext，记录仍然可见/可恢复
    restarted = _new_app(db_path, tmp_path)
    stored = topic_recovery.get_record(restarted, record["record_id"])
    assert stored is not None
    assert stored["status"] == "pending"
    assert stored["turn_id"] == "turn_r"
    assert stored["topic_id"] == topic_id
    assert [item["record_id"] for item in topic_recovery.list_records(restarted)] == [
        record["record_id"]
    ]


def test_answer_key_is_stable_logical_identity(app):
    topic_id, _fragment, message_id = _prepare(app)
    record = _make_record(app, topic_id, message_id)
    expected = hashlib.sha256(f"turn_r:{message_id}".encode()).hexdigest()
    assert record["answer_key"] == expected
    assert answer_key_for("turn_r", user_message_id=message_id) == expected
    # 没有 user_message_id 时退化为 turn_id+operation_id
    fallback = hashlib.sha256(b"turn_x:op_x").hexdigest()
    assert answer_key_for("turn_x", operation_id="op_x") == fallback

    # messages.answer_key 唯一约束：同一逻辑答案只能有一行
    direct = app.conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, content_type, model, raw, "
        "created_at, storage_tier, turn_id, answer_key) "
        "SELECT ?, fragment_id, 'assistant', 'dup', 'text', 'fake', '{}', ?, 'hot', turn_id, ? "
        "FROM messages WHERE id = ?",
        ("msg_dup_1", "2026-11-10T00:00:00+00:00", expected, message_id),
    )
    assert direct.rowcount == 1
    with pytest.raises(sqlite3.IntegrityError):
        app.conn.execute(
            "INSERT INTO messages (id, fragment_id, role, content, content_type, model, raw, "
            "created_at, storage_tier, turn_id, answer_key) "
            "SELECT ?, fragment_id, 'assistant', 'dup2', 'text', 'fake', '{}', ?, 'hot', turn_id, ? "
            "FROM messages WHERE id = ?",
            ("msg_dup_2", "2026-11-10T00:00:01+00:00", expected, message_id),
        )
    app.conn.execute("DELETE FROM messages WHERE id = 'msg_dup_1'")


# ---------------------------------------------------------------------------
# B. 原子领取 / 租约 / 并发
# ---------------------------------------------------------------------------


async def test_concurrent_retry_generates_once_and_second_reports_in_progress(app):
    topic_id, _fragment, message_id = _prepare(app)
    adapter = FakeAdapter()
    orchestrator = FakeOrchestrator()
    _wire(app, orchestrator, adapter)
    adapter.release.clear()  # 卡在模型调用
    record = _make_record(app, topic_id, message_id)

    first = asyncio.create_task(retry_topic_answer(app, record["record_id"]))
    await asyncio.wait_for(adapter.started.wait(), timeout=5)
    # 第一个请求已经领取租约并卡在模型调用里；第二个请求不得重复生成
    second = await retry_topic_answer(app, record["record_id"])
    assert second["ok"] is True
    assert second.get("answered") is False
    assert second.get("in_progress") is True
    assert second["code"] == "recovery_in_progress"
    assert second["status_code"] == 202
    assert second["record_id"] == record["record_id"]
    assert adapter.calls == 1, "处理中不得重复调用模型"

    adapter.release.set()
    done = await asyncio.wait_for(first, timeout=5)
    assert done["ok"] is True and done["answered"] is True
    assert done["answer_id"]
    assert adapter.calls == 1
    assert len(_assistant_rows(app)) == 1

    # 已完成后再次回调：返回同一 answer_id，不重复生成
    again = await retry_topic_answer(app, record["record_id"])
    assert again["answered"] is True
    assert again["answer_id"] == done["answer_id"]
    assert adapter.calls == 1
    assert len(_assistant_rows(app)) == 1


async def test_two_concurrent_http_retry_requests_generate_once(app):
    """两个并发 HTTP 请求：一个生成、另一个 202 处理中；最终只有一份答案。"""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from agent.api.recovery_routes import build_router

    topic_id, _fragment, message_id = _prepare(app)
    adapter = FakeAdapter()
    _wire(app, FakeOrchestrator(), adapter)
    adapter.release.clear()
    record = _make_record(app, topic_id, message_id)

    api = FastAPI()
    api.include_router(build_router(app))
    url = "/api/recovery/records/{}/retry-answer".format(record["record_id"])
    async with AsyncClient(
        transport=ASGITransport(app=api), base_url="http://test"
    ) as client:
        first_task = asyncio.create_task(client.post(url, json={}))
        await asyncio.wait_for(adapter.started.wait(), timeout=5)
        second = await client.post(url, json={})
        assert second.status_code == 202, second.text
        second_body = second.json()
        assert second_body["code"] == "recovery_in_progress"
        assert second_body["in_progress"] is True
        assert second_body["record_id"] == record["record_id"]
        assert adapter.calls == 1, "并发 HTTP 请求不得重复生成"

        adapter.release.set()
        first = await asyncio.wait_for(first_task, timeout=5)
        assert first.status_code == 200, first.text
        first_body = first.json()
        assert first_body["answered"] is True
        assert first_body["answer_id"]

    assert adapter.calls == 1
    assert len(_assistant_rows(app)) == 1
    stored = topic_recovery.get_record(app, record["record_id"])
    assert stored["status"] == "answered"
    assert stored["answer_id"] == first_body["answer_id"]


async def test_retry_does_not_hold_sqlite_write_transaction_during_model_call(
    app, db_path
):
    topic_id, _fragment, message_id = _prepare(app)
    adapter = FakeAdapter()
    orchestrator = FakeOrchestrator()
    _wire(app, orchestrator, adapter)
    adapter.release.clear()
    record = _make_record(app, topic_id, message_id)

    task = asyncio.create_task(retry_topic_answer(app, record["record_id"]))
    await asyncio.wait_for(adapter.started.wait(), timeout=5)

    # 模型调用期间另一个连接必须能写库（说明没有持有写事务/写锁）
    side = connect(db_path)
    side.execute("PRAGMA busy_timeout = 300")
    try:
        side.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            ("r14_b_side_write", "1", "2026-11-10T00:00:00+00:00"),
        )
    finally:
        side.close()

    adapter.release.set()
    result = await asyncio.wait_for(task, timeout=5)
    assert result["ok"] is True


async def test_valid_lease_blocks_and_expired_lease_allows_reclaim(app):
    topic_id, _fragment, message_id = _prepare(app)
    adapter = FakeAdapter()
    _wire(app, FakeOrchestrator(), adapter)
    record = _make_record(app, topic_id, message_id)

    # 有效租约（另一个仍未退出的执行者）：明确处理中，不生成
    app.conn.execute(
        f"UPDATE {TABLE} SET claim_owner = 'qio_dead', claim_until = "
        "(SELECT datetime('now', '+1 hour')) WHERE record_id = ?",
        (record["record_id"],),
    )
    blocked = await retry_topic_answer(app, record["record_id"])
    assert blocked.get("in_progress") is True
    assert blocked["status_code"] == 202
    assert adapter.calls == 0
    assert len(_assistant_rows(app)) == 0

    # 租约已失效 → 可重新领取并成功（启动/崩溃恢复后的可重试）
    app.conn.execute(
        f"UPDATE {TABLE} SET claim_until = '2000-01-01T00:00:00+00:00' "
        "WHERE record_id = ?",
        (record["record_id"],),
    )
    reclaimed = claim_record(
        app.conn,
        record["record_id"],
        owner="qio_reclaimer",
        lease_seconds=60,
    )
    assert reclaimed is not None
    assert reclaimed["claim_owner"] == "qio_reclaimer"
    app.conn.execute(
        f"UPDATE {TABLE} SET claim_owner = NULL, claim_until = NULL WHERE record_id = ?",
        (record["record_id"],),
    )
    ok = await retry_topic_answer(app, record["record_id"])
    assert ok["ok"] is True and ok["answered"] is True
    assert adapter.calls == 1


async def test_duplicate_recovery_records_share_single_authoritative_answer(app):
    topic_id, _fragment, message_id = _prepare(app)
    adapter = FakeAdapter()
    orchestrator = FakeOrchestrator()
    _wire(app, orchestrator, adapter)
    first_record = _make_record(app, topic_id, message_id, record_id="trec_first")
    second_record = _make_record(app, topic_id, message_id, record_id="trec_second")
    assert first_record["answer_key"] == second_record["answer_key"]

    first = await retry_topic_answer(app, first_record["record_id"])
    assert first["ok"] is True and first["answered"] is True
    second = await retry_topic_answer(app, second_record["record_id"])
    assert second["ok"] is True and second["answered"] is True
    assert second["answer_id"] == first["answer_id"]
    assert adapter.calls == 1, "同一逻辑答案已存在时不得重复生成"
    assert len(_assistant_rows(app)) == 1
    assert topic_recovery.get_record(app, second_record["record_id"])["status"] == "answered"


# ---------------------------------------------------------------------------
# C. 事务收口 / 暂存
# ---------------------------------------------------------------------------


async def test_answer_row_and_answered_status_commit_in_one_transaction(app, monkeypatch):
    topic_id, _fragment, message_id = _prepare(app)
    adapter = FakeAdapter()
    _wire(app, FakeOrchestrator(), adapter)
    record = _make_record(app, topic_id, message_id)

    real_mark = topic_recovery._mark_answered_row

    def boom_mark(*args, **kwargs):
        real_mark(*args, **kwargs)
        raise RuntimeError("simulated failure after answer row insert")

    monkeypatch.setattr(topic_recovery, "_mark_answered_row", boom_mark)
    failed = await retry_topic_answer(app, record["record_id"])

    assert failed["ok"] is False
    assert failed["code"] == "recovery_persist_failed"
    assert failed["recoverable"] is True
    assert failed["status_code"] == 503
    # 同一事务：记录更新失败 → 已插入的答案行必须一起回滚（不能留下半截）
    assert _assistant_rows(app) == []
    stored = _row(app, record["record_id"])
    assert stored["status"] == "pending"
    # 生成的正文已暂存，后续只补存、不重复生成
    assert stored["answer_text"] == "恢复后的回答"
    assert stored["attempt_count"] == 1
    assert stored["claim_owner"] is None and stored["claim_until"] is None
    assert "simulated failure" in stored["last_error"]

    monkeypatch.setattr(topic_recovery, "_mark_answered_row", real_mark)
    ok = await retry_topic_answer(app, record["record_id"])
    assert ok["ok"] is True and ok["answered"] is True
    assert adapter.calls == 1, "保存失败后的重试只补存，不得重复调用模型"
    assert len(_assistant_rows(app)) == 1
    assert _row(app, record["record_id"])["answer_text"] in (None, "")


async def test_persist_failure_with_secret_is_redacted(app, monkeypatch):
    topic_id, _fragment, message_id = _prepare(app)
    adapter = FakeAdapter()
    _wire(app, FakeOrchestrator(), adapter)
    record = _make_record(app, topic_id, message_id)

    real_insert = topic_recovery._insert_answer_message

    def boom(conn, **kwargs):
        raise RuntimeError("persist exploded with Authorization: sk-live-ABCDEF123456")

    monkeypatch.setattr(topic_recovery, "_insert_answer_message", boom)
    failed = await retry_topic_answer(app, record["record_id"])
    assert failed["ok"] is False
    stored = _row(app, record["record_id"])
    assert "sk-live-ABCDEF123456" not in (stored["last_error"] or "")
    assert "***redacted***" in (stored["last_error"] or "")
    monkeypatch.setattr(topic_recovery, "_insert_answer_message", real_insert)
    ok = await retry_topic_answer(app, record["record_id"])
    assert ok["ok"] is True
    assert adapter.calls == 1


# ---------------------------------------------------------------------------
# D. 已完成不被迟到失败覆盖
# ---------------------------------------------------------------------------


async def test_answered_record_is_not_overwritten_by_late_failure_replay(app):
    topic_id, _fragment, message_id = _prepare(app)
    adapter = FakeAdapter()
    _wire(app, FakeOrchestrator(), adapter)
    record = _make_record(app, topic_id, message_id)
    done = await retry_topic_answer(app, record["record_id"])
    assert done["answered"] is True

    # 迟到的失败记录（同一 record_id 的重放）：不得把 answered 打回 pending
    _make_record(app, topic_id, message_id, record_id=record["record_id"], failed_stage="answer")
    stored = topic_recovery.get_record(app, record["record_id"])
    assert stored["status"] == "answered"
    assert stored["answer_id"] == done["answer_id"]

    again = await retry_topic_answer(app, record["record_id"])
    assert again["answered"] is True
    assert again["answer_id"] == done["answer_id"]
    assert adapter.calls == 1
    assert len(_assistant_rows(app)) == 1
