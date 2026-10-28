"""D：日常对话合并行原子保存 + 目录显示「真实生效目标」（全部 fake，不联网）。

问题（发布前第一轮修复任务 D）：
1. 前端把 main-chat / general-chat 合并行分两次 POST /api/models/usage 顺序保存，
   第一次成功、第二次失败就留下半个配置 —— 后端没有原子多用途保存入口；
2. GET /api/models/catalog 没有「实际生效目标」：合并行显示 main-chat 自己的
   resolved，而真实运行规则（AppContext.resolve_main_target →
   resolve_shared_target(["general-chat", "main-chat"], primary_key="main-chat")）
   是 general-chat 显式指定优先；两份配置不一致时会显示一个、实际调用另一个。

本文件守住的语义：
* POST /api/models/usages 在一个 SQLite transaction 内全部写入；任一校验失败全部回滚；
* 单用途旧接口 POST /api/models/usage 保持兼容；
* catalog 的 effective 与真实 resolve_main_target 是同一份结果；
* 缺省 / 恢复默认时 effective 反映真实默认 / 政策目标或如实给出不可用原因；
* 任何响应都不含密钥原文。
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
from agent.services.app import AppContext
from agent.services.usage_target import (
    SOURCE_DEFAULT,
    SOURCE_EXPLICIT,
    resolve_shared_target,
)
from agent.storage.settings import SettingsStore

DEEPSEEK = "https://api.deepseek.com/v1"
OPENAI = "https://api.openai.com/v1"
SECRET_A = "sk-fake-step1d-key-a-0123456789"
SECRET_B = "sk-fake-step1d-key-b-0123456789"

#: 合并行的两个用途 key：顺序即真实显式优先级（general-chat > main-chat）。
MERGED_KEYS = ("main-chat", "general-chat")


@pytest.fixture()
def credentials(db_conn: sqlite3.Connection) -> CredentialStore:
    return CredentialStore(db_conn, keyring_backend=MemoryKeyring())


@pytest.fixture()
def settings_store(db_conn: sqlite3.Connection) -> SettingsStore:
    return SettingsStore(db_conn)


@pytest.fixture()
def ctx(
    db_conn: sqlite3.Connection,
    credentials: CredentialStore,
    settings_store: SettingsStore,
) -> SimpleNamespace:
    """与 server.py 挂载时同形的上下文（policy 在真实 AppContext 里也有）。"""
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


@pytest.fixture()
def seeded(ctx: SimpleNamespace, credentials: CredentialStore) -> ModelCatalogStore:
    """两条已验证连接，各带模型；没有任何用途分配 / 默认模型。"""
    credentials.create(
        "key_a",
        SECRET_A,
        tags=["main-loop"],
        endpoint=DEEPSEEK,
        default_model="model-a",
        verify_state="verified",
        note="工作用 A",
    )
    credentials.create(
        "key_b",
        SECRET_B,
        tags=["main-loop"],
        endpoint=OPENAI,
        default_model="model-b",
        verify_state="verified",
        note="工作用 B",
    )
    store = ModelCatalogStore(ctx.conn, credentials)
    store.upsert_model("key_a", "model-a")
    store.upsert_model("key_a", "model-a2")
    store.upsert_model("key_b", "model-b")
    return store


def _usage_row_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM model_usages").fetchone()[0])


def _runtime_main_target(ctx: SimpleNamespace):
    """真正跑一遍 AppContext.resolve_main_target（只依赖那三个属性）。"""
    fake_self = SimpleNamespace(
        credentials=ctx.credentials,
        settings_store=ctx.settings_store,
        policy=ctx.policy,
    )
    return AppContext.resolve_main_target(fake_self)


def _usages_by_key(payload: dict) -> dict[str, dict]:
    return {item["usage_key"]: item for item in payload["usages"]}


# ---------------------------------------------------------------------------
# 1. 原子批量保存：一次请求写两个用途，要么全成功要么全失败
# ---------------------------------------------------------------------------


def test_batch_assign_writes_both_usage_rows(client, seeded):
    resp = client.post(
        "/api/models/usages",
        json={
            "assignments": [
                {"usage_key": "main-chat", "credential_id": "key_a", "model_id": "model-a"},
                {"usage_key": "general-chat", "credential_id": "key_b", "model_id": "model-b"},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["ok"] is True
    usages = _usages_by_key(payload)
    assert usages["main-chat"]["resolved"]["credential_id"] == "key_a"
    assert usages["main-chat"]["resolved"]["model_id"] == "model-a"
    assert usages["general-chat"]["resolved"]["credential_id"] == "key_b"
    assert usages["general-chat"]["resolved"]["model_id"] == "model-b"

    # 后端读回完整新配置（不是只回包不回读）。
    assert seeded.get_usage("main-chat")["credential_id"] == "key_a"
    assert seeded.get_usage("main-chat")["model_id"] == "model-a"
    assert seeded.get_usage("general-chat")["credential_id"] == "key_b"
    assert seeded.get_usage("general-chat")["mode"] == "explicit"

    assert SECRET_A not in resp.text
    assert SECRET_B not in resp.text


def test_batch_assign_rolls_back_when_second_assignment_invalid(client, seeded, db_conn):
    """第 2 项校验失败（连接不存在）时，第 1 项也必须完全没写。"""
    seeded.set_usage("main-chat", "key_a", "model-a")
    seeded.set_usage("general-chat", "key_a", "model-a")
    before_rows = _usage_row_count(db_conn)

    resp = client.post(
        "/api/models/usages",
        json={
            "assignments": [
                {"usage_key": "main-chat", "credential_id": "key_b", "model_id": "model-b"},
                {"usage_key": "general-chat", "credential_id": "missing", "model_id": "model-b"},
            ]
        },
    )
    assert resp.status_code == 404, resp.text
    # 明确错误（不是路由不存在的 "Not Found"）：说明是第 2 项无效。
    detail = str(resp.json()["detail"])
    assert "missing" in detail, detail

    # 第 1 项完全未写：DB 原值保持。
    assert seeded.get_usage("main-chat")["credential_id"] == "key_a"
    assert seeded.get_usage("main-chat")["model_id"] == "model-a"
    assert seeded.get_usage("general-chat")["credential_id"] == "key_a"
    assert _usage_row_count(db_conn) == before_rows


def test_batch_assign_rejects_unknown_usage_and_rolls_back(client, seeded, db_conn):
    seeded.set_usage("main-chat", "key_a", "model-a")
    before_rows = _usage_row_count(db_conn)

    resp = client.post(
        "/api/models/usages",
        json={
            "assignments": [
                {"usage_key": "main-chat", "credential_id": "key_b", "model_id": "model-b"},
                {"usage_key": "not-a-usage", "credential_id": "key_b", "model_id": "model-b"},
            ]
        },
    )
    assert resp.status_code == 400, resp.text
    assert "not-a-usage" in str(resp.json()["detail"])
    assert seeded.get_usage("main-chat")["credential_id"] == "key_a"
    assert _usage_row_count(db_conn) == before_rows


def test_batch_assign_rejects_malformed_requests(client, seeded, db_conn):
    before_rows = _usage_row_count(db_conn)
    malformed = [
        {},
        {"assignments": []},
        {"assignments": "not-a-list"},
        {"assignments": [{}]},
        {"assignments": [{"usage_key": "main-chat", "credential_id": None, "model_id": "model-a"}]},
        {"assignments": [{"usage_key": "main-chat", "credential_id": "key_a"}]},
        {
            "assignments": [
                {"usage_key": "main-chat", "credential_id": "key_a", "model_id": "model-a"},
                {"usage_key": "main-chat", "credential_id": "key_b", "model_id": "model-b"},
            ]
        },
    ]
    for body in malformed:
        resp = client.post("/api/models/usages", json=body)
        assert resp.status_code == 400, (body, resp.status_code, resp.text)
    # 全部被拒 = 一行都没写。
    assert _usage_row_count(db_conn) == before_rows


def test_batch_restore_follows_default_clears_both_keys(client, seeded):
    seeded.set_usage("main-chat", "key_a", "model-a")
    seeded.set_usage("general-chat", "key_b", "model-b")
    seeded.set_default("key_a", "model-a2")

    resp = client.post(
        "/api/models/usages",
        json={
            "assignments": [
                {"usage_key": "main-chat", "credential_id": None, "model_id": None},
                {"usage_key": "general-chat", "credential_id": None, "model_id": None},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    assert seeded.get_usage("main-chat") is None
    assert seeded.get_usage("general-chat") is None

    catalog = client.get("/api/models/catalog").json()
    effective = _usages_by_key(catalog)["main-chat"]["effective"]
    assert effective["ok"] is True
    assert effective["source"] == SOURCE_DEFAULT
    assert effective["credential_id"] == "key_a"
    assert effective["model_id"] == "model-a2"


def test_single_usage_endpoint_still_works(client, seeded):
    """既有单用途接口保持兼容（新接口不替代它）。"""
    resp = client.post(
        "/api/models/usage",
        json={"usage_key": "vision", "credential_id": "key_a", "model_id": "model-a"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    vision = next(item for item in resp.json()["usages"] if item["usage_key"] == "vision")
    assert vision["follows_default"] is False
    assert vision["resolved"]["model_id"] == "model-a"
    seeded.clear_usage("vision")


# ---------------------------------------------------------------------------
# 2. catalog 的 effective：与真实 resolve_main_target 同一份结果
# ---------------------------------------------------------------------------


def test_catalog_effective_matches_runtime_main_target(client, seeded, ctx):
    seeded.set_default("key_a", "model-a")
    payload = client.get("/api/models/catalog").json()
    usages = _usages_by_key(payload)
    for key in MERGED_KEYS:
        assert "effective" in usages[key], f"{key} 必须给出真实生效目标 effective"

    runtime = _runtime_main_target(ctx)
    effective = usages["main-chat"]["effective"]
    direct = resolve_shared_target(
        ctx.credentials,
        ctx.settings_store,
        ["general-chat", "main-chat"],
        policy=ctx.policy,
        primary_key="main-chat",
    )
    for candidate in (runtime, direct):
        assert effective["ok"] is True
        assert effective["credential_id"] == candidate.credential_id
        assert effective["model_id"] == candidate.model_id
        assert effective["source"] == candidate.source
        assert effective["matched_usage_key"] == (
            candidate.matched_usage_key or candidate.usage_key
        )
    assert effective["source"] == SOURCE_DEFAULT
    assert effective["usable"] is True
    assert effective["unavailable_reason"] == ""
    assert effective["reason_code"] == ""
    # 合并行两个 key 展示的是同一份真实生效目标。
    assert usages["general-chat"]["effective"] == effective


def test_effective_prefers_explicit_general_chat_over_main_chat(client, seeded, ctx):
    """main-chat=A、general-chat=B：显示与调用都必须走 B（general-chat 显式优先）。"""
    seeded.set_usage("main-chat", "key_a", "model-a")
    seeded.set_usage("general-chat", "key_b", "model-b")

    payload = client.get("/api/models/catalog").json()
    effective = _usages_by_key(payload)["main-chat"]["effective"]
    runtime = _runtime_main_target(ctx)

    assert effective["ok"] is True
    assert (effective["credential_id"], effective["model_id"]) == ("key_b", "model-b")
    assert effective["matched_usage_key"] == "general-chat"
    assert effective["source"] == SOURCE_EXPLICIT
    # 与真实运行规则一致：两份配置不一致时，真实调用的是 general-chat 那一份。
    assert (runtime.credential_id, runtime.model_id) == ("key_b", "model-b")
    assert (runtime.credential_id, runtime.model_id) == (
        effective["credential_id"],
        effective["model_id"],
    )


def test_effective_reports_explicit_unavailable(client, seeded, ctx):
    """显式指定的目标被停用：effective 如实给不可用原因，不伪装成可用。"""
    seeded.set_usage("main-chat", "key_b", "model-b")
    ctx.credentials.set_enabled("key_b", False)

    payload = client.get("/api/models/catalog").json()
    effective = _usages_by_key(payload)["main-chat"]["effective"]
    assert effective["ok"] is False
    assert effective["usable"] is False
    assert effective["reason_code"] == "explicit_unavailable"
    assert effective["detail_code"] == "credential_disabled"
    assert "停用" in effective["unavailable_reason"]
    assert (effective["credential_id"], effective["model_id"]) == ("key_b", "model-b")
    # 与运行时同一判断：真实调用会抛 UsageUnavailable（这里只验证目录不撒谎）。
    from agent.services.usage_target import UsageUnavailable

    with pytest.raises(UsageUnavailable) as raised:
        _runtime_main_target(ctx)
    assert raised.value.detail_code == effective["detail_code"]


def test_effective_reports_no_credential_without_any_target(client, db_conn):
    """没有默认模型、没有可用政策候选：effective 如实给 no_credential。"""
    payload = client.get("/api/models/catalog").json()
    usages = _usages_by_key(payload)
    assert "effective" in usages["main-chat"]
    effective = usages["main-chat"]["effective"]
    assert effective["ok"] is False
    assert effective["usable"] is False
    assert effective["reason_code"] == "no_credential"
    assert effective["unavailable_reason"]
    assert effective["credential_id"] is None
    assert SECRET_A not in str(payload) and SECRET_B not in str(payload)


def test_effective_reports_verify_unusable_connection(client, seeded, ctx):
    """连接验证失败（unverified/failed）时 effective 与真实调用一致地不可用。"""
    ctx.credentials.create(
        "key_c",
        "sk-fake-step1d-key-c-0123456789",
        tags=["main-loop"],
        endpoint=DEEPSEEK,
        default_model="model-c",
        verify_state="unverified",
        note="未验证 C",
    )
    seeded.upsert_model("key_c", "model-c")
    seeded.set_usage("main-chat", "key_c", "model-c")

    payload = client.get("/api/models/catalog").json()
    effective = _usages_by_key(payload)["main-chat"]["effective"]
    assert effective["ok"] is False
    assert effective["reason_code"] == "explicit_unavailable"
    assert effective["detail_code"] == "credential_unverified"

    from agent.services.usage_target import UsageUnavailable

    with pytest.raises(UsageUnavailable) as raised:
        _runtime_main_target(ctx)
    assert raised.value.detail_code == "credential_unverified"
