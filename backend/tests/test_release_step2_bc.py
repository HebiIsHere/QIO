"""B+C（发布第二轮）：用途分配真实生效性 + 候选按用途能力判断（全部 fake，不联网）。

B（原报告 F05/F06）：没有独立模型调用路径的用途，不能被画/存成「已生效」
----------------------------------------------------------------------
逐项核对真实读取路径后的结论（证据见各用途的 call_path 与实际调用点）：

* 真正参与调用（delivery=model）：main-chat / general-chat（同一路径，general 显式优先）、
  subagent（tools/subagent_tool.py 的 usage_assignment）、vision（build_vision_adapter →
  resolve_alongside_target）、memory-organize（app.py 预算整理 + maintenance.py）、
  knowledge-distill（build_derived_adapters / maintenance runner）、
  entity-extract（build_derived_adapters）；
* 没有独立调用路径（delivery=unsupported）：research（没有任何 resolve_usage("research")
  调用点，仅历史标签/policy 回退）、code（没有任何调用点读取 code 的分配；
  写代码由日常对话模型完成）、custom:<tag>（resolve_shared_target 直接判 no_call_path）；
* 沿用现有处理方式（delivery=existing）：video / audio（没有模型调用路径）、
  embedding（services/retrieval.py 走既有本地/远程嵌入方案，不读 model_usages）。

因此：这些用途不许再写新的独立分配；历史行仍可读并标「未生效」；清除历史覆盖仍允许。

C（F07）：候选与保存都按当前用途判断能力
----------------------------------------
* catalog?usage_key=… 服务端给出 usage_usable / usage_reason / capability_state；
* 四类结果：能力已声明支持可选 / 明确缺失不可选 / 能力未知仍可选 / 连接不可用不可选；
* 保存（单条与批量）用同一个 model_usable(..., usage_key=...)，不满足整批失败；
* 判断只来自已有能力与用途要求：不按模型名猜，不发起验证 / 联网。
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.api.model_routes import register_model_routes
from agent.credentials.models import ModelCatalogStore
from agent.credentials.policy import CredentialPolicy
from agent.credentials.store import CredentialStore, MemoryKeyring
from agent.credentials.usages import USAGES, list_usages
from agent.services import usage_target as usage_module
from agent.storage.settings import SettingsStore

DEEPSEEK = "https://api.deepseek.com/v1"
SECRET = "sk-fake-step2-bc-secret-0123456789abcdef"
KEY_ID = "key_bc"
MODEL_ID = "model-bc"

#: 逐项核对真实读取路径后的用途 → delivery 表（B1 的结论，钉死在测试里防漂移）。
EXPECTED_DELIVERY = {
    # 真正参与调用
    "main-chat": "model",
    "general-chat": "model",
    "subagent": "model",
    "vision": "model",
    "memory-organize": "model",
    "knowledge-distill": "model",
    "entity-extract": "model",
    # 没有独立调用路径
    "code": "unsupported",
    "research": "unsupported",
    # 沿用现有处理方式
    "video": "existing",
    "audio": "existing",
    "embedding": "existing",
}

#: 不允许新写独立分配的用途（按结论）。
INEFFECTIVE_USAGES = ["code", "research", "video", "audio", "embedding", "custom:写作助手"]
#: 真正会生效、必须还能保存的用途。
EFFECTIVE_USAGES = [
    "main-chat",
    "general-chat",
    "subagent",
    "vision",
    "memory-organize",
    "knowledge-distill",
    "entity-extract",
]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def credentials(db_conn: sqlite3.Connection) -> CredentialStore:
    return CredentialStore(db_conn, keyring_backend=MemoryKeyring())


@pytest.fixture()
def store(db_conn: sqlite3.Connection, credentials: CredentialStore) -> ModelCatalogStore:
    return ModelCatalogStore(db_conn, credentials)


@pytest.fixture()
def settings_store(db_conn: sqlite3.Connection) -> SettingsStore:
    return SettingsStore(db_conn)


@pytest.fixture()
def ctx(
    db_conn: sqlite3.Connection,
    credentials: CredentialStore,
    settings_store: SettingsStore,
) -> SimpleNamespace:
    return SimpleNamespace(
        conn=db_conn,
        credentials=credentials,
        settings_store=settings_store,
        policy=CredentialPolicy(credentials, register_default_accounting=False),
    )


@pytest.fixture()
def client(ctx: SimpleNamespace):
    app = FastAPI()
    register_model_routes(app, ctx)
    with TestClient(app) as test_client:
        yield test_client


def _make_connection(
    credentials: CredentialStore,
    *,
    key_id: str = KEY_ID,
    verify_state: str = "verified",
    **kwargs,
) -> None:
    credentials.create(
        key_id,
        f"{SECRET}-{key_id}",
        ["main-loop"],
        DEEPSEEK,
        MODEL_ID,
        verify_state=verify_state,
        **kwargs,
    )


def _seed_models(store: ModelCatalogStore, models: dict[str, list[str]] | None = None) -> None:
    for model_id, capabilities in (models or {MODEL_ID: []}).items():
        store.upsert_model(KEY_ID, model_id, capabilities=capabilities)


def _usage_rows(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT usage_key, credential_id, model_id, mode FROM model_usages"
    ).fetchall()
    return {str(row["usage_key"]): dict(row) for row in rows}


def _insert_usage_row(
    conn: sqlite3.Connection,
    usage_key: str,
    credential_id: str | None,
    model_id: str | None,
    *,
    mode: str = "legacy",
) -> None:
    """外部改库/迁移造历史行：这里故意绕过保存校验（历史分配必须可读）。"""
    now = "2026-10-29T00:00:00+00:00"
    conn.execute(
        "INSERT INTO model_usages (id, usage_key, credential_id, model_id, mode, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(usage_key) DO UPDATE SET credential_id=excluded.credential_id,"
        " model_id=excluded.model_id, mode=excluded.mode, updated_at=excluded.updated_at",
        (f"mu_{usage_key}", usage_key, credential_id, model_id, mode, now),
    )
    conn.commit()


def _usages_by_key(payload: dict) -> dict[str, dict]:
    return {item["usage_key"]: item for item in payload["usages"]}


def _models_by_id(payload: dict) -> dict[str, dict]:
    return {item["model_id"]: item for item in payload["connections"][0]["models"]}


# ---------------------------------------------------------------------------
# B1：逐用途结论（真实读取路径的产物，不是文案）
# ---------------------------------------------------------------------------


def test_usage_delivery_matches_real_read_paths():
    """delivery 表必须与真实读取路径一致：没有调用点就不许说会生效。"""
    by_key = {spec.key: spec.to_dict() for spec in USAGES}
    for usage_key, expected in EXPECTED_DELIVERY.items():
        assert by_key[usage_key]["delivery"] == expected, (
            f"{usage_key} 的 delivery={by_key[usage_key]['delivery']!r}，"
            f"按真实读取路径应为 {expected!r}"
        )
    # 分类边界：只有 delivery=model 的用途会被运行时读取。
    assert {spec.key for spec in USAGES if spec.delivery == "model"} == set(EFFECTIVE_USAGES)


def test_code_has_no_independent_call_path_and_is_read_only(store, credentials):
    """code（写代码）没有任何地方单独读取它的分配 → 不能继续假装可选。"""
    _make_connection(credentials)
    _seed_models(store)
    code = {item["usage_key"]: item for item in store.list_usages()}["code"]
    assert code["delivery"] == "unsupported"
    assert code["assignment_effective"] is False
    assert "跟随" in code["note"] or "主对话" in code["note"], (
        f"code 的说明必须讲清当前行为：{code['note']!r}"
    )
    with pytest.raises(ValueError) as excinfo:
        store.set_usage("code", KEY_ID, MODEL_ID)
    assert getattr(excinfo.value, "code", "") == "usage_not_effective"
    # 调试接口也不得把「实际不会被打到」的目标说成当前目标。
    assert store.resolve_usage("code") is None
    assert store.legacy_fallback("code") is None


# ---------------------------------------------------------------------------
# B2：后端拦得住无法生效的新分配（两个 HTTP 入口都要拦）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("usage_key", INEFFECTIVE_USAGES)
def test_store_rejects_new_assignment_for_ineffective_usage(
    store, credentials, db_conn, usage_key
):
    _make_connection(credentials)
    _seed_models(store)
    with pytest.raises(ValueError) as excinfo:
        store.set_usage(usage_key, KEY_ID, MODEL_ID)
    error = excinfo.value
    assert getattr(error, "code", "") == "usage_not_effective"
    assert getattr(error, "usage_key", "") == usage_key
    assert "不会" in str(error) or "不生效" in str(error)
    assert _usage_rows(db_conn) == {}, "被拒绝的分配不得留下任何行"
    # 清除（两者留空）不是新分配：始终允许。
    store.clear_usage(usage_key)


@pytest.mark.parametrize("usage_key", INEFFECTIVE_USAGES)
def test_single_usage_endpoint_rejects_ineffective_assignment(
    client, credentials, db_conn, usage_key
):
    _make_connection(credentials)
    ModelCatalogStore(db_conn, credentials).upsert_model(KEY_ID, MODEL_ID)
    resp = client.post(
        "/api/models/usage",
        json={"usage_key": usage_key, "credential_id": KEY_ID, "model_id": MODEL_ID},
    )
    assert resp.status_code == 422, resp.text
    payload = resp.json()
    assert payload["ok"] is False
    assert payload["code"] == "usage_not_effective", "必须给稳定的机器可读 code"
    assert payload["usage_key"] == usage_key
    assert payload["detail"], "必须有人话说明"
    assert _usage_rows(db_conn) == {}


def test_batch_usage_endpoint_rejects_ineffective_assignment_atomically(
    client, credentials, db_conn
):
    """批量里混入一项不生效用途：整批失败，已生效的那一项也不许写。"""
    _make_connection(credentials)
    ModelCatalogStore(db_conn, credentials).upsert_model(KEY_ID, MODEL_ID)
    resp = client.post(
        "/api/models/usages",
        json={
            "assignments": [
                {"usage_key": "main-chat", "credential_id": KEY_ID, "model_id": MODEL_ID},
                {"usage_key": "video", "credential_id": KEY_ID, "model_id": MODEL_ID},
            ]
        },
    )
    assert resp.status_code == 422, resp.text
    payload = resp.json()
    assert payload["code"] == "usage_not_effective"
    assert "第 2 项" in payload["detail"], payload["detail"]
    assert _usage_rows(db_conn) == {}, "整批原子失败：第一项也不得写入"


# ---------------------------------------------------------------------------
# B3：历史分配可读且标「未生效」；清除历史覆盖仍然允许
# ---------------------------------------------------------------------------


def test_historical_assignment_is_readable_and_marked_not_effective(store, credentials, db_conn):
    _make_connection(credentials)
    _seed_models(store)
    _insert_usage_row(db_conn, "video", KEY_ID, MODEL_ID, mode="legacy")

    usage = {item["usage_key"]: item for item in store.list_usages()}["video"]
    # 可读：历史行原样保留（不静默删除）。
    assert (usage["credential_id"], usage["model_id"]) == (KEY_ID, MODEL_ID)
    assert usage["mode"] == "legacy"
    # 但不冒充当前实际使用。
    assert usage["assignment_effective"] is False
    assert usage["historical"] is True
    assert usage["resolved"] is None, "未生效的用途不得给出「当前生效目标」"
    assert "历史配置" in usage["note"] and "未生效" in usage["note"]
    assert usage["historical_target"]["model_id"] == MODEL_ID
    assert usage["historical_target"]["usable"] is True

    # catalog 同一份事实（界面读的是它）。
    catalog = store.catalog()
    video = _usages_by_key(catalog)["video"]
    assert video["assignment_effective"] is False and video["historical"] is True

    # custom:<tag> 的历史行同样可读、同样标未生效。
    _insert_usage_row(db_conn, "custom:写作助手", KEY_ID, MODEL_ID, mode="legacy")
    custom = _usages_by_key(store.catalog()).get("custom:写作助手")
    assert custom is not None, "历史自定义标签必须继续出现在目录里"
    assert custom["delivery"] == "unsupported"
    assert custom["assignment_effective"] is False
    assert custom["historical"] is True


def test_clearing_historical_overlay_is_allowed_via_both_endpoints(
    client, credentials, db_conn
):
    _make_connection(credentials)
    ModelCatalogStore(db_conn, credentials).upsert_model(KEY_ID, MODEL_ID)

    _insert_usage_row(db_conn, "video", KEY_ID, MODEL_ID)
    resp = client.post(
        "/api/models/usage",
        json={"usage_key": "video", "credential_id": None, "model_id": None},
    )
    assert resp.status_code == 200, resp.text
    assert "video" not in _usage_rows(db_conn), "单条入口必须允许清除历史覆盖"

    _insert_usage_row(db_conn, "audio", KEY_ID, MODEL_ID)
    resp = client.post(
        "/api/models/usages",
        json={
            "assignments": [
                {"usage_key": "audio", "credential_id": None, "model_id": None},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    assert "audio" not in _usage_rows(db_conn), "批量入口必须允许清除历史覆盖"


# ---------------------------------------------------------------------------
# B4：真正生效的用途正常保存，并如实告诉前端「会生效」
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("usage_key", EFFECTIVE_USAGES)
def test_effective_usages_still_accept_assignment(store, credentials, db_conn, usage_key):
    _make_connection(credentials)
    _seed_models(store)
    assignment = store.set_usage(usage_key, KEY_ID, MODEL_ID)
    assert assignment["mode"] == "explicit"

    usage = {item["usage_key"]: item for item in store.list_usages()}[usage_key]
    assert usage["assignment_effective"] is True
    assert usage["historical"] is False
    assert usage["resolved"]["model_id"] == MODEL_ID
    assert _usage_rows(db_conn)[usage_key]["mode"] == "explicit"


def test_merged_chat_row_keeps_atomic_batch_semantics(client, credentials, db_conn):
    """第一轮的合并行原子保存语义保留（main-chat + general-chat 一次写）。"""
    _make_connection(credentials)
    ModelCatalogStore(db_conn, credentials).upsert_model(KEY_ID, MODEL_ID)
    resp = client.post(
        "/api/models/usages",
        json={
            "assignments": [
                {"usage_key": "main-chat", "credential_id": KEY_ID, "model_id": MODEL_ID},
                {"usage_key": "general-chat", "credential_id": KEY_ID, "model_id": MODEL_ID},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    rows = _usage_rows(db_conn)
    assert set(rows) == {"main-chat", "general-chat"}
    payload = resp.json()
    assert _usages_by_key(payload)["main-chat"]["resolved"]["model_id"] == MODEL_ID
    # effective 只在目录读里由运行时同一份 resolve_shared_target 算出。
    catalog = client.get("/api/models/catalog").json()
    merged = _usages_by_key(catalog)["main-chat"]
    assert merged["effective"]["ok"] is True
    assert merged["effective"]["model_id"] == MODEL_ID


# ---------------------------------------------------------------------------
# C1：catalog 按用途给出候选结论（四类结果）
# ---------------------------------------------------------------------------


def _seed_capability_models(store: ModelCatalogStore) -> None:
    store.upsert_model(KEY_ID, "text-only", capabilities=["text"])
    store.upsert_model(KEY_ID, "vision-capable", capabilities=["vision", "text"])
    store.upsert_model(KEY_ID, "unknown-caps")


def test_catalog_usage_scope_reports_four_candidate_classes(client, credentials, db_conn):
    _make_connection(credentials)
    store = ModelCatalogStore(db_conn, credentials)
    _seed_capability_models(store)

    payload = client.get("/api/models/catalog", params={"usage_key": "vision"}).json()
    assert payload["usage"]["usage_key"] == "vision"
    assert payload["usage"]["effective"] is True
    assert payload["usage"]["requires_capabilities"] == ["vision"]

    models = _models_by_id(payload)
    # 1) 明确支持：可选。
    assert models["vision-capable"]["capability_state"] == "supported"
    assert models["vision-capable"]["usage_usable"] is True
    assert models["vision-capable"]["usage_reason"] == ""
    # 2) 明确缺失：不可选 + 原因（一般可用与用途不适用是两条不同事实）。
    assert models["text-only"]["usable"] is True, "这个模型本身（一般可用性）没问题"
    assert models["text-only"]["usage_usable"] is False
    assert models["text-only"]["capability_state"] == "missing"
    assert "vision" in models["text-only"]["usage_reason"]
    # 3) 能力未知：仍可选，只标注未确认（不判为不支持）。
    assert models["unknown-caps"]["usage_usable"] is True
    assert models["unknown-caps"]["capability_state"] == "unknown"


def test_catalog_without_usage_key_stays_usage_agnostic(client, credentials, db_conn):
    _make_connection(credentials)
    ModelCatalogStore(db_conn, credentials).upsert_model(KEY_ID, "text-only", capabilities=["text"])
    payload = client.get("/api/models/catalog").json()
    model = _models_by_id(payload)["text-only"]
    assert "usage_usable" not in model, "没带 usage_key 时不得假装算了用途结论"
    assert payload.get("usage") is None


def test_catalog_usage_scope_keeps_connection_unavailable_reason(client, credentials, db_conn):
    """连接不可用：四类里的第 4 类——禁止选择，并沿用第一轮原因文案。"""
    _make_connection(credentials)
    store = ModelCatalogStore(db_conn, credentials)
    _seed_capability_models(store)
    credentials.set_enabled(KEY_ID, False)

    payload = client.get("/api/models/catalog", params={"usage_key": "vision"}).json()
    for model in _models_by_id(payload).values():
        assert model["usage_usable"] is False
        assert "停用" in model["usage_reason"], model["usage_reason"]
        assert model["usable"] is False, "一般可用性也要一致地不可用"
        assert usage_module._reason_code_for(model["usage_reason"]) == (
            usage_module.REASON_CREDENTIAL_DISABLED
        )


def test_catalog_rejects_unknown_usage_key(client, credentials, db_conn):
    _make_connection(credentials)
    resp = client.get("/api/models/catalog", params={"usage_key": "not-a-usage"})
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "unknown_usage"


def test_catalog_usage_scope_never_touches_network(
    client, credentials, db_conn, monkeypatch
):
    """按用途取候选只读本地能力事实：不发验证、不联网（不得为打开候选付费探测）。"""
    import httpx

    from agent.services import model_catalog as discovery
    from agent.services import verify as verify_service

    _make_connection(credentials)
    store = ModelCatalogStore(db_conn, credentials)
    _seed_capability_models(store)
    attempts: list[str] = []

    def boom(*_args, **_kwargs):
        attempts.append("network")
        raise AssertionError("候选判定不允许联网 / 发起验证")

    class BoomAsyncClient:
        def __init__(self, *_args, **_kwargs) -> None:
            attempts.append("httpx.AsyncClient")
            raise AssertionError("候选判定不允许联网")

    monkeypatch.setattr(httpx, "AsyncClient", BoomAsyncClient)
    monkeypatch.setattr(discovery, "fetch_provider_models", boom)
    monkeypatch.setattr(verify_service, "verify_model", boom)

    resp = client.get("/api/models/catalog", params={"usage_key": "vision"})
    assert resp.status_code == 200, resp.text
    assert _models_by_id(resp.json())["text-only"]["usage_usable"] is False
    assert attempts == [], f"候选判定碰了网络入口：{attempts}"


# ---------------------------------------------------------------------------
# C2：保存校验与真实调用同一份 model_usable(..., usage_key=...)
# ---------------------------------------------------------------------------


def test_assignment_rejects_model_missing_required_capability(store, db_conn, credentials):
    _make_connection(credentials)
    store.upsert_model(KEY_ID, "text-only", capabilities=["text"])
    with pytest.raises(ValueError) as excinfo:
        store.set_usage("vision", KEY_ID, "text-only")
    error = excinfo.value
    assert getattr(error, "code", "") == "usage_target_unusable"
    assert "vision" in str(error), "拒绝原因必须指出缺的是哪项能力"
    assert _usage_rows(db_conn) == {}, "被拒绝的分配不得落库"
    # 同一个模型对普通对话仍然可以分配（两条不同事实）。
    store.set_usage("main-chat", KEY_ID, "text-only")
    assert _usage_rows(db_conn)["main-chat"]["model_id"] == "text-only"


def test_assignment_allows_unknown_capability(store, credentials, db_conn):
    """能力未知必须仍然允许保存（与候选一致），不因「没声明」判为不支持。"""
    _make_connection(credentials)
    store.upsert_model(KEY_ID, "unknown-caps")
    store.set_usage("vision", KEY_ID, "unknown-caps")
    assert _usage_rows(db_conn)["vision"]["model_id"] == "unknown-caps"
    usage = {item["usage_key"]: item for item in store.list_usages()}["vision"]
    assert usage["resolved"]["usable"] is True


def test_assignment_rejects_unusable_connection_with_round1_reason(store, credentials, db_conn):
    _make_connection(credentials, verify_state="unverified")
    store.upsert_model(KEY_ID, MODEL_ID)
    with pytest.raises(ValueError) as excinfo:
        store.set_usage("subagent", KEY_ID, MODEL_ID)
    error = excinfo.value
    assert getattr(error, "code", "") == "usage_target_unusable"
    reason = str(getattr(error, "reason", "") or error)
    assert "验证" in reason
    assert usage_module._reason_code_for(reason) == usage_module.REASON_CREDENTIAL_UNVERIFIED
    assert _usage_rows(db_conn) == {}


def test_batch_rejects_capability_missing_atomically(client, credentials, db_conn):
    _make_connection(credentials)
    store = ModelCatalogStore(db_conn, credentials)
    _seed_capability_models(store)
    resp = client.post(
        "/api/models/usages",
        json={
            "assignments": [
                {"usage_key": "main-chat", "credential_id": KEY_ID, "model_id": "vision-capable"},
                {"usage_key": "vision", "credential_id": KEY_ID, "model_id": "text-only"},
            ]
        },
    )
    assert resp.status_code == 422, resp.text
    payload = resp.json()
    assert payload["code"] == "usage_target_unusable"
    assert "第 2 项" in payload["detail"], payload["detail"]
    assert _usage_rows(db_conn) == {}, "整批原子失败：第一项也不得写入"


def test_save_validation_matches_catalog_usage_candidates(client, credentials, db_conn):
    """提交校验与真实调用一致：目录里 usage_usable=False 的候选，保存也必须被拒。"""
    _make_connection(credentials)
    store = ModelCatalogStore(db_conn, credentials)
    _seed_capability_models(store)
    catalog = client.get("/api/models/catalog", params={"usage_key": "vision"}).json()

    for model in catalog["connections"][0]["models"]:
        model_id = model["model_id"]
        try:
            store.set_usage("vision", KEY_ID, model_id)
        except ValueError as exc:
            assert model["usage_usable"] is False, (
                f"目录说 {model_id} 适用于 vision，保存却被拒：{exc}"
            )
            assert getattr(exc, "code", "") == "usage_target_unusable"
        else:
            assert model["usage_usable"] is True, (
                f"目录说 {model_id} 不适用于 vision，保存却成功"
            )
            store.clear_usage("vision")
    assert "vision" not in _usage_rows(db_conn)
