# -*- coding: utf-8 -*-
"""PM-QIO032 #49：实体属性置信度的显式 0 在整个往返里都是 0。

用户能明确输入置信度 0（"这条我不确定"），它不能被任何默认值逻辑吞成 0.8。
本组覆盖两层：服务层整段替换属性后 0 原样落库并在读回时仍是 0；
没给 confidence 的旧数据仍按 0.8 处理（缺省 ≠ 显式 0）。
"""

from __future__ import annotations

import sqlite3

import httpx
import pytest

from agent.entities.cards import EntityAttribute, EntityCardCandidate, EntityCardService


def _seed(conn: sqlite3.Connection) -> str:
    svc = EntityCardService(conn)
    card = svc.upsert(
        EntityCardCandidate(
            name="薄荷",
            kind="animal",
            summary="一只猫",
            attributes=[EntityAttribute(key="接受抚摸", value="偶尔", confidence=0.9)],
        )
    )
    assert card is not None
    return card.id


def test_revise_keeps_explicit_zero_confidence(db_conn: sqlite3.Connection):
    entity_id = _seed(db_conn)
    svc = EntityCardService(db_conn)
    start = svc.get(entity_id)
    assert start is not None

    updated = svc.revise(
        entity_id,
        attributes=[
            {"key": "接受抚摸", "value": "0 次", "confidence": 0},
            {"key": "体重(kg)", "value": "0", "confidence": 0.0},
        ],
        expected_revision=start.revision,
    )

    assert updated is not None
    assert updated.attributes == [
        {"key": "接受抚摸", "value": "0 次", "confidence": 0.0},
        {"key": "体重(kg)", "value": "0", "confidence": 0.0},
    ], "显式 0 置信度与属性值 '0' 都必须原样保留"
    # 刷新回显走 to_dict：同一份真实值，不是 0.8。
    echoed = svc.to_dict(updated)["attributes"]
    assert [a["confidence"] for a in echoed] == [0.0, 0.0]


def test_revise_missing_confidence_falls_back_to_default(db_conn: sqlite3.Connection):
    entity_id = _seed(db_conn)
    svc = EntityCardService(db_conn)
    start = svc.get(entity_id)
    assert start is not None

    updated = svc.revise(
        entity_id,
        attributes=[{"key": "接受抚摸", "value": "经常"}],
        expected_revision=start.revision,
    )

    assert updated is not None
    assert updated.attributes[0]["confidence"] == 0.8, "没填置信度才用默认 0.8"

# ---------------------------------------------------------------------------
# HTTP 往返：请求 JSON 里的 0 → 落库 → 响应/刷新回显仍是 0
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(tmp_path, db_conn, settings):
    from agent.api.server import create_app

    return create_app(settings, db_conn)


async def test_route_revise_echoes_zero_confidence(app, db_conn: sqlite3.Connection):
    entity_id = _seed(db_conn)
    start = EntityCardService(db_conn).get(entity_id)
    assert start is not None

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://qio.test"
    ) as client:
        resp = await client.post(
            f"/api/entities/{entity_id}/revise",
            json={
                "attributes": [
                    {"key": "接受抚摸", "value": "0", "confidence": 0},
                ],
                "expected_revision": start.revision,
            },
        )
        assert resp.status_code == 200, resp.text
        entity = resp.json()["entity"]
        assert entity["attributes"] == [
            {"key": "接受抚摸", "value": "0", "confidence": 0.0}
        ], "显式 0 置信度与属性值 '0' 必须穿过 API 原样返回"

        # 刷新回显：GET 也必须是 0（不是默认 0.8）
        echoed = await client.get(f"/api/entities/{entity_id}")
        assert echoed.status_code == 200, echoed.text
        assert echoed.json()["entity"]["attributes"][0]["confidence"] == 0.0
