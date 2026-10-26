# -*- coding: utf-8 -*-
"""F 独立验收 · 反例 5/5（按 D-CP1 实际 API 调整）：子任务通知容量与可靠投递。

契约（COMMON §4.4）：
- 只追加迁移 71：subtask_notifications(task_id PK, payload, origin, created_at,
  delivered_at, attempts, last_error, last_attempt_at) + idx_subtask_notifications_pending；
  72 为补偿入口；SCHEMA_VERSION = COMPENSATION_VERSION；
- enqueue 只做持久化去重，不把入队当交付；超过 200 条只告警不丢最早；
- 交付唯一判据是 delivered_at：只有通知轮被 TurnManager 接受（turns.submit 成功）才写；
  失败记录 attempts/last_error/last_attempt_at 并保留待重试；
- 重启加载未送达继续投递；同一 task_id 最多投递一次；FIFO。

基线（373fda40）证据：纯内存 deque + 200 上限静默丢最早、无迁移 71/表、无持久化
（旧构造 SubtaskNotificationQueue(manager) 没有 conn，重启丢失）。基线红日志保留在
evidence/F/baseline/item5-notifications.log；本文件按 D-CP1 的
SubtaskNotificationQueue(manager, *, conn=None, max_pending=200) 与
enqueue/take/mark_delivered/mark_failed/pending/pending_count 实际 API 编写。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent.api.bus import EventBus
from agent.storage.migrate import current_version
from agent.storage.schema import COMPENSATION_VERSION, MIGRATIONS, SCHEMA_VERSION
from agent.tools.subtask_notifications import (
    MAX_PENDING_NOTIFICATIONS,
    NOTIFICATION_COLUMNS,
    NOTIFICATION_TABLE,
    SubtaskNotificationQueue,
)
from agent.tools.task_manager import TaskManager

REQUIRED_COLUMNS = set(NOTIFICATION_COLUMNS)


def _manager() -> TaskManager:
    return TaskManager(EventBus(), max_concurrent=2)


def _record(task_id: str, *, outcome: str = "completed") -> SimpleNamespace:
    """最小 TaskRecord 替身：只提供 build_notification 需要的事实。"""
    return SimpleNamespace(
        task_id=task_id,
        tool="research",
        status="done",
        outcome=outcome,
        cancelled=False,
        full_content="结果正文",
        total_chars=4,
        origin={"turn_id": "turn_origin", "topic_id": "topic_origin"},
        finished_at="2026-10-26T00:00:00+00:00",
        result=SimpleNamespace(ok=True, error=None, content="结果正文"),
    )


def _queue(conn=None, **kwargs) -> SubtaskNotificationQueue:
    return SubtaskNotificationQueue(_manager(), conn=conn, **kwargs)


def _row(conn, task_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM subtask_notifications WHERE task_id = ?", (task_id,)
    ).fetchone()
    return dict(row) if row is not None else {}


# --- 1) 容量：超过 200 条不得静默丢弃最早项（持久化与内存形态都不丢） ------------


def test_250_notifications_are_not_silently_dropped(db_conn):
    for queue in (_queue(db_conn), _queue()):
        for index in range(250):
            assert queue.enqueue(f"task_fu2_{index}", _record(f"task_fu2_{index}"))
        assert queue.pending_count() == 250, (
            "入队 250 条后只剩 "
            + str(queue.pending_count())
            + " 条（告警阈值 "
            + str(MAX_PENDING_NOTIFICATIONS)
            + " 被误当成容量上限，最早的通知被丢弃）"
        )
        pending = queue.pending()
        assert pending[0]["task_id"] == "task_fu2_0", "FIFO 最早的一条必须还在"
        assert pending[-1]["task_id"] == "task_fu2_249"


# --- 2) 迁移 71/72 与表结构 ------------------------------------------------------


def test_migration_71_and_compensation_72_create_table(db_conn):
    numbers = {int(number) for number, _ in MIGRATIONS}
    assert 71 in numbers, "没有登记只追加迁移 71（子任务通知持久化）"
    assert 72 in numbers, "没有补偿入口迁移 72"
    assert SCHEMA_VERSION == COMPENSATION_VERSION, (
        "SCHEMA_VERSION 与 COMPENSATION_VERSION 不一致："
        + str(SCHEMA_VERSION) + " vs " + str(COMPENSATION_VERSION)
    )
    assert current_version(db_conn) >= 72, "迁移 71/72 没有应用"
    tables = {
        str(row[0])
        for row in db_conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert NOTIFICATION_TABLE in tables, "缺少 " + NOTIFICATION_TABLE + " 表"
    columns = {
        str(row[1])
        for row in db_conn.execute(f"PRAGMA table_info({NOTIFICATION_TABLE})")
    }
    missing = REQUIRED_COLUMNS - columns
    assert not missing, NOTIFICATION_TABLE + " 缺少契约字段：" + str(sorted(missing))
    indexes = {
        str(row[0])
        for row in db_conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    assert "idx_subtask_notifications_pending" in indexes, (
        "缺少 idx_subtask_notifications_pending（按 delivered_at/created_at 取最早未送达）"
    )


# --- 3) 入队即持久化；take 只登记尝试；只有 mark_delivered 才是交付 ---------------


def test_delivery_fact_is_only_delivered_at_and_restart_resumes(db_conn):
    queue = _queue(db_conn)
    for task_id in ("task_durable_1", "task_durable_2", "task_durable_3"):
        assert queue.enqueue(task_id, _record(task_id))
    assert queue.pending_count() == 3

    first = queue.take()
    assert first is not None and first["task_id"] == "task_durable_1", "take 必须 FIFO 取最早"
    # take 不是交付：attempts+1，但 delivered_at 仍为空
    row = _row(db_conn, "task_durable_1")
    assert row.get("delivered_at") is None, "take 就把 delivered_at 写了（把取件当成交付）"
    assert int(row.get("attempts") or 0) == 1, "take 没有登记一次投递尝试"
    assert queue.pending_count() == 3, "取件不应让通知从待投递里消失（失败要能重试）"

    assert queue.mark_delivered(first) is True
    row = _row(db_conn, "task_durable_1")
    assert row.get("delivered_at"), "mark_delivered 没有写交付事实"
    assert queue.pending_count() == 2, "已交付通知仍在待投递里"

    # 重启：同一数据库上新建队列（新进程 = 新 manager），未送达继续投递
    restarted = _queue(db_conn)
    assert restarted.pending_count() == 2, "重启后未送达通知没有恢复"
    restored = restarted.take()
    assert restored is not None and restored["task_id"] == "task_durable_2", "重启后 FIFO 顺序不对"
    assert restarted.mark_delivered(restored) is True
    assert restarted.pending_count() == 1


# --- 4) 失败保留 attempts/last_error（不丢、可重试） ----------------------------


def test_failed_delivery_keeps_record_with_attempts_and_error(db_conn):
    queue = _queue(db_conn)
    assert queue.enqueue("task_fail_1", _record("task_fail_1"))
    item = queue.take()
    assert item is not None and item["task_id"] == "task_fail_1"
    assert queue.mark_failed(item, "RuntimeError: turn submit failed: boom") is True

    row = _row(db_conn, "task_fail_1")
    assert row.get("delivered_at") is None, "投递失败却写了 delivered_at（把失败当交付）"
    assert int(row.get("attempts") or 0) >= 1, "失败没有记录 attempts"
    assert "boom" in str(row.get("last_error") or ""), "失败没有记录 last_error"
    assert row.get("last_attempt_at"), "失败没有记录 last_attempt_at"
    assert queue.pending_count() == 1, "失败后通知被丢弃（应保留待重试）"

    # 重启后仍可重试：新队列（新进程）取出同一条并成功交付
    restarted = _queue(db_conn)
    retry = restarted.take()
    assert retry is not None and retry["task_id"] == "task_fail_1"
    assert int(_row(db_conn, "task_fail_1").get("attempts") or 0) >= 2, "重试没有累计 attempts"
    assert restarted.mark_delivered(retry) is True
    assert restarted.pending_count() == 0


# --- 5) 同一 task_id 最多投递一次（含跨重启与已交付后） -------------------------


def test_duplicate_task_id_is_deduplicated_across_restart(db_conn):
    queue = _queue(db_conn)
    assert queue.enqueue("task_dup", _record("task_dup")) is True
    assert queue.enqueue("task_dup", _record("task_dup")) is False, "同一队列内重复入队未被去重"

    restarted = _queue(db_conn)
    assert restarted.enqueue("task_dup", _record("task_dup")) is False, (
        "重启后同一 task_id 又被当成新通知（持久化去重失效）"
    )
    count = db_conn.execute(
        f"SELECT COUNT(*) FROM {NOTIFICATION_TABLE} WHERE task_id = 'task_dup'"
    ).fetchone()[0]
    assert int(count) == 1

    delivered = restarted.take()
    assert delivered is not None and restarted.mark_delivered(delivered)
    assert restarted.enqueue("task_dup", _record("task_dup")) is False, "已交付的通知被重新入队"


# --- 6) 保留：FIFO 与取消/非通知终态过滤 ----------------------------------------


def test_fifo_and_terminal_filter_are_preserved(db_conn):
    queue = _queue(db_conn)
    for task_id in ("task_a", "task_b", "task_c"):
        queue.enqueue(task_id, _record(task_id))
    assert [item["task_id"] for item in queue.pending()] == ["task_a", "task_b", "task_c"]
    cancelled = SimpleNamespace(
        task_id="task_cancelled",
        outcome="cancelled",
        cancelled=True,
        result=SimpleNamespace(ok=False, error="cancelled", content=""),
        tool="research",
        status="cancelled",
        total_chars=0,
        origin={},
        finished_at=None,
        full_content="",
    )
    assert queue.enqueue("task_cancelled", cancelled) is False, "已取消的任务不应入队"
    assert queue.pending_count() == 3
