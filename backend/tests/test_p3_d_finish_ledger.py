# -*- coding: utf-8 -*-
"""D 独立契约测试：board_finish_intent 的完成核对与迟到保护（契约 §13.4）。

覆盖：
* 完成前核对权威操作账本 / operation_keys / 等待 / 冲突 / 必要依赖；存在未结束项不接受 completed；
* 迟到完成不能覆盖用户的取消 / 暂停 / 新尝试；
* partially_completed / failed 保留已完成的成果（不整体回滚），并如实记录结果；
* 完成 / 失败的结果引用由服务从真实操作记录生成（不是模型自报）。

基线说明：本阶段实现未合入时（intent_service / board_tools 缺失）本文件失败是预期。
运行（只跑本文件）：
    cd backend
    & 'D:/qio-dev/qio/backend/.venv/Scripts/python.exe' -m pytest tests/test_p3_d_finish_ledger.py -q
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

CLAUSE = "契约 §13.4"
BOARD = "board_p3_d_finish"
SUBMISSION = "sub_p3_d_finish"


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
    _load("agent.interactive.board_tools")
    service = _load("agent.interactive.intent_service")
    _load("agent.interactive.submission")
    from agent.api.bus import EventBus
    from agent.config import Settings
    from agent.interactive import board_store, models
    from agent.interactive.runtime import BoardTurnContext, reset_board_turn, set_board_turn
    from agent.services.app import AppContext

    app = AppContext(Settings(data_dir=tmp_path), db_conn, EventBus())
    board_store.save_board(db_conn, BOARD, models.empty_state(BOARD), reason="p3-d-seed")
    token = set_board_turn(BoardTurnContext(
        turn_id="turn_finish", mode="interactive", board_id=BOARD,
        submission_id=SUBMISSION, intent_ids=()))
    try:
        yield {"app": app, "conn": db_conn, "service": service}
    finally:
        reset_board_turn(token)


def _run(coro):
    return asyncio.run(coro)


def _exec(env, name: str, arguments: dict):
    from agent.adapters.base import ToolCall

    async def _run_tool():
        return await env["app"].registry.execute(
            ToolCall(id=f"call_{name}", name=name, arguments=arguments))

    return _run(_run_tool())


def _spec(client_ref: str = "a1", **overrides) -> dict:
    base = {
        "client_ref": client_ref,
        "title": "整理材料",
        "goal": "整理成结论",
        "expected": "结论卡",
        "scope": {"allowed_actions": ["create_card"], "max_new_cards": 3},
    }
    base.update(overrides)
    return base


def _propose_confirm(env, intents: list[dict]) -> dict[str, str]:
    proposed = _run(env["service"].propose_intents(
        env["conn"], board_id=BOARD, submission_id=SUBMISSION, intents=intents,
        resolved_question_ids=()))
    mapping = proposed.get("client_refs") or {}
    ids: dict[str, str] = {}
    for item in proposed.get("intents") or []:
        ref = str(item.get("client_ref") or "")
        if ref:
            ids[ref] = str(item.get("id"))
    if not ids and isinstance(mapping, dict):
        ids = {str(k): str(v) for k, v in mapping.items()}
    assert ids, f"{CLAUSE}：提案结果没有可解析的意图身份：{str(proposed)[:400]}"
    _run(env["service"].confirm_intents(
        env["conn"], board_id=BOARD, intent_ids=list(ids.values())))
    return ids


def _intent_version(env, intent_id: str) -> int:
    row = env["conn"].execute(
        "SELECT version FROM board_intents WHERE id = ?", (intent_id,)).fetchone()
    assert row is not None, f"{CLAUSE}：意图 {intent_id} 不存在"
    return int(row["version"])


def _create_card(env, intent_id: str, key: str, content: str = "成果") -> dict:
    return _exec(env, "board_create_card", {
        "intent_id": intent_id, "operation_key": key, "content": content})


def _cards(env) -> dict:
    from agent.interactive import board_store

    state = board_store.load_board(env["conn"], BOARD)["state"]
    return {str(card.get("id")): card for card in state.get("cards") or []}


def _finish(env, intent_id: str, outcome: str, *, operation_keys=None,
            expected_version=None, summary="完成") -> dict:
    args = {
        "intent_id": intent_id,
        "expected_version": expected_version or _intent_version(env, intent_id),
        "outcome": outcome,
        "summary": summary,
    }
    if operation_keys is not None:
        args["operation_keys"] = operation_keys
    return _exec(env, "board_finish_intent", args)


def _status(env, intent_id: str) -> str:
    row = env["conn"].execute(
        "SELECT status FROM board_intents WHERE id = ?", (intent_id,)).fetchone()
    assert row is not None
    return str(row["status"])


# ---------------------------------------------------------------------------
# 完整账本 → completed
# ---------------------------------------------------------------------------


def test_completed_requires_matching_operation_keys(env):
    """§13.4：完成必须与权威操作账本一致（漏列已执行操作不接受）。"""
    ids = _propose_confirm(env, [_spec()])
    intent_id = ids["a1"]
    created = _create_card(env, intent_id, "op_ledger_1")
    assert created.ok is True, f"{CLAUSE}：前置创建应成功（实际 {created}）"

    missing_keys = _finish(env, intent_id, "completed", operation_keys=[])
    assert missing_keys.ok is False, (
        f"{CLAUSE}：漏列已执行操作时不得接受 completed（实际 {missing_keys}）")

    ok = _finish(env, intent_id, "completed", operation_keys=["op_ledger_1"])
    assert ok.ok is True, (
        f"{CLAUSE}：账本完整时应接受 completed（实际 {ok}）")
    assert _status(env, intent_id) == "completed", (
        f"{CLAUSE}：完成后状态必须是 completed（实际 {_status(env, intent_id)}）")
    created_id = next(iter(_cards(env)))
    assert created_id in str(ok.data or {}), (
        f"{CLAUSE}：完成结果的成果引用必须来自真实操作记录（{str(ok.data)[:400]}）")


def test_completed_with_wrong_expected_version_is_rejected(env):
    ids = _propose_confirm(env, [_spec()])
    intent_id = ids["a1"]
    _create_card(env, intent_id, "op_ver_1")
    stale = _finish(env, intent_id, "completed",
                    operation_keys=["op_ver_1"], expected_version=1)
    if int(_intent_version(env, intent_id)) != 1:
        assert stale.ok is False, f"{CLAUSE}：旧版本不能完成新尝试（实际 {stale}）"


# ---------------------------------------------------------------------------
# 未结束项：账本 / 等待 / 冲突 / 依赖
# ---------------------------------------------------------------------------


def test_completed_refused_with_pending_ledger_operation(env):
    """§13.4：账本里仍有未结束操作 → 不接受 completed。"""
    ids = _propose_confirm(env, [_spec()])
    intent_id = ids["a1"]
    _create_card(env, intent_id, "op_pending_1")
    env["conn"].execute(
        "INSERT INTO board_operations"
        " (id, board_id, intent_id, operation_key, request_hash, action, status,"
        "  before, after, result, created_at, updated_at)"
        " VALUES ('mo_pending', ?, ?, 'op_pending_2', 'hash', 'create_card', 'pending',"
        "  '[]', '{}', NULL, '2026-10-21T00:00:00+00:00', '2026-10-21T00:00:00+00:00')",
        (BOARD, intent_id))
    env["conn"].commit()
    result = _finish(env, intent_id, "completed",
                     operation_keys=["op_pending_1", "op_pending_2"])
    assert result.ok is False, (
        f"{CLAUSE}：账本有未结束操作时不得 completed（实际 {result}）")
    assert _status(env, intent_id) != "completed"


def test_completed_refused_with_open_waiting(env):
    """§13.4：仍有等待回答的问题 → 不接受 completed。"""
    submission = _load("agent.interactive.submission")
    ids = _propose_confirm(env, [_spec()])
    intent_id = ids["a1"]
    _create_card(env, intent_id, "op_wait_1")
    if not hasattr(submission, "record_waiting"):
        pytest.fail(f"{CLAUSE}：submission.record_waiting 未实现（A 的等待持久化缺失）",
                    pytrace=False)
    _run_or_sync(submission.record_waiting(
        env["conn"], board_id=BOARD, submission_id=SUBMISSION,
        question="还需要确认截止时间吗？", missing_fields=["deadline"], turn_id="turn_finish"))
    result = _finish(env, intent_id, "completed", operation_keys=["op_wait_1"])
    assert result.ok is False, (
        f"{CLAUSE}：等待未解决时不得 completed（实际 {result}）")


def _run_or_sync(value):
    if asyncio.iscoroutine(value):
        return asyncio.run(value)
    return value


def test_completed_refused_with_unfinished_dependency(env):
    """§13.4：必要依赖未完成 → 不接受 completed。"""
    ids = _propose_confirm(env, [
        _spec("a1"),
        _spec("b1", depends_on=[{"kind": "client_ref", "value": "a1"}]),
    ])
    _create_card(env, ids["b1"], "op_dep_1")
    result = _finish(env, ids["b1"], "completed", operation_keys=["op_dep_1"])
    assert result.ok is False, (
        f"{CLAUSE}：依赖意图未完成时不得 completed（实际 {result}）")


def test_completed_refused_with_unresolved_conflict(env):
    """§13.4：存在未解决冲突 → 不接受 completed。"""
    ids = _propose_confirm(env, [
        _spec("a1", conflicts_with=[{"kind": "client_ref", "value": "b1"}]),
        _spec("b1"),
    ])
    _create_card(env, ids["a1"], "op_conflict_1")
    result = _finish(env, ids["a1"], "completed", operation_keys=["op_conflict_1"])
    assert result.ok is False, (
        f"{CLAUSE}：存在未解决冲突时不得 completed（实际 {result}）")


# ---------------------------------------------------------------------------
# 迟到完成 / 部分完成 / 失败保留成果
# ---------------------------------------------------------------------------


def test_late_completion_cannot_override_cancellation(env):
    """§13.4：用户取消之后，迟到的 completed 不得覆盖取消状态。"""
    ids = _propose_confirm(env, [_spec()])
    intent_id = ids["a1"]
    _create_card(env, intent_id, "op_late_1")
    _run(env["service"].cancel_intent(
        env["conn"], board_id=BOARD, intent_id=intent_id)) if hasattr(
        env["service"], "cancel_intent") else _cancel_via_intents(env, intent_id)
    assert _status(env, intent_id) == "cancelled"
    result = _finish(env, intent_id, "completed", operation_keys=["op_late_1"])
    assert result.ok is False, (
        f"{CLAUSE}：迟到完成不得覆盖取消（实际 {result}）")
    assert _status(env, intent_id) == "cancelled"


def _cancel_via_intents(env, intent_id: str) -> None:
    intents = _load("agent.interactive.intents")
    intents.cancel_intent(env["conn"], intent_id, reason="用户取消")


def test_partially_completed_keeps_results_and_does_not_lock(env):
    """§13.4：partially_completed 保留已完成成果，不整体回滚，也不永久锁死。"""
    ids = _propose_confirm(env, [_spec()])
    intent_id = ids["a1"]
    _create_card(env, intent_id, "op_partial_1", "已经完成的成果")
    result = _finish(env, intent_id, "partially_completed",
                     operation_keys=["op_partial_1"], summary="完成一部分")
    assert result.ok is True, (
        f"{CLAUSE}：账本一致时 partially_completed 应被接受（实际 {result}）")
    assert _status(env, intent_id) == "partially_completed"
    assert _cards(env), f"{CLAUSE}：部分完成必须保留已完成成果（不得整体回滚）"


def test_failed_keeps_applied_results(env):
    """§13.4：失败不整体回滚成果；只标明未完成部分。"""
    ids = _propose_confirm(env, [_spec()])
    intent_id = ids["a1"]
    _create_card(env, intent_id, "op_failed_1", "已经做出来但未全部完成")
    result = _finish(env, intent_id, "failed", operation_keys=["op_failed_1"],
                     summary="执行失败")
    assert result.ok is True, f"{CLAUSE}：失败结果应被接受（实际 {result}）"
    assert _status(env, intent_id) == "failed"
    assert _cards(env), (
        f"{CLAUSE}：失败必须保留已经完成且有用的成果（旧 _settle_failed 整体回滚已删除）")
