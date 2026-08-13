# -*- coding: utf-8 -*-
"""实体卡纠错：correct_entity 工具 + /api/entities 路由。"""
import sqlite3

from fastapi.testclient import TestClient

from agent.entities.cards import EntityAttribute, EntityCardCandidate, EntityCardService
from agent.tools.entity_tools import CorrectEntityTool


def _seed(db_conn: sqlite3.Connection) -> str:
    svc = EntityCardService(db_conn)
    return svc.upsert(
        EntityCardCandidate(
            name="我家的鹅",
            aliases=["鹅"],
            summary="最近嘴巴红肿",
            attributes=[EntityAttribute(key="健康状况", value="红肿治疗中")],
        )
    ).id


async def test_tool_updates_attribute_and_relation(db_conn: sqlite3.Connection):
    _seed(db_conn)
    tool = CorrectEntityTool(db_conn)
    r = await tool.run(entity="我家的鹅", attribute_key="健康状况", attribute_value="已康复")
    assert r.ok
    card = EntityCardService(db_conn).find_by_name("我家的鹅")
    assert card is not None
    assert any(a["key"] == "健康状况" and a["value"] == "已康复" for a in card.attributes)

    r2 = await tool.run(entity="鹅", relation_target="王翠华", relation_type="饲养")
    assert r2.ok
    edge = db_conn.execute("SELECT type FROM edges WHERE type='饲养'").fetchone()
    assert edge is not None


async def test_tool_delete_attribute_and_entity(db_conn: sqlite3.Connection):
    _seed(db_conn)
    tool = CorrectEntityTool(db_conn)
    r = await tool.run(entity="鹅", attribute_key="健康状况", delete_attribute=True)
    assert r.ok
    card = EntityCardService(db_conn).find_by_name("鹅")
    assert card is not None and card.attributes == []

    r2 = await tool.run(entity="我家的鹅", delete_entity=True)
    assert r2.ok
    assert EntityCardService(db_conn).find_by_name("我家的鹅") is None


async def test_tool_missing_entity(db_conn: sqlite3.Connection):
    tool = CorrectEntityTool(db_conn)
    r = await tool.run(entity="不存在的实体")
    assert not r.ok


def test_api_list_revise_revoke(db_conn: sqlite3.Connection, settings):
    from agent.api.server import create_app

    _seed(db_conn)
    app = create_app(settings, db_conn)
    with TestClient(app) as client:
        listed = client.get("/api/entities")
        assert listed.status_code == 200
        assert any("我家的鹅" in e for e in listed.json()["entities"])

        card = EntityCardService(db_conn).find_by_name("我家的鹅")
        resp = client.post(
            f"/api/entities/{card.id}/revise",
            json={"summary": "嘴巴已康复"},
        )
        assert resp.status_code == 200
        assert "嘴巴已康复" in resp.json()["entity"]

        revoke = client.post(f"/api/entities/{card.id}/revoke")
        assert revoke.status_code == 200
        assert EntityCardService(db_conn).get(card.id).state == "archived"
