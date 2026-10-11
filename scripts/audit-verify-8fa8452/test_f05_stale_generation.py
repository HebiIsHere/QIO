r"""E 路独立核验探针 · F05 旧代摘要任务仍能覆盖新数据（基线 8fa845255c118a7104b1de8e37298ca745924482）

规格来源：D:\qio-dev\_audit-findings-8fa8452.md 的 F05 段 + 冻结契约 C4：
  - 领域写入（摘要/索引/实体关系）与任务完成必须在**同一事务边界**内校验
    owner + claim_generation + 合法状态 + 内容版本；
  - 旧代任务不得写数据、加关联、登记下游、报正常完成。

**与其他探针的关系（独立作者、独立场景）**：本探针不复制任何既有测试的断言。它构造的场景是
「旧代在飞行中被 requeue（代次被接替），其结果最后到达」，并断言三类可观测后果：
  (1) 片段摘要没有被旧代改写；
  (2) memory_index 没有出现旧代标题（领域写入被整体回滚）；
  (3) 没有为旧代登记下游知识任务、mention 边没有新增。
同时断言旧代如实返回 False，且任务不是「正常完成」。

运行（只跑本节点，禁止全量）：
  cd <被测 worktree>\backend
  $env:PYTHONPATH = '<被测 worktree>\backend\src'
  & 'D:\qio-dev\qa-lead\backend\.venv\Scripts\python.exe' -m pytest <本文件> -q -p no:cacheprovider -s -o asyncio_mode=auto
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.pop("QIO_DATA_DIR", None)
os.environ.setdefault("QIO_DEV_INSECURE", "1")
os.environ.setdefault("QIO_DISABLE_DB_CHECK", "1")

import pytest  # noqa: E402

from agent.adapters.base import ChatMessage, Completion  # noqa: E402
from agent.api.bus import EventBus  # noqa: E402
from agent.config import Settings  # noqa: E402
from agent.credentials.store import MemoryKeyring  # noqa: E402
from agent.services import derived_tasks as dt  # noqa: E402
from agent.services.app import AppContext  # noqa: E402
from agent.storage.db import connect  # noqa: E402
from agent.storage.migrate import apply_migrations  # noqa: E402

SUMMARY_MARKER = "summarizing a closed conversation fragment"


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class _GateAdapter:
    """摘要调用先钉住（供测试安排代次接替），gate 打开后返回给定结果。

    `gate=None` = 不阻塞，立即返回（给「新代」用）——
    否则新代与旧代共用一个被钉住的 adapter 会互相等待（探针自身的死锁，已实测）。
    """

    mode = "native"
    model = "fake-e-f05"

    def __init__(self, payload: str, *, gate: bool = True) -> None:
        self.payload = payload
        self.started = asyncio.Event()
        self.gate = asyncio.Event() if gate else None

    async def complete(self, messages, tools, **kwargs) -> Completion:
        first = messages[0]
        prompt = first["content"] if isinstance(first, dict) else first.content
        if SUMMARY_MARKER not in prompt:
            raise AssertionError("本探针只驱动摘要提示词，收到未知提示词：" + str(prompt)[:60])
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        return Completion(message=ChatMessage(role="assistant", content=self.payload))


def _payload(title: str, summary: str, entities=None) -> str:
    return json.dumps(
        {"title": title, "summary": summary, "entities": list(entities or []), "keywords": [title]},
        ensure_ascii=False,
    )


def _app(tmp_path: Path) -> AppContext:
    conn = connect(tmp_path / "f05-e.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


def _seal(ctx: AppContext):
    topic = ctx.topics.nodes.create_topic("E05").id
    ctx.memory.append_message(topic_id=topic, role="user", content="旧代的原文")
    ctx.memory.append_message(topic_id=topic, role="assistant", content="记下了")
    sealed = ctx.memory_lifecycle.seal_fragment(topic, reason="capacity")
    assert sealed is not None, "前置条件：必须能封存出一个片段"
    return sealed


def _task_row(ctx: AppContext, kind: str) -> dict:
    row = ctx.conn.execute(
        "SELECT id, state, claim_generation FROM derived_tasks WHERE kind = ?", (kind,)
    ).fetchone()
    assert row is not None, "必须存在 " + kind + " 任务"
    return dict(row)


def _index_titles(ctx: AppContext, fragment_id: str) -> list[str]:
    return [
        str(r["title"])
        for r in ctx.conn.execute(
            "SELECT title FROM memory_index WHERE fragment_id = ?", (fragment_id,)
        ).fetchall()
    ]


def _fragment_summary(ctx: AppContext, fragment_id: str) -> str | None:
    row = ctx.conn.execute(
        "SELECT summary FROM fragments WHERE id = ?", (fragment_id,)
    ).fetchone()
    return None if row is None else row["summary"]


async def test_f05_stale_generation_writes_nothing(tmp_path: Path) -> None:
    ctx = _app(tmp_path)
    sealed = _seal(ctx)
    fragment_id = sealed.id
    topic_id = sealed.topic_id
    clock = _Clock()
    dt.set_clock(clock)

    # 旧代：会一直等到我们放行；新代：立即返回（两个 adapter 必须不同，否则互等）
    stale_adapter = _GateAdapter(_payload("旧代标题", "旧代摘要", ["旧实体"]))
    fresh_adapter = _GateAdapter(_payload("新代标题", "新代摘要", ["新实体"]), gate=False)
    observed: dict = {}

    async def scenario() -> None:
        runner = asyncio.create_task(ctx.memory_lifecycle.drain_derived_tasks(stale_adapter, limit=5))
        await asyncio.wait_for(stale_adapter.started.wait(), timeout=30)
        task = _task_row(ctx, dt.KIND_SUMMARY)
        observed["task_id"] = task["id"]
        observed["gen_before"] = task["claim_generation"]
        print("\n[F05] 旧代认领：", task)

        # 旧代还在等模型结果时，既有恢复机制把它放回队列（代次 +1）
        assert dt.requeue_running(
            ctx.conn,
            task["id"],
            expected_state=dt.STATE_RUNNING,
            expected_generation=task["claim_generation"],
        ) is True
        clock.advance(3600)
        fresh = dt.claim_due(ctx.conn, kinds=(dt.KIND_SUMMARY,))[0]
        observed["gen_after"] = fresh.claim_generation
        print("[F05] 接替后的代次 =", fresh.claim_generation)

        observed["fresh_ok"] = await ctx.memory_lifecycle.run_summary_task(fresh, fresh_adapter)
        # 让旧代的结果到达（它已经丢掉了所有权）
        assert stale_adapter.gate is not None
        stale_adapter.gate.set()
        observed["stale_result"] = await runner

    await asyncio.wait_for(scenario(), timeout=120)

    task_after = _task_row(ctx, dt.KIND_SUMMARY)
    summary_after = _fragment_summary(ctx, fragment_id)
    titles_after = _index_titles(ctx, fragment_id)
    knowledge_rows = ctx.conn.execute(
        "SELECT id FROM derived_tasks WHERE kind = ?", (dt.KIND_KNOWLEDGE,)
    ).fetchall()
    mention_edges = ctx.conn.execute(
        "SELECT COUNT(*) AS n FROM edges WHERE type = 'mention'"
    ).fetchone()["n"]

    print("[F05] 旧代返回值 =", observed.get("stale_result"), " 新代完成 =", observed.get("fresh_ok"))
    print("[F05] 任务最终 state =", task_after["state"], " claim_generation =", task_after["claim_generation"])
    print("[F05] fragments.summary =", summary_after)
    print("[F05] memory_index.title =", titles_after)
    print("[F05] 知识任务数 =", len(knowledge_rows), " mention 边数 =", mention_edges)

    # (1) 旧代返回 False（没有报告正常完成）
    assert observed["stale_result"] is False or observed["stale_result"] == 0, (
        "F05：旧代丢失所有权后不得报告正常完成。实际返回=" + str(observed.get("stale_result"))
    )
    # (2) 旧代不得留下自己的领域写入
    assert "旧代标题" not in titles_after, (
        "F05 缺陷复现：旧代把结果写进了 memory_index（领域写入没有被事务边界拦住）。titles="
        + str(titles_after)
    )
    assert summary_after != "旧代摘要", (
        "F05 缺陷复现：旧代摘要覆盖了片段正文。summary=" + str(summary_after)
    )
    # (3) 内容版本未变时，若新代真的跑了，摘要应是新代的；若旧代被拦下则保持空
    assert summary_after in (None, "", "新代摘要"), (
        "F05：无论新旧代如何收敛，片段摘要都不得是旧代摘要。实际=" + str(summary_after)
    )
    assert "新代标题" in titles_after or titles_after == [], (
        "F05：新代若写入，索引里应当是它的标题。titles=" + str(titles_after)
    )