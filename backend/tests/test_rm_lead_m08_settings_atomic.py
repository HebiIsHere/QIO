"""M08：多字段设置必须先全部校验、再原子保存（Lead 接线的长期回归用例）。

只依据可观察行为断言：HTTP 状态码、GET 返回的生效值、settings 表里的行。
不依赖 settings_service 的任何内部函数名，因此在接线前后都能正常收集。

反例（基线 6e073e9）：
`PUT /api/settings/loop {"max_iterations": 7, "output_token_budget": -1}` 返回 400，
但 `loop.max_iterations` 已经被写成 7——校验失败前已经部分落库。
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _stored(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    return row["value"]


def _put(client: TestClient, path: str, payload: dict) -> "object":
    return client.put(path, json=payload)


# --- 校验失败：整套设置不能变 ------------------------------------------------


def test_loop_invalid_second_field_keeps_first_field_unchanged(client, db_conn):
    before = client.get("/api/settings/loop").json()

    resp = _put(
        client,
        "/api/settings/loop",
        {"max_iterations": 7, "output_token_budget": -1},
    )

    assert resp.status_code == 400
    after = client.get("/api/settings/loop").json()
    assert after == before
    assert _stored(db_conn, "loop.max_iterations") != "7"


def test_memory_invalid_second_field_keeps_first_field_unchanged(client, db_conn):
    before = client.get("/api/settings/memory").json()

    resp = _put(
        client,
        "/api/settings/memory",
        {"fragment_max_tokens": 12345, "fragment_max_turns": 0},
    )

    assert resp.status_code == 400
    after = client.get("/api/settings/memory").json()
    assert after == before
    assert _stored(db_conn, "fragment.max_tokens") != "12345"


def test_maintenance_invalid_second_field_keeps_first_field_unchanged(client, db_conn):
    before = client.get("/api/settings/maintenance").json()

    resp = _put(
        client,
        "/api/settings/maintenance",
        {"enabled": not before["enabled"], "interval_hours": 99999},
    )

    assert resp.status_code == 400
    after = client.get("/api/settings/maintenance").json()
    assert after == before
    assert _stored(db_conn, "maintenance.enabled") != (
        "true" if not before["enabled"] else "false"
    )


def test_tool_history_invalid_second_field_keeps_first_field_unchanged(client, db_conn):
    before = client.get("/api/settings/tools").json()

    resp = _put(
        client,
        "/api/settings/tools",
        {
            "record_outputs": not before["record_outputs"],
            "output_retention_days": 3,
            "record_retention_days": "abc",
        },
    )

    assert resp.status_code == 400
    after = client.get("/api/settings/tools").json()
    assert after["record_outputs"] == before["record_outputs"]
    assert after["output_retention_days"] == before["output_retention_days"]
    assert _stored(db_conn, "tools.output_retention_days") != "3"


def test_search_invalid_second_field_keeps_first_field_unchanged(client, db_conn):
    before = client.get("/api/settings/search").json()

    resp = _put(
        client,
        "/api/settings/search",
        {"searxng_url": "http://127.0.0.1:9999", "top_k_default": "abc"},
    )

    assert resp.status_code == 400
    after = client.get("/api/settings/search").json()
    assert after["searxng_url"] == before["searxng_url"]
    assert _stored(db_conn, "search.searxng_url") != "http://127.0.0.1:9999"


# --- 合法请求：整套一起生效 --------------------------------------------------


def test_loop_valid_multifield_applies_all(client):
    resp = _put(
        client,
        "/api/settings/loop",
        {"max_iterations": 33, "output_token_budget": 4096},
    )

    assert resp.status_code == 200
    body = client.get("/api/settings/loop").json()
    assert body["max_iterations"] == 33
    assert body["output_token_budget"] == 4096


def test_maintenance_valid_multifield_applies_all(client):
    resp = _put(
        client,
        "/api/settings/maintenance",
        {"enabled": False, "interval_hours": 12},
    )

    assert resp.status_code == 200
    body = client.get("/api/settings/maintenance").json()
    assert body["enabled"] is False
    assert body["interval_hours"] == 12


# --- 中途写库失败：整体回滚 --------------------------------------------------


def test_loop_midwrite_failure_rolls_back_first_field(client, db_conn):
    # 先把两个键写成确定值，让后续 upsert 走 UPDATE 分支
    assert (
        _put(
            client,
            "/api/settings/loop",
            {"max_iterations": 11, "output_token_budget": 2048},
        ).status_code
        == 200
    )
    db_conn.execute(
        "CREATE TRIGGER rm_m08_block BEFORE UPDATE ON settings "
        "WHEN NEW.key = 'loop.output_token_budget' "
        "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
    )

    resp = _put(
        client,
        "/api/settings/loop",
        {"max_iterations": 44, "output_token_budget": 4096},
    )

    assert resp.status_code >= 400
    assert _stored(db_conn, "loop.max_iterations") == "11"
    assert _stored(db_conn, "loop.output_token_budget") == "2048"
    assert client.get("/api/settings/loop").json() == {
        "max_iterations": 11,
        "output_token_budget": 2048,
    }


def test_retention_validation_failure_does_not_trigger_cleanup(client, db_conn):
    """保留期限设置校验失败时不得触发清理（清理只能在完整校验之后）。"""
    from agent.storage.tool_records import count_records

    before_count = count_records(db_conn)
    before = client.get("/api/settings/tools").json()

    resp = _put(
        client,
        "/api/settings/tools",
        {"record_outputs": True, "output_retention_days": 1, "record_retention_days": "abc"},
    )

    assert resp.status_code == 400
    after = client.get("/api/settings/tools").json()
    assert after["record_retention_days"] == before["record_retention_days"]
    assert after["output_retention_days"] == before["output_retention_days"]
    assert count_records(db_conn) == before_count
