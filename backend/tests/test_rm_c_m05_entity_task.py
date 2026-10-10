# -*- coding: utf-8 -*-
"""M05：实体提炼作为**独立派生任务**（登记 / 认领 / 完成 / 失败 / 重试）。

验收覆盖（逐条）：
* 摘要成功、实体首次失败 → 之后只重试实体并成功，摘要不重复调用；
* 重复 drain 不重复写卡片；
* 摘要失败时实体仍然能产出（失败隔离，不倒退）；
* 对既有 completed 摘要但实体缺失的片段提供**有边界**补派，且不覆盖人工修订、
  不制造重复卡片；
* 任务状态 / trace 与实际结果一致。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.entities.cards import EntityAttribute, EntityCardCandidate, EntityCardService
from agent.services import derived_tasks as dt
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.trace.recorder import TurnTracer
from agent.trace.store import TraceStore

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


class _Clock:
    def __init__(self) -> None:
        self.now = CLOCK_START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now = self.now + timedelta(seconds=seconds)


@pytest.fixture()
def clock():
    extra = _Clock()
    dt.set_clock(extra)
    try:
        yield extra
    finally:
        dt.reset_clock()


def _summary_payload() -> str:
    return json.dumps(
        {"title": "标题", "summary": "这是一段摘要", "entities": [], "keywords": []},
        ensure_ascii=False,
    )


def _knowledge_payload() -> str:
    return json.dumps(
        {
            "candidates": [
                {
                    "content": "用户家的鹅在治疗",
                    "category": "general_fact",
                    "attach": "topic",
                    "entity": None,
                }
            ]
        },
        ensure_ascii=False,
    )


def _entity_payload(*, name: str = "我家的鹅", health: str = "红肿") -> dict:
    return {
        "entities": [
            {
                "name": name,
                "aliases": [],
                "kind": "动物",
                "summary": "用户养的鹅",
                "attributes": [{"key": "健康状况", "value": health}],
                "relations": [],
            }
        ]
    }


class _ScriptedAdapter:
    """按提示词分派的假 provider；可脚本化「实体第 N 次失败」。"""

    mode = "native"
    model = "fake-m05"

    def __init__(self, *, summary=None, entities=None, knowledge=None, entity_fail_times=0):
        self.summary = _summary_payload() if summary is None else summary
        self.entities = _entity_payload() if entities is None else entities
        self.knowledge = _knowledge_payload() if knowledge is None else knowledge
        self.entity_fail_times = entity_fail_times
        self.calls = {"summary": 0, "entities": 0, "knowledge": 0}

    async def complete(self, messages, tools, **kwargs) -> Completion:
        first = messages[0]
        prompt = first["content"] if isinstance(first, dict) else first.content
        if "summarizing a closed conversation fragment" in prompt:
            key, payload = "summary", self.summary
        elif "extracting durable knowledge" in prompt:
            key, payload = "knowledge", self.knowledge
        elif "你是实体提炼器" in prompt:
            key, payload = "entities", self.entities
        else:
            raise AssertionError(f"未知提示词：{prompt[:40]!r}")
        self.calls[key] += 1
        if key == "entities" and self.entity_fail_times > 0:
            self.entity_fail_times -= 1
            payload = "完全不是 JSON"
        content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return Completion(message=ChatMessage(role="assistant", content=content))


def _app(tmp_path: Path) -> AppContext:
    conn = connect(tmp_path / "rm_c_m05.db")
    apply_migrations(conn)
    _ensure_columns(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


def _seal_topic(ctx: AppContext, index: int):
    topic = ctx.topics.nodes.create_topic(f"可靠性{index}").id
    ctx.memory.append_message(topic_id=topic, role="user", content="我家的鹅最近有点生病")
    ctx.memory.append_message(topic_id=topic, role="assistant", content="记下了")
    sealed = ctx.memory_lifecycle.seal_fragment(topic, reason="capacity")
    assert sealed is not None
    return sealed


def _seal(ctx: AppContext):
    return _seal_topic(ctx, 0)


def _tracer(ctx: AppContext, turn_id: str) -> TurnTracer:
    store = TraceStore(ctx.conn, enabled=True)
    store.begin(turn_id)
    return TurnTracer(store, turn_id)


def _warnings(ctx: AppContext, turn_id: str) -> list[dict]:
    trace = TraceStore(ctx.conn).get(turn_id)
    assert trace is not None
    return trace["warnings"]


def _count(ctx: AppContext, table: str) -> int:
    return int(ctx.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"])


def _count_kind(ctx: AppContext, kind: str) -> int:
    return int(
        ctx.conn.execute(
            "SELECT COUNT(*) c FROM derived_tasks WHERE kind = ?", (kind,)
        ).fetchone()["c"]
    )


def _states(ctx: AppContext) -> dict[str, str]:
    return {
        row["kind"]: row["state"]
        for row in ctx.conn.execute("SELECT kind, state FROM derived_tasks").fetchall()
    }


def _task_row_for(ctx: AppContext, kind: str) -> dict | None:
    row = ctx.conn.execute(
        "SELECT state, attempts, last_error FROM derived_tasks WHERE kind = ?", (kind,)
    ).fetchone()
    return dict(row) if row is not None else None


def _attrs(ctx: AppContext, name: str = "我家的鹅") -> dict[str, str]:
    card = EntityCardService(ctx.conn).find_by_name(name)
    assert card is not None
    return {str(a.get("key")): str(a.get("value")) for a in card.attributes}


# ---------------------------------------------------------------------------
# 摘要成功 / 实体首次失败 → 只重试实体
# ---------------------------------------------------------------------------


def test_entity_failure_retries_only_entities(tmp_path: Path, clock):
    ctx = _app(tmp_path)
    _seal(ctx)
    adapter = _ScriptedAdapter(entity_fail_times=1)
    tracer = _tracer(ctx, "turn_m05_first")

    first = asyncio.run(ctx.memory_lifecycle.drain_derived_tasks(adapter, limit=5, tracer=tracer))

    assert first == 2, "摘要 + 知识完成；实体这条独立失败，不算完成"
    assert _states(ctx) == {
        "summary": "completed",
        "knowledge": "completed",
        "entities": "failed",
    }
    entity_task = _task_row_for(ctx, "entities")
    assert entity_task is not None
    assert entity_task["attempts"] == 1, "实体失败有自己的尝试次数"
    assert entity_task["last_error"], "实体失败原因必须可读地落库"
    assert adapter.calls == {"summary": 1, "entities": 1, "knowledge": 1}
    assert _count(ctx, "entity_cards") == 0
    assert any(
        w["code"] == "entity_card_extraction_failed" for w in _warnings(ctx, "turn_m05_first")
    ), "状态与 trace 要与实际结果一致"

    clock.advance(dt.backoff_delay(1))
    second = asyncio.run(ctx.memory_lifecycle.drain_derived_tasks(adapter, limit=5))

    assert second == 1, "这一轮只把实体这条做完"
    assert adapter.calls["summary"] == 1, "摘要不得重复调用"
    assert adapter.calls["knowledge"] == 1, "已成功的知识也不重复调用"
    assert adapter.calls["entities"] == 2
    assert _count(ctx, "entity_cards") == 1
    assert _states(ctx) == {
        "summary": "completed",
        "knowledge": "completed",
        "entities": "completed",
    }


def test_repeated_drain_does_not_duplicate_cards(tmp_path: Path):
    ctx = _app(tmp_path)
    _seal(ctx)
    adapter = _ScriptedAdapter()

    first = asyncio.run(ctx.memory_lifecycle.drain_derived_tasks(adapter, limit=5))
    second = asyncio.run(ctx.memory_lifecycle.drain_derived_tasks(adapter, limit=5))

    assert first == 3 and second == 0, "没有到期任务时不该有第二次模型调用"
    assert adapter.calls == {"summary": 1, "entities": 1, "knowledge": 1}
    assert _count(ctx, "entity_cards") == 1

    # 幂等身份：同一条实体任务被放回队列再跑，也不会重复写卡片 / 重复调模型
    ctx.conn.execute(
        "UPDATE derived_tasks SET state = 'pending', run_after = NULL WHERE kind = 'entities'"
    )
    third = asyncio.run(ctx.memory_lifecycle.drain_derived_tasks(adapter, limit=5))

    assert third == 1
    assert adapter.calls["entities"] == 1, "同一内容版本已提炼过：不重复调用模型"
    assert _count(ctx, "entity_cards") == 1, "不得重复制造卡片"


def test_summary_failure_still_produces_entity_cards(tmp_path: Path):
    """摘要失败不让实体跟着失败（基线行为，不得倒退）；知识依赖摘要所以没有。"""
    ctx = _app(tmp_path)
    sealed = _seal(ctx)
    adapter = _ScriptedAdapter(summary="{不是 JSON")
    tracer = _tracer(ctx, "turn_m05_summary_fail")

    done = asyncio.run(ctx.memory_lifecycle.drain_derived_tasks(adapter, limit=5, tracer=tracer))

    assert done == 1, "实体这条独立完成"
    assert _states(ctx) == {"summary": "failed", "entities": "completed"}
    assert _task_row_for(ctx, "knowledge") is None, "知识依赖摘要，摘要失败时不登记"
    assert _count(ctx, "entity_cards") == 1, "实体只依赖原文，仍然产出"
    summary_row = ctx.conn.execute(
        "SELECT summary FROM fragments WHERE id = ?", (sealed.id,)
    ).fetchone()
    assert not (summary_row["summary"] or ""), "不得伪造摘要"
    warnings = {w["code"] for w in _warnings(ctx, "turn_m05_summary_fail")}
    assert "summary_derivation_failed" in warnings


# ---------------------------------------------------------------------------
# 有边界补派（历史片段：摘要已完成、实体任务缺失）
# ---------------------------------------------------------------------------


def _make_legacy_fragments(ctx: AppContext, count: int) -> list[str]:
    """造出「摘要任务已完成、实体任务缺失」的历史片段（模拟升级前的库）。"""
    fragment_ids = [_seal_topic(ctx, index).id for index in range(count)]
    ctx.conn.execute("UPDATE derived_tasks SET state = 'completed' WHERE kind = 'summary'")
    ctx.conn.execute("UPDATE fragments SET summary = '历史摘要'")
    assert _count_kind(ctx, "entities") == 0
    return fragment_ids


def test_backfill_is_bounded_and_idempotent(tmp_path: Path):
    ctx = _app(tmp_path)
    _make_legacy_fragments(ctx, 3)

    assert ctx.memory_lifecycle.backfill_entity_tasks(limit=2) == 2, "每次补派有边界"
    assert _count_kind(ctx, "entities") == 2
    assert ctx.memory_lifecycle.backfill_entity_tasks(limit=2) == 1
    assert ctx.memory_lifecycle.backfill_entity_tasks(limit=2) == 0, "补过就不再补"
    assert _count_kind(ctx, "entities") == 3

    adapter = _ScriptedAdapter()
    asyncio.run(ctx.memory_lifecycle.drain_derived_tasks(adapter, limit=5))
    # 三个片段提到的是同一个实体：合并成一张卡（不是三张）
    assert _count(ctx, "entity_cards") == 1
    assert _states(ctx)["entities"] == "completed"
    asyncio.run(ctx.memory_lifecycle.drain_derived_tasks(adapter, limit=5))
    assert _count(ctx, "entity_cards") == 1, "重复 drain 不制造重复卡片"


def test_backfill_does_not_override_user_revision(tmp_path: Path):
    """补派走自动合并：人工纠正优先，也不制造重复卡片。"""
    ctx = _app(tmp_path)
    _make_legacy_fragments(ctx, 1)
    svc = EntityCardService(ctx.conn)
    svc.upsert(EntityCardCandidate(name="我家的鹅", attributes=[]))
    card = svc.find_by_name("我家的鹅")
    assert card is not None
    svc.set_attribute(card.id, "健康状况", "已康复")  # 用户明确纠正

    adapter = _ScriptedAdapter(entities=_entity_payload(health="红肿"))
    asyncio.run(ctx.memory_lifecycle.drain_derived_tasks(adapter, limit=5))

    assert _attrs(ctx)["健康状况"] == "已康复", "补派不得覆盖人工修订"
    assert _count(ctx, "entity_cards") == 1, "不得制造重复卡片"
    pending = svc.pending_candidates(card.id)
    assert any(p["field"] == "attributes.健康状况" for p in pending), pending


class _GatedEntitiesAdapter(_ScriptedAdapter):
    """实体提炼被闸门挂住：用来制造「暂停提炼期间人工纠正」的时序。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.gate = asyncio.Event()
        self.entities_entered = asyncio.Event()

    async def complete(self, messages, tools, **kwargs) -> Completion:
        first = messages[0]
        prompt = first["content"] if isinstance(first, dict) else first.content
        if "你是实体提炼器" in prompt:
            self.entities_entered.set()
            await self.gate.wait()
        return await super().complete(messages, tools, **kwargs)


