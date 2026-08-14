# -*- coding: utf-8 -*-
"""EntityCardService：实体卡读写、关系边、格式化。"""
import sqlite3

from agent.entities.cards import (
    EntityAttribute,
    EntityCardCandidate,
    EntityRelation,
    EntityCardService,
)


def _candidate(name="我家的鹅", aliases=("鹅", "家里的鹅")):
    return EntityCardCandidate(
        name=name,
        aliases=list(aliases),
        kind="animal",
        summary="我家里养的鹅，最近嘴巴红肿正在治疗",
        attributes=[EntityAttribute(key="健康状况", value="嘴巴红肿（治疗中）")],
        relations=[EntityRelation(target="我", type="属于")],
    )


def test_upsert_creates_card_node_and_relation(db_conn: sqlite3.Connection):
    svc = EntityCardService(db_conn)
    card = svc.upsert(_candidate())
    assert card.id
    assert card.name == "我家的鹅"
    # 关联的实体节点已创建
    node = db_conn.execute(
        "SELECT * FROM nodes WHERE id = ?", (card.node_id,)
    ).fetchone()
    assert node is not None and node["type"] == "entity" and node["name"] == "我家的鹅"
    # 关系边已写入（任意关系类型）
    edge = db_conn.execute(
        "SELECT type FROM edges WHERE src = ?", (card.node_id,)
    ).fetchone()
    assert edge is not None and edge["type"] == "属于"


def test_find_by_name_and_alias(db_conn: sqlite3.Connection):
    svc = EntityCardService(db_conn)
    svc.upsert(_candidate())
    assert svc.find_by_name("我家的鹅") is not None
    assert svc.find_by_name("鹅") is not None
    assert svc.find_by_name("不存在的鹅") is None


def test_revoke_archives_card(db_conn: sqlite3.Connection):
    svc = EntityCardService(db_conn)
    card = svc.upsert(_candidate())
    assert svc.revoke(card.id) is True
    assert all(c.id != card.id for c in svc.list_active())


def test_format_card_includes_template(db_conn: sqlite3.Connection):
    svc = EntityCardService(db_conn)
    card = svc.upsert(_candidate())
    text = svc.format_card(card)
    assert "【实体·我家的鹅】" in text
    assert "健康状况" in text
    assert "属于" in text

def test_to_dict_structured_with_relations(db_conn):
    from agent.entities.cards import (
        EntityAttribute,
        EntityCardCandidate,
        EntityCardService,
        EntityRelation,
    )

    svc = EntityCardService(db_conn)
    card = svc.upsert(EntityCardCandidate(
        name="王翠华",
        aliases=["我妈"],
        kind="家人",
        summary="我妈妈，退休教师",
        attributes=[EntityAttribute(key="职业", value="退休教师", confidence=0.9)],
        relations=[EntityRelation(target="王翠华的弟弟", type="属于")],
    ))
    d = svc.to_dict(card)
    assert d["name"] == "王翠华"
    assert d["aliases"] == ["我妈"]
    assert d["attributes"] == [{"key": "职业", "value": "退休教师", "confidence": 0.9}]
    assert d["relations"] == [{"type": "属于", "target": "王翠华的弟弟"}]
    assert d["node_id"] == card.node_id
