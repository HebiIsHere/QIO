# -*- coding: utf-8 -*-
"""P1R-F 独立验证：上一轮已知失败的根因回归 + AppContext 生命周期（不写生产代码）。

背景（证据见 _lead-logs/p1r-verify/report.md §1）：
* CI/local 完整批次里 `tests/test_rm_c_r05_derived_claims.py::test_claim_mirrors_record_owners_and_clears_it`
  在并行组偶发失败：`dt.complete(conn, task_id, expected_generation=1) is False`。
* 根因：`AppContext.__init__` 在进程级 `bind_instance(qio_xxx)`，而 `aclose()` 原来不解绑；
  同一 xdist worker 里前面跑过任何建 AppContext 的测试后，`_INSTANCE_ID` 泄漏，
  `complete()` 的 `_owner_clause` 追加 `owner_instance_id = qio_xxx` → UPDATE 0 行 → False。
* 修复：Lead 在 `AppContext.aclose()` 末尾解绑。本文件把该契约钉死：
  同进程先后建两个 AppContext，派生任务归属必须跟着**当前**实例，关闭后不得残留身份；
  且解绑不得放宽「别的实例不能终结我的任务」的所有者校验。

本文件只证明接线与生命周期事实，不声称真实模型准确率。运行：
    cd backend; .venv\\Scripts\\python.exe -m pytest tests/test_p1r_f_known_failures.py -q
"""
from __future__ import annotations

import sqlite3

import pytest

from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services import derived_tasks as dt
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


def _new_ctx(tmp_path, name: str) -> AppContext:
    conn = connect(tmp_path / name)
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


async def test_two_app_contexts_do_not_inherit_derived_instance_identity(tmp_path):
    """同进程先后两个 AppContext：关闭必须解绑，后建的实例身份必须生效。"""
    first = _new_ctx(tmp_path, "f_first.db")
    first_id = first.instance_id
    assert first_id
    assert dt.bound_instance_id() == first_id, "构造 AppContext 后应绑定自己的实例身份"

    await first.aclose()
    assert dt.bound_instance_id() is None, (
        "AppContext.aclose() 必须在进程级解绑派生任务实例身份；"
        "否则同一 worker 的后继测试/应用会沿用已关闭实例 → 派生任务归属条件永远不匹配"
    )

    second = _new_ctx(tmp_path, "f_second.db")
    try:
        assert second.instance_id != first_id
        assert dt.bound_instance_id() == second.instance_id, "第二个 AppContext 必须绑定自己的身份"

        task_id, created = dt.enqueue(second.conn, dt.KIND_SUMMARY, "frag_f_lifecycle", 1)
        assert created is True
        claimed = dt.claim_due(second.conn, instance_id=second.instance_id)
        assert [task.id for task in claimed] == [task_id]
        assert dt.complete(
            second.conn, task_id, expected_generation=claimed[0].claim_generation
        ) is True, "当前绑定实例完成自己认领的任务必须成功（这是已知失败的直接回归）"

        # 解绑不能放宽所有者校验：别的实例认领的任务，当前实例不得终结。
        other_id, _ = dt.enqueue(second.conn, dt.KIND_SUMMARY, "frag_other_owner", 1)
        other_claimed = dt.claim_due(second.conn, instance_id="inst_other")
        assert [task.id for task in other_claimed] == [other_id]
        assert dt.complete(
            second.conn, other_id, expected_generation=other_claimed[0].claim_generation
        ) is False, "所有者校验不得因为解绑/重绑而失效"
        assert dt.owned_task_ids(second.conn, "inst_other") == [other_id]
    finally:
        await second.aclose()
    assert dt.bound_instance_id() is None


async def test_derived_complete_after_a_closed_app_context(tmp_path):
    """上一轮并行失败的最小复现场景：先建过并关闭一个 AppContext，再做派生任务完成。"""
    prior = _new_ctx(tmp_path, "f_prior.db")
    await prior.aclose()
    assert dt.bound_instance_id() is None

    conn = connect(tmp_path / "f_derived.db")
    apply_migrations(conn)
    try:
        task_id, _ = dt.enqueue(conn, dt.KIND_SUMMARY, "frag_x", 1)
        claimed = dt.claim_due(conn, instance_id="inst_A")
        assert dt.owned_task_ids(conn, "inst_A") == [task_id]
        assert dt.complete(
            conn, task_id, expected_generation=claimed[0].claim_generation
        ) is True, (
            "没有本地绑定（解绑之后）时 complete 必须走诚实降级，不得带已关闭实例的 owner 条件"
        )
        assert dt.owned_task_ids(conn, "inst_A") == []
    finally:
        conn.close()
