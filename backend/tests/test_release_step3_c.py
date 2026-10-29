"""发布前第三轮 C：模型获取归属（F09）与 Key 保存重试（F12/F13）的后端语义。

守住的边界（全部 fake，不联网、不用真实 Key）：
* 模型发现是一次带 await 的网络调用：期间连接被改地址 / 协议 / 换 Key 后，
  旧结果不得写进当前模型列表、不得改写获取状态，响应如实标 stale + config_changed；
* 发现缓存必须属于当前配置：换配置后不得把上一份配置的结果当成本次结果；
* 网络异常 / 空列表 / 成功都返回结构化结果（不抛未分类 500），分类可区分；
* 同一个 client_request_id 的同内容重试必须幂等（同一 key_id、只有一条记录）；
  内容不同则明确 409，不静默复用旧记录、不改写它。
"""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.models import MODEL_SOURCE_MANUAL, ModelCatalogStore
from agent.credentials.store import CredentialStore, MemoryKeyring
from agent.services import model_catalog as discovery

DEEPSEEK = "https://api.deepseek.com/v1"
OPENAI = "https://api.openai.com/v1"
MOONSHOT = "https://api.moonshot.cn/v1"
SECRET = "sk-fake-step3-secret-0123456789"
SECRET_NEXT = "sk-fake-step3-secret-next-0123456789"
MODEL = "deepseek-v4-flash"


@pytest.fixture()
def credentials(db_conn: sqlite3.Connection) -> CredentialStore:
    return CredentialStore(db_conn, keyring_backend=MemoryKeyring())


@pytest.fixture()
def store(db_conn: sqlite3.Connection, credentials: CredentialStore) -> ModelCatalogStore:
    return ModelCatalogStore(db_conn, credentials)


@pytest.fixture()
def ctx(db_conn: sqlite3.Connection, credentials: CredentialStore) -> SimpleNamespace:
    return SimpleNamespace(conn=db_conn, credentials=credentials)


def _make(credentials: CredentialStore, key_id: str = "key_a") -> None:
    credentials.create(
        key_id,
        SECRET,
        tags=["main-loop"],
        endpoint=DEEPSEEK,
        default_model=MODEL,
        verify_state="verified",
        note="工作用",
    )


# ---------------------------------------------------------------------------
# F09：发现结果按配置归属校验
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_discarded_when_config_changes_midflight(
    monkeypatch, ctx, credentials, store
):
    _make(credentials)
    store.upsert_model("key_a", "manual-one", source=MODEL_SOURCE_MANUAL)
    store.set_usage("vision", "key_a", "manual-one")
    store.set_default("key_a", MODEL)

    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_fetch(**_kwargs):
        started.set()
        await release.wait()
        return [{"model_id": "stale-model", "display_name": "旧配置的模型"}]

    monkeypatch.setattr(discovery, "fetch_provider_models", fake_fetch)
    task = asyncio.create_task(discovery.discover(ctx, "key_a", refresh=True))
    await asyncio.wait_for(started.wait(), timeout=5)

    # 网络请求在途时用户换地址 + 换 Key（推进版本）
    credentials.reconfigure(
        "key_a",
        secret=SECRET_NEXT,
        endpoint=OPENAI,
        confirm_reconfigure=True,
        default_model="gpt-5.6-luna",
    )
    release.set()
    result = await asyncio.wait_for(task, timeout=5)

    assert result["stale"] is True
    assert result["reason_code"] == discovery.REASON_CONFIG_CHANGED
    assert result["error"] == discovery.REASON_MESSAGES[discovery.REASON_CONFIG_CHANGED]
    # 旧结果没有写进当前模型列表
    model_ids = [m["model_id"] for m in store.list_models("key_a")]
    assert "stale-model" not in model_ids
    assert "manual-one" in model_ids
    # 获取状态没有被旧请求改写（初始就没有检查记录）
    assert store.discovery_state("key_a")["checked_at"] is None
    # 手动模型与用途引用保留
    assert store.get_model("key_a", "manual-one")["source"] == MODEL_SOURCE_MANUAL
    usage = next(u for u in store.list_usages() if u["usage_key"] == "vision")
    assert usage["model_id"] == "manual-one"
    # 默认模型没有被旧请求改动
    assert store.get_default() == {"credential_id": "key_a", "model_id": MODEL}


