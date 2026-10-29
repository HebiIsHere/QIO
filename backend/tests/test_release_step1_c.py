"""C：模型目录 / 用途候选 / 真实调用的连接可用性判断必须一致（全部 fake，不联网）。

问题：目录（catalog / list_models / model_usable / describe_usage / resolve_usage）
此前只看「这个模型验证失败」，不看连接级 verify_state，也不看密钥是否还可读；
而真实调用那一侧（services.usage_target.credential_problem）以
credential_unverified / secret_missing 拒绝。于是「连接未验证 / 密钥已不可读」的
目标在目录里被标成可用、界面可选，真正发请求却必然失败。

本文件要把「同一目标、同一状态只有一个结论」钉死：

* 目录（model_usable / get_model / list_models / catalog / describe_usage）
  与真实调用（credential_problem）对同一条连接给出的可用性判断一致；
* 每条原因都可读（中文），且经 usage_target 解析出的 reason_code 与真实调用同码；
* 合法历史不被误伤：legacy 连接仍可用；连接 verified + 模型未单独验证仍可用；
  模型级失败只在「结论适用于当前配置」时才算不可用；
* GET 目录只读本地事实（不联网、不发起验证调用），响应/日志不含密钥原文。

全部 fake：MemoryKeyring + 迁移后的空库；唯一的「走 HTTP」用例用一个只读目录的
最小 app + 把所有网络入口换成 boom 替身来证明「根本没碰网络」。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.api.model_routes import register_model_routes
from agent.credentials import models as models_module
from agent.credentials.models import ModelCatalogStore
from agent.credentials.store import CredentialStore, MemoryKeyring
from agent.services import model_catalog as discovery
from agent.services import usage_target as usage_module
from agent.services import verify as verify_service
from agent.services.usage_target import (
    REASON_EXPLICIT_UNAVAILABLE,
    SOURCE_EXPLICIT,
    UsageUnavailable,
    credential_problem,
    resolve_usage_target,
)
from agent.storage.settings import SettingsStore

DEEPSEEK = "https://api.deepseek.com/v1"
#: 假密钥：只用于证明「响应 / 日志里不出现原文」，不是任何真实凭据。
SECRET = "sk-fake-c-secret-0123456789abcdef"
KEY_ID = "key_c"
MODEL_ID = "model-c1"


@pytest.fixture()
def credentials(db_conn: sqlite3.Connection) -> CredentialStore:
    return CredentialStore(db_conn, keyring_backend=MemoryKeyring())


@pytest.fixture()
def store(db_conn: sqlite3.Connection, credentials: CredentialStore) -> ModelCatalogStore:
    return ModelCatalogStore(db_conn, credentials)


@pytest.fixture()
def settings_store(db_conn: sqlite3.Connection) -> SettingsStore:
    return SettingsStore(db_conn)


def _make_connection(
    credentials: CredentialStore,
    *,
    key_id: str = KEY_ID,
    verify_state: str = "verified",
    **kwargs,
) -> str:
    credentials.create(
        key_id,
        SECRET,
        ["main-loop"],
        DEEPSEEK,
        MODEL_ID,
        verify_state=verify_state,
        **kwargs,
    )
    return key_id


def _make_explicit_assignment(
    store: ModelCatalogStore, credentials: CredentialStore, usage_key: str = "main-chat"
) -> None:
    """一条连接 + 一个模型 + 一条显式用途分配（真实调用走的就是这条）。

    这里直接写分配行（不经保存校验）：用例要构造的场景是「分配保存之后连接/模型
    才变成未验证、停用、密钥不可读」——保存入口会拒绝这种新分配（发布第二轮 C），
    但**已经存在的**分配必须继续被目录与运行时如实判定。
    """
    store.upsert_model(KEY_ID, MODEL_ID)
    now = "2026-10-29T00:00:00+00:00"
    store.conn.execute(
        "INSERT INTO model_usages (id, usage_key, credential_id, model_id, mode, updated_at)"
        " VALUES (?, ?, ?, ?, 'explicit', ?)"
        " ON CONFLICT(usage_key) DO UPDATE SET credential_id=excluded.credential_id,"
        " model_id=excluded.model_id, mode=excluded.mode, updated_at=excluded.updated_at",
        (f"mu_{usage_key}", usage_key, KEY_ID, MODEL_ID, now),
    )
    store.conn.commit()


def _apply_state(credentials: CredentialStore, key_id: str, state: str) -> None:
    """把连接推到参数化用例要求的状态（全部本地操作，不联网）。"""
    if state == "disabled":
        credentials.set_enabled(key_id, False)
    elif state == "revoked":
        credentials.revoke(key_id)
    elif state == "expired":
        credentials.conn.execute(
            "UPDATE credentials SET status = 'expired' WHERE id = ?", (key_id,)
        )
        credentials.conn.commit()
    elif state == "budget":
        credentials.record_usage(key_id, input_tokens=200, output_tokens=0)
    elif state == "secret_missing":
        # 元数据仍然 active/verified，只是本机读不到密钥了（撤销 / 后端丢失）。
        credentials._kr.delete_password(credentials.service, key_id)  # noqa: SLF001
    elif state in ("unverified", "failed"):
        pass  # 由 create(verify_state=...) 决定
    elif state != "ok":
        raise AssertionError(f"未覆盖的状态：{state}")


#: (状态, 期望真实调用 reason_code（None=可用）, 目录原因里必须出现的关键词)
CONNECTION_STATES = [
    ("ok", None, ""),
    ("unverified", usage_module.REASON_CREDENTIAL_UNVERIFIED, "验证"),
    ("failed", usage_module.REASON_CREDENTIAL_UNVERIFIED, "验证"),
    ("disabled", usage_module.REASON_CREDENTIAL_DISABLED, "停用"),
    ("revoked", usage_module.REASON_CREDENTIAL_REVOKED, "撤销"),
    ("expired", usage_module.REASON_CREDENTIAL_EXPIRED, "过期"),
    ("budget", usage_module.REASON_BUDGET_EXHAUSTED, "上限"),
    ("secret_missing", usage_module.REASON_SECRET_MISSING, "密钥"),
]


@pytest.mark.parametrize("state, expected_code, reason_keyword", CONNECTION_STATES)
def test_catalog_usage_candidates_and_real_call_agree_on_connection_state(
    db_conn: sqlite3.Connection,
    credentials: CredentialStore,
    settings_store: SettingsStore,
    state: str,
    expected_code,
    reason_keyword: str,
):
    """同一目标、同一状态：目录 / 用途候选 / 真实调用必须是同一个结论。

    这是任务 C 的核心不变式：model_usable 说可用 ⇔ credential_problem 说没问题。
    旧实现里未验证连接与「密钥已不可读」的连接会被目录标成可用，真实调用却拒绝
    —— 两边各说各话。
    """
    store = ModelCatalogStore(db_conn, credentials)
    verify_state = state if state in ("unverified", "failed") else "verified"
    _make_connection(
        credentials,
        verify_state=verify_state,
        budget=100.0 if state == "budget" else None,
    )
    _make_explicit_assignment(store, credentials)
    _apply_state(credentials, KEY_ID, state)

    meta = credentials.get_metadata(KEY_ID)
    problem = credential_problem(credentials, KEY_ID)

    # 1) 目录：model_usable / get_model / list_models / catalog 四处结论一致。
    model = store.get_model(KEY_ID, MODEL_ID)
    listed = {item["model_id"]: item for item in store.list_models(KEY_ID)}[MODEL_ID]
    catalog = store.catalog()
    connection = {c["credential_id"]: c for c in catalog["connections"]}[KEY_ID]
    catalog_model = {m["model_id"]: m for m in connection["models"]}[MODEL_ID]
    direct_usable, direct_reason = store.model_usable(model, meta)
    assert direct_usable is model["usable"]
    assert direct_reason == model["unavailable_reason"]
    for item in (model, listed, catalog_model):
        assert item["usable"] is (problem is None), (
            f"状态 {state}：目录说 usable={item['usable']}，真实调用说 {problem}"
        )
        assert item["unavailable_reason"] == model["unavailable_reason"]

    # 2) 原因可读，且经 usage_target 解析出的 reason_code 与真实调用同码。
    if problem is None:
        assert model["unavailable_reason"] == ""
        assert model["usable"] is True
    else:
        assert model["unavailable_reason"], "不可用必须说明原因"
        assert reason_keyword in model["unavailable_reason"], (
            f"状态 {state}：原因不明确：{model['unavailable_reason']!r}"
        )
        assert model["usable"] is False
        assert usage_module._reason_code_for(model["unavailable_reason"]) == problem[0]
        assert problem[0] == expected_code

    # 3) 用途候选（describe_usage）与真实调用一致。
    main_chat = next(item for item in catalog["usages"] if item["usage_key"] == "main-chat")
    assert main_chat["resolved"] is not None
    assert main_chat["resolved"]["usable"] is (problem is None)
    assert main_chat["resolved"]["unavailable_reason"] == model["unavailable_reason"]

    # 4) 显式分配 → 运行时解析：可用就返回同一目标，不可用就带同码 detail_code 失败。
    if problem is None:
        target = resolve_usage_target(credentials, settings_store, "main-chat")
        assert target.source == SOURCE_EXPLICIT
        assert (target.credential_id, target.model_id) == (KEY_ID, MODEL_ID)
    else:
        with pytest.raises(UsageUnavailable) as excinfo:
            resolve_usage_target(credentials, settings_store, "main-chat")
        error = excinfo.value
        assert error.reason_code == REASON_EXPLICIT_UNAVAILABLE
        assert error.detail_code == problem[0], (
            f"状态 {state}：运行时 detail_code={error.detail_code!r} "
            f"与真实调用 {problem[0]!r} 不一致"
        )


def test_unverified_connection_is_not_marked_usable_in_catalog(
    store: ModelCatalogStore, credentials: CredentialStore
):
    """反例回归：连接未验证时，目录不得把模型标成可用（旧实现这里会失败）。

    模型行本身可能写着 verified（来自另一次配置/历史），但连接级 verify_state 才是
    真实调用认的那一层：未验证连接发不出请求，目录就必须说不可用。
    """
    _make_connection(credentials, verify_state="unverified")
    store.upsert_model(KEY_ID, "model-verified-row")
    store.upsert_model(KEY_ID, "model-unverified-row")
    # 让其中一行模型自己「验证通过」（与连接级状态互相独立）。
    store.record_verification(KEY_ID, "model-verified-row", ok=True)

    models = {item["model_id"]: item for item in store.list_models(KEY_ID)}
    for model_id in ("model-verified-row", "model-unverified-row"):
        item = models[model_id]
        assert item["usable"] is False, (
            f"连接未验证，模型 {model_id} 却被目录标成可用：{item}"
        )
        assert item["unavailable_reason"], "不可用必须带可读原因"
        assert "验证" in item["unavailable_reason"], item["unavailable_reason"]
        # 连接级原因，不是「这个模型验证失败」那句（不能把锅扣在模型上）。
        assert not item["unavailable_reason"].startswith("这个模型"), item["unavailable_reason"]
        assert (
            usage_module._reason_code_for(item["unavailable_reason"])
            == usage_module.REASON_CREDENTIAL_UNVERIFIED
        )


def test_unverified_connection_explicit_assignment_fails_with_unverified_code(
    store: ModelCatalogStore, credentials: CredentialStore, settings_store: SettingsStore
):
    """反例回归：未验证连接的显式分配必须如实失败，detail_code=credential_unverified。

    旧实现：目录说 usable=True，于是 _explicit_ownership 把 True 带进 _target_problem；
    虽然真实调用那一侧仍会拒绝，但目录与运行时已经不一致。本条把「目录结论」和
    「运行时失败码」同时钉住。
    """
    _make_connection(credentials, verify_state="unverified")
    _make_explicit_assignment(store, credentials)

    assert store.get_model(KEY_ID, MODEL_ID)["usable"] is False
    with pytest.raises(UsageUnavailable) as excinfo:
        resolve_usage_target(credentials, settings_store, "main-chat")
    error = excinfo.value
    assert error.reason_code == REASON_EXPLICIT_UNAVAILABLE
    assert error.detail_code == usage_module.REASON_CREDENTIAL_UNVERIFIED
    assert "验证" in error.message
    assert SECRET not in error.message


def test_missing_secret_blocks_catalog_and_real_call(
    store: ModelCatalogStore, credentials: CredentialStore, settings_store: SettingsStore
):
    """密钥不可读：元数据仍 active/verified，但目录与真实调用都必须说不可用。

    这是「撤销删了密钥 / keyring 后端丢了」的真实状态：目录此前只看元数据，会把
    一条永远发不出请求的连接展示成可用。读 keyring 是本地事实，允许；联网不允许。
    """
    _make_connection(credentials)
    _make_explicit_assignment(store, credentials)
    assert store.get_model(KEY_ID, MODEL_ID)["usable"] is True  # 前置：现在可用

    credentials._kr.delete_password(credentials.service, KEY_ID)  # noqa: SLF001

    model = store.get_model(KEY_ID, MODEL_ID)
    assert model["usable"] is False
    assert credentials.get_metadata(KEY_ID)["verify_state"] == "verified", (
        "连接元数据没变：变的是「密钥还在不在」"
    )
    assert "密钥" in model["unavailable_reason"]
    assert (
        usage_module._reason_code_for(model["unavailable_reason"])
        == usage_module.REASON_SECRET_MISSING
    )
    assert credential_problem(credentials, KEY_ID)[0] == usage_module.REASON_SECRET_MISSING

    with pytest.raises(UsageUnavailable) as excinfo:
        resolve_usage_target(credentials, settings_store, "main-chat")
    assert excinfo.value.detail_code == usage_module.REASON_SECRET_MISSING
    assert SECRET not in excinfo.value.message
    assert SECRET not in str(store.catalog())


def test_list_models_reads_secret_once_per_connection(db_conn: sqlite3.Connection):
    """列出多条模型只读一次 keyring：语义一致，但不在每个模型上重复读密钥。"""

    class CountingKeyring(MemoryKeyring):
        def __init__(self) -> None:
            super().__init__()
            self.reads = 0

        def get_password(self, service: str, username: str) -> str | None:
            self.reads += 1
            return super().get_password(service, username)

    counting = CountingKeyring()
    counted = CredentialStore(db_conn, keyring_backend=counting)
    _make_connection(counted)
    counting_store = ModelCatalogStore(db_conn, counted)
    for model_id in ("model-c1", "model-c2", "model-c3"):
        counting_store.upsert_model(KEY_ID, model_id)

    counting.reads = 0
    models = counting_store.list_models(KEY_ID)
    assert len(models) == 3
    assert all(item["usable"] is True for item in models)
    assert counting.reads == 1, (
        f"列出 3 个模型读了 {counting.reads} 次密钥；应当每条连接只读一次"
    )


def test_legacy_connection_stays_usable(
    store: ModelCatalogStore, credentials: CredentialStore, settings_store: SettingsStore
):
    """合法历史状态：连接 verify_state=legacy 必须仍然可用（不误禁老用户）。"""
    _make_connection(credentials, verify_state="legacy")
    _make_explicit_assignment(store, credentials)

    model = store.get_model(KEY_ID, MODEL_ID)
    assert model["usable"] is True and model["unavailable_reason"] == ""
    assert credential_problem(credentials, KEY_ID) is None
    target = resolve_usage_target(credentials, settings_store, "main-chat")
    assert (target.credential_id, target.model_id) == (KEY_ID, MODEL_ID)
    assert target.source == SOURCE_EXPLICIT


def test_verified_connection_with_unverified_model_stays_usable(
    store: ModelCatalogStore, credentials: CredentialStore, settings_store: SettingsStore
):
    """连接 verified + 模型未单独验证 = 仍可用（真实调用规则如此，不能一律禁用）。"""
    _make_connection(credentials)
    _make_explicit_assignment(store, credentials)

    model = store.get_model(KEY_ID, MODEL_ID)
    assert model["verify_state"] == "unverified"
    assert model["verify_applies"] is False
    assert model["usable"] is True, "不能因为模型没单独验证过就禁用整条连接"
    target = resolve_usage_target(credentials, settings_store, "main-chat")
    assert target.source == SOURCE_EXPLICIT


def test_model_level_failure_only_blocks_while_conclusion_applies(
    store: ModelCatalogStore, credentials: CredentialStore
):
    """模型失败只在「结论适用于当前配置」时不可用；旧配置的失败结论不能判死新配置。

    连接本身仍然可用（模型级失败不连带整把 Key），所以这里比较的是
    「usable=True ⇒ credential_problem 为空」这条方向，而不是完全等价。
    """
    _make_connection(credentials)
    store.upsert_model(KEY_ID, MODEL_ID)
    store.record_verification(KEY_ID, MODEL_ID, ok=False, reason_code="invalid_key")

    model = store.get_model(KEY_ID, MODEL_ID)
    assert model["verify_applies"] is True
    assert model["usable"] is False
    assert model["unavailable_reason"].startswith("这个模型")
    assert (
        usage_module._reason_code_for(model["unavailable_reason"])
        == usage_module.REASON_MODEL_UNUSABLE
    )
    # 连接级仍然没问题：失败只属于这一个模型。
    assert credential_problem(credentials, KEY_ID) is None

    # 配置变了（换密钥）：旧结论不再适用于当前配置 → 不再判死。
    credentials.update_secret(KEY_ID, SECRET + "-rotated")
    model = store.get_model(KEY_ID, MODEL_ID)
    assert model["verify_applies"] is False
    assert model["usable"] is True
    assert model["unavailable_reason"] == ""


def test_new_catalog_reason_constants_exist_and_map_to_runtime_codes():
    """目录新增的两个中文原因常量必须存在，并与运行时 reason_code 同义。"""
    unverified = getattr(models_module, "REASON_CREDENTIAL_UNVERIFIED", None)
    secret_missing = getattr(models_module, "REASON_SECRET_MISSING", None)
    assert unverified, "目录必须能说出「连接还没通过验证」这条原因"
    assert secret_missing, "目录必须能说出「密钥不可读」这条原因"
    assert "验证" in unverified and "密钥" in secret_missing
    assert usage_module._reason_code_for(unverified) == usage_module.REASON_CREDENTIAL_UNVERIFIED
    assert usage_module._reason_code_for(secret_missing) == usage_module.REASON_SECRET_MISSING


# ---------------------------------------------------------------------------
# GET 目录：只读本地事实（不联网、不发验证调用）；响应 / 日志不含密钥原文
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(db_conn: sqlite3.Connection, credentials: CredentialStore):
    ctx = SimpleNamespace(conn=db_conn, credentials=credentials)
    app = FastAPI()
    register_model_routes(app, ctx)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def seeded(credentials: CredentialStore) -> ModelCatalogStore:
    _make_connection(credentials, verify_state="unverified")
    catalog_store = ModelCatalogStore(credentials.conn, credentials)
    catalog_store.upsert_model(KEY_ID, MODEL_ID)
    return catalog_store


def test_catalog_get_never_touches_network(client, seeded, monkeypatch):
    """GET /api/models/catalog 只读本地事实：所有网络入口都换成 boom 替身。"""
    attempts: list[str] = []

    def boom(*_args, **_kwargs):
        attempts.append("network")
        raise AssertionError("目录读取不允许联网")

    class BoomAsyncClient:
        def __init__(self, *_args, **_kwargs) -> None:
            attempts.append("httpx.AsyncClient")
            raise AssertionError("目录读取不允许联网")

    monkeypatch.setattr(httpx, "AsyncClient", BoomAsyncClient)
    monkeypatch.setattr(discovery, "fetch_provider_models", boom)
    monkeypatch.setattr(verify_service, "verify_model", boom)

    response = client.get("/api/models/catalog")
    assert response.status_code == 200
    payload = response.json()
    assert [c["credential_id"] for c in payload["connections"]] == [KEY_ID]
    assert [m["model_id"] for m in payload["connections"][0]["models"]] == [MODEL_ID]

    # 目录读取照样给出了不可用结论（本地事实足够），且一次网络入口都没碰。
    assert payload["connections"][0]["models"][0]["usable"] is False
    assert attempts == [], f"目录读取碰了网络入口：{attempts}"


def test_catalog_response_and_logs_never_contain_secret(client, seeded, caplog):
    """响应与日志都不得出现密钥原文（读 keyring 可以，但它只进内存打码表）。"""
    caplog.set_level(logging.DEBUG)
    payload = client.get("/api/models/catalog").json()
    text = json.dumps(payload, ensure_ascii=False)
    assert SECRET not in text
    assert SECRET not in caplog.text
    # 连接元数据本身（不含密钥）确实被返回了，证明上面的「不含密钥」不是空数据凑数。
    assert payload["connections"][0]["credential_id"] == KEY_ID
