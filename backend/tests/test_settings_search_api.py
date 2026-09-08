from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings


@pytest.fixture
def client(db_conn: sqlite3.Connection, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def test_get_missing_uses_defaults(client):
    r = client.get("/api/settings/search")
    assert r.status_code == 200
    body = r.json()
    assert "bocha_has_key" in body
    assert body["bocha_has_key"] is False
    assert body["searxng_url"] == ""


def test_put_and_get_roundtrip(client):
    r = client.put(
        "/api/settings/search",
        json={
            "searxng_url": "http://127.0.0.1:8080",
            "bocha_api_key": "bk-abc",
            "top_k_default": 8,
            "max_fetch_chars": 20000,
        },
    )
    assert r.status_code == 200
    body = client.get("/api/settings/search").json()
    assert body["searxng_url"] == "http://127.0.0.1:8080"
    assert body["bocha_has_key"] is True
    assert "bk-abc" not in str(body)
    assert body["top_k_default"] == 8
    assert body["max_fetch_chars"] == 20000


def test_put_clears_key_when_empty(client):
    client.put("/api/settings/search", json={"bocha_api_key": "bk-1"})
    client.put("/api/settings/search", json={"bocha_api_key": ""})
    body = client.get("/api/settings/search").json()
    assert body["bocha_has_key"] is False


def test_put_rejects_invalid_int(client):
    r = client.put("/api/settings/search", json={"top_k_default": "abc"})
    assert r.status_code == 400
