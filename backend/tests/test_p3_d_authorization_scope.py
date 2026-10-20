# -*- coding: utf-8 -*-
"""D 独立契约测试：授权范围、operation_key 幂等、创建数量累计与依赖（契约 §13.4）。

覆盖：
* 未确认意图不能写板（awaiting_approval）；中文说明 / 模型自报批准不构成授权；
* scope 越界拒绝（写名单外卡片、超额创建）；实质扩大范围使旧授权失效并重新确认；
* operation_key：相同 key + 相同请求重放（不重复建卡）；相同 key + 不同请求 conflict；
* 创建数量按整个意图与续作累计，重试不重置；
* 依赖：自依赖 / 环 / 未知引用拒绝；冲突正规化（对称）。

装置：真实 DB + B 的 board_tools（只依赖 app.conn）+ 冻结 runtime 上下文；模型不参与。

基线说明：本阶段实现未合入时（board_tools / intent_service 缺失）本文件失败是预期。
运行（只跑本文件）：
    cd backend
    & 'D:/qio-dev/qio/backend/.venv/Scripts/python.exe' -m pytest tests/test_p3_d_authorization_scope.py -q
"""

from __future__ import annotations

import asyncio
import importlib
import json

import pytest

CLAUSE = "契约 §13.4"
BOARD = "board_p3_d_auth"

def _ensure_p3_schema(conn) -> None:
    """在 Lead 登记迁移 49/50/51 之前，测试库按分支迁移模块自备对象（幂等）。

    Lead 登记后 schema 已是 51，这里再 apply 一次也不会改变任何对象（幂等）。
    """
    for name in ("agent.storage.migration_p3_a", "agent.storage.migration_p3_b"):
        try:
            module = importlib.import_module(name)
        except Exception:  # noqa: BLE001 - 分支未合入时由用例自己给出契约失败信息
            continue
        apply = getattr(module, "apply", None)
        if callable(apply):
            try:
                apply(conn)
            except Exception:  # noqa: BLE001 - 对象已由 Lead 登记时允许幂等差异
                pass


@pytest.fixture(autouse=True)
def _p3_schema(db_conn):
    _ensure_p3_schema(db_conn)
    return db_conn

SUBMISSION = "sub_p3_d_auth"


def _load(name: str):
    try:
        return importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            f"{CLAUSE}：契约要求的 {name} 不可导入；阶段三实现未合入时属于「未验证」：{exc!r}",
            pytrace=False,
        )


class FakeApp:
    """板面工具只依赖 app.conn（完整 AppContext 注册由 tools_registry 用例核对）。"""

    def __init__(self, conn) -> None:
        self.conn = conn


@pytest.fixture()
def env(db_conn):
    board_tools_mod = _load("agent.interactive.board_tools")
    service = _load("agent.interactive.intent_service")
    from agent.interactive import board, board_store, models, runtime

    conn = db_conn
    board_store.ensure_board(conn, board_id=BOARD)
    conn.execute(
        "INSERT OR IGNORE INTO board_submissions (id, board_id, seq, status, created_at)"
        " VALUES (?, ?, 1, 'accepted', ?)",
        (SUBMISSION, BOARD, models.now_iso()),
    )
    conn.commit()
    app = FakeApp(conn)
    tools = {tool.name: tool for tool in board_tools_mod.build_board_tools(app)}
    token = runtime.set_board_turn(runtime.BoardTurnContext(
        turn_id="turn_auth", mode=runtime.MODE_INTERACTIVE, board_id=BOARD,
        submission_id=SUBMISSION, topic_id="topic_auth", attempt=1))
    try:
        yield {"conn": conn, "service": service, "tools": tools, "board": board,
               "board_store": board_store, "models": models}
    finally:
        runtime.clear_delivered_questions()
        runtime.reset_board_turn(token)


def _run(value):
    if asyncio.iscoroutine(value):
        return asyncio.run(value)
    return value


def _tool(env, name: str, arguments: dict):
    tool = env["tools"][name]
    assert tool is not None, f"{CLAUSE}：工具 {name} 未注册"
    return _run(tool.run(**arguments))


