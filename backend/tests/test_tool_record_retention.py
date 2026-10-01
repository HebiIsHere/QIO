"""F4: 工具历史的保留策略 —— 增加能力，但默认行为必须兼容。

现状（缺陷记录）：90 天后只清 output，参数 / 错误 / 状态永久保留，用户既没有
「整条记录保留多少天」的设置，也不能单独删一条或清空历史。

这里新增能力且**默认不变**：
* tools.record_retention_days 默认 0 = 永久保留整条记录（与既有行为一致）；
* 用户主动动作：删除某一条、清空（全部 / 某个话题 / N 天前的）；
* 维护与启动顺手按设置清理（只动用户能回看的 tool_records，不动审计表）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.storage.tool_records import (
    DEFAULT_RECORD_RETENTION_DAYS,
    count_records,
    delete_record,
    delete_records,
    prune_records,
    record_tool_call,
)


def _add(conn, *, turn_id="turn_1", call_id="c1", name="echo", output="正文", topic_id=None):
    return record_tool_call(
        conn,
        turn_id=turn_id,
        topic_id=topic_id,
        call_id=call_id,
        tool_name=name,
        arguments={"text": "参数也在记录里"},
        output=output,
        status="success",
        duration_ms=5,
    )


def _age(conn, record_id: str, days: int) -> None:
    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn.execute("UPDATE tool_records SET created_at = ? WHERE id = ?", (stamp, record_id))


def test_default_is_forever_and_compatible(db_conn: sqlite3.Connection):
    assert DEFAULT_RECORD_RETENTION_DAYS == 0
    rid = _add(db_conn, call_id="old")
    _age(db_conn, rid, 400)
    # 默认（0）不清理任何整行：与「只清输出」的既有行为一致
    assert prune_records(db_conn, 0) == 0
    row = db_conn.execute("SELECT * FROM tool_records WHERE id = ?", (rid,)).fetchone()
    assert row is not None
    assert row["arguments"]
    assert row["output"] == "正文"


def test_record_retention_deletes_whole_rows_only_when_configured(db_conn: sqlite3.Connection):
    old_id = _add(db_conn, call_id="old")
    new_id = _add(db_conn, call_id="new")
    _age(db_conn, old_id, 40)

    assert prune_records(db_conn, 7) == 1
    assert db_conn.execute(
        "SELECT COUNT(*) FROM tool_records WHERE id = ?", (old_id,)
    ).fetchone()[0] == 0
    assert db_conn.execute(
        "SELECT COUNT(*) FROM tool_records WHERE id = ?", (new_id,)
    ).fetchone()[0] == 1


def test_delete_one_and_clear_history(db_conn: sqlite3.Connection):
    a = _add(db_conn, call_id="a", topic_id="topic_x")
    _add(db_conn, call_id="b", topic_id="topic_y")
    assert count_records(db_conn) == 2
    assert count_records(db_conn, topic_id="topic_x") == 1

    assert delete_record(db_conn, a) is True
    assert delete_record(db_conn, "tr_not_exist") is False
    assert count_records(db_conn) == 1

    assert delete_records(db_conn, topic_id="topic_y") == 1
    assert count_records(db_conn) == 0


@pytest.fixture()
def client(tmp_path):
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path), conn)
    with TestClient(app) as c:
        yield c


def test_settings_api_default_is_compatible_and_exposes_count(client):
    ctx = client.app.state.ctx
    body = client.get("/api/settings/tools").json()
    assert body["record_retention_days"] == 0  # 默认永久保留（行为不变）
    assert body["record_count"] == 0

    _add(ctx.conn, call_id="one", output="一条历史")
    body = client.get("/api/settings/tools").json()
    assert body["record_count"] == 1

    updated = client.put("/api/settings/tools", json={"record_retention_days": 3}).json()
    assert updated["record_retention_days"] == 3
    assert "records_purged" in updated
    assert ctx.settings_store.get_int("tools.record_retention_days", -1) == 3


def test_delete_endpoints_and_audit_scope(client):
    ctx = client.app.state.ctx
    ctx.conn.execute(
        "INSERT INTO tool_calls (id, topic_id, tool_name, arguments, result, error, ok, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("tc_1", None, "echo", "{}", "x", "", 1, "2020-01-01T00:00:00+00:00"),
    )
    first = _add(ctx.conn, call_id="one", output="正文一")
    _add(ctx.conn, call_id="two", output="正文二")

    assert client.get(f"/api/tool-records/{first}").status_code == 200
    assert client.delete(f"/api/tool-records/{first}").status_code == 200
    assert client.delete(f"/api/tool-records/{first}").status_code == 404

    cleared = client.delete("/api/tool-records").json()
    assert cleared["deleted"] == 1
    assert cleared["scope"] == "tool_records"
    assert count_records(ctx.conn) == 0
    # 审计表不在这个动作的范围里（诚实说明边界，而不是假装全删干净）
    assert ctx.conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 1


def test_output_retention_still_only_clears_output(client):
    ctx = client.app.state.ctx
    rid = _add(ctx.conn, call_id="old", output="很久以前的正文")
    _age(ctx.conn, rid, 200)
    body = client.put("/api/settings/tools", json={"output_retention_days": 30}).json()
    assert body["purged"] == 1
    row = ctx.conn.execute("SELECT * FROM tool_records WHERE id = ?", (rid,)).fetchone()
    assert row is not None  # 记录本身保留
    assert row["output"] == ""
    assert row["output_missing"] == 1
    assert row["arguments"]  # 参数仍然在（既有的兼容行为）