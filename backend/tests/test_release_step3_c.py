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
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.models import (
    CURRENT_SET_KEY_PREFIX,
    DISCOVERY_KEY_PREFIX,
    MODEL_SOURCE_MANUAL,
    ModelCatalogStore,
)
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

# ---------------------------------------------------------------------------
# 返修：获取归属与顺序 / 当前目录与历史分开
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_old_config_failure_does_not_overwrite_new_config_success(
    monkeypatch, ctx, credentials, store
):
    _make(credentials)
    started = asyncio.Event()
    release_failure = asyncio.Event()
    calls = {"n": 0}

    async def fake_fetch(**_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            started.set()
            await release_failure.wait()
            raise discovery.DiscoveryError(discovery.REASON_TIMEOUT, "timed out")
        return [{"model_id": "new-model"}]

    monkeypatch.setattr(discovery, "fetch_provider_models", fake_fetch)
    old_task = asyncio.create_task(discovery.discover(ctx, "key_a", refresh=True))
    await asyncio.wait_for(started.wait(), timeout=5)

    # 旧配置请求仍在途时连接被重配，新配置的一次获取先成功
    credentials.reconfigure(
        "key_a",
        secret=SECRET_NEXT,
        endpoint=OPENAI,
        confirm_reconfigure=True,
        default_model="gpt-5.6-luna",
    )
    newest = await asyncio.wait_for(discovery.discover(ctx, "key_a", refresh=True), timeout=5)
    assert newest["stale"] is False
    assert [m["model_id"] for m in newest["models"]] == ["new-model"]
    state_after_success = store.discovery_state("key_a")
    assert state_after_success["error"] is None
    assert state_after_success["model_count"] == 1

    # 旧配置的超时此刻才回来：不得把新配置的成功状态改成失败
    release_failure.set()
    old = await asyncio.wait_for(old_task, timeout=5)
    assert old["stale"] is True
    assert old["reason_code"] in {
        discovery.REASON_SUPERSEDED,
        discovery.REASON_CONFIG_CHANGED,
    }
    state_now = store.discovery_state("key_a")
    assert state_now["error"] is None
    assert state_now["model_count"] == 1
    current, _historical = store.current_model_view("key_a")
    assert "new-model" in [item["model_id"] for item in current]


@pytest.mark.asyncio
async def test_older_request_same_config_does_not_overwrite_newer(monkeypatch, ctx, credentials, store):
    _make(credentials)
    started = asyncio.Event()
    release_old = asyncio.Event()
    calls = {"n": 0}

    async def fake_fetch(**_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            started.set()
            await release_old.wait()
            return [{"model_id": "older-model"}]
        return [{"model_id": "newer-model"}]

    monkeypatch.setattr(discovery, "fetch_provider_models", fake_fetch)
    older_task = asyncio.create_task(discovery.discover(ctx, "key_a", refresh=True))
    await asyncio.wait_for(started.wait(), timeout=5)

    newer = await asyncio.wait_for(discovery.discover(ctx, "key_a", refresh=True), timeout=5)
    assert newer["stale"] is False
    assert [m["model_id"] for m in newer["models"]] == ["newer-model"]

    release_old.set()
    older = await asyncio.wait_for(older_task, timeout=5)
    assert older["stale"] is True
    assert older["reason_code"] == discovery.REASON_SUPERSEDED
    # 较早请求的模型没有被写入，当前目录仍是较新一次的结果
    assert store.get_model("key_a", "older-model") is None
    current, _historical = store.current_model_view("key_a")
    assert [item["model_id"] for item in current] == ["newer-model"]
    state = store.discovery_state("key_a")
    assert state["error"] is None
    assert state["model_count"] == 1


@pytest.mark.asyncio
async def test_current_directory_excludes_old_config_models(monkeypatch, ctx, credentials, store):
    _make(credentials)
    store.upsert_model("key_a", "manual-keep", source=MODEL_SOURCE_MANUAL)
    store.set_usage("vision", "key_a", "manual-keep")

    async def fetch_old(**_kwargs):
        return [{"model_id": "old-a"}, {"model_id": "old-b"}]

    monkeypatch.setattr(discovery, "fetch_provider_models", fetch_old)
    first = await discovery.discover(ctx, "key_a", refresh=True)
    assert sorted(m["model_id"] for m in first["models"]) == ["manual-keep", "old-a", "old-b"]
    assert first["historical_total"] == 0

    # 换地址：旧地址的模型继续留在库里（历史），但不再是「当前目录」
    credentials.reconfigure(
        "key_a",
        secret=SECRET_NEXT,
        endpoint=MOONSHOT,
        confirm_reconfigure=True,
        default_model="kimi-k2",
    )

    async def fetch_new(**_kwargs):
        return [{"model_id": "new-only"}]

    monkeypatch.setattr(discovery, "fetch_provider_models", fetch_new)
    refreshed = await discovery.discover(ctx, "key_a", refresh=True)
    current_ids = [m["model_id"] for m in refreshed["models"]]
    historical_ids = [m["model_id"] for m in refreshed["historical_models"]]
    assert sorted(current_ids) == ["manual-keep", "new-only"]
    assert refreshed["total"] == len(current_ids) == 2
    assert sorted(historical_ids) == ["old-a", "old-b"]
    assert refreshed["historical_total"] == 2

    # 缓存返回同一份「当前目录 / 历史」划分
    cached = await discovery.discover(ctx, "key_a")
    assert cached["source"] == "cache"
    assert [m["model_id"] for m in cached["models"]] == current_ids
    assert [m["model_id"] for m in cached["historical_models"]] == historical_ids

    # 重载后（目录快照）与获取结果一致：当前目录只算当前配置，历史单独列出
    catalog = store.catalog()
    connection = next(item for item in catalog["connections"] if item["credential_id"] == "key_a")
    assert [m["model_id"] for m in connection["models"]] == current_ids
    assert [m["model_id"] for m in connection["historical_models"]] == historical_ids

    # 历史行、手动模型与用途引用都保留（不靠删除记录规避）
    all_rows = {item["model_id"] for item in store.list_models("key_a")}
    assert {"old-a", "old-b", "new-only", "manual-keep"} <= all_rows
    usage = next(item for item in store.list_usages() if item["usage_key"] == "vision")
    assert usage["model_id"] == "manual-keep"

@pytest.mark.asyncio
async def test_old_format_without_set_field_uses_explicit_provenance(
    monkeypatch, ctx, credentials, store
):
    """旧格式（没有本次新增集合字段）但发现状态已有来源签名：换配置后必须分开。"""
    _make(credentials)
    store.upsert_model("key_a", "manual-keep", source=MODEL_SOURCE_MANUAL)
    store.set_usage("vision", "key_a", "manual-keep")

    async def fetch_old(**_kwargs):
        return [{"model_id": "old-a"}]

    monkeypatch.setattr(discovery, "fetch_provider_models", fetch_old)
    await discovery.discover(ctx, "key_a", refresh=True)

    # 模拟旧格式记录：删掉集合字段，只保留发现状态里的来源签名。
    store._settings_delete(CURRENT_SET_KEY_PREFIX + "key_a")
    assert store.current_set("key_a")["config_signature"] == ""
    assert store.discovery_state("key_a")["config_signature"]

    # 同一份配置下：按来源签名仍属于当前（旧数据兼容，不误判成历史）。
    current_same, historical_same = store.current_model_view("key_a")
    assert "old-a" in [item["model_id"] for item in current_same]
    assert [item["model_id"] for item in historical_same] == []

    # 换地址 / Key 后：来源签名不同 → 旧模型进入历史，不能一律列为当前。
    credentials.reconfigure(
        "key_a",
        secret=SECRET_NEXT,
        endpoint=MOONSHOT,
        confirm_reconfigure=True,
        default_model="kimi-k2",
    )
    current_old, historical_old = store.current_model_view("key_a")
    assert [item["model_id"] for item in current_old] == ["manual-keep"]
    assert [item["model_id"] for item in historical_old] == ["old-a"]
    catalog = store.catalog()
    connection = next(item for item in catalog["connections"] if item["credential_id"] == "key_a")
    assert [item["model_id"] for item in connection["models"]] == ["manual-keep"]
    assert [item["model_id"] for item in connection["historical_models"]] == ["old-a"]

    # 新地址成功获取后：当前 = 手动 + 新模型；旧模型仍在历史里，引用与行都保留。
    async def fetch_new(**_kwargs):
        return [{"model_id": "new-only"}]

    monkeypatch.setattr(discovery, "fetch_provider_models", fetch_new)
    refreshed = await discovery.discover(ctx, "key_a", refresh=True)
    assert sorted(m["model_id"] for m in refreshed["models"]) == ["manual-keep", "new-only"]
    assert [m["model_id"] for m in refreshed["historical_models"]] == ["old-a"]
    rows = {item["model_id"] for item in store.list_models("key_a")}
    assert {"old-a", "manual-keep", "new-only"} <= rows
    usage = next(item for item in store.list_usages() if item["usage_key"] == "vision")
    assert usage["model_id"] == "manual-keep"

async def _make_old_format_connection(monkeypatch, ctx, credentials, store, old_models):
    """建立「旧格式」记录：没有集合字段，只有发现状态里的成功来源签名。"""
    _make(credentials)
    store.upsert_model("key_a", "manual-keep", source=MODEL_SOURCE_MANUAL)
    store.set_usage("vision", "key_a", "manual-keep")

    async def fetch_old(**_kwargs):
        return [{"model_id": item} for item in old_models]

    monkeypatch.setattr(discovery, "fetch_provider_models", fetch_old)
    await discovery.discover(ctx, "key_a", refresh=True)

    # 模拟旧格式：删掉集合字段与来源签名字段，只留成功状态里的 config_signature。
    store._settings_delete(CURRENT_SET_KEY_PREFIX + "key_a")
    state = store.discovery_state("key_a")
    legacy = {
        "checked_at": state.get("checked_at"),
        "error": None,
        "reason_code": None,
        "source": "provider",
        "provider_id": state.get("provider_id"),
        "model_count": int(state.get("model_count") or 0),
        "config_signature": state.get("config_signature"),
    }
    store._settings_set(DISCOVERY_KEY_PREFIX + "key_a", json.dumps(legacy, ensure_ascii=False))
    credentials.reconfigure(
        "key_a",
        secret=SECRET_NEXT,
        endpoint=MOONSHOT,
        confirm_reconfigure=True,
        default_model="kimi-k2",
    )
    return store.discovery_signature("key_a")


def _assert_old_models_stay_historical(store) -> None:
    current, historical = store.current_model_view("key_a")
    assert [item["model_id"] for item in current] == ["manual-keep"]
    assert [item["model_id"] for item in historical] == ["old-a"]
    catalog = store.catalog()
    connection = next(item for item in catalog["connections"] if item["credential_id"] == "key_a")
    assert [item["model_id"] for item in connection["models"]] == ["manual-keep"]
    assert [item["model_id"] for item in connection["historical_models"]] == ["old-a"]
    rows = {item["model_id"] for item in store.list_models("key_a")}
    assert {"old-a", "manual-keep"} <= rows
    usage = next(item for item in store.list_usages() if item["usage_key"] == "vision")
    assert usage["model_id"] == "manual-keep"


@pytest.mark.asyncio
async def test_old_format_keeps_history_after_new_config_timeout(monkeypatch, ctx, credentials, store):
    new_signature = await _make_old_format_connection(monkeypatch, ctx, credentials, store, ["old-a"])

    async def fetch_timeout(**_kwargs):
        raise discovery.DiscoveryError(discovery.REASON_TIMEOUT, "timed out")

    monkeypatch.setattr(discovery, "fetch_provider_models", fetch_timeout)
    failed = await discovery.discover(ctx, "key_a", refresh=True)
    assert failed["stale"] is False
    assert failed["reason_code"] == discovery.REASON_TIMEOUT
    # 失败尝试不能改写「模型来源」：旧模型仍是历史，而不是重新变成当前。
    assert [m["model_id"] for m in failed["models"]] == ["manual-keep"]
    assert [m["model_id"] for m in failed["historical_models"]] == ["old-a"]
    _assert_old_models_stay_historical(store)

    # 重新读取 / 缓存同样一致（不会因为最近一次失败把旧模型列回当前）
    cached = await discovery.discover(ctx, "key_a")
    assert cached["source"] == "cache"
    assert cached["error"]
    assert [m["model_id"] for m in cached["models"]] == ["manual-keep"]
    assert [m["model_id"] for m in cached["historical_models"]] == ["old-a"]
    _assert_old_models_stay_historical(store)

    # 状态里「最近一次尝试」属于新配置，但「来源」仍是旧配置：两者分开记录。
    state = store.discovery_state("key_a")
    assert state["config_signature"] == new_signature
    assert state["source_signature"] and state["source_signature"] != new_signature


@pytest.mark.asyncio
async def test_old_format_keeps_history_after_new_config_empty_list(monkeypatch, ctx, credentials, store):
    await _make_old_format_connection(monkeypatch, ctx, credentials, store, ["old-a"])

    async def fetch_empty(**_kwargs):
        return []

    monkeypatch.setattr(discovery, "fetch_provider_models", fetch_empty)
    empty = await discovery.discover(ctx, "key_a", refresh=True)
    assert empty["stale"] is False
    assert empty["reason_code"] == discovery.REASON_NO_LISTING
    assert [m["model_id"] for m in empty["models"]] == ["manual-keep"]
    assert [m["model_id"] for m in empty["historical_models"]] == ["old-a"]
    _assert_old_models_stay_historical(store)

    cached = await discovery.discover(ctx, "key_a")
    assert cached["source"] == "cache"
    assert [m["model_id"] for m in cached["models"]] == ["manual-keep"]
    assert [m["model_id"] for m in cached["historical_models"]] == ["old-a"]
    _assert_old_models_stay_historical(store)
