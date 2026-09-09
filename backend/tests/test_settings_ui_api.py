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


def test_ui_get_missing_uses_default(client):
    r = client.get("/api/settings/ui")
    assert r.status_code == 200
    assert r.json()["typewriter_cps"] == 50


def test_ui_put_saves_cps(client):
    r = client.put("/api/settings/ui", json={"typewriter_cps": 75})
    assert r.status_code == 200
    assert r.json()["typewriter_cps"] == 75
    assert client.get("/api/settings/ui").json()["typewriter_cps"] == 75


def test_ui_put_rejects_unsupported_cps(client):
    r = client.put("/api/settings/ui", json={"typewriter_cps": 33})
    assert r.status_code == 400


def test_ui_put_rejects_non_int(client):
    r = client.put("/api/settings/ui", json={"typewriter_cps": "fast"})
    assert r.status_code == 400
