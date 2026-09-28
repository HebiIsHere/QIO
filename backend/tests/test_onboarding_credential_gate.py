"""首次引导的「已配好模型」判定：必须真的有一把能被主循环选中的凭据。

判定口径（`OnboardingService._usable_credential` → `CredentialPolicy.resolve`）：
启用中、用途含主对话、预算没用完、**并且通过过验证**。任何一条不满足都不算，
否则首次引导会在用户其实用不了模型的情况下宣布「已配置完成」。
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.services.verify import REASON_INVALID_KEY, VerifyResult


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


SECRET = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
BODY = {
    "secret": SECRET,
    "provider": "deepseek",
    "endpoint": "https://api.deepseek.com/v1",
    "default_model": "deepseek-v4-flash",
}


def _status(client) -> dict:
    return client.get("/api/onboarding/status").json()


def test_create_defaults_usage_to_main_loop(client):
    """新建时不选用途 = 主对话（普通用户只需选厂商、填 Key、点保存）。"""
    created = client.post("/api/credentials", json=BODY).json()
    assert created["ok"] is True
    assert created["credential"]["tags"] == ["main-loop"]
    assert created["verify"]["ok"] is True
    assert _status(client)["has_credential"] is True


def test_explicitly_empty_usage_is_refused(client):
    """用户明确提交空用途时给出看得懂的错误，不静默覆盖成主对话。"""
    resp = client.post("/api/credentials", json={**BODY, "tags": []})
    assert resp.status_code == 400
    assert "用途" in resp.json()["detail"]
    assert client.get("/api/credentials").json()["credentials"] == []
    assert _status(client)["has_credential"] is False


def test_unverified_credential_does_not_count(client, fake_verify):
    """保存成功但验证没过：凭据在，但不能算「已配好」，也不会被主循环选中。"""
    fake_verify.set(VerifyResult(False, REASON_INVALID_KEY, "401"))
    created = client.post("/api/credentials", json=BODY).json()
    assert created["saved"] is True
    assert created["verify"]["ok"] is False
    assert created["credential"]["verify_state"] == "failed"
    assert _status(client)["has_credential"] is False

    # 重试成功后就地转正（还是同一条记录）
    fake_verify.set(VerifyResult(True, mode="native"))
    retried = client.post(f"/api/credentials/{created['key_id']}/verify").json()
    assert retried["verify"]["ok"] is True
    assert _status(client)["has_credential"] is True
    assert len(client.get("/api/credentials").json()["credentials"]) == 1


def test_disabled_credential_does_not_count(client):
    created = client.post("/api/credentials", json=BODY).json()
    assert _status(client)["has_credential"] is True

    client.post(f"/api/credentials/{created['key_id']}/disable")
    assert _status(client)["has_credential"] is False


def test_revoked_credential_does_not_count(client):
    created = client.post("/api/credentials", json=BODY).json()
    client.post(f"/api/credentials/{created['key_id']}/revoke")
    assert _status(client)["has_credential"] is False


def test_unverified_special_purpose_credential_does_not_count(client, fake_verify):
    """专项用途的凭据也不能带着「没验证」的状态被当成已配好。

    （专项凭据在**没有**主对话凭据时本来就可以回落给主循环用 —— 这是既有语义，
    见 test_credential_routing.py；但回落的前提同样是「通过过验证」。）
    """
    fake_verify.set(VerifyResult(False, REASON_INVALID_KEY, "401"))
    created = client.post("/api/credentials", json={**BODY, "tags": ["vision"]}).json()
    assert created["credential"]["tags"] == ["vision"]
    assert _status(client)["has_credential"] is False

    fake_verify.set(VerifyResult(True, mode="native"))
    client.post(f"/api/credentials/{created['key_id']}/verify")
    assert _status(client)["has_credential"] is True
