"""保存 / 验证 / 默认项 / 重复提交 —— 凭据体验的四条主路径（全部用 fake provider）。

规格要点：

- 「保存成功」与「验证通过」是两件事，必须分别反馈；
- 写入失败绝不能留下半条记录，也不能回「成功」；
- 新建但没通过验证的凭据不能被自动选中，也不能让首次引导误判为已配置；
- 超时重试 / 重试验证都作用在同一条记录上，不重复创建；
- 验证只打用户选定的那个地址。
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.services.verify import (
    REASON_INVALID_KEY,
    REASON_UNREACHABLE,
    VerifyResult,
)

SECRET = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
MODEL = "deepseek-v4-flash"
ENDPOINT = "https://api.deepseek.com/v1"


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _body(**overrides) -> dict:
    payload = {
        "secret": SECRET,
        "provider": "deepseek",
        "endpoint": ENDPOINT,
        "default_model": MODEL,
    }
    payload.update(overrides)
    return payload


def _creds(client) -> list[dict]:
    return client.get("/api/credentials").json()["credentials"]


def test_save_reports_saved_and_verified_separately(client):
    resp = client.post("/api/credentials", json=_body()).json()
    assert resp["saved"] is True
    assert resp["verify"]["ok"] is True
    assert resp["verify"]["state"] == "verified"
    assert resp["credential"]["verify_state"] == "verified"
    # 第一条验证可用的主对话凭据自动成为默认
    assert resp["credential"]["is_default"] is True
    assert client.get("/api/credentials").json()["default_key_id"] == resp["key_id"]


def test_saved_but_unverified_is_reported_honestly(client, fake_verify):
    fake_verify.set(VerifyResult(False, REASON_UNREACHABLE, "connect error"))
    resp = client.post("/api/credentials", json=_body()).json()
    assert resp["saved"] is True, "写入是成功的"
    assert resp["verify"]["ok"] is False
    assert resp["verify"]["reason_code"] == REASON_UNREACHABLE
    # 网络类失败如实记为「上次没通过」，但不会被当成「这把钥匙被服务拒绝」
    assert resp["verify"]["state"] == "failed"
    assert resp["credential"]["verify_state"] == "failed"
    assert client.get("/api/credentials").json()["default_key_id"] is None


def test_second_credential_does_not_steal_the_default(client):
    first = client.post("/api/credentials", json=_body()).json()
    second = client.post(
        "/api/credentials",
        json=_body(endpoint="https://api.moonshot.cn/v1", default_model="kimi-k2"),
    ).json()
    assert first["key_id"] != second["key_id"]
    assert client.get("/api/credentials").json()["default_key_id"] == first["key_id"]
    assert second["credential"]["is_default"] is False


def test_repeated_save_with_same_request_id_writes_once(client):
    """请求超时后用户又点了一次「保存」：不能留下两条凭据。"""
    body = _body(client_request_id="3b7c0f6e-2b0f-4b5e-9a3a-1f0d2c4b5a60")
    first = client.post("/api/credentials", json=body).json()
    second = client.post("/api/credentials", json=body).json()
    assert first["key_id"] == second["key_id"]
    assert second["idempotent"] is True
    assert len(_creds(client)) == 1


def test_keyring_failure_leaves_nothing_behind(client):
    class _WriteFails:
        def get_password(self, service, username):  # noqa: ANN001, ANN201
            return None

        def set_password(self, service, username, password):  # noqa: ANN001, ANN201
            raise RuntimeError("no usable OS credential backend")

        def delete_password(self, service, username):  # noqa: ANN001, ANN201
            raise RuntimeError("no usable OS credential backend")

    client.app.state.ctx.credentials._kr = _WriteFails()
    resp = client.post("/api/credentials", json=_body())
    assert resp.status_code == 500
    assert "保存失败" in resp.json()["detail"]
    assert _creds(client) == []
    assert client.get("/api/onboarding/status").json()["has_credential"] is False


def test_verification_only_touches_the_selected_endpoint(client, fake_verify):
    client.post(
        "/api/credentials",
        json=_body(endpoint="https://api.moonshot.cn/v1", default_model="kimi-k2"),
    )
    assert len(fake_verify.calls) == 1
    call = fake_verify.calls[0]
    assert call["endpoint"] == "https://api.moonshot.cn/v1"
    assert call["model"] == "kimi-k2"
    assert call["secret"] == SECRET


def test_verify_draft_writes_nothing(client, fake_verify):
    resp = client.post(
        "/api/credentials/verify-draft",
        json={"secret": SECRET, "endpoint": ENDPOINT, "default_model": MODEL, "kind": "openai"},
    ).json()
    assert resp["ok"] is True
    assert fake_verify.calls[-1]["endpoint"] == ENDPOINT
    assert _creds(client) == []
    assert client.get("/api/onboarding/status").json()["has_credential"] is False


def test_retry_verification_reuses_the_same_record(client, fake_verify):
    fake_verify.set(VerifyResult(False, REASON_INVALID_KEY, "401"))
    created = client.post("/api/credentials", json=_body()).json()
    fake_verify.set(VerifyResult(True, mode="native"))
    client.post(f"/api/credentials/{created['key_id']}/verify")
    assert len(_creds(client)) == 1
    assert client.get("/api/credentials").json()["default_key_id"] == created["key_id"]


def test_set_default_validates_usage_enabled_and_verification(client, fake_verify):
    first = client.post("/api/credentials", json=_body()).json()
    fake_verify.set(VerifyResult(False, REASON_INVALID_KEY, "401"))
    second = client.post(
        "/api/credentials",
        json=_body(endpoint="https://api.moonshot.cn/v1", default_model="kimi-k2"),
    ).json()

    # 没通过验证 → 不能设为默认
    denied = client.post(f"/api/credentials/{second['key_id']}/default")
    assert denied.status_code == 400
    assert "验证" in denied.json()["detail"]

    # 通过验证后可以设为默认，且只影响排序
    fake_verify.set(VerifyResult(True, mode="native"))
    client.post(f"/api/credentials/{second['key_id']}/verify")
    ok = client.post(f"/api/credentials/{second['key_id']}/default")
    assert ok.status_code == 200
    assert client.get("/api/credentials").json()["default_key_id"] == second["key_id"]
    assert first["key_id"] != second["key_id"]

    # 停用之后不能再设为默认
    client.post(f"/api/credentials/{second['key_id']}/disable")
    assert client.post(f"/api/credentials/{second['key_id']}/default").status_code == 400


def test_set_default_rejects_non_main_loop_usage(client):
    created = client.post("/api/credentials", json=_body(tags=["vision"])).json()
    resp = client.post(f"/api/credentials/{created['key_id']}/default")
    assert resp.status_code == 400
    assert "主对话" in resp.json()["detail"]


def test_custom_provider_requires_address_and_model(client):
    missing_endpoint = client.post(
        "/api/credentials", json={"secret": SECRET, "provider": "custom", "default_model": MODEL}
    )
    assert missing_endpoint.status_code == 400
    assert "服务地址" in missing_endpoint.json()["detail"]

    missing_model = client.post(
        "/api/credentials", json={"secret": SECRET, "provider": "custom", "endpoint": ENDPOINT}
    )
    assert missing_model.status_code == 400
    assert "模型" in missing_model.json()["detail"]
    assert _creds(client) == []


def test_create_fills_endpoint_and_model_from_provider_preset(client):
    created = client.post(
        "/api/credentials", json={"secret": SECRET, "provider": "kimi"}
    ).json()
    assert created["credential"]["endpoint"] == "https://api.moonshot.cn/v1"
    assert created["credential"]["default_model"] == "kimi-k2"
    assert created["credential"]["tags"] == ["main-loop"]


def test_providers_endpoint_is_the_single_source(client):
    payload = client.get("/api/credentials/providers").json()
    ids = [p["id"] for p in payload["providers"]]
    assert "deepseek" in ids and "custom" in ids
    custom = next(p for p in payload["providers"] if p["id"] == "custom")
    assert custom["category"] == "custom"
    assert "推荐值" in payload["model_note"]
    # 不再有「自动识别」入口：那会把同一把 Key 发给用户没选过的厂商
    assert client.post("/api/credentials/identify", json={"secret": SECRET}).status_code in (404, 405)


def test_switching_key_resets_and_rechecks_verification(client, fake_verify):
    created = client.post("/api/credentials", json=_body()).json()
    fake_verify.set(VerifyResult(False, REASON_INVALID_KEY, "401"))
    resp = client.patch(
        f"/api/credentials/{created['key_id']}",
        json={"secret": "sk-brand-new-key-value"},
    ).json()
    assert resp["verify"]["ok"] is False
    assert resp["credential"]["verify_state"] == "failed"
    # 换钥失败：记录还在，但不会被自动选中；旧密钥仍然保留在库里由用户决定
    assert len(_creds(client)) == 1

    fake_verify.set(VerifyResult(True, mode="native"))
    resp = client.patch(
        f"/api/credentials/{created['key_id']}",
        json={"secret": "sk-another-key-value"},
    ).json()
    assert resp["verify"]["ok"] is True
    assert resp["credential"]["verify_state"] == "verified"


def test_editing_one_field_keeps_the_others(client):
    created = client.post("/api/credentials", json=_body(note="我的主力 Key")).json()
    updated = client.patch(
        f"/api/credentials/{created['key_id']}", json={"note": "改名了"}
    ).json()["credential"]
    assert updated["note"] == "改名了"
    assert updated["tags"] == ["main-loop"]
    assert updated["endpoint"] == ENDPOINT
    assert updated["default_model"] == MODEL


def test_clearing_optional_field_really_clears_it(client):
    created = client.post("/api/credentials", json=_body(note="有备注", budget=500)).json()
    updated = client.patch(
        f"/api/credentials/{created['key_id']}", json={"note": None, "budget": None}
    ).json()["credential"]
    assert updated["note"] is None
    assert updated["budget"] is None
