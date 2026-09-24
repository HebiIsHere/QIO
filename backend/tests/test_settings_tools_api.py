"""工具调用历史的两个设置：保存输出全文、输出保留天数。

保留期到点只清空**输出全文**，参数、状态、失败原因、耗时继续保留 ——
三个月后仍要能查到「当时哪个工具失败、为什么失败」。
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.storage.tool_records import get_record, record_tool_call


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def ctx(client) -> AppContext:
    return client.app.state.ctx


def test_tool_history_settings_defaults(client):
    body = client.get("/api/settings/tools").json()
    assert body["record_outputs"] is True
    assert body["output_retention_days"] == 90


def test_tool_history_settings_roundtrip(client):
    body = client.put(
        "/api/settings/tools",
        json={"record_outputs": False, "output_retention_days": 7},
    ).json()
    assert body["record_outputs"] is False
    assert body["output_retention_days"] == 7
    saved = client.get("/api/settings/tools").json()
    assert saved["record_outputs"] is False
    assert saved["output_retention_days"] == 7


def test_retention_days_rejects_non_number(client):
    resp = client.put("/api/settings/tools", json={"output_retention_days": "很久"})
    assert resp.status_code == 400


def test_retention_days_clamps_out_of_range(client):
    assert client.put(
        "/api/settings/tools", json={"output_retention_days": 99999}
    ).json()["output_retention_days"] == 3650
    assert client.put(
        "/api/settings/tools", json={"output_retention_days": -5}
    ).json()["output_retention_days"] == 0


def test_saving_settings_purges_expired_outputs_immediately(client, db_conn):
    old = record_tool_call(
        db_conn, turn_id="t_old", call_id="c_old", seq=1, tool_name="fs_list",
        arguments={"path": "."}, output="很久以前的输出", status="failed",
        error="找不到路径",
    )
    db_conn.execute(
        "UPDATE tool_records SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (old,)
    )

    body = client.put("/api/settings/tools", json={"output_retention_days": 30}).json()

    assert body["purged"] == 1
    cleared = get_record(db_conn, old)
    assert cleared["output"] == ""
    assert cleared["missing_reason"] == "retention"
    assert cleared["error"] == "找不到路径"      # 失败原因继续保留


def test_retention_zero_keeps_everything(client, db_conn):
    old = record_tool_call(
        db_conn, turn_id="t_old", call_id="c_old", seq=1, tool_name="fs_list",
        arguments={}, output="很久以前的输出", status="success",
    )
    db_conn.execute(
        "UPDATE tool_records SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (old,)
    )
    body = client.put("/api/settings/tools", json={"output_retention_days": 0}).json()
    assert body["purged"] == 0
    assert get_record(db_conn, old)["output"] == "很久以前的输出"


def test_maintenance_prunes_tool_outputs(ctx: AppContext, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(ctx, "prune_tool_outputs", lambda: calls.append(1) or 3)

    result = asyncio.run(ctx.maintenance.run_once())

    assert calls, "维护巡检必须顺带清理过期输出"
    assert result["tool_outputs_purged"] == 3


def test_prune_failure_does_not_break_startup_or_maintenance(ctx: AppContext, monkeypatch):
    def boom():
        raise RuntimeError("数据库锁住了")

    monkeypatch.setattr("agent.storage.tool_records.prune_outputs", boom)
    assert ctx.prune_tool_outputs() == 0      # 不抛，返回值退化为 0
