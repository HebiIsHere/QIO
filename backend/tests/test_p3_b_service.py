# -*- coding: utf-8 -*-
"""迁移 50 / 对象版本 CAS / 意图授权账本服务的定向用例（P3-B）。"""

from __future__ import annotations

import pytest

from agent.interactive import board, board_store, intent_service, models, tool_models
from agent.storage import migration_p3_b

from test_p3_b_support import (
    add_submission,
    add_visible_card,
    current_card,
    intent_scope,
    intent_spec,
    make_board,
    prepare,
)


@pytest.fixture()
def env(db_conn):
    prepare(db_conn)
    make_board(db_conn)
    add_submission(db_conn)
    add_visible_card(db_conn, content="用户材料一")
    return db_conn


def test_migration_50_number_columns_tables_and_idempotent(db_conn):
    assert migration_p3_b.MIGRATION[0] == 50
    assert set(migration_p3_b.added_columns()) == {
        "client_ref",
        "version",
        "scope",
        "authorization",
        "outcome",
        "attempt",
        "finished_at",
    }
    assert set(migration_p3_b.tables()) == {"board_intent_versions", "board_operations"}
    columns = migration_p3_b.columns()
    assert set(columns["board_intent_versions"]) == {
        "intent_id",
        "version",
        "payload",
        "authorization",
        "created_at",
    }
    assert set(columns["board_operations"]) == {
        "id",
        "board_id",
        "intent_id",
        "operation_key",
        "request_hash",
        "action",
        "status",
        "before",
        "after",
        "result",
        "created_at",
        "updated_at",
    }
    first = migration_p3_b.apply(db_conn)
    assert first
    migration_p3_b.apply(db_conn)
    migration_p3_b.apply(db_conn)
    names = {
        str(row["name"])
        for row in db_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"board_intent_versions", "board_operations"} <= names


def test_board_objects_have_versions_and_bump_on_real_change(env):
    conn = env
    card_id = add_visible_card(conn, content="第二张材料")
    state = board_store.current_state(conn, "board_p3")
    for card in state["cards"]:
        assert int(card["version"]) >= 1
    before = int(current_card(conn, "board_p3", card_id)["version"])
    saved = board_store.write_board_cas(
        conn,
        "board_p3",
        expected_versions={card_id: before},
        mutate=lambda state: (
            board.update_card(state, card_id, {"content": "第二张材料（改）"}),
            {"after": {"card_id": card_id}, "result": {"changed": True}},
        ),
    )
    assert saved["ok"] is True
    assert int(current_card(conn, "board_p3", card_id)["version"]) == before + 1
    # 同内容再写一次：不构成实际修改，版本不递增。
    version_now = int(current_card(conn, "board_p3", card_id)["version"])
    board_store.write_board_cas(
        conn,
        "board_p3",
        expected_versions={card_id: version_now},
        mutate=lambda state: (
            board.update_card(state, card_id, {"content": "第二张材料（改）"}),
            {"after": {}, "result": {}},
        ),
    )
    assert int(current_card(conn, "board_p3", card_id)["version"]) == version_now


def test_board_cas_conflict_does_not_write(env):
    conn = env
    card_id = add_visible_card(conn, content="版本保护材料")
    seq_before = board_store.saved_seq(conn, "board_p3")
    saved = board_store.write_board_cas(
        conn,
        "board_p3",
        expected_versions={card_id: 999},
        mutate=lambda state: (
            board.update_card(state, card_id, {"content": "不应落地"}),
            {"after": {}, "result": {}},
        ),
    )
    assert saved["ok"] is False
    assert saved["conflict"] is True
    assert saved["conflicts"][0]["reason"] == "version_mismatch"
    assert board_store.saved_seq(conn, "board_p3") == seq_before
    assert current_card(conn, "board_p3", card_id)["content"] == "版本保护材料"


def test_propose_and_confirm_authorization_snapshot(env):
    conn = env
    card_id = next(iter(board_store.current_state(conn, "board_p3")["cards"]))["id"]
    spec = intent_spec("a", scope=intent_scope(read_card_ids=[card_id], write_card_ids=[card_id], allowed_actions=["update_card"], max_new_cards=1))
    proposed = intent_service.propose_intents(
        conn, board_id="board_p3", submission_id="sub_p3_1", intents=[spec]
    )
    assert proposed["ok"] is True
    view = proposed["intents"][0]
    assert view["status"] == intent_service.STATUS_AWAITING
    assert view["version"] == 1
    assert view["client_ref"] == "a"
    assert view["scope"]["write_card_ids"] == [card_id]
    assert view["authorization_valid"] is False
    intent_id = view["intent_id"]
    confirmed = intent_service.confirm_intents(conn, board_id="board_p3", intent_ids=[intent_id])
    assert confirmed["ok"] is True
    assert confirmed["confirmed"] == [intent_id]
    confirmed_view = intent_service.get_intent(conn, "board_p3", intent_id)
    assert confirmed_view["status"] == intent_service.STATUS_APPROVED
    assert confirmed_view["authorization_valid"] is True
    assert confirmed_view["authorization"]["scope"]["write_card_ids"] == [card_id]
    assert confirmed_view["authorization"]["confirmed_by"] == "user"
    # 版本历史：提案 v1 + 确认 v1（同一版本只记一次，确认更新了授权快照）。
    rows = conn.execute(
        "SELECT version, authorization FROM board_intent_versions WHERE intent_id = ? ORDER BY version",
        (intent_id,),
    ).fetchall()
    assert rows and int(rows[-1]["version"]) == 1


