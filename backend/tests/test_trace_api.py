from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def test_list_empty(client):
    r = client.get("/api/traces")
    assert r.status_code == 200
    body = r.json()
    assert body["traces"] == []
    assert body["total"] == 0


def test_get_missing_trace_404(client):
    assert client.get("/api/traces/nope").status_code == 404


def test_trace_roundtrip_via_store(client):
    ctx = client.app.state.ctx
    ctx.trace_store.begin("turn_1", initial_topic="t")
    ctx.trace_store.finish("turn_1", "done", final_preview="ok")
    listed = client.get("/api/traces").json()
    assert listed["total"] == 1
    got = client.get("/api/traces/turn_1").json()
    assert got["status"] == "done"
    assert got["final_preview"] == "ok"


def test_trace_pagination(client):
    ctx = client.app.state.ctx
    for i in range(5):
        ctx.trace_store.begin(f"t{i}")
        ctx.trace_store.finish(f"t{i}", "done")
    page = client.get("/api/traces?limit=2&offset=1").json()
    assert len(page["traces"]) == 2
    assert page["total"] == 5
    assert page["limit"] == 2 and page["offset"] == 1


def test_trace_settings_toggle(client):
    assert client.get("/api/settings/trace").json()["enabled"] is True
    r = client.put("/api/settings/trace", json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    assert client.get("/api/settings/trace").json()["enabled"] is False


def test_trace_list_does_not_expose_raw_fields(client):
    ctx = client.app.state.ctx
    ctx.trace_store.begin("t1")
    ctx.trace_store.finish("t1", "done")
    item = client.get("/api/traces").json()["traces"][0]
    # 列表只返回元数据，不含 injection/model_calls 等重字段
    assert "model_calls" not in item
    assert "injection" not in item
