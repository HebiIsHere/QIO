# -*- coding: utf-8 -*-
"""D 独立契约测试：可用凭据目录与凭据选择（契约 §14.3、§14.7）。

验收点：
* list_available_credentials 返回 credential_ref/provider/models/capabilities/availability/
  usage_limits，绝不返回密钥；模型凭据与业务服务凭据区分；
* 自主选择尊重用户明确指定、实际权限与使用上限；不固定首条/标签/主对话配置；
* credential_ref 统一，旧 credential_key 只在兼容层归一化；双参冲突拒绝；
* 调用/选择/用量归因一致（同一 credential_ref）。

装置：真实 AppContext + 临时 DB + MemoryKeyring（不碰系统凭据）；模型 fake、不联网。
实现未合入时按契约失败（不 skip）。
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
from pathlib import Path

import pytest

CLAUSE = "契约 §14.3/§14.7"
ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "backend" / "src" / "agent"
SECRET_MODEL = "sk-p4d-model-secret-绝不外泄"
SECRET_SERVICE = "svc-p4d-business-token-绝不外泄"


def _load(name: str):
    try:
        return importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"{CLAUSE}：{name} 不可导入（阶段四实现未合入）：{exc!r}", pytrace=False)


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
                 budget=10.0, kind="model", verify_state=VERIFY_STATE_VERIFIED)
    store.create("cred_service_x", SECRET_SERVICE, ["web"],
                 endpoint="https://service.example.com", default_model=None,
                 budget=None, kind="service", verify_state=VERIFY_STATE_VERIFIED)
    store.set_enabled("cred_model_b", False)
    # cred_model_b 被禁用；再放一条预算耗尽的可用模型凭据
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


def _call_catalog(ctx, **arguments):
    tool = _catalog_tool(ctx)
    return _run(tool.run(**arguments))


def _serialized(result) -> str:
    return json.dumps(result.data or {}, ensure_ascii=False) + "\n" + str(result.content or "") + str(result.error or "")


def test_credential_catalog_tool_is_registered_with_frozen_description(ctx):
    """§14.2/§14.3：工具存在、说明冻结、参数含 purpose。"""
    prompts = _load("agent.prompts")
    tool = _catalog_tool(ctx)
    assert tool.description == prompts.TOOL_DESCRIPTIONS["list_available_credentials"], (
        f"{CLAUSE}：工具说明必须引用 prompts.TOOL_DESCRIPTIONS 单源")
    schema = ctx.registry.specs()
    default_spec = next((spec for spec in schema if spec.name == "list_available_credentials"), None)
    assert default_spec is not None, f"{CLAUSE}：注册表 specs 缺少凭据目录工具"
    parameters = getattr(default_spec, "parameters", {}) or {}
    assert "purpose" in json.dumps(parameters), (
        f"{CLAUSE}：参数 schema 必须包含 purpose（用于子任务等用途）")


def test_credential_catalog_returns_required_fields_and_never_secrets(ctx, caplog):
    """§14.3：返回目录字段；不返回密钥原文（结果/日志都不允许）。"""
    _make_credentials(ctx)
    caplog.set_level(logging.DEBUG)
    result = _call_catalog(ctx, purpose="subagent")
    assert result.ok is True, f"{CLAUSE}：凭据目录调用失败：{result.error}"
    text = _serialized(result)
    assert SECRET_MODEL not in text and SECRET_SERVICE not in text, (
        f"{CLAUSE}：凭据目录不得返回密钥原文")
    for token in ("credential_ref", "provider", "models", "capabilities",
                  "availability", "usage_limits"):
        assert token in text, f"{CLAUSE}：目录结果缺少字段 {token}"
    joined_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert SECRET_MODEL not in joined_logs and SECRET_SERVICE not in joined_logs, (
        f"{CLAUSE}：密钥进入日志")


def test_credential_catalog_distinguishes_model_and_business_credentials(ctx):
    """§14.3：模型凭据与业务服务凭据区分，不得把业务凭据当模型凭据。"""
    _make_credentials(ctx)
    result = _call_catalog(ctx, purpose="subagent")
    text = _serialized(result)
    assert "cred_model_a" in text, f"{CLAUSE}：模型凭据应出现在目录里"
    # 业务服务凭据要么不出现，要么被明确标为 service 类（不能当作模型可用）
    if "cred_service_x" in text:
        around = text[max(0, text.index("cred_service_x") - 200): text.index("cred_service_x") + 400]
        assert "service" in around.lower() or "业务" in around, (
            f"{CLAUSE}：业务服务凭据必须被明确区分（不能混在模型凭据里）")
    parsed = result.data or {}
    model_refs = {
        str(item.get("credential_ref") or item.get("id"))
        for item in (parsed.get("credentials") or parsed.get("items") or [])
        if isinstance(item, dict)
    } if isinstance(parsed, dict) else set()
    if model_refs:
        assert "cred_service_x" not in model_refs, (
            f"{CLAUSE}：业务服务凭据不得出现在模型凭据列表中：{sorted(model_refs)}")


def test_credential_catalog_availability_and_usage_limits_are_real(ctx):
    """§14.3：availability / usage_limits 反映真实状态（禁用、预算耗尽）。"""
    store = _make_credentials(ctx)
    result = _call_catalog(ctx, purpose="subagent")
    text = _serialized(result)
    parsed = result.data or {}
    items = parsed.get("credentials") or parsed.get("items") or [] if isinstance(parsed, dict) else []
    by_ref = {
        str(item.get("credential_ref") or item.get("id")): item
        for item in items if isinstance(item, dict)
    }
    if by_ref:
        assert "cred_model_b" not in by_ref, (
            f"{CLAUSE}：被禁用的凭据不能出现在可用目录里")
        assert by_ref.get("cred_model_c", {}).get("availability") in (
            False, "exhausted", "unavailable", "budget_exhausted", "false"), (
            f"{CLAUSE}：预算耗尽的凭据 availability 必须如实不可用：{by_ref.get('cred_model_c')}")
        assert store.budget_left("cred_model_c") == -2.0
    else:
        assert "cred_model_c" not in text or "耗尽" in text or "不可用" in text, (
            f"{CLAUSE}：预算耗尽的凭据必须标为不可用")


def test_credential_selection_respects_explicit_choice_and_permissions(ctx):
    """§14.3：用户明确指定优先；选择只在实际适用权限/使用上限内。"""
    policy_module = _load("agent.credentials.policy")
    store = _make_credentials(ctx)
    policy = policy_module.CredentialPolicy(store)
    refs = policy.resolve("subagent", ["subagent"])
    ids = [ref.id for ref in refs]
    assert ids, f"{CLAUSE}：subagent 用途应能解析出可用凭据"
    assert "cred_model_b" not in ids, f"{CLAUSE}：禁用的凭据不得被自动选中"
    assert "cred_model_c" not in ids, f"{CLAUSE}：预算耗尽的凭据不得被自动选中"
    snapshot = policy.snapshot("subagent", ["subagent"])
    assert snapshot.required_tags == ("subagent",)
    # 明确指定优先于排序：显式选择接口必须接受指定 id
    for name in ("select", "choose", "resolve_explicit", "pick"):
        function = getattr(policy, name, None)
        if callable(function):
            chosen = function("subagent", ["subagent"], preferred="cred_model_a")
            chosen_id = getattr(chosen, "id", chosen)
            assert str(chosen_id) == "cred_model_a", (
                f"{CLAUSE}：用户明确指定必须优先（实际 {chosen_id}）")
            return
    pytest.fail(f"{CLAUSE}：CredentialPolicy 缺少「按用户明确指定选择」的公开方法",
                pytrace=False)


def test_credential_ref_normalization_and_dual_parameter_conflict(ctx):
    """§14.3：credential_ref 统一；旧 credential_key 只在兼容层归一化；双参冲突拒绝。"""
    modules = []
    for name in ("agent.credentials.refs", "agent.credentials.catalog",
                 "agent.credentials.selection", "agent.credentials.policy"):
        try:
            modules.append(importlib.import_module(name))
        except Exception:  # noqa: BLE001
            continue
    functions = []
    for module in modules:
        for name, value in vars(module).items():
            if inspect.isfunction(value) and any(
                token in name for token in ("normalize", "resolve_ref", "credential_ref", "coerce_ref")
            ):
                functions.append((module.__name__, name, value))
    if not functions:
        pytest.fail(
            f"{CLAUSE}：找不到 credential_ref 归一化入口（兼容层未合入）", pytrace=False)
    _, name, function = functions[0]
    try:
        ref = function(credential_ref="cred_a", credential_key=None)
    except TypeError:
        ref = function("cred_a")
    assert "cred_a" in str(ref), (
        f"{CLAUSE}：{name} 未返回统一 credential_ref：{ref!r}")
    with pytest.raises(Exception):
        try:
            function(credential_ref="cred_a", credential_key="cred_b")
        except TypeError:
            function({"credential_ref": "cred_a", "credential_key": "cred_b"})


def test_usage_attribution_uses_the_same_credential_ref(ctx):
    """§14.3/§14.7：调用/选择/用量归因一致（同一 credential_ref）。"""
    usage = _load("agent.credentials.usage")
    store = _make_credentials(ctx)
    before = store.get_metadata("cred_model_a")
    assert before is not None
    class _FakeAdapter:
        key_id = "cred_model_a"

    sink = usage.credential_usage_sink(store, _FakeAdapter())
    assert sink is not None, f"{CLAUSE}：用量汇无法绑定到选择的凭据"
    result = sink(12, 8)
    if inspect.iscoroutine(result):
        _run(result)
    after = store.get_metadata("cred_model_a")
    used = float(after.get("budget_used") or 0)
    assert used >= 20, (
        f"{CLAUSE}：用量必须归因到同一凭据（budget_used={used}）")
    assert store.budget_left("cred_model_a") is not None


def test_credential_summary_never_uses_first_item_as_default_choice(ctx):
    """§14.3：QIO 自主选择不得固定首条 / 只按标签 / 主对话配置。"""
    selection_source = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (SRC / "credentials").glob("*.py")
    )
    assert "list_tagged(\"subagent\")[0]" not in selection_source, (
        f"{CLAUSE}：选择逻辑不得固定取首条")
    subagent = (SRC / "tools" / "subagent_tool.py").read_text(encoding="utf-8")
    assert "get_default_meta()" not in subagent or "purpose" in subagent, (
        f"{CLAUSE}：子任务选择不得静默回落到主对话配置（需按用途与上限选择）")
