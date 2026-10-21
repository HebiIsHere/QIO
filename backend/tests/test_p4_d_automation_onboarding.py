# -*- coding: utf-8 -*-
"""D 独立契约测试：自动化建议与首次引导（契约 §14.5、§14.7）。

验收点：
* 自动化建议：完整需求 + 实际结果 + 已有工具判断；可空；结构化 schema
  {suggestions:[{name,description,reason,parameters,evidence_ids}]}；公共结构化规则 +
  实际 schema + 独立 JSON 数据；无聊天回答标记；已拒绝且无新依据不催问。
* 首次引导：ONBOARDING_FOLLOWUP_SYSTEM；最多两问；可空、可跳过、失败不阻塞。

装置：fake adapter（不联网、不真实模型）；API 用 TestClient；实现未合入时按契约失败。
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

CLAUSE = "契约 §14.5"
ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "backend" / "src" / "agent"


def _load(name: str):
    try:
        return importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"{CLAUSE}：契约要求的 {name} 不可导入（实现未合入）：{exc!r}",
                    pytrace=False)


class FakeAdapter:
    """最小 fake adapter：脚本化返回文本、记录调用。"""

    mode = "native"
    model = "fake-p4-d"

    def __init__(self, content: str = "{}", *, raise_exc: bool = False) -> None:
        self.content = content
        self.raise_exc = raise_exc
        self.calls: list[dict[str, Any]] = []

    async def complete(self, messages, tools, *, temperature=None, max_tokens=None):
        from agent.adapters.base import ChatMessage, Completion

        self.calls.append({
            "messages": [(m.role, m.content) for m in messages],
            "tools": list(tools or []),
        })
        if self.raise_exc:
            raise RuntimeError("fake model failure")
        return Completion(message=ChatMessage(role="assistant", content=self.content))


@pytest.fixture()
def client(db_conn, settings, monkeypatch):
    from unittest.mock import AsyncMock

    from agent.api.server import create_app

    app = create_app(settings, db_conn)
    ctx = app.state.ctx
    monkeypatch.setattr(ctx, "build_adapter", AsyncMock(return_value=None))
    with TestClient(app) as c:
        yield c, ctx


def _run(value):
    if asyncio.iscoroutine(value):
        return asyncio.run(value)
    return value


def _automation():
    return _load("agent.services.automation")


def _suggestion(**overrides) -> dict:
    base = {
        "name": "weekly_digest",
        "description": "按周汇总重复请求",
        "reason": "同一类请求反复出现",
        "parameters": {"type": "object", "properties": {"week": {"type": "string"}}},
        "evidence_ids": ["msg_1", "msg_2"],
    }
    base.update(overrides)
    return base


def _captured_text(adapter) -> str:
    return "\n".join(content for call in adapter.calls for _, content in call["messages"])


def test_automation_suggestion_uses_frozen_prompt_and_structured_schema():
    """§14.2/§14.5：使用 AUTOMATION_SUGGESTION_SYSTEM；公共规则 + schema + 独立 JSON。"""
    prompts = _load("agent.prompts")
    frozen = prompts.PROMPTS["AUTOMATION_SUGGESTION_SYSTEM"]
    automation = _automation()
    adapter = FakeAdapter(json.dumps({"suggestions": [_suggestion()]}, ensure_ascii=False))
    suggestions, error = _run(automation.suggest_automations(
        adapter,
        requests=[{"request": "每周整理一次周报", "count": 3}],
        actual_results=[{"result": "已完成三次整理"}],
        existing_tools=[{"name": "note_tool", "description": "记录"}],
    ))
    assert error is None, f"{CLAUSE}：合法输出不应报错：{error}"
    assert suggestions and getattr(suggestions[0], "name", None) == "weekly_digest", (
        f"{CLAUSE}：有效建议必须被解析：{suggestions}")
    merged = _captured_text(adapter)
    assert frozen in merged, (
        f"{CLAUSE}：自动化建议必须注入 prompts.AUTOMATION_SUGGESTION_SYSTEM")
    assert "suggestions" in merged and "evidence_ids" in merged, (
        f"{CLAUSE}：输出 schema 必须包含 suggestions / evidence_ids")
    assert "[[QIO:ANSWER]]" not in merged, (
        f"{CLAUSE}：自动化建议不得注入聊天回答标记")
    assert "existing_tools" in merged and "actual_results" in merged, (
        f"{CLAUSE}：独立 JSON 数据必须包含已有工具与实际结果")


def test_automation_suggestion_uses_complete_requirement_and_allows_empty():
    """§14.5：判断依据是完整需求，不是前 200 字截断；允许空建议。"""
    automation = _automation()
    long_request = "我需要每周整理一次周报。" + "详细说明：" + "补充需求描述。" * 400
    assert len(long_request) > 2000
    adapter = FakeAdapter(json.dumps({"suggestions": []}))
    suggestions, error = _run(automation.suggest_automations(
        adapter, requests=[{"request": long_request, "count": 5}]))
    assert error is None and suggestions == [], (
        f"{CLAUSE}：模型返回空建议时必须如实返回空：{suggestions!r} / {error}")
    captured = _captured_text(adapter)
    assert long_request[-80:] in captured or "补充需求描述" in captured, (
        f"{CLAUSE}：必须把完整需求交给模型（疑似只取前 200 字）")


def test_automation_suggestion_invalid_output_does_not_crash_or_fabricate():
    """§14.5：非法 JSON / 模型失败不阻塞；不得编造建议。"""
    automation = _automation()
    for adapter in (FakeAdapter("这不是 JSON"), FakeAdapter(raise_exc=True)):
        suggestions, error = _run(automation.suggest_automations(
            adapter, requests=[{"request": "每周整理报告", "count": 3}]))
        assert suggestions == [], (
            f"{CLAUSE}：非法/失败输出下编造了建议：{suggestions!r}")
        assert error, f"{CLAUSE}：失败必须给出可读原因（不静默成功）"
    # 无请求时不调用模型
    empty = FakeAdapter(json.dumps({"suggestions": []}))
    suggestions, _error = _run(automation.suggest_automations(empty, requests=[]))
    assert suggestions == [] and not empty.calls, (
        f"{CLAUSE}：没有重复请求时不得调用模型")


def test_automation_suggestions_are_not_development_authorization():
    """§14.5：建议本身不是开发授权；不得只因建议就创建开发任务。"""
    automation_source = (SRC / "services" / "automation.py").read_text(encoding="utf-8")
    assert "create(" not in automation_source.replace("create_at", ""), (
        f"{CLAUSE}：建议模块不得直接创建开发任务（必须用户同意后走统一开发流程）")
    prompts = _load("agent.prompts")
    assert "不是开发授权" in prompts.PROMPTS["AUTOMATION_SUGGESTION_SYSTEM"]


def test_rejected_suggestions_are_not_nagged_without_new_evidence():
    """§14.5：已拒绝且无新依据不催问（同一依据指纹过滤）。"""
    automation = _automation()
    suggestion = automation.AutomationSuggestion(**_suggestion())
    signature = automation.suggestion_signature(
        {"name": suggestion.name, "description": suggestion.description})
    basis = automation.basis_fingerprint(["每周整理一次周报"])
    rejections = {signature: {"basis": basis, "reason": "用户拒绝"}}
    kept, skipped = automation.filter_suggestions(
        [suggestion],
        existing_tool_names=[],
        rejections=rejections,
        basis=basis,
    )
    assert kept == [] and skipped and skipped[0]["reason"] == "rejected_no_new_basis", (
        f"{CLAUSE}：已拒绝且无新依据不得再次催问：{kept} / {skipped}")
    kept_new, _skipped_new = automation.filter_suggestions(
        [suggestion],
        existing_tool_names=[],
        rejections=rejections,
        basis=automation.basis_fingerprint(["每周整理一次周报", "新增：还要汇总图表"]),
    )
    assert kept_new == [suggestion], (
        f"{CLAUSE}：出现新依据后应允许重新建议")
    kept_existing, skipped_existing = automation.filter_suggestions(
        [suggestion],
        existing_tool_names=[suggestion.name],
        rejections={},
        basis=basis,
    )
    assert kept_existing == [] and skipped_existing[0]["reason"] == "existing_tool", (
        f"{CLAUSE}：已有工具能覆盖时不得再建议开发")


# ---------------------------------------------------------------------------
# 首次引导追问
# ---------------------------------------------------------------------------


def _fake_ctx_adapter(ctx, adapter, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(ctx, "build_adapter", AsyncMock(return_value=adapter))


def test_onboarding_followup_uses_frozen_prompt_and_limits_two(client, monkeypatch):
    """§14.2/§14.5：使用 ONBOARDING_FOLLOWUP_SYSTEM；最多两个问题。"""
    prompts = _load("agent.prompts")
    frozen = prompts.PROMPTS["ONBOARDING_FOLLOWUP_SYSTEM"]
    client_obj, ctx = client
    adapter = FakeAdapter(json.dumps({"questions": ["问题一？", "问题二？", "问题三？"]},
                                     ensure_ascii=False))
    _fake_ctx_adapter(ctx, adapter, monkeypatch)
    response = client_obj.post("/api/onboarding/followups",
                               json={"description": "我做产品设计，希望助手记住长期偏好"})
    assert response.status_code == 200, (
        f"{CLAUSE}：追问接口失败（{response.status_code}: {response.text[:200]}）")
    body = response.json()
    assert isinstance(body.get("questions"), list)
    assert len(body["questions"]) <= 2, (
        f"{CLAUSE}：首次引导追问最多两个（实际 {len(body['questions'])}）")
    merged = _captured_text(adapter)
    assert frozen in merged, (
        f"{CLAUSE}：追问必须注入 prompts.ONBOARDING_FOLLOWUP_SYSTEM")
    assert "[[QIO:ANSWER]]" not in merged, (
        f"{CLAUSE}：引导追问不得注入聊天回答标记")


def test_onboarding_followup_empty_description_skips_model(client, monkeypatch):
    """§14.5：没有描述时没有问题、不调用模型、不阻塞。"""
    client_obj, ctx = client
    adapter = FakeAdapter(json.dumps({"questions": ["不该调用"]}))
    _fake_ctx_adapter(ctx, adapter, monkeypatch)
    response = client_obj.post("/api/onboarding/followups", json={"description": "  "})
    assert response.status_code == 200
    assert response.json().get("questions") == [], (
        f"{CLAUSE}：空描述必须返回空问题列表")
    assert not adapter.calls, f"{CLAUSE}：空描述不得调用模型"


def test_onboarding_followup_failure_or_invalid_json_is_non_blocking(client, monkeypatch):
    """§14.5：模型失败 / 非法 JSON 不阻塞引导（返回空问题而不是 500）。"""
    client_obj, ctx = client
    for adapter in (FakeAdapter(raise_exc=True), FakeAdapter("不是 JSON")):
        _fake_ctx_adapter(ctx, adapter, monkeypatch)
        response = client_obj.post("/api/onboarding/followups",
                                   json={"description": "我经常整理会议记录"})
        assert response.status_code == 200, (
            f"{CLAUSE}：追问失败必须不阻塞（实际 {response.status_code}: {response.text[:200]}）")
        assert response.json().get("questions") == [], (
            f"{CLAUSE}：失败时不得凭空返回问题")


def test_onboarding_followup_is_skippable_in_ui(client, monkeypatch):
    """§14.5：可跳过（前端提供跳过入口；跳过时不请求模型）。"""
    wizard = (ROOT / "frontend" / "src" / "components" / "onboarding"
              / "OnboardingWizard.vue").read_text(encoding="utf-8")
    assert "skip-followup" in wizard or "跳过" in wizard, (
        f"{CLAUSE}：引导追问步骤必须有跳过入口")
    client_obj, ctx = client
    adapter = FakeAdapter(json.dumps({"questions": ["不该问"]}))
    _fake_ctx_adapter(ctx, adapter, monkeypatch)
    response = client_obj.post("/api/onboarding/followups",
                               json={"description": "", "skip": True})
    assert response.status_code == 200
    assert response.json().get("questions") == []
    assert not adapter.calls, f"{CLAUSE}：跳过时不得调用模型"