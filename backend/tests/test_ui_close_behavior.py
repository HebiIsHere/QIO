"""R9 §11：关闭主窗口行为设置（ui.close_behavior）的持久化与校验。

契约：默认 `ask`，取值 `ask|tray|quit`，经 `/api/settings/ui` 持久化；
旧客户端没有这个字段时按默认，不报错。
"""

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


def test_close_behavior_default_is_ask(client):
    body = client.get("/api/settings/ui").json()
    assert body["close_behavior"] == "ask"
    # 旧字段照旧返回：不是「有了新字段就把旧的藏起来」。
    assert body["typewriter_cps"] == 50


def test_close_behavior_round_trip_and_persisted(client):
    for value in ("tray", "quit", "ask"):
        r = client.put("/api/settings/ui", json={"close_behavior": value})
        assert r.status_code == 200, r.text
        assert r.json()["close_behavior"] == value
        assert client.get("/api/settings/ui").json()["close_behavior"] == value


def test_close_behavior_rejects_unknown_value(client):
    r = client.put("/api/settings/ui", json={"close_behavior": "hide"})
    assert r.status_code == 400
    # 非法值不能落库：默认仍是 ask。
    assert client.get("/api/settings/ui").json()["close_behavior"] == "ask"


def test_old_client_put_with_only_typewriter_still_works(client):
    r = client.put("/api/settings/ui", json={"typewriter_cps": 25})
    assert r.status_code == 200
    assert r.json()["typewriter_cps"] == 25
    assert r.json()["close_behavior"] == "ask"


def test_put_with_no_known_field_is_rejected(client):
    r = client.put("/api/settings/ui", json={})
    assert r.status_code == 400


def test_close_behavior_and_typewriter_saved_together(client):
    r = client.put(
        "/api/settings/ui", json={"typewriter_cps": 75, "close_behavior": "quit"}
    )
    assert r.status_code == 200
    body = client.get("/api/settings/ui").json()
    assert body["typewriter_cps"] == 75
    assert body["close_behavior"] == "quit"