def test_substantive_update_invalidates_authorization(env):
    conn = env
    card_id = next(iter(board_store.current_state(conn, "board_p3")["cards"]))["id"]
    view = intent_service.propose_intents(
        conn,
        board_id="board_p3",
        submission_id="sub_p3_1",
        intents=[intent_spec("a", scope=intent_scope(read_card_ids=[card_id], write_card_ids=[card_id], allowed_actions=["update_card"]))],
    )["intents"][0]
    intent_id = view["intent_id"]
    intent_service.confirm_intents(conn, board_id="board_p3", intent_ids=[intent_id])
    updated = intent_service.update_intent(
        conn,
        board_id="board_p3",
        intent_id=intent_id,
        expected_version=1,
        changes={"goal": "扩大后的目标：同时整理两批材料"},
    )
    assert updated["ok"] is True
    assert updated["requires_confirmation"] is True
    assert updated["version"] == 2
    new_view = intent_service.get_intent(conn, "board_p3", intent_id)
    assert new_view["status"] == intent_service.STATUS_AWAITING
    assert new_view["authorization_valid"] is False
    authorized = intent_service.authorize_operation(
        conn,
        board_id="board_p3",
        intent_id=intent_id,
        action="update_card",
        operation_key="k1",
        request_payload={},
    )
    assert authorized["ok"] is False
    assert authorized["code"] in ("awaiting_approval", "authorization_stale")
    # 旧版本更新必须冲突（CAS）。
    stale = intent_service.update_intent(
        conn, board_id="board_p3", intent_id=intent_id, expected_version=1, changes={"title": "x"}
    )
    assert stale["ok"] is False and stale["code"] == "version_conflict"


def test_non_substantive_update_keeps_authorization(env):
    conn = env
    card_id = next(iter(board_store.current_state(conn, "board_p3")["cards"]))["id"]
    view = intent_service.propose_intents(
        conn,
        board_id="board_p3",
        submission_id="sub_p3_1",
        intents=[intent_spec("a", scope=intent_scope(read_card_ids=[card_id], write_card_ids=[card_id], allowed_actions=["update_card"]))],
    )["intents"][0]
    intent_id = view["intent_id"]
    intent_service.confirm_intents(conn, board_id="board_p3", intent_ids=[intent_id])
    updated = intent_service.update_intent(
        conn, board_id="board_p3", intent_id=intent_id, expected_version=1, changes={"title": "换个标题"}
    )
    assert updated["ok"] is True
    assert updated["requires_confirmation"] is False
    assert updated["version"] == 2
    new_view = intent_service.get_intent(conn, "board_p3", intent_id)
    assert new_view["status"] == intent_service.STATUS_APPROVED
    assert new_view["authorization_valid"] is True
    assert new_view["authorization"]["version"] == 2


def test_dependency_resolution_and_cycle_rejection(env):
    conn = env
    proposed = intent_service.propose_intents(
        conn,
        board_id="board_p3",
        submission_id="sub_p3_1",
        intents=[
            intent_spec("a"),
            intent_spec("b", depends_on=[("client_ref", "a")]),
        ],
    )
    assert proposed["ok"] is True
    by_ref = {view["client_ref"]: view for view in proposed["intents"]}
    assert by_ref["b"]["depends_on"] == [by_ref["a"]["intent_id"]]
    cycle = intent_service.propose_intents(
        conn,
        board_id="board_p3",
        submission_id="sub_p3_1",
        intents=[
            intent_spec("c", depends_on=[("client_ref", "d")]),
            intent_spec("d", depends_on=[("client_ref", "c")]),
        ],
    )
    assert cycle["ok"] is False
    assert cycle["code"] == "dependency_cycle"
    # 自依赖由冻结模型直接拒绝。
    self_spec = dict(intent_spec("e", depends_on=[("client_ref", "e")]))
    self_result = intent_service.propose_intents(
        conn, board_id="board_p3", submission_id="sub_p3_1", intents=[self_spec]
    )
    assert self_result["ok"] is False
    assert self_result["code"] in ("invalid_intent", "dependency_invalid")


