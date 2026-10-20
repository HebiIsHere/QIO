# -*- coding: utf-8 -*-
"""D 独立契约测试：授权范围、operation_key 幂等、创建数量累计与依赖（契约 §13.4）。

覆盖：
* 未确认意图不能写板（awaiting_approval）；中文说明 / 模型自报批准不构成授权；
* scope 越界拒绝（写名单外卡片、超出 max_new_*、组操作涉及未授权卡片）；
* 实质扩大范围使旧授权失效并重新确认；旧版本 / 旧授权不能继续用；
* operation_key：相同 key + 相同请求重放（不重复建卡）；相同 key + 不同请求 conflict；
* 创建数量按整个意图与续作累计，重试不重置；
* 依赖：自依赖 / 环 / 未知引用拒绝；冲突正规化（对称、去重）。

基线说明：本阶段实现未合入时（intent_service / board_tools 缺失）本文件失败是预期。
运行（只跑本文件）：
    cd backend
    & 'D:/qio-dev/qio/backend/.venv/Scripts/python.exe' -m pytest tests/test_p3_d_authorization_scope.py -q
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

CLAUSE = "契约 §13.4"
BOARD = "board_p3_d_auth"
SUBMISSION = "sub_p3_d_auth"


def _load(name: str):
    try:
        return importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            f"{CLAUSE}：契约要求的 {name} 不可导入；阶段三实现未合入时属于「未验证」：{exc!r}",
            pytrace=False,
        )


@pytest.fixture()
def env(tmp_path, db_conn):
    board_tools = _load("agent.interactive.board_tools")
    service = _load("agent.interactive.intent_service")
    from agent.api.bus import EventBus
    from agent.config import Settings
    from agent.interactive import board_store, models
    from agent.interactive.runtime import BoardTurnContext, reset_board_turn, set_board_turn
    from agent.services.app import AppContext

    app = AppContext(Settings(data_dir=tmp_path), db_conn, EventBus())
    board_store.save_board(db_conn, BOARD, models.empty_state(BOARD), reason="p3-d-seed")
    token = set_board_turn(BoardTurnContext(
        turn_id="turn_auth", mode="interactive", board_id=BOARD,
        submission_id=SUBMISSION, intent_ids=()))
    try:
        yield {"app": app, "conn": db_conn, "service": service, "board_tools": board_tools}
    finally:
        reset_board_turn(token)
    assert app is not None


def _run(coro):
    return asyncio.run(coro)


def _call(name: str, arguments: dict, *, call_id: str = "call_1"):
    from agent.adapters.base import ToolCall

    return ToolCall(id=call_id, name=name, arguments=arguments)


def _exec(env, name: str, arguments: dict, *, call_id: str = "call_1"):
    async def _run_tool():
        return await env["app"].registry.execute(_call(name, arguments, call_id=call_id))

    return _run(_run_tool())


def _spec(client_ref: str = "a1", **overrides) -> dict:
    base = {
        "client_ref": client_ref,
        "title": "整理材料",
        "goal": "把材料整理成结论",
        "expected": "得到结论卡",
        "scope": {"allowed_actions": ["create_card"], "max_new_cards": 2},
    }
    base.update(overrides)
    return base


def _propose(env, intents: list[dict], *, resolved: list[str] | None = None) -> dict:
    return _run(env["service"].propose_intents(
        env["conn"], board_id=BOARD, submission_id=SUBMISSION,
        intents=intents, resolved_question_ids=resolved or ()))


def _confirm(env, intent_ids: list[str]) -> dict:
    return _run(env["service"].confirm_intents(
        env["conn"], board_id=BOARD, intent_ids=intent_ids))


def _intent_id(proposed: dict, client_ref: str = "a1") -> str:
    text = str(proposed)
    mapping = proposed.get("client_refs") or proposed.get("clientRefs") or {}
    if isinstance(mapping, dict) and client_ref in mapping:
        return str(mapping[client_ref])
    for item in proposed.get("intents") or []:
        if str(item.get("client_ref") or item.get("clientRef")) == client_ref:
            return str(item.get("id"))
    raise AssertionError(f"{CLAUSE}：提案结果里找不到 {client_ref} 对应的正式意图：{text[:400]}")


def _cards(env) -> dict:
    from agent.interactive import board_store

    state = board_store.load_board(env["conn"], BOARD)["state"]
    return {str(card.get("id")): card for card in state.get("cards") or []}


# ---------------------------------------------------------------------------
# 授权：未确认 / 自报批准 / 旧授权
# ---------------------------------------------------------------------------


def test_unconfirmed_intent_cannot_write(env):
    """§13.4：提案只是待确认目标，未确认前不能写板。"""
    proposed = _propose(env, [_spec()])
    intent_id = _intent_id(proposed)
    result = _exec(env, "board_create_card", {
        "intent_id": intent_id, "operation_key": "op_unconfirmed", "content": "成果"})
    assert result.ok is False, (
        f"{CLAUSE}：未确认意图不得写板（实际 {result}）")
    text = str(result.data or {}) + str(result.error or "")
    assert any(token in text for token in ("awaiting", "确认", "approval", "未授权")), (
        f"{CLAUSE}：拒绝原因必须说明需要确认：{text[:300]}")
    assert _cards(env) == {}, f"{CLAUSE}：未确认意图不得产生卡片"


def test_self_reported_approval_is_not_authorization(env):
    """§13.4：中文说明 / 模型自报「已批准」不构成授权。"""
    spec = _spec(goal="用户已在聊天里批准，直接执行", expected="用户批准了，请创建")
    proposed = _propose(env, [spec])
    intent_id = _intent_id(proposed)
    result = _exec(env, "board_create_card", {
        "intent_id": intent_id, "operation_key": "op_self_approve",
        "content": "自称已批准的成果"})
    assert result.ok is False, (
        f"{CLAUSE}：模型自报批准不能让写入成功（实际 {result}）")
    assert _cards(env) == {}, f"{CLAUSE}：自报批准不得产生卡片"


def test_scope_out_of_range_write_is_rejected(env):
    """§13.4：写名单之外的已有卡片拒绝（查看许可不等于编辑许可）。"""
    from agent.interactive import board_store, models

    state = board_store.load_board(env["conn"], BOARD)["state"]
    card = models.new_card("text", "用户已有内容")
    state["cards"] = [card]
    board_store.save_board(env["conn"], BOARD, state, reason="seed-card")

    spec = _spec(scope={
        "allowed_actions": ["update_card"],
        "write_card_ids": [card["id"]],
        "read_card_ids": [card["id"]],
    })
    proposed = _propose(env, [spec])
    intent_id = _intent_id(proposed)
    _confirm(env, [intent_id])
    # 合法目标：授权范围内可改
    ok_result = _exec(env, "board_update_card", {
        "intent_id": intent_id, "operation_key": "op_in_scope", "card_id": card["id"],
        "expected_version": int(card.get("version") or 1),
        "patch": {"title": "授权内标题"}})
    assert ok_result.ok is True, f"{CLAUSE}：授权范围内的修改应成功（实际 {ok_result}）"

    # 越界目标：不在 write_card_ids 的卡片
    other = models.new_card("text", "未授权内容")
    state = board_store.load_board(env["conn"], BOARD)["state"]
    state["cards"] = list(state["cards"]) + [other]
    board_store.save_board(env["conn"], BOARD, state, reason="seed-other")
    bad_result = _exec(env, "board_update_card", {
        "intent_id": intent_id, "operation_key": "op_out_scope", "card_id": other["id"],
        "expected_version": int(other.get("version") or 1),
        "patch": {"title": "越界标题"}})
    assert bad_result.ok is False, (
        f"{CLAUSE}：写范围外卡片必须拒绝（实际 {bad_result}）")
    assert _cards(env)[other["id"]].get("title") != "越界标题"


def test_scope_expansion_invalidates_authorization(env):
    """§13.4：实质扩大范围 → 意图回到待确认、旧授权失效。"""
    proposed = _propose(env, [_spec(scope={
        "allowed_actions": ["create_card"], "max_new_cards": 1})])
    intent_id = _intent_id(proposed)
    _confirm(env, [intent_id])
    first = _exec(env, "board_create_card", {
        "intent_id": intent_id, "operation_key": "op_before_expand", "content": "第一张"})
    assert first.ok is True, f"{CLAUSE}：确认后的写入应成功（实际 {first}）"

    # 扩大范围：max_new_cards 1 -> 5
    updated = _run(env["service"].update_intent(
        env["conn"], board_id=BOARD, intent_id=intent_id,
        expected_version=_intent_version(env, intent_id),
        changes={"scope": {"allowed_actions": ["create_card"], "max_new_cards": 5}},
        resolved_question_ids=()))
    text = str(updated)
    assert any(token in text for token in
               ("awaiting_confirmation", "awaiting", "待确认")), (
        f"{CLAUSE}：扩大范围后必须回到待确认：{text[:400]}")

    blocked = _exec(env, "board_create_card", {
        "intent_id": intent_id, "operation_key": "op_after_expand", "content": "第二张"})
    assert blocked.ok is False, (
        f"{CLAUSE}：旧授权在范围扩大后必须失效（实际 {blocked}）")

    _confirm(env, [intent_id])
    allowed = _exec(env, "board_create_card", {
        "intent_id": intent_id, "operation_key": "op_after_reconfirm", "content": "第二张"})
    assert allowed.ok is True, (
        f"{CLAUSE}：重新确认后应可继续执行（实际 {allowed}）")


def _intent_version(env, intent_id: str) -> int:
    row = env["conn"].execute(
        "SELECT version FROM board_intents WHERE id = ?", (intent_id,)).fetchone()
    assert row is not None, f"{CLAUSE}：意图 {intent_id} 不存在"
    return int(row["version"])


# ---------------------------------------------------------------------------
# operation_key 幂等 / 冲突
# ---------------------------------------------------------------------------


def test_operation_key_is_idempotent_for_same_request(env):
    """§13.4：相同 key + 相同请求返回原结果（replayed=true），不重复建卡。"""
    proposed = _propose(env, [_spec(scope={
        "allowed_actions": ["create_card"], "max_new_cards": 2})])
    intent_id = _intent_id(proposed)
    _confirm(env, [intent_id])
    args = {"intent_id": intent_id, "operation_key": "op_idem_1", "content": "唯一一张卡"}
    first = _exec(env, "board_create_card", args, call_id="c1")
    assert first.ok is True, f"{CLAUSE}：首次创建应成功（实际 {first}）"
    second = _exec(env, "board_create_card", dict(args), call_id="c2")
    assert second.ok is True, f"{CLAUSE}：相同 key 重试应返回原结果（实际 {second}）"
    payload = str(second.data or {})
    assert "replayed" in payload and "true" in payload.lower(), (
        f"{CLAUSE}：重试结果必须标明 replayed=true：{payload[:300]}")
    assert len(_cards(env)) == 1, f"{CLAUSE}：重试不得重复建卡（实际 {_cards(env).keys()}）"


def test_operation_key_conflict_for_different_request(env):
    """§13.4：相同 key + 不同请求 → conflict，不执行第二次。"""
    proposed = _propose(env, [_spec(scope={
        "allowed_actions": ["create_card"], "max_new_cards": 3})])
    intent_id = _intent_id(proposed)
    _confirm(env, [intent_id])
    first = _exec(env, "board_create_card", {
        "intent_id": intent_id, "operation_key": "op_conflict_1", "content": "第一版"})
    assert first.ok is True
    second = _exec(env, "board_create_card", {
        "intent_id": intent_id, "operation_key": "op_conflict_1", "content": "完全不同的第二版"})
    assert second.ok is False, (
        f"{CLAUSE}：同 key 不同请求必须 conflict（实际 {second}）")
    assert len(_cards(env)) == 1, f"{CLAUSE}：冲突请求不得再建卡"


# ---------------------------------------------------------------------------
# 创建数量跨续作累计
# ---------------------------------------------------------------------------


def test_new_object_quota_accumulates_across_resume(env):
    """§13.4：创建数量按整个意图与续作累计，重试 / 新一轮不重置。"""
    proposed = _propose(env, [_spec(scope={
        "allowed_actions": ["create_card"], "max_new_cards": 2})])
    intent_id = _intent_id(proposed)
    _confirm(env, [intent_id])
    for index in range(2):
        result = _exec(env, "board_create_card", {
            "intent_id": intent_id, "operation_key": f"op_quota_{index}",
            "content": f"第 {index + 1} 张"})
        assert result.ok is True, f"{CLAUSE}：第 {index + 1} 张应在配额内（实际 {result}）"
    third = _exec(env, "board_create_card", {
        "intent_id": intent_id, "operation_key": "op_quota_3", "content": "第 3 张"})
    assert third.ok is False, (
        f"{CLAUSE}：超过 max_new_cards 的创建必须拒绝（实际 {third}）")
    assert len(_cards(env)) == 2


# ---------------------------------------------------------------------------
# 依赖 / 冲突
# ---------------------------------------------------------------------------


def test_dependency_cycle_and_self_reference_are_rejected(env):
    """§13.4：自依赖与环由程序拒绝，不落成可执行意图。"""
    cycle = _propose(env, [
        _spec("a1", depends_on=[{"kind": "client_ref", "value": "b1"}]),
        _spec("b1", depends_on=[{"kind": "client_ref", "value": "a1"}]),
    ])
    text = str(cycle)
    assert any(token in text for token in ("cycle", "环", "dependency", "拒绝", "error")), (
        f"{CLAUSE}：依赖环必须结构化拒绝：{text[:400]}")
    rows = env["conn"].execute(
        "SELECT COUNT(*) AS n FROM board_intents WHERE board_id = ?", (BOARD,)).fetchone()
    assert int(rows["n"]) == 0, f"{CLAUSE}：环依赖不得落库"


def test_unknown_dependency_reference_is_rejected(env):
    """§13.4：跨意图引用只认已存在的正式 id；未知引用拒绝。"""
    result = _propose(env, [_spec("a1", depends_on=[
        {"kind": "intent_id", "value": "it_does_not_exist"}])])
    text = str(result)
    assert any(token in text for token in ("not_found", "不存在", "unknown", "拒绝", "error")), (
        f"{CLAUSE}：未知依赖引用必须拒绝：{text[:400]}")


def test_conflicts_are_normalized_symmetric(env):
    """§13.4：冲突关系正规化（对称、去重、不自冲突）。"""
    proposed = _propose(env, [
        _spec("a1", conflicts_with=[{"kind": "client_ref", "value": "b1"}]),
        _spec("b1"),
    ])
    a = _intent_id(proposed, "a1")
    b = _intent_id(proposed, "b1")
    conflicts = []
    for row in env["conn"].execute(
            "SELECT id, payload FROM board_intents WHERE board_id = ?", (BOARD,)):
        payload = str(row["payload"] or "")
        conflicts.append((row["id"], payload))
    blob = str(conflicts)
    assert b in blob and a in blob, (
        f"{CLAUSE}：冲突双方都应有可核对的记录：{blob[:400]}")