def _spec(client_ref: str = "a1", **overrides) -> dict:
    base = {
        "client_ref": client_ref,
        "title": "整理材料",
        "goal": "把材料整理成结论",
        "expected": "得到结论卡",
        "basis": [],
        "scope": {"allowed_actions": ["create_card"], "max_new_cards": 2},
        "depends_on": [],
        "conflicts_with": [],
    }
    base.update(overrides)
    return base


def _require_ok(result, what: str) -> None:
    assert result.get("ok") is True, (
        f"{CLAUSE}：{what} 失败（{result.get('code')}: {result.get('reason') or result.get('message')}）")


def _propose(env, intents: list[dict]) -> dict:
    return env["service"].propose_intents(
        env["conn"], board_id=BOARD, submission_id=SUBMISSION, intents=intents,
        resolved_question_ids=())


def _confirm(env, intent_ids: list[str]) -> dict:
    return env["service"].confirm_intents(env["conn"], board_id=BOARD, intent_ids=intent_ids)


def _intent_id(proposed: dict, client_ref: str = "a1") -> str:
    mapping = proposed.get("client_refs") or {}
    if isinstance(mapping, dict) and client_ref in mapping:
        return str(mapping[client_ref])
    for item in proposed.get("intents") or []:
        if str(item.get("client_ref")) == client_ref:
            return str(item.get("intent_id") or item.get("id"))
    raise AssertionError(f"{CLAUSE}：提案结果里找不到 {client_ref}：{str(proposed)[:400]}")


def _intent_version(env, intent_id: str) -> int:
    row = env["conn"].execute(
        "SELECT version FROM board_intents WHERE id = ?", (intent_id,)).fetchone()
    assert row is not None, f"{CLAUSE}：意图 {intent_id} 不存在"
    return int(row["version"])


def _cards(env) -> dict:
    state = env["board_store"].current_state(env["conn"], BOARD)
    return {str(card.get("id")): card for card in state.get("cards") or []}


def _add_card(env, content: str, *, checked: bool = True) -> str:
    state = env["board_store"].current_state(env["conn"], BOARD)
    state = env["board"].add_card(state, "text", content)
    card_id = str(state["cards"][-1]["id"])
    if checked:
        state = env["board"].set_checked(state, card_id, True)
    env["board_store"].save_board(env["conn"], BOARD, state, reason="p3-d-seed")
    return card_id


# ---------------------------------------------------------------------------
# 授权：未确认 / 自报批准 / 旧授权失效
# ---------------------------------------------------------------------------


def test_unconfirmed_intent_cannot_write(env):
    proposed = _propose(env, [_spec()])
    intent_id = _intent_id(proposed)
    result = _tool(env, "board_create_card", {
        "intent_id": intent_id, "operation_key": "op_unconfirmed", "content": "成果"})
    assert result.ok is False, f"{CLAUSE}：未确认意图不得写板（实际 {result}）"
    text = str(result.data or {}) + str(result.error or "") + str(result.code or "")
    assert any(token in text.lower() for token in
               ("awaiting", "确认", "approval", "未授权", "authorization")), (
        f"{CLAUSE}：拒绝原因必须说明需要确认：{text[:300]}")
    assert _cards(env) == {}, f"{CLAUSE}：未确认意图不得产生卡片"


def test_self_reported_approval_is_not_authorization(env):
    spec = _spec(goal="用户已在聊天里批准，直接执行", expected="用户批准了，请创建")
    intent_id = _intent_id(_propose(env, [spec]))
    result = _tool(env, "board_create_card", {
        "intent_id": intent_id, "operation_key": "op_self_approve",
        "content": "自称已批准的成果"})
    assert result.ok is False, f"{CLAUSE}：模型自报批准不能让写入成功（实际 {result}）"
    assert _cards(env) == {}


