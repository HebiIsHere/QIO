"""用途解析（`services/usage_target.py`）：显式 → 默认模型 → 基线 policy。

这一组用例刻意**不联网、不建 adapter**：解析是纯读，错就要在这里说清楚。
另一半「接了真实调用链」的证据在 tests/test_usage_runtime.py。

数据层（B 的迁移 73：credential_models / model_usages）在本分支可能还没合并，
所以这里的辅助函数按「表不存在就建一份与迁移 73 同结构的表」准备环境：
合并前跑的是 C 的兼容读法，合并后跑的是数据层 `ModelCatalogStore` —— 同一组
断言两边都必须成立（否则就是两条会漂移的实现）。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from agent.credentials.policy import CredentialPolicy
from agent.credentials.store import CredentialStore, MemoryKeyring
from agent.storage.settings import SettingsStore
from agent.services import usage_target as usage_module
from agent.services.usage_target import (
    REASON_BUDGET_EXHAUSTED,
    REASON_CREDENTIAL_DISABLED,
    REASON_CREDENTIAL_REVOKED,
    REASON_CREDENTIAL_UNVERIFIED,
    REASON_EXPLICIT_UNAVAILABLE,
    REASON_MODEL_UNUSABLE,
    REASON_NOT_INHERITED,
    REASON_NO_CALL_PATH,
    REASON_NO_CREDENTIAL,
    SOURCE_DEFAULT,
    SOURCE_EXPLICIT,
    SOURCE_POLICY,
    UsageUnavailable,
    resolve_usage_target,
)

DEFAULT_MODEL_KEY = "model.default"

# 与迁移 73 同结构（合并后 CREATE IF NOT EXISTS 是空操作，这里只给未合并的分支补环境）。
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
def settings_store(db_conn) -> SettingsStore:
    """运行时设置库（`model.default` 就存在这里；与 conftest 的 Settings 不是一回事）。"""
    return SettingsStore(db_conn)


def _catalog_tables(conn: sqlite3.Connection) -> None:
    """准备数据层两张表（迁移 73 已跑过时是空操作）。"""
    conn.execute(_MODEL_CATALOG_DDL)
    conn.execute(_MODEL_USAGES_DDL)
    conn.commit()


def _assign(
    conn: sqlite3.Connection,
    usage_key: str,
    credential_id: str,
    model_id: str,
    *,
    mode: str = "explicit",
) -> None:
    """写一条用途分配（同时补上 credential_models 行：数据层按模型判断可用性）。"""
    _catalog_tables(conn)
    now = "2026-10-28T00:00:00+00:00"
    conn.execute(
        "INSERT INTO credential_models (id, credential_id, model_id, source, capabilities,"
        " verify_state, discovered_at, updated_at)"
        " VALUES (?, ?, ?, 'manual', '[]', 'verified', ?, ?)"
        " ON CONFLICT(credential_id, model_id) DO NOTHING",
        (f"cm_{credential_id}_{model_id}", credential_id, model_id, now, now),
    )
    conn.execute(
        "INSERT INTO model_usages (id, usage_key, credential_id, model_id, mode, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(usage_key) DO UPDATE SET credential_id=excluded.credential_id,"
        " model_id=excluded.model_id, mode=excluded.mode, updated_at=excluded.updated_at",
        (f"mu_{usage_key}", usage_key, credential_id, model_id, mode, now),
    )
    conn.commit()


def _set_default(conn: sqlite3.Connection, settings_store, credential_id: str, model_id: str) -> None:
    """写默认模型。

    同时补一行 credential_models：数据层按「这条连接下有没有记录这个模型」判断可用性，
    只写 settings 会让合并后的数据层把默认模型判成不可用（两套读法结论要一致）。
    """
    _catalog_tables(conn)
    now = "2026-10-28T00:00:00+00:00"
    conn.execute(
        "INSERT INTO credential_models (id, credential_id, model_id, source, capabilities,"
        " verify_state, discovered_at, updated_at)"
        " VALUES (?, ?, ?, 'manual', '[]', 'verified', ?, ?)"
        " ON CONFLICT(credential_id, model_id) DO NOTHING",
        (f"cm_{credential_id}_{model_id}", credential_id, model_id, now, now),
    )
    conn.commit()
    settings_store.set(
        DEFAULT_MODEL_KEY,
        json.dumps({"credential_id": credential_id, "model_id": model_id}),
    )


def _grant(
    store: CredentialStore,
    key_id: str,
    *,
    tags: list[str] | None = None,
    model: str = "gpt-x",
    **kwargs,
) -> None:
    store.create(
        key_id,
        "sk-test-secret",
        tags if tags is not None else ["main-loop"],
        "https://api.example.com/v1",
        model,
        **kwargs,
    )


# -- 老库：没有 model_usages 表 / 没有 default 键 ------------------------------


def test_legacy_db_without_usage_table_falls_back_to_policy(store, settings_store, db_conn):
    """未迁移的老库：没有表、没有默认模型 → 直接走基线 policy，不抛异常。

    合并数据层后迁移 73 会把表建出来，所以这里显式把表去掉，真正测「老库」那条路径。
    """
    db_conn.execute("DROP TABLE IF EXISTS model_usages")
    db_conn.commit()
    _grant(store, "main-key", verify_state="verified")
    target = resolve_usage_target(store, settings_store, "main-chat")
    assert target.source == SOURCE_POLICY
    assert target.credential_id == "main-key"
    assert target.model_id == "gpt-x"
    assert target.degraded is False


def test_legacy_db_without_usage_table_can_still_report_no_credential(store, settings_store):
    with pytest.raises(UsageUnavailable) as excinfo:
        resolve_usage_target(store, settings_store, "main-chat")
    assert excinfo.value.reason_code == REASON_NO_CREDENTIAL


# -- 默认模型层 ----------------------------------------------------------------


def test_default_model_is_used_when_no_explicit_assignment(store, settings_store, db_conn):
    _catalog_tables(db_conn)
    _grant(store, "main-key", model="gpt-old", verify_state="verified")
    _grant(store, "daily-key", model="gpt-daily", verify_state="verified")
    _set_default(db_conn, settings_store, "daily-key", "gpt-daily")

    target = resolve_usage_target(store, settings_store, "main-chat")
    assert target.source == SOURCE_DEFAULT
    assert (target.credential_id, target.model_id) == ("daily-key", "gpt-daily")

    # 后台用途默认继承同一个默认模型
    background = resolve_usage_target(store, settings_store, "memory-organize")
    assert background.source == SOURCE_DEFAULT
    assert background.credential_id == "daily-key"


def test_explicit_assignment_overrides_default(store, settings_store, db_conn):
    _grant(store, "main-key", verify_state="verified")
    _grant(store, "daily-key", model="gpt-daily", verify_state="verified")
    _grant(store, "cheap-key", model="gpt-cheap", verify_state="verified")
    _set_default(db_conn, settings_store, "daily-key", "gpt-daily")
    _assign(db_conn, "memory-organize", "cheap-key", "gpt-cheap")

    target = resolve_usage_target(store, settings_store, "memory-organize")
    assert target.source == SOURCE_EXPLICIT
    assert (target.credential_id, target.model_id) == ("cheap-key", "gpt-cheap")

    # 没被显式指定的用途仍然跟随默认
    main = resolve_usage_target(store, settings_store, "main-chat")
    assert main.source == SOURCE_DEFAULT


def test_clearing_assignment_returns_to_following_default(store, settings_store, db_conn):
    _grant(store, "daily-key", model="gpt-daily", verify_state="verified")
    _set_default(db_conn, settings_store, "daily-key", "gpt-daily")
    _assign(db_conn, "entity-extract", "daily-key", "gpt-daily")
    assert resolve_usage_target(store, settings_store, "entity-extract").source == SOURCE_EXPLICIT

    db_conn.execute("DELETE FROM model_usages WHERE usage_key = 'entity-extract'")
    db_conn.commit()
    assert resolve_usage_target(store, settings_store, "entity-extract").source == SOURCE_DEFAULT


def test_default_model_unavailable_degrades_to_policy_with_reason(
    store, settings_store, db_conn
):
    """默认模型不可用（这里：被停用）→ 回退 policy，但**留痕**，不是静默换 Key。"""
    _grant(store, "main-key", model="gpt-main", verify_state="verified")
    _grant(store, "daily-key", model="gpt-daily", verify_state="verified")
    store.set_enabled("daily-key", False)
    _set_default(db_conn, settings_store, "daily-key", "gpt-daily")

    target = resolve_usage_target(store, settings_store, "main-chat")
    assert target.source == SOURCE_POLICY
    assert target.credential_id == "main-key"
    assert target.degraded is True
    assert target.degrade_reason == REASON_CREDENTIAL_DISABLED
    assert "daily-key" in target.degrade_message


def test_default_model_unavailable_and_no_alternative_raises(
    store, settings_store, db_conn
):
    _grant(store, "daily-key", model="gpt-daily", verify_state="verified")
    store.set_enabled("daily-key", False)
    _set_default(db_conn, settings_store, "daily-key", "gpt-daily")

    with pytest.raises(UsageUnavailable) as excinfo:
        resolve_usage_target(store, settings_store, "main-chat")
    assert excinfo.value.reason_code == REASON_CREDENTIAL_DISABLED
    assert "默认模型不可用" in excinfo.value.message


# -- 显式指定不可用：如实失败，绝不静默换 Key ----------------------------------


@pytest.mark.parametrize(
    "breakage, expected, expected_text",
    [
        ("disabled", REASON_CREDENTIAL_DISABLED, "已停用"),
        ("revoked", REASON_CREDENTIAL_REVOKED, "已撤销"),
        ("unverified", REASON_CREDENTIAL_UNVERIFIED, "还没有通过验证"),
        ("budget", REASON_BUDGET_EXHAUSTED, "用量上限已耗尽"),
    ],
)
def test_explicit_assignment_unavailable_fails_with_reason(
    store, settings_store, db_conn, breakage, expected, expected_text
):
    _grant(store, "pinned-key", model="gpt-pinned", verify_state="verified", budget=1000)
    # 另一把完全可用的 Key：绝不能因为显式指定的那把坏了就偷偷换它。
    _grant(store, "spare-key", model="gpt-spare", verify_state="verified")
    _assign(db_conn, "knowledge-distill", "pinned-key", "gpt-pinned")

    if breakage == "disabled":
        store.set_enabled("pinned-key", False)
    elif breakage == "revoked":
        store.revoke("pinned-key")
    elif breakage == "unverified":
        store.set_verified("pinned-key", False)
    elif breakage == "budget":
        store.record_usage("pinned-key", input_tokens=1500, output_tokens=0)

    with pytest.raises(UsageUnavailable) as excinfo:
        resolve_usage_target(store, settings_store, "knowledge-distill")
    error = excinfo.value
    assert error.reason_code == REASON_EXPLICIT_UNAVAILABLE
    assert "pinned-key" in error.message
    assert error.credential_id == "pinned-key"
    # 具体是哪一项不可用：既有原因码，也有可读原因（界面要能说清）
    assert error.detail_code == expected
    assert expected_text in error.message
    # 没有静默换用另一把可用的 Key
    assert "spare-key" not in error.message


def test_explicit_assignment_to_missing_credential_fails(store, settings_store, db_conn):
    _grant(store, "spare-key", verify_state="verified")
    _assign(db_conn, "entity-extract", "ghost-key", "gpt-x")
    with pytest.raises(UsageUnavailable) as excinfo:
        resolve_usage_target(store, settings_store, "entity-extract")
    assert excinfo.value.reason_code == REASON_EXPLICIT_UNAVAILABLE


def test_explicit_assignment_model_not_in_catalog_fails(store, settings_store, db_conn):
    """显式指定的模型不在该连接下（数据层判定）时也要如实失败，不猜模型名。"""
    _grant(store, "main-key", verify_state="verified")
    _catalog_tables(db_conn)
    _assign(db_conn, "main-chat", "main-key", "gpt-known")
    # 把模型行删掉：数据层会认为这条连接下没有记录这个模型
    db_conn.execute("DELETE FROM credential_models")
    db_conn.commit()
    if usage_module._catalog_store(store) is None:
        pytest.skip("当前分支没有数据层（ModelCatalogStore），该判定由合并后的数据层覆盖")
    with pytest.raises(UsageUnavailable) as excinfo:
        resolve_usage_target(store, settings_store, "main-chat")
    assert excinfo.value.reason_code == REASON_EXPLICIT_UNAVAILABLE


# -- 不继承默认模型的用途 ------------------------------------------------------


@pytest.mark.parametrize("usage_key", ["video", "audio", "embedding"])
def test_non_inheriting_usages_never_take_default_chat_model(
    store, settings_store, db_conn, usage_key
):
    _grant(store, "daily-key", model="gpt-daily", verify_state="verified")
    _set_default(db_conn, settings_store, "daily-key", "gpt-daily")
    with pytest.raises(UsageUnavailable) as excinfo:
        resolve_usage_target(store, settings_store, usage_key)
    assert excinfo.value.reason_code == REASON_NOT_INHERITED


def test_non_inheriting_usage_honours_explicit_assignment(store, settings_store, db_conn):
    _grant(store, "daily-key", model="gpt-daily", verify_state="verified")
    _grant(store, "vision-key", model="gpt-vision", verify_state="verified")
    _assign(db_conn, "vision", "vision-key", "gpt-vision")
    target = resolve_usage_target(store, settings_store, "vision")
    assert target.source == SOURCE_EXPLICIT
    assert target.credential_id == "vision-key"


def test_custom_usage_has_no_execution_path(store, settings_store):
    _grant(store, "main-key", verify_state="verified")
    with pytest.raises(UsageUnavailable) as excinfo:
        resolve_usage_target(store, settings_store, "custom:写作助手")
    assert excinfo.value.reason_code == REASON_NO_CALL_PATH


# -- 主对话 / 通用对话：同一路径，通用对话显式优先 ------------------------------


def test_general_chat_explicit_wins_over_main_chat(store, settings_store, db_conn, policy):
    _grant(store, "main-key", model="gpt-main", verify_state="verified")
    _grant(store, "general-key", model="gpt-general", verify_state="verified")
    _assign(db_conn, "general-chat", "general-key", "gpt-general")
    _assign(db_conn, "main-chat", "main-key", "gpt-main")

    target = usage_module.resolve_shared_target(
        store,
        settings_store,
        ["general-chat", "main-chat"],
        policy=policy,
        primary_key="main-chat",
    )
    assert target.credential_id == "general-key"
    assert target.usage_key == "main-chat"
    assert target.matched_usage_key == "general-chat"


def test_main_chat_policy_fallback_uses_main_loop_candidates(store, settings_store):
    """没有显式/默认时，主对话的 policy 回退必须与基线同一批候选（含 chat/code 标签）。"""
    store.create(
        "chat-key",
        "sk-chat",
        ["chat"],
        "https://api.example.com/v1",
        "gpt-chat",
        verify_state="verified",
    )
    target = resolve_usage_target(store, settings_store, "main-chat")
    assert target.source == SOURCE_POLICY
    assert target.credential_id == "chat-key"


@pytest.fixture()
def policy(store) -> CredentialPolicy:
    return CredentialPolicy(store, register_default_accounting=False)


def test_vision_does_not_inherit_default_chat_model(store, settings_store, db_conn):
    _grant(store, "main-key", model="gpt-main", verify_state="verified")
    store.create(
        "vision-key",
        "sk-vision",
        ["vision"],
        "https://api.example.com/v1",
        "gpt-vision",
        verify_state="verified",
    )
    _set_default(db_conn, settings_store, "main-key", "gpt-main")
    with pytest.raises(UsageUnavailable) as excinfo:
        resolve_usage_target(store, settings_store, "vision")
    assert excinfo.value.reason_code == REASON_NOT_INHERITED
