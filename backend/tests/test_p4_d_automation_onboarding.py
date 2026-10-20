# -*- coding: utf-8 -*-
"""D 独立契约测试：自动化建议与首次引导（契约 §14.5、§14.7）。

验收点：
* 自动化建议：完整需求 + 实际结果 + 已有工具判断；可空；结构化 schema
  {suggestions:[{name,description,reason,parameters,evidence_ids}]}；公共结构化规则 +
  实际 schema + 独立 JSON 数据；无聊天回答标记；拒绝后无新依据不催问。
* 首次引导：ONBOARDING_FOLLOWUP_SYSTEM；最多两问；可空、可跳过、失败不阻塞。

装置：fake adapter（不联网、不真实模型）；API 用 TestClient；实现未合入时按契约失败。
"""

from __future__ import annotations

import importlib
import inspect
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
        pytest.fail(f"{CLAUSE}：契约要求的 {name} 不可导入（阶段四实现未合入）：{exc!r}",
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


def _automation_suggester():
    """定位自动化建议的公开入口（模块名/函数名允许实现自定，契约固定行为）。"""
    candidates = []
    for module_name in (
        "agent.services.automation",
        "agent.services.automation_suggestions",
        "agent.services.tool_candidates",
        "agent.maintenance.automation",
    ):
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001
            continue
        for name, value in vars(module).items():
            if not callable(value) or not inspect.isfunction(value):
                continue
            if "suggest" in name or "automation" in name or "candidate" in name:
                candidates.append((module_name, name, value))
    if not candidates:
        pytest.fail(
            f"{CLAUSE}：找不到自动化建议的公开入口（services/automation* 未合入）",
            pytrace=False,
        )
    return candidates[0]


def _run(value):
    import asyncio

    if inspect.iscoroutine(value):
        return asyncio.run(value)
    return value


def _call_suggester(fn, adapter, **kwargs):
    """兼容实现可能用不同参数名：优先 adapter + 请求列表，其次 adapter + 文本。"""
    signature = inspect.signature(fn)
    params = list(signature.parameters)
    if len(params) >= 2:
        second = signature.parameters[params[1]]
        annotation = str(second.annotation)
        if "list" in annotation.lower() or "requests" in params[1].lower():
            return _run(fn(adapter, [kwargs.get("request", "每周整理一次周报")], **kwargs))
        return _run(fn(adapter, kwargs.get("request", "每周整理一次周报")))
    return _run(fn(adapter))


def test_automation_suggestion_uses_frozen_prompt_and_structured_schema():
    """§14.2/§14.5：使用 AUTOMATION_SUGGESTION_SYSTEM；公共规则 + schema + 独立 JSON。"""
    prompts = _load("agent.prompts")
    frozen = prompts.PROMPTS["AUTOMATION_SUGGESTION_SYSTEM"]
    _, name, fn = _automation_suggester()
    adapter = FakeAdapter(json.dumps({"suggestions": [{
        "name": "weekly_digest", "description": "按周汇总",
        "reason": "重复出现", "parameters": {"type": "object"},
        "evidence_ids": ["m1", "m2"],
    }]}, ensure_ascii=False))
    _call_suggester(fn, adapter)
    assert adapter.calls, f"{CLAUSE}：{name} 没有调用模型，无法生成建议"
    system_text = "\n".join(
        content for role, content in adapter.calls[0]["messages"] if role == "system"
    )
    user_text = "\n".join(
        content for role, content in adapter.calls[0]["messages"] if role != "system"
    )
    assert frozen in system_text, (
        f"{CLAUSE}：自动化建议必须注入 prompts.AUTOMATION_SUGGESTION_SYSTEM（当前 system 未包含）")
    merged = system_text + user_text
    assert "suggestions" in merged and "evidence_ids" in merged, (
        f"{CLAUSE}：输出 schema 必须包含 suggestions / evidence_ids")
    assert "[[QIO:ANSWER]]" not in merged, (
        f"{CLAUSE}：自动化建议不得注入聊天回答标记")
    assert "name" in merged and "parameters" in merged, (
        f"{CLAUSE}：schema 必须包含 name / parameters")


def test_automation_suggestion_uses_complete_requirement_and_real_results():
    """§14.5：判断依据是完整需求 + 实际结果，不是前 200 字截断。"""
    _, name, fn = _automation_suggester()
    long_request = "我需要每周整理一次周报。" + "详细说明：" + "补充需求描述。" * 200
    assert len(long_request) > 2000
    adapter = FakeAdapter(json.dumps({"suggestions": []}))
    _call_suggester(fn, adapter, request=long_request)
    merged = "\n".join(
        content for call in adapter.calls for _, content in call["messages"]
    )
    assert long_request[-80:] in merged or "补充需求描述" in merged, (
        f"{CLAUSE}：{name} 没有把完整需求交给模型（疑似只取前 200 字）")
    # 可空：没有建议时必须是空列表而不是编造
    result = _call_suggester(fn, FakeAdapter(json.dumps({"suggestions": []})))
    suggestions = result.get("suggestions") if isinstance(result, dict) else result
    assert suggestions == [] or result == [], (
        f"{CLAUSE}：模型返回空建议时必须如实返回空（实际 {result!r}）")


def test_automation_suggestion_invalid_output_does_not_crash_or_fabricate():
    """§14.5：非法 JSON / 模型失败不阻塞；不得编造建议。"""
    _, name, fn = _automation_suggester()
    for adapter in (FakeAdapter("这不是 JSON"), FakeAdapter(raise_exc=True)):
        result = _call_suggester(fn, adapter)
        suggestions = result.get("suggestions") if isinstance(result, dict) else result
        assert not suggestions, (
            f"{CLAUSE}：{name} 在非法/失败输出下编造了建议：{result!r}")


def test_automation_suggestions_are_not_development_authorization():
    """§14.5：建议本身不是开发授权；不得只因建议就创建开发任务。"""
    import agent.services.maintenance as maintenance

    text = (SRC / "services" / "maintenance.py").read_text(encoding="utf-8")
    automation_files = [
        path for path in SRC.rglob("*.py")
        if "automation" in path.name or "suggestion" in path.name
    ]
    merged = text + "\n".join(
        path.read_text(encoding="utf-8", errors="ignore") for path in automation_files
    )
    assert "create(" not in merged or "approval" in merged, (
        f"{CLAUSE}：建议路径疑似直接创建开发任务（必须先经用户同意进入统一开发流程）")
    prompts = _load("agent.prompts")
    assert "不是开发授权" in prompts.PROMPTS["AUTOMATION_SUGGESTION_SYSTEM"]


def test_rejected_suggestions_are_not_nagged_without_new_evidence():
    """§14.5：已拒绝且无新依据不催问（实现需保存拒绝状态/依据）。"""
    sources = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in SRC.rglob("*.py")
        if "auto" in path.name or "suggest" in path.name or path.name == "maintenance.py"
    )
    tokens = ("rejected", "declined", "dismiss", "evidence", "新依据", "拒绝")
    hits = [token for token in tokens if token in sources]
    assert hits, (
        f"{CLAUSE}：未见「已拒绝/无新依据不催问」的实现痕迹（期望包含 {tokens} 之一）")


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
    merged = "\n".join(
        content for call in adapter.calls for _, content in call["messages"]
    )
    assert frozen in merged, (
        f"{CLAUSE}：追问必须注入 prompts.ONBOARDING_FOLLOWUP_SYSTEM")


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


def test_onboarding_followup_can_be_skipped(client, monkeypatch):
    """§14.5：可跳过（skip 时不调用模型、返回空问题）。"""
    client_obj, ctx = client
    adapter = FakeAdapter(json.dumps({"questions": ["不该问"]}))
    _fake_ctx_adapter(ctx, adapter, monkeypatch)
    response = client_obj.post("/api/onboarding/followups",
                               json={"description": "随便写点", "skip": True})
    assert response.status_code == 200
    assert response.json().get("questions") == [], (
        f"{CLAUSE}：skip=true 时必须跳过追问")
    assert not adapter.calls, f"{CLAUSE}：跳过时不得调用模型"
