"""B：模型目录 / 用途分配 / 默认模型 / 按模型验证（全部 fake，不联网）。

覆盖任务书 §8 要求：模型目录 CRUD、发现分页 / 缓存 / 错误分类、用途分配默认继承
与显式覆盖与恢复跟随默认、同 Key 多模型、同名模型跨 Key 区分、按模型验证失效规则、
迟到结果、删除引用保护。
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from agent.credentials.models import (
    DEFAULT_MODEL_KEY,
    MODEL_SOURCE_MANUAL,
    ModelCatalogStore,
    compute_model_fingerprint,
    usage_kind,
)
from agent.credentials.providers import find_preset, list_presets
from agent.credentials.store import CredentialStore, MemoryKeyring
from agent.services import model_catalog as discovery
from agent.services.verify import VerifyResult

DEEPSEEK = "https://api.deepseek.com/v1"


@pytest.fixture()
def credentials(db_conn: sqlite3.Connection) -> CredentialStore:
    return CredentialStore(db_conn, keyring_backend=MemoryKeyring())


@pytest.fixture()
def store(db_conn: sqlite3.Connection, credentials: CredentialStore) -> ModelCatalogStore:
    return ModelCatalogStore(db_conn, credentials)


@pytest.fixture()
def ctx(db_conn: sqlite3.Connection, credentials: CredentialStore) -> SimpleNamespace:
    return SimpleNamespace(conn=db_conn, credentials=credentials)


def make(
    credentials: CredentialStore,
    key_id: str = "key_a",
    *,
    tags: list[str] | None = None,
    endpoint: str = DEEPSEEK,
    model: str = "deepseek-v4-flash",
    **kwargs,
) -> None:
    credentials.create(
        key_id,
        f"sk-fake-{key_id}",
        tags=tags or ["main-loop"],
        endpoint=endpoint,
        default_model=model,
        verify_state="verified",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 模型目录 CRUD / 按连接分组 / 同名模型跨 Key 区分
# ---------------------------------------------------------------------------


def test_same_model_name_on_two_connections_is_distinguished(store, credentials):
    make(credentials, "key_a")
    make(credentials, "key_b", endpoint="https://api.openai.com/v1", model="gpt-5.6-luna")
    store.upsert_model("key_a", "shared-model", source=MODEL_SOURCE_MANUAL)
    store.upsert_model("key_b", "shared-model", source=MODEL_SOURCE_MANUAL)

    catalog = store.catalog()
    assert len(catalog["connections"]) == 2
    by_id = {item["credential_id"]: item for item in catalog["connections"]}
    assert by_id["key_a"]["provider_id"] == "deepseek"
    assert by_id["key_b"]["provider_id"] == "openai"
    # 同名模型分属两条连接：各自一条记录，互不覆盖。
    assert [m["model_id"] for m in by_id["key_a"]["models"]] == ["shared-model"]
    assert [m["model_id"] for m in by_id["key_b"]["models"]] == ["shared-model"]
    assert store.get_model("key_a", "shared-model")["capabilities"] == []


def test_discovery_upsert_keeps_manual_models(store, credentials):
    make(credentials)
    store.upsert_model("key_a", "manual-model", source=MODEL_SOURCE_MANUAL)
    result = store.upsert_discovered(
        "key_a",
        [
            {"model_id": "discovered-1", "display_name": "Discovered One"},
            {"model_id": "manual-model"},
        ],
    )
    assert result == {"added": 1, "updated": 1, "total": 2}
    models = {item["model_id"]: item for item in store.list_models("key_a")}
    assert models["manual-model"]["source"] == MODEL_SOURCE_MANUAL
    assert models["discovered-1"]["source"] == "discovered"
    assert models["discovered-1"]["display_name"] == "Discovered One"


def test_delete_model_is_blocked_while_referenced_by_usage(store, credentials):
    make(credentials)
    store.upsert_model("key_a", "model-x")
    store.set_usage("vision", "key_a", "model-x")
    with pytest.raises(ValueError):
        store.delete_model("key_a", "model-x")
    store.clear_usage("vision")
    assert store.delete_model("key_a", "model-x") is True
    assert store.get_model("key_a", "model-x") is None


# ---------------------------------------------------------------------------
# 默认模型 / 用途分配
# ---------------------------------------------------------------------------


def test_default_model_roundtrip(store, credentials):
    make(credentials)
    assert store.get_default() is None
    assert store.set_default("key_a", "deepseek-v4-flash") == {
        "credential_id": "key_a",
        "model_id": "deepseek-v4-flash",
    }
    assert store.get_default() == {"credential_id": "key_a", "model_id": "deepseek-v4-flash"}
    # 手动指定的模型名（高级入口）会补一条 manual 记录，而不是拒绝。
    store.set_default("key_a", "typed-model")
    assert store.get_model("key_a", "typed-model")["source"] == MODEL_SOURCE_MANUAL
    store.clear_default()
    assert store.get_default() is None
    with pytest.raises(KeyError):
        store.set_default("missing-key", "m")


def test_usage_follows_default_then_explicit_override_then_restore(store, credentials):
    make(credentials, "key_a")
    make(credentials, "key_b", endpoint="https://api.openai.com/v1", model="gpt-5.6-luna")
    store.upsert_model("key_a", "model-a")
    store.upsert_model("key_b", "model-b")
    store.set_default("key_a", "model-a")

    main = {item["usage_key"]: item for item in store.list_usages()}["main-chat"]
    assert main["follows_default"] is True
    assert main["resolved"]["model_id"] == "model-a"
    assert main["resolved"]["credential_id"] == "key_a"

    store.set_usage("main-chat", "key_b", "model-b")
    main = {item["usage_key"]: item for item in store.list_usages()}["main-chat"]
    assert main["follows_default"] is False
    assert main["model_id"] == "model-b"
    assert main["resolved"]["model_id"] == "model-b"

    # 换默认模型不覆盖显式指定。
    store.set_default("key_a", "deepseek-v4-flash")
    main = {item["usage_key"]: item for item in store.list_usages()}["main-chat"]
    assert main["resolved"]["model_id"] == "model-b", "显式分配必须保留"

    # 恢复跟随默认：两者留空 = 删掉覆盖行。
    store.clear_usage("main-chat")
    main = {item["usage_key"]: item for item in store.list_usages()}["main-chat"]
    assert main["follows_default"] is True
    assert main["resolved"]["model_id"] == "deepseek-v4-flash"


def test_usage_requires_credential_when_model_given(store, credentials):
    make(credentials)
    with pytest.raises(ValueError):
        store.set_usage("vision", None, "model-x")
    with pytest.raises(ValueError):
        store.set_usage("vision", "key_a", None)
    with pytest.raises(ValueError):
        store.set_usage("not-a-usage", "key_a", "model-x")


def test_non_inheriting_usages_do_not_borrow_default_chat_model(store, credentials):
    make(credentials)
    store.upsert_model("key_a", "chat-model")
    store.set_default("key_a", "chat-model")
    usages = {item["usage_key"]: item for item in store.list_usages()}
    for key in ("video", "audio", "embedding"):
        # 不继承默认对话模型：没有显式指定就没有「当前目标」，也不假装跟随默认。
        assert usages[key]["follows_default"] is False
        assert usages[key]["resolved"] is None
        assert usages[key]["assignment_effective"] is False
        assert "沿用现有处理方式" in usages[key]["note"]
        assert store.resolve_usage(key) is None, "不继承默认对话模型"
    # 显式指定同样不会生效：这些用途没有独立调用路径，保存时就会被拒（B）。
    with pytest.raises(ValueError):
        store.set_usage("video", "key_a", "chat-model")
    assert usage_kind("video") == "video"
    assert usage_kind("custom:写作") == "custom"


def test_usage_reference_protection_lists_usage_labels(store, credentials):
    make(credentials)
    store.upsert_model("key_a", "model-x")
    store.set_usage("vision", "key_a", "model-x")
    store.set_usage("main-chat", "key_a", "model-x")
    refs = store.references_to_credential("key_a")
    labels = {item["label"] for item in refs}
    assert {"看图 / 视觉", "主对话"} <= labels


# ---------------------------------------------------------------------------
# 可用性：停用 / 撤销 / 过期 / 预算耗尽 / 验证失败 —— 都要说出原因
# ---------------------------------------------------------------------------


def test_unusable_reasons_are_explicit(store, credentials, db_conn):
    make(credentials, "key_a")
    store.upsert_model("key_a", "model-x")

    credentials.set_enabled("key_a", False)
    model = store.get_model("key_a", "model-x")
    assert model["usable"] is False and "停用" in model["unavailable_reason"]

    credentials.set_enabled("key_a", True)
    db_conn.execute("UPDATE credentials SET status = 'expired' WHERE id = 'key_a'")
    assert "过期" in store.get_model("key_a", "model-x")["unavailable_reason"]

    db_conn.execute("UPDATE credentials SET status = 'revoked' WHERE id = 'key_a'")
    assert "撤销" in store.get_model("key_a", "model-x")["unavailable_reason"]

    db_conn.execute(
        "UPDATE credentials SET status = 'active', budget = 100, budget_used = 100 WHERE id = 'key_a'"
    )
    assert "上限" in store.get_model("key_a", "model-x")["unavailable_reason"]

    db_conn.execute("UPDATE credentials SET budget = NULL, budget_used = 0 WHERE id = 'key_a'")
    assert store.get_model("key_a", "model-x")["usable"] is True


def test_verify_failure_only_affects_one_model(store, credentials):
    make(credentials)
    for model_id in ("model-a", "model-b"):
        store.upsert_model("key_a", model_id)
    store.record_verification("key_a", "model-a", ok=False, reason_code="invalid_key")
    assert store.get_model("key_a", "model-a")["usable"] is False
    assert store.get_model("key_a", "model-a")["unavailable_reason"].startswith("这个模型")
    assert store.get_model("key_a", "model-b")["usable"] is True, "同 Key 其它模型不受影响"
    # Key 级验证状态不被模型级失败改写。
    assert credentials.get_metadata("key_a")["verify_state"] == "verified"


# ---------------------------------------------------------------------------
# 按模型验证：指纹 / 迟到结果 / 临时故障
# ---------------------------------------------------------------------------


def test_model_fingerprint_tracks_current_config(store, credentials):
    make(credentials)
    store.upsert_model("key_a", "model-x")
    outcome = store.record_verification("key_a", "model-x", ok=True)
    assert outcome["applied"] is True
    model = store.get_model("key_a", "model-x")
    assert model["verify_state"] == "verified"
    assert model["verify_applies"] is True
    expected = compute_model_fingerprint(
        version=1, endpoint=DEEPSEEK, kind="openai", model_id="model-x"
    )
    assert model["verify_fingerprint"] == expected

    # 配置变了（换 Key 版本）：旧结论不再适用，且不能把模型判成不可用。
    credentials.update_secret("key_a", "sk-fake-rotated")
    model = store.get_model("key_a", "model-x")
    assert model["verify_applies"] is False
    assert model["usable"] is True, "旧配置的结论不能用来判死当前配置"


def test_late_verification_result_only_leaves_trace(store, credentials):
    make(credentials)
    store.upsert_model("key_a", "model-x")
    snapshot = store.model_fingerprint("key_a", "model-x")
    credentials.update_secret("key_a", "sk-fake-rotated")
    outcome = store.record_verification(
        "key_a", "model-x", ok=False, reason_code="invalid_key", fingerprint=snapshot
    )
    assert outcome["applied"] is False
    assert outcome["result_state"] == "historical"
    assert store.get_model("key_a", "model-x")["verify_state"] == "unverified"


def test_transient_failure_keeps_previous_state(store, credentials):
    make(credentials)
    store.upsert_model("key_a", "model-x")
    store.record_verification("key_a", "model-x", ok=True)
    outcome = store.record_verification(
        "key_a", "model-x", ok=False, reason_code="timeout", transient=True
    )
    assert outcome["state_kept"] is True
    assert store.get_model("key_a", "model-x")["verify_state"] == "verified"


# ---------------------------------------------------------------------------
# 发现服务：分页 / 缓存 / 错误分类 / 安全边界
# ---------------------------------------------------------------------------


def _no_fetch(**_kwargs):
    raise AssertionError("这条路径不应该发网络请求")


@pytest.mark.asyncio
async def test_discovery_paginates_and_caches(monkeypatch, ctx, credentials, store):
    make(credentials)
    calls: list[dict] = []

    async def fake_fetch(**kwargs):
        calls.append(kwargs)
        return [
            {"model_id": "m1", "display_name": "M1"},
            {"model_id": "m2", "display_name": "M2"},
            {"model_id": "m3", "display_name": None},
        ]

    monkeypatch.setattr(discovery, "fetch_provider_models", fake_fetch)
    first = await discovery.discover(ctx, "key_a", page=1, page_size=2)
    assert first["source"] == "provider"
    assert first["total"] == 3 and first["has_more"] is True
    assert [m["model_id"] for m in first["models"]] == ["m1", "m2"]
    second = await discovery.discover(ctx, "key_a", page=2, page_size=2)
    assert second["source"] == "cache", "缓存期内不得重复发请求"
    assert [m["model_id"] for m in second["models"]] == ["m3"]
    assert second["has_more"] is False
    assert len(calls) == 1
    # 显式刷新才重新发请求。
    refreshed = await discovery.discover(ctx, "key_a", page=1, page_size=10, refresh=True)
    assert refreshed["source"] == "provider"
    assert len(calls) == 2
    state = store.discovery_state("key_a")
    assert state["checked_at"] and state["error"] is None and state["model_count"] == 3


@pytest.mark.asyncio
async def test_discovery_classifies_errors(monkeypatch, ctx, credentials, store):
    make(credentials)

    async def failing(**_kwargs):
        raise discovery.DiscoveryError(discovery.REASON_INVALID_KEY, "HTTP 401")

    monkeypatch.setattr(discovery, "fetch_provider_models", failing)
    result = await discovery.discover(ctx, "key_a", refresh=True)
    assert result["error"] and result["reason_code"] == discovery.REASON_INVALID_KEY
    assert result["models"] == []
    state = store.discovery_state("key_a")
    assert state["error"] and state["checked_at"]

    # 缓存期内重复调用直接返回上次错误（带同样的分类），不重新打网络。
    calls: list[dict] = []

    async def counting(**kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(discovery, "fetch_provider_models", counting)
    cached = await discovery.discover(ctx, "key_a")
    assert cached["source"] == "cache" and cached["error"]
    assert cached["reason_code"] == discovery.REASON_INVALID_KEY
    assert calls == [], "缓存期内不得重新打网络；刷新必须由 refresh=true 显式触发"


@pytest.mark.asyncio
async def test_discovery_never_sends_key_for_unavailable_or_unknown(monkeypatch, ctx, credentials):
    monkeypatch.setattr(discovery, "fetch_provider_models", _no_fetch)
    with pytest.raises(KeyError):
        await discovery.discover(ctx, "missing-key")

    make(credentials, "key_disabled")
    credentials.set_enabled("key_disabled", False)
    result = await discovery.discover(ctx, "key_disabled")
    assert result["reason_code"] == discovery.REASON_CREDENTIAL_UNAVAILABLE
    assert result["models"] == []


def test_public_catalog_marks_openrouter_and_is_offline():
    info = discovery.public_catalog_info()
    ids = [item["provider_id"] for item in info["providers"]]
    assert "openrouter" in ids
    entry = next(item for item in info["providers"] if item["provider_id"] == "openrouter")
    assert entry["public_catalog"] is True
    assert "不代表" in entry["discovery_note"]
    preset = find_preset("openrouter")
    assert preset.public_catalog is True
    assert preset.auth_path == "/key"
    # 预设名单没有被缩减（不得通过删服务商制造达标结果）。
    assert len(list_presets()) >= 16


# ---------------------------------------------------------------------------
# 按模型验证（服务层，用 conftest 的 fake verify）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_catalog_model_records_only_that_model(ctx, credentials, store, fake_verify):
    make(credentials)
    store.upsert_model("key_a", "model-a")
    store.upsert_model("key_a", "model-b")
    payload = await discovery.verify_catalog_model(ctx, "key_a", "model-a")
    assert payload["ok"] is True
    assert payload["applies_to_current_config"] is True
    assert payload["model_id"] == "model-a"
    assert store.get_model("key_a", "model-a")["verify_state"] == "verified"
    assert store.get_model("key_a", "model-b")["verify_state"] == "unverified"
    assert credentials.get_metadata("key_a")["verify_state"] == "verified"

    fake_verify.set(VerifyResult(False, "invalid_key", "bad key"))
    payload = await discovery.verify_catalog_model(ctx, "key_a", "model-b")
    assert payload["ok"] is False
    assert store.get_model("key_a", "model-b")["verify_state"] == "failed"
    assert store.get_model("key_a", "model-a")["verify_state"] == "verified"
    # 模型级失败不改变 Key 级状态。
    assert credentials.get_metadata("key_a")["verify_state"] == "verified"


@pytest.mark.asyncio
async def test_usage_resolve_service_falls_back_to_legacy_tags(ctx, credentials, store):
    make(credentials, tags=["main-loop"], model="deepseek-v4-flash")
    resolved = discovery.resolve_usage(ctx, "main-chat")
    assert resolved["supported"] is True
    # 没有默认模型、没有显式分配 → 兼容回退到基线 tags。
    assert resolved["resolved"]["model_id"] == "deepseek-v4-flash"
    assert resolved["source"] == "legacy_tags"
    custom = discovery.resolve_usage(ctx, "custom:写作")
    assert custom["supported"] is False and custom["resolved"] is None
