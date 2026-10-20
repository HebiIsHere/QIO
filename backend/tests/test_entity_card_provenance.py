"""实体卡的来源与适用范围（阶段 3 的 A 项）。

知识那一侧已经有「其他话题的知识只作参考」的标注；实体卡也必须同样处理，
否则「某个话题里形成的决定」可以经实体卡注入绕过路径隔离。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from agent import prompts
from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.entities.cards import EntityAttribute, EntityCardCandidate, EntityCardService
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


class _Adapter:
    mode = "native"
    model = "fake"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, messages, tools, **kwargs):
        self.prompts.append(messages[0].content if messages else "")
        return Completion(message=ChatMessage(role="assistant", content="收到"))


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "cards.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


def _card(ctx: AppContext, name: str, topic_id: str | None) -> str:
    """建一张实体卡；给了话题就同时写 mention 边（卡挂在那个话题上）。"""
    svc = EntityCardService(ctx.conn)
    card = svc.upsert(
        EntityCardCandidate(
            name=name,
            aliases=[],
            kind="project",
            summary="一句话摘要",
            attributes=[EntityAttribute(key="状态", value="进行中")],
        )
    )
    if topic_id and card.node_id:
        ctx.conn.execute(
            "INSERT OR IGNORE INTO edges (id, src, dst, type, weight, created_at, updated_at) "
            "VALUES (?, ?, ?, 'mention', 1.0, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (f"edge_{card.id}", topic_id, card.node_id),
        )
        ctx.conn.commit()
    return card.name


async def _run(ctx: AppContext, message: str, topic_id: str) -> str:
    adapter = _Adapter()
    ctx.build_adapter = AsyncMock(return_value=adapter)
    tctx = ctx.turns.submit(message, topic_id)
    await ctx.turns.wait(tctx.turn_id, timeout=10)
    return "\n".join(adapter.prompts)


def _entity_entries(prompt: str) -> list[dict]:
    """取本轮 system 里的结构化实体块（契约 §3.4：标签 + JSON 数据，不是预拼中文文本）。"""
    heading = "【" + prompts.CONTEXT_LABELS["entity"] + "】"
    assert heading in prompt, "实体上下文块缺失（应使用 prompts.CONTEXT_LABELS 的标签）"
    tail = prompt.split(heading, 1)[1].lstrip()
    data, _ = json.JSONDecoder().raw_decode(tail)
    return list(data.get("entries") or [])


def _entity_entry(prompt: str, name: str) -> dict:
    for entry in _entity_entries(prompt):
        if entry.get("name") == name:
            return entry
    raise AssertionError(f"结构化实体块里没有 {name!r}：{_entity_entries(prompt)!r}")


async def test_card_from_another_topic_is_marked_reference_only(ctx: AppContext):
    other = ctx.topics.nodes.create_topic("别的话题").id
    current = ctx.topics.nodes.create_topic("当前话题").id
    _card(ctx, "QIO 前端", other)

    prompt = await _run(ctx, "QIO 前端现在做到哪了？", current)

    # Phase 1 §3.4：实体卡以结构化上下文块进入 system（固定标签 + JSON 数据），
    # 来源与适用范围用程序字段表达（scope.type / topic_ids），不再拼「【实体·…】」中文正文。
    entry = _entity_entry(prompt, "QIO 前端")
    assert entry["kind"] == "entity_card"
    assert entry["summary"] == "一句话摘要"
    assert {
        "key": "状态",
        "value": "进行中",
    }.items() <= next(
        (a for a in entry["attributes"] if a.get("key") == "状态"), {}
    ).items()
    assert entry["scope"]["type"] == "other_topic", (
        "契约：其他话题的实体卡必须标成仅参考（结构化 scope.type=other_topic）"
    )
    assert other in entry["scope"]["topic_ids"], (
        "来源话题必须可追溯（scope.topic_ids 里要有点名来源话题的 id）"
    )


async def test_card_of_the_current_topic_has_no_reference_note(ctx: AppContext):
    current = ctx.topics.nodes.create_topic("当前话题").id
    _card(ctx, "QIO 前端", current)

    prompt = await _run(ctx, "QIO 前端现在做到哪了？", current)

    entry = _entity_entry(prompt, "QIO 前端")
    assert entry["scope"]["type"] == "topic", "当前话题的实体卡不应标成其他话题"
    assert entry["scope"]["topic_ids"] == [current]
    assert all(
        item.get("scope", {}).get("type") != "other_topic" for item in _entity_entries(prompt)
    ), "当前话题的实体卡不应出现其他话题的参考标记"


async def test_card_without_topic_is_marked_unknown(ctx: AppContext):
    current = ctx.topics.nodes.create_topic("当前话题").id
    _card(ctx, "无归属的卡", None)

    prompt = await _run(ctx, "无归属的卡是什么？", current)

    entry = _entity_entry(prompt, "无归属的卡")
    assert entry["scope"]["type"] == "unknown", "没有话题归属的实体卡必须标成来源未知"
    assert entry["scope"]["topic_ids"] == []
