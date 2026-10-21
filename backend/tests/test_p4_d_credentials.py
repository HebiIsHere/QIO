# -*- coding: utf-8 -*-
"""D 独立契约测试：可用凭据目录与凭据选择（契约 §14.3、§14.7）。

验收点：
* list_available_credentials 返回 credential_ref/provider/models/capabilities/availability/
  usage_limits，绝不返回密钥；模型凭据与业务服务凭据区分；
* 自主选择尊重用户明确指定、实际权限与使用上限；不固定首条/标签/主对话配置；
* credential_ref 统一，旧 credential_key 只在兼容层归一化；双参冲突拒绝；
* 调用/选择/用量归因一致（同一 credential_ref）。

装置：真实 AppContext + 临时 DB + MemoryKeyring（不碰系统凭据）；fake 模型、不联网。
"""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest

CLAUSE = "契约 §14.3/§14.7"
SECRET_MODEL = "sk-p4d-model-secret-绝不外泄"
SECRET_SERVICE = "svc-p4d-business-token-绝不外泄"


def _load(name: str):
    try:
        return __import__(name, fromlist=["*"])
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"{CLAUSE}：{name} 不可导入（实现未合入）：{exc!r}", pytrace=False)


def _run(value):
    if inspect.iscoroutine(value):
        return asyncio.run(value)
    return value


@pytest.fixture()
def ctx(db_conn, settings):
    from agent.api.server import create_app
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    context = app.state.ctx
    context.credentials._kr = MemoryKeyring()
    return context


def _make_credentials(ctx):
    from agent.credentials.store import VERIFY_STATE_VERIFIED

    store = ctx.credentials
    store.create("cred_model_a", SECRET_MODEL, ["main-loop", "subagent"],
                 endpoint="https://api.example.com/v1", default_model="model-a",
                 budget=1000.0, kind="model", verify_state=VERIFY_STATE_VERIFIED)
    store.create("cred_model_b", SECRET_MODEL, ["subagent"],
                 endpoint="https://api.example.com/v1", default_model="model-b",
                 budget=100.0, kind="model", verify_state=VERIFY_STATE_VERIFIED)
    store.set_enabled("cred_model_b", False)
    store.create("cred_model_c", SECRET_MODEL, ["subagent"],
                 endpoint="https://api.example.com/v1", default_model="model-c",
                 budget=5.0, kind="model", verify_state=VERIFY_STATE_VERIFIED)
    store.record_usage("cred_model_c", input_tokens=4, output_tokens=3)
    return store


def _catalog_tool(ctx):
    tool = ctx.registry.get("list_available_credentials")
    if tool is None:
        pytest.fail(f"{CLAUSE}：list_available_credentials 工具未注册（A 的凭据目录未合入）",
                    pytrace=False)
    return tool


def _catalog(ctx, purpose="subagent"):
    return _run(_catalog_tool(ctx).run(purpose=purpose))


def test_credential_catalog_tool_is_registered_with_frozen_description(ctx):
    """§14.2/§14.3：工具存在、说明冻结、参数含 purpose。"""
    prompts = _load("agent.prompts")
    tool = _catalog_tool(ctx)
    assert tool.description == prompts.TOOL_DESCRIPTIONS["list_available_credentials"], (
        f"{CLAUSE}：工具说明必须引用 prompts.TOOL_DESCRIPTIONS 单源")
    assert "purpose" in json.dumps(tool.parameters or {}), (
        f"{CLAUSE}：参数 schema 必须包含 purpose（用于子任务等用途）")


def test_credential_catalog_returns_required_fields_and_never_secrets(ctx):
    """§14.3：返回目录字段；不返回密钥原文。"""
    _make_credentials(ctx)
    result = _catalog(ctx)
    assert result.ok is True, f"{CLAUSE}：凭据目录调用失败：{result.error}"
    data = result.data or {}
    assert data.get("secrets_included") is False, (
        f"{CLAUSE}：结果必须明确 secrets_included=false")
    entries = data.get("credentials") or []
    assert entries, f"{CLAUSE}：subagent 用途应列出可用凭据"
    text = json.dumps(data, ensure_ascii=False) + str(result.content or "")
    assert SECRET_MODEL not in text and SECRET_SERVICE not in text, (
        f"{CLAUSE}：凭据目录不得返回密钥原文")
    entry = entries[0]
    for field in ("credential_ref", "provider", "models", "capabilities",
                  "availability", "usage_limits"):
        assert field in entry, f"{CLAUSE}：目录项缺少字段 {field}：{entry}"


def test_credential_catalog_distinguishes_model_and_business_credentials(ctx):
    """§14.3：模型凭据与业务服务凭据分开展示（结构上互不混入）。"""
    _make_credentials(ctx)
    result = _catalog(ctx, purpose="all")
    data = result.data or {}
    model_entries = data.get("model_credentials") or []
    service_entries = data.get("service_credentials") or []
    assert model_entries, f"{CLAUSE}：all 用途应返回模型凭据"
    assert all(str(item.get("kind")) == "model" for item in model_entries), (
        f"{CLAUSE}：model_credentials 混入了非模型凭据")
    assert all(str(item.get("kind")) == "service" for item in service_entries), (
        f"{CLAUSE}：service_credentials 混入了非业务凭据")
    source = (__import__("pathlib").Path(__file__).resolve().parents[2]
              / "backend" / "src" / "agent" / "credentials" / "selection.py").read_text(encoding="utf-8")
    assert "SERVICE_CREDENTIAL_SPECS" in source, (
        f"{CLAUSE}：业务服务凭据必须由独立清单/来源描述，不能与模型凭据同表混用")


