# -*- coding: utf-8 -*-
"""D 独立契约测试：board_finish_intent 的完成核对与迟到保护（契约 §13.4）。

覆盖：
* 完成前核对权威操作账本 / operation_keys / 等待 / 冲突 / 必要依赖；存在未结束项不接受 completed；
* 迟到完成不能覆盖用户的取消 / 暂停 / 新尝试；
* partially_completed / failed 保留已完成的成果（不整体回滚），并如实记录结果；
* 完成 / 失败的结果引用由服务从真实操作记录生成（不是模型自报）。

装置：真实 DB + B 的 board_tools / intent_service + 冻结 runtime 上下文；模型不参与。

基线说明：本阶段实现未合入时（board_tools / intent_service 缺失）本文件失败是预期。
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

SUBMISSION = "sub_p3_d_finish"


def _load(name: str):
    try:
        return importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            f"{CLAUSE}：契约要求的 {name} 不可导入；阶段三实现未合入时属于「未验证」：{exc!r}",
            pytrace=False,
        )


class FakeApp:
    def __init__(self, conn) -> None:
        self.conn = conn


@pytest.fixture()
def env(db_conn):
    board_tools_mod = _load("agent.interactive.board_tools")
    service = _load("agent.interactive.intent_service")
    _load("agent.interactive.submission")
    from agent.interactive import board_store, models, runtime

    conn = db_conn
    board_store.ensure_board(conn, board_id=BOARD)
    conn.execute(
        "INSERT OR IGNORE INTO board_submissions (id, board_id, seq, status, created_at)"
        " VALUES (?, ?, 1, 'accepted', ?)",
        (SUBMISSION, BOARD, models.now_iso()))
    conn.commit()
    tools = {tool.name: tool for tool in board_tools_mod.build_board_tools(FakeApp(conn))}
    token = runtime.set_board_turn(runtime.BoardTurnContext(
        turn_id="turn_finish", mode=runtime.MODE_INTERACTIVE, board_id=BOARD,
        submission_id=SUBMISSION, topic_id="topic_finish", attempt=1))
    try:
        yield {"conn": conn, "service": service, "tools": tools,
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
        "goal": "整理成结论",
        "expected": "结论卡",
        "basis": [],
        "scope": {"allowed_actions": ["create_card"], "max_new_cards": 3},
        "depends_on": [],
        "conflicts_with": [],
    }
    base.update(overrides)
    return base


def _propose_confirm(env, intents: list[dict]) -> dict[str, str]:
    proposed = env["service"].propose_intents(
        env["conn"], board_id=BOARD, submission_id=SUBMISSION, intents=intents,
        resolved_question_ids=())
    assert proposed.get("ok") is True, f"{CLAUSE}：前置提案失败：{str(proposed)[:400]}"
    mapping = {str(k): str(v) for k, v in (proposed.get("client_refs") or {}).items()}
    assert mapping, f"{CLAUSE}：提案结果缺少 client_refs：{str(proposed)[:400]}"
    confirmed = env["service"].confirm_intents(
        env["conn"], board_id=BOARD, intent_ids=list(mapping.values()))
    assert confirmed.get("ok") is True, (
        f"{CLAUSE}：前置确认失败：{str(confirmed)[:400]}")
    return mapping


def _intent_version(env, intent_id: str) -> int:
    row = env["conn"].execute(
        "SELECT version FROM board_intents WHERE id = ?", (intent_id,)).fetchone()
    assert row is not None, f"{CLAUSE}：意图 {intent_id} 不存在"
    return int(row["version"])


def _create_card(env, intent_id: str, key: str, content: str = "成果") -> dict:
    return _tool(env, "board_create_card", {
        "intent_id": intent_id, "operation_key": key, "content": content})


def _cards(env) -> dict:
    state = env["board_store"].current_state(env["conn"], BOARD)
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
    return _tool(env, "board_finish_intent", args)


def _status(env, intent_id: str) -> str:
    row = env["conn"].execute(
        "SELECT status FROM board_intents WHERE id = ?", (intent_id,)).fetchone()
    assert row is not None
    return str(row["status"])


# ---------------------------------------------------------------------------
# 完整账本 → completed
# ---------------------------------------------------------------------------


def test_completed_requires_matching_operation_keys(env):
    ids = _propose_confirm(env, [_spec()])
    intent_id = ids["a1"]
    created = _create_card(env, intent_id, "op_ledger_1")
    assert created.ok is True, f"{CLAUSE}：前置创建应成功（实际 {created}）"
    created_card = next(iter(_cards(env)))

    missing_keys = _finish(env, intent_id, "completed", operation_keys=[])
    assert missing_keys.ok is False, (
        f"{CLAUSE}：漏列已执行操作时不得接受 completed（实际 {missing_keys}）")

    ok = _finish(env, intent_id, "completed", operation_keys=["op_ledger_1"])
    assert ok.ok is True, f"{CLAUSE}：账本完整时应接受 completed（实际 {ok}）"
    assert _status(env, intent_id) == "completed"
    assert created_card in str(ok.data or {}), (
        f"{CLAUSE}：完成结果的成果引用必须来自真实操作记录（{str(ok.data)[:400]}）")


def test_completed_with_wrong_expected_version_is_rejected(env):
    ids = _propose_confirm(env, [_spec()])
    intent_id = ids["a1"]
    _create_card(env, intent_id, "op_ver_1")
    stale = _finish(env, intent_id, "completed", operation_keys=["op_ver_1"],
                    expected_version=457)
    assert stale.ok is False, f"{CLAUSE}：旧版本不能完成新尝试（实际 {stale}）"
    assert _status(env, intent_id) != "completed"


# ---------------------------------------------------------------------------
# 未结束项：账本 / 等待 / 冲突 / 依赖
# ---------------------------------------------------------------------------


def test_completed_refused_with_pending_ledger_operation(env):
    ids = _propose_confirm(env, [_spec()])
    intent_id = ids["a1"]
    _create_card(env, intent_id, "op_pending_1")
    env["conn"].execute(
        "INSERT INTO board_operations"
        " (id, board_id, intent_id, operation_key, request_hash, action, status,"
        "  before, after, result, created_at, updated_at)"
        " VALUES ('mo_pending', ?, ?, 'op_pending_2', 'hash', 'create_card', 'pending',"
        "  '{}', '{}', '{}', '2026-10-21T00:00:00+00:00', '2026-10-21T00:00:00+00:00')",
        (BOARD, intent_id))
    env["conn"].commit()
    result = _finish(env, intent_id, "completed",
                     operation_keys=["op_pending_1", "op_pending_2"])
    assert result.ok is False, (
        f"{CLAUSE}：账本有未结束操作时不得 completed（实际 {result}）")
    assert _status(env, intent_id) != "completed"


def test_completed_refused_with_open_waiting(env):
    submission = _load("agent.interactive.submission")
    ids = _propose_confirm(env, [_spec()])
    intent_id = ids["a1"]
    _create_card(env, intent_id, "op_wait_1")
    if not hasattr(submission, "record_waiting"):
        pytest.fail(f"{CLAUSE}：submission.record_waiting 未实现（等待持久化缺失）",
                    pytrace=False)
    _run(submission.record_waiting(
        env["conn"], board_id=BOARD, submission_id=SUBMISSION,
        question="还需要确认截止时间吗？", missing_fields=["deadline"],
        turn_id="turn_finish"))
    result = _finish(env, intent_id, "completed", operation_keys=["op_wait_1"])
    assert result.ok is False, (
        f"{CLAUSE}：等待未解决时不得 completed（实际 {result}）")


def test_completed_refused_with_unfinished_dependency(env):
    ids = _propose_confirm(env, [
        _spec("a1"),
        _spec("b1", depends_on=[{"kind": "client_ref", "value": "a1"}]),
    ])
    _create_card(env, ids["b1"], "op_dep_1")
    result = _finish(env, ids["b1"], "completed", operation_keys=["op_dep_1"])
    assert result.ok is False, (
        f"{CLAUSE}：依赖意图未完成时不得 completed（实际 {result}）")


def test_completed_refused_with_unresolved_conflict(env):
    proposed = env["service"].propose_intents(
        env["conn"], board_id=BOARD, submission_id=SUBMISSION,
        intents=[
            _spec("a1", conflicts_with=[{"kind": "client_ref", "value": "b1"}]),
            _spec("b1"),
        ], resolved_question_ids=())
    assert proposed.get("ok") is True, str(proposed)[:300]
    a = str(proposed["client_refs"]["a1"])
    b = str(proposed["client_refs"]["b1"])
    env["service"].confirm_intents(env["conn"], board_id=BOARD, intent_ids=[a])
    # 模拟另一条冲突意图已被用户确认（DB 状态注入，测完成核对器）
    env["conn"].execute(
        "UPDATE board_intents SET status = 'approved' WHERE id = ?", (b,))
    env["conn"].commit()
    _create_card(env, a, "op_conflict_1")
    result = _finish(env, a, "completed", operation_keys=["op_conflict_1"])
    assert result.ok is False, (
        f"{CLAUSE}：存在未解决冲突时不得 completed（实际 {result}）")
    assert "conflict" in str(result.data or "") or "冲突" in str(result.data or "")


# ---------------------------------------------------------------------------
# 迟到完成 / 部分完成 / 失败保留成果
# ---------------------------------------------------------------------------


def test_late_completion_cannot_override_cancellation(env):
    ids = _propose_confirm(env, [_spec()])
    intent_id = ids["a1"]
    _create_card(env, intent_id, "op_late_1")
    cancel = env["service"].cancel_intent(
        env["conn"], board_id=BOARD, intent_id=intent_id,
        expected_version=_intent_version(env, intent_id))
    assert cancel.get("ok") is True, f"{CLAUSE}：前置取消失败：{str(cancel)[:300]}"
    assert _status(env, intent_id) == "cancelled"
    result = _finish(env, intent_id, "completed", operation_keys=["op_late_1"])
    assert result.ok is False, f"{CLAUSE}：迟到完成不得覆盖取消（实际 {result}）"
    assert _status(env, intent_id) == "cancelled"


def test_partially_completed_keeps_results_and_does_not_lock(env):
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
    ids = _propose_confirm(env, [_spec()])
    intent_id = ids["a1"]
    _create_card(env, intent_id, "op_failed_1", "已经做出来但未全部完成")
    result = _finish(env, intent_id, "failed", operation_keys=["op_failed_1"],
                     summary="执行失败")
    assert result.ok is True, f"{CLAUSE}：失败结果应被接受（实际 {result}）"
    assert _status(env, intent_id) == "failed"
    assert _cards(env), (
        f"{CLAUSE}：失败必须保留已经完成且有用的成果（旧 _settle_failed 整体回滚已删除）")
