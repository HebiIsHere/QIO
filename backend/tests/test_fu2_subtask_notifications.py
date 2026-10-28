# -*- coding: utf-8 -*-
"""FU2-D 子任务通知可靠投递：先红后绿的回归测试（基线 373fda40）。

基线缺陷（任务书 §4.4）：
1. 纯内存 deque + MAX_PENDING_NOTIFICATIONS=200：超过 200 条时 popleft **静默丢弃**最早通知；
2. enqueue 时调用 TaskManager.mark_notified —— 把「入队」当成「已通知/已交付」；
3. 没有持久化：重启后队列清空；take 之后进程崩溃会丢该通知（已标记 notified 无法补投）。

本文件同时覆盖迁移 71（新表）与迁移 72（补偿入口）：
* 全新库一次迁移后表 / 索引存在；
* 版本 70 的旧库升级到 72 后有表；
* 版本已登记（72）但对象缺失时按 REQUIRED_OBJECTS 重放补偿补齐；
* 表存在但缺可修复列时，由队列初始化如实报错（不静默当作可用）。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from agent.api.bus import EventBus
from agent.config import Settings
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.storage.schema import COMPENSATION_VERSION, MIGRATIONS, SCHEMA_VERSION
from agent.tools.base import ToolResult
from agent.tools.subtask_notifications import (
    MAX_PENDING_NOTIFICATIONS,
    SubtaskNotificationQueue,
)
from agent.tools.task_manager import TaskManager, TaskRecord

LEGACY_CAPACITY = MAX_PENDING_NOTIFICATIONS  # 200：旧实现的上限（现在是告警阈值）


def _manager() -> TaskManager:
    return TaskManager(EventBus(), max_concurrent=4)


def _record(task_id: str, *, outcome: str = "completed", cancelled: bool = False) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        tool="research_x",
        status="done",
        result=ToolResult(ok=True, content="ok"),
        outcome=outcome,
        cancelled=cancelled,
        origin={"turn_id": "turn_1", "topic_id": "topic_1"},
    )


def _db(tmp_path: Path, name: str = "fu2_notifications.db") -> sqlite3.Connection:
    conn = connect(tmp_path / name)
    apply_migrations(conn)
    return conn


def _rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT task_id, payload, origin, created_at, delivered_at, attempts, last_error,"
        " last_attempt_at FROM subtask_notifications ORDER BY created_at, rowid"
    ).fetchall()


# ---------------------------------------------------------------------------
# 1) 不再静默丢弃：250 条全部排队（内存模式保留兼容；DB 模式为运行时形态）
# ---------------------------------------------------------------------------


def test_no_silent_drop_over_legacy_capacity():
    """250 条通知必须全部排队：超过 200 只告警，不得 popleft 丢弃最早的。"""
    manager = _manager()
    queue = SubtaskNotificationQueue(manager)
    ids = [f"task_{index:03d}" for index in range(LEGACY_CAPACITY + 50)]
    for task_id in ids:
        assert queue.enqueue(task_id, _record(task_id)) is True, task_id
    assert queue.pending_count() == len(ids), (
        f"基线把超过 {LEGACY_CAPACITY} 的最早通知静默丢弃了："
        f"期望 {len(ids)} 条，实际 {queue.pending_count()} 条"
    )
    pending = queue.pending()
    assert [item["task_id"] for item in pending] == ids, "FIFO 顺序必须保留，且一条不丢"
    # 取第一条：仍是最早那条（不是被丢弃后剩下的第 51 条）
    assert queue.take()["task_id"] == ids[0]


def test_persisted_queue_keeps_250_rows(tmp_path: Path):
    """DB 模式：250 条全部落库、可逐条读回（重启也不丢）。"""
    conn = _db(tmp_path)
    manager = _manager()
    queue = SubtaskNotificationQueue(manager, conn=conn)
    ids = [f"task_{index:03d}" for index in range(LEGACY_CAPACITY + 50)]
    for task_id in ids:
        assert queue.enqueue(task_id, _record(task_id)) is True
    assert queue.pending_count() == len(ids)
    rows = _rows(conn)
    assert [row["task_id"] for row in rows] == ids
    assert all(row["delivered_at"] is None for row in rows)


# ---------------------------------------------------------------------------
# 2) 入队不是交付：mark_notified 不再被当作交付判据
# ---------------------------------------------------------------------------


def test_enqueue_is_not_delivery():
    """入队只登记待投递：不得调用 mark_notified / 不得把 record.notified 当作已交付。"""
    manager = _manager()
    queue = SubtaskNotificationQueue(manager)
    record = _record("task_enqueue")
    assert queue.enqueue("task_enqueue", record) is True
    assert manager.was_notified("task_enqueue") is False, (
        "入队不得标记「已通知/已交付」：交付的唯一判据是投递成功（DB delivered_at）"
    )
    assert record.notified is False


def test_enqueue_does_not_write_delivered_at(tmp_path: Path):
    conn = _db(tmp_path)
    queue = SubtaskNotificationQueue(_manager(), conn=conn)
    assert queue.enqueue("task_a", _record("task_a")) is True
    row = _rows(conn)[0]
    assert row["delivered_at"] is None and row["attempts"] == 0
    assert row["last_error"] is None


# ---------------------------------------------------------------------------
# 3) 投递状态机：成功才写 delivered_at；失败保留 attempts/last_error 可重试
# ---------------------------------------------------------------------------


def test_failed_delivery_keeps_row_with_attempt_and_error(tmp_path: Path):
    conn = _db(tmp_path)
    queue = SubtaskNotificationQueue(_manager(), conn=conn)
    queue.enqueue("task_fail", _record("task_fail"))

    item = queue.take()
    assert item is not None and item["task_id"] == "task_fail"
    row = _rows(conn)[0]
    assert row["delivered_at"] is None, "取出一条 ≠ 已交付"
    assert row["attempts"] == 1 and row["last_attempt_at"], "投递尝试必须留痕"

    assert queue.mark_failed(item, "RuntimeError: turn submit failed") is True
    row = _rows(conn)[0]
    assert row["delivered_at"] is None, "投递失败不得写成已交付"
    assert row["last_error"] == "RuntimeError: turn submit failed"
    assert queue.pending_count() == 1, "失败的通知必须保留待重试"

    again = queue.take()
    assert again is not None and again["task_id"] == "task_fail", "失败后可再次投递同一条"
    assert _rows(conn)[0]["attempts"] == 2

    assert queue.mark_delivered(again) is True
    row = _rows(conn)[0]
    assert row["delivered_at"], "只有投递成功才写 delivered_at"
    assert queue.pending_count() == 0
    assert queue.take() is None, "已交付的永不重投"


def test_mark_delivered_is_idempotent_and_only_once(tmp_path: Path):
    conn = _db(tmp_path)
    queue = SubtaskNotificationQueue(_manager(), conn=conn)
    queue.enqueue("task_once", _record("task_once"))
    item = queue.take()
    assert queue.mark_delivered(item) is True
    assert queue.mark_delivered(item) is False, "重复交付同一 task_id 必须被拒绝"
    assert len(_rows(conn)) == 1


# ---------------------------------------------------------------------------
# 4) 去重、终态过滤与重启恢复
# ---------------------------------------------------------------------------


def test_duplicate_enqueue_is_idempotent(tmp_path: Path):
    conn = _db(tmp_path)
    queue = SubtaskNotificationQueue(_manager(), conn=conn)
    assert queue.enqueue("task_dup", _record("task_dup")) is True
    assert queue.enqueue("task_dup", _record("task_dup")) is False
    assert len(_rows(conn)) == 1
    assert queue.pending_count() == 1


def test_cancelled_and_non_notify_outcomes_are_not_queued(tmp_path: Path):
    conn = _db(tmp_path)
    queue = SubtaskNotificationQueue(_manager(), conn=conn)
    assert queue.enqueue("task_cancel", _record("task_cancel", cancelled=True)) is False
    assert queue.enqueue("task_quiet", _record("task_quiet", outcome="running")) is False
    assert _rows(conn) == []


def test_restart_recovers_undelivered_and_does_not_redeliver_delivered(tmp_path: Path):
    conn = _db(tmp_path)
    first = SubtaskNotificationQueue(_manager(), conn=conn)
    for task_id in ("task_1", "task_2", "task_3"):
        first.enqueue(task_id, _record(task_id))
    delivered = first.take()
    assert delivered is not None and delivered["task_id"] == "task_1"
    first.mark_delivered(delivered)

    # 模拟进程重启：同一个库、全新的队列与 manager
    restarted = SubtaskNotificationQueue(_manager(), conn=conn)
    assert restarted.pending_count() == 2, "重启必须恢复未送达的通知"
    ids = [item["task_id"] for item in restarted.pending()]
    assert ids == ["task_2", "task_3"], "已交付的不得重投；顺序保持 FIFO"
    assert restarted.take()["task_id"] == "task_2"

    # 再重启一次：task_2 只是被取出（未交付），仍在待投递里
    third = SubtaskNotificationQueue(_manager(), conn=conn)
    assert third.pending_count() == 2


def test_payload_and_origin_roundtrip(tmp_path: Path):
    conn = _db(tmp_path)
    queue = SubtaskNotificationQueue(_manager(), conn=conn)
    record = _record("task_payload")
    record.full_content = "完整结果正文"
    record.total_chars = 6
    queue.enqueue("task_payload", record)
    item = queue.take()
    assert item["origin"] == {"turn_id": "turn_1", "topic_id": "topic_1"}
    assert item["tool"] == "research_x"
    assert item["outcome"] == "completed"
    assert item["status_label"] == "完成"
    row = _rows(conn)[0]
    assert json.loads(row["payload"])["task_id"] == "task_payload"
    assert json.loads(row["origin"])["topic_id"] == "topic_1"


def test_missing_table_is_reported_not_silently_ignored(tmp_path: Path):
    """表不存在（库未迁移）时初始化必须如实报错，不能假装队列可用。"""
    conn = connect(tmp_path / "no_table.db")
    with pytest.raises(RuntimeError) as excinfo:
        SubtaskNotificationQueue(_manager(), conn=conn)
    assert "subtask_notifications" in str(excinfo.value)


def test_missing_repairable_column_is_reported(tmp_path: Path):
    """表存在但缺可修复列：初始化如实报错并指名列（不静默把通知投进坏表）。"""
    conn = _db(tmp_path)
    # 重建一张「表在、状态列没了」的坏表（delivered_at 上有索引，SQLite 不允许 DROP COLUMN；
    # 重建更接近真实的形状损坏：迁移半截 / 被外部工具改过）。
    conn.execute("DROP TABLE subtask_notifications")
    conn.execute("CREATE TABLE subtask_notifications (task_id TEXT PRIMARY KEY)")
    with pytest.raises(RuntimeError) as excinfo:
        SubtaskNotificationQueue(_manager(), conn=conn)
    assert "delivered_at" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 5) 迁移 71 / 补偿 72 与 REQUIRED_OBJECTS
# ---------------------------------------------------------------------------


def test_migrations_71_and_72_exist_and_are_ordered():
    targets = [target for target, _ in MIGRATIONS]
    assert targets == sorted(set(targets)), targets
    assert 71 in targets and 72 in targets
    assert 70 in targets, "历史迁移 70 必须保留（只追加新迁移）"
    # 迁移只追加：后续轮次继续在 71/72 之后登记新迁移，所以这里只断言「不低于 71/72」
    # 且「补偿入口 = 最新迁移」，不再写死具体号（写死会让后来任何一次追加迁移都误报）。
    assert SCHEMA_VERSION >= 72, f"SCHEMA_VERSION 应至少为 72，实际 {SCHEMA_VERSION}"
    assert COMPENSATION_VERSION == SCHEMA_VERSION, (
        f"COMPENSATION_VERSION 必须等于 SCHEMA_VERSION，实际 {COMPENSATION_VERSION}/{SCHEMA_VERSION}"
    )


def test_compensation_72_contains_new_table_and_is_additive():
    statements = [str(stmt) for target, stmts in MIGRATIONS if target == 72 for stmt in stmts]
    blob = "\n".join(statements)
    assert "CREATE TABLE IF NOT EXISTS subtask_notifications" in blob
    assert "idx_subtask_notifications_pending" in blob
    for stmt in statements:
        head = stmt.strip().upper()
        assert not head.startswith(("DROP ", "DELETE ", "UPDATE ", "TRUNCATE")), stmt[:60]


def test_required_objects_include_notification_table_and_index():
    from agent.storage.migrate import REQUIRED_OBJECTS

    assert "subtask_notifications" in REQUIRED_OBJECTS["tables"]
    assert "idx_subtask_notifications_pending" in REQUIRED_OBJECTS["indexes"]
    for spec in REQUIRED_OBJECTS["columns"]:
        if spec.startswith("subtask_notifications."):
            column = spec.partition(".")[2]
            statements = [
                str(stmt) for target, stmts in MIGRATIONS if target == 72 for stmt in stmts
            ]
            assert any(f"ADD COLUMN {column}" in stmt for stmt in statements), (
                f"REQUIRED_OBJECTS 里的列 {spec} 必须有补偿 ADD COLUMN 语句"
            )


def test_fresh_database_gets_notification_table_and_index(tmp_path: Path):
    conn = _db(tmp_path)
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(subtask_notifications)")
    }
    assert {
        "task_id",
        "payload",
        "origin",
        "created_at",
        "delivered_at",
        "attempts",
        "last_error",
        "last_attempt_at",
    } <= columns
    indexes = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='subtask_notifications'"
        )
    }
    assert "idx_subtask_notifications_pending" in indexes


def test_version_70_old_database_upgrades_to_72(tmp_path: Path):
    """版本 70 的存量库（本轮之前）升级后必须有通知表，且历史版本行不动。"""
    conn = connect(tmp_path / "old70.db")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " id INTEGER PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    for target, statements in MIGRATIONS:
        if target > 70:
            break
        for stmt in statements:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as exc:
                # 与真实迁移执行器一致：对象已存在 = 这条已经应用过（补偿语句允许冗余）。
                message = str(exc).lower()
                if "duplicate column name" in message or "already exists" in message:
                    continue
                raise
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, '2026-10-26T00:00:00+00:00')",
            (target,),
        )
    assert _current(conn) == 70
    recorded_before = _versions(conn)

    assert apply_migrations(conn) == SCHEMA_VERSION
    assert _current(conn) == SCHEMA_VERSION
    tables = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "subtask_notifications" in tables
    assert recorded_before <= _versions(conn), "历史版本行不得被删"


def test_version_recorded_but_table_missing_is_repaired(tmp_path: Path):
    """版本号已到 72、对象却被删：按 REQUIRED_OBJECTS 重放补偿补齐（不只看版本号）。"""
    conn = _db(tmp_path)
    conn.execute("DROP TABLE subtask_notifications")
    assert "subtask_notifications" not in _tables(conn)

    assert apply_migrations(conn) == SCHEMA_VERSION, "版本已到最新时仍要按对象复核并补偿"
    assert "subtask_notifications" in _tables(conn)
    queue = SubtaskNotificationQueue(_manager(), conn=conn)
    assert queue.enqueue("task_repaired", _record("task_repaired")) is True


def _current(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version").fetchone()
    return int(row["v"])


def _versions(conn: sqlite3.Connection) -> set[int]:
    return {int(row["version"]) for row in conn.execute("SELECT version FROM schema_version")}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

# ---------------------------------------------------------------------------
# 6) 接线层：pump 只在 turns.submit 受理后写 delivered_at；失败安排延迟重试；重启恢复
# ---------------------------------------------------------------------------


import types  # noqa: E402


def _app_ctx(tmp_path: Path):
    from agent.services.app import AppContext

    conn = connect(tmp_path / "app_notifications.db")
    apply_migrations(conn)
    return AppContext(Settings(data_dir=tmp_path), conn, EventBus())


def _row(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT delivered_at, attempts, last_error, last_attempt_at"
        " FROM subtask_notifications WHERE task_id = ?",
        (task_id,),
    ).fetchone()


@pytest.mark.asyncio
async def test_pump_writes_delivered_at_only_after_submit_accepted(tmp_path: Path, monkeypatch):
    ctx = _app_ctx(tmp_path)
    assert ctx.subtask_notifications.enqueue("task_pump", _record("task_pump")) is True

    def boom(*_args, **_kwargs):
        raise RuntimeError("turn manager submit boom")

    monkeypatch.setattr(ctx.turns, "submit", boom)
    await ctx._pump_subtask_notifications()
    row = _row(ctx.conn, "task_pump")
    assert row["delivered_at"] is None, "turns.submit 失败时不得写 delivered_at"
    assert row["attempts"] == 1 and row["last_attempt_at"]
    assert "boom" in str(row["last_error"] or ""), row["last_error"]
    assert ctx.subtask_notifications.pending_count() == 1, "失败必须保留待重试"
    assert getattr(ctx, "_notify_retry_task", None) is not None, (
        "投递失败必须安排延迟重试，不能等下一次轮末才想起"
    )
    ctx.cancel_notification_retry()

    delivered: list[tuple[str, str | None, bool]] = []

    def ok(message, topic=None, *, notify=False, request_id=None):
        delivered.append((message, topic, notify))
        return types.SimpleNamespace()

    monkeypatch.setattr(ctx.turns, "submit", ok)
    await ctx._pump_subtask_notifications()
    row = _row(ctx.conn, "task_pump")
    assert row["delivered_at"], "turns.submit 受理后才写 delivered_at"
    assert ctx.subtask_notifications.pending_count() == 0
    assert delivered and delivered[0][2] is True, "通知轮必须以 notify=True 提交"
    assert delivered[0][1] == "topic_1", "投递到原话题（origin.topic_id）"


@pytest.mark.asyncio
async def test_failed_delivery_is_retried_by_scheduled_retry(tmp_path: Path, monkeypatch):
    ctx = _app_ctx(tmp_path)
    ctx.subtask_notification_retry_delay = 0
    ctx.subtask_notifications.enqueue("task_retry", _record("task_retry"))
    calls = {"n": 0}

    def flaky(message, topic=None, *, notify=False, request_id=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first submit fails")
        return types.SimpleNamespace()

    monkeypatch.setattr(ctx.turns, "submit", flaky)
    await ctx._pump_subtask_notifications()
    assert ctx.subtask_notifications.pending_count() == 1
    handle = getattr(ctx, "_notify_retry_task", None)
    assert handle is not None
    await asyncio.wait_for(handle, timeout=2)
    row = _row(ctx.conn, "task_retry")
    assert row["delivered_at"], "延迟重试后必须真正交付"
    assert ctx.subtask_notifications.pending_count() == 0
    ctx.cancel_notification_retry()


@pytest.mark.asyncio
async def test_restart_resume_delivers_undelivered_only(tmp_path: Path):
    ctx = _app_ctx(tmp_path)
    ctx.subtask_notifications.enqueue("task_done", _record("task_done"))
    ctx.subtask_notifications.enqueue("task_left", _record("task_left"))
    item = ctx.subtask_notifications.take()
    assert item is not None and item["task_id"] == "task_done"
    assert ctx.subtask_notifications.mark_delivered(item) is True

    from agent.services.app import AppContext

    restarted = AppContext(Settings(data_dir=tmp_path), ctx.conn, EventBus())
    submitted: list[tuple[str, str | None, bool]] = []

    def ok(message, topic=None, *, notify=False, request_id=None):
        submitted.append((message, topic, notify))
        return types.SimpleNamespace()

    restarted.turns.submit = ok
    pending_before = await restarted.resume_subtask_notifications()
    assert pending_before == 1, f"重启应恢复 1 条未送达（实际 {pending_before}）"
    assert [entry[2] for entry in submitted] == [True]
    assert "task_left" in submitted[0][0]
    assert _row(restarted.conn, "task_left")["delivered_at"]
    assert restarted.subtask_notifications.pending_count() == 0

    # 已交付的 task_done 永不重投
    restarted.subtask_notifications.enqueue("task_left", _record("task_left"))  # 幂等：仍 False
    assert restarted.subtask_notifications.enqueue("task_left", _record("task_left")) is False
    restarted.cancel_notification_retry()

@pytest.mark.asyncio
async def test_pump_uses_deterministic_request_id_and_dedupes_in_process(
    tmp_path: Path, monkeypatch
):
    """确定性 request_id + 进程内查重：已受理但 delivered_at 没落库时不得重复提交。"""
    ctx = _app_ctx(tmp_path)
    ctx.subtask_notifications.enqueue("task_req", _record("task_req"))
    captured: list[dict] = []

    def submit(message, topic=None, *, notify=False, request_id=None):
        captured.append({"notify": notify, "request_id": request_id})
        return types.SimpleNamespace()

    monkeypatch.setattr(ctx.turns, "submit", submit)
    await ctx._pump_subtask_notifications()
    assert captured == [{"notify": True, "request_id": "subtask-notify:task_req"}]
    assert ctx.subtask_notifications.pending_count() == 0

    # 模拟「submit 已受理、但写 delivered_at 失败」：库里仍是未送达
    ctx.conn.execute(
        "UPDATE subtask_notifications SET delivered_at = NULL WHERE task_id = 'task_req'"
    )
    marker = types.SimpleNamespace(turn_id="turn_already_accepted")
    monkeypatch.setattr(
        ctx.turns,
        "lookup_request",
        lambda request_id: marker if request_id == "subtask-notify:task_req" else None,
    )
    await ctx._pump_subtask_notifications()
    assert len(captured) == 1, "同一进程内已受理的通知不得重复提交"
    assert ctx.subtask_notifications.pending_count() == 0, "查重命中后应补记交付"
    assert _row(ctx.conn, "task_req")["delivered_at"]
