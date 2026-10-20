# -*- coding: utf-8 -*-
"""P2-A / 调度器：触发判定 / 快照 / 单轮互斥 / 失败退避 / 跨重启累计。

覆盖简报里的定向验收点：
* 跨重启累计（变化计数 / 等待轮次 / 处理位置保留）；
* 新变化不重置较早内容的等待轮数；
* 没触发不调用 runner（fake 计数为 0、turn_seq 不增长）；
* 20 条触发 / 30 轮触发 / 配置可改阈值 / 单次最多 50 条；
* 快照期间新增变化留到下一批（处理位置只到快照上界）；
* 维护自身来源不计数、不形成循环触发；
* 失败保留位置 + 退避期间不再调用 runner；同一时间只运行一轮；busy=True 跳过；
* running 过期回收；用户 turn 按 turn_id 去重。

模型一律 fake（FakeRunner），不联网、不使用真实 API Key。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from agent.maintenance.models import MaintenanceSnapshot, MaintenanceTrigger
from agent.maintenance.scheduler import KnowledgeMaintenanceScheduler
from agent.storage.db import connect

from test_p2_a_support import (
    FakeClock,
    FakeRunner,
    FakeSettings,
    add_changes,
    add_confirmed_turns,
    change_seq,
    migrated_conn,
    outcome_failed,
    outcome_ok,
    pending_rows,
    state_row,
)


def _scheduler(conn, *, runner=None, settings=None, clock=None):
    return KnowledgeMaintenanceScheduler(
        conn,
        runner=runner if runner is not None else FakeRunner(),
        settings=settings,
        clock=clock,
    )


async def _wait_for_call(runner: FakeRunner, timeout: float = 5.0) -> None:
    """等 fake runner 真的被调用（避免死等到超时）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while not runner.calls:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("runner 未被调用")
        await asyncio.sleep(0.005)


# ---------------------------------------------------------------------------
# 默认值 / 状态视图 / 等待轮次
# ---------------------------------------------------------------------------


