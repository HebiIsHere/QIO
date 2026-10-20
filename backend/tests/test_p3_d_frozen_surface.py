# -*- coding: utf-8 -*-
"""D 独立契约测试：阶段三冻结面（契约 §13.2）。

口径：只检查冻结面（提示词键与硬规则文字、12 个工具参数模型、意图 / 范围 / 位置结构、
冻结上限、可信运行上下文），不检查实现内部函数。基线（f80a5fa8）上本文件应全绿；
它防止整合期间冻结面被改写。

运行（只跑本文件）：
    cd backend
    & 'D:/qio-dev/qio/backend/.venv/Scripts/python.exe' -m pytest tests/test_p3_d_frozen_surface.py -q
"""

from __future__ import annotations

import pytest

CLAUSE = "契约 §13.2"

BOARD_TOOLS = (
    "board_inspect",
    "board_read_cards",
    "board_propose_intents",
    "board_update_intent",
    "board_wait_for_input",
    "board_create_card",
    "board_update_card",
    "board_move_card",
    "board_group_cards",
    "board_order_group",
    "board_add_link",
    "board_finish_intent",
)


# ---------------------------------------------------------------------------
# 提示词：固定文案只在 prompts.py（不做全文快照）
# ---------------------------------------------------------------------------


def test_interactive_policy_exists_and_contains_hard_rules():
    """§13.2：INTERACTIVE_POLICY 是模式规则唯一来源，含关键硬规则。"""
    from agent import prompts

    assert "INTERACTIVE_POLICY" in prompts.PROMPTS, f"{CLAUSE}：缺少 INTERACTIVE_POLICY"
    text = prompts.PROMPTS["INTERACTIVE_POLICY"]
    assert isinstance(text, str) and text.strip()
    for phrase in (
        "板面身份、提交身份、查看权限、意图状态及授权范围以程序提供的数据为准",
        "使用 board_wait_for_input 记录等待",
        "系统已将问题作为正常对话展示时，不再重复发送同一问题",
        "用户回答后继续原提交",
        "通过 resolved_question_ids 明确解除对应等待",
        "实质扩大目标、对象、数量、成本或影响范围时更新意图并重新确认",
        "保存或编辑本身不自动构成新的提交",
        "失败时保留已经完成且有用的成果",
        "通过 board_finish_intent 明确本次执行结果",
        "自然语言中的“完成”不能代替程序状态",
    ):
        assert phrase in text, f"{CLAUSE}：INTERACTIVE_POLICY 缺少硬规则「{phrase}」"


def test_twelve_board_tool_descriptions_are_frozen():
    """§13.2：12 个板面工具说明只在 prompts.TOOL_DESCRIPTIONS，且不为空。"""
    from agent.prompts import TOOL_DESCRIPTIONS

    for name in BOARD_TOOLS:
        assert name in TOOL_DESCRIPTIONS, f"{CLAUSE}：TOOL_DESCRIPTIONS 缺少 {name}"
        text = TOOL_DESCRIPTIONS[name]
        assert isinstance(text, str) and len(text.strip()) >= 10, (
            f"{CLAUSE}：{name} 的说明为空/过短")


def test_board_argument_descriptions_are_frozen():
    """§13.2：板面参数文案进入 ARGUMENT_DESCRIPTIONS（唯一来源）。"""
    from agent.prompts import ARGUMENT_DESCRIPTIONS

    for key in (
        "submission_id", "intents", "intent_id", "changes", "card_ids", "source",
        "card_id", "patch", "group_id", "group_ids", "member_order", "arrange",
        "src", "dst", "direction", "meaning", "outcome", "summary", "operation_keys",
        "missing_fields", "question", "resolved_question_ids", "position", "scope",
        "expected_version", "expected_versions", "operation_key",
    ):
        assert key in ARGUMENT_DESCRIPTIONS, f"{CLAUSE}：ARGUMENT_DESCRIPTIONS 缺少 {key}"


# ---------------------------------------------------------------------------
# 12 个工具参数模型 / 冻结上限 / schema 同源
# ---------------------------------------------------------------------------


def test_tool_arg_models_are_exactly_the_twelve_tools():
    """§13.2：TOOL_ARG_MODELS 恰好覆盖 12 个板面工具。"""
    from agent.interactive import tool_models

    assert tuple(tool_models.TOOL_ARG_MODELS) == BOARD_TOOLS, (
        f"{CLAUSE}：TOOL_ARG_MODELS 与 12 个工具不一致：{list(tool_models.TOOL_ARG_MODELS)}")


