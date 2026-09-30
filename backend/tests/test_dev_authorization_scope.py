"""范围授权的查询与撤销。

回归的缺口：用户批准过一次「可以在这个环境里跑这个任务的生成代码」之后，
既没有一个地方能看清**授权范围**（在哪儿跑、能碰什么、用哪个凭据），
也没有任何办法收回 —— 只能一直有效。
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.tools.dev_workspace import DevWorkspace

SCOPE = {
    "executor": "subprocess",
    "isolated": False,
    "capabilities": ["受限子进程"],
    "filesystem": [],
    "network": False,
    "network_allow": [],
    "credentials": ["weather_key"],
}


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch):
    monkeypatch.delenv("QIO_DATA_DIR", raising=False)


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def test_scope_is_recorded_with_the_grant(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("查天气")

    ws.grant_test_authorization(
        task.id, policy_fingerprint="p1", executor="subprocess", scope=SCOPE
    )

    rows = ws.authorizations()
    assert len(rows) == 1
    row = rows[0]
    assert row["task_id"] == task.id
    assert row["request"] == "查天气"
    assert row["executor"] == "subprocess"
    assert row["credentials"] == ["weather_key"]
    assert row["granted_at"]


def test_revoke_makes_the_next_run_ask_again(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("查天气")
    ws.grant_test_authorization(
        task.id, policy_fingerprint="p1", executor="subprocess", scope=SCOPE
    )
    assert ws.test_authorized(task.id, policy_fingerprint="p1", executor="subprocess")

    assert ws.revoke_test_authorization(task.id) is True

    assert ws.test_authorized(task.id, policy_fingerprint="p1", executor="subprocess") is False
    assert ws.authorizations() == []


def test_revoke_survives_a_restart(tmp_path):
    root = tmp_path / "ws"
    ws = DevWorkspace(root)
    task = ws.create("查天气")
    ws.grant_test_authorization(
        task.id, policy_fingerprint="p1", executor="subprocess", scope=SCOPE
    )
    ws.revoke_test_authorization(task.id)

    reborn = DevWorkspace(root)

    assert reborn.test_authorized(task.id, policy_fingerprint="p1", executor="subprocess") is False


def test_revoking_an_unknown_task_changes_nothing(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    assert ws.revoke_test_authorization("ws_ffffffffffff") is False


def test_api_lists_and_revokes_authorizations(client):
    ctx = client.app.state.ctx
    task = ctx.dev_workspaces.create("查天气")
    ctx.dev_workspaces.grant_test_authorization(
        task.id, policy_fingerprint="p1", executor="subprocess", scope=SCOPE
    )

    listed = client.get("/api/dev/authorizations").json()
    assert [row["task_id"] for row in listed["authorizations"]] == [task.id]
    assert listed["authorizations"][0]["credentials"] == ["weather_key"]

    revoked = client.post(f"/api/dev/authorizations/{task.id}/revoke")
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] is True
    assert client.get("/api/dev/authorizations").json()["authorizations"] == []


def test_dev_tasks_reports_whether_a_task_is_authorized(client):
    ctx = client.app.state.ctx
    task = ctx.dev_workspaces.create("查天气")
    assert client.get("/api/dev/tasks").json()["tasks"][0]["authorized"] is False

    ctx.dev_workspaces.grant_test_authorization(
        task.id, policy_fingerprint="p1", executor="subprocess", scope=SCOPE
    )

    assert client.get("/api/dev/tasks").json()["tasks"][0]["authorized"] is True
