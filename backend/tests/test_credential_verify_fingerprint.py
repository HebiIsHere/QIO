"""F09：旧凭据的验证结果不能覆盖新配置 —— 定向用例。

基线缺陷（8fa8452）：api/server.py::_run_verification 先读一次凭据元数据，再 await
一次真实的验证调用，最后**无条件** set_verified(...)。这一次 await 的窗口里用户可能
已经换了密钥 / 地址 / 模型 / 协议，于是「旧配置的验证结论」被写进了「新配置」的状态：

* 旧配置的成功晚到 → 一把从没验过的新 Key 被标成「已验证」，还可能被提成默认项、
  被主循环自动选中（可用性 / 默认项 / 自动候选全被污染）；
* 旧配置的失败晚到 → 刚刚验过的新配置被改回「未通过」。

修复分两层：

1. credentials/store.py（本分支）：验证开始前冻结**输入指纹**（密钥版本 version +
   endpoint + default_model + kind），set_verified(..., input_fingerprint=...) 条件写入；
   指纹对不上就只留痕、不动当前事实。配置变更本身（reconfigure / update_metadata）
   也会让旧结论失效 —— verify_state 回到 unverified；
2. api/server.py::_run_verification（**由 Lead 落地**）：把指纹带进 set_verified，并按
   结果区分「历史结果」与「当前状态」。补丁与响应字段见交付文档 _audit-deliver-D.md。

本文件里：
* 「存储层」小节现在就应该全绿（本分支已经生效）；
* 「API 层」小节在 server.py 补丁落地前会红 —— 它们就是那份补丁的定向验收，
  失败信息会直接指出还差哪一步。
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.bus import EventBus
from agent.api.events import EventType
from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import (
    CredentialStore,
    MemoryKeyring,
    compute_input_fingerprint,
)
from agent.services import verify as verify_service
from agent.services.verify import REASON_INVALID_KEY, REASON_UNREACHABLE, VerifyResult

SECRET = "sk-f09-abcdefghijklmnopqrstuvwxyz0123"
NEW_SECRET = "sk-f09-brand-new-key-value-0000"
MODEL = "deepseek-v4-flash"
NEW_MODEL = "kimi-k2"
ENDPOINT = "https://api.deepseek.com/v1"
NEW_ENDPOINT = "https://api.moonshot.cn/v1"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(db_conn: sqlite3.Connection) -> CredentialStore:
    return CredentialStore(db_conn, keyring_backend=MemoryKeyring())


def _make(store: CredentialStore, key_id: str = "k1", **overrides) -> str:
    kwargs = {
        "tags": ["main-loop"],
        "endpoint": ENDPOINT,
        "default_model": MODEL,
        "verify_state": "verified",
    }
    kwargs.update(overrides)
    store.create(key_id, SECRET, **kwargs)
    return key_id


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
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


def _create_verified(client) -> str:
    body = client.post("/api/credentials", json=_body()).json()
    assert body["credential"]["verify_state"] == "verified"
    return body["key_id"]


# ---------------------------------------------------------------------------
# 存储层：指纹本身（本分支已生效）
# ---------------------------------------------------------------------------


def test_fingerprint_covers_version_endpoint_model_and_kind():
    """四个输入里任何一个变了，指纹就必须变；没变的输入不能让指纹变。"""
    base = compute_input_fingerprint(
        version=1, endpoint=ENDPOINT, default_model=MODEL, kind="openai"
    )
    assert compute_input_fingerprint(
        version=2, endpoint=ENDPOINT, default_model=MODEL, kind="openai"
    ) != base, "密钥版本变了（换 Key / 轮换），指纹必须变"
    assert compute_input_fingerprint(
        version=1, endpoint=NEW_ENDPOINT, default_model=MODEL, kind="openai"
    ) != base, "服务地址变了，指纹必须变"
    assert compute_input_fingerprint(
        version=1, endpoint=ENDPOINT, default_model=NEW_MODEL, kind="openai"
    ) != base, "模型变了，指纹必须变"
    assert compute_input_fingerprint(
        version=1, endpoint=ENDPOINT, default_model=MODEL, kind="anthropic"
    ) != base, "协议变了，指纹必须变"
    # 纯格式差异不算配置变更（两侧统一规范化）
    assert compute_input_fingerprint(
        version="1", endpoint=" " + ENDPOINT + " ", default_model=MODEL + " ", kind="openai"
    ) == base


def test_snapshot_freezes_the_current_inputs_without_any_secret(store):
    _make(store)
    snap = store.verification_snapshot("k1")
    assert snap["key_id"] == "k1"
    assert snap["version"] == 1
    assert snap["endpoint"] == ENDPOINT
    assert snap["default_model"] == MODEL
    assert snap["kind"] == "openai", "老数据没有 kind 时按地址解析，与运行期一致"
    assert snap["input_fingerprint"] == store.input_fingerprint("k1")
    assert SECRET not in json.dumps(snap), "快照里不能出现密钥原文"
    with pytest.raises(KeyError):
        store.verification_snapshot("ghost")
    with pytest.raises(KeyError):
        store.input_fingerprint("ghost")


def test_an_inferred_kind_is_not_a_protocol_change_for_legacy_rows(store):
    """老数据 kind 为空：按地址解析出 openai 并写回去，不算「改了协议」。"""
    _make(store, key_id="legacy", kind=None, verify_state="legacy")
    before = store.input_fingerprint("legacy")
    store.update_metadata("legacy", kind="openai")
    assert store.input_fingerprint("legacy") == before
    assert store.get_metadata("legacy")["verify_state"] == "legacy", (
        "「什么都没改」不该把老凭据打回未验证"
    )


# ---------------------------------------------------------------------------
# 存储层：条件写入（F09 核心）
# ---------------------------------------------------------------------------


def test_late_success_for_the_old_config_does_not_verify_the_new_one(store):
    """旧配置的成功晚到：不能把新配置标成已验证，也不能改变默认项 / 自动候选。"""
    _make(store)
    snap = store.verification_snapshot("k1")
    store.reconfigure("k1", secret=NEW_SECRET, confirm_reconfigure=True)
    assert store.get_metadata("k1")["verify_state"] == "unverified"

    result = store.set_verified("k1", True, input_fingerprint=snap["input_fingerprint"])

    assert result["verify_result"]["applied"] is False
    assert result["verify_result"]["result_state"] == "historical"
    assert result["verify_result"]["current_fingerprint"] != snap["input_fingerprint"]
    meta = store.get_metadata("k1")
    assert meta["verify_state"] == "unverified", "旧配置的成功不能算新配置通过"
    assert meta["verified_at"] is None
    assert meta["verify_error"] is None
    assert store.list_tagged("main-loop") == [], "旧结论不能让凭据重新变成自动候选"
    assert store.current_default() is None
    assert store.promote_default() is None


def test_late_success_cannot_override_a_failure_of_the_new_config(store):
    """新配置已经验证失败，旧配置的成功晚到 —— 不能把失败洗成可用。"""
    _make(store)
    snap = store.verification_snapshot("k1")
    store.reconfigure("k1", secret=NEW_SECRET, confirm_reconfigure=True)
    store.set_verified("k1", False, REASON_INVALID_KEY)
    assert store.get_metadata("k1")["verify_state"] == "failed"

    result = store.set_verified("k1", True, input_fingerprint=snap["input_fingerprint"])

    assert result["verify_result"]["applied"] is False
    assert store.get_metadata("k1")["verify_state"] == "failed"
    assert store.list_tagged("main-loop") == []


def test_late_failure_cannot_override_a_success_of_the_new_config(store):
    """新配置已经验证通过，旧配置的失败晚到 —— 不能把通过降级成失败。"""
    _make(store)
    snap = store.verification_snapshot("k1")
    store.reconfigure("k1", secret=NEW_SECRET, confirm_reconfigure=True)
    store.set_verified("k1", True)
    assert store.get_metadata("k1")["verify_state"] == "verified"

    result = store.set_verified(
        "k1", False, REASON_INVALID_KEY, input_fingerprint=snap["input_fingerprint"]
    )

    assert result["verify_result"]["applied"] is False
    assert result["verify_result"]["result_state"] == "historical"
    assert store.get_metadata("k1")["verify_state"] == "verified"
    assert [c["id"] for c in store.list_tagged("main-loop")] == ["k1"]


def test_late_result_leaves_the_row_untouched_and_leaves_one_history_row(store):
    """迟到结果只留痕：凭据行一个字段都不动，审计里多一条 test。"""
    _make(store)
    snap = store.verification_snapshot("k1")
    store.reconfigure("k1", secret=NEW_SECRET, confirm_reconfigure=True)
    before = store.get_metadata("k1")
    audit_before = len(store.audit_log("k1"))

    store.set_verified("k1", True, input_fingerprint=snap["input_fingerprint"])

    after = store.get_metadata("k1")
    for field in ("verify_state", "verified_at", "verify_error", "updated_at", "version"):
        assert after[field] == before[field], f"{field} 不该被迟到结果改动"
    log = store.audit_log("k1")
    assert len(log) == audit_before + 1
    assert log[-1]["action"] == "test"


def test_result_with_the_matching_fingerprint_still_applies(store):
    _make(store, key_id="k1", verify_state="unverified")
    fingerprint = store.input_fingerprint("k1")
    result = store.set_verified("k1", True, input_fingerprint=fingerprint)
    assert result["verify_result"]["applied"] is True
    assert result["verify_result"]["result_state"] == "current"
    assert result["verify_state"] == "verified"
    assert [c["id"] for c in store.list_tagged("main-loop")] == ["k1"]


def test_set_verified_without_a_fingerprint_keeps_the_old_contract(store):
    """老调用方（不带指纹）行为不变：仍然无条件写入。"""
    _make(store, key_id="k1", verify_state="unverified")
    result = store.set_verified("k1", True, "fake provider: ok")
    assert result["verify_state"] == "verified"
    assert result["verify_result"]["applied"] is True


def test_a_stale_result_is_refused_after_a_secret_rotation(store):
    """update_secret 会推进密钥版本 → 轮换**之前**开始的验证结果同样写不进去。

    边界（见交付文档 §5）：update_secret 是 store 原语，产品路径不调用它，
    因此它只推进版本、不重置 verify_state；但指纹含版本，旧结果依旧被挡下。
    """
    _make(store)
    snap = store.verification_snapshot("k1")
    assert store.update_secret("k1", NEW_SECRET) == 2
    late = store.set_verified("k1", True, input_fingerprint=snap["input_fingerprint"])
    assert late["verify_result"]["result_state"] == "historical"
    assert late["verify_result"]["applied"] is False

# ---------------------------------------------------------------------------
# 存储层：配置变更让旧结论失效（密钥 / 地址 / 模型 / 协议）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["secret", "endpoint", "kind", "default_model"])
def test_every_verification_input_invalidates_the_old_conclusion(store, field):
    """密钥、地址、协议、模型任一变化 → 旧结论作废 + 旧快照的迟到结果也写不进去。"""
    _make(store)
    snap = store.verification_snapshot("k1")

    if field == "secret":
        store.reconfigure("k1", secret=NEW_SECRET, confirm_reconfigure=True)
    elif field == "endpoint":
        store.reconfigure(
            "k1", endpoint=NEW_ENDPOINT, secret=NEW_SECRET, confirm_reconfigure=True
        )
    elif field == "kind":
        store.reconfigure(
            "k1", kind="anthropic", secret=NEW_SECRET, confirm_reconfigure=True
        )
    else:
        store.reconfigure("k1", default_model=NEW_MODEL)

    meta = store.get_metadata("k1")
    assert meta["verify_state"] == "unverified", f"{field} 变了，旧结论必须作废"
    assert meta["verified_at"] is None
    assert store.list_tagged("main-loop") == [], f"{field} 变了，不能继续进自动候选"
    assert store.current_default() is None

    late = store.set_verified("k1", True, input_fingerprint=snap["input_fingerprint"])
    assert late["verify_result"]["result_state"] == "historical"
    assert store.get_metadata("k1")["verify_state"] == "unverified"


def test_unrelated_edits_keep_the_verification(store):
    """备注 / 标签 / 预算 / 同值写入都不影响「已验证」，不能误伤。"""
    _make(store)
    store.reconfigure("k1", note="只是一句备注", tags=["main-loop", "vision"], budget=1000)
    store.reconfigure("k1", default_model=MODEL)
    store.update_metadata("k1", endpoint=ENDPOINT)
    meta = store.get_metadata("k1")
    assert meta["verify_state"] == "verified"
    assert meta["note"] == "只是一句备注"
    assert meta["tags"] == ["main-loop", "vision"]


def test_update_metadata_invalidates_on_model_change(store):
    _make(store)
    store.update_metadata("k1", default_model=NEW_MODEL)
    assert store.get_metadata("k1")["verify_state"] == "unverified"
    # 只改备注不失效
    store.set_verified("k1", True)
    store.update_metadata("k1", note="备注")
    assert store.get_metadata("k1")["verify_state"] == "verified"


# ---------------------------------------------------------------------------
# 存储层：临时网络错误不能把好钥匙判死
# ---------------------------------------------------------------------------


def test_a_transient_failure_never_downgrades_a_usable_credential(store):
    _make(store)
    fingerprint = store.input_fingerprint("k1")
    result = store.set_verified(
        "k1", False, REASON_UNREACHABLE, input_fingerprint=fingerprint, transient=True
    )
    assert result["verify_result"]["state_kept"] is True
    assert result["verify_result"]["applied"] is False
    assert result["verify_state"] == "verified", "断网不能把一把本来可用的钥匙判死"
    assert [c["id"] for c in store.list_tagged("main-loop")] == ["k1"]


def test_a_transient_failure_still_records_failure_for_an_unverified_credential(store):
    """从未通过验证的凭据上一次断网 → 如实记「上次没通过」，不是「被服务拒绝」。"""
    _make(store, key_id="k1", verify_state="unverified")
    result = store.set_verified(
        "k1",
        False,
        REASON_UNREACHABLE,
        input_fingerprint=store.input_fingerprint("k1"),
        transient=True,
    )
    assert result["verify_result"]["applied"] is True
    assert result["verify_state"] == "failed"
    assert store.get_metadata("k1")["verify_error"] == REASON_UNREACHABLE


# ---------------------------------------------------------------------------
# API 层：需要 Lead 落地 server.py 补丁才有意义（补丁落地前会红）
# ---------------------------------------------------------------------------


def _configure_in_flight(store, key_id: str, change: str) -> None:
    """在验证「飞行中」把凭据配置改掉（模拟并发的 PATCH 请求已经落库）。"""
    if change == "secret":
        store.reconfigure(key_id, secret=NEW_SECRET, confirm_reconfigure=True)
    elif change == "endpoint":
        store.reconfigure(
            key_id,
            endpoint=NEW_ENDPOINT,
            default_model=NEW_MODEL,
            secret=NEW_SECRET,
            confirm_reconfigure=True,
        )
    elif change == "model":
        store.reconfigure(key_id, default_model=NEW_MODEL)
    else:  # pragma: no cover - 参数表里覆盖了全部取值
        raise AssertionError(change)


def test_api_late_success_never_marks_a_changed_config_verified(client, monkeypatch):
    key_id = _create_verified(client)
    store = client.app.state.ctx.credentials

    async def _in_flight(**kwargs):
        _configure_in_flight(store, key_id, "secret")
        return VerifyResult(True, mode="native", detail="fake provider: ok")

    monkeypatch.setattr(verify_service, "verify_model", _in_flight)
    response = client.post(f"/api/credentials/{key_id}/verify").json()
    verify = response["verify"]

    assert store.get_metadata(key_id)["verify_state"] == "unverified", (
        "F09：飞行中的验证属于改动前的配置，不能把新配置标成已验证"
    )
    assert client.get("/api/credentials").json()["default_key_id"] is None, (
        "F09：旧配置的迟到成功不能把这条凭据提成默认项"
    )
    assert verify["result_state"] == "historical", (
        "API 必须把这次结果说成历史结果（需要 server.py 补丁）"
    )
    assert verify["applies_to_current_config"] is False
    assert verify["state"] == "unverified", "响应里的状态必须是当前状态，不是这次探测的结论"


@pytest.mark.parametrize("change", ["secret", "endpoint", "model"])
def test_api_late_success_never_covers_any_changed_input(client, monkeypatch, change):
    """密钥、地址、模型三种变化都要核验（任务书 F09 验收）。"""
    key_id = _create_verified(client)
    store = client.app.state.ctx.credentials

    async def _in_flight(**kwargs):
        _configure_in_flight(store, key_id, change)
        return VerifyResult(True, mode="native", detail="fake provider: ok")

    monkeypatch.setattr(verify_service, "verify_model", _in_flight)
    verify = client.post(f"/api/credentials/{key_id}/verify").json()["verify"]

    assert store.get_metadata(key_id)["verify_state"] == "unverified"
    assert verify["result_state"] == "historical"
    assert verify["applies_to_current_config"] is False


def test_api_late_success_cannot_whitewash_a_new_config_failure(client, monkeypatch):
    key_id = _create_verified(client)
    store = client.app.state.ctx.credentials

    async def _in_flight(**kwargs):
        store.reconfigure(key_id, secret=NEW_SECRET, confirm_reconfigure=True)
        store.set_verified(key_id, False, REASON_INVALID_KEY)  # 新配置已经验证失败
        return VerifyResult(True, mode="native", detail="旧的成功晚到")

    monkeypatch.setattr(verify_service, "verify_model", _in_flight)
    verify = client.post(f"/api/credentials/{key_id}/verify").json()["verify"]

    assert store.get_metadata(key_id)["verify_state"] == "failed"
    assert verify["result_state"] == "historical"
    assert verify["ok"] is False, "这次探测通过了，但它不是当前配置的结论"
    assert client.get("/api/credentials").json()["default_key_id"] is None


def test_api_late_failure_cannot_undo_a_new_config_success(client, monkeypatch):
    key_id = _create_verified(client)
    store = client.app.state.ctx.credentials

    async def _in_flight(**kwargs):
        store.reconfigure(key_id, secret=NEW_SECRET, confirm_reconfigure=True)
        store.set_verified(key_id, True)  # 新配置已经验证通过
        return VerifyResult(False, REASON_INVALID_KEY, "401")

    monkeypatch.setattr(verify_service, "verify_model", _in_flight)
    verify = client.post(f"/api/credentials/{key_id}/verify").json()["verify"]

    assert store.get_metadata(key_id)["verify_state"] == "verified"
    assert verify["result_state"] == "historical"
    assert verify["state"] == "verified"


def test_api_event_tells_history_from_current_state(client, monkeypatch):
    """事件也要区分：历史结果不能被当成「当前配置已通过」。"""
    key_id = _create_verified(client)
    store = client.app.state.ctx.credentials
    recorded: list[dict] = []
    original_publish = EventBus.publish

    async def _spy(self, event):  # noqa: ANN001
        if getattr(event, "type", None) == EventType.CREDENTIAL_STATUS:
            recorded.append(dict(event.data))
        return await original_publish(self, event)

    monkeypatch.setattr(EventBus, "publish", _spy)

    async def _in_flight(**kwargs):
        store.reconfigure(key_id, secret=NEW_SECRET, confirm_reconfigure=True)
        return VerifyResult(True, mode="native", detail="fake provider: ok")

    monkeypatch.setattr(verify_service, "verify_model", _in_flight)
    client.post(f"/api/credentials/{key_id}/verify")

    assert recorded, "验证结束必须发 CREDENTIAL_STATUS 事件"
    last = recorded[-1]
    assert last["result_state"] == "historical"
    assert last["status"] == "unverified", "事件里的状态必须是当前状态"


def test_api_unchanged_config_still_verifies_normally(client):
    """同一配置下的普通验证不受影响：仍然写入 verified 并成为默认项。"""
    key_id = _create_verified(client)
    verify = client.post(f"/api/credentials/{key_id}/verify").json()["verify"]
    assert verify["ok"] is True
    assert verify["result_state"] == "current"
    assert verify["applies_to_current_config"] is True
    assert client.get("/api/credentials").json()["default_key_id"] == key_id


def test_api_transient_failure_keeps_a_working_credential(client, monkeypatch):
    """未改配置时的临时网络故障：状态保持不变（不因为一次断网把好钥匙判死）。"""
    key_id = _create_verified(client)

    async def _transient(**kwargs):
        return VerifyResult(False, REASON_UNREACHABLE, "connect error")

    monkeypatch.setattr(verify_service, "verify_model", _transient)
    verify = client.post(f"/api/credentials/{key_id}/verify").json()["verify"]

    assert verify["ok"] is False
    assert verify["state"] == "verified"
    assert verify["state_kept"] is True
    assert client.get("/api/credentials").json()["default_key_id"] == key_id
