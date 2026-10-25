"""权限模式：三模式 + 旧值映射 + 只有用户能改 + 展示（契约 §1、§12）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.security.engine import AuthorizationEngine
from agent.security.modes import (
    DEFAULT_MODE,
    LEGACY_MODE_MAP,
    MODE_LABELS,
    PermissionMode,
    is_valid_mode_value,
    label_for,
    legacy_value_for,
    normalize_mode,
)
from agent.storage.settings import SettingsStore


def test_three_modes_and_labels():
    assert [m.value for m in PermissionMode] == ["read_only", "restricted", "unrestricted"]
    assert MODE_LABELS == {
        "read_only": "只读模式",
        "restricted": "受限模式",
        "unrestricted": "完全无限制模式",
    }
    assert DEFAULT_MODE is PermissionMode.RESTRICTED


def test_legacy_values_map_to_new_modes():
    assert LEGACY_MODE_MAP == {
        "default": "restricted",
        "plan": "read_only",
        "accept-edits": "restricted",
        "bypass": "unrestricted",
    }
    assert normalize_mode("plan") is PermissionMode.READ_ONLY
    assert normalize_mode("default") is PermissionMode.RESTRICTED
    assert normalize_mode("accept-edits") is PermissionMode.RESTRICTED
    assert normalize_mode("bypass") is PermissionMode.UNRESTRICTED


def test_unknown_and_missing_values_fall_back_to_restricted():
    """未知值不得静默提高权限（fail-closed）。"""
    for value in (None, "", "hack", 3, object()):
        assert normalize_mode(value) is PermissionMode.RESTRICTED
    # 大小写不敏感（历史设置可能来自不同客户端）
    assert normalize_mode("READ_ONLY") is PermissionMode.READ_ONLY
    assert label_for("nope") == "受限模式"
    assert legacy_value_for("read_only") == "plan"
    assert legacy_value_for("restricted") == "default"
    assert legacy_value_for("unrestricted") == "bypass"


def test_is_valid_mode_value_accepts_new_and_legacy():
    for value in ("read_only", "restricted", "unrestricted", "plan", "default", "accept-edits", "bypass"):
        assert is_valid_mode_value(value)
    for value in ("hack", "", None):
        assert not is_valid_mode_value(value)


def test_engine_reads_legacy_setting_normalized(db_conn, tmp_path):
    store = SettingsStore(db_conn)
    store.set("computer.permission_mode", "plan")  # 旧数据
    engine = AuthorizationEngine(
        conn=db_conn,
        approvals=None,
        settings=store,
        workspace_root=lambda: tmp_path,
        internal_data_paths=lambda: [],
    )
    assert engine.mode() is PermissionMode.READ_ONLY


def test_engine_set_mode_is_user_only_and_persists(db_conn, tmp_path):
    store = SettingsStore(db_conn)
    engine = AuthorizationEngine(
        conn=db_conn,
        approvals=None,
        settings=store,
        workspace_root=lambda: tmp_path,
        internal_data_paths=lambda: [],
    )
    # 模型/工具/子智能体没有调用路径：非用户发起一律拒绝
    with pytest.raises(PermissionError):
        engine.set_mode("unrestricted")
    with pytest.raises(ValueError):
        engine.set_mode("bypass", user_initiated=True)  # 旧值只在读入时映射
    changed = engine.set_mode("unrestricted", user_initiated=True)
    assert changed is PermissionMode.UNRESTRICTED
    assert store.get("computer.permission_mode") == "unrestricted"
    # 新引擎（同一份设置）读到同一模式
    again = AuthorizationEngine(
        conn=db_conn, approvals=None, settings=store,
        workspace_root=lambda: tmp_path, internal_data_paths=lambda: [],
    )
    assert again.mode() is PermissionMode.UNRESTRICTED


@pytest.fixture()
def client(db_conn, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def test_security_mode_api_default_and_display(client):
    body = client.get("/api/security/mode").json()
    assert body["mode"] == "restricted"
    assert body["label"] == "受限模式"
    assert body["legacy_value"] == "default"


def test_security_mode_api_requires_user_confirmation_and_new_values(client):
    # 没有用户确认 -> 400
    assert client.put("/api/security/mode", json={"mode": "unrestricted"}).status_code == 400
    assert (
        client.put(
            "/api/security/mode", json={"mode": "unrestricted", "user_confirmed": False}
        ).status_code
        == 400
    )
    # 旧值不在这个入口接受（只接受新值）
    assert (
        client.put(
            "/api/security/mode", json={"mode": "bypass", "user_confirmed": True}
        ).status_code
        == 400
    )
    # 用户确认的新值 -> 生效且可展示
    resp = client.put(
        "/api/security/mode", json={"mode": "unrestricted", "user_confirmed": True}
    )
    assert resp.status_code == 200
    body = client.get("/api/security/mode").json()
    assert body["mode"] == "unrestricted"
    assert body["label"] == "完全无限制模式"
    assert body["legacy_value"] == "bypass"
    # 落库的是新值（不是旧值）
    row = client.app.state.ctx.conn.execute(
        "SELECT value FROM settings WHERE key = 'computer.permission_mode'"
    ).fetchone()
    assert row["value"] == "unrestricted"


def test_app_context_registers_the_authorization_service(client):
    """生产装配：引擎作为 security 服务注入，审批服务拿到同一引擎。"""
    ctx = client.app.state.ctx
    assert client.app.state.authorization is ctx.authorization
    assert ctx.services.resolve("security") is ctx.authorization
    assert ctx.approvals.engine is ctx.authorization
    assert ctx.approvals.store is not None
    assert ctx.instructions.current() is None
