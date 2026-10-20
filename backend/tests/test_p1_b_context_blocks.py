# -*- coding: utf-8 -*-
"""Phase 1 / B：结构化上下文块（ContextBlock）与预算优先级。

验收点：
* key 全部来自 prompts.CONTEXT_LABELS；data 是结构化数据（可直接 json.dumps）；
* 知识只来自 state=active（pending_review 不进注入面）；
* short_term = 滚动摘要 + 未覆盖原文（已覆盖不重复、未覆盖不跳过）；
* 预算沿用 TokenBudgetPlanner 口径：按完整条目裁剪、不截断序列化 JSON；
* 优先级 Focus > 实体卡 > 短期记忆：短期记忆再长也挤不掉实体完整卡片。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.entities.cards import EntityCardCandidate, EntityCardService
from agent.knowledge.lifecycle import KnowledgeService
from agent.prompts import CONTEXT_LABELS, render_context_blocks
from agent.services.app import AppContext
from agent.storage import migration_p1_b
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


class _RollingAdapter:
    mode = "native"
    model = "fake-p1b-context-rolling"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def complete(self, messages, tools, **kwargs) -> Completion:
        texts = [
            message["content"] if isinstance(message, dict) else (message.content or "")
            for message in messages
        ]
        data = json.loads(texts[1]) if len(texts) > 1 else {}
        self.calls.append(data)
        ids = [str(item["message_id"]) for item in data.get("new_messages", [])]
        summary = json.dumps(
            {"title": "滚动", "summary": "滚动摘要:" + ",".join(ids), "entities": [], "keywords": []},
            ensure_ascii=False,
        )
        return Completion(message=ChatMessage(role="assistant", content=summary))


class _CapturingAdapter:
    mode = "native"
    model = "fake-p1b-context-knowledge"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, messages, tools, **kwargs) -> Completion:
        prompt = "\n".join(
            message["content"] if isinstance(message, dict) else (message.content or "")
            for message in messages
        )
        self.prompts.append(prompt)
        return Completion(
            message=ChatMessage(role="assistant", content='{"candidates": []}')
        )


def _app(tmp_path: Path) -> AppContext:
    conn = connect(tmp_path / "context_blocks.db")
    apply_migrations(conn)
    migration_p1_b.apply(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


def _seed_topic(ctx: AppContext, name: str = "上下文块") -> str:
    topic = ctx.topics.nodes.create_topic(name).id
    ctx.memory.append_message(topic_id=topic, role="user", content="你好，我养了一只鹅")
    ctx.memory.append_message(topic_id=topic, role="assistant", content="记下了")
    return topic


def _activate_knowledge(ctx: AppContext, topic: str, content: str) -> str:
    ks = KnowledgeService(ctx.conn)
    item = ks.create(category="general_fact", content=content, node_ids=[topic])
    ks.submit(item.id)
    ks.verify(item.id, verified_by="system")
    ks.activate(item.id)
    return item.id


def _pending_knowledge(ctx: AppContext, topic: str, content: str) -> str:
    ks = KnowledgeService(ctx.conn)
    item = ks.create(category="general_fact", content=content, node_ids=[topic])
    ks.submit(item.id)
    return item.id


def _blocks(ctx: AppContext, topic: str, query: str, **kwargs):
    fragment = ctx.memory_lifecycle.fragments.open_fragment(topic)
    return ctx.context_assembler.build_context_blocks(
        query,
        topic_id=topic,
        fragment_id=fragment.id if fragment is not None else None,
        user_node_id=ctx._user_root_id(),
        model="unknown",
        **kwargs,
    )


def _by_key(blocks, key: str):
    return next((block for block in blocks if block.key == key), None)


# ---------------------------------------------------------------------------
# 结构契约：key / 结构化 data / 只注入 active 知识
# ---------------------------------------------------------------------------


def test_blocks_use_frozen_labels_and_only_active_knowledge(tmp_path: Path):
    ctx = _app(tmp_path)
    topic = _seed_topic(ctx)
    active_id = _activate_knowledge(ctx, topic, "用户喜欢清淡饮食")
    pending_id = _pending_knowledge(ctx, topic, "待审知识不应注入")
    EntityCardService(ctx.conn).upsert(
        EntityCardCandidate(name="鹅", summary="用户的宠物鹅"), source="auto"
    )

    blocks = _blocks(ctx, topic, "我的鹅最近怎么样")
    assert blocks
    keys = [block.key for block in blocks]
    assert set(keys) <= set(CONTEXT_LABELS), keys
    assert "current_topic" in keys
    for block in blocks:
        # data 必须是可整体序列化的结构化数据（不是预先拼好的中文大段标题）
        encoded = json.dumps(block.data, ensure_ascii=False)
        assert encoded
    rendered = render_context_blocks(
        [{"key": block.key, "data": block.data} for block in blocks]
    )
    assert "当前话题" in rendered

    knowledge_block = _by_key(blocks, "knowledge")
    assert knowledge_block is not None
    entries = knowledge_block.data["entries"]
    ids = [entry["id"] for entry in entries]
    assert active_id in ids
    assert pending_id not in ids, "pending_review 不得进入注入面"
    assert all(entry["state"] == "active" for entry in entries)
    assert all(entry["scope"]["type"] for entry in entries)

    entity_block = _by_key(blocks, "entity")
    assert entity_block is not None
    cards = entity_block.data["entries"]
    assert cards and cards[0]["name"] == "鹅"
    assert "scope" in cards[0] and "source" in cards[0]


# ---------------------------------------------------------------------------
# 短期记忆：滚动摘要 + 未覆盖原文；排除当前用户消息
# ---------------------------------------------------------------------------


def test_short_term_is_rolling_summary_plus_uncovered_messages(tmp_path: Path):
    ctx = _app(tmp_path)
    topic = _seed_topic(ctx)
    fragment = ctx.memory_lifecycle.fragments.open_fragment(topic)
    adapter = _RollingAdapter()
    assert asyncio.run(ctx.memory_lifecycle.consolidate(topic, adapter)) is True
    covered_ids = [
        str(row["id"])
        for row in ctx.conn.execute(
            "SELECT id FROM messages WHERE fragment_id = ? ORDER BY rowid",
            (fragment.id,),
        ).fetchall()
    ]

    # 新增两条未覆盖消息 + 一条“当前用户消息”（应被 exclude 掉）
    ctx.memory.append_message(topic_id=topic, role="user", content="新消息：它今天下蛋了")
    ctx.memory.append_message(topic_id=topic, role="assistant", content="好消息")
    current_id, _ = ctx.memory.append_message(
        topic_id=topic, role="user", content="当前这句是用户本轮消息"
    )

    blocks = _blocks(
        ctx, topic, "它今天怎么样", exclude_message_id=current_id
    )
    short = _by_key(blocks, "short_term")
    assert short is not None
    rolling = short.data["rolling_summary"]
    assert rolling is not None and rolling["summary"].startswith("滚动摘要:")
    uncovered = short.data["uncovered_messages"]
    uncovered_ids = [entry["id"] for entry in uncovered]
    assert current_id not in uncovered_ids, "当前用户消息不能重复注入"
    assert set(uncovered_ids) == {
        str(row["id"])
        for row in ctx.conn.execute(
            "SELECT id FROM messages WHERE fragment_id = ? ORDER BY rowid",
            (fragment.id,),
        ).fetchall()
    } - set(covered_ids) - {current_id}
    assert not (set(uncovered_ids) & set(covered_ids)), "已覆盖原文不得重复注入"
    assert fragment.id == short.data["fragment_id"]


# ---------------------------------------------------------------------------
# 预算：完整条目裁剪；实体卡优先于短期记忆，不被挤掉
# ---------------------------------------------------------------------------


def test_budget_prunes_complete_entries_and_keeps_entity_cards(tmp_path: Path):
    ctx = _app(tmp_path)
    topic = ctx.topics.nodes.create_topic("预算").id
    fragment = ctx.memory_lifecycle.fragments.get_or_create_open(topic)
    EntityCardService(ctx.conn).upsert(
        EntityCardCandidate(
            name="长颈鹿",
            summary="用户提到的长颈鹿",
            attributes=[{"key": "颜色", "value": "斑点"}],
        ),
        source="auto",
    )
    # 直接灌入大量长消息（200 字 × 40 条），总量超过小窗口注入预算。
    originals: list[tuple[str, str]] = []
    for index in range(80):
        message_id = f"msg_budget_{index}"
        content = f"第{index}条：" + "填充内容" * 40
        originals.append((message_id, content))
        ctx.conn.execute(
            "INSERT INTO messages (id, fragment_id, role, content, created_at) "
            "VALUES (?, ?, 'user', ?, ?)",
            (message_id, fragment.id, content, "2026-10-21T00:00:%02d+00:00" % index),
        )
    ctx.conn.execute(
        "UPDATE fragments SET start_message_id = ? WHERE id = ?",
        (originals[0][0], fragment.id),
    )
    # 用小窗口预算（未知模型 32k）逼近真实上限，而不是用默认大窗口。
    monkeypatch_target = ctx.context_assembler.context_registry
    original_get = monkeypatch_target.get
    try:
        monkeypatch_target.get = lambda *args, **kwargs: 12_000  # type: ignore[assignment]
        blocks = _blocks(ctx, topic, "长颈鹿最近如何")
    finally:
        monkeypatch_target.get = original_get  # type: ignore[assignment]

    keys = [block.key for block in blocks]
    entity_block = _by_key(blocks, "entity")
    assert entity_block is not None, f"实体卡被短期记忆挤掉了：{keys}"
    assert entity_block.data["entries"][0]["name"] == "长颈鹿"
    attributes = entity_block.data["entries"][0]["attributes"]
    assert len(attributes) == 1, "实体完整卡片不得被裁剪成半个对象"
    assert attributes[0]["key"] == "颜色" and attributes[0]["value"] == "斑点"

    short = _by_key(blocks, "short_term")
    assert short is not None
    kept = short.data["uncovered_messages"]
    assert kept, "短期记忆至少保留最近条目"
    assert len(kept) < 80, "预算不足时必须按完整条目裁剪，而不是全部塞进去"
    original_map = dict(originals)
    for entry in kept:
        assert entry["content"] == original_map[entry["id"]], "保留的条目必须是完整原文"
    limits_block = _by_key(blocks, "limits")
    if limits_block is not None:
        limits = limits_block.data
        assert limits["truncated"] is True
        assert limits["dropped_entries"].get("short_term", 0) > 0
        assert limits["used_tokens"] <= limits["injection_hard_cap"], "硬上限不得被突破"
