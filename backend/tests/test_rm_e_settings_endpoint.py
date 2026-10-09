"""E 组 · M08 端点级验收（Lead 已接线，全部为正式用例）。

契约要求设置写入是**整体**的：合法前字段 + 非法后字段 → HTTP 400 且数据库与
运行时都不变。这条语义由 `agent/services/settings_service.py` 提供，
`api/server.py` 的 5 个设置端点 + computer 端点已改为「先全量校验 → 单事务提交
→ 再应用运行时」（`_apply_settings_patch`）。

用例只依据可观察行为（HTTP 状态码 + 后续 GET 的生效值），不依赖接线细节。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings


@pytest.fixture()
def client(tmp_path: Path, db_conn: sqlite3.Connection) -> TestClient:
    app = create_app(Settings(data_dir=tmp_path), db_conn)
    with TestClient(app) as c:
        yield c


def _memory(client: TestClient) -> dict:
    return client.get("/api/settings/memory").json()


def _tools(client: TestClient) -> dict:
    return client.get("/api/settings/tools").json()


def test_valid_pair_takes_effect_as_a_whole(client: TestClient) -> None:
    """合法字段整套生效（接线前后都应为绿：这是不能破的既有语义）。"""
    resp = client.put(
        "/api/settings/memory",
        json={"fragment_max_turns": 6, "fragment_max_tokens": 6000},
    )
    assert resp.status_code == 200, resp.text
    body = _memory(client)
    assert body["fragment_max_turns"] == 6
    assert body["fragment_max_tokens"] == 6000


def test_legal_memory_field_plus_illegal_later_field_changes_nothing(
    client: TestClient,
) -> None:
    before = _memory(client)
    resp = client.put(
        "/api/settings/memory",
        # 前一个字段合法、后一个字段越界：整套都不该生效
        json={"fragment_max_tokens": 5000, "fragment_max_turns": 999},
    )
    assert resp.status_code == 400, resp.text
    assert _memory(client) == before


def test_legal_tools_field_plus_illegal_later_field_changes_nothing(
    client: TestClient,
) -> None:
    before = _tools(client)
    resp = client.put(
        "/api/settings/tools",
        # record_outputs 合法在前，保留期非法在后：整套都不该生效
        json={"record_outputs": not before["record_outputs"], "record_retention_days": "很久"},
    )
    assert resp.status_code == 400, resp.text
    after = _tools(client)
    assert after["record_outputs"] == before["record_outputs"]
    assert after["record_retention_days"] == before["record_retention_days"]
