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


SECRET = "sk-abcdefghijklmnopqrstuvwxyz0123456789"


def _status(client) -> dict:
    return client.get("/api/onboarding/status").json()


def test_credential_without_usage_tag_does_not_count(client):
    """没有用途标签的密钥选不中任何角色 —— 不能当成"已经配好"。"""
    created = client.post("/api/credentials", json={"secret": SECRET}).json()
    assert _status(client)["has_credential"] is False

    client.patch(f"/api/credentials/{created['key_id']}", json={"tags": ["main-loop"]})
    assert _status(client)["has_credential"] is True


def test_disabled_credential_does_not_count(client):
    created = client.post(
        "/api/credentials", json={"secret": SECRET, "tags": ["main-loop"]}
    ).json()
    assert _status(client)["has_credential"] is True

    client.post(f"/api/credentials/{created['key_id']}/disable")
    assert _status(client)["has_credential"] is False


def test_revoked_credential_does_not_count(client):
    created = client.post(
        "/api/credentials", json={"secret": SECRET, "tags": ["main-loop"]}
    ).json()
    client.post(f"/api/credentials/{created['key_id']}/revoke")
    assert _status(client)["has_credential"] is False
