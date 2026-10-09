"""R05：派生任务的实例归属、认领代次与迟到结果丢弃（受控时钟，不真实等待）。

验收覆盖（逐条）：
* 任务刚认领就重启（未超 300s）→ 后续 drain/claim 能恢复；
* 另一活实例正在跑的任务不被抢；
* unknown（判不出来）一律不改状态；
* 迟到结果（旧 generation）不覆盖新认领；
* 退避与内容版本等既有语义保留。

全部用受控时钟 + 注入的存活判定替身：不 sleep 300 秒、不重启进程。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent.services import derived_tasks as dt
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

CLOCK_START = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)


class _Clock:
    def __init__(self) -> None:
        self.now = CLOCK_START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class _Registry:
    """最小存活判定替身：只回答「这个实例现在算不算活着」。

    `alive={"inst_A": False}` → 确认已退出；缺键 → None（unknown）。
    """

    def __init__(self, **alive: bool | None) -> None:
        self._alive = alive

    def owner_alive(self, instance_id: str) -> bool | None:
        return self._alive.get(instance_id)


@pytest.fixture(autouse=True)
def _reset_instance_binding():
    try:
        yield
    finally:
        dt.bind_instance(None, None)


@pytest.fixture()
def clock():
    extra = _Clock()
    dt.set_clock(extra)
    try:
        yield extra
    finally:
        dt.reset_clock()


@pytest.fixture()
def conn(tmp_path):
    connection = connect(tmp_path / "rm_c_r05.db")
    apply_migrations(connection)
    _ensure_derived_ownership_columns(connection)
    try:
        yield connection
    finally:
        connection.close()


def _ensure_derived_ownership_columns(connection) -> None:
    """A 的迁移会补这两列 + record_owners 表；本组测试自带幂等补齐，保证验收可独立跑。"""
    cols = {row["name"] for row in connection.execute("PRAGMA table_info(derived_tasks)")}
    if "owner_instance_id" not in cols:
        connection.execute("ALTER TABLE derived_tasks ADD COLUMN owner_instance_id TEXT")
    if "claim_generation" not in cols:
        connection.execute(
            "ALTER TABLE derived_tasks ADD COLUMN claim_generation INTEGER NOT NULL DEFAULT 0"
        )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS record_owners ("
        "record_type TEXT NOT NULL, record_id TEXT NOT NULL, instance_id TEXT NOT NULL, "
        "PRIMARY KEY (record_type, record_id))"
    )


def _task(conn, fragment_id: str = "frag_x", version: int = 1) -> dt.DerivedTask:
    task = dt.task_for(conn, dt.KIND_SUMMARY, fragment_id, version)
    assert task is not None
    return task


def test_claim_writes_owner_and_generation(conn):
    task_id, created = dt.enqueue(conn, dt.KIND_SUMMARY, "frag_x", 1)
    assert created is True

    claimed = dt.claim_due(conn, instance_id="inst_A")

    assert [t.id for t in claimed] == [task_id]
    task = _task(conn)
    assert task.state == dt.STATE_RUNNING
    assert task.owner_instance_id == "inst_A"
    assert task.claim_generation == 1


def test_restart_within_timeout_recovers_the_claim(conn, clock):
    """刚认领（1 秒前）就重启：归属者确认已退出 → 恢复，而不是等满 300 秒。"""
    dt.enqueue(conn, dt.KIND_SUMMARY, "frag_x", 1)
    dt.claim_due(conn, instance_id="inst_A")
    clock.advance(1)
    dead = _Registry(inst_A=False)

    recovered = dt.recover_stale(conn, instance_id="inst_B", registry=dead)

    assert recovered == 1
    assert _task(conn).state == dt.STATE_PENDING
    reclaimed = dt.claim_due(conn, instance_id="inst_B", registry=dead)
    assert [t.id for t in reclaimed] == [_task(conn).id]
    assert reclaimed[0].owner_instance_id == "inst_B"
    assert reclaimed[0].claim_generation == 2, "重新认领必须递增代次"


def test_claim_due_also_reconciles_stale_running(conn, clock):
    """恢复核对不只在启动一次：claim_due 自己就会顺带核对 running 行。"""
    task_id, _ = dt.enqueue(conn, dt.KIND_SUMMARY, "frag_x", 1)
    dt.claim_due(conn, instance_id="inst_A")
    clock.advance(1)
    dead = _Registry(inst_A=False)

    claimed = dt.claim_due(conn, instance_id="inst_B", registry=dead)

    assert [t.id for t in claimed] == [task_id], "claim_due 应当先把死掉实例的任务收回"
    assert claimed[0].claim_generation == 2


def test_live_owner_task_is_not_stolen(conn, clock):
    """另一活实例正在跑的任务不被抢：确认活着 → 不改状态、不可认领。"""
    dt.enqueue(conn, dt.KIND_SUMMARY, "frag_x", 1)
    dt.claim_due(conn, instance_id="inst_A")
    clock.advance(3600)  # 就算超期，活着就不动
    alive = _Registry(inst_A=True)

    assert dt.recover_stale(conn, instance_id="inst_B", registry=alive) == 0
    assert _task(conn).state == dt.STATE_RUNNING
    assert dt.claim_due(conn, instance_id="inst_B", registry=alive) == []


def test_unknown_owner_is_left_untouched(conn, clock):
    """unknown（判不出来）一律不改状态 —— 包括超期的情况。"""
    dt.enqueue(conn, dt.KIND_SUMMARY, "frag_x", 1)
    dt.claim_due(conn, instance_id="inst_A")
    clock.advance(7200)
    unknown = _Registry(inst_A=None)

    assert dt.recover_stale(conn, instance_id="inst_B", registry=unknown) == 0

    task = _task(conn)
    assert task.state == dt.STATE_RUNNING
    assert task.owner_instance_id == "inst_A"


def test_late_result_does_not_override_a_new_claim(conn, clock):
    """迟到结果（旧 generation）不覆盖新认领：完成/失败都被丢弃。"""
    task_id, _ = dt.enqueue(conn, dt.KIND_SUMMARY, "frag_x", 1)
    dt.claim_due(conn, instance_id="inst_A")  # generation = 1
    clock.advance(1)
    dead = _Registry(inst_A=False)
    dt.recover_stale(conn, instance_id="inst_B", registry=dead)
    dt.claim_due(conn, instance_id="inst_B", registry=dead)  # generation = 2

    assert dt.complete(conn, task_id, expected_generation=1) is False, "迟到的完成必须被丢弃"
    assert dt.fail(conn, task_id, "迟到的失败", expected_generation=1) is False
    task = _task(conn)
    assert task.state == dt.STATE_RUNNING and task.attempts == 0
    assert task.owner_instance_id == "inst_B" and task.claim_generation == 2

    assert dt.complete(conn, task_id, expected_generation=2) is True
    assert _task(conn).state == dt.STATE_COMPLETED


def test_backoff_is_preserved_with_generations(conn, clock):
    """保留退避语义：失败后要等 run_after 到点才能再认领。"""
    task_id, _ = dt.enqueue(conn, dt.KIND_SUMMARY, "frag_x", 1)
    claimed = dt.claim_due(conn, instance_id="inst_A")
    assert claimed[0].claim_generation == 1

    assert dt.fail(
        conn, task_id, "模型超时", expected_generation=claimed[0].claim_generation
    ) is True
    task = _task(conn)
    assert task.state == dt.STATE_FAILED and task.attempts == 1
    assert task.run_after is not None
    assert dt.claim_due(conn, instance_id="inst_A") == [], "退避期间不该被认领"

    clock.advance(dt.backoff_delay(1))
    again = dt.claim_due(conn, instance_id="inst_A")
    assert [t.id for t in again] == [task_id]
    assert again[0].claim_generation == 2


def test_legacy_running_row_without_owner_falls_back_to_timeout(conn, clock):
    """旧记录没有归属（未接线实例）：明确超期后按时限兜底放回，保留原行为。"""
    dt.enqueue(conn, dt.KIND_SUMMARY, "frag_x", 1)
    dt.claim_due(conn)  # 不传 instance_id：owner 为 NULL
    assert _task(conn).owner_instance_id is None

    clock.advance(dt._STALE_RUNNING_SECONDS + 1)
    assert dt.recover_stale(conn) == 1
    assert _task(conn).state == dt.STATE_PENDING


def test_release_requires_matching_generation(conn, clock):
    """取消/关闭的释放也过代次校验：迟到释放不动新认领。"""
    task_id, _ = dt.enqueue(conn, dt.KIND_SUMMARY, "frag_x", 1)
    dt.claim_due(conn, instance_id="inst_A")
    assert dt.release(conn, task_id, expected_generation=0) is False
    assert _task(conn).state == dt.STATE_RUNNING

    assert dt.release(conn, task_id, expected_generation=1) is True
    task = _task(conn)
    assert task.state == dt.STATE_PENDING and task.run_after is None
    assert task.attempts == 0, "取消不是失败，不该递增 attempts"


def test_completion_requires_the_bound_owner(conn):
    """所有者 + 代次双校验（M06）：别的实例拿着同一个代次也不能终结这条任务。"""
    task_id, _ = dt.enqueue(conn, dt.KIND_SUMMARY, "frag_x", 1)
    dt.claim_due(conn, instance_id="inst_A")

    dt.bind_instance("inst_B", None)
    assert dt.complete(conn, task_id, expected_generation=1) is False
    assert dt.fail(conn, task_id, "另一个实例的失败", expected_generation=1) is False
    assert dt.release(conn, task_id, expected_generation=1) is False
    task = _task(conn)
    assert task.state == dt.STATE_RUNNING and task.attempts == 0
    assert task.owner_instance_id == "inst_A"

    dt.bind_instance("inst_A", None)
    assert dt.complete(conn, task_id, expected_generation=1) is True
    assert _task(conn).state == dt.STATE_COMPLETED


def test_claim_mirrors_record_owners_and_clears_it(conn, clock):
    """契约 C1：record_owners 也覆盖派生任务；认领写、终结/回收时清。"""
    task_id, _ = dt.enqueue(conn, dt.KIND_SUMMARY, "frag_x", 1)
    claimed = dt.claim_due(conn, instance_id="inst_A")
    assert dt.owned_task_ids(conn, "inst_A") == [task_id]

    assert dt.complete(conn, task_id, expected_generation=claimed[0].claim_generation)
    assert dt.owned_task_ids(conn, "inst_A") == []

    dt.enqueue(conn, dt.KIND_SUMMARY, "frag_y", 1)
    again = dt.claim_due(conn, instance_id="inst_A")
    assert dt.owned_task_ids(conn, "inst_A") == [again[0].id]
    clock.advance(1)
    assert dt.recover_stale(conn, instance_id="inst_B", registry=_Registry(inst_A=False)) == 1
    assert dt.owned_task_ids(conn, "inst_A") == [], "回收后旧归属行不得留着"


def test_release_running_scopes_to_one_instance(conn):
    """干净退出兜底：只释放本实例名下的 running。"""
    dt.enqueue(conn, dt.KIND_SUMMARY, "frag_mine", 1)
    dt.enqueue(conn, dt.KIND_SUMMARY, "frag_other", 1)
    dt.claim_due(conn, instance_id="inst_A", kinds=(dt.KIND_SUMMARY,))
    # 第二条归另一实例（模拟库里有另一个活实例）
    conn.execute(
        "UPDATE derived_tasks SET owner_instance_id = ? WHERE fragment_id = ?",
        ("inst_B", "frag_other"),
    )

    released = dt.release_running(conn, instance_id="inst_A", reason="干净退出")

    assert released == 1
    assert dt.task_for(conn, dt.KIND_SUMMARY, "frag_mine", 1).state == dt.STATE_PENDING
    assert dt.task_for(conn, dt.KIND_SUMMARY, "frag_other", 1).state == dt.STATE_RUNNING