@pytest.mark.asyncio
async def test_discovery_cache_is_owned_by_current_config(monkeypatch, ctx, credentials, store):
    _make(credentials)
    calls: list[dict] = []

    async def fake_fetch(**kwargs):
        calls.append(kwargs)
        return [{"model_id": "m-current"}]

    monkeypatch.setattr(discovery, "fetch_provider_models", fake_fetch)
    first = await discovery.discover(ctx, "key_a", refresh=True)
    assert first["stale"] is False
    assert [m["model_id"] for m in first["models"]] == ["m-current"]
    assert len(calls) == 1

    # 同一配置下，缓存期内直接读缓存
    cached = await discovery.discover(ctx, "key_a")
    assert cached["source"] == "cache"
    assert len(calls) == 1

    # 换地址/协议（需要重填 Key 并确认）：旧缓存不再属于当前配置
    credentials.reconfigure(
        "key_a",
        secret=SECRET_NEXT,
        endpoint=MOONSHOT,
        kind="openai",
        confirm_reconfigure=True,
        default_model="kimi-k2",
    )

    async def fake_fetch_new(**_kwargs):
        calls.append({"new": True})
        return [{"model_id": "m-new-config"}]

    monkeypatch.setattr(discovery, "fetch_provider_models", fake_fetch_new)
    after = await discovery.discover(ctx, "key_a")
    assert after["source"] == "provider", "换配置后不能把上一份配置的缓存当成当前结果"
    assert after["stale"] is False
    assert "m-new-config" in [m["model_id"] for m in after["models"]]
    # 旧模型行属于历史记录，可以留在本地（不删除手动模型/用途引用），但不能冒充这次结果来源。
    assert after["source"] != "cache"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_discovery_error_paths_are_structured(monkeypatch, ctx, credentials):
    _make(credentials)

    async def failing(**_kwargs):
        raise discovery.DiscoveryError(discovery.REASON_TIMEOUT, "timed out")

    monkeypatch.setattr(discovery, "fetch_provider_models", failing)
    failed = await discovery.discover(ctx, "key_a", refresh=True)
    assert failed["stale"] is False
    assert failed["reason_code"] == discovery.REASON_TIMEOUT
    assert failed["error"]

    async def empty(**_kwargs):
        return []

    monkeypatch.setattr(discovery, "fetch_provider_models", empty)
    no_listing = await discovery.discover(ctx, "key_a", refresh=True)
    assert no_listing["stale"] is False
    assert no_listing["reason_code"] == discovery.REASON_NO_LISTING
    assert no_listing["error"]


# ---------------------------------------------------------------------------
# F13：Key 保存重试的幂等身份
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as test_client:
        yield test_client


def _body(**overrides) -> dict:
    payload = {
        "secret": SECRET,
        "provider": "deepseek",
        "endpoint": DEEPSEEK,
        "default_model": MODEL,
        "client_request_id": "cred-step3-fixed-request-id",
    }
    payload.update(overrides)
    return payload


def _creds(client: TestClient) -> list[dict]:
    return client.get("/api/credentials").json()["credentials"]


def test_same_request_id_same_content_is_idempotent(client):
    first = client.post("/api/credentials", json=_body())
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["idempotent"] is False

    # 客户端丢了应答，用同一个请求身份重试：只有一条连接、同一个标识。
    second = client.post("/api/credentials", json=_body())
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["idempotent"] is True
    assert second_body["key_id"] == first_body["key_id"]
    assert second_body["version"] == first_body["version"]
    assert len(_creds(client)) == 1
    assert _creds(client)[0]["key_id"] == first_body["key_id"]

    # 第三次重试仍然幂等（不会因为重试次数变成新连接）
    third = client.post("/api/credentials", json=_body()).json()
    assert third["idempotent"] is True
    assert third["key_id"] == first_body["key_id"]
    assert len(_creds(client)) == 1


def test_same_request_id_different_content_is_rejected(client):
    first = client.post("/api/credentials", json=_body()).json()
    before = _creds(client)

    # 内容变了（换 Key）：同一个请求身份不能被静默当成同一次提交。
    conflict = client.post(
        "/api/credentials",
        json=_body(secret="sk-other-secret-abcdefghijklmnopqrstuvwxyz", default_model="other-model"),
    )
    assert conflict.status_code == 409
    body = conflict.json()
    assert body["ok"] is False
    assert body["code"] == "client_request_id_conflict"
    assert body["detail"]

    # 原记录没有被改写，也没有新增连接
    after = _creds(client)
    assert len(after) == 1
    assert after[0]["key_id"] == first["key_id"]
    assert after[0]["default_model"] == before[0]["default_model"]
    assert after[0]["endpoint"] == before[0]["endpoint"]
    assert after[0]["version"] == before[0]["version"]
