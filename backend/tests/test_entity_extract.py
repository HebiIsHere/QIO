# -*- coding: utf-8 -*-
"""实体卡封块提炼：主模型输出解析 + 失败降级。"""
import asyncio

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


def test_invalid_item_skipped():
    reply = '{"entities": [{"name": "OK", "attributes": [{"key": 1, "value": 2}]}]}'
    cards = asyncio.run(extract_entity_cards(FakeAdapter(reply), _msgs()))
    assert cards == []