def test_conflict_is_symmetric_and_cross_board_rejected(env):
    conn = env
    make_board(conn, "board_p3_other")
    add_submission(conn, "board_p3_other", "sub_other")
    other = intent_service.propose_intents(
        conn,
        board_id="board_p3_other",
        submission_id="sub_other",
        intents=[intent_spec("x")],
    )["intents"][0]
    cross = intent_service.propose_intents(
        conn,
        board_id="board_p3",
        submission_id="sub_p3_1",
        intents=[intent_spec("a", depends_on=[("intent_id", other["intent_id"])])],
    )
    assert cross["ok"] is False
    assert cross["code"] == "dependency_invalid"
    proposed = intent_service.propose_intents(
        conn,
        board_id="board_p3",
        submission_id="sub_p3_1",
        intents=[
            intent_spec("a", conflicts_with=[("client_ref", "b")]),
            intent_spec("b"),
        ],
    )
    by_ref = {view["client_ref"]: view for view in proposed["intents"]}
    assert by_ref["a"]["intent_id"] in by_ref["b"]["conflicts_with"]
    assert by_ref["b"]["intent_id"] in by_ref["a"]["conflicts_with"]


def test_operation_ledger_idempotency_and_missing_keys(env):
    conn = env
    card_id = next(iter(board_store.current_state(conn, "board_p3")["cards"]))["id"]
    view = intent_service.propose_intents(
        conn,
        board_id="board_p3",
        submission_id="sub_p3_1",
        intents=[intent_spec("a", scope=intent_scope(read_card_ids=[card_id], allowed_actions=["create_card"], max_new_cards=2))],
    )["intents"][0]
    intent_id = view["intent_id"]
    intent_service.confirm_intents(conn, board_id="board_p3", intent_ids=[intent_id])
    request = {"content": "第一张结果"}
    first = intent_service.begin_operation(
        conn,
        board_id="board_p3",
        intent_id=intent_id,
        action="create_card",
        operation_key="k1",
        request_payload=request,
    )
    assert first["ok"] and first["replayed"] is False
    intent_service.complete_operation(
        conn, first["operation_id"], after={"card_id": "kn_x"}, result={"card_id": "kn_x"}
    )
    replay = intent_service.begin_operation(
        conn,
        board_id="board_p3",
        intent_id=intent_id,
        action="create_card",
        operation_key="k1",
        request_payload=request,
    )
    assert replay["ok"] is True and replay["replayed"] is True
    conflict = intent_service.authorize_operation(
        conn,
        board_id="board_p3",
        intent_id=intent_id,
        action="create_card",
        operation_key="k1",
        request_payload={"content": "不同请求"},
    )
    assert conflict["ok"] is False and conflict["code"] == "operation_key_conflict"
    assert intent_service.new_objects_used(conn, intent_id) == {"cards": 1, "groups": 0, "links": 0}
    # 漏列已完成操作 → completed 被拒绝。
    blocked = intent_service.finish_intent(
        conn,
        board_id="board_p3",
        intent_id=intent_id,
        expected_version=1,
        outcome="completed",
        summary="完成",
        operation_keys=[],
    )
    assert blocked["ok"] is False and blocked["code"] == "completion_blocked"
    ok = intent_service.finish_intent(
        conn,
        board_id="board_p3",
        intent_id=intent_id,
        expected_version=1,
        outcome="completed",
        summary="完成",
        operation_keys=["k1"],
    )
    assert ok["ok"] is True
    assert ok["status"] == intent_service.STATUS_COMPLETED
    assert ok["results"][0]["operation_key"] == "k1"
    # 迟到完成不能覆盖已完成 / 取消。
    late = intent_service.finish_intent(
        conn,
        board_id="board_p3",
        intent_id=intent_id,
        expected_version=1,
        outcome="failed",
        summary="迟到",
        operation_keys=[],
    )
    assert late["ok"] is False
    assert late["code"] in ("version_conflict", "late_completion")


