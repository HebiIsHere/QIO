# -*- coding: utf-8 -*-
"""D 独立契约测试：12 个板面工具只在互动模式暴露、只从可信运行上下文取板面（契约 §13.4）。

覆盖：
* 12 个工具已由 Lead 注册（services/app.py -> build_board_tools），说明与参数模型来自冻结面；
* 普通模式 / 无绑定：板面工具返回 interactive_unavailable，不产生任何板面修改；
* 模型传入 board_id / mode 越界无效（参数模型 extra=forbid + 工具不采信模型自报身份）；
* 互动模式 + 可信上下文：读工具返回真实板面，未获准读取的对象不可读；
* 只读工具的并发标记只对 READ_ONLY_TOOLS 为真。

基线说明：本阶段实现未合入时（build_board_tools / 注册接线缺失）本文件失败是预期，
失败信息写明契约条款。运行（只跑本文件）：
    cd backend
    & 'D:/qio-dev/qio/backend/.venv/Scripts/python.exe' -m pytest tests/test_p3_d_tools_registry.py -q
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

CLAUSE = "契约 §13.4"
BOARD = "board_p3_d_tools"


def _load(name: str):
    try:
        return importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            f"{CLAUSE}：契约要求的 {name} 不可导入；阶段三实现未合入时属于「未验证」：{exc!r}",
            pytrace=False,
        )


@pytest.fixture()
def app_ctx(tmp_path, db_conn):
    _load("agent.interactive.board_tools")
    from agent.api.bus import EventBus
    from agent.config import Settings
    from agent.services.app import AppContext

    return AppContext(Settings(data_dir=tmp_path), db_conn, EventBus())


def _seed_board(conn, board_id: str = BOARD) -> None:
    from agent.interactive import board_store, models

    board_store.ensure_board(conn, board_id=board_id)
    board_store.save_board(conn, board_id, models.empty_state(board_id), reason="p3-d-seed")


def _call(name: str, arguments: dict, *, call_id: str = "call_1"):
    from agent.adapters.base import ToolCall

    return ToolCall(id=call_id, name=name, arguments=arguments)


def _exec(app_ctx, name: str, arguments: dict, *, call_id: str = "call_1"):
    async def _run():
        return await app_ctx.registry.execute(_call(name, arguments, call_id=call_id))

    return asyncio.run(_run())


def _valid_args(board_id: str = BOARD) -> dict[str, dict]:
    """结构合法的最小参数（不依赖真实对象；普通模式应在 runtime 检查处拒绝）。"""
    spec = {
        "client_ref": "a1",
        "title": "整理材料",
        "goal": "整理",
        "expected": "结论",
        "scope": {"allowed_actions": ["create_card"], "max_new_cards": 1},
    }
    return {
        "board_inspect": {},
        "board_read_cards": {},
        "board_propose_intents": {"submission_id": "sub_1", "intents": [spec]},
        "board_update_intent": {
            "intent_id": "it_1", "expected_version": 1, "changes": {"title": "新标题"}},
        "board_wait_for_input": {"submission_id": "sub_1", "question": "还缺什么？"},
        "board_create_card": {
            "intent_id": "it_1", "operation_key": "op_1", "content": "完整正文"},
        "board_update_card": {
            "intent_id": "it_1", "operation_key": "op_1", "card_id": "c_1",
            "expected_version": 1, "patch": {"title": "新标题"}},
        "board_move_card": {
            "intent_id": "it_1", "operation_key": "op_1", "card_id": "c_1",
            "expected_version": 1, "position": {"kind": "absolute", "x": 10, "y": 20}},
        "board_group_cards": {
            "intent_id": "it_1", "operation_key": "op_1", "card_ids": ["c_1"]},
        "board_order_group": {
            "intent_id": "it_1", "operation_key": "op_1", "group_id": "g_1",
            "member_order": ["c_1"]},
        "board_add_link": {
            "intent_id": "it_1", "operation_key": "op_1", "src": "c_1", "dst": "c_2"},
        "board_finish_intent": {
            "intent_id": "it_1", "expected_version": 1,
            "outcome": "completed", "summary": "完成"},
    }


def test_all_twelve_tools_are_registered_with_frozen_surface(app_ctx):
    """§13.4：12 个工具由 Lead 注册；description/arg_model 来自冻结面。"""
    from agent.interactive import tool_models
    from agent.prompts import TOOL_DESCRIPTIONS

    for name, model in tool_models.TOOL_ARG_MODELS.items():
        tool = app_ctx.registry.get(name)
        assert tool is not None, (
            f"{CLAUSE}：工具 {name} 未注册（Lead 接线缺失：services/app.py 应注册 "
            "build_board_tools(app)）")
        assert tool.arg_model is model, (
            f"{CLAUSE}：{name} 的 arg_model 必须来自 tool_models.TOOL_ARG_MODELS")
        assert tool.description == TOOL_DESCRIPTIONS[name], (
            f"{CLAUSE}：{name} 的 description 必须引用 prompts.TOOL_DESCRIPTIONS")
        assert tool.is_concurrency_safe is (name in tool_models.READ_ONLY_TOOLS), (
            f"{CLAUSE}：{name} 的并发安全标记必须只对 READ_ONLY_TOOLS 为真")


def test_normal_mode_rejects_all_board_tools(app_ctx):
    """§13.2/§13.4：普通模式没有可信上下文 → 全部板面工具 interactive_unavailable。"""
    for name, args in _valid_args().items():
        result = _exec(app_ctx, name, args)
        assert result.ok is False, (
            f"{CLAUSE}：普通模式下 {name} 不得成功（实际 {result}）")
        assert (result.code or "").startswith("interactive_unavailable") or (
            (result.data or {}).get("code") == "interactive_unavailable"), (
            f"{CLAUSE}：普通模式下 {name} 必须返回 interactive_unavailable，"
            f"实际 code={result.code!r} data={result.data!r}")


def test_model_supplied_board_id_is_not_trusted(app_ctx):
    """§13.4：板面身份只来自运行上下文；模型传 board_id 一律无效。"""
    for name, args in _valid_args().items():
        forged = dict(args)
        forged["board_id"] = "board_attacker"
        result = _exec(app_ctx, name, forged)
        assert result.ok is False, (
            f"{CLAUSE}：{name} 不得接受模型传入的 board_id（实际 {result}）")
    from agent.interactive import runtime

    _seed_board(app_ctx.conn)
    token = runtime.set_board_turn(runtime.BoardTurnContext(
        turn_id="t1", mode="interactive", board_id=BOARD))
    try:
        result = _exec(app_ctx, "board_inspect", {"board_id": "board_attacker"})
        assert result.ok is False, (
            f"{CLAUSE}：互动模式下模型仍不能改板面身份（实际 {result}）")
    finally:
        runtime.reset_board_turn(token)


def test_interactive_context_reads_the_bound_board(app_ctx):
    """§13.4：互动模式下读工具只看可信上下文里的板面。"""
    from agent.interactive import board_store, models, runtime

    conn = app_ctx.conn
    _seed_board(conn)
    card = models.new_card("text", "板面上的真实内容", checked=True)
    state = board_store.load_board(conn, BOARD)["state"]
    state["cards"] = [card]
    board_store.save_board(conn, BOARD, state, reason="seed-card")

    token = runtime.set_board_turn(runtime.BoardTurnContext(
        turn_id="t1", mode="interactive", board_id=BOARD, submission_id="sub_1"))
    try:
        inspected = _exec(app_ctx, "board_inspect", {})
        assert inspected.ok is True, f"{CLAUSE}：board_inspect 应成功（实际 {inspected}）"
        payload = inspected.data or {}
        text = str(payload)
        assert BOARD in text, f"{CLAUSE}：board_inspect 必须返回当前板面身份：{text[:300]}"

        read = _exec(app_ctx, "board_read_cards", {"card_ids": [card["id"]]})
        assert read.ok is True, f"{CLAUSE}：board_read_cards 应成功（实际 {read}）"
        assert "板面上的真实内容" in str(read.data), (
            f"{CLAUSE}：读取结果必须包含真实卡片正文")
    finally:
        runtime.reset_board_turn(token)


def test_unknown_card_is_rejected(app_ctx):
    """§13.4：只能读取真实存在且获准的卡片，不编造对象。"""
    from agent.interactive import runtime

    _seed_board(app_ctx.conn)
    token = runtime.set_board_turn(runtime.BoardTurnContext(
        turn_id="t1", mode="interactive", board_id=BOARD))
    try:
        result = _exec(app_ctx, "board_read_cards", {"card_ids": ["c_ghost"]})
        assert result.ok is False, (
            f"{CLAUSE}：不存在的卡片必须拒绝（实际 {result}）")
    finally:
        runtime.reset_board_turn(token)
