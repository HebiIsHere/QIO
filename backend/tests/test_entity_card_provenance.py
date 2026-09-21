"""实体卡的来源与适用范围（阶段 3 的 A 项）。

知识那一侧已经有「其他话题的知识只作参考」的标注；实体卡也必须同样处理，
否则「某个话题里形成的决定」可以经实体卡注入绕过路径隔离。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

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


async def test_card_from_another_topic_is_marked_reference_only(ctx: AppContext):
    other = ctx.topics.nodes.create_topic("别的话题").id
    current = ctx.topics.nodes.create_topic("当前话题").id
    _card(ctx, "QIO 前端", other)

    prompt = await _run(ctx, "QIO 前端现在做到哪了？", current)

    assert "【实体·QIO 前端】" in prompt
    assert "来自其他话题" in prompt
    assert "只作参考" in prompt
    assert "别的话题" in prompt, "要点名来源话题，用户才看得出这是别处的结论"


async def test_card_of_the_current_topic_has_no_reference_note(ctx: AppContext):
    current = ctx.topics.nodes.create_topic("当前话题").id
    _card(ctx, "QIO 前端", current)

    prompt = await _run(ctx, "QIO 前端现在做到哪了？", current)

    assert "【实体·QIO 前端】" in prompt
    assert "只作参考" not in prompt


async def test_card_without_topic_is_marked_unknown(ctx: AppContext):
    current = ctx.topics.nodes.create_topic("当前话题").id
    _card(ctx, "无归属的卡", None)

    prompt = await _run(ctx, "无归属的卡是什么？", current)

    assert "【实体·无归属的卡】" in prompt
    assert "来源未知" in prompt
