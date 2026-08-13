# -*- coding: utf-8 -*-
"""实体卡检索：Retriever 合并实体卡（名称命中 / 向量命中）。"""
import pytest

from agent.api.bus import EventBus
from agent.config import Settings
from agent.entities.cards import EntityCardCandidate, EntityCardService
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    return AppContext(Settings(data_dir=tmp_path), conn, EventBus())


@pytest.fixture()
def topic(ctx: AppContext) -> str:
    return ctx.topics.nodes.create_topic("测试话题").id


def test_retriever_returns_entity_card_on_name_hit(ctx: AppContext, topic: str):
    svc = EntityCardService(ctx.conn)
    svc.upsert(
        EntityCardCandidate(
            name="我家的鹅", aliases=["鹅", "家里的鹅"], summary="最近嘴巴红肿正在治疗"
        )
    )
    hits = ctx.retriever.search("鹅的伤口处理了吗", anchor_topic_id=topic, top_k=5)
    cards = [h for h in hits if "entity_card" in h.sources]
    assert len(cards) == 1
    assert cards[0].title == "我家的鹅"
    assert "嘴巴红肿" in cards[0].preview


def test_retriever_no_entity_card_when_no_hit(ctx: AppContext, topic: str):
    svc = EntityCardService(ctx.conn)
    svc.upsert(
        EntityCardCandidate(name="我家的鹅", aliases=["鹅"], summary="嘴巴红肿")
    )
    hits = ctx.retriever.search("帮我写一段 SQL 建表", anchor_topic_id=topic, top_k=5)
    assert not any("entity_card" in h.sources for h in hits)
