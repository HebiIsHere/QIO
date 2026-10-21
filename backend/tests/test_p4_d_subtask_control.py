# -*- coding: utf-8 -*-
"""D 独立契约测试：子任务 _qio_subtask 控制字段（契约 §14.2、§14.3、§14.7）。

验收点：
* _qio_subtask 必填 credential_ref；model/context/evidence_ids/budget 严格校验；
* 控制字段与业务参数分离：先剥离控制字段再把业务参数交给工具；
* 只允许 subagent 工具使用；其他工具出现 _qio_subtask 必须拒绝；
* 原任务 / 原话题 / 目标版本 / 授权由程序绑定，模型不能伪造；
* 参数 schema 从执行模型生成（json_schema_for），不再手写散落 schema。

装置：真实 AppContext；fake 模型；实现未合入时按契约失败、不 skip。
"""

from __future__ import annotations

import json

import pytest

CLAUSE = "契约 §14.2/§14.3/§14.7"


def _load(name: str):
    try:
        return __import__(name, fromlist=["*"])
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"{CLAUSE}：{name} 不可导入（实现未合入）：{exc!r}", pytrace=False)


@pytest.fixture()
def ctx(db_conn, settings):
    from agent.api.server import create_app

    app = create_app(settings, db_conn)
    return app.state.ctx


def _subagent_module():
    return _load("agent.tools.subagent_tool")


