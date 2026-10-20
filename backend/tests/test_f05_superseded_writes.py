# -*- coding: utf-8 -*-
"""F05：旧代派生任务不得覆盖新数据（C 组定向验收）。

缺陷（基线 8fa8452）
--------------------

三个派生 runner（摘要 / 知识 / 实体）形状相同：

1. 认领后把 task.claim_generation 记在本地变量里；
2. **先写领域数据**（摘要 / memory_index / entity_cards / nodes / edges / knowledge）；
3. 最后才调 derived_tasks.complete(..., expected_generation=generation)。

第 3 步的代次校验确实会拒绝迟到结果 —— 但**拒绝得太晚**：数据已经落库了。
一条失去所有权（被恢复重排 / 被用户重排 / 取消后接替）的任务，仍然能把旧摘要
覆盖新摘要、把旧结论写进 memory_index 与知识表、把旧实体关系边加进图里，
只是最后「没有报告完成」。

修法：域写入与「任务完成」放进**同一个事务边界**，事务开头先核验
owner + claim_generation + state=running + 内容版本（derived_tasks.hold）；
核验不过整体回滚，调用方如实返回 False。

本文件只跑定向节点，使用临时库 + 假适配器，不联网、不碰用户数据。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services import derived_tasks as dt
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

from agent.prompts import PROMPTS

SUMMARY_MARKER = "summarizing a closed conversation fragment"
# Phase 1：实体 / 知识提炼改为固定 system（prompts.PROMPTS）+ 程序 schema，
# 这里按固定文案本身的稳定前缀分派（不是全文快照断言）。
ENTITY_MARKER = PROMPTS["ENTITY_EXTRACTION_SYSTEM"][:20]
KNOWLEDGE_MARKER = PROMPTS["KNOWLEDGE_EXTRACTION_SYSTEM"][:20]


def _summary_payload(title: str, summary: str, entities=None) -> str:
    return json.dumps(
        {"title": title, "summary": summary, "entities": list(entities or []), "keywords": [title]},
        ensure_ascii=False,
    )


def _entities_payload(names) -> str:
    return json.dumps(
        {
            "entities": [
                {
                    "name": name,
                    "aliases": [],
                    "kind": "对象",
                    "summary": "自动提炼",
                    "attributes": [],
                    "relations": [],
                }
                for name in names
            ]
        },
        ensure_ascii=False,
    )


def _knowledge_payload(contents) -> str:
    return json.dumps(
        {
            "candidates": [
                {"content": text, "category": "general_fact", "attach": "topic"}
                for text in contents
            ]
        },
        ensure_ascii=False,
    )


class _ScriptedAdapter:
    """按提示词分派的假 provider：每种提示词给一个（可等待的）响应。

    响应可以是字符串，也可以是协程函数 —— 测试用它制造「旧代结果最后到达」。
    """

    mode = "native"
    model = "fake-f05"

    def __init__(self, **responses) -> None:
        self.responses = responses
        self.calls: list[str] = []

    async def complete(self, messages, tools, **kwargs) -> Completion:
        first = messages[0]
        prompt = first["content"] if isinstance(first, dict) else first.content
        if SUMMARY_MARKER in prompt:
            key = "summary"
        elif KNOWLEDGE_MARKER in prompt:
            key = "knowledge"
        elif ENTITY_MARKER in prompt:
            key = "entities"
        else:  # pragma: no cover - 未知提示词直接暴露，不静默
            raise AssertionError(f"未知提示词：{prompt[:60]!r}")
        self.calls.append(key)
        value = self.responses.get(key)
        if callable(value):
            value = await value()
        if value is None:
            value = _summary_payload("默认", "默认摘要")
        return Completion(message=ChatMessage(role="assistant", content=value))


def _app(tmp_path: Path) -> AppContext:
    conn = connect(tmp_path / "f05.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


def _seal(ctx: AppContext):
    topic = ctx.topics.nodes.create_topic("F05").id
    ctx.memory.append_message(topic_id=topic, role="user", content="我家的鹅最近有点生病")
    ctx.memory.append_message(topic_id=topic, role="assistant", content="记下了，我留意着")
    sealed = ctx.memory_lifecycle.seal_fragment(topic, reason="capacity")
    assert sealed is not None
    return sealed


class _Clock:
    """受控时钟：让「刚认领的任务」在下一次 claim_due 里被判为超期可回收。"""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds: float):
        self.now = self.now + timedelta(seconds=seconds)


def _fragment_row(ctx: AppContext, fragment_id: str) -> dict:
    row = ctx.conn.execute(
        "SELECT summary, summary_version FROM fragments WHERE id = ?", (fragment_id,)
    ).fetchone()
    return dict(row)


def _index_rows(ctx: AppContext, fragment_id: str) -> list[dict]:
    return [
        dict(row)
        for row in ctx.conn.execute(
            "SELECT title, keywords FROM memory_index WHERE fragment_id = ?",
            (fragment_id,),
        ).fetchall()
    ]


def _mention_edges(ctx: AppContext, topic_id: str) -> list[str]:
    rows = ctx.conn.execute(
        "SELECT n.name AS name FROM edges e JOIN nodes n ON n.id = e.dst "
        "WHERE e.src = ? AND e.type = 'mention' ORDER BY n.name",
        (topic_id,),
    ).fetchall()
    return [str(r["name"]) for r in rows]



# ---------------------------------------------------------------------------
# F05-1：摘要任务 —— 旧代最后到达不得覆盖新代写入的摘要 / 索引 / 关联
# ---------------------------------------------------------------------------


def test_summary_late_result_cannot_overwrite_newer_generation(tmp_path):
    ctx = _app(tmp_path)
    sealed = _seal(ctx)
    topic_id = sealed.topic_id
    # 前提：节点服务对实体是**惰性**的（默认提到 2 次才建节点），所以先让新代的
    # 实体成为已存在节点，才能确定性地观察「谁加了 mention 边」。
    ctx.topics.nodes.create_entity("新实体")
    clock = _Clock()
    dt.set_clock(clock)

    late_gate = asyncio.Event()
    started = asyncio.Event()

    async def late_summary():
        started.set()
        await late_gate.wait()
        return _summary_payload("旧代", "旧代摘要", ["旧实体"])

    stale_adapter = _ScriptedAdapter(
        summary=late_summary,
        knowledge=_knowledge_payload(["旧代知识"]),
        entities=_entities_payload(["旧实体"]),
    )
    fresh_adapter = _ScriptedAdapter(
        summary=_summary_payload("新代", "新代摘要", ["新实体"]),
        knowledge=_knowledge_payload(["新代知识"]),
        entities=_entities_payload(["新实体"]),
    )

    observed: dict = {}

    async def scenario():
        first = asyncio.create_task(
            ctx.memory_lifecycle.drain_derived_tasks(stale_adapter, limit=5)
        )
        await started.wait()  # 旧代（代次 1）已认领、正在等模型结果
        running = ctx.conn.execute(
            "SELECT id, claim_generation FROM derived_tasks WHERE kind = 'summary'"
        ).fetchone()
        old_id = str(running["id"])
        old_generation = int(running["claim_generation"])
        assert old_generation == 1, "前提：旧代是第一次认领"
        observed["id"] = old_id

        # 既有恢复机制：代次 1 仍卡在 running 时把它放回队列（代次 +1），
        # 于是旧执行者实际已经丢掉所有权。
        assert dt.requeue_running(
            ctx.conn,
            old_id,
            expected_state=dt.STATE_RUNNING,
            expected_generation=old_generation,
        ) is True
        clock.advance(3600)
        fresh_task = dt.claim_due(ctx.conn, kinds=(dt.KIND_SUMMARY,))[0]
        assert fresh_task.claim_generation == old_generation + 2, (
            "重排 +1、重新认领 +1"
        )

        # 新代跑完整条链（摘要 → 知识 / 实体）
        assert await ctx.memory_lifecycle.run_summary_task(fresh_task, fresh_adapter) is True
        for kind in (dt.KIND_KNOWLEDGE, dt.KIND_ENTITIES):
            for item in dt.claim_due(ctx.conn, kinds=(kind,)):
                if kind == dt.KIND_KNOWLEDGE:
                    await ctx.memory_lifecycle.run_knowledge_task(item, fresh_adapter)
                else:
                    await ctx.memory_lifecycle.run_entities_task(item, fresh_adapter)

        assert _fragment_row(ctx, sealed.id)["summary"] == "新代摘要"
        assert [r["title"] for r in _index_rows(ctx, sealed.id)] == ["新代"]

        # 旧代结果现在才到达
        late_gate.set()
        done_first = await first
        return done_first, old_generation

    try:
        done_first, old_generation = asyncio.run(scenario())
    finally:
        dt.reset_clock()
    old_id = observed["id"]

    assert done_first == 0, f"旧代任务丢了所有权就不能报正常完成：{done_first}"

    assert _fragment_row(ctx, sealed.id)["summary"] == "新代摘要", "旧摘要不得覆盖新摘要"
    index_rows = _index_rows(ctx, sealed.id)
    assert [r["title"] for r in index_rows] == ["新代"], f"旧代不得写 memory_index：{index_rows}"
    assert _mention_edges(ctx, topic_id) == ["新实体"], (
        f"旧代不得给图加 mention 关联：{_mention_edges(ctx, topic_id)}"
    )
    contents = {
        str(r["content"])
        for r in ctx.conn.execute("SELECT content FROM knowledge").fetchall()
    }
    assert contents == {"新代知识"}, f"旧代不得写知识：{contents}"

    row = ctx.conn.execute(
        "SELECT state, claim_generation FROM derived_tasks WHERE id = ?", (old_id,)
    ).fetchone()
    assert str(row["state"]) == dt.STATE_COMPLETED, "新代的任务必须真的完成"
    assert int(row["claim_generation"]) == old_generation + 2, (
        "旧代不得改动认领代次"
    )
    ctx.conn.close()


# ---------------------------------------------------------------------------
# F05-2：实体任务 —— 旧代最后到达不得写实体卡 / 关系边
# ---------------------------------------------------------------------------


def test_entities_late_result_cannot_write_cards_after_losing_ownership(tmp_path):
    ctx = _app(tmp_path)
    sealed = _seal(ctx)
    version = int(sealed.content_version)
    task_id, _ = dt.enqueue(ctx.conn, dt.KIND_ENTITIES, sealed.id, version)
    old_task = dt.claim_due(ctx.conn, kinds=(dt.KIND_ENTITIES,))[0]
    assert old_task.claim_generation == 1

    assert dt.requeue_running(
        ctx.conn,
        task_id,
        expected_state=dt.STATE_RUNNING,
        expected_generation=old_task.claim_generation,
    )

    adapter = _ScriptedAdapter(entities=_entities_payload(["旧实体卡"]))
    ok = asyncio.run(ctx.memory_lifecycle.run_entities_task(old_task, adapter))

    assert ok is False, "丢了所有权就不许报正常完成"
    cards = [
        str(r["name"]) for r in ctx.conn.execute("SELECT name FROM entity_cards").fetchall()
    ]
    assert cards == [], f"旧代不得写实体卡：{cards}"
    assert adapter.calls == ["entities"], "模型调用本身不受影响（只是结果不落地）"
    row = dt.task_for(ctx.conn, dt.KIND_ENTITIES, sealed.id, version)
    assert row.state == dt.STATE_PENDING, "重排后的 pending 状态不得被旧执行者改掉"
    assert row.claim_generation == 2
    ctx.conn.close()


# ---------------------------------------------------------------------------
# F05-3：知识任务 —— 旧代最后到达不得写知识条目
# ---------------------------------------------------------------------------


def test_knowledge_late_result_cannot_write_entries_after_losing_ownership(tmp_path):
    ctx = _app(tmp_path)
    sealed = _seal(ctx)
    version = int(sealed.content_version)
    dt.enqueue(ctx.conn, dt.KIND_SUMMARY, sealed.id, version)
    summary_task = dt.claim_due(ctx.conn, kinds=(dt.KIND_SUMMARY,))[0]
    asyncio.run(
        ctx.memory_lifecycle.run_summary_task(
            summary_task, _ScriptedAdapter(summary=_summary_payload("标题", "已落库摘要"))
        )
    )

    task_id, _ = dt.enqueue(ctx.conn, dt.KIND_KNOWLEDGE, sealed.id, version)
    knowledge_task = dt.claim_due(ctx.conn, kinds=(dt.KIND_KNOWLEDGE,))[0]
    assert knowledge_task.claim_generation == 1
    assert dt.requeue_running(
        ctx.conn,
        task_id,
        expected_state=dt.STATE_RUNNING,
        expected_generation=knowledge_task.claim_generation,
    )

    adapter = _ScriptedAdapter(knowledge=_knowledge_payload(["旧代知识"]))
    ok = asyncio.run(ctx.memory_lifecycle.run_knowledge_task(knowledge_task, adapter))

    assert ok is False
    assert int(ctx.conn.execute("SELECT COUNT(*) c FROM knowledge").fetchone()["c"]) == 0
    row = dt.task_for(ctx.conn, dt.KIND_KNOWLEDGE, sealed.id, version)
    assert row.state == dt.STATE_PENDING and row.claim_generation == 2
    ctx.conn.close()


# ---------------------------------------------------------------------------
# F05-4：核验必须在**域写入之前**（不是最后 complete 才发现代次过期）
# ---------------------------------------------------------------------------


def test_hold_rejects_superseded_generation_and_rolls_back(tmp_path):
    ctx = _app(tmp_path)
    sealed = _seal(ctx)
    version = int(sealed.content_version)
    dt.enqueue(ctx.conn, dt.KIND_SUMMARY, sealed.id, version)
    task = dt.claim_due(ctx.conn, kinds=(dt.KIND_SUMMARY,))[0]
    assert dt.requeue_running(
        ctx.conn,
        task.id,
        expected_state=dt.STATE_RUNNING,
        expected_generation=task.claim_generation,
    )

    raised: list[str] = []
    try:
        with dt.hold(ctx.conn, task.id, expected_generation=task.claim_generation):
            ctx.conn.execute(
                "UPDATE fragments SET summary = '不该留下的摘要' WHERE id = ?",
                (sealed.id,),
            )
            raised.append("未拦截")
        # 任务行未匹配时也必须拒绝
    except dt.TaskSuperseded as exc:
        raised.append(str(exc))

    assert raised and raised[0] != "未拦截", "代次过期的任务必须被 hold 拦下"
    assert task.id in raised[0], raised
    assert _fragment_row(ctx, sealed.id)["summary"] in (None, ""), (
        "核验失败必须整体回滚，不能留下半截写入"
    )
    ctx.conn.close()


def test_hold_matches_owner_and_state(tmp_path):
    """合法所有者 + running + 代次正确 ⇒ hold 放行（不是一律拒绝）。"""
    ctx = _app(tmp_path)
    sealed = _seal(ctx)
    version = int(sealed.content_version)
    dt.enqueue(ctx.conn, dt.KIND_SUMMARY, sealed.id, version)
    task = dt.claim_due(ctx.conn, kinds=(dt.KIND_SUMMARY,))[0]

    with dt.hold(ctx.conn, task.id, expected_generation=task.claim_generation):
        ctx.conn.execute(
            "UPDATE fragments SET summary = '合法写入' WHERE id = ?", (sealed.id,)
        )
        dt.complete(ctx.conn, task.id, expected_generation=task.claim_generation)

    assert _fragment_row(ctx, sealed.id)["summary"] == "合法写入"
    assert dt.task_for(ctx.conn, dt.KIND_SUMMARY, sealed.id, version).state == dt.STATE_COMPLETED
    ctx.conn.close()


# ---------------------------------------------------------------------------
# F05-5：取消路径
# ---------------------------------------------------------------------------


def test_cancel_path_still_releases_task_back_to_queue(tmp_path):
    """取消照旧：CancelledError 原样抛出，任务由 release 放回队列（不留永久 running）。"""
    ctx = _app(tmp_path)
    sealed = _seal(ctx)
    version = int(sealed.content_version)
    dt.enqueue(ctx.conn, dt.KIND_SUMMARY, sealed.id, version)

    class _Cancelling:
        mode = "native"
        model = "cancel"

        async def complete(self, messages, tools, **kwargs):
            raise asyncio.CancelledError()

    async def scenario():
        return await ctx.memory_lifecycle.drain_derived_tasks(_Cancelling(), limit=5)

    raised = False
    try:
        asyncio.run(scenario())
    except asyncio.CancelledError:
        raised = True
    assert raised, "取消必须原样抛出，不能吞掉"

    row = dt.task_for(ctx.conn, dt.KIND_SUMMARY, sealed.id, version)
    assert row.state == dt.STATE_PENDING, "取消后的任务必须放回队列，不留永久 running"
    assert _fragment_row(ctx, sealed.id)["summary"] in (None, "")
    ctx.conn.close()


# ---------------------------------------------------------------------------
# F05-6：归属（owner）也是核验的一部分（不只代次）
# ---------------------------------------------------------------------------


def test_hold_rejects_task_owned_by_another_instance(tmp_path):
    ctx = _app(tmp_path)
    sealed = _seal(ctx)
    version = int(sealed.content_version)
    dt.enqueue(ctx.conn, dt.KIND_SUMMARY, sealed.id, version)
    task = dt.claim_due(ctx.conn, kinds=(dt.KIND_SUMMARY,))[0]
    assert task.owner_instance_id == ctx.instance_id

    rejected: list[str] = []
    try:
        with dt.hold(
            ctx.conn, task.id, expected_generation=task.claim_generation, owner_key="qio_other"
        ):
            ctx.conn.execute(
                "UPDATE fragments SET summary = '别的实例写的' WHERE id = ?", (sealed.id,)
            )
            rejected.append("未拦截")
    except dt.TaskSuperseded as exc:
        rejected.append(str(exc))

    assert rejected and rejected[0] != "未拦截", "归属不是本实例时必须拒绝写入"
    assert _fragment_row(ctx, sealed.id)["summary"] in (None, "")
    ctx.conn.close()