def test_models_forbid_extra_fields_and_schemas_are_generated():
    """§13.2：参数模型 extra=forbid；模型可见 schema 由 json_schema_for 生成。"""
    from agent.tools.contracts import json_schema_for

    from agent.interactive import tool_models

    schemas = tool_models.tool_argument_schemas()
    assert set(schemas) == set(BOARD_TOOLS)
    for name, model in tool_models.TOOL_ARG_MODELS.items():
        assert model.model_config.get("extra") == "forbid", (
            f"{CLAUSE}：{name} 的参数模型必须 extra=forbid")
        assert schemas[name] == json_schema_for(model), (
            f"{CLAUSE}：{name} 的展示 schema 必须由 json_schema_for 从同一模型生成")
    spec_schema = tool_models.intent_spec_schema()
    assert spec_schema == json_schema_for(tool_models.IntentSpec)
    assert {"client_ref", "title", "goal", "expected", "scope"} <= set(
        spec_schema.get("properties") or {})


def test_frozen_limits_and_enums():
    """§13.2：冻结上限 / 动作枚举 / 意图状态。"""
    from agent.interactive import tool_models as m

    assert m.MAX_INTENTS_PER_PROPOSAL == 10
    assert m.MAX_CARD_IDS == 64
    assert m.MAX_IDS == 128
    assert m.MAX_REF_IDS == 64
    assert m.MAX_OPERATION_KEYS == 200
    assert m.MAX_NEW_CARDS_PER_INTENT == 40
    assert m.MAX_QUESTION_CHARS == 500
    assert m.MAX_CONTENT_CHARS == 200_000
    assert m.MIN_SIZE == 1.0
    assert m.MAX_SIZE == 20_000.0
    assert tuple(action.value for action in m.BoardAction) == m.ALLOWED_BOARD_ACTIONS
    assert set(m.INTENT_STATUSES) == {
        "proposed", "awaiting_confirmation", "approved", "running", "paused",
        "completed", "partially_completed", "failed", "cancelled", "rejected",
    }
    assert m.READ_ONLY_TOOLS == ("board_inspect", "board_read_cards")


# ---------------------------------------------------------------------------
# 意图 / 范围 / 位置结构：本地强制执行
# ---------------------------------------------------------------------------


def _spec(**overrides):
    base = {
        "client_ref": "a1",
        "title": "整理材料",
        "goal": "把材料整理成结论",
        "expected": "得到一张结论卡",
        "scope": {"allowed_actions": ["create_card"], "max_new_cards": 1},
    }
    base.update(overrides)
    return base


def test_intent_spec_requires_program_checkable_identity_and_scope():
    """§13.4：client_ref 是程序可校验的临时身份；目标 / 预期 / 范围必须齐全。"""
    from pydantic import ValidationError

    from agent.interactive.tool_models import IntentSpec

    spec = IntentSpec.model_validate(_spec())
    assert spec.client_ref == "a1"
    for field in ("title", "goal", "expected"):
        with pytest.raises(ValidationError):
            IntentSpec.model_validate(_spec(**{field: ""}))
    for bad_ref in ("A1", "1abc", "a 1", "a/1", ""):
        with pytest.raises(ValidationError):
            IntentSpec.model_validate(_spec(client_ref=bad_ref))


def test_intent_spec_rejects_self_dependency_and_duplicates():
    """§13.4：不允许自依赖 / 重复依赖 / 自冲突 / 重复冲突。"""
    from pydantic import ValidationError

    from agent.interactive.tool_models import IntentSpec

    with pytest.raises(ValidationError):
        IntentSpec.model_validate(_spec(depends_on=[{"kind": "client_ref", "value": "a1"}]))
    with pytest.raises(ValidationError):
        IntentSpec.model_validate(_spec(depends_on=[
            {"kind": "client_ref", "value": "b1"},
            {"kind": "client_ref", "value": "b1"},
        ]))
    with pytest.raises(ValidationError):
        IntentSpec.model_validate(_spec(conflicts_with=[{"kind": "client_ref", "value": "a1"}]))
    with pytest.raises(ValidationError):
        IntentSpec.model_validate(_spec(conflicts_with=[
            {"kind": "client_ref", "value": "b1"},
            {"kind": "client_ref", "value": "b1"},
        ]))


def test_intent_scope_bounds_are_enforced():
    """§13.4：创建数量上限由程序执行；写范围必须同时声明写入动作。"""
    from pydantic import ValidationError

    from agent.interactive.tool_models import IntentScope

    with pytest.raises(ValidationError):
        IntentScope.model_validate({"max_new_cards": 41})
    with pytest.raises(ValidationError):
        IntentScope.model_validate({"max_new_cards": -1})
    with pytest.raises(ValidationError):
        IntentScope.model_validate({
            "write_card_ids": ["c1"], "allowed_actions": ["create_card"]})
    ok = IntentScope.model_validate({
        "write_card_ids": ["c1"], "allowed_actions": ["update_card"], "max_new_cards": 2})
    assert ok.max_new_cards == 2
    with pytest.raises(ValidationError):
        IntentScope.model_validate({"allowed_actions": ["delete_card"]})
    with pytest.raises(ValidationError):
        IntentScope.model_validate({"read_card_ids": ["", "c1"]})


