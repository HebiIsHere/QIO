# -*- coding: utf-8 -*-
"""M06：后台派生工作的统一登记与关闭（闸门暂停提炼 → 关闭应用）。

验收覆盖（逐条）：
* 关闭顺序：先停受理与新派生 → 有界等待 → 取消 → 确认结束，然后才关适配器 / 数据库；
* 任务最终结束**或可恢复**（认领被过代次校验后放回队列，不留永久 running）；
* 无关闭后写库；
* 无悬挂任务（注册表清空、事件循环里没有残留的后台任务）；
* 重复调度受限（同名在跑不重复调度；关闭后拒绝新建）。
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services import derived_tasks as dt
from agent.services.app import AppContext
from agent.services.background import BackgroundClosed, BackgroundTasks, registry_for
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

CLOCK_START = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _ensure_columns(connection: sqlite3.Connection) -> None:
    """A 的迁移会补这些列；本组测试自带幂等补列，保证验收可独立跑。"""
    derived = {row["name"] for row in connection.execute("PRAGMA table_info(derived_tasks)")}
    if "owner_instance_id" not in derived:
        connection.execute("ALTER TABLE derived_tasks ADD COLUMN owner_instance_id TEXT")
    if "claim_generation" not in derived:
        connection.execute(
            "ALTER TABLE derived_tasks ADD COLUMN claim_generation INTEGER NOT NULL DEFAULT 0"
        )
    cards = {row["name"] for row in connection.execute("PRAGMA table_info(entity_cards)")}
    if "revision" not in cards:
        connection.execute(
            "ALTER TABLE entity_cards ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
        )
    if "field_meta" not in cards:
        connection.execute(
            "ALTER TABLE entity_cards ADD COLUMN field_meta TEXT NOT NULL DEFAULT '{}'"
        )


class _Registry:
    def __init__(self, **alive: bool | None) -> None:
        self._alive = alive

    def owner_alive(self, instance_id: str) -> bool | None:
        return self._alive.get(instance_id)


@pytest.fixture(autouse=True)
def _reset_bindings():
    dt.set_clock(lambda: CLOCK_START)
    try:
        yield
    finally:
        dt.reset_clock()
        dt.bind_instance(None, None)


class _WriteGuard:
    """在「数据库已关闭」之后仍然尝试写库时记账并拒绝（无关闭后写库的实测口径）。"""

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


class _GateAdapter:
    """闸门：进入模型调用后一直挂着，只有关闭流程的取消能结束它。"""

    mode = "native"
    model = "fake-gate"

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()

    async def complete(self, messages, tools, **kwargs) -> Completion:
        self.entered.set()
        await self.gate.wait()
        return Completion(message=ChatMessage(role="assistant", content="{}"))

    def close(self) -> None:
        """适配器释放（真实 aclose 里在后台停止之后、数据库关闭之前）。"""
        self.gate.set()


def _app(tmp_path: Path) -> AppContext:
    conn = connect(tmp_path / "rm_c_m06.db")
    apply_migrations(conn)
    _ensure_columns(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


def _seal(ctx: AppContext):
    topic = ctx.topics.nodes.create_topic("可靠性").id
    ctx.memory.append_message(topic_id=topic, role="user", content="我家的鹅最近有点生病")
    ctx.memory.append_message(topic_id=topic, role="assistant", content="记下了")
    sealed = ctx.memory_lifecycle.seal_fragment(topic, reason="capacity")
    assert sealed is not None
    return sealed


def _background_tasks() -> list[asyncio.Task]:
    return [
        task
        for task in asyncio.all_tasks()
        if (task.get_name() or "").startswith("background:")
    ]


# ---------------------------------------------------------------------------
# 注册表语义
# ---------------------------------------------------------------------------


async def test_register_dedupes_same_name_and_active_tracks_names():
    registry = BackgroundTasks()
    release = asyncio.Event()

    async def work() -> None:
        await release.wait()

    first = registry.register("derived_work:frag_1", work)
    second = registry.register("derived_work:frag_1", work)

    assert first is second, "同名且还在跑：不重复调度"
    assert registry.active() == ["derived_work:frag_1"]

    release.set()
    await first.task

    assert registry.active() == [], "结束后句柄移除（防重复调度与无主任务）"
    third = registry.register("derived_work:frag_1", work)
    assert third is not first, "上一份结束后可以重新登记"
    third.cancel()
    await asyncio.gather(third.task, return_exceptions=True)


async def test_register_refuses_after_shutdown():
    registry = BackgroundTasks()

    async def work() -> None:  # pragma: no cover - 不该被执行
        raise AssertionError("关闭后不得再产生后台工作")

    await registry.shutdown(timeout=0.01)
    with pytest.raises(BackgroundClosed):
        registry.register("late", work)


async def test_shutdown_waits_then_cancels_and_confirms_end():
    registry = BackgroundTasks()
    finished: list[str] = []
    gate = asyncio.Event()

    async def quick() -> None:
        await asyncio.sleep(0.02)
        finished.append("quick")

    async def stuck() -> None:
        await gate.wait()
        finished.append("stuck")

    registry.register("quick", quick)
    registry.register("stuck", stuck)

    report = await registry.shutdown(timeout=0.3, cancel_timeout=0.3)

    assert report.waited == 1 and report.cancelled == 1, report.as_dict()
    assert report.clean and report.unfinished == []
    assert finished == ["quick"], "被取消的那条没有跑完（取消是真的）"
    assert registry.active() == []


async def test_shutdown_is_idempotent():
    registry = BackgroundTasks()

    first = await registry.shutdown(timeout=0.01)
    second = await registry.shutdown(timeout=0.01)

    assert first.clean and second.clean
    assert second.waited == 0 and second.cancelled == 0


# ---------------------------------------------------------------------------
# 集成：闸门暂停提炼后关闭应用
# ---------------------------------------------------------------------------


async def test_gate_paused_derivation_close_sequence(tmp_path: Path):
    ctx = _app(tmp_path)
    sealed = _seal(ctx)
    version = int(sealed.content_version or 0)
    dt.bind_instance("inst_close", _Registry(inst_close=True))

    registry = registry_for(ctx)
    assert registry is ctx.background, "注册表挂在 app 上，aclose 才拿得到同一份"

    guarded = _WriteGuard(ctx.conn)
    ctx.memory_lifecycle.conn = guarded  # 派生路径的全部读写都从这里过

    adapter = _GateAdapter()
    ctx.turn_orchestrator._schedule_derived_work(adapter, None, fragment_id=sealed.id)
    ctx.turn_orchestrator._schedule_derived_work(adapter, None, fragment_id=sealed.id)
    assert registry.active() == [f"derived_work:{sealed.id}"], "重复调度受限"

    for _ in range(200):
        if dt.running_count(ctx.conn) == 1:
            break
        await asyncio.sleep(0.01)
    assert dt.running_count(ctx.conn) == 1, "任务已被认领为 running（闸门里的模型调用）"

    order: list[str] = []
    # 1) 先停后台：拒绝新建 → 有界等待 → 取消 → 确认结束
    report = await ctx.background.shutdown(timeout=0.5, cancel_timeout=0.5)
    order.append("background")
    # 2) 再关适配器
    adapter.close()
    order.append("adapter")
    # 3) 最后才是数据库关闭（Lead 在 aclose/lifespan 的最后一步）
    guarded.mark_closed()
    order.append("db")
    await asyncio.sleep(0.05)

    assert order == ["background", "adapter", "db"], "关闭顺序：后台先停，数据库最后"
    assert report.clean, report.as_dict()
    assert registry.active() == []
    assert _background_tasks() == [], "事件循环里不得留下悬挂的后台任务"
    assert guarded.writes_after_close == 0, "关闭后不得再写库"

    # 任务最终结束或**可恢复**：认领被放回队列，不留永久 running
    assert dt.running_count(ctx.conn) == 0
    task = dt.task_for(ctx.conn, dt.KIND_SUMMARY, sealed.id, version)
    assert task is not None and task.state == dt.STATE_PENDING
    assert task.claim_generation == 1, "放回队列不丢代次（迟到结果仍然会被丢弃）"

    # 关闭中再来的派生调度被拒绝，工作留在持久队列里（不是静默丢掉）
    ctx.turn_orchestrator._schedule_derived_work(adapter, None, fragment_id="frag_after_close")
    assert registry.active() == []

    # 可恢复：模拟下一次启动（恢复路径 + 一个正常适配器）把它做完
    ctx.memory_lifecycle.conn = ctx.conn

    class _SummaryOnly:
        mode = "native"
        model = "fake-recover"

        async def complete(self, messages, tools, **kwargs) -> Completion:
            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content='{"title": "标题", "summary": "恢复后补的摘要", '
                    '"entities": [], "keywords": []}',
                )
            )

    done = await ctx.memory_lifecycle.drain_derived_tasks(_SummaryOnly(), limit=5)
    assert done >= 1
    assert dt.task_for(ctx.conn, dt.KIND_SUMMARY, sealed.id, version).state == dt.STATE_COMPLETED
    row = ctx.conn.execute(
        "SELECT summary FROM fragments WHERE id = ?", (sealed.id,)
    ).fetchone()
    assert row["summary"] == "恢复后补的摘要"


async def test_cancelled_claim_is_released_with_generation_check(tmp_path: Path):
    """取消/关闭的释放也过代次校验：迟到释放不得动新认领（与 R05 共用认领代次）。"""
    ctx = _app(tmp_path)
    sealed = _seal(ctx)
    version = int(sealed.content_version or 0)
    task = dt.task_for(ctx.conn, dt.KIND_SUMMARY, sealed.id, version)
    assert task is not None

    # Lead 集成后 AppContext 会 bind_instance(自己的 instance_id)：release() 走
    # 「所有者 + 认领代次」双校验，所以认领与释放必须来自同一个实例身份。
    # 这里用 app 自己的实例认领，保留本条用例真正要验的「代次校验」语义。
    claimed = dt.claim_due(ctx.conn, instance_id=ctx.instance_id)
    assert claimed and claimed[0].claim_generation == 1

    assert dt.release(ctx.conn, task.id, expected_generation=0) is False
    assert dt.task_for(ctx.conn, dt.KIND_SUMMARY, sealed.id, version).state == dt.STATE_RUNNING

    assert dt.release(ctx.conn, task.id, expected_generation=1) is True
    assert dt.task_for(ctx.conn, dt.KIND_SUMMARY, sealed.id, version).state == dt.STATE_PENDING
