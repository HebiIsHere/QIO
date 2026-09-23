from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.entities.cards import EntityCardService
from agent.graph.nodes import NodeService
from agent.knowledge.lifecycle import KnowledgeService


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _payload(**overrides) -> dict:
    base = {
        "name": "祠莎",
        "background": "学生",
        "current_focus": "开发 QIO",
        "interests": ["agent 记忆问题"],
        "familiarity": "刚入门",
        "limits": {"dont_do": "不要自动动用外部工具", "how_to_talk": "先讲逻辑再给代码"},
        "preferences": [
            {"kind": "verbosity", "value": "简洁", "scope": {"type": "global"}},
            {"kind": "explanation", "value": "先讲逻辑再给代码", "scope": {"type": "topic", "topic_title": "开发 QIO"}},
        ],
        "goals": ["把 QIO 的记忆问题做完"],
        "inferred": [],
    }
    base.update(overrides)
    return base


def _active(conn: sqlite3.Connection, category: str | None = None):
    ks = KnowledgeService(conn)
    return [i for i in ks.list_items(category=category) if i.state.value == "active"]


def test_submit_attaches_profile_to_the_user_anchor(client, db_conn):
    user_node_id = NodeService(db_conn).get_or_create_user_root().id
    assert client.get("/api/onboarding/status").json()["has_content"] is False

    body = client.post("/api/onboarding/submit", json=_payload()).json()
    assert body["self_card_id"]

    rows = _active(db_conn, "user_profile")
    assert rows, "画像知识应当被写下来"
    for item in rows:
        if "·仅" in item.content:
            # 只在某个话题里生效的偏好：挂在那个话题上，而不是全局
            continue
        assert item.node_ids == [user_node_id], f"{item.content} 没有挂到「你」上"

    contents = " ".join(item.content for item in rows)
    assert "祠莎" in contents and "学生" in contents and "开发 QIO" in contents


def test_submit_goal_becomes_knowledge_and_topic(client, db_conn):
    client.post("/api/onboarding/submit", json=_payload())

    goals = _active(db_conn, "goal")
    assert any("把 QIO 的记忆问题做完" in g.content for g in goals)
    topic_names = {t.name for t in NodeService(db_conn).list_topics()}
    assert "把 QIO 的记忆问题做完" in topic_names


def test_submit_preference_scope_picks_the_right_anchor(client, db_conn):
    user_node_id = NodeService(db_conn).get_or_create_user_root().id
    client.post("/api/onboarding/submit", json=_payload())

    rows = _active(db_conn, "user_profile")
    topic = next(t for t in NodeService(db_conn).list_topics() if t.name == "开发 QIO")
    global_pref = next(i for i in rows if i.content.startswith("偏好（详略）"))
    topic_pref = next(i for i in rows if i.content.startswith("偏好（解释方式·仅开发 QIO）"))
    assert global_pref.node_ids == [user_node_id]
    assert topic_pref.node_ids == [topic.id]


def test_inferred_knowledge_waits_for_confirmation(client, db_conn):
    client.post(
        "/api/onboarding/submit",
        json=_payload(inferred=[{"content": "用户可能在做记忆相关的研究", "category": "user_profile"}]),
    )
    ks = KnowledgeService(db_conn)
    pending = [i for i in ks.list_items(category="user_profile") if i.state.value == "pending_review"]
    assert len(pending) == 1
    assert pending[0].provenance.get("inferred") is True


def test_submit_is_idempotent(client, db_conn):
    client.post("/api/onboarding/submit", json=_payload())
    first = len(_active(db_conn))
    first_topics = len(NodeService(db_conn).list_topics())

    client.post("/api/onboarding/submit", json=_payload())
    assert len(_active(db_conn)) == first
    assert len(NodeService(db_conn).list_topics()) == first_topics


def test_current_focus_can_be_submitted_as_ended(client, db_conn):
    client.post("/api/onboarding/submit", json=_payload(current_focus_ended=True))
    rows = _active(db_conn, "user_profile")
    focus = next(i for i in rows if i.content.startswith("最近在做："))
    assert focus.provenance.get("ended_at")


def test_rename_keeps_a_single_self_card(client, db_conn):
    first = client.post("/api/onboarding/submit", json=_payload()).json()
    second = client.post("/api/onboarding/submit", json=_payload(name="柯莎")).json()

    assert first["self_card_id"] == second["self_card_id"]
    card = EntityCardService(db_conn).get(first["self_card_id"])
    assert card is not None
    assert card.name == "柯莎"
    assert "祠莎" in card.aliases
    active_cards = EntityCardService(db_conn).list_active()
    assert len([c for c in active_cards if c.id == card.id]) == 1
    assert not [c for c in active_cards if c.name == "祠莎" and c.id != card.id]


def test_submit_requires_name(client):
    assert client.post("/api/onboarding/submit", json={"name": "  "}).status_code == 400
