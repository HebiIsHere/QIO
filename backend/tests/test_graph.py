from __future__ import annotations

import sqlite3

import pytest

from agent.graph.anchors import AnchorService
from agent.graph.edges import EdgeService
from agent.graph.nodes import NodeService
from agent.graph.topics import TopicService


@pytest.fixture()
def nodes(db_conn: sqlite3.Connection) -> NodeService:
    return NodeService(db_conn)


@pytest.fixture()
def edges(db_conn: sqlite3.Connection) -> EdgeService:
    return EdgeService(db_conn)


@pytest.fixture()
def anchors(db_conn: sqlite3.Connection) -> AnchorService:
    return AnchorService(db_conn)


def test_entity_mentions_table_in_schema(db_conn: sqlite3.Connection):
    tables = {
        r["name"]
        for r in db_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "entity_mentions" in tables


def test_user_root_singleton(nodes: NodeService):
    first = nodes.get_or_create_user_root()
    second = nodes.get_or_create_user_root()
    assert first.id == second.id
    assert first.type == "user"
    # exactly one user node
    rows = nodes.conn.execute("SELECT COUNT(*) AS c FROM nodes WHERE type='user'").fetchone()
    assert rows["c"] == 1


def test_topic_creation_and_owns_edge(nodes: NodeService, edges: EdgeService):
    user = nodes.get_or_create_user_root()
    topic = nodes.create_topic("饮食偏好")
    assert topic.type == "topic"
    edges.add(user.id, topic.id, "owns")
    assert topic.id in edges.neighbors(user.id, "owns")


def test_entity_lazy_creation_by_threshold(nodes: NodeService):
    topic = nodes.create_topic("t")
    # first mention: no node yet
    entity, created = nodes.mention("牛奶", topic.id, threshold=2)
    assert entity is None and not created
    # second mention: node created
    entity, created = nodes.mention("牛奶", topic.id, threshold=2)
    assert entity is not None and created
    assert entity.name == "牛奶"
    # third mention: existing node returned
    again, created = nodes.mention("牛奶", topic.id, threshold=2)
    assert again is not None and again.id == entity.id and not created


def test_entity_force_annotation(nodes: NodeService):
    entity, created = nodes.mention("项目Alpha", None, force=True)
    assert entity is not None and created


def test_entity_merge_redirects_edges(nodes: NodeService, edges: EdgeService):
    a = nodes.create_entity("甲")
    b = nodes.create_entity("乙")
    topic = nodes.create_topic("t")
    edges.add(topic.id, a.id, "mention")
    edges.add(topic.id, b.id, "mention")
    nodes.merge_entities(a.id, b.id)
    neighbors = edges.neighbors(a.id, "mention")
    assert b.id not in edges.neighbors(b.id, "mention")
    assert topic.id in neighbors
    merged = nodes.get(b.id)
    assert merged.meta.get("merged_into") == a.id


def test_merge_requires_entities(nodes: NodeService):
    topic = nodes.create_topic("t")
    with pytest.raises(ValueError):
        nodes.merge_entities(topic.id, topic.id)


def test_anchor_switch_preserves_history(anchors: AnchorService, nodes: NodeService):
    t1 = nodes.create_topic("t1")
    t2 = nodes.create_topic("t2")
    anchors.set_active(t1.id)
    assert anchors.get_active().topic_id == t1.id
    anchors.set_active(t2.id)
    # old position preserved as history
    position = anchors.get_position(t1.id)
    assert position is not None and position.anchor_type == "history"
    assert anchors.get_active().topic_id == t2.id


def test_anchor_pending_debounce(anchors: AnchorService, nodes: NodeService):
    t1 = nodes.create_topic("t1")
    t2 = nodes.create_topic("t2")
    anchors.set_active(t1.id)
    anchors.request_pending(t2.id)
    assert anchors.get_active().topic_id == t1.id  # not yet switched
    anchors.confirm_pending()
    assert anchors.get_active().topic_id == t2.id
    # discard path
    anchors.request_pending(t1.id)
    anchors.discard_pending()
    assert anchors.get_active().topic_id == t2.id


def test_edge_weight_accumulation(edges: EdgeService, nodes: NodeService):
    t1 = nodes.create_topic("t1")
    t2 = nodes.create_topic("t2")
    edges.add(t1.id, t2.id, "related")
    edges.add(t1.id, t2.id, "related")
    edge_rows = edges.list_for(t1.id)
    assert len(edge_rows) == 1
    assert edge_rows[0].weight == 2.0


def test_topic_fingerprint_aggregates(nodes: NodeService, db_conn: sqlite3.Connection):
    topic = nodes.create_topic("饮食")
    # fragment + index rows directly (memory pipeline is M6; here we test aggregation)
    now = "2026-08-02T00:00:00+00:00"
    db_conn.execute(
        "INSERT INTO fragments (id, topic_id, summary, created_at, closed_at) "
        "VALUES ('f1', ?, '用户喜欢清淡', ?, ?)",
        (topic.id, now, now),
    )
    db_conn.execute(
        "INSERT INTO memory_index (id, fragment_id, topic_id, entity_ids, keywords, "
        "title, token_estimate, created_at) VALUES ('i1', 'f1', ?, '[]', ?, '饮食', 10, ?)",
        (topic.id, '["清淡", "饮食", "清淡", "牛奶"]', now),
    )
    service = TopicService(db_conn)
    fingerprint = service.fingerprint(topic.id)
    assert fingerprint.fragment_count == 1
    assert "清淡" in fingerprint.keywords
    assert fingerprint.summary_preview == "用户喜欢清淡"
    assert fingerprint.title == "饮食"