def test_structural_actions_validate_and_dedupe():
    """§13.2/§13.4（DEFECT-P3-2 回归）：结构类动作可单独 / 组合校验，重复动作被正规化。

    覆盖 group_cards / order_group / add_link 单独、组合与完整六动作；
    这条用例在 Lead 修复「list 与 set 求交」前会失败，修复后必须保持通过。
    """
    from pydantic import ValidationError

    from agent.interactive.tool_models import (
        ALLOWED_BOARD_ACTIONS,
        BoardAction,
        IntentScope,
        IntentSpec,
    )

    for name in ("group_cards", "order_group", "add_link"):
        scope = IntentScope.model_validate({"allowed_actions": [name]})
        assert [action.value for action in scope.allowed_actions] == [name]

    full = IntentScope.model_validate({
        "allowed_actions": list(ALLOWED_BOARD_ACTIONS) + ["create_card", "create_card"],
        "max_new_cards": 2,
    })
    assert [action.value for action in full.allowed_actions] == list(ALLOWED_BOARD_ACTIONS)
    assert all(isinstance(action, BoardAction) for action in full.allowed_actions)

    spec = IntentSpec.model_validate(_spec(scope={
        "allowed_actions": list(ALLOWED_BOARD_ACTIONS),
        "max_new_cards": 2,
        "max_new_groups": 1,
        "max_new_links": 1,
    }))
    assert {action.value for action in spec.scope.allowed_actions} == set(ALLOWED_BOARD_ACTIONS)

    with pytest.raises(ValidationError):
        IntentScope.model_validate({"allowed_actions": ["create_card", "delete_card"]})


def test_position_rules_are_enforced():
    """§13.4：位置 absolute / relative 二选一；有限数字、正尺寸、非负间距。"""
    from pydantic import ValidationError

    from agent.interactive.tool_models import BoardCreateCardArgs, Position

    with pytest.raises(ValidationError):
        Position.model_validate({"kind": "absolute", "x": 1})
    with pytest.raises(ValidationError):
        Position.model_validate({"kind": "relative"})
    with pytest.raises(ValidationError):
        Position.model_validate({"kind": "absolute", "x": 1, "y": 2, "relative_to": "c1"})
    with pytest.raises(ValidationError):
        Position.model_validate({"kind": "relative", "relative_to": "c1", "x": 1})
    with pytest.raises(ValidationError):
        Position.model_validate({"kind": "relative", "relative_to": "c1", "side": "right", "gap": -1})
    with pytest.raises(ValidationError):
        Position.model_validate({"kind": "absolute", "x": float("inf"), "y": 0})
    with pytest.raises(ValidationError):
        Position.model_validate({"kind": "absolute", "x": 2_000_000, "y": 0})
    assert Position.model_validate({"kind": "absolute", "x": 1, "y": 2}).kind == "absolute"
    assert Position.model_validate(
        {"kind": "relative", "relative_to": "c1", "side": "right", "gap": 12}).gap == 12

    with pytest.raises(ValidationError):
        BoardCreateCardArgs.model_validate({
            "intent_id": "i1", "operation_key": "k1", "content": "x", "width": 0})
    with pytest.raises(ValidationError):
        BoardCreateCardArgs.model_validate({
            "intent_id": "i1", "operation_key": "k1", "content": "x", "height": -5})


def test_content_rules_reject_empty_and_allow_long_markdown():
    """§13.5：正文必须完整非空；上限 200000 字符（长 Markdown 不被静默截断）。"""
    from pydantic import ValidationError

    from agent.interactive.tool_models import BoardCreateCardArgs, BoardUpdateCardArgs

    # 结构层只强制「非空字符串」；纯空白由工具语义层拒绝（见长正文 / 空正文集成用例）
    with pytest.raises(ValidationError):
        BoardCreateCardArgs.model_validate({
            "intent_id": "i1", "operation_key": "k1", "content": ""})
    long_markdown = "# 标题\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n" + "内容" * 3000
    args = BoardCreateCardArgs.model_validate({
        "intent_id": "i1", "operation_key": "k1", "content": long_markdown})
    assert args.content == long_markdown
    with pytest.raises(ValidationError):
        BoardUpdateCardArgs.model_validate({
            "intent_id": "i1", "operation_key": "k1", "card_id": "c1",
            "expected_version": 1, "patch": {"content": ""}})
    with pytest.raises(ValidationError):
        BoardUpdateCardArgs.model_validate({
            "intent_id": "i1", "operation_key": "k1", "card_id": "c1",
            "expected_version": 1, "patch": {"unknown": 1}})
    with pytest.raises(ValidationError):
        BoardUpdateCardArgs.model_validate({
            "intent_id": "i1", "operation_key": "k1", "card_id": "c1",
            "expected_version": 1, "patch": {}})


