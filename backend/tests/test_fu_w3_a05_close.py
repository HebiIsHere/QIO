# -*- coding: utf-8 -*-
"""A05 验收：关闭结果必须真实（W3）。

口径（不长时间等待、不真联网、模型/执行单元全是受控替身）：

* 四种关闭状态——**正常结束 / 配合取消 / 延迟取消 / 取消后仍未结束**——
  一律用「闸门 + 缩短超时」构造，断言调用方拿到的 report、真实任务状态、
  是否写干净退出、依赖释放顺序、重启可恢复、无关闭后写库、无悬挂任务、
  无永久 running；
* 核心纠正：**「asyncio 句柄取消了」不等于「底层执行单元结束了」**。
  底层执行单元用声明的探针表示；句柄已取消而探针未确认时，报告必须
  不干净（`still_running` 有名字），并且不得写干净退出、不得关数据库。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from agent.services import derived_tasks as dt
from agent.services import lifecycle as lc
from agent.services.background import (
    PHASE_CANCEL,
    PHASE_CANCEL_GRACE,
    PHASE_FINAL_GRACE,
    PHASE_REJECT_NEW,
    PHASE_VERIFY,
    PHASE_WAIT,
    BackgroundClosed,
    BackgroundTasks,
    ShutdownReport,
)
from agent.storage.db import connect
from agent.storage.instance_registry import InstanceRegistry
from agent.storage.migrate import apply_migrations
from agent.trace import redact

# 受控、缩短的关闭预算：三档加起来是硬上限，测试不会长时间等待。
T_WAIT = 0.02
T_CANCEL = 0.05
T_FINAL = 0.5

TASK_PREFIX = "background:"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _background_tasks() -> list[asyncio.Task]:
    """事件循环里还挂着的后台协程（注册表给它们起的名字带 `background:` 前缀）。"""
    return [
        task
        for task in asyncio.all_tasks()
        if (task.get_name() or "").startswith(TASK_PREFIX)
    ]


class _WriteGuard:
    """数据库已关闭之后仍然尝试写库时记账并拒绝（「无关闭后写库」的实测口径）。"""

    WRITE_PREFIXES = ("UPDATE", "INSERT", "DELETE", "REPLACE", "ALTER", "DROP", "CREATE")

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.closed = False
        self.writes_after_close = 0

    @property
    def in_transaction(self) -> bool:
        return self._conn.in_transaction

    def mark_closed(self) -> None:
        self.closed = True

    def execute(self, sql: str, params=()):  # noqa: ANN001
        if self.closed and str(sql).strip().upper().startswith(self.WRITE_PREFIXES):
            self.writes_after_close += 1
            raise sqlite3.ProgrammingError("数据库已关闭：拒绝写入")
        return self._conn.execute(sql, params)


def _db(tmp_path: Path, name: str) -> sqlite3.Connection:
    conn = connect(tmp_path / name)
    apply_migrations(conn)
    return conn


def _seed_running_derived_task(conn: sqlite3.Connection, task_id: str = "task_w3") -> str:
    """插一条**正在跑**的派生任务（模拟关闭时正被认领的那份工作）。"""
    now = "2026-10-10T00:00:00+00:00"
    conn.execute(
        "INSERT INTO derived_tasks (id, kind, fragment_id, content_version, state, attempts, "
        "last_error, run_after, created_at, updated_at, owner_instance_id, claim_generation) "
        "VALUES (?, 'summary', 'frag_w3', 1, 'running', 0, NULL, NULL, ?, ?, NULL, 0)",
        (task_id, now, now),
    )
    return task_id


# ---------------------------------------------------------------------------
# 注册表层：四种关闭状态
# ---------------------------------------------------------------------------


async def test_normal_finish_is_clean_and_never_enters_cancel_phase():
    """① 正常结束：有界等待内收尾，report 干净，且**没有**走到取消阶段。"""
    registry = BackgroundTasks()
    finished: list[str] = []
    gate = asyncio.Event()

    async def quick(name: str) -> None:
        await gate.wait()
        finished.append(name)

    first = registry.register("quick_a", lambda: quick("a"))
    registry.register("quick_b", lambda: quick("b"))
    await asyncio.sleep(0)  # 让两个协程真的跑起来
    gate.set()

    report = await registry.shutdown(timeout=1.0, cancel_timeout=T_CANCEL, final_timeout=T_FINAL)

    assert report.clean, report.as_dict()
    assert report.waited == 2 and report.cancelled == 0, report.as_dict()
    assert report.unfinished == [] and report.still_running == []
    assert sorted(finished) == ["a", "b"], "两个都跑完了（自然收尾，不是取消）"
    assert PHASE_WAIT in report.phases and PHASE_REJECT_NEW in report.phases
    assert PHASE_CANCEL not in report.phases, "没有东西要取消时不该进取消阶段"
    assert report.phases[-1] == PHASE_VERIFY
    assert not first.task.cancelled()
    assert registry.active() == [] and _background_tasks() == []


async def test_cooperative_cancel_ends_inside_cancel_window():
    """② 配合取消：等待超时 → 取消 → 在取消确认窗口内真的结束。"""
    registry = BackgroundTasks()
    finished: list[str] = []
    gate = asyncio.Event()

    async def stuck() -> None:
        await gate.wait()
        finished.append("stuck")

    handle = registry.register("stuck", stuck)
    await asyncio.sleep(0)
    report = await registry.shutdown(timeout=T_WAIT, cancel_timeout=1.0, final_timeout=T_FINAL)

    assert report.clean, report.as_dict()
    assert report.cancelled == 1 and report.waited == 0, report.as_dict()
    assert report.unfinished == [] and report.still_running == []
    assert PHASE_CANCEL in report.phases and PHASE_CANCEL_GRACE in report.phases
    assert PHASE_FINAL_GRACE not in report.phases, "取消确认窗口内就结束了，不该再用最后一档"
    assert handle.task.cancelled(), "取消是真的：协程没有跑完"
    assert finished == [], "被取消的那条没有完成它的工作"
    assert registry.active() == [] and _background_tasks() == []


async def test_delayed_cancel_needs_the_final_grace():
    """③ 延迟取消：吞掉取消、稍后才退出 —— 靠 `final_timeout` grace 才确认结束。"""
    registry = BackgroundTasks()
    gate = asyncio.Event()

    async def late() -> None:
        try:
            await gate.wait()
        except asyncio.CancelledError:
            # 先做完手上的一小段（受控的短等待），再结束。
            await asyncio.sleep(0.12)
            raise

    handle = registry.register("late", late)
    await asyncio.sleep(0)
    report = await registry.shutdown(timeout=T_WAIT, cancel_timeout=T_CANCEL, final_timeout=1.0)

    assert report.clean, report.as_dict()
    assert report.cancelled == 1, report.as_dict()
    assert PHASE_CANCEL_GRACE in report.phases and PHASE_FINAL_GRACE in report.phases, report.phases
    assert handle.task.cancelled() and handle.settled
    assert report.unfinished == [] and report.still_running == []
    assert registry.active() == [] and _background_tasks() == []


async def test_cancel_swallowing_task_is_reported_unfinished_never_pretended_clean():
    """④ 取消后仍未结束：三档 grace 用尽 → 如实 `unfinished`，不假装干净。"""
    registry = BackgroundTasks()
    gate = asyncio.Event()
    release = asyncio.Event()

    async def swallows_cancel() -> None:
        while not release.is_set():
            try:
                await gate.wait()
            except asyncio.CancelledError:  # noqa: PERF203 - 就是要在意这条取消
                continue

    handle = registry.register("swallows", swallows_cancel)
    await asyncio.sleep(0)
    report = await registry.shutdown(timeout=T_WAIT, cancel_timeout=T_CANCEL, final_timeout=T_CANCEL)

    assert not report.clean, report.as_dict()
    assert report.unfinished == ["swallows"], report.as_dict()
    assert report.still_running == ["swallows"], report.as_dict()
    assert PHASE_FINAL_GRACE in report.phases
    assert "swallows" in report.detail, report.detail
    assert registry.unsettled() == ["swallows"], "未确认收尾的任务仍挂在账上"

    # 第二次关闭必须**继续如实**报出来（不能靠「已经关过一次」洗干净）
    again = await registry.shutdown(timeout=T_WAIT, cancel_timeout=T_CANCEL, final_timeout=T_CANCEL)
    assert not again.clean and again.unfinished == ["swallows"], again.as_dict()

    # 放行之后：真的结束了，账也就清了（不是「报告说结束了」）
    release.set()
    gate.set()
    await asyncio.wait([handle.task], timeout=2.0)
    assert handle.settled, "放行之后底层执行单元真的结束了"
    assert registry.unsettled() == [] and registry.active() == []
    assert _background_tasks() == [], "无悬挂任务"
    final = await registry.shutdown(timeout=T_WAIT, cancel_timeout=T_CANCEL, final_timeout=T_FINAL)
    assert final.clean and final.waited == 0 and final.cancelled == 0, final.as_dict()


async def test_cancelled_asyncio_handle_is_not_a_finished_execution_unit():
    """核心纠正：句柄被取消了 ≠ 底层执行单元结束了。

    底层执行单元用 `unit_finished` 探针表示（真实场景：线程池里的阻塞调用、
    子进程、外部副作用）。句柄已经取消、探针仍未确认时，报告必须**不干净**。
    """
    registry = BackgroundTasks()
    gate = asyncio.Event()
    unit_done = asyncio.Event()

    async def hands_off() -> None:
        # 协程只负责等待；真正的执行单元在别处（探针代表它）。
        await gate.wait()

    handle = registry.register("hands_off", hands_off, unit_finished=unit_done)
    await asyncio.sleep(0)
    report = await registry.shutdown(timeout=T_WAIT, cancel_timeout=T_CANCEL, final_timeout=T_CANCEL)

    assert handle.task.cancelled(), "asyncio 句柄确实被取消了"
    assert handle.done and not handle.settled, "句柄结束了，但底层执行单元没确认结束"
    assert report.clean is False, report.as_dict()
    assert report.unfinished == [], "asyncio 句柄没有剩下（取消是真的）"
    assert report.still_running == ["hands_off"], "底层执行单元未确认结束必须在报告里"
    assert registry.unsettled() == ["hands_off"]
    # 上层拿到的关闭报告同样不干净 —— 它据此决定不写干净退出、不关数据库
    close = lc.close_report_from_shutdown(report)
    assert not close.clean and "hands_off" in close.unfinished
    assert lc.close_decision(close).record_clean_exit is False

    # 探针确认结束之后才算收尾（不是靠「句柄早就没了」）
    unit_done.set()
    assert registry.unsettled() == [] and registry.still_running() == []
    settled = await registry.shutdown(timeout=T_WAIT, cancel_timeout=T_CANCEL, final_timeout=T_FINAL)
    assert settled.clean and settled.still_running == [], settled.as_dict()
    assert _background_tasks() == []


async def test_unit_probe_failure_counts_as_not_confirmed():
    """探针自己出错时算「未确认结束」——不能把探测故障当成收尾成功。"""

    def broken_probe() -> bool:
        raise RuntimeError("探针坏了")

    registry = BackgroundTasks()
    gate = asyncio.Event()

    async def work() -> None:
        await gate.wait()

    registry.register("probe_broken", work, unit_finished=broken_probe)
    await asyncio.sleep(0)
    report = await registry.shutdown(timeout=T_WAIT, cancel_timeout=T_CANCEL, final_timeout=T_CANCEL)
    assert not report.clean and report.still_running == ["probe_broken"], report.as_dict()


async def test_shutdown_rejects_new_work_and_keeps_secrets_out_of_detail():
    """关闭后拒绝新建；detail 是给日志/用户看的出口，里面不得出现密钥原文。"""
    secret = "W3FakeSecretValue123"
    redact.register_secret(secret)
    try:
        registry = BackgroundTasks()
        gate = asyncio.Event()
        release = asyncio.Event()

        async def work() -> None:
            await gate.wait()

        async def swallows_cancel() -> None:
            while not release.is_set():
                try:
                    await gate.wait()
                except asyncio.CancelledError:  # noqa: PERF203
                    continue

        registry.register(f"derived:{secret}", swallows_cancel)
        await asyncio.sleep(0)
        report = await registry.shutdown(timeout=T_WAIT, cancel_timeout=T_CANCEL, final_timeout=T_CANCEL)

        assert not report.clean, report.as_dict()
        assert report.unfinished == [f"derived:{secret}"]
        assert secret not in report.detail, "detail 必须打码"
        assert "***redacted***" in report.detail
        with pytest.raises(BackgroundClosed):
            registry.register("late", work)

        # 收尾：不留悬挂任务
        release.set()
        gate.set()
        for _ in range(200):
            if _background_tasks() == []:
                break
            await asyncio.sleep(0.005)
        assert _background_tasks() == []
    finally:
        redact.clear_registered_secrets()


# ---------------------------------------------------------------------------
# 接线层：由 CloseReport 驱动的三个决定（Lead 的 aclose / lifespan 就照这个做）
# ---------------------------------------------------------------------------


class _FakeRuntime:
    """最小接线替身：把 `aclose()` / lifespan 的收尾顺序与三个决定固定下来。"""

    def __init__(self, conn: sqlite3.Connection, registry: BackgroundTasks) -> None:
        self.conn = conn
        self.registry = registry
        self.instances = InstanceRegistry(conn, instance_id="inst_w3")
        self.instances.start()
        self.order: list[str] = []
        self.adapter_closed = False
        self.db_closed = False

    async def aclose(self) -> lc.CloseReport:
        report = await lc.close_background(
            self.registry, timeout=T_WAIT, cancel_timeout=T_CANCEL, final_timeout=T_CANCEL
        )
        decision = lc.close_decision(report)
        self.order.append("background")
        if decision.record_clean_exit:
            self.instances.mark_clean_exit()
            report = report.with_phase(lc.PHASE_CLEAN_EXIT_RECORDED)
            self.order.append("clean_exit")
        else:
            report = report.with_phase(lc.PHASE_CLEAN_EXIT_SKIPPED)
        if decision.release_adapters:
            self.adapter_closed = True
            report = report.with_phase(lc.PHASE_ADAPTERS_RELEASED)
            self.order.append("adapter")
        else:
            report = report.with_phase(lc.PHASE_ADAPTERS_KEPT)
        return report

    def lifespan_close(self, report: lc.CloseReport) -> None:
        decision = lc.close_decision(report)
        if decision.close_database:
            self.db_closed = True
            self.order.append("db")


def _clean_exit_written(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT exited_at FROM instances WHERE instance_id = 'inst_w3'"
    ).fetchone()
    return row is not None and row["exited_at"] is not None


async def test_clean_close_writes_clean_exit_then_releases_in_order(tmp_path: Path):
    """干净关闭：写干净退出 → 释放适配器 → 关数据库，且关闭后不再写库。"""
    conn = _db(tmp_path, "a05_clean.db")
    guard = _WriteGuard(conn)
    runtime = _FakeRuntime(guard, BackgroundTasks())
    finished: list[str] = []
    started = asyncio.Event()
    gate = asyncio.Event()

    async def work() -> None:
        started.set()
        await gate.wait()
        finished.append("done")

    runtime.registry.register("work", work)
    await asyncio.wait_for(started.wait(), timeout=2.0)
    gate.set()  # 放行：它会在有界等待窗口内自然收尾

    report = await runtime.aclose()
    runtime.lifespan_close(report)
    guard.mark_closed()  # 数据库关闭（lifespan 的最后一步）
    await asyncio.sleep(0.05)  # 留出「关闭后仍然写库」的暴露窗口

    assert report.clean and report.unfinished == (), report.to_dict()
    assert runtime.order == ["background", "clean_exit", "adapter", "db"], runtime.order
    assert _clean_exit_written(guard), "clean=True 必须写干净退出"
    assert finished == ["done"]
    assert guard.writes_after_close == 0, "关闭后不得再写库"
    assert _background_tasks() == [], "无悬挂任务"


async def test_unfinished_close_writes_no_clean_exit_and_keeps_db_open(tmp_path: Path):
    """不干净关闭：**不写干净退出、不释放适配器、不关数据库**，并如实说明谁还没结束。"""
    conn = _db(tmp_path, "a05_dirty.db")
    guard = _WriteGuard(conn)
    registry = BackgroundTasks()
    runtime = _FakeRuntime(guard, registry)
    gate = asyncio.Event()
    unit_done = asyncio.Event()

    async def hands_off() -> None:
        await gate.wait()

    handle = registry.register("derived_work:frag_w3", hands_off, unit_finished=unit_done)
    await asyncio.sleep(0)
    report = await runtime.aclose()
    runtime.lifespan_close(report)

    assert not report.clean, report.to_dict()
    assert report.unfinished == ("derived_work:frag_w3",), report.to_dict()
    assert lc.PHASE_CLEAN_EXIT_SKIPPED in report.phases
    assert lc.PHASE_ADAPTERS_KEPT in report.phases
    assert runtime.order == ["background"], runtime.order
    assert not runtime.adapter_closed, "残留协程还会用适配器：不许先释放它"
    assert runtime.db_closed is False and not guard.closed, "没确认结束就不许关数据库"
    assert not _clean_exit_written(guard), "clean=False 不许写干净退出（否则污染下次启动的判定）"

    # 关不上数据库的代价换来的是：残留执行单元还能安全落库（而不是打到已关的连接）
    guard.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('w3_probe', 'late', "
        "'2026-10-10T00:00:00+00:00')"
    )
    assert guard.writes_after_close == 0, "数据库没关：这次写入不该被记成「关闭后写库」"

    # 释放底层执行单元之后，报告才是干净的（重启时按归属恢复，而不是靠这次关闭）
    unit_done.set()
    gate.set()
    await asyncio.wait([handle.task], timeout=2.0)
    assert registry.unsettled() == []
    assert _background_tasks() == []


async def test_cancelled_derived_claim_is_released_and_recoverable(tmp_path: Path):
    """重启可恢复 / 无永久 running：取消时把认领**过代次校验后**放回队列。"""
    conn = _db(tmp_path, "a05_recover.db")
    task_id = _seed_running_derived_task(conn)
    registry = BackgroundTasks()
    gate = asyncio.Event()
    releases: list[bool] = []

    async def work() -> None:
        try:
            await gate.wait()
        finally:
            # 真实路径由 MemoryLifecycle 在取消时调用；这里用真实 release 语义。
            releases.append(dt.release(conn, task_id, expected_generation=None, reason="shutdown"))

    registry.register("derived_work:frag_w3", work)
    await asyncio.sleep(0)
    assert dt.running_count(conn) == 1, "关闭前它确实在被认领着"

    report = await registry.shutdown(timeout=T_WAIT, cancel_timeout=1.0, final_timeout=T_FINAL)

    assert report.clean, report.as_dict()
    assert releases == [True], "取消路径把认领放回了队列"
    row = conn.execute(
        "SELECT state, run_after FROM derived_tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert row["state"] == dt.STATE_PENDING and row["run_after"] is None
    assert dt.running_count(conn) == 0, "不留永久 running"
    assert dt.pending_count(conn) == 1, "下次启动的 drain 会接着做（可恢复）"
    assert _background_tasks() == []


# ---------------------------------------------------------------------------
# 「后端已停止」必须来自真实结果
# ---------------------------------------------------------------------------


class _FakeClock:
    """受控时钟：`backend_stop_verdict` 不用真的 sleep。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))


