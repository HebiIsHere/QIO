"""B：模型目录 HTTP 路由（/api/models/*）—— 形状、错误码、不含密钥。

路由模块由 server.py 以 register_model_routes(app, ctx) 挂载；这里用一个只挂了
这组路由的最小 app 验证契约形状，不启动完整应用（避免把 server.py 的长生命周期
牵进来）。全部用 fake，不联网。
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.api.model_routes import register_model_routes
from agent.credentials.models import ModelCatalogStore
from agent.credentials.store import CredentialStore, MemoryKeyring
from agent.services import model_catalog as discovery
from agent.services.verify import VerifyResult

DEEPSEEK = "https://api.deepseek.com/v1"
SECRET = "sk-fake-route-secret-0123456789"


@pytest.fixture()
def credentials(db_conn: sqlite3.Connection) -> CredentialStore:
    return CredentialStore(db_conn, keyring_backend=MemoryKeyring())


@pytest.fixture()
def client(db_conn: sqlite3.Connection, credentials: CredentialStore):
    ctx = SimpleNamespace(conn=db_conn, credentials=credentials)
    app = FastAPI()
    register_model_routes(app, ctx)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def seeded(credentials: CredentialStore) -> ModelCatalogStore:
    credentials.create(
        "key_a",
        SECRET,
        tags=["main-loop"],
        endpoint=DEEPSEEK,
        default_model="deepseek-v4-flash",
        verify_state="verified",
        note="DeepSeek 官方",
    )
    store = ModelCatalogStore(credentials.conn, credentials)
    store.upsert_model("key_a", "deepseek-v4-flash")
    store.upsert_model("key_a", "deepseek-chat", source="manual")
    return store


def test_catalog_returns_connections_usages_default_without_secrets(client, seeded):
    payload = client.get("/api/models/catalog").json()
    assert [item["credential_id"] for item in payload["connections"]] == ["key_a"]
    connection = payload["connections"][0]
    assert connection["name"] == "DeepSeek 官方"
    assert connection["provider_id"] == "deepseek"
    assert connection["endpoint"] == DEEPSEEK
    assert {m["model_id"] for m in connection["models"]} == {
        "deepseek-v4-flash",
        "deepseek-chat",
    }
    assert payload["default"] is None
    usage_keys = {item["usage_key"] for item in payload["usages"]}
    assert {"main-chat", "general-chat", "vision", "video", "embedding"} <= usage_keys
    assert "key_a" in payload["discovery"]
    # 任何响应都不含密钥原文。
    assert SECRET not in str(payload)


def test_set_default_and_usage_roundtrip(client, seeded):
    resp = client.post(
        "/api/models/default", json={"credential_id": "key_a", "model_id": "deepseek-chat"}
    )
    assert resp.status_code == 200
    assert resp.json()["default"] == {"credential_id": "key_a", "model_id": "deepseek-chat"}
    main = {item["usage_key"]: item for item in resp.json()["main_chat"] and [resp.json()["main_chat"]]}
    assert main["main-chat"]["follows_default"] is True

    resp = client.post(
        "/api/models/usage",
        json={"usage_key": "vision", "credential_id": "key_a", "model_id": "deepseek-chat"},
    )
    assert resp.status_code == 200
    vision = next(item for item in resp.json()["usages"] if item["usage_key"] == "vision")
    assert vision["follows_default"] is False
    assert vision["resolved"]["model_id"] == "deepseek-chat"

    # 恢复跟随默认：两者都为空。
    resp = client.post(
        "/api/models/usage",
        json={"usage_key": "vision", "credential_id": None, "model_id": None},
    )
    vision = next(item for item in resp.json()["usages"] if item["usage_key"] == "vision")
    assert vision["follows_default"] is True
    assert vision["model_id"] is None


def test_usage_and_default_validation_errors(client, seeded):
    assert client.post("/api/models/usage", json={}).status_code == 400
    assert (
        client.post(
            "/api/models/usage", json={"usage_key": "main-chat", "credential_id": None, "model_id": "m"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/models/usage",
            json={"usage_key": "main-chat", "credential_id": "missing", "model_id": "m"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/models/default", json={"credential_id": "missing", "model_id": "m"}
        ).status_code
        == 404
    )
    assert (
        client.post("/api/models/default", json={"credential_id": "key_a"}).status_code == 400
    )


def test_discover_endpoint_returns_page_and_failure_classification(
    client, seeded, monkeypatch
):
    async def fake_fetch(**_kwargs):
        return [
            {"model_id": "m1", "display_name": "M1"},
            {"model_id": "m2", "display_name": None},
        ]

    monkeypatch.setattr(discovery, "fetch_provider_models", fake_fetch)
    resp = client.post(
        "/api/models/discover", json={"credential_id": "key_a", "page": 1, "page_size": 1}
    )
    payload = resp.json()
    assert resp.status_code == 200
    assert payload["credential_id"] == "key_a"
    assert payload["source"] == "provider"
    # 种子数据里已有 2 个模型，发现再补 2 个：分页按目录里的全集算。
    assert payload["total"] == 4 and payload["has_more"] is True
    assert payload["page_size"] == 1 and len(payload["models"]) == 1
    assert SECRET not in str(payload)

    async def failing(**_kwargs):
        raise discovery.DiscoveryError(discovery.REASON_RATE_LIMIT, "HTTP 429")

    monkeypatch.setattr(discovery, "fetch_provider_models", failing)
    payload = client.post(
        "/api/models/discover", json={"credential_id": "key_a", "refresh": True}
    ).json()
    assert payload["error"] and payload["reason_code"] == discovery.REASON_RATE_LIMIT

    assert client.post("/api/models/discover", json={}).status_code == 400
    assert (
        client.post("/api/models/discover", json={"credential_id": "missing"}).status_code == 404
    )


def test_verify_endpoint_reports_per_model_result(client, seeded, fake_verify):
    fake_verify.set(VerifyResult(True, mode="native", detail="fake ok"))
    payload = client.post(
        "/api/models/verify", json={"credential_id": "key_a", "model_id": "deepseek-chat"}
    ).json()
    assert payload["ok"] is True
    assert payload["applies_to_current_config"] is True
    assert payload["model_id"] == "deepseek-chat"

    fake_verify.set(VerifyResult(False, "invalid_key", "bad key"))
    payload = client.post(
        "/api/models/verify", json={"credential_id": "key_a", "model_id": "deepseek-v4-flash"}
    ).json()
    assert payload["ok"] is False
    assert payload["state"] == "failed"
    # 单模型失败不影响同 Key 另一模型的记录。
    catalog = client.get("/api/models/catalog").json()
    models = {m["model_id"]: m for m in catalog["connections"][0]["models"]}
    assert models["deepseek-chat"]["usable"] is True
    assert models["deepseek-v4-flash"]["usable"] is False
    assert client.post("/api/models/verify", json={}).status_code == 400
    assert (
        client.post(
            "/api/models/verify", json={"credential_id": "missing", "model_id": "m"}
        ).status_code
        == 404
    )


def test_usage_resolve_and_public_catalog(client, seeded):
    resolved = client.get("/api/models/usage-resolve", params={"usage_key": "main-chat"}).json()
    assert resolved["supported"] is True
    assert resolved["source"] == "legacy_tags", "没有默认模型时回退到基线 tags"
    assert resolved["resolved"]["model_id"] == "deepseek-v4-flash"

    public = client.get("/api/models/catalog/public").json()
    assert "providers" in public and "note" in public
    assert any(item["provider_id"] == "openrouter" for item in public["providers"])