def test_credential_catalog_availability_and_usage_limits_are_real(ctx):
    """§14.3：availability / usage_limits 反映真实状态（禁用、预算耗尽）。"""
    _make_credentials(ctx)
    result = _catalog(ctx)
    entries = (result.data or {}).get("credentials") or []
    by_ref = {str(item.get("credential_ref")): item for item in entries}
    disabled = by_ref.get("cred_model_b")
    assert disabled is not None, f"{CLAUSE}：禁用凭据应作为「不可用」列出而非消失"
    assert disabled["availability"]["usable"] is False, (
        f"{CLAUSE}：被禁用的凭据 availability.usable 必须为 false：{disabled['availability']}")
    exhausted = by_ref.get("cred_model_c")
    assert exhausted is not None, f"{CLAUSE}：预算耗尽凭据应列出并标为不可用"
    assert exhausted["availability"]["usable"] is False, (
        f"{CLAUSE}：预算耗尽凭据必须不可用：{exhausted['availability']}")
    assert exhausted["usage_limits"]["budget_left"] is not None
    assert float(exhausted["usage_limits"]["budget_left"]) <= 0


def test_credential_selection_respects_explicit_choice_and_permissions(ctx):
    """§14.3：用户明确指定优先；不可用/不存在必须如实失败（不静默换人）。"""
    selection = _load("agent.credentials.selection")
    store = _make_credentials(ctx)
    choice = selection.resolve_credential_choice(
        store, credential_ref="cred_model_a", purpose="subagent")
    assert choice["credential_ref"] == "cred_model_a" and choice["explicit"] is True, (
        f"{CLAUSE}：明确指定必须优先且标记 explicit：{choice}")
    assert choice.get("model"), f"{CLAUSE}：选择结果必须给出适用模型"
    with pytest.raises(selection.CredentialSelectionError):
        selection.resolve_credential_choice(
            store, credential_ref="cred_model_b", purpose="subagent")
    with pytest.raises(selection.CredentialSelectionError):
        selection.resolve_credential_choice(
            store, credential_ref="cred_does_not_exist", purpose="subagent")


def test_credential_ref_normalization_and_dual_parameter_conflict(ctx):
    """§14.3：credential_ref 统一；旧 credential_key 只在兼容层归一化；双参冲突拒绝。"""
    selection = _load("agent.credentials.selection")
    ref, legacy = selection.normalize_credential_selector(credential_ref="cred_a")
    assert ref == "cred_a" and legacy is False
    legacy_ref, legacy_flag = selection.normalize_credential_selector(
        credential_key="cred_b")
    assert legacy_ref == "cred_b" and legacy_flag is True, (
        f"{CLAUSE}：旧 credential_key 必须在兼容层归一化为 credential_ref")
    same, _ = selection.normalize_credential_selector(
        credential_ref="cred_c", credential_key="cred_c")
    assert same == "cred_c"
    with pytest.raises(selection.CredentialSelectionError):
        selection.normalize_credential_selector(
            credential_ref="cred_a", credential_key="cred_b")


def test_autonomous_selection_uses_default_and_skips_unusable(ctx):
    """§14.3：自主选择尊重用户默认/权限/上限，不固定首条/主对话配置。"""
    selection = _load("agent.credentials.selection")
    store = _make_credentials(ctx)
    store.set_default("cred_model_a")
    choice = selection.resolve_credential_choice(store, purpose="subagent")
    assert choice["credential_ref"] == "cred_model_a", (
        f"{CLAUSE}：自主选择必须尊重用户默认项（实际 {choice['credential_ref']}）")
    assert choice["explicit"] is False
    # 默认项若不可用，选择必须失败或换到真正可用的，而不是照抄默认
    store.set_default("cred_model_c")  # 预算已耗尽
    try:
        bad = selection.resolve_credential_choice(store, purpose="subagent")
    except selection.CredentialSelectionError:
        bad = None
    assert bad is None or bad["credential_ref"] != "cred_model_c", (
        f"{CLAUSE}：预算耗尽的默认凭据不得被选中：{bad}")


def test_usage_attribution_uses_the_same_credential_ref(ctx):
    """§14.3/§14.7：调用/选择/用量归因一致（同一 credential_ref）。"""
    usage = _load("agent.credentials.usage")
    store = _make_credentials(ctx)
    sink = usage.credential_usage_sink(store, type("FakeAdapter", (), {"key_id": "cred_model_a"})())
    assert sink is not None, f"{CLAUSE}：用量汇无法绑定到选择的凭据"
    result = sink(12, 8)
    if inspect.iscoroutine(result):
        _run(result)
    after = store.get_metadata("cred_model_a")
    assert float(after.get("budget_used") or 0) >= 20, (
        f"{CLAUSE}：用量必须归因到同一凭据（budget_used={after.get('budget_used')}）")
    assert store.budget_left("cred_model_a") is not None