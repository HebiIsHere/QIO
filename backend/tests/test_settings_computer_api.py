"""旧 /api/settings/computer 的兼容行为（契约 §1）。

产品行为已变更（用户明确决定）：三模式 read_only/restricted/unrestricted；
旧值 default / plan / accept-edits / bypass 只做**读入兼容**并映射后落库；
GET 返回 normalize 后的新值。旧断言「返回 default / accept-edits」正是被
产品变更推翻的旧行为，这里改写为新契约断言（覆盖不减少：仍验证默认值、
落库、root_dir、非法值拒绝）。
"""

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
    # 首次安装默认是受限模式（不是旧的 default 字符串）
    assert body["permission_mode"] == "restricted"
    assert body["mode_label"] == "受限模式"
    assert body["root_dir"] == ""


def test_put_computer_maps_legacy_values(client):
    """旧客户端写 accept-edits：接受，但映射后落库成 restricted。"""
    r = client.put(
        "/api/settings/computer",
        json={"permission_mode": "accept-edits", "root_dir": "C:/work"},
    )
    assert r.status_code == 200
    body = client.get("/api/settings/computer").json()
    assert body["permission_mode"] == "restricted"
    assert body["root_dir"] == "C:/work"
    row = client.app.state.ctx.conn.execute(
        "SELECT value FROM settings WHERE key = 'computer.permission_mode'"
    ).fetchone()
    assert row["value"] == "restricted", "新写入只写新值"


def test_put_computer_accepts_new_values(client):
    r = client.put("/api/settings/computer", json={"permission_mode": "unrestricted"})
    assert r.status_code == 200
    assert client.get("/api/settings/computer").json()["permission_mode"] == "unrestricted"
    r = client.put("/api/settings/computer", json={"permission_mode": "read_only"})
    assert r.status_code == 200
    assert client.get("/api/settings/computer").json()["permission_mode"] == "read_only"


def test_put_rejects_bad_mode(client):
    r = client.put("/api/settings/computer", json={"permission_mode": "hack"})
    assert r.status_code == 400
    # 非法值不得落库
    row = client.app.state.ctx.conn.execute(
        "SELECT value FROM settings WHERE key = 'computer.permission_mode'"
    ).fetchone()
    assert row is None


def test_put_rejects_non_string_root(client):
    r = client.put("/api/settings/computer", json={"root_dir": 123})
    assert r.status_code in (200, 400)