def test_stop_verdict_refuses_to_claim_stopped_while_backend_still_answers():
    clock = _FakeClock()
    verdict = lc.backend_stop_verdict(
        probe=lambda: True, budget_s=0.2, interval_s=0.01, clock=clock, sleep=clock.sleep
    )
    assert verdict.stopped is False
    assert verdict.survivors == ("backend",)
    assert "不得报告「后端已停止」" in verdict.detail


def test_stop_verdict_waits_for_real_probe_to_go_quiet():
    clock = _FakeClock()
    calls = {"n": 0}

    def probe() -> bool:
        calls["n"] += 1
        return calls["n"] <= 3

    verdict = lc.backend_stop_verdict(
        probe=probe, budget_s=1.0, interval_s=0.01, clock=clock, sleep=clock.sleep
    )
    assert verdict.stopped is True and verdict.survivors == ()


def test_stop_verdict_uses_backend_self_report_first():
    """后端自报还有未结束的后台执行单元 → 直接不许报「已停止」（连通探针都不必探）。"""
    probed: list[int] = []
    close = lc.CloseReport(
        clean=False, unfinished=("derived_work:frag",), phases=(), detail="有执行单元没结束"
    )
    verdict = lc.backend_stop_verdict(
        probe=lambda: probed.append(1) or False, close_report=close, budget_s=0.1
    )
    assert verdict.stopped is False
    assert verdict.survivors == ("derived_work:frag",)
    assert "不能报告「后端已停止」" in verdict.detail
    assert probed == [], "自报不干净时不必再探测"


# ---------------------------------------------------------------------------
# 报告形状
# ---------------------------------------------------------------------------


def test_reports_are_serializable_and_decision_is_all_or_nothing():
    clean = lc.CloseReport(clean=True, phases=("wait",), detail="ok")
    dirty = lc.CloseReport(clean=False, unfinished=("a", "b"), phases=("wait",), detail="no")
    assert clean.to_dict()["clean"] is True and clean.to_dict()["unfinished"] == []
    d = lc.close_decision(dirty)
    assert (d.record_clean_exit, d.release_adapters, d.close_database) == (False, False, False)
    assert "a" in d.reason and "b" in d.reason
    c = lc.close_decision(clean)
    assert (c.record_clean_exit, c.release_adapters, c.close_database) == (True, True, True)
    report = ShutdownReport(waited=0, cancelled=1, unfinished=["x"], still_running=["x"])
    assert report.as_dict()["still_running"] == ["x"] and report.clean is False