def test_partial_completion_then_recover(env):
    conn = env
    card_id = next(iter(board_store.current_state(conn, "board_p3")["cards"]))["id"]
    view = intent_service.propose_intents(
        conn,
        board_id="board_p3",
        submission_id="sub_p3_1",
        intents=[intent_spec("a", scope=intent_scope(read_card_ids=[card_id], allowed_actions=["create_card"], max_new_cards=2))],
    )["intents"][0]
    intent_id = view["intent_id"]
    intent_service.confirm_intents(conn, board_id="board_p3", intent_ids=[intent_id])
    started = intent_service.begin_operation(
        conn, board_id="board_p3", intent_id=intent_id, action="create_card", operation_key="k1", request_payload={}
    )
    intent_service.complete_operation(conn, started["operation_id"], after={}, result={"card_id": "c1"})
    partial = intent_service.finish_intent(
        conn,
        board_id="board_p3",
        intent_id=intent_id,
        expected_version=1,
        outcome="partially_completed",
        summary="先完成一张",
        operation_keys=["k1"],
    )
    assert partial["ok"] is True
    assert partial["status"] == intent_service.STATUS_PARTIALLY_COMPLETED
    assert partial["results"]
    preview = intent_service.preview_recovery(conn, intent_id)
    assert preview["can_resume"] is True
    # finish 不改变 version（只结束本次尝试）；recover 推进到 v2。
    recovered = intent_service.recover_intent(
        conn, board_id="board_p3", intent_id=intent_id, expected_version=1
    )
    assert recovered["ok"] is True
    assert recovered["status"] == intent_service.STATUS_APPROVED
    done = intent_service.finish_intent(
        conn,
        board_id="board_p3",
        intent_id=intent_id,
        expected_version=2,
        outcome="completed",
        summary="全部完成",
        operation_keys=["k1"],
    )
    assert done["ok"] is True
    assert done["status"] == intent_service.STATUS_COMPLETED


def test_finish_blocks_unfinished_dependencies_and_waiting(env, monkeypatch):
    conn = env
    proposed = intent_service.propose_intents(
        conn,
        board_id="board_p3",
        submission_id="sub_p3_1",
        intents=[
            intent_spec("a"),
            intent_spec("b", depends_on=[("client_ref", "a")]),
            intent_spec("c"),
        ],
    )
    assert proposed["ok"] is True
    views = {view["client_ref"]: view for view in proposed["intents"]}
    intent_service.confirm_intents(
        conn, board_id="board_p3", intent_ids=[views["a"]["intent_id"], views["b"]["intent_id"]]
    )
    blocked = intent_service.finish_intent(
        conn,
        board_id="board_p3",
        intent_id=views["b"]["intent_id"],
        expected_version=1,
        outcome="completed",
        summary="完成",
        operation_keys=[],
    )
    assert blocked["ok"] is False and blocked["code"] == "completion_blocked"
    kinds = {item["kind"] for item in blocked.get("blockers") or []}
    assert "dependencies" in kinds
    dependency_done = intent_service.finish_intent(
        conn,
        board_id="board_p3",
        intent_id=views["a"]["intent_id"],
        expected_version=1,
        outcome="completed",
        summary="完成",
        operation_keys=[],
    )
    assert dependency_done["ok"] is True
    now_ok = intent_service.finish_intent(
        conn,
        board_id="board_p3",
        intent_id=views["b"]["intent_id"],
        expected_version=1,
        outcome="completed",
        summary="完成",
        operation_keys=[],
    )
    assert now_ok["ok"] is True
    # 等待未解除：A 的 get_waiting 被注入等待项时，completed 必须被拒绝。
    import agent.interactive.submission as submission_mod

    intent_service.confirm_intents(
        conn, board_id="board_p3", intent_ids=[views["c"]["intent_id"]]
    )
    monkeypatch.setattr(
        submission_mod,
        "get_waiting",
        lambda *args, **kwargs: {"items": [{"id": "w1", "question": "还缺一个字段", "status": "waiting"}]},
        raising=False,
    )
    waiting_blocked = intent_service.finish_intent(
        conn,
        board_id="board_p3",
        intent_id=views["c"]["intent_id"],
        expected_version=1,
        outcome="completed",
        summary="完成",
        operation_keys=[],
    )
    assert waiting_blocked["ok"] is False
    assert waiting_blocked["code"] == "completion_blocked"
    waiting_kinds = {item["kind"] for item in waiting_blocked.get("blockers") or []}
    assert "waiting" in waiting_kinds


def test_confirm_blocks_conflicting_intents(env):
    conn = env
    proposed = intent_service.propose_intents(
        conn,
        board_id="board_p3",
        submission_id="sub_p3_1",
        intents=[
            intent_spec("a", conflicts_with=[("client_ref", "b")]),
            intent_spec("b"),
        ],
    )
    views = {view["client_ref"]: view for view in proposed["intents"]}
    decided = intent_service.confirm_intents(
        conn,
        board_id="board_p3",
        intent_ids=[views["a"]["intent_id"], views["b"]["intent_id"]],
    )
    results = {item["intent_id"]: item for item in decided["results"]}
    assert results[views["a"]["intent_id"]]["ok"] is True
    blocked = results[views["b"]["intent_id"]]
    assert blocked["ok"] is False and blocked["status"] == "conflict"
    assert "冲突" in blocked["reason"]