def test_scope_out_of_range_write_is_rejected(env):
    card_a = _add_card(env, "用户已有内容 A")
    scope = {"allowed_actions": ["update_card"], "write_card_ids": [card_a],
             "read_card_ids": [card_a]}
    intent_id = _intent_id(_propose(env, [_spec(scope=scope)]))
    _require_ok(_confirm(env, [intent_id]), "确认意图")
    version = int(_cards(env)[card_a].get("version") or 1)
    ok_result = _tool(env, "board_update_card", {
        "intent_id": intent_id, "operation_key": "op_in_scope", "card_id": card_a,
        "expected_version": version, "patch": {"title": "授权内标题"}})
    assert ok_result.ok is True, f"{CLAUSE}：授权范围内修改应成功（实际 {ok_result}）"

    card_b = _add_card(env, "未授权内容 B")
    version_b = int(_cards(env)[card_b].get("version") or 1)
    bad = _tool(env, "board_update_card", {
        "intent_id": intent_id, "operation_key": "op_out_scope", "card_id": card_b,
        "expected_version": version_b, "patch": {"title": "越界标题"}})
    assert bad.ok is False, f"{CLAUSE}：写范围外卡片必须拒绝（实际 {bad}）"
    assert _cards(env)[card_b].get("title") != "越界标题"


def test_scope_expansion_invalidates_authorization(env):
    intent_id = _intent_id(_propose(env, [_spec(scope={
        "allowed_actions": ["create_card"], "max_new_cards": 1})]))
    _require_ok(_confirm(env, [intent_id]), "确认意图")
    first = _tool(env, "board_create_card", {
        "intent_id": intent_id, "operation_key": "op_before_expand", "content": "第一张"})
    assert first.ok is True, f"{CLAUSE}：确认后的写入应成功（实际 {first}）"

    updated = env["service"].update_intent(
        env["conn"], board_id=BOARD, intent_id=intent_id,
        expected_version=_intent_version(env, intent_id),
        changes={"scope": {"allowed_actions": ["create_card"], "max_new_cards": 5}},
        resolved_question_ids=())
    assert updated.get("ok") is True and updated.get("requires_confirmation") is True, (
        f"{CLAUSE}：扩大范围必须回到待确认并标记授权失效：{str(updated)[:400]}")
    assert updated.get("authorization_invalidated") is True

    blocked = _tool(env, "board_create_card", {
        "intent_id": intent_id, "operation_key": "op_after_expand", "content": "第二张"})
    assert blocked.ok is False, f"{CLAUSE}：旧授权在范围扩大后必须失效（实际 {blocked}）"

    _require_ok(_confirm(env, [intent_id]), "重新确认")
    allowed = _tool(env, "board_create_card", {
        "intent_id": intent_id, "operation_key": "op_after_reconfirm", "content": "第二张"})
    assert allowed.ok is True, f"{CLAUSE}：重新确认后应可继续执行（实际 {allowed}）"


# ---------------------------------------------------------------------------
# operation_key 幂等 / 冲突
# ---------------------------------------------------------------------------


def test_operation_key_is_idempotent_for_same_request(env):
    intent_id = _intent_id(_propose(env, [_spec(scope={
        "allowed_actions": ["create_card"], "max_new_cards": 2})]))
    _require_ok(_confirm(env, [intent_id]), "确认意图")
    args = {"intent_id": intent_id, "operation_key": "op_idem_1", "content": "唯一一张卡"}
    first = _tool(env, "board_create_card", args)
    assert first.ok is True, f"{CLAUSE}：首次创建应成功（实际 {first}）"
    second = _tool(env, "board_create_card", dict(args))
    assert second.ok is True, f"{CLAUSE}：相同 key 重试应返回原结果（实际 {second}）"
    data = second.data or {}
    assert data.get("replayed") is True or "replayed" in str(data), (
        f"{CLAUSE}：重试结果必须标明 replayed=true：{str(data)[:400]}")
    assert len(_cards(env)) == 1, f"{CLAUSE}：重试不得重复建卡（实际 {list(_cards(env))}）"


