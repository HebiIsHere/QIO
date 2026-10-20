# -*- coding: utf-8 -*-
"""D 独立契约测试：知识维护调度（变化计数触发 / 等待轮数触发 / 跨重启 / 单轮互斥）契约 §12.3。

公开接口（契约冻结）：

    class KnowledgeMaintenanceScheduler:
        def __init__(self, conn, *, runner, settings=None, clock=None)
        def note_user_turn_completed(self, turn_id: str) -> None
        def pending_state(self) -> dict
        async def maybe_run(self, *, busy: bool = False) -> dict

口径：调度器的输入是「变化日志 + 用户轮次」，输出是「是否调用 runner.run_batch」。
本文件只用公开行为断言（不读调度器私有属性），用 fake runner 记录快照。

基线说明：阶段二实现未合入时模块 / 迁移 46 不存在，本文件会**失败**（不是 skip）。
运行（只跑本文件）：
    cd backend
    & 'D:/qio-dev/qio/backend/.venv/Scripts/python.exe' -m pytest tests/test_p2_d_scheduler_trigger.py -q
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

CLAUSE = "契约 §12.3"


# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------


def _load(name: str):
    try:
        return importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            f"{CLAUSE}：契约要求的 {name} 不可导入；阶段二实现尚未合入时属于「未验证」：{exc!r}",
            pytrace=False,
        )


def _require_tables(conn):
    have = {str(row[0]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [t for t in (
        "knowledge_change_log", "knowledge_maintenance_state",
        "knowledge_maintenance_turn_log") if t not in have]
    if missing:
        from agent.storage.migrate import current_version

        pytest.fail(
            f"{CLAUSE}：缺少 {missing}（schema 版本 {current_version(conn)}）；"
            "迁移 46 尚未登记到 storage/schema.py。",
            pytrace=False,
        )


def _settings_store(conn):
    from agent.storage.settings import SettingsStore

    return SettingsStore(conn)


def _record_changes(conn, count: int, *, prefix: str = "kn") -> list[int]:
    """直接经契约写入接口造待处理变化（这是调度器的公开输入）。"""
    changes = _load("agent.maintenance.changes")
    seqs: list[int] = []
    for index in range(count):
        ok = changes.record_effective_change(
            conn,
            knowledge_id=f"{prefix}_{index:03d}",
            chain_id=f"chain_{prefix}_{index:03d}",
            version=1,
            source="main_chat",
        )
        assert ok is True, f"{CLAUSE}：造变化记录失败（{prefix}_{index}）"
        row = conn.execute(
            "SELECT seq FROM knowledge_change_log ORDER BY seq DESC LIMIT 1").fetchone()
        seqs.append(int(row["seq"]))
    return seqs


class FakeRunner:
    """记录调度器送来的快照；可按队列返回失败 / 成功。"""

    def __init__(self, *, fail_first: int = 0, on_run=None) -> None:
        self.calls = 0
        self.snapshots: list = []
        self._fail_first = fail_first
        self._on_run = on_run

    async def run_batch(self, snapshot):
        models = _load("agent.maintenance.models")
        self.calls += 1
        self.snapshots.append(snapshot)
        if self._on_run is not None:
            self._on_run(snapshot)
        if self.calls <= self._fail_first:
            return models.MaintenanceRunOutcome(
                ok=False, status="failed", error="fake runner failure")
        return models.MaintenanceRunOutcome(
            ok=True,
            status="completed",
            processed_change_seq=int(snapshot.change_seq_max),
            applied=0,
            asked=0,
            rejected=0,
        )


def _scheduler(conn, runner, settings=None):
    scheduler_mod = _load("agent.maintenance.scheduler")
    _require_tables(conn)
    return scheduler_mod.KnowledgeMaintenanceScheduler(
        conn, runner=runner, settings=settings or _settings_store(conn)
    )


def _turn(scheduler, count: int, *, start: int = 1, tag: str = "t") -> None:
    for index in range(start, start + count):
        scheduler.note_user_turn_completed(f"{tag}{index}")


# ---------------------------------------------------------------------------
# 触发阈值
# ---------------------------------------------------------------------------


async def test_default_change_threshold_is_20(db_conn):
    """§12.3：待处理变化数 >= 20 触发。"""
    _require_tables(db_conn)
    runner = FakeRunner()
    scheduler = _scheduler(db_conn, runner)
    _turn(scheduler, 1)  # turn_seq = 1
    _record_changes(db_conn, 19)
    await scheduler.maybe_run()
    assert runner.calls == 0, f"{CLAUSE}：19 条变化不应达到默认阈值 20"

    _record_changes(db_conn, 1, prefix="kn_last")
    await scheduler.maybe_run()
    assert runner.calls == 1, f"{CLAUSE}：20 条待处理变化必须触发一轮维护"
    snapshot = runner.snapshots[0]
    models = _load("agent.maintenance.models")
    assert snapshot.trigger == models.MaintenanceTrigger.CHANGE_COUNT
    assert int(snapshot.change_seq_max) == 20, (
        f"{CLAUSE}：快照 change_seq_max 应为处理位置（20），实际 {snapshot.change_seq_max}")
    assert int(snapshot.pending_change_count) == 20

    # 成功消费后：没有待处理变化，不重复调用模型
    await scheduler.maybe_run()
    assert runner.calls == 1, f"{CLAUSE}：已消费的变化不得重复触发"


async def test_default_waiting_turns_threshold_is_30(db_conn):
    """§12.3：最老待处理变化等待轮数 >= 30 触发（新变化不重置等待）。"""
    _require_tables(db_conn)
    runner = FakeRunner()
    scheduler = _scheduler(db_conn, runner)
    _turn(scheduler, 1)              # turn_seq = 1
    _record_changes(db_conn, 1)
    _turn(scheduler, 29, start=2)    # turn_seq = 30，最老一条等待 29 轮
    await scheduler.maybe_run()
    assert runner.calls == 0, f"{CLAUSE}：等待 29 轮不应达到默认阈值 30"

    # 期间又来一条新变化：它不能把最老一条的等待轮数重置
    _record_changes(db_conn, 1, prefix="kn_new")
    await scheduler.maybe_run()
    assert runner.calls == 0, f"{CLAUSE}：新变化不得重置较早内容的等待轮数"

    _turn(scheduler, 1, start=31)    # turn_seq = 31，等待 30 轮
    await scheduler.maybe_run()
    assert runner.calls == 1, f"{CLAUSE}：等待满 30 轮必须触发一轮维护"
    models = _load("agent.maintenance.models")
    assert runner.snapshots[0].trigger == models.MaintenanceTrigger.WAITING_TURNS


async def test_thresholds_are_configurable(db_conn):
    """§12.3：change_threshold / waiting_turns 可从 settings 覆盖。"""
    _require_tables(db_conn)
    store = _settings_store(db_conn)
    store.set("knowledge_maintenance.change_threshold", "2")
    store.set("knowledge_maintenance.waiting_turns", "1000")
    db_conn.commit()
    runner = FakeRunner()
    scheduler = _scheduler(db_conn, runner, store)
    _turn(scheduler, 1)
    _record_changes(db_conn, 1)
    await scheduler.maybe_run()
    assert runner.calls == 0, f"{CLAUSE}：配置阈值 2 时 1 条变化不该触发"
    _record_changes(db_conn, 1, prefix="kn_cfg")
    await scheduler.maybe_run()
    assert runner.calls == 1, f"{CLAUSE}：配置阈值 2 时第 2 条变化必须触发"

    # 只靠等待轮数触发（阈值 3）
    store.set("knowledge_maintenance.change_threshold", "1000")
    store.set("knowledge_maintenance.waiting_turns", "3")
    db_conn.commit()
    runner2 = FakeRunner()
    scheduler2 = _scheduler(db_conn, runner2, store)
    _turn(scheduler2, 1, start=100, tag="u")
    _record_changes(db_conn, 1, prefix="kn_wait_cfg")
    _turn(scheduler2, 3, start=101, tag="u")
    await scheduler2.maybe_run()
    assert runner2.calls == 1, f"{CLAUSE}：配置等待阈值 3 时应触发"


async def test_no_pending_changes_never_calls_model(db_conn):
    """§12.3：没有待处理变化时既不累计等待轮数、也不调用模型。"""
    _require_tables(db_conn)
    runner = FakeRunner()
    scheduler = _scheduler(db_conn, runner)
    _turn(scheduler, 40)
    await scheduler.maybe_run()
    assert runner.calls == 0, f"{CLAUSE}：无变化时不得调用模型"

    # 之后才出现变化：等待轮数从变化创建时算起，不从调度器启动时算起
    _record_changes(db_conn, 1)
    await scheduler.maybe_run()
    assert runner.calls == 0, f"{CLAUSE}：刚创建的变化不应立刻因历史轮数触发"


async def test_turn_notification_is_deduplicated_by_turn_id(db_conn):
    """§12.3：note_user_turn_completed 按 turn_id 去重（同一 turn 重复通知只计一次）。"""
    _require_tables(db_conn)
    store = _settings_store(db_conn)
    store.set("knowledge_maintenance.change_threshold", "1000")
    store.set("knowledge_maintenance.waiting_turns", "5")
    db_conn.commit()
    runner = FakeRunner()
    scheduler = _scheduler(db_conn, runner, store)
    _record_changes(db_conn, 1)  # 有待处理变化才累计轮次
    scheduler.note_user_turn_completed("turn-dup-1")
    scheduler.note_user_turn_completed("turn-dup-1")
    scheduler.note_user_turn_completed("turn-dup-1")
    state = db_conn.execute(
        "SELECT turn_seq FROM knowledge_maintenance_state WHERE id = 1").fetchone()
    assert int(state["turn_seq"]) == 1, (
        f"{CLAUSE}：同一 turn_id 重复通知只能计一次（turn_seq={state['turn_seq']}）")
    scheduler.note_user_turn_completed("turn-dup-2")
    state = db_conn.execute(
        "SELECT turn_seq FROM knowledge_maintenance_state WHERE id = 1").fetchone()
    assert int(state["turn_seq"]) == 2, (
        f"{CLAUSE}：不同 turn_id 应继续累计（turn_seq={state['turn_seq']}）")


async def test_disabled_switch_preserves_pending_state(db_conn):
    """§12.3：enabled=false 时不启动；恢复后待处理状态仍在（下次继续）。"""
    _require_tables(db_conn)
    store = _settings_store(db_conn)
    runner = FakeRunner()
    scheduler = _scheduler(db_conn, runner, store)
    _turn(scheduler, 1)
    _record_changes(db_conn, 20)
    store.set("knowledge_maintenance.enabled", "false")
    db_conn.commit()
    await scheduler.maybe_run()
    assert runner.calls == 0, f"{CLAUSE}：关闭后不得调用模型"
    store.set("knowledge_maintenance.enabled", "true")
    db_conn.commit()
    await scheduler.maybe_run()
    assert runner.calls == 1, f"{CLAUSE}：重新开启后必须继续处理积压变化"
    assert int(runner.snapshots[0].pending_change_count) == 20


async def test_busy_skips_without_consuming(db_conn):
    """§12.3：busy=True（主任务繁忙 / 关闭中）时跳过，不消费处理位置。"""
    _require_tables(db_conn)
    runner = FakeRunner()
    scheduler = _scheduler(db_conn, runner)
    _turn(scheduler, 1)
    _record_changes(db_conn, 20)
    result = await scheduler.maybe_run(busy=True)
    assert runner.calls == 0, f"{CLAUSE}：busy 时必须跳过"
    assert isinstance(result, dict), f"{CLAUSE}：maybe_run 必须返回状态 dict"
    await scheduler.maybe_run()
    assert runner.calls == 1, f"{CLAUSE}：busy 跳过后未消费，下一轮必须照常处理"
    assert int(runner.snapshots[0].pending_change_count) == 20


# ---------------------------------------------------------------------------
# 快照 / 处理位置 / 单轮互斥
# ---------------------------------------------------------------------------


async def test_snapshot_batches_new_changes_for_later(db_conn):
    """§12.3：按快照 change_seq_max 推进；运行期间新增的变化留给后续批次。"""
    _require_tables(db_conn)
    store = _settings_store(db_conn)
    store.set("knowledge_maintenance.change_threshold", "1")
    store.set("knowledge_maintenance.waiting_turns", "1000")
    store.set("knowledge_maintenance.max_changes_per_run", "5")
    db_conn.commit()

    added_during_run: list[int] = []

    def _on_run(snapshot):
        if snapshot.batch_seq == 1:
            added_during_run.extend(_record_changes(db_conn, 1, prefix="kn_during"))

    runner = FakeRunner(on_run=_on_run)
    scheduler = _scheduler(db_conn, runner, store)
    _turn(scheduler, 1)
    _record_changes(db_conn, 5)
    await scheduler.maybe_run()
    assert runner.calls == 1
    first = runner.snapshots[0]
    assert int(first.change_seq_max) == 5, (
        f"{CLAUSE}：首轮快照只能覆盖批次开始时的变化（5），实际 {first.change_seq_max}")
    assert added_during_run and added_during_run[0] == 6

    # 第二轮：快照从 6 起，包含运行期间新增的一条
    await scheduler.maybe_run()
    assert runner.calls == 2, f"{CLAUSE}：运行期间新增的变化必须留给后续批次处理"
    second = runner.snapshots[1]
    assert int(second.change_seq_max) == 6, (
        f"{CLAUSE}：第二轮应处理到 seq 6，实际 {second.change_seq_max}")


async def test_max_changes_per_run_limits_snapshot(db_conn):
    """§12.3：单次处理量上限（默认 50，可配置）；按快照推进处理位置。"""
    _require_tables(db_conn)
    store = _settings_store(db_conn)
    store.set("knowledge_maintenance.change_threshold", "1")
    store.set("knowledge_maintenance.waiting_turns", "1000")
    store.set("knowledge_maintenance.max_changes_per_run", "3")
    db_conn.commit()
    runner = FakeRunner()
    scheduler = _scheduler(db_conn, runner, store)
    _turn(scheduler, 1)
    _record_changes(db_conn, 5)

    await scheduler.maybe_run()
    assert runner.calls == 1
    assert int(runner.snapshots[0].change_seq_max) == 3, (
        f"{CLAUSE}：max_changes_per_run=3 时首个快照最多覆盖 3 条")
    await scheduler.maybe_run()
    assert runner.calls == 2, f"{CLAUSE}：剩余变化应在下一批处理"
    assert int(runner.snapshots[1].change_seq_max) == 5


async def test_single_run_mutex(db_conn):
    """§12.3：同一时间只运行一轮（并发 maybe_run 不得执行两轮）。"""
    _require_tables(db_conn)
    store = _settings_store(db_conn)
    store.set("knowledge_maintenance.change_threshold", "1")
    db_conn.commit()

    started = asyncio.Event()

    class SlowRunner(FakeRunner):
        async def run_batch(self, snapshot):
            started.set()
            await asyncio.sleep(0.05)
            return await super().run_batch(snapshot)

    runner = SlowRunner()
    scheduler = _scheduler(db_conn, runner, store)
    _turn(scheduler, 1)
    _record_changes(db_conn, 2)
    await asyncio.gather(scheduler.maybe_run(), scheduler.maybe_run())
    assert runner.calls == 1, (
        f"{CLAUSE}：并发 maybe_run 必须互斥（单轮运行），实际调用了 {runner.calls} 轮")


async def test_failure_retains_position_and_backs_off(db_conn):
    """§12.3：失败保留处理位置，记录失败次数，并按冷却轮数退避（不每轮重复失败调用）。"""
    _require_tables(db_conn)
    runner = FakeRunner(fail_first=1)
    scheduler = _scheduler(db_conn, runner)
    _turn(scheduler, 1)
    _record_changes(db_conn, 20)
    await scheduler.maybe_run()
    assert runner.calls == 1, f"{CLAUSE}：达到阈值应尝试一轮"
    first = runner.snapshots[0]

    # 同一轮立即重试：必须被退避挡住（不能每轮重复失败调用）
    await scheduler.maybe_run()
    assert runner.calls == 1, f"{CLAUSE}：失败后立即重试必须被退避挡住"

    # 冷却轮数（默认 5）未到：仍不重试
    _turn(scheduler, 4, start=2)
    await scheduler.maybe_run()
    assert runner.calls == 1, f"{CLAUSE}：冷却轮数未到不得重试"

    # 冷却已过：重试，且处理位置没有因失败而前进
    _turn(scheduler, 1, start=6)
    await scheduler.maybe_run()
    assert runner.calls == 2, f"{CLAUSE}：冷却结束必须重试"
    assert int(runner.snapshots[1].change_seq_max) == int(first.change_seq_max), (
        f"{CLAUSE}：失败轮不得推进处理位置"
        f"（first={first.change_seq_max}, retry={runner.snapshots[1].change_seq_max}）")


async def test_cross_restart_continues_turn_and_change_state(db_conn):
    """§12.3：跨重启保留轮次序号 / 处理位置 / 待处理变化（状态持久化）。"""
    _require_tables(db_conn)
    store = _settings_store(db_conn)
    store.set("knowledge_maintenance.change_threshold", "1000")
    store.set("knowledge_maintenance.waiting_turns", "5")
    db_conn.commit()
    runner1 = FakeRunner()
    first = _scheduler(db_conn, runner1, store)
    _turn(first, 1, tag="a")
    _record_changes(db_conn, 1)
    _turn(first, 4, start=2, tag="a")   # 等待 4 轮，未触发
    await first.maybe_run()
    assert runner1.calls == 0

    # 关闭 + 重启：新的调度器实例复用同一数据库
    runner2 = FakeRunner()
    second = _scheduler(db_conn, runner2, store)
    _turn(second, 1, start=6, tag="a")  # 累计等待 5 轮 → 触发
    await second.maybe_run()
    assert runner2.calls == 1, (
        f"{CLAUSE}：跨重启后等待轮数应继续累计并触发；"
        "重启不得把轮次序号 / 变化记录清零")
    models = _load("agent.maintenance.models")
    assert runner2.snapshots[0].trigger == models.MaintenanceTrigger.WAITING_TURNS


async def test_stale_running_flag_is_recovered(db_conn):
    """§12.3：持久化 running 标记 + 超时回收（崩溃遗留的 running 不能永久卡死）。

    回收后按失败处理（计一次失败 + 退避），处理位置不动；退避结束后必须能继续执行。
    """
    _require_tables(db_conn)
    runner = FakeRunner()
    scheduler = _scheduler(db_conn, runner)
    _turn(scheduler, 1)
    _record_changes(db_conn, 20)
    await scheduler.maybe_run()
    assert runner.calls == 1

    rows = list(db_conn.execute("SELECT * FROM knowledge_maintenance_state"))
    if not rows:
        pytest.fail(
            f"{CLAUSE}：knowledge_maintenance_state 没有单行状态记录，"
            "无法验证持久化 running 标记 / 超时回收。",
            pytrace=False,
        )
    db_conn.execute(
        "UPDATE knowledge_maintenance_state SET running = 1, "
        "run_started_at = '2000-01-01T00:00:00+00:00', "
        "failed_attempts = 0, retry_blocked_until_turn = 0")
    db_conn.commit()

    _record_changes(db_conn, 20, prefix="kn_recover")
    recovered = await scheduler.maybe_run()
    reason = str(recovered.get("reason") or "") if isinstance(recovered, dict) else ""
    assert runner.calls == 1, (
        f"{CLAUSE}：过期 running 的回收轮本身不应把同一批变化再执行一次（reason={reason!r}）")
    # 回收按失败记账（记一次失败 + 退避），不与成功轮混淆
    assert any(token in reason for token in ("stale", "recover", "recovered")) or (
        int(db_conn.execute(
            "SELECT failed_attempts FROM knowledge_maintenance_state WHERE id = 1"
        ).fetchone()["failed_attempts"]) >= 1), (
        f"{CLAUSE}：running 超时回收必须如实记一次失败（reason={reason!r}）")

    # 退避结束后必须能继续执行（默认 cooldown 5 个用户轮；这里用默认配置）
    _turn(scheduler, 5, start=2)
    await scheduler.maybe_run()
    assert runner.calls == 2, (
        f"{CLAUSE}：超时回收 + 退避结束后，下一轮必须能继续执行积压变化")
