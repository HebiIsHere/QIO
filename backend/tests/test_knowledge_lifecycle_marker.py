from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.knowledge.lifecycle import KnowledgeService


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _active_knowledge(
    conn: sqlite3.Connection,
    *,
    content: str = "用户偏好清淡饮食",
    provenance: dict | None = None,
    node_ids: list[str] | None = None,
):
    ks = KnowledgeService(conn)
    item = ks.create(
        category="user_profile",
        content=content,
        node_ids=node_ids or [],
        provenance=provenance if provenance is not None else {"fragment_id": "frag_1"},
    )
    ks.submit(item.id)
    ks.verify(item.id, verified_by="user")
    ks.activate(item.id)
    active = ks.get(item.id)
    assert active is not None
    return active


def test_mark_ended_keeps_state_and_stamps_provenance(db_conn: sqlite3.Connection):
    ks = KnowledgeService(db_conn)
    item = _active_knowledge(db_conn)

    ended = ks.mark_ended(item.id, reason="user_confirmed")
    assert ended.provenance["ended_at"]
    assert ended.provenance["ended_reason"] == "user_confirmed"
    # 结束不是状态迁移：条目仍然是 active，只是权重降低
    assert ended.state.value == "active"

    resumed = ks.resume(item.id)
    assert "ended_at" not in resumed.provenance
    assert "ended_reason" not in resumed.provenance


def test_payload_exposes_source_scope_and_ended(client, db_conn: sqlite3.Connection):
    item = _active_knowledge(db_conn, provenance={"source": "onboarding"})

    first = client.get("/api/knowledge").json()["knowledge"][0]
    assert first["source"] == "引导"
    assert first["ended"] is False
    assert first["ended_at"] is None
    assert isinstance(first["scope"], str) and first["scope"]

    ended = client.post(f"/api/knowledge/{item.id}/end", json={"reason": "user_confirmed"})
    assert ended.status_code == 200
    body = client.get("/api/knowledge").json()["knowledge"][0]
    assert body["ended"] is True
    assert body["ended_at"]

    resumed = client.post(f"/api/knowledge/{item.id}/resume")
    assert resumed.status_code == 200
    assert client.get("/api/knowledge").json()["knowledge"][0]["ended"] is False


def test_scope_names_the_attached_topic(client, db_conn: sqlite3.Connection):
    from agent.graph.nodes import NodeService

    topic = NodeService(db_conn).create_topic("开发 QIO")
    _active_knowledge(db_conn, content="这个话题下先讲逻辑再给代码", node_ids=[topic.id])

    row = client.get("/api/knowledge").json()["knowledge"][0]
    assert row["scope"] == "话题：开发 QIO"
