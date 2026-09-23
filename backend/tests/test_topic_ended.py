from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.graph.nodes import NodeService
from agent.graph.topics import TopicService
from agent.selector.base import IndexedDoc
from agent.selector.selector import Selector
from agent.services.retrieval import Retriever


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_ended_topic_leaves_main_view_and_shows_in_ended_group(client, db_conn):
    topic = NodeService(db_conn).create_topic("开发 QIO")

    before = client.get("/api/planet/overview").json()
    assert [t["topic_id"] for t in before["topics"]] == [topic.id]
    assert before["ended_topics"] == []

    assert client.post(f"/api/graph/topics/{topic.id}/end").status_code == 200
    after = client.get("/api/planet/overview").json()
    assert after["topics"] == []
    assert [t["topic_id"] for t in after["ended_topics"]] == [topic.id]

    listed = client.get("/api/graph/topics").json()["topics"]
    assert listed[0]["ended"] is True

    assert client.post(f"/api/graph/topics/{topic.id}/resume").status_code == 200
    back = client.get("/api/planet/overview").json()
    assert [t["topic_id"] for t in back["topics"]] == [topic.id]


def test_ended_topic_is_still_reachable_by_memory_search(db_conn):
    """结束 ≠ 删掉：它的片段仍然留在记忆里，能被检索到。"""
    topic = NodeService(db_conn).create_topic("开发 QIO")
    nodes = NodeService(db_conn)
    nodes.mark_topic_ended(topic.id)

    selector = Selector()
    selector.load(
        [
            IndexedDoc(
                doc_id="idx_memory",
                text="QIO 记忆问题讨论：片段与知识的职责",
                topic_id=topic.id,
                keywords=["记忆", "QIO"],
                created_at=_iso(1),
            )
        ],
        titles={"idx_memory": "QIO 记忆问题"},
    )
    retriever = Retriever(selector, TopicService(db_conn))

    hits = retriever.search("记忆 问题", top_k=3)
    assert [h.doc_id for h in hits] == ["idx_memory"]
    # 已结束话题不再参与联想（不给 affinity 加分），但检索本身不受影响
    assert topic.id not in retriever._fingerprint_scores("记忆 QIO")
