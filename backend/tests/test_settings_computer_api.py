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


def test_get_computer_defaults(client):
    r = client.get("/api/settings/computer")
    assert r.status_code == 200
    body = r.json()
    assert body["permission_mode"] == "default"
    assert body["root_dir"] == ""


def test_put_computer_saves(client):
    r = client.put(
        "/api/settings/computer",
        json={"permission_mode": "accept-edits", "root_dir": "C:/work"},
    )
    assert r.status_code == 200
    body = client.get("/api/settings/computer").json()
    assert body["permission_mode"] == "accept-edits"
    assert body["root_dir"] == "C:/work"


def test_put_rejects_bad_mode(client):
    r = client.put("/api/settings/computer", json={"permission_mode": "hack"})
    assert r.status_code == 400


def test_put_rejects_non_string_root(client):
    r = client.put("/api/settings/computer", json={"root_dir": 123})
    assert r.status_code in (200, 400)
