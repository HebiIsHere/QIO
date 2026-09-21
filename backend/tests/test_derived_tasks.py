"""派生任务队列：入队幂等、到期认领、失败退避、重启恢复（阶段 2）。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from agent.services import derived_tasks as dt


def _now_iso(offset_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


def test_enqueue_is_idempotent_per_content_version(db_conn):
    first_id, created = dt.enqueue(db_conn, dt.KIND_SUMMARY, "frag_1", 3)
    second_id, created_again = dt.enqueue(db_conn, dt.KIND_SUMMARY, "frag_1", 3)

    assert created is True and created_again is False
    assert first_id == second_id
    assert db_conn.execute("SELECT COUNT(*) c FROM derived_tasks").fetchone()["c"] == 1


def test_new_content_version_creates_a_new_task(db_conn):
    dt.enqueue(db_conn, dt.KIND_SUMMARY, "frag_1", 3)
    dt.enqueue(db_conn, dt.KIND_SUMMARY, "frag_1", 4)
    assert db_conn.execute("SELECT COUNT(*) c FROM derived_tasks").fetchone()["c"] == 2


def test_claim_due_marks_running_and_respects_backoff(db_conn):
    task_id, _ = dt.enqueue(db_conn, dt.KIND_SUMMARY, "frag_1", 1)

    claimed = dt.claim_due(db_conn)
    assert [t.id for t in claimed] == [task_id]
    assert dt.task_for(db_conn, dt.KIND_SUMMARY, "frag_1", 1).state == dt.STATE_RUNNING
    # 已被认领的任务不会被第二次认领
    assert dt.claim_due(db_conn) == []

    dt.fail(db_conn, task_id, "模型超时")
    task = dt.task_for(db_conn, dt.KIND_SUMMARY, "frag_1", 1)
    assert task.state == dt.STATE_FAILED
    assert task.attempts == 1
    assert task.last_error == "模型超时"
    # 退避期间不再被认领
    assert dt.claim_due(db_conn) == []


def test_failed_task_becomes_claimable_after_run_after(db_conn):
    task_id, _ = dt.enqueue(db_conn, dt.KIND_SUMMARY, "frag_1", 1)
    dt.claim_due(db_conn)
    dt.fail(db_conn, task_id, "第一次失败")
    # 把退避时间拨到过去：模拟「时间到了」
    db_conn.execute(
        "UPDATE derived_tasks SET run_after = ? WHERE id = ?", (_now_iso(-1), task_id)
    )

    claimed = dt.claim_due(db_conn)
    assert [t.id for t in claimed] == [task_id]


def test_backoff_grows_and_is_capped():
    delays = [dt.backoff_delay(n) for n in range(1, 9)]
    assert delays[0] < delays[1] < delays[2]
    assert delays[-1] == delays[-2] == 3600  # 上限 1 小时，不会无限增长


def test_complete_takes_task_out_of_the_queue(db_conn):
    task_id, _ = dt.enqueue(db_conn, dt.KIND_SUMMARY, "frag_1", 1)
    dt.claim_due(db_conn)
    dt.complete(db_conn, task_id)

    assert dt.task_for(db_conn, dt.KIND_SUMMARY, "frag_1", 1).state == dt.STATE_COMPLETED
    assert dt.claim_due(db_conn) == []
    assert dt.pending_count(db_conn) == 0


def test_recover_stale_running_tasks_after_restart(db_conn):
    task_id, _ = dt.enqueue(db_conn, dt.KIND_SUMMARY, "frag_1", 1)
    dt.claim_due(db_conn)  # 进入 running，然后「进程被杀」
    db_conn.execute(
        "UPDATE derived_tasks SET updated_at = ? WHERE id = ?", (_now_iso(-3600), task_id)
    )

    recovered = dt.recover_stale(db_conn)

    assert recovered == 1
    assert dt.task_for(db_conn, dt.KIND_SUMMARY, "frag_1", 1).state == dt.STATE_PENDING
    assert [t.id for t in dt.claim_due(db_conn)] == [task_id]


def test_recover_stale_leaves_fresh_running_alone(db_conn):
    dt.enqueue(db_conn, dt.KIND_SUMMARY, "frag_1", 1)
    dt.claim_due(db_conn)
    assert dt.recover_stale(db_conn) == 0


def test_claim_can_filter_by_kind(db_conn):
    dt.enqueue(db_conn, dt.KIND_SUMMARY, "frag_1", 1)
    index_id, _ = dt.enqueue(db_conn, dt.KIND_INDEX, "frag_1", 1)

    claimed = dt.claim_due(db_conn, kinds=(dt.KIND_INDEX,))

    assert [t.id for t in claimed] == [index_id]
    assert dt.task_for(db_conn, dt.KIND_SUMMARY, "frag_1", 1).state == dt.STATE_PENDING