def test_subtask_schema_comes_from_execution_model_and_requires_credential_ref():
    """§14.2：schema 由执行模型生成；_qio_subtask 必填 credential_ref。"""
    module = _subagent_module()
    schema = module.subtask_control_schema()
    assert isinstance(schema, dict) and schema.get("type") == "object"
    properties = schema.get("properties") or {}
    assert "credential_ref" in properties, f"{CLAUSE}：缺少 credential_ref：{schema}"
    assert "credential_ref" in (schema.get("required") or []), (
        f"{CLAUSE}：credential_ref 必须必填")
    nested = json.dumps(schema, ensure_ascii=False)
    for field in ("model", "context", "evidence_ids", "budget"):
        assert field in nested, f"{CLAUSE}：schema 缺少控制字段 {field}"
    merged = module.inject_subtask_control({
        "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})
    assert "_qio_subtask" in (merged.get("properties") or {}), (
        f"{CLAUSE}：动态 subagent 参数必须注入 _qio_subtask")
    assert merged["properties"].get("query") == {"type": "string"}, (
        f"{CLAUSE}：业务参数 schema 必须原样保留")


def test_subtask_control_validation_is_strict():
    """§14.2：必填 / 严格校验；未知字段、类型不符一律失败。"""
    module = _subagent_module()
    Control = module.SubtaskControl
    valid = Control.model_validate({"credential_ref": "cred_ok"})
    assert valid.credential_ref == "cred_ok"
    invalid_cases = [
        {},
        {"credential_ref": ""},
        {"credential_ref": "c", "model": 123},
        {"credential_ref": "c", "budget": "lots"},
        {"credential_ref": "c", "unknown_control": True},
        {"credential_ref": "c", "evidence_ids": "not-a-list"},
    ]
    for payload in invalid_cases:
        with pytest.raises(Exception):
            Control.model_validate(payload)
    with pytest.raises(module.SubtaskControlError):
        module.split_subtask_control({"_qio_subtask": "not-an-object"})
    with pytest.raises(module.SubtaskControlError):
        module.split_subtask_control({"_qio_subtask": {}})


def test_subtask_control_fields_are_stripped_before_business_arguments():
    """§14.2：先剥离控制字段，再把业务参数交给工具。"""
    module = _subagent_module()
    control, business = module.split_subtask_control({
        "_qio_subtask": {
            "credential_ref": "cred_ok",
            "context": {"text": "材料 A"},
            "evidence_ids": ["m1"],
            "budget": {"max_iterations": 2},
        },
        "query": "问题正文",
    })
    assert control is not None and control.credential_ref == "cred_ok"
    assert "query" in business, f"{CLAUSE}：业务参数必须保留"
    assert "_qio_subtask" not in business, (
        f"{CLAUSE}：控制字段必须被剥离，不能进入业务参数：{business}")
    # 无控制字段时业务参数原样返回
    none_control, raw_business = module.split_subtask_control({"query": "x"})
    assert none_control is None and raw_business == {"query": "x"}


def test_only_subagent_tool_accepts_subtask_control_field(ctx):
    """§14.2/§14.7：只允许 subagent 工具使用 _qio_subtask；其他工具必须拒绝。"""
    from agent.adapters.base import ToolCall

    for name in ("await_task", "read_task_result", "dev_list_tasks", "declare_completion"):
        tool = ctx.registry.get(name)
        if tool is None:
            pytest.fail(f"{CLAUSE}：工具 {name} 未注册", pytrace=False)
        assert not getattr(tool, "subtask_control_allowed", False), (
            f"{CLAUSE}：{name} 不是 subagent 工具，不得声明允许控制字段")
        arguments = {"_qio_subtask": {"credential_ref": "cred_ok"}}
        if name in ("await_task", "read_task_result"):
            arguments["task_id"] = "task_x"
        if name == "declare_completion":
            arguments["task_id"] = "ws_x"
        result = __import__("asyncio").run(ctx.registry.execute(
            ToolCall(id="call_x", name=name, arguments=arguments)))
        assert result.ok is False, (
            f"{CLAUSE}：{name} 不得接受 _qio_subtask（实际 {result}）")
        assert result.code == "unexpected_control_field", (
            f"{CLAUSE}：拒绝代码应为 unexpected_control_field（实际 {result.code}）")


def test_program_binds_original_task_and_topic_not_the_model(ctx):
    """§14.3：原任务/原话题/目标版本/授权由程序绑定；模型不能伪造。"""
    module = _subagent_module()
    schema_text = json.dumps(module.subtask_control_schema(), ensure_ascii=False)
    for forbidden in ("original_task", "original_topic", "authorization", "task_version"):
        assert forbidden not in schema_text, (
            f"{CLAUSE}：控制字段不得允许模型填写 {forbidden}（必须由程序绑定）")
    source = __import__("pathlib").Path(module.__file__).read_text(encoding="utf-8")
    assert "origin_provider" in source and "origin" in source, (
        f"{CLAUSE}：子任务必须由程序绑定 origin（原任务/原话题/版本）")
    app_source = __import__("pathlib").Path(
        _load("agent.services.app").__file__).read_text(encoding="utf-8")
    assert "set_origin_provider" in app_source, (
        f"{CLAUSE}：app 必须把程序侧的 origin 提供给子任务工具")
    notifications = _load("agent.tools.subtask_notifications")
    hint = notifications.completion_system_hint()
    prompts = _load("agent.prompts")
    assert prompts.PROMPTS["SUBTASK_COMPLETION_SYSTEM"] in hint, (
        f"{CLAUSE}：完成通知必须使用 SUBTASK_COMPLETION_SYSTEM 单源")
    assert prompts.PROMPTS["SUBTASK_RETURN_POLICY"] in hint, (
        f"{CLAUSE}：完成通知必须使用 SUBTASK_RETURN_POLICY 单源")


def test_subagent_system_prompt_is_injected_into_subagent_loop(ctx):
    """§14.3：子任务执行者使用 SUBAGENT_SYSTEM，而不是主对话 SYSTEM_PROMPT。"""
    import pathlib

    prompts = _load("agent.prompts")
    module = _subagent_module()
    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    assert "SUBAGENT_SYSTEM" in source, (
        f"{CLAUSE}：子任务循环必须注入 prompts.SUBAGENT_SYSTEM（当前源码未引用）")
    assert "SUBAGENT_SYSTEM" in prompts.PROMPTS
    assert "不假设拥有" in prompts.PROMPTS["SUBAGENT_SYSTEM"] or (
        "完整对话" in prompts.PROMPTS["SUBAGENT_SYSTEM"])