async def test_correction_while_extraction_paused_is_kept(tmp_path: Path):
    """暂停提炼期间人工纠正 → 放行旧结果后人工值仍保留（迟到的自动结果只落候选）。"""
    ctx = _app(tmp_path)
    _seal(ctx)
    svc = EntityCardService(ctx.conn)
    svc.upsert(
        EntityCardCandidate(
            name="我家的鹅",
            attributes=[
                EntityAttribute(key="健康状况", value="红肿"),
                EntityAttribute(key="年龄", value="2 岁"),
            ],
        )
    )
    card = svc.find_by_name("我家的鹅")
    assert card is not None
    stale_revision = card.revision

    adapter = _GatedEntitiesAdapter(entities=_entity_payload(health="红肿"))
    drain = asyncio.create_task(ctx.memory_lifecycle.drain_derived_tasks(adapter, limit=5))
    await asyncio.wait_for(adapter.entities_entered.wait(), timeout=5)

    # 提炼被闸门暂停期间：用户（纠正工具入口）改了字段
    from agent.tools.entity_tools import CorrectEntityTool

    tool = CorrectEntityTool(ctx.conn)
    result = await tool.run(entity="我家的鹅", attribute_key="健康状况", attribute_value="已康复")
    assert result.ok
    assert svc.get(card.id).revision > stale_revision

    adapter.gate.set()
    done = await asyncio.wait_for(drain, timeout=5)

    assert done == 3
    values = _attrs(ctx)
    assert values["健康状况"] == "已康复", "人工纠正优先于迟到的自动结果"
    assert values["年龄"] == "2 岁", "未提及的属性保持"
    pending = svc.pending_candidates(card.id)
    assert any(p["field"] == "attributes.健康状况" for p in pending), pending
