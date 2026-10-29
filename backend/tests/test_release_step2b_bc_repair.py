# -*- coding: utf-8 -*-
"""返修回归（B）：批量接口必须允许清除既有 custom 历史分配。

验收遗漏：`_assignment_entries` 把所有非预设用途都当「未知用途」拒绝，
于是界面上的「清除历史覆盖」对 `custom:<tag>` 历史行在批量入口直接 400，
用户没法清掉历史覆盖。修复要求：
* 清除（连接与模型都留空）现有 custom 历史分配 → 允许；
* 新增无效分配（不生效用途 / custom 新分配）→ 继续拒绝（422）；
* 整批仍然整体成功或整体失败（不写半个配置）。
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.api.model_routes import register_model_routes
from agent.credentials.models import ModelCatalogStore
from agent.credentials.policy import CredentialPolicy
from agent.credentials.store import CredentialStore, MemoryKeyring
from agent.storage.settings import SettingsStore

DEEPSEEK = "https://api.deepseek.com/v1"
SECRET = "sk-fake-step2b-custom-0123456789abcdef"
KEY_ID = "key_r2b"
MODEL_ID = "model-r2b"
CUSTOM_KEY = "custom:写作助手"


@pytest.fixture()
def credentials(db_conn: sqlite3.Connection) -> CredentialStore:
    return CredentialStore(db_conn, keyring_backend=MemoryKeyring())


@pytest.fixture()
def store(db_conn: sqlite3.Connection, credentials: CredentialStore) -> ModelCatalogStore:
    return ModelCatalogStore(db_conn, credentials)


@pytest.fixture()
def client(db_conn: sqlite3.Connection, credentials: CredentialStore):
    ctx = SimpleNamespace(
        conn=db_conn,
        credentials=credentials,
        settings_store=SettingsStore(db_conn),
        policy=CredentialPolicy(credentials, register_default_accounting=False),
    )
    app = FastAPI()
    register_model_routes(app, ctx)
    with TestClient(app) as test_client:
        yield test_client


def _seed(credentials: CredentialStore, store: ModelCatalogStore) -> None:
    credentials.create(KEY_ID, SECRET, ["main-loop"], DEEPSEEK, MODEL_ID, verify_state="verified")
    store.upsert_model(KEY_ID, MODEL_ID)


def _insert_custom_row(db_conn: sqlite3.Connection, mode: str = "legacy") -> None:
    db_conn.execute(
        "INSERT INTO model_usages (id, usage_key, credential_id, model_id, mode, updated_at)"
        " VALUES (?, ?, ?, ?, ?, '2026-10-29T00:00:00+00:00')"
        " ON CONFLICT(usage_key) DO UPDATE SET credential_id=excluded.credential_id,"
        " model_id=excluded.model_id, mode=excluded.mode",
        ("mu_custom", CUSTOM_KEY, KEY_ID, MODEL_ID, mode),
    )
    db_conn.commit()


def _usage_rows(db_conn: sqlite3.Connection) -> dict[str, dict]:
    rows = db_conn.execute(
        "SELECT usage_key, credential_id, model_id, mode FROM model_usages"
    ).fetchall()
    return {str(row["usage_key"]): dict(row) for row in rows}


def test_batch_clears_existing_custom_historical_assignment(
    client, credentials, store, db_conn
):
    _seed(credentials, store)
    _insert_custom_row(db_conn)

    response = client.post(
        "/api/models/usages",
        json={"assignments": [{"usage_key": CUSTOM_KEY, "credential_id": None, "model_id": None}]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert CUSTOM_KEY not in _usage_rows(db_conn), "清除历史覆盖必须真的删掉那一行"


def test_batch_mixes_clear_and_valid_assignment_atomically(client, credentials, store, db_conn):
    _seed(credentials, store)
    _insert_custom_row(db_conn)

    response = client.post(
        "/api/models/usages",
        json={
            "assignments": [
                {"usage_key": CUSTOM_KEY, "credential_id": None, "model_id": None},
                {"usage_key": "main-chat", "credential_id": KEY_ID, "model_id": MODEL_ID},
            ]
        },
    )
    assert response.status_code == 200, response.text
    rows = _usage_rows(db_conn)
    assert CUSTOM_KEY not in rows
    assert rows["main-chat"]["credential_id"] == KEY_ID
    assert rows["main-chat"]["model_id"] == MODEL_ID


def test_batch_still_rejects_new_custom_assignment(client, credentials, store, db_conn):
    _seed(credentials, store)
    response = client.post(
        "/api/models/usages",
        json={
            "assignments": [
                {"usage_key": CUSTOM_KEY, "credential_id": KEY_ID, "model_id": MODEL_ID}
            ]
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "usage_not_effective", response.text
    assert _usage_rows(db_conn) == {}


def test_batch_keeps_all_or_nothing_when_one_item_invalid(client, credentials, store, db_conn):
    _seed(credentials, store)
    response = client.post(
        "/api/models/usages",
        json={
            "assignments": [
                {"usage_key": "main-chat", "credential_id": KEY_ID, "model_id": MODEL_ID},
                {"usage_key": "video", "credential_id": KEY_ID, "model_id": MODEL_ID},
            ]
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "usage_not_effective", response.text
    assert _usage_rows(db_conn) == {}, "整批失败时不允许留下半个配置"


def test_single_endpoint_also_clears_custom_history(client, credentials, store, db_conn):
    _seed(credentials, store)
    _insert_custom_row(db_conn)
    response = client.post(
        "/api/models/usage",
        json={"usage_key": CUSTOM_KEY, "credential_id": None, "model_id": None},
    )
    assert response.status_code == 200, response.text
    assert CUSTOM_KEY not in _usage_rows(db_conn)
