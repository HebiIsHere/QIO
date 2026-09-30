"""开发任务列表接口：界面的「未完成任务」入口靠它。

任务状态以前只在工具调用里出现（模型说出来，用户才知道）。刷新、重启、换设备
之后就再也找不到那个任务了。这里给出权威列表：状态来自工作区本身。
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch):
    """本机可能设了 QIO_DATA_DIR（开发用），它会让 Settings 改用真实数据目录 —— 
    这些用例必须只看自己那份临时目录。"""
    monkeypatch.delenv("QIO_DATA_DIR", raising=False)


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def test_dev_tasks_lists_the_authoritative_state(client):
    ctx = client.app.state.ctx
    task = ctx.dev_workspaces.create("做一个求和工具")

    body = client.get("/api/dev/tasks").json()
    assert [row["id"] for row in body["tasks"]] == [task.id]
    row = body["tasks"][0]
    assert row["request"] == "做一个求和工具"
    assert row["phase"] == "created"
    assert row["submitted"] is False
    assert row["test_passed"] is None


def test_dev_tasks_reports_test_evidence_state(client):
    ctx = client.app.state.ctx
    task = ctx.dev_workspaces.create("x")
    ctx.dev_workspaces.record_test(task.id, True, "1/1 tests passed")
    ctx.dev_workspaces.write_file(task.id, "tool.py", "broken")  # 通过之后又改过

    row = client.get("/api/dev/tasks").json()["tasks"][0]
    assert row["test_passed"] is True
    assert row["test_evidence_current"] is False
    assert row["updated_at"]


def test_dev_tasks_is_empty_on_a_fresh_install(client):
    assert client.get("/api/dev/tasks").json() == {"tasks": []}
