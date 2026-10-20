# -*- coding: utf-8 -*-
"""D 独立契约测试：子任务 _qio_subtask 控制字段（契约 §14.2、§14.3、§14.7）。

验收点：
* _qio_subtask 必填 credential_ref；model/context/evidence_ids/budget 严格校验；
* 控制字段与业务参数分离：先剥离控制字段再把业务参数交给工具；
* 只允许 subagent 工具使用；其他工具出现 _qio_subtask 必须拒绝；
* 原任务 / 原话题 / 目标版本 / 授权由程序绑定，模型不能伪造；
* 参数 schema 从执行模型生成（json_schema_for），不再手写散落 schema。

装置：真实 AppContext；fake adapter / fake DB；实现未合入时按契约失败、不 skip。
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json

import pytest

CLAUSE = "契约 §14.2/§14.3/§14.7"


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


class FakeTaskManager:
    def __init__(self) -> None:
        self.submitted: list[dict] = []
        self.results = {}

    def submit(self, tool_name, coro_factory, *, task_id=None):  # noqa: ANN001
        self.submitted.append({"tool": tool_name, "task_id": task_id})
        self.results[task_id] = coro_factory()
        return task_id

    async def await_result(self, task_id, timeout=None):  # noqa: ANN001
        from agent.tools.base import ToolResult

        return ("completed", ToolResult(ok=True, content="fake subagent result"))

    def record_info(self, task_id):  # noqa: ANN001
        return None


class FakeCredentials:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def list_tagged(self, tag):  # noqa: ANN001
        self.calls.append({"list_tagged": tag})
        return [{
            "id": "cred_ok", "default_model": "model-ok", "tags": ["subagent"],
            "provider": "fake", "models": ["model-ok"], "capabilities": ["chat"],
        }]

    def get_metadata(self, key_id):  # noqa: ANN001
        self.calls.append({"get_metadata": key_id})
        return {"id": key_id, "default_model": "model-ok", "tags": ["subagent"]}

    def get_default_meta(self):
        self.calls.append({"get_default_meta": True})
        return None


def _subagent_tool(ctx, *, captured=None):
    from agent.tools.base import ToolResult
    from agent.tools.spec import ToolDefinition
    from agent.tools.subagent_tool import SubagentTool

    definition = ToolDefinition(
        name="research_task", description="研究一个子问题", tool_type="subagent",
        sync=True,
    )

    async def factory(credential_ref, model):  # noqa: ANN001
        if captured is not None:
            captured["credential_ref"] = credential_ref
            captured["model"] = model
        return "fake-adapter"

    tool = SubagentTool(
        definition,
        credentials=FakeCredentials(),
        task_manager=FakeTaskManager(),
        adapter_factory=factory,
    )
    return tool


def test_subtask_schema_comes_from_execution_model_and_requires_credential_ref(ctx):
    """§14.2：schema 由执行模型生成；_qio_subtask 必填 credential_ref。"""
    tool = _subagent_tool(ctx)
    assert getattr(tool, "arg_model", None) is not None, (
        f"{CLAUSE}：子任务工具必须提供 pydantic 参数模型（schema 由执行模型生成）")
    from agent.adapters.base import json_schema_for

    schema = json_schema_for(tool.arg_model)
    control = (schema.get("properties") or {}).get("_qio_subtask")
    assert control is not None, (
        f"{CLAUSE}：参数 schema 缺少保留字段 _qio_subtask：{json.dumps(schema, ensure_ascii=False)[:400]}")
    nested = json.dumps(control, ensure_ascii=False)
    assert "credential_ref" in nested, f"{CLAUSE}：_qio_subtask 缺少 credential_ref"
    required = control.get("required") if isinstance(control, dict) else None
    assert required and "credential_ref" in required, (
        f"{CLAUSE}：credential_ref 必须必填（required）：{nested[:300]}")


def test_subtask_missing_or_invalid_control_fields_are_rejected(ctx):
    """§14.2：必填/严格校验；未知字段、类型不符一律失败（不是静默忽略）。"""
    tool = _subagent_tool(ctx)
    cases = [
        {"_qio_subtask": {}},  # 缺 credential_ref
        {"_qio_subtask": {"credential_ref": "c", "model": 123}},  # model 类型错误
        {"_qio_subtask": {"credential_ref": "c", "budget": "lots"}},  # budget 类型错误
        {"_qio_subtask": {"credential_ref": "c", "evidence_ids": "not-a-list"}},
        {"_qio_subtask": {"credential_ref": "c", "unknown_control": True}},  # 未知控制字段
    ]
    for arguments in cases:
        result = _run(tool.run(**arguments))
        assert result.ok is False, (
            f"{CLAUSE}：非法控制字段必须拒绝：{arguments}（实际 {result}）")
        text = str(result.code or "") + str(result.error or "") + str(result.data or "")
        assert "invalid" in text.lower() or "参数" in text or "校验" in text, (
            f"{CLAUSE}：拒绝原因必须是参数校验失败：{text[:300]}")


def test_subtask_control_fields_are_stripped_before_business_arguments(ctx, monkeypatch):
    """§14.2：先剥离控制字段，再把业务参数交给工具（控制字段不进入业务调用）。"""
    tool = _subagent_tool(ctx)
    captured: dict = {}
    original = tool._run_subagent

    async def _capture(task_id, adapter, kwargs, tid, tracer):
        captured["kwargs"] = dict(kwargs)
        from agent.tools.base import ToolResult

        return ToolResult(ok=True, content="done")

    monkeypatch.setattr(tool, "_run_subagent", _capture)
    result = _run(tool.run(
        _qio_subtask={"credential_ref": "cred_ok", "context": ["材料 A"], "evidence_ids": ["m1"]},
        query="问题正文",
    ))
    assert result.ok is True, f"{CLAUSE}：合法控制字段应被接受（实际 {result}）"
    assert "_qio_subtask" not in captured["kwargs"], (
        f"{CLAUSE}：控制字段 _qio_subtask 未被剥离：{captured['kwargs']}")
    assert captured["kwargs"].get("query") == "问题正文", (
        f"{CLAUSE}：业务参数必须原样传递：{captured['kwargs']}")


def test_only_subagent_tool_accepts_subtask_control_field(ctx):
    """§14.2/§14.7：只允许 subagent 工具使用 _qio_subtask；其他工具必须拒绝。"""
    for name in ("await_task", "read_task_result", "dev_list_tasks", "declare_completion"):
        tool = ctx.registry.get(name)
        if tool is None:
            pytest.fail(f"{CLAUSE}：工具 {name} 未注册（实现未合入）", pytrace=False)
        arguments = {"_qio_subtask": {"credential_ref": "cred_ok"}}
        if name == "await_task":
            arguments["task_id"] = "task_x"
        if name == "read_task_result":
            arguments["task_id"] = "task_x"
        if name == "declare_completion":
            arguments["task_id"] = "ws_x"
        result = _run(tool.run(**arguments))
        assert result.ok is False, (
            f"{CLAUSE}：{name} 不是 subagent 工具，不得接受 _qio_subtask（实际 {result}）")
        text = str(result.code or "") + str(result.error or "") + str(result.data or "")
        assert "invalid" in text.lower() or "参数" in text or "未知" in text, (
            f"{CLAUSE}：{name} 拒绝原因应为参数校验：{text[:300]}")


def test_program_binds_original_task_and_topic_not_the_model(ctx):
    """§14.3：原任务/原话题/目标版本/授权由程序绑定；模型不能伪造。"""
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "backend" / "src" / "agent"
    subagent = (src / "tools" / "subagent_tool.py").read_text(encoding="utf-8")
    app = (src / "services" / "app.py").read_text(encoding="utf-8")
    orchestrator = (src / "services" / "turn_orchestrator.py").read_text(encoding="utf-8")
    combined = subagent + app + orchestrator
    for token in ("original_task", "original_topic", "task_version", "authorization"):
        assert token in combined, (
            f"{CLAUSE}：未见程序绑定的原任务/原话题/版本/授权字段（{token}）")
    for forged in ("original_task_id", "original_topic_id", "authorization"):
        assert forged not in json.dumps(
            {"properties": (_subagent_tool(ctx).arg_model.model_json_schema().get("properties") or {})}
        ), f"{CLAUSE}：_qio_subtask 不应允许模型填写 {forged}（必须由程序绑定）"
    assert "不允许伪造" in _load("agent.prompts").PROMPTS["SUBTASK_CONTROL_DESCRIPTION"]


def test_subagent_system_prompt_is_injected_into_subagent_loop(ctx):
    """§14.3：子任务执行者使用 SUBAGENT_SYSTEM，而不是主对话 SYSTEM_PROMPT。"""
    from pathlib import Path

    prompts = _load("agent.prompts")
    src = Path(__file__).resolve().parents[2] / "backend" / "src" / "agent"
    subagent = (src / "tools" / "subagent_tool.py").read_text(encoding="utf-8")
    assert "SUBAGENT_SYSTEM" in subagent, (
        f"{CLAUSE}：子任务循环必须注入 prompts.SUBAGENT_SYSTEM（当前源码未引用）")
    assert "SUBAGENT_SYSTEM" in prompts.PROMPTS
