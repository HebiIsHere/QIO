# -*- coding: utf-8 -*-
"""F 独立验收（基线 373fda40）· 反例 5/5：子任务通知容量与可靠投递。

契约（COMMON §4.4）：
- 新增只追加迁移 71：表字段至少 task_id 主键、payload(JSON)、created_at、delivered_at、
  attempts、last_error、origin(JSON)；
- enqueue 只做持久化去重（同一 task_id 不重复），**不得**把入队当交付成功；
- 超过 200 条不再静默丢弃最早项；
- pump 取最早未送达；只有通知轮被 turn manager 接受后才写 delivered_at；
  失败记录 attempts/last_error 并保留待重试，不丢；重启时加载未送达继续投递；
- 同一 task_id 最多投递一次；FIFO。

基线预期（红）：
- 队列纯内存（deque），超过 200 条时 popleft 静默丢最早项；
- 没有迁移 71 / subtask_notifications 表（delivered_at/attempts/last_error/origin 都不存在）；
- 重启后新队列为空，未送达通知全部丢失。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.api.bus import EventBus
from agent.storage.migrate import current_version
from agent.storage.schema import MIGRATIONS
from agent.tools.subtask_notifications import (
    MAX_PENDING_NOTIFICATIONS,
    SubtaskNotificationQueue,
)
from agent.tools.task_manager import TaskManager

REQUIRED_COLUMNS = {
    "task_id",
    "payload",
    "created_at",
    "delivered_at",
    "attempts",
    "last_error",
    "origin",
}


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


def _tables(conn) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


# --- 1) 容量：超过 200 条不得静默丢弃最早项 -------------------------------------


def test_250_notifications_are_not_silently_dropped(db_conn):
    queue = SubtaskNotificationQueue(_manager())
    for index in range(250):
        queue.enqueue(f"task_fu2_{index}", _record(f"task_fu2_{index}"))

    assert queue.pending_count() == 250, (
        "入队 250 条后队列只剩 "
        + str(queue.pending_count())
        + " 条（上限 "
        + str(MAX_PENDING_NOTIFICATIONS)
        + " 把最早的通知静默丢掉了）"
    )
    pending = queue.pending()
    assert pending[0]["task_id"] == "task_fu2_0", "FIFO 最早的一条必须还在"
    assert pending[-1]["task_id"] == "task_fu2_249"


# --- 2) 持久化：迁移 71 与表结构 ------------------------------------------------


def test_migration_71_creates_notification_table(db_conn):
    assert any(int(number) == 71 for number, _ in MIGRATIONS), (
        "没有登记只追加迁移 71（子任务通知持久化）"
    )
    assert current_version(db_conn) >= 71, "迁移 71 没有应用"
    assert "subtask_notifications" in _tables(db_conn), "缺少 subtask_notifications 表"
    columns = {
        str(row[1]) for row in db_conn.execute("PRAGMA table_info(subtask_notifications)")
    }
    missing = REQUIRED_COLUMNS - columns
    assert not missing, "subtask_notifications 缺少契约字段：" + str(sorted(missing))


# --- 3) 入队即持久化（未送达），重启后继续投递 ----------------------------------


def test_enqueue_persists_undelivered_and_survives_restart(db_conn):
    first = SubtaskNotificationQueue(_manager(), conn=db_conn)
    assert first.enqueue("task_durable_1", _record("task_durable_1"))
    assert first.enqueue("task_durable_2", _record("task_durable_2"))
    # take 只表示「取出一条去投递」，不是交付成功：此时仍未送达
    first_item = first.take()
    assert first_item is not None
    first.requeue(first_item)

    rows = {
        str(row["task_id"]): dict(row)
        for row in db_conn.execute("SELECT * FROM subtask_notifications")
    }
    assert set(rows) == {"task_durable_1", "task_durable_2"}, (
        "入队没有持久化（或 take 后被当成已交付）：" + str(sorted(rows))
    )
    for task_id, row in rows.items():
        assert row.get("delivered_at") is None, task_id + " 尚未交付就被写了 delivered_at"
        assert str(row.get("payload") or "").strip(), task_id + " 的 payload 为空"
        assert str(row.get("origin") or "").strip(), task_id + " 的 origin 为空"

    # 模拟重启：同一数据库上新建队列（新的 manager = 新进程）
    restarted = SubtaskNotificationQueue(_manager(), conn=db_conn)
    assert restarted.pending_count() == 2, (
        "重启后未送达通知丢失：pending=" + str(restarted.pending_count())
    )
    restored = restarted.take()
    assert restored is not None and str(restored.get("task_id")) == "task_durable_1", "重启后 FIFO 顺序不对"


# --- 4) 跨重启去重：同一 task_id 最多投递一次 ----------------------------------


def test_duplicate_task_id_is_deduplicated_across_restart(db_conn):
    queue = SubtaskNotificationQueue(_manager(), conn=db_conn)
    assert queue.enqueue("task_dup", _record("task_dup")) is True
    assert queue.enqueue("task_dup", _record("task_dup")) is False, "同一队列内重复入队未被去重"

    restarted = SubtaskNotificationQueue(_manager(), conn=db_conn)
    assert restarted.enqueue("task_dup", _record("task_dup")) is False, (
        "重启后同一 task_id 又被当成新通知（持久化去重失效）"
    )
    rows = db_conn.execute(
        "SELECT COUNT(*) FROM subtask_notifications WHERE task_id = 'task_dup'"
    ).fetchone()
    assert int(rows[0]) == 1


# --- 5) 保留：FIFO 与取消/非通知终态过滤（基线已成立，修复不得破坏） -------------


def test_fifo_and_terminal_filter_are_preserved(db_conn):
    queue = SubtaskNotificationQueue(_manager())
    for task_id in ("task_a", "task_b", "task_c"):
        queue.enqueue(task_id, _record(task_id))
    assert [item["task_id"] for item in queue.pending()] == ["task_a", "task_b", "task_c"]
    assert queue.enqueue("task_cancelled", SimpleNamespace(
        task_id="task_cancelled", outcome="cancelled", cancelled=True,
        result=SimpleNamespace(ok=False, error="cancelled", content=""),
        tool="research", status="cancelled", total_chars=0, origin={}, finished_at=None,
        full_content="",
    )) is False, "已取消的任务不应入队"
