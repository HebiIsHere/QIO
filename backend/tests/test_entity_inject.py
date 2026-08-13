# -*- coding: utf-8 -*-
"""实体卡注入：消息命中实体 → 高优注入卡文本。"""
import pytest

from agent.api.bus import EventBus
from agent.config import Settings
from agent.entities.cards import EntityAttribute, EntityCardCandidate, EntityCardService
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


def _seed_goose_card(ctx: AppContext) -> None:
    svc = EntityCardService(ctx.conn)
    svc.upsert(
        EntityCardCandidate(
            name="我家的鹅",
            aliases=["鹅", "家里的鹅"],
            kind="animal",
            summary="最近嘴巴红肿正在治疗",
            attributes=[EntityAttribute(key="健康状况", value="红肿治疗中")],
        )
    )


def test_match_cards_hits_name_and_alias(ctx: AppContext):
    _seed_goose_card(ctx)
    svc = EntityCardService(ctx.conn)
    assert [c.name for c in svc.match_cards("我家鹅今天怎么样了")] == ["我家的鹅"]
    assert [c.name for c in svc.match_cards("鹅的伤口处理了吗")] == ["我家的鹅"]
    assert svc.match_cards("帮我写一段 SQL") == []


def test_entity_card_injected_when_message_hits(ctx: AppContext, topic: str):
    _seed_goose_card(ctx)
    svc = EntityCardService(ctx.conn)
    hits = svc.match_cards("我家鹅今天怎么样了")
    payload = ctx.build_injection(
        "我家鹅今天怎么样了", topic_id=topic, model="gpt-4o",
        entity_cards=[svc.format_card(c) for c in hits],
    )
    assert "【实体·我家的鹅】" in payload.text
    assert "健康状况" in payload.text


def test_entity_card_not_injected_when_no_hit(ctx: AppContext, topic: str):
    _seed_goose_card(ctx)
    payload = ctx.build_injection(
        "帮我写一段 SQL", topic_id=topic, model="gpt-4o", entity_cards=None
    )
    assert "【实体·我家的鹅】" not in payload.text
