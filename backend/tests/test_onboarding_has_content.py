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


def _has_content(client) -> bool:
    return client.get("/api/onboarding/status").json()["has_content"]


def test_truly_empty_client_counts_as_new(client):
    assert _has_content(client) is False


def test_existing_topic_counts_as_content_even_without_messages(client, db_conn):
    """有话题（哪怕还没聊过）= 不是空白客户端，引导应当可以关掉。"""
    NodeService(db_conn).create_topic("开发 QIO")
    assert _has_content(client) is True


def test_existing_knowledge_counts_as_content(client, db_conn):
    ks = KnowledgeService(db_conn)
    ks.create(category="general_fact", content="雨天的地铁更挤")
    assert _has_content(client) is True
