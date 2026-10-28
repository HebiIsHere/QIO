# -*- coding: utf-8 -*-
"""Release step 1 / B：并行派生工作各持自己的用途配置（消除共享可变状态）。

缺陷（修复前）：MemoryLifecycle.use_adapters 把「按 kind 的 adapter 映射」写进
**实例属性** self._task_adapters。两个后台派生工作交错时：

* A 的第二批任务读到 B 的配置（A 的实体提炼被悄悄换成 B 的模型）；
* A 先结束时把属性恢复成 None，B 的后续批次读不到任何用途配置；
* B 结束时又把 A 的旧配置恢复成实例属性，残留给之后的工作。

验收口径（对应任务 B）：
1. 每项工作使用自己开始时快照的配置，不会被其他工作覆盖；
2. 同一项工作的 summary / knowledge / entities 三阶段始终用自己那份；
3. 用户中途改了用途分配只影响新开始的工作（本文件用两份不同映射交错验证）；
4. 成功 / 取消收尾不污染其他工作，也不留下可被后续工作误用的旧配置；
5. 走真实 drain_derived_tasks 路径（只替换 claim_due / 三个 runner，不用 sleep）。

全部用 fake adapter / fake 任务，不联网、不读真实密钥。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services import derived_tasks
from agent.services.app import AppContext
from agent.services.memory_lifecycle import MemoryLifecycle
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


class _LabelAdapter:
    """可辨识 label 的 fake adapter（断言失败时能直接看出拿到了谁的配置）。"""

    def __init__(self, label: str) -> None:
        self.label = label
        self.model = f"model-{label}"

    def __repr__(self) -> str:  # pragma: no cover - 只在失败信息里出现
        return f"<adapter {self.label}>"


class _Task:
    """最小派生任务（只带 drain 需要的字段）。"""

    def __init__(self, job: str, kind: str) -> None:
        self.job = job
        self.kind = kind
        self.id = f"{job}-{kind}"
        self.fragment_id = f"frag-{job}"
        self.content_version = 1
        self.claim_generation = 1

    def __repr__(self) -> str:  # pragma: no cover - 只在失败信息里出现
        return self.id


def _lifecycle() -> MemoryLifecycle:
    """不接数据库的 lifecycle：claim / runner / 落库出口全部由测试替换。"""
    return MemoryLifecycle(
        conn=object(),
        fragments=object(),
        topics=object(),
        index_builder=object(),
        user_root_id=lambda: "user-root",
        refresh_selector=lambda: None,
    )


class _DrainHarness:
    """把 drain 的认领序列与三个 runner 换成可控的 fake，记录每次派发用的 adapter。"""

    def __init__(self, monkeypatch, claims: list[list[_Task]]) -> None:
        self.claims = claims
        self.claim_calls = 0
        self.used: list[tuple[str, str, str]] = []
        self.failures: list[tuple[str, str, int]] = []
        self.releases: list[tuple[str, str]] = []
        self.monkeypatch = monkeypatch

    def install(self, lifecycle: MemoryLifecycle, runners: dict) -> None:
        monkeypatch = self.monkeypatch

        def claim_due(conn, limit, kinds):
            self.claim_calls += 1
            queued = self.claims.pop(0) if self.claims else []
            return [task for task in queued if task.kind in kinds]

        def fail(conn, task_id, reason, *, expected_generation):
            self.failures.append((task_id, reason, expected_generation))

        def release(conn, task_id, *, expected_generation, reason=""):
            self.releases.append((task_id, reason))

        monkeypatch.setattr(derived_tasks, "claim_due", claim_due)
        monkeypatch.setattr(derived_tasks, "fail", fail)
        monkeypatch.setattr(derived_tasks, "release", release)

        # drain 入口的有界补派不碰数据库（本文件只验用途配置隔离）。
        monkeypatch.setattr(lifecycle, "backfill_entity_tasks", lambda limit=2: 0)

        for kind, runner in runners.items():
            attr = {
                derived_tasks.KIND_SUMMARY: "run_summary_task",
                derived_tasks.KIND_KNOWLEDGE: "run_knowledge_task",
                derived_tasks.KIND_ENTITIES: "run_entities_task",
            }[kind]
            monkeypatch.setattr(lifecycle, attr, runner)


def _record_runner(harness: _DrainHarness, hooks=None):
    """记录 (kind, task, adapter) 的 runner；hooks 可在进入后暂停/释放。"""

    async def runner(task, adapter, *, tracer=None):
        harness.used.append((task.kind, task.id, adapter.label))
        if hooks is not None:
            await hooks(task, adapter)
        return True

    return runner


# ---------------------------------------------------------------------------
# 1) 两个工作交错：A 暂停在阶段间，B 启动并暂停，放行后 A 的后续阶段仍是 A 的配置
# ---------------------------------------------------------------------------


async def test_interleaved_derivations_keep_their_own_adapters_per_kind(monkeypatch):
    lifecycle = _lifecycle()
    adapters_a = {
        derived_tasks.KIND_SUMMARY: _LabelAdapter("A-summary"),
        derived_tasks.KIND_KNOWLEDGE: _LabelAdapter("A-knowledge"),
        derived_tasks.KIND_ENTITIES: _LabelAdapter("A-entities"),
    }
    adapters_b = {
        derived_tasks.KIND_SUMMARY: _LabelAdapter("B-summary"),
        derived_tasks.KIND_KNOWLEDGE: _LabelAdapter("B-knowledge"),
        derived_tasks.KIND_ENTITIES: _LabelAdapter("B-entities"),
    }
    harness = _DrainHarness(
        monkeypatch,
        claims=[
            # A 的第一批（summary + knowledge）、B 的第一批
            [_Task("a", derived_tasks.KIND_SUMMARY), _Task("a", derived_tasks.KIND_KNOWLEDGE)],
            [_Task("b", derived_tasks.KIND_SUMMARY), _Task("b", derived_tasks.KIND_KNOWLEDGE)],
            # A 的第二批（entities）—— 此时 B 已经把自己的配置放进来了
            [_Task("a", derived_tasks.KIND_ENTITIES)],
            # B 的第二批（entities）—— 此时 A 已经结束
            [_Task("b", derived_tasks.KIND_ENTITIES)],
        ],
    )
    a_started = asyncio.Event()
    b_started = asyncio.Event()
    a_release = asyncio.Event()
    b_release = asyncio.Event()

    async def summary_hooks(task: _Task, adapter) -> None:
        if task.job == "a":
            a_started.set()
            await a_release.wait()
        else:
            b_started.set()
            await b_release.wait()

    harness.install(
        lifecycle,
        runners={
            derived_tasks.KIND_SUMMARY: _record_runner(harness, summary_hooks),
            derived_tasks.KIND_KNOWLEDGE: _record_runner(harness),
            derived_tasks.KIND_ENTITIES: _record_runner(harness),
        },
    )

    async def run_a() -> None:
        with lifecycle.use_adapters(adapters_a):
            await lifecycle.drain_derived_tasks(None, limit=5)

    async def run_b() -> None:
        with lifecycle.use_adapters(adapters_b):
            await lifecycle.drain_derived_tasks(None, limit=5)

    task_a = asyncio.create_task(run_a())
    await asyncio.wait_for(a_started.wait(), timeout=5)
    task_b = asyncio.create_task(run_b())
    await asyncio.wait_for(b_started.wait(), timeout=5)

    # A 的第二批在 B 的配置「生效期间」执行：它必须仍然拿 A 自己的配置。
    a_release.set()
    await asyncio.wait_for(task_a, timeout=5)

    b_release.set()
    await asyncio.wait_for(task_b, timeout=5)

    assert harness.failures == [], f"用途配置被覆盖会让任务被错判失败：{harness.failures}"
    # 记录顺序：A 的 summary 先进入（随后暂停）→ B 的 summary 进入（随后暂停）→
    # 放行 A 的后续批次 → 放行 B 的后续批次。
    assert harness.used == [
        ("summary", "a-summary", "A-summary"),
        ("summary", "b-summary", "B-summary"),
        ("knowledge", "a-knowledge", "A-knowledge"),
        ("entities", "a-entities", "A-entities"),
        ("knowledge", "b-knowledge", "B-knowledge"),
        ("entities", "b-entities", "B-entities"),
    ], (
        "每项派生工作的 summary/knowledge/entities 必须始终使用自己开始时快照的那份配置，"
        f"不能被并行工作覆盖；实际={harness.used}"
    )


# ---------------------------------------------------------------------------
# 2) A 被取消后 B 的后续阶段仍用 B 的配置；结束后无残留被第三方工作读到
# ---------------------------------------------------------------------------


async def test_cancelled_work_does_not_clobber_running_or_later_work(monkeypatch):
    lifecycle = _lifecycle()
    adapters_a = {derived_tasks.KIND_SUMMARY: _LabelAdapter("A-summary")}
    adapters_b = {
        derived_tasks.KIND_SUMMARY: _LabelAdapter("B-summary"),
        derived_tasks.KIND_ENTITIES: _LabelAdapter("B-entities"),
    }
    fallback = _LabelAdapter("main-fallback")

    harness = _DrainHarness(
        monkeypatch,
        claims=[
            [_Task("a", derived_tasks.KIND_SUMMARY)],
            [_Task("b", derived_tasks.KIND_SUMMARY)],
            [_Task("b", derived_tasks.KIND_ENTITIES)],
            [_Task("c", derived_tasks.KIND_SUMMARY)],
        ],
    )
    a_started = asyncio.Event()
    b_started = asyncio.Event()
    b_release = asyncio.Event()

    async def summary_hooks(task: _Task, adapter) -> None:
        if task.job == "a":
            a_started.set()
            # A 一直卡在这里，直到测试取消它
            await asyncio.Event().wait()
        elif task.job == "b":
            b_started.set()
            await b_release.wait()

    harness.install(
        lifecycle,
        runners={
            derived_tasks.KIND_SUMMARY: _record_runner(harness, summary_hooks),
            derived_tasks.KIND_ENTITIES: _record_runner(harness),
        },
    )

    async def run_a() -> None:
        with lifecycle.use_adapters(adapters_a):
            await lifecycle.drain_derived_tasks(None, limit=5)

    async def run_b() -> None:
        with lifecycle.use_adapters(adapters_b):
            await lifecycle.drain_derived_tasks(None, limit=5)

    task_a = asyncio.create_task(run_a())
    await asyncio.wait_for(a_started.wait(), timeout=5)
    task_b = asyncio.create_task(run_b())
    await asyncio.wait_for(b_started.wait(), timeout=5)

    # A 在 B 进行中被取消：A 的收尾不得动到 B 正在用的配置。
    task_a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task_a
    assert harness.releases, "取消必须把认领放回队列（不留下永久 running）"

    b_release.set()
    await asyncio.wait_for(task_b, timeout=5)

    assert harness.failures == [], f"B 的后续阶段被 A 的收尾污染：{harness.failures}"
    assert harness.used == [
        ("summary", "a-summary", "A-summary"),
        ("summary", "b-summary", "B-summary"),
        ("entities", "b-entities", "B-entities"),
    ], f"B 的每个阶段必须仍用 B 的配置；实际={harness.used}"

    # 第三方工作（同一 lifecycle 实例、没有 use_adapters）：不得读到 A/B 留下的旧配置，
    # 只能回落到调用方给的 main adapter。
    await asyncio.wait_for(lifecycle.drain_derived_tasks(fallback, limit=5), timeout=5)

    third_used = [row for row in harness.used if row[1] == "c-summary"]
    assert third_used == [("summary", "c-summary", "main-fallback")], (
        "前序工作结束后不得把旧配置留在实例上，让之后的工作误用；"
        f"实际={third_used}"
    )


# ---------------------------------------------------------------------------
# 3) 派发接线：_schedule_derived_work 把本次解析出的配置交给真实 drain
# ---------------------------------------------------------------------------


@pytest.fixture()
def app_ctx(tmp_path: Path):
    conn = connect(tmp_path / "release_step1_b.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    try:
        yield ctx
    finally:
        conn.close()


async def test_schedule_derived_work_hands_snapshot_to_real_drain(app_ctx, monkeypatch):
    """编排器派发路径的接线：每次派发解析一次，并把这份快照交给真实 drain。"""
    ctx = app_ctx
    summary_adapter = _LabelAdapter("dispatch-summary")
    build_calls: list[dict] = []

    async def fake_build_derived_adapters():
        adapters = {derived_tasks.KIND_SUMMARY: summary_adapter}
        build_calls.append(adapters)
        return adapters

    monkeypatch.setattr(ctx, "build_derived_adapters", fake_build_derived_adapters)
    monkeypatch.setattr(ctx.memory_lifecycle, "backfill_entity_tasks", lambda limit=2: 0)

    task = _Task("dispatch", derived_tasks.KIND_SUMMARY)
    claims = [[task]]

    def claim_due(conn, limit, kinds):
        queued = claims.pop(0) if claims else []
        return [t for t in queued if t.kind in kinds]

    monkeypatch.setattr(derived_tasks, "claim_due", claim_due)
    used: list[object] = []

    async def summary_runner(task_obj, adapter, *, tracer=None):
        used.append(adapter)
        return True

    monkeypatch.setattr(ctx.memory_lifecycle, "run_summary_task", summary_runner)

    ctx.turn_orchestrator._schedule_derived_work(None, None, fragment_id="frag-dispatch")
    handle = ctx.background.handle("derived_work:frag-dispatch")
    assert handle is not None, "派发必须登记在后台注册表里"
    await asyncio.wait_for(handle.task, timeout=5)

    assert len(build_calls) == 1, "一次派发只解析一次用途配置（快照）"
    assert used == [summary_adapter], "派发解析出的 adapter 必须真的交给 drain 执行"
