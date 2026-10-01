# -*- coding: utf-8 -*-
"""实体卡封块提炼：主模型输出解析 + 失败降级。"""
import asyncio
import json

from agent.adapters.base import ChatMessage, Completion
from agent.entities.extract import extract_entity_cards


class FakeAdapter:
    mode = "native"

    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def complete(self, messages, tools, **kwargs):
        assert any(m.role == "user" and "实体提炼器" in (m.content or "") for m in messages)
        return Completion(message=ChatMessage(role="assistant", content=self.reply))


def _msgs():
    return [
        {"role": "user", "content": "我家的鹅嘴巴红肿了"},
        {"role": "assistant", "content": "先用碘伏消毒，观察几天。"},
    ]


def test_parse_valid_entity_json():
    reply = '```json\n{"entities": [{"name": "我家的鹅", "aliases": ["鹅"], "kind": "animal", "summary": "最近嘴巴红肿", "attributes": [{"key": "健康状况", "value": "红肿治疗中"}], "relations": [{"target": "我", "type": "属于"}]}]}\n```'
    cards = asyncio.run(extract_entity_cards(FakeAdapter(reply), _msgs()))
    assert len(cards) == 1
    assert cards[0].name == "我家的鹅"
    assert cards[0].attributes[0].key == "健康状况"
    assert cards[0].relations[0].type == "属于"


def test_bad_json_degrades_to_empty():
    cards = asyncio.run(extract_entity_cards(FakeAdapter("not json at all"), _msgs()))
    assert cards == []


def test_empty_entities_degrades_to_empty():
    cards = asyncio.run(extract_entity_cards(FakeAdapter('{"entities": []}'), _msgs()))
    assert cards == []


def test_invalid_attribute_does_not_drop_the_whole_card():
    """坏属性只丢属性：实体卡本身仍然要留下（统一可修正层）。

    旧行为是「一个属性不合格 → 整张卡被丢掉」，那是把可修正输出当成 schema failure。
    """
    reply = '{"entities": [{"name": "OK", "attributes": [{"key": 1, "value": 2}]}]}'
    cards = asyncio.run(extract_entity_cards(FakeAdapter(reply), _msgs()))
    assert [c.name for c in cards] == ["OK"]
    assert cards[0].attributes == []


def test_over_limit_entity_cards_are_capped_not_failed():
    from agent.entities.extract import MAX_ENTITY_CARDS

    reply = json.dumps(
        {"entities": [{"name": f"实体{i}"} for i in range(MAX_ENTITY_CARDS * 3)]},
        ensure_ascii=False,
    )
    cards = asyncio.run(extract_entity_cards(FakeAdapter(reply), _msgs()))
    assert len(cards) == MAX_ENTITY_CARDS
    assert cards[0].name == "实体0"


def test_unrecoverable_entity_output_reports_a_reason():
    """无法安全恢复时要给可读原因，而不是静默返回空列表。"""
    from agent.entities.extract import extract_entity_cards_outcome

    outcome = asyncio.run(
        extract_entity_cards_outcome(FakeAdapter('{"entities": [{"name": 123}]}'), _msgs())
    )
    assert outcome.value is None
    assert outcome.error and "无法安全恢复" in outcome.error, outcome.error

    broken = asyncio.run(
        extract_entity_cards_outcome(FakeAdapter("not json at all"), _msgs())
    )
    assert broken.value is None and broken.error and "JSON" in broken.error


def test_repairs_are_reported_as_notes():
    from agent.entities.extract import MAX_ALIASES, extract_entity_cards_outcome

    reply = json.dumps(
        {
            "entities": [
                {
                    "name": " 我家的鹅 ",
                    "aliases": ["鹅", "鹅", "", "  大鹅 "],
                    "attributes": [{"key": "状态", "value": "红肿"}, {"key": "", "value": "x"}],
                }
            ]
        },
        ensure_ascii=False,
    )
    outcome = asyncio.run(extract_entity_cards_outcome(FakeAdapter(reply), _msgs()))
    assert outcome.error is None, outcome.error
    card = outcome.value[0]
    assert card.name == "我家的鹅"
    assert card.aliases == ["鹅", "大鹅"]
    assert [a.key for a in card.attributes] == ["状态"]
    codes = {n.code for n in outcome.notes}
    assert "deduped" in codes and "dropped_blank" in codes, outcome.notes
    assert len(card.aliases) <= MAX_ALIASES
