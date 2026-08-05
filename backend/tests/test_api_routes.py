from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def test_credential_crud(client):
    resp = client.post(
        "/api/credentials",
        json={
            "key_id": "main-key",
            "secret": "sk-test",
            "tags": ["main-loop"],
            "endpoint": "https://api.example.com/v1",
            "default_model": "m1",
            "budget": 1000,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == 1

    listing = client.get("/api/credentials").json()["credentials"]
    assert len(listing) == 1
    assert listing[0]["key_id"] == "main-key"
    assert "secret" not in listing[0]

    resp = client.post("/api/credentials/main-key/revoke")
    assert resp.status_code == 200
    meta = client.get("/api/credentials").json()["credentials"][0]
    assert meta["status"] == "revoked"


def test_credential_test_endpoint_no_secret(client):
    resp = client.post("/api/credentials/ghost/test")
    assert resp.status_code == 404


async def test_turn_without_credential_warns(client):
    ctx = client.app.state.ctx
    collected: list[str] = []

    async def consume():
        async for chunk in ctx.bus.stream():
            collected.append(chunk)
            if "WARNING" in chunk:
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    result = await ctx.run_turn("你好")
    assert result["ok"] is False
    assert result["reason"] == "no_credential"
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert any("WARNING" in c for c in collected)


def test_graph_topics_and_positions(client):
    topics = client.get("/api/graph/topics").json()["topics"]
    assert isinstance(topics, list)
    positions = client.get("/api/graph/positions").json()["topics"]
    assert isinstance(positions, list)


def test_graph_position_inertia(client):
    app = client.app
    ctx = app.state.ctx
    topic = ctx.topics.nodes.create_topic("测试话题")
    first = client.get("/api/graph/positions").json()["topics"]
    assert any(t["topic_id"] == topic.id and len(t["position"]) == 3 for t in first)
    second = client.get("/api/graph/positions").json()["topics"]
    pos1 = {t["topic_id"]: t["position"] for t in first}
    pos2 = {t["topic_id"]: t["position"] for t in second}
    assert pos1 == pos2


def test_topic_detail(client):
    app = client.app
    ctx = app.state.ctx
    topic = ctx.topics.nodes.create_topic("话题A")
    resp = client.get(f"/api/graph/topics/{topic.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "话题A"
    assert "fragments" in data and "entities" in data and "knowledge" in data