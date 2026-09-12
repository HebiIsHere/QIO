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


def test_put_applies_config_to_running_search_service(client):
    """保存设置必须立刻生效（此前只写 store，运行中的 SearchService 不更新，等于要重启）。"""
    ctx = client.app.state.ctx
    client.put(
        "/api/settings/search",
        json={
            "searxng_url": "http://127.0.0.1:8080",
            "bocha_api_key": "bk-abc",
            "keyless_fallback": False,
        },
    )
    names = [p.name for p in ctx.search_service._resolver.providers()]
    assert names == ["bocha", "searxng", "bing", "baidu"]


def test_keyless_fallback_default_on_and_toggleable(client):
    ctx = client.app.state.ctx
    body = client.get("/api/settings/search").json()
    assert body["keyless_fallback"] is True  # 默认开启免密钥通道
    assert ctx.search_service._resolver.keyless is True

    client.put("/api/settings/search", json={"keyless_fallback": False})
    assert client.get("/api/settings/search").json()["keyless_fallback"] is False
    assert ctx.search_service._resolver.keyless is False

    client.put("/api/settings/search", json={"keyless_fallback": True})
    assert ctx.search_service._resolver.keyless is True
