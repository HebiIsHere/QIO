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


def test_loop_settings_defaults(client):
    r = client.get("/api/settings/loop")
    assert r.status_code == 200
    body = r.json()
    assert body["max_iterations"] == 128
    assert body["output_token_budget"] == 51200


def test_loop_settings_roundtrip(client):
    r = client.put(
        "/api/settings/loop",
        json={"max_iterations": 200, "output_token_budget": 90000},
    )
    assert r.status_code == 200
    body = client.get("/api/settings/loop").json()
    assert body["max_iterations"] == 200
    assert body["output_token_budget"] == 90000


def test_loop_settings_reject_bad_iterations(client):
    assert client.put("/api/settings/loop", json={"max_iterations": 0}).status_code == 400
    assert client.put("/api/settings/loop", json={"max_iterations": 5000}).status_code == 400
    assert client.put("/api/settings/loop", json={"max_iterations": "x"}).status_code == 400


def test_loop_settings_reject_negative_tokens(client):
    assert client.put(
        "/api/settings/loop", json={"output_token_budget": -1}
    ).status_code == 400
