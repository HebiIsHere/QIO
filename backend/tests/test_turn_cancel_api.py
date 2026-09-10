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


def test_cancel_when_idle_returns_false(client):
    r = client.post("/api/turns/cancel")
    assert r.status_code == 200
    body = r.json()
    assert body["cancelled"] is False
    assert body["turn_id"] is None
