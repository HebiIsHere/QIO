from __future__ import annotations

import json
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

def test_ui_conversation_layout_round_trip(client):
    layout = {
        "version": 1,
        "leftWidth": 300,
        "leftCollapsed": False,
        "rightWidth": 420,
        "rightOpen": True,
        "taskTab": "current",
        "previewFull": False,
        "previewTabs": [],
        "activePreviewTabId": None,
    }
    r = client.put(
        "/api/settings/ui", json={"conversation_layout": json.dumps(layout, ensure_ascii=False)}
    )
    assert r.status_code == 200, r.text
    assert r.json()["conversation_layout"]["leftWidth"] == 300
    fetched = client.get("/api/settings/ui").json()["conversation_layout"]
    assert fetched["rightWidth"] == 420
    assert fetched["rightOpen"] is True


def test_ui_conversation_layout_rejects_bad_or_oversized_values(client):
    assert client.put(
        "/api/settings/ui", json={"conversation_layout": "{not json"}
    ).status_code == 400
    assert client.put(
        "/api/settings/ui", json={"conversation_layout": {"leftWidth": -1}}
    ).status_code == 400
    huge = {"previewTabs": [{"id": "x" * 70000}]}
    assert client.put(
        "/api/settings/ui", json={"conversation_layout": huge}
    ).status_code == 400


def test_ui_put_without_known_keys_is_rejected(client):
    r = client.put("/api/settings/ui", json={"unknown": 1})
    assert r.status_code == 400