def test_links_finish_and_update_intent_shapes():
    """§13.4：自连接拒绝；finish outcome 枚举；update_intent 字段白名单。"""
    from pydantic import ValidationError

    from agent.interactive.tool_models import (
        BoardAddLinkArgs,
        BoardFinishIntentArgs,
        BoardUpdateIntentArgs,
    )

    with pytest.raises(ValidationError):
        BoardAddLinkArgs.model_validate({
            "intent_id": "i1", "operation_key": "k1", "src": "c1", "dst": "c1"})
    with pytest.raises(ValidationError):
        BoardFinishIntentArgs.model_validate({
            "intent_id": "i1", "expected_version": 1, "outcome": "done", "summary": "s"})
    finish = BoardFinishIntentArgs.model_validate({
        "intent_id": "i1", "expected_version": 1, "outcome": "partially_completed",
        "summary": "完成一半", "operation_keys": ["k1", "k1"]})
    assert finish.operation_keys == ["k1"]
    with pytest.raises(ValidationError):
        BoardUpdateIntentArgs.model_validate({
            "intent_id": "i1", "expected_version": 1,
            "changes": {"status": "approved"}})
    with pytest.raises(ValidationError):
        BoardUpdateIntentArgs.model_validate({
            "intent_id": "i1", "expected_version": 1})
    ok = BoardUpdateIntentArgs.model_validate({
        "intent_id": "i1", "expected_version": 1, "changes": {"title": "新标题"}})
    assert ok.changes == {"title": "新标题"}


# ---------------------------------------------------------------------------
# 可信运行上下文（runtime.py）
# ---------------------------------------------------------------------------


def test_board_turn_context_is_frozen_and_bounded():
    """§13.2：BoardTurnContext 字段冻结；interactive 需要互动模式 + 板面身份。"""
    import dataclasses

    from agent.interactive.runtime import BoardTurnContext

    fields = {f.name for f in dataclasses.fields(BoardTurnContext)}
    assert fields == {
        "turn_id", "mode", "board_id", "submission_id", "topic_id", "attempt",
        "intent_ids", "authorized_scopes",
    }
    assert dataclasses.is_dataclass(BoardTurnContext)
    context = BoardTurnContext(turn_id="t1", mode="interactive", board_id="b1")
    assert context.interactive is True
    assert BoardTurnContext(turn_id="t1", mode="normal", board_id="b1").interactive is False
    assert BoardTurnContext(turn_id="t1", mode="interactive").interactive is False


def test_require_board_turn_rejects_missing_and_normal_mode():
    """§13.2：普通模式 / 无绑定 → interactive_unavailable，工具不得执行。"""
    from agent.interactive.runtime import (
        BoardToolUnavailable,
        BoardTurnContext,
        require_board_turn,
        reset_board_turn,
        set_board_turn,
    )

    with pytest.raises(BoardToolUnavailable) as excinfo:
        require_board_turn()
    assert excinfo.value.code == "interactive_unavailable"

    token = set_board_turn(BoardTurnContext(turn_id="t1", mode="normal", board_id="b1"))
    try:
        with pytest.raises(BoardToolUnavailable):
            require_board_turn()
    finally:
        reset_board_turn(token)

    token = set_board_turn(BoardTurnContext(turn_id="t2", mode="interactive", board_id="b2"))
    try:
        assert require_board_turn().board_id == "b2"
    finally:
        reset_board_turn(token)
    # 复位之后不可用（不把上一轮绑定泄漏到下一轮）
    with pytest.raises(BoardToolUnavailable):
        require_board_turn()


def test_delivered_questions_are_buffered_and_taken_once():
    """§13.2：问题由工具参数写出；轮末取走一次后清空（不重复呈现）。"""
    from agent.interactive import runtime

    runtime.clear_delivered_questions()
    runtime.record_delivered_question("还缺项目截止时间吗？", missing_fields=["deadline"])
    runtime.record_delivered_question("需要指定负责人吗？")
    taken = runtime.take_delivered_questions()
    assert [item["question"] for item in taken] == [
        "还缺项目截止时间吗？", "需要指定负责人吗？"]
    assert taken[0]["missing_fields"] == ["deadline"]
    assert runtime.take_delivered_questions() == []