def test_defaults_match_contract(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        scheduler = _scheduler(conn)
        assert scheduler.enabled is True
        assert scheduler.change_threshold == 20
        assert scheduler.waiting_turns_threshold == 30
        assert scheduler.max_changes_per_run == 50
        assert scheduler.retry_cooldown_turns == 5
        assert scheduler.check_interval_seconds == 60
        state = scheduler.pending_state()
        assert state["pending_change_count"] == 0
        assert state["waiting_turns"] == 0
        assert state["turn_seq"] == 0
        assert state["last_processed_seq"] == 0
        assert state["batch_seq"] == 0
        assert state["running"] is False
        assert state["trigger"] is None
        assert state["retry_blocked"] is False
        assert state["check_interval_seconds"] == 60
    finally:
        conn.close()


def test_settings_override_thresholds_and_interval(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        settings = FakeSettings(
            {
                "knowledge_maintenance.enabled": "false",
                "knowledge_maintenance.change_threshold": "7",
                "knowledge_maintenance.waiting_turns": "3",
                "knowledge_maintenance.max_changes_per_run": "9",
                "knowledge_maintenance.retry_cooldown_turns": "2",
                "knowledge_maintenance.check_interval_seconds": "15",
            }
        )
        scheduler = _scheduler(conn, settings=settings)
        assert scheduler.enabled is False
        assert scheduler.change_threshold == 7
        assert scheduler.waiting_turns_threshold == 3
        assert scheduler.max_changes_per_run == 9
        assert scheduler.retry_cooldown_turns == 2
        assert scheduler.check_interval_seconds == 15
    finally:
        conn.close()


async def test_no_pending_changes_do_not_accumulate_waiting_turns(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        runner = FakeRunner()
        scheduler = _scheduler(conn, runner=runner)
        add_confirmed_turns(conn, scheduler, 50)
        assert int(state_row(conn)["turn_seq"]) == 0
        result = await scheduler.maybe_run()
        assert result["ran"] is False
        assert result["reason"] == "no_trigger"
        assert runner.calls == []
        # 没有待处理变化时新增的变化 waiting=0（之前的 50 轮不算等待）。
        add_changes(conn, 1)
        state = scheduler.pending_state()
        assert state["pending_change_count"] == 1
        assert state["waiting_turns"] == 0
        result = await scheduler.maybe_run()
        assert result["ran"] is False and result["reason"] == "no_trigger"
        assert runner.calls == []
    finally:
        conn.close()


async def test_turn_id_is_deduplicated(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        scheduler = _scheduler(conn)
        add_changes(conn, 1)
        scheduler.note_user_turn_completed("turn-1")
        scheduler.note_user_turn_completed("turn-1")
        scheduler.note_user_turn_completed("  turn-1  ")
        assert int(state_row(conn)["turn_seq"]) == 1
        scheduler.note_user_turn_completed("turn-2")
        assert int(state_row(conn)["turn_seq"]) == 2
        scheduler.note_user_turn_completed("")
        assert int(state_row(conn)["turn_seq"]) == 2
    finally:
        conn.close()


async def test_waiting_turns_use_oldest_change_not_newest(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        scheduler = _scheduler(conn)
        add_changes(conn, 1)  # created_turn_seq = 0
        add_confirmed_turns(conn, scheduler, 3)  # turn_seq = 3
        add_changes(conn, 1, start=2)  # created_turn_seq = 3（新变化）
        add_confirmed_turns(conn, scheduler, 1, prefix="more")  # turn_seq = 4
        state = scheduler.pending_state()
        # 按最早一条（0）算 = 4，而不是按最新加入的（3）算 = 1。
        assert state["waiting_turns"] == 4
        assert state["pending_change_count"] == 2
    finally:
        conn.close()


async def test_new_change_does_not_reset_earlier_waiting_for_trigger(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        settings = FakeSettings({"knowledge_maintenance.waiting_turns": "4"})
        runner = FakeRunner()
        scheduler = _scheduler(conn, runner=runner, settings=settings)
        add_changes(conn, 1)
        add_confirmed_turns(conn, scheduler, 4)  # 等待 4 轮
        add_changes(conn, 1, start=2)  # 新变化不重置
        result = await scheduler.maybe_run()
        assert result["ran"] is True
        snapshot = runner.calls[0]
        assert snapshot.trigger is MaintenanceTrigger.WAITING_TURNS
        assert snapshot.waiting_turns == 4
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 触发阈值 / 快照
# ---------------------------------------------------------------------------


async def test_change_count_threshold_20_triggers(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        runner = FakeRunner()
        scheduler = _scheduler(conn, runner=runner)
        add_changes(conn, 20)
        result = await scheduler.maybe_run()
        assert result["ran"] is True and result["ok"] is True
        snapshot = runner.calls[0]
        assert isinstance(snapshot, MaintenanceSnapshot)
        assert snapshot.trigger is MaintenanceTrigger.CHANGE_COUNT
        assert snapshot.pending_change_count == 20
        assert len(snapshot.change_ids) == 20
        assert snapshot.change_seq_max == change_seq(conn, "kid-20")
        assert snapshot.change_ids[-1] == snapshot.change_seq_max
        assert len(snapshot.knowledge_ids) == 20
        assert snapshot.knowledge_ids[0] == "kid-1"
        assert snapshot.limits["max_batch_changes"] == 50
        assert snapshot.limits["max_candidates"] >= 1
        # 成功：位置推进到快照上界、失败计数清零、batch 前进。
        state = scheduler.pending_state()
        assert state["last_processed_seq"] == snapshot.change_seq_max
        assert state["pending_change_count"] == 0
        assert state["batch_seq"] == 1
        assert state["failed_attempts"] == 0
        assert state["last_error"] is None
        assert state["running"] is False
        consumed = [int(row["consumed_batch_seq"]) for row in pending_rows(conn)]
        assert consumed == [1] * 20
    finally:
        conn.close()


async def test_below_both_thresholds_does_not_call_runner(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        runner = FakeRunner()
        scheduler = _scheduler(conn, runner=runner)
        add_changes(conn, 19)
        add_confirmed_turns(conn, scheduler, 29)
        before = int(state_row(conn)["turn_seq"])
        result = await scheduler.maybe_run()
        assert result["ran"] is False and result["reason"] == "no_trigger"
        assert runner.calls == []
        assert int(state_row(conn)["turn_seq"]) == before  # 没触发不增长等待轮次
        # 第 30 轮：等待达到阈值 → 触发（原因 waiting_turns）。
        add_confirmed_turns(conn, scheduler, 1, prefix="last")
        result = await scheduler.maybe_run()
        assert result["ran"] is True
        assert runner.calls[0].trigger is MaintenanceTrigger.WAITING_TURNS
        assert runner.calls[0].waiting_turns == 30
    finally:
        conn.close()


async def test_configurable_change_threshold(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        runner = FakeRunner()
        scheduler = _scheduler(
            conn,
            runner=runner,
            settings=FakeSettings({"knowledge_maintenance.change_threshold": "3"}),
        )
        add_changes(conn, 2)
        result = await scheduler.maybe_run()
        assert result["ran"] is False and result["reason"] == "no_trigger"
        add_changes(conn, 1, start=3)
        result = await scheduler.maybe_run()
        assert result["ran"] is True
        assert runner.calls[0].pending_change_count == 3
    finally:
        conn.close()


async def test_configurable_waiting_turns_threshold(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        runner = FakeRunner()
        scheduler = _scheduler(
            conn,
            runner=runner,
            settings=FakeSettings({"knowledge_maintenance.waiting_turns": "2"}),
        )
        add_changes(conn, 1)
        add_confirmed_turns(conn, scheduler, 1)
        assert (await scheduler.maybe_run())["reason"] == "no_trigger"
        add_confirmed_turns(conn, scheduler, 1, prefix="second")
        result = await scheduler.maybe_run()
        assert result["ran"] is True
        assert runner.calls[0].trigger is MaintenanceTrigger.WAITING_TURNS
        assert runner.calls[0].waiting_turns == 2
    finally:
        conn.close()


async def test_configurable_max_changes_per_run(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        runner = FakeRunner()
        scheduler = _scheduler(
            conn,
            runner=runner,
            settings=FakeSettings(
                {
                    "knowledge_maintenance.change_threshold": "3",
                    "knowledge_maintenance.max_changes_per_run": "2",
                }
            ),
        )
        add_changes(conn, 3)
        result = await scheduler.maybe_run()
        assert result["ran"] is True
        snapshot = runner.calls[0]
        assert len(snapshot.change_ids) == 2
        assert snapshot.change_seq_max == change_seq(conn, "kid-2")
        assert snapshot.limits["max_batch_changes"] == 2
        assert scheduler.pending_state()["pending_change_count"] == 1
    finally:
        conn.close()


async def test_snapshot_takes_earliest_50_and_leaves_rest(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        runner = FakeRunner()
        scheduler = _scheduler(conn, runner=runner)
        add_changes(conn, 60)
        first = await scheduler.maybe_run()
        snapshot = runner.calls[0]
        assert len(snapshot.change_ids) == 50
        assert snapshot.change_ids == tuple(range(1, 51))
        assert snapshot.change_seq_max == change_seq(conn, "kid-50")
        assert first["processed_change_seq"] == snapshot.change_seq_max
        state = scheduler.pending_state()
        assert state["pending_change_count"] == 10
        assert state["last_processed_seq"] == snapshot.change_seq_max

        # 剩余 10 条 + 新记 10 条 = 20 条 → 下一批从剩余的第一条开始。
        add_changes(conn, 10, start=61)
        second = await scheduler.maybe_run()
        snapshot2 = runner.calls[1]
        assert len(snapshot2.change_ids) == 20
        assert snapshot2.change_ids[0] == change_seq(conn, "kid-51")
        assert second["processed_change_seq"] == snapshot2.change_seq_max
        assert scheduler.pending_state()["pending_change_count"] == 0
    finally:
        conn.close()


async def test_changes_added_during_run_are_left_for_next_batch(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        runner = FakeRunner()
        scheduler = _scheduler(conn, runner=runner)
        add_changes(conn, 20)

        def _insert_during_run(snapshot):
            add_changes(conn, 1, start=100)

        runner.on_call = _insert_during_run
        result = await scheduler.maybe_run()
        snapshot = runner.calls[0]
        assert result["processed_change_seq"] == snapshot.change_seq_max
        state = scheduler.pending_state()
        # 快照期间新增的变化 seq 更大，留给下一批，不会被本批吞掉。
        assert state["pending_change_count"] == 1
        assert state["change_seq_max"] == change_seq(conn, "kid-100")
    finally:
        conn.close()


async def test_outcome_processed_change_seq_advances_position(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        runner = FakeRunner()
        scheduler = _scheduler(conn, runner=runner)
        add_changes(conn, 20)
        runner.outcome = outcome_ok(processed_change_seq=change_seq(conn, "kid-5"), applied=4)
        result = await scheduler.maybe_run()
        assert result["processed_change_seq"] == change_seq(conn, "kid-5")
        assert result["applied"] == 4
        assert scheduler.pending_state()["pending_change_count"] == 15
        assert scheduler.pending_state()["last_processed_seq"] == change_seq(conn, "kid-5")
    finally:
        conn.close()


async def test_outcome_processed_seq_is_clamped_to_snapshot_max(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        runner = FakeRunner()
        scheduler = _scheduler(conn, runner=runner)
        add_changes(conn, 20)
        runner.outcome = outcome_ok(processed_change_seq=999999)
        result = await scheduler.maybe_run()
        assert result["processed_change_seq"] == runner.calls[0].change_seq_max
        assert scheduler.pending_state()["pending_change_count"] == 0
    finally:
        conn.close()


async def test_success_resets_failure_state(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        runner = FakeRunner()
        scheduler = _scheduler(conn, runner=runner, clock=FakeClock())
        add_changes(conn, 20)
        runner.outcome = outcome_failed("第一次失败")
        await scheduler.maybe_run()
        assert scheduler.pending_state()["failed_attempts"] == 1
        add_confirmed_turns(conn, scheduler, 5)
        runner.outcome = outcome_ok()
        await scheduler.maybe_run()
        state = scheduler.pending_state()
        assert state["failed_attempts"] == 0
        assert state["retry_blocked_until_turn"] == 0
        assert state["last_error"] is None
        assert state["retry_blocked"] is False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 失败 / 退避 / 单轮互斥 / busy / 关闭状态
# ---------------------------------------------------------------------------


async def test_failure_keeps_position_and_blocks_retry(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        runner = FakeRunner()
        scheduler = _scheduler(conn, runner=runner)
        add_changes(conn, 20)
        runner.outcome = outcome_failed("模型调用失败")
        result = await scheduler.maybe_run()
        assert result["ran"] is True
        assert result["ok"] is False
        assert result["reason"] == "failed"
        assert "模型调用失败" in result["error"]
        assert result["processed_change_seq"] == 0  # 处理位置不动
        state = scheduler.pending_state()
        assert state["last_processed_seq"] == 0
        assert state["pending_change_count"] == 20
        assert state["failed_attempts"] == 1
        assert state["retry_blocked_until_turn"] == 5
        assert state["running"] is False

        # 退避期间：不再调用 runner（fake 计数不变），位置不动。
        add_confirmed_turns(conn, scheduler, 4)
        calls_before = len(runner.calls)
        blocked = await scheduler.maybe_run()
        assert blocked["ran"] is False
        assert blocked["reason"] == "retry_cooldown"
        assert len(runner.calls) == calls_before
        assert scheduler.pending_state()["last_processed_seq"] == 0

        # 到点（第 5 个用户轮）：恢复执行。
        add_confirmed_turns(conn, scheduler, 1, prefix="reach")
        runner.outcome = outcome_ok()
        recovered = await scheduler.maybe_run()
        assert recovered["ran"] is True and recovered["ok"] is True
        assert len(runner.calls) == 2
        assert scheduler.pending_state()["last_processed_seq"] == runner.calls[1].change_seq_max
    finally:
        conn.close()


async def test_runner_exception_is_isolated_and_redacted(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        runner = FakeRunner()
        scheduler = _scheduler(conn, runner=runner)
        add_changes(conn, 20)
        runner.error = RuntimeError("provider returned sk-abcdef123456 while calling")
        result = await scheduler.maybe_run()
        assert result["ok"] is False
        assert "sk-abcdef123456" not in result["error"]
        assert "***redacted***" in result["error"]
        state = scheduler.pending_state()
        assert state["running"] is False
        assert state["last_processed_seq"] == 0
        assert state["failed_attempts"] == 1
        assert "sk-abcdef123456" not in (state["last_error"] or "")
    finally:
        conn.close()


async def test_busy_skips_before_claiming(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        runner = FakeRunner()
        scheduler = _scheduler(conn, runner=runner)
        add_changes(conn, 20)
        result = await scheduler.maybe_run(busy=True)
        assert result["ran"] is False and result["reason"] == "busy"
        assert runner.calls == []
        assert scheduler.pending_state()["running"] is False
        assert int(state_row(conn)["batch_seq"]) == 0
    finally:
        conn.close()


async def test_disabled_skips_until_enabled_again(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        runner = FakeRunner()
        settings = FakeSettings({"knowledge_maintenance.enabled": "false"})
        scheduler = _scheduler(conn, runner=runner, settings=settings)
        add_changes(conn, 20)
        result = await scheduler.maybe_run()
        assert result["ran"] is False and result["reason"] == "disabled"
        assert runner.calls == []
        settings["knowledge_maintenance.enabled"] = "true"
        result = await scheduler.maybe_run()
        assert result["ran"] is True
        assert len(runner.calls) == 1
    finally:
        conn.close()


async def test_only_one_round_runs_at_a_time(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        runner = FakeRunner()
        gate = asyncio.Event()
        runner.gate = gate
        scheduler = _scheduler(conn, runner=runner)
        add_changes(conn, 20)
        task = asyncio.create_task(scheduler.maybe_run())
        await _wait_for_call(runner)
        second = await scheduler.maybe_run()
        assert second["ran"] is False and second["reason"] == "already_running"
        assert len(runner.calls) == 1
        assert scheduler.pending_state()["running"] is True
        gate.set()
        first = await task
        assert first["ran"] is True and first["ok"] is True
        assert scheduler.pending_state()["running"] is False
    finally:
        conn.close()


async def test_cancelled_run_releases_running_and_keeps_position(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        runner = FakeRunner()
        runner.gate = asyncio.Event()
        scheduler = _scheduler(conn, runner=runner)
        add_changes(conn, 20)
        task = asyncio.create_task(scheduler.maybe_run())
        await _wait_for_call(runner)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        state = scheduler.pending_state()
        assert state["running"] is False
        assert state["last_processed_seq"] == 0
        assert state["failed_attempts"] == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# running 过期回收（进程崩溃 / 强杀）
# ---------------------------------------------------------------------------


async def test_stale_running_is_recovered_as_interruption(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        clock = FakeClock()
        runner = FakeRunner()
        scheduler = _scheduler(conn, runner=runner, clock=clock)
        add_changes(conn, 20)
        started = clock.moment - timedelta(minutes=31)
        conn.execute(
            "UPDATE knowledge_maintenance_state SET running = 1, run_started_at = ?"
            " WHERE id = 1",
            (started.isoformat(),),
        )
        result = await scheduler.maybe_run()
        assert result["ran"] is False
        assert result["reason"] == "stale_recovered"
        state = scheduler.pending_state()
        assert state["running"] is False
        assert state["failed_attempts"] == 1
        assert state["last_processed_seq"] == 0  # 不丢处理位置
        assert state["retry_blocked_until_turn"] == 5
        assert "中断" in (state["last_error"] or "")
        assert runner.calls == []
        # 回收后按退避处理，再等 5 个用户轮到点。
        assert (await scheduler.maybe_run())["reason"] == "retry_cooldown"
        add_confirmed_turns(conn, scheduler, 5)
        result = await scheduler.maybe_run()
        assert result["ran"] is True and result["ok"] is True
    finally:
        conn.close()


async def test_fresh_running_flag_blocks_without_recovery(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        clock = FakeClock()
        runner = FakeRunner()
        scheduler = _scheduler(conn, runner=runner, clock=clock)
        add_changes(conn, 20)
        conn.execute(
            "UPDATE knowledge_maintenance_state SET running = 1, run_started_at = ?"
            " WHERE id = 1",
            (clock.moment.isoformat(),),
        )
        result = await scheduler.maybe_run()
        assert result["ran"] is False and result["reason"] == "already_running"
        state = scheduler.pending_state()
        assert state["running"] is True
        assert state["failed_attempts"] == 0
        assert runner.calls == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 跨重启 / 维护来源不循环
# ---------------------------------------------------------------------------


async def test_accumulation_survives_restart(tmp_path):
    path = tmp_path / "p2a.db"
    conn = migrated_conn(tmp_path)
    try:
        scheduler1 = _scheduler(conn)
        add_changes(conn, 5)
        add_confirmed_turns(conn, scheduler1, 3)
        assert int(state_row(conn)["turn_seq"]) == 3
    finally:
        conn.close()

    reopened = connect(path)
    try:
        runner = FakeRunner()
        scheduler2 = _scheduler(reopened, runner=runner)
        state = scheduler2.pending_state()
        assert state["pending_change_count"] == 5
        assert state["turn_seq"] == 3  # 跨重启累计
        assert state["last_processed_seq"] == 0
        assert state["batch_seq"] == 0
        # 继续累计：3 + 27 = 30 轮 → 等待触发。
        add_confirmed_turns(reopened, scheduler2, 27, prefix="after")
        result = await scheduler2.maybe_run()
        assert result["ran"] is True
        assert result["trigger"] == "waiting_turns"
        processed = result["processed_change_seq"]
        assert processed == change_seq(reopened, "kid-5")
    finally:
        reopened.close()

    third = connect(path)
    try:
        scheduler3 = _scheduler(third)
        state = scheduler3.pending_state()
        assert state["last_processed_seq"] == processed
        assert state["batch_seq"] == 1
        assert state["pending_change_count"] == 0
    finally:
        third.close()


async def test_turn_dedup_survives_restart(tmp_path):
    path = tmp_path / "p2a.db"
    conn = migrated_conn(tmp_path)
    try:
        scheduler1 = _scheduler(conn)
        add_changes(conn, 1)
        scheduler1.note_user_turn_completed("turn-1")
        assert int(state_row(conn)["turn_seq"]) == 1
    finally:
        conn.close()

    reopened = connect(path)
    try:
        scheduler2 = _scheduler(reopened)
        scheduler2.note_user_turn_completed("turn-1")  # 重启后重复通知：仍只算一次
        assert int(state_row(reopened)["turn_seq"]) == 1
    finally:
        reopened.close()


async def test_maintenance_sources_do_not_form_trigger_loop(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        from agent.maintenance.changes import record_effective_change
        from agent.maintenance.models import MAINTENANCE_ORIGINS

        runner = FakeRunner()
        scheduler = _scheduler(conn, runner=runner)
        for index, origin in enumerate(sorted(MAINTENANCE_ORIGINS), start=1):
            assert (
                record_effective_change(
                    conn,
                    knowledge_id=f"maintenance-{index}",
                    chain_id=f"maintenance-chain-{index}",
                    version=1,
                    source=origin,
                )
                is False
            )
        add_confirmed_turns(conn, scheduler, 40)
        state = scheduler.pending_state()
        assert state["pending_change_count"] == 0
        assert state["turn_seq"] == 0
        assert state["trigger"] is None
        result = await scheduler.maybe_run()
        assert result["ran"] is False and result["reason"] == "no_trigger"
        assert runner.calls == []
    finally:
        conn.close()
