"""用途分配的 mode 语义：**只有 `mode='explicit'` 是用户显式指定**。

为什么单独一组用例
------------------
迁移 73 回填历史 `credentials.tags` 时写的是 `mode='legacy'`（lead 改的）：历史标签只说明
「这把 Key 以前服务过这个用途」，并不锁定模型。如果把它当成显式指定，老用户换过默认模型后
会被旧标签钉住（「我在设置里改了默认模型，主对话却还用旧 Key」）。

因此：
* `explicit` —— 用户在设置页明确选的那一对 (连接, 模型)；不可用 → 硬失败，不静默替换；
* `legacy` / `inherit` —— 不是显式：按「默认模型 → 基线 policy」解析（能跟随默认模型的用途）；
* 数据层 `ModelCatalogStore._is_explicit` 与 `services/usage_target.is_explicit_assignment`
  必须同口径（本文件同时断言两边）。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from agent.credentials.store import CredentialStore, MemoryKeyring
from agent.services import usage_target as usage_module
from agent.services.usage_target import (
    SOURCE_DEFAULT,
    SOURCE_EXPLICIT,
    SOURCE_POLICY,
    is_explicit_assignment,
    resolve_usage_target,
)

DEFAULT_MODEL_KEY = "model.default"

_MODEL_CATALOG_DDL = """
CREATE TABLE IF NOT EXISTS credential_models (
    id                 TEXT PRIMARY KEY,
    credential_id      TEXT NOT NULL,
    model_id           TEXT NOT NULL,
    display_name       TEXT,
    source             TEXT NOT NULL DEFAULT 'discovered',
    capabilities       TEXT NOT NULL DEFAULT '[]',
    verify_state       TEXT NOT NULL DEFAULT 'unverified',
    verified_at        TEXT,
    verify_error       TEXT,
    verify_fingerprint TEXT,
    context_window     INTEGER,
    discovered_at      TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE (credential_id, model_id)
)
"""

_MODEL_USAGES_DDL = """
CREATE TABLE IF NOT EXISTS model_usages (
    id            TEXT PRIMARY KEY,
    usage_key     TEXT NOT NULL UNIQUE,
    credential_id TEXT,
    model_id      TEXT,
    mode          TEXT NOT NULL DEFAULT 'inherit',
    updated_at    TEXT NOT NULL
)
"""


@pytest.fixture()
def store(db_conn) -> CredentialStore:
    return CredentialStore(db_conn, keyring_backend=MemoryKeyring())


@pytest.fixture()
def settings_store(db_conn):
    from agent.storage.settings import SettingsStore

    return SettingsStore(db_conn)


def _tables(conn: sqlite3.Connection) -> None:
    conn.execute(_MODEL_CATALOG_DDL)
    conn.execute(_MODEL_USAGES_DDL)
    conn.commit()


def _model_row(conn: sqlite3.Connection, credential_id: str, model_id: str) -> None:
    now = "2026-10-28T00:00:00+00:00"
    conn.execute(
        "INSERT INTO credential_models (id, credential_id, model_id, source, capabilities,"
        " verify_state, discovered_at, updated_at)"
        " VALUES (?, ?, ?, 'legacy', '[]', 'legacy', ?, ?)"
        " ON CONFLICT(credential_id, model_id) DO NOTHING",
        (f"cm_{credential_id}_{model_id}", credential_id, model_id, now, now),
    )
    conn.commit()


def _usage_row(
    conn: sqlite3.Connection,
    usage_key: str,
    credential_id: str,
    model_id: str,
    *,
    mode: str,
) -> None:
    _tables(conn)
    _model_row(conn, credential_id, model_id)
    now = "2026-10-28T00:00:00+00:00"
    conn.execute(
        "INSERT INTO model_usages (id, usage_key, credential_id, model_id, mode, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(usage_key) DO UPDATE SET credential_id=excluded.credential_id,"
        " model_id=excluded.model_id, mode=excluded.mode, updated_at=excluded.updated_at",
        (f"mu_{usage_key}", usage_key, credential_id, model_id, mode, now),
    )
    conn.commit()


def _grant(store: CredentialStore, key_id: str, *, tags=None, model="gpt-x", state="verified") -> None:
    store.create(
        key_id,
        "sk-test-secret",
        list(tags or ["main-loop"]),
        "https://api.example.com/v1",
        model,
        verify_state=state,
    )


def _set_default(conn: sqlite3.Connection, settings_store, credential_id: str, model_id: str) -> None:
    _tables(conn)
    _model_row(conn, credential_id, model_id)
    settings_store.set(
        DEFAULT_MODEL_KEY,
        json.dumps({"credential_id": credential_id, "model_id": model_id}),
    )


# -- mode 判定 -----------------------------------------------------------------


@pytest.mark.parametrize("mode", ["legacy", "inherit", "", "LEGACY"])
def test_only_explicit_mode_counts_as_explicit(mode):
    row = {"credential_id": "k", "model_id": "m", "mode": mode}
    assert is_explicit_assignment(row) is False


def test_explicit_mode_counts_as_explicit():
    assert is_explicit_assignment({"credential_id": "k", "model_id": "m", "mode": "explicit"}) is True


def test_missing_mode_column_falls_back_to_model_id_presence():
    """极简表（没有 mode 列）只有「写了模型」这一种可能，仍按显式处理。"""
    assert is_explicit_assignment({"credential_id": "k", "model_id": "m"}) is True
    assert is_explicit_assignment({"credential_id": "k", "model_id": None}) is False


# -- legacy 行不锁定模型：仍然按「默认模型 → policy」解析 ----------------------


def test_legacy_usage_row_follows_default_model(store, settings_store, db_conn):
    """迁移回填的 legacy 行不能顶替默认模型（老用户改了默认模型就必须跟着变）。"""
    _grant(store, "old-key", tags=["main-loop"], model="gpt-old")
    _grant(store, "daily-key", model="gpt-daily")
    _usage_row(db_conn, "main-chat", "old-key", "gpt-old", mode="legacy")
    _set_default(db_conn, settings_store, "daily-key", "gpt-daily")

    target = resolve_usage_target(store, settings_store, "main-chat")
    assert target.source == SOURCE_DEFAULT
    assert (target.credential_id, target.model_id) == ("daily-key", "gpt-daily")


def test_legacy_usage_row_falls_back_to_policy_without_default(store, settings_store, db_conn):
    _grant(store, "old-key", tags=["main-loop"], model="gpt-old")
    _usage_row(db_conn, "main-chat", "old-key", "gpt-old", mode="legacy")

    target = resolve_usage_target(store, settings_store, "main-chat")
    assert target.source == SOURCE_POLICY
    assert target.credential_id == "old-key"


def test_explicit_row_wins_over_default(store, settings_store, db_conn):
    _grant(store, "pinned-key", model="gpt-pinned")
    _grant(store, "daily-key", model="gpt-daily")
    _usage_row(db_conn, "knowledge-distill", "pinned-key", "gpt-pinned", mode="explicit")
    _set_default(db_conn, settings_store, "daily-key", "gpt-daily")

    target = resolve_usage_target(store, settings_store, "knowledge-distill")
    assert target.source == SOURCE_EXPLICIT
    assert (target.credential_id, target.model_id) == ("pinned-key", "gpt-pinned")


def test_legacy_row_pointing_to_disabled_key_still_uses_policy(store, settings_store, db_conn):
    """legacy 行指向的旧 Key 停用了：不是「显式指定不可用」，而是按基线另选一把。"""
    _grant(store, "old-key", tags=["main-loop"], model="gpt-old")
    _grant(store, "other-key", tags=["main-loop"], model="gpt-other")
    store.set_enabled("old-key", False)
    _usage_row(db_conn, "memory-organize", "old-key", "gpt-old", mode="legacy")

    target = resolve_usage_target(store, settings_store, "memory-organize")
    assert target.source in (SOURCE_POLICY, SOURCE_DEFAULT)
    assert target.credential_id == "other-key"


# -- 与数据层判定同口径（数据层合入后自动生效） --------------------------------


def test_matches_data_layer_judgement_when_available(store, db_conn):
    """数据层的 _is_explicit 与本模块的 is_explicit_assignment 必须给出同一结论。"""
    catalog = usage_module._catalog_store(store)
    if catalog is None:
        pytest.skip("当前分支没有数据层（ModelCatalogStore），合并后本用例自动生效")

    _tables(db_conn)
    for mode, expected in (("explicit", True), ("legacy", False), ("inherit", False)):
        _usage_row(db_conn, "main-chat", "k-x", "m-x", mode=mode)
        row = {"credential_id": "k-x", "model_id": "m-x", "mode": mode}
        assert is_explicit_assignment(row) is expected
        data_layer = catalog.get_usage("main-chat")
        assert (str(data_layer.get("mode")) == "explicit") is expected