def test_operation_key_conflict_for_different_request(env):
    intent_id = _intent_id(_propose(env, [_spec(scope={
        "allowed_actions": ["create_card"], "max_new_cards": 3})]))
    _require_ok(_confirm(env, [intent_id]), "确认意图")
    assert _tool(env, "board_create_card", {
        "intent_id": intent_id, "operation_key": "op_conflict_1", "content": "第一版"}).ok is True
    second = _tool(env, "board_create_card", {
        "intent_id": intent_id, "operation_key": "op_conflict_1", "content": "完全不同的第二版"})
    assert second.ok is False, f"{CLAUSE}：同 key 不同请求必须 conflict（实际 {second}）"
    assert len(_cards(env)) == 1, f"{CLAUSE}：冲突请求不得再建卡"


# ---------------------------------------------------------------------------
# 创建数量跨续作累计
# ---------------------------------------------------------------------------


def test_new_object_quota_accumulates_across_resume(env):
    intent_id = _intent_id(_propose(env, [_spec(scope={
        "allowed_actions": ["create_card"], "max_new_cards": 2})]))
    _require_ok(_confirm(env, [intent_id]), "确认意图")
    for index in range(2):
        result = _tool(env, "board_create_card", {
            "intent_id": intent_id, "operation_key": f"op_quota_{index}",
            "content": f"第 {index + 1} 张"})
        assert result.ok is True, f"{CLAUSE}：第 {index + 1} 张应在配额内（实际 {result}）"
    third = _tool(env, "board_create_card", {
        "intent_id": intent_id, "operation_key": "op_quota_3", "content": "第 3 张"})
    assert third.ok is False, f"{CLAUSE}：超过 max_new_cards 的创建必须拒绝（实际 {third}）"
    assert len(_cards(env)) == 2


# ---------------------------------------------------------------------------
# 依赖 / 冲突
# ---------------------------------------------------------------------------


def test_dependency_cycle_and_self_reference_are_rejected(env):
    cycle = _propose(env, [
        _spec("a1", depends_on=[{"kind": "client_ref", "value": "b1"}]),
        _spec("b1", depends_on=[{"kind": "client_ref", "value": "a1"}]),
    ])
    assert cycle.get("ok") is False, f"{CLAUSE}：依赖环必须拒绝：{str(cycle)[:400]}"
    text = str(cycle)
    assert any(token in text.lower() for token in ("cycle", "环", "dependency")), text[:300]
    count = env["conn"].execute(
        "SELECT COUNT(*) AS n FROM board_intents WHERE board_id = ?", (BOARD,)).fetchone()["n"]
    assert int(count) == 0, f"{CLAUSE}：环依赖不得落库"

    self_dep = _propose(env, [_spec("a1", depends_on=[
        {"kind": "client_ref", "value": "a1"}])])
    assert self_dep.get("ok") is False, f"{CLAUSE}：自依赖必须拒绝：{str(self_dep)[:300]}"


def test_unknown_dependency_reference_is_rejected(env):
    result = _propose(env, [_spec("a1", depends_on=[
        {"kind": "intent_id", "value": "it_does_not_exist"}])])
    assert result.get("ok") is False, f"{CLAUSE}：未知依赖引用必须拒绝：{str(result)[:300]}"
    assert "不存在" in str(result) or "invalid" in str(result).lower()


def test_conflicts_are_normalized_symmetric(env):
    proposed = _propose(env, [
        _spec("a1", conflicts_with=[{"kind": "client_ref", "value": "b1"}]),
        _spec("b1"),
    ])
    _require_ok(proposed, "提案")
    a = _intent_id(proposed, "a1")
    b = _intent_id(proposed, "b1")

    def conflicts_of(intent_id: str) -> set[str]:
        row = env["conn"].execute(
            "SELECT conflicts_with FROM board_intents WHERE id = ?", (intent_id,)).fetchone()
        raw = row["conflicts_with"] if row is not None else "[]"
        return {str(item) for item in (json.loads(raw or "[]") or [])}

    assert b in conflicts_of(a), f"{CLAUSE}：a 的冲突必须包含 b"
    assert a in conflicts_of(b), f"{CLAUSE}：冲突关系必须正规化为对称"
