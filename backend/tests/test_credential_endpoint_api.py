"""凭据端点是安全身份，不是普通元数据：改 endpoint 必须重新输入 key 并显式确认。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring


@pytest.fixture()
def client(db_conn, settings: Settings):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _create(client, endpoint="https://api.openai.com/v1"):
    resp = client.post(
        "/api/credentials",
        json={
            "key_id": "k1",
            "secret": "sk-original",
            "tags": ["main-loop"],
            "endpoint": endpoint,
            "default_model": "gpt-4o-mini",
        },
    )
    assert resp.status_code == 200
    return resp


def test_endpoint_change_without_secret_is_rejected(client):
    _create(client)
    resp = client.patch(
        "/api/credentials/k1", json={"endpoint": "https://attacker.example/v1"}
    )
    assert resp.status_code == 400
    meta = client.get("/api/credentials").json()["credentials"][0]
    assert meta["endpoint"] == "https://api.openai.com/v1"


def test_endpoint_change_needs_explicit_confirmation(client):
    _create(client)
    resp = client.patch(
        "/api/credentials/k1",
        json={"endpoint": "https://attacker.example/v1", "secret": "sk-new"},
    )
    assert resp.status_code == 400
    assert "confirm_reconfigure" in resp.json()["detail"]


def test_endpoint_change_with_secret_and_confirmation_rotates_the_key(client):
    _create(client)
    resp = client.patch(
        "/api/credentials/k1",
        json={
            "endpoint": "https://gateway.example/v1",
            "secret": "sk-new",
            "confirm_reconfigure": True,
        },
    )
    assert resp.status_code == 200
    meta = resp.json()["credential"]
    assert meta["endpoint"] == "https://gateway.example/v1"
    assert meta["version"] == 2, "换提供商必须轮换密钥版本"
    assert client.app.state.ctx.credentials.get_secret("k1") == "sk-new"


def test_plain_http_endpoint_only_for_localhost(client):
    _create(client)
    resp = client.patch(
        "/api/credentials/k1",
        json={
            "endpoint": "http://remote.example/v1",
            "secret": "sk-new",
            "confirm_reconfigure": True,
        },
    )
    assert resp.status_code == 400

    ok = client.patch(
        "/api/credentials/k1",
        json={
            "endpoint": "http://127.0.0.1:11434/v1",
            "secret": "sk-new",
            "confirm_reconfigure": True,
        },
    )
    assert ok.status_code == 200


def test_metadata_edit_without_endpoint_change_needs_no_secret(client):
    _create(client)
    resp = client.patch("/api/credentials/k1", json={"note": "hello", "budget": 500})
    assert resp.status_code == 200
    meta = resp.json()["credential"]
    assert meta["note"] == "hello"
    assert meta["version"] == 1
    assert client.app.state.ctx.credentials.get_secret("k1") == "sk-original"
