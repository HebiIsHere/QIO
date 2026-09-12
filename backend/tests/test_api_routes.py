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


def test_credential_update_metadata_enable_disable_audit(client):
    resp = client.post(
        "/api/credentials",
        json={"key_id": "k1", "secret": "sk-1", "tags": ["main-loop"]},
    )
    assert resp.status_code == 200

    resp = client.patch(
        "/api/credentials/k1",
        json={"note": "hello", "budget": 500},
    )
    assert resp.status_code == 200
    meta = resp.json()["credential"]
    assert meta["note"] == "hello"
    assert meta["budget"] == 500
    assert meta["version"] == 1

    assert client.post("/api/credentials/k1/disable").json()["credential"]["enabled"] is False
    assert client.app.state.ctx.credentials.get_secret("k1") is None
    assert client.post("/api/credentials/k1/enable").json()["credential"]["enabled"] is True
    assert client.app.state.ctx.credentials.get_secret("k1") == "sk-1"

    audit = client.get("/api/credentials/k1/audit").json()["audit"]
    assert [e["action"] for e in audit] == ["create", "update", "update", "update"]


def test_credential_delete_removes_record(client):
    client.post(
        "/api/credentials",
        json={"key_id": "todelete", "secret": "s", "tags": ["main-loop"]},
    )
    resp = client.delete("/api/credentials/todelete")
    assert resp.status_code == 200
    listing = client.get("/api/credentials").json()["credentials"]
    assert all(c["key_id"] != "todelete" for c in listing)
    assert client.delete("/api/credentials/todelete").status_code == 404




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
    # 界面文案统一中文：无凭据提示必须是中文（用户会看到这条 WARNING）
    warning = "".join(collected)
    assert "还没有配置可用的模型凭据" in warning
    assert "no main-loop credential" not in warning
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
def test_create_auto_generates_key_id_when_empty(client):
    resp = client.post(
        "/api/credentials",
        json={"key_id": "", "secret": "sk-test", "tags": ["main-loop"]},
    )
    assert resp.status_code == 200
    generated = resp.json()["key_id"]
    assert generated.startswith("key_")
    listing = client.get("/api/credentials").json()["credentials"]
    assert listing[0]["key_id"] == generated
def test_memory_settings_roundtrip(client):
    resp = client.get("/api/settings/memory")
    assert resp.status_code == 200
    assert resp.json()["fragment_max_messages"] == 10

    resp = client.put("/api/settings/memory", json={"fragment_max_messages": 5})
    assert resp.status_code == 200
    assert resp.json()["fragment_max_messages"] == 5

    resp = client.get("/api/settings/memory")
    assert resp.json()["fragment_max_messages"] == 5

    resp = client.put("/api/settings/memory", json={"fragment_max_messages": 99})
    assert resp.status_code == 400

    resp = client.put("/api/settings/memory", json={"fragment_max_messages": 0})
    assert resp.status_code == 400

    resp = client.put("/api/settings/memory", json={"fragment_max_messages": "abc"})
    assert resp.status_code == 400
