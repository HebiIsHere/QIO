from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.graph.nodes import NodeService
from agent.knowledge.lifecycle import KnowledgeService


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _knowledge(conn: sqlite3.Connection) -> str:
    ks = KnowledgeService(conn)
    item = ks.create(category="user_profile", content="先讲逻辑再给代码", node_ids=[])
    ks.submit(item.id)
    ks.verify(item.id, verified_by="user")
    ks.activate(item.id)
    return item.id


def test_scope_can_move_from_global_to_a_topic(client, db_conn):
    user_node_id = NodeService(db_conn).get_or_create_user_root().id
    topic = NodeService(db_conn).create_topic("开发 QIO")
    knowledge_id = _knowledge(db_conn)

    # 默认：全局（你）
    first = client.post(f"/api/knowledge/{knowledge_id}/scope", json={"type": "global"}).json()
    assert first["knowledge"]["scope"] == "全局（你）"
    assert first["knowledge"]["topic_id"] is None

    # 改成"只在某个话题里生效"
    second = client.post(
        f"/api/knowledge/{knowledge_id}/scope",
        json={"type": "topic", "topic_id": topic.id},
    ).json()
    assert second["knowledge"]["scope"] == "话题：开发 QIO"
    assert second["knowledge"]["topic_id"] == topic.id

    stored = KnowledgeService(db_conn).get(knowledge_id)
    assert stored is not None
    assert stored.node_ids == [topic.id]

    # 还能改回全局
    third = client.post(f"/api/knowledge/{knowledge_id}/scope", json={"type": "global"}).json()
    assert third["knowledge"]["scope"] == "全局（你）"
    assert KnowledgeService(db_conn).get(knowledge_id).node_ids == [user_node_id]


def test_scope_rejects_unknown_topic(client, db_conn):
    knowledge_id = _knowledge(db_conn)
    resp = client.post(
        f"/api/knowledge/{knowledge_id}/scope", json={"type": "topic", "topic_id": "topic_nope"}
    )
    assert resp.status_code == 404
