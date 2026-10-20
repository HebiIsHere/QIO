# -*- coding: utf-8 -*-
"""12 个板面工具 / 授权范围 / 账本 / 版本保护 / 完成核对 的定向用例（P3-B）。"""

from __future__ import annotations

import json

import pytest

from agent.interactive import board, board_store, intent_service, models, runtime, tool_models
from agent.prompts import TOOL_DESCRIPTIONS

from test_p3_b_support import (
    FakeApp,
    add_submission,
    add_visible_card,
    board_tools,
    bound,
    current_card,
    intent_scope,
    intent_spec,
    make_board,
    prepare,
)

BOARD = "board_p3"
SUBMISSION = "sub_p3_1"


@pytest.fixture()
def env(db_conn):
    prepare(db_conn)
    make_board(db_conn)
    add_submission(db_conn)
    card = add_visible_card(db_conn, content="用户材料一")
    hidden = add_visible_card(db_conn, content="未勾选材料", checked=False)
    app = FakeApp(db_conn)
    return db_conn, app, board_tools(app), card, hidden


def _propose_confirm(conn, intents: list[dict]) -> list[dict]:
    proposed = intent_service.propose_intents(
        conn, board_id=BOARD, submission_id=SUBMISSION, intents=intents
    )
    assert proposed["ok"], proposed
    ids = [view["intent_id"] for view in proposed["intents"]]
    confirmed = intent_service.confirm_intents(conn, board_id=BOARD, intent_ids=ids)
    assert confirmed["ok"], confirmed
    return proposed["intents"]


def _intent(conn, *, scope: dict, **overrides) -> dict:
    spec = intent_spec("a", scope=scope, **overrides)
    return _propose_confirm(conn, [spec])[0]


def test_tool_registry_uses_frozen_sources(env):
    _conn, _app, tools, _card, _hidden = env
    assert len(tools) == 12
    assert set(tools) == set(tool_models.TOOL_ARG_MODELS)
    assert set(tool_models.READ_ONLY_TOOLS) == {"board_inspect", "board_read_cards"}
    for name, tool in tools.items():
        assert tool.description == TOOL_DESCRIPTIONS[name]
        assert tool.arg_model is tool_models.TOOL_ARG_MODELS[name]
        assert tool.is_concurrency_safe is (name in tool_models.READ_ONLY_TOOLS)


async def test_normal_mode_has_no_board_tools(env):
    _conn, _app, tools, _card, _hidden = env
    inspected = await tools["board_inspect"].run()
    assert inspected.ok is False
    assert inspected.code == "interactive_unavailable"
    assert inspected.protocol_version == 1
    created = await tools["board_create_card"].run(
        intent_id="i_dummy", operation_key="k_dummy", content="普通模式不得写板面"
    )
    assert created.ok is False
    assert created.code == "interactive_unavailable"
    # 普通模式伪造 board_id 也不能绕过：参数模型 extra=forbid。
    forged = await tools["board_inspect"].run(board_id="board_p3")
    assert forged.ok is False
    assert forged.code == "invalid_arguments"


async def test_inspect_and_read_respect_visibility_and_versions(env):
    conn, _app, tools, card, hidden = env
    with bound(board_id=BOARD):
        inspected = await tools["board_inspect"].run()
        assert inspected.ok is True
        data = inspected.data
        assert data["board_id"] == BOARD
        ids = {item["id"] for item in data["cards"]}
        assert card in ids and hidden not in ids
        assert all(int(item["version"]) >= 1 for item in data["cards"])
        read = await tools["board_read_cards"].run(card_ids=[card])
        assert read.ok is True
        assert read.data["cards"][0]["content"] == "用户材料一"
        denied = await tools["board_read_cards"].run(card_ids=[hidden])
        assert denied.ok is False
        assert denied.code == "not_readable"
        snapshot = await tools["board_read_cards"].run(source="submission")
        assert snapshot.ok is True
        assert snapshot.data["source"] == "submission"


async def test_propose_via_tool_and_fake_submission_rejected(env):
    conn, _app, tools, card, _hidden = env
    spec = intent_spec("a", scope=intent_scope(read_card_ids=[card], allowed_actions=["create_card"], max_new_cards=1))
    with bound(board_id=BOARD, submission_id=SUBMISSION):
        result = await tools["board_propose_intents"].run(
            submission_id="sub_other", intents=[spec]
        )
        assert result.ok is False
        assert result.code == "submission_binding_mismatch"
        result = await tools["board_propose_intents"].run(
            submission_id=SUBMISSION, intents=[spec]
        )
        assert result.ok is True
        assert result.status == "awaiting_approval"
        assert result.data["intents"][0]["status"] == intent_service.STATUS_AWAITING
        assert result.data["requires_confirmation"] is True


async def test_wait_for_input_delivers_question_once_and_persists(env):
    conn, _app, tools, _card, _hidden = env
    # 整合前提变化：A 的等待持久化模块已合入，问题必须真实落库（不再降级）。
    from agent.interactive import submission as submission_mod

    with bound(board_id=BOARD, submission_id=SUBMISSION):
        runtime.clear_delivered_questions()
        result = await tools["board_wait_for_input"].run(
            submission_id=SUBMISSION, question="你希望整理成哪种格式？", missing_fields=["format"]
        )
        assert result.ok is True
        assert result.status == "awaiting_input"
        assert result.data["question_delivered"] is True
        assert result.data["waiting_unavailable"] is False
        assert result.data["persisted"] is True
        assert result.data["waiting_id"]
        waiting = submission_mod.get_waiting(
            conn, BOARD, submission_id=SUBMISSION
        )
        assert waiting is not None
        assert waiting["items"][0]["question"] == "你希望整理成哪种格式？"
        delivered = runtime.take_delivered_questions()
        assert delivered and delivered[-1]["question"] == "你希望整理成哪种格式？"


async def test_create_card_scope_limit_replay_and_conflict(env):
    conn, _app, tools, card, _hidden = env
    view = _intent(
        conn,
        scope=intent_scope(
            read_card_ids=[card],
            allowed_actions=["create_card"],
            max_new_cards=2,
        ),
    )
    with bound(board_id=BOARD):
        first = await tools["board_create_card"].run(
            intent_id=view["intent_id"], operation_key="k1", content="结果卡一"
        )
        assert first.ok is True, first.error
        assert first.data["replayed"] is False
        card_id = first.data["result"]["card_id"]
        created = current_card(conn, BOARD, card_id)
        assert created["kind"] == models.REPLY_KIND
        assert created["content"] == "结果卡一"
        assert not created["checked"]
        replay = await tools["board_create_card"].run(
            intent_id=view["intent_id"], operation_key="k1", content="结果卡一"
        )
        assert replay.ok is True
        assert replay.data["replayed"] is True
        assert replay.data["result"]["card_id"] == card_id
        conflict = await tools["board_create_card"].run(
            intent_id=view["intent_id"], operation_key="k1", content="不同请求"
        )
        assert conflict.ok is False
        assert conflict.code == "operation_key_conflict"
        second = await tools["board_create_card"].run(
            intent_id=view["intent_id"], operation_key="k2", content="结果卡二"
        )
        assert second.ok is True
        third = await tools["board_create_card"].run(
            intent_id=view["intent_id"], operation_key="k3", content="结果卡三"
        )
        assert third.ok is False
        assert third.code == "scope_limit"
    # 重试不重置计数：恢复后的下一次尝试仍然按整个意图累计。
    assert intent_service.new_objects_used(conn, view["intent_id"])["cards"] == 2


async def test_update_card_version_conflict_is_isolated(env):
    conn, _app, tools, card, _hidden = env
    version = int(current_card(conn, BOARD, card)["version"])
    view = _intent(
        conn,
        scope=intent_scope(read_card_ids=[card], write_card_ids=[card], allowed_actions=["update_card"]),
    )
    with bound(board_id=BOARD):
        first = await tools["board_update_card"].run(
            intent_id=view["intent_id"],
            operation_key="u1",
            card_id=card,
            expected_version=version,
            patch={"content": "用户材料一（整理后）"},
        )
        assert first.ok is True, first.error
        after_version = int(current_card(conn, BOARD, card)["version"])
        assert after_version == version + 1
        stale = await tools["board_update_card"].run(
            intent_id=view["intent_id"],
            operation_key="u2",
            card_id=card,
            expected_version=version,
            patch={"content": "不应落地"},
        )
        assert stale.ok is False
        assert stale.code == "version_conflict"
        assert current_card(conn, BOARD, card)["content"] == "用户材料一（整理后）"
        ledger = intent_service.ledger_operations(
            conn, board_id=BOARD, intent_id=view["intent_id"]
        )
        statuses = [op["status"] for op in ledger["operations"]]
        assert "applied" in statuses and "conflict" in statuses


async def test_write_scope_is_not_read_scope(env):
    conn, _app, tools, card, hidden = env
    # 隐藏 / 未勾选卡片不进入授权范围（read scope 只列获准读取的卡片）。
    view = _intent(
        conn,
        scope=intent_scope(
            read_card_ids=[card],
            write_card_ids=[card],
            allowed_actions=["update_card"],
        ),
    )
    with bound(board_id=BOARD):
        denied = await tools["board_create_card"].run(
            intent_id=view["intent_id"], operation_key="x1", content="越权创建"
        )
        assert denied.ok is False
        assert denied.code == "scope_violation"
        denied_update = await tools["board_update_card"].run(
            intent_id=view["intent_id"],
            operation_key="x2",
            card_id=hidden,
            expected_version=1,
            patch={"content": "查看许可不是编辑许可"},
        )
        assert denied_update.ok is False
        assert denied_update.code == "scope_violation"


async def test_relative_position_requires_authorized_card(env):
    conn, _app, tools, card, hidden = env
    visible = add_visible_card(conn, content="第二张可见材料")
    view = _intent(
        conn,
        scope=intent_scope(
            read_card_ids=[card, visible],
            write_card_ids=[card, visible],
            allowed_actions=["move_card"],
        ),
    )
    with bound(board_id=BOARD):
        denied = await tools["board_move_card"].run(
            intent_id=view["intent_id"],
            operation_key="m1",
            card_id=card,
            expected_version=int(current_card(conn, BOARD, card)["version"]),
            position={"kind": "relative", "relative_to": hidden, "side": "right", "gap": 10},
        )
        assert denied.ok is False
        assert denied.code == "invalid_position"
        moved = await tools["board_move_card"].run(
            intent_id=view["intent_id"],
            operation_key="m2",
            card_id=card,
            expected_version=int(current_card(conn, BOARD, card)["version"]),
            position={"kind": "relative", "relative_to": visible, "side": "below", "gap": 12},
        )
        assert moved.ok is True, moved.error
        assert moved.data["after"]["y"] > 0


async def test_group_and_order_permissions_and_limits(env):
    conn, _app, tools, card, _hidden = env
    second = add_visible_card(conn, content="第二张可见材料")
    state = board_store.current_state(conn, BOARD)
    state = board.create_group(state, [card, second], "现有组")
    group_id = str(state["groups"][-1]["id"])
    board_store.save_board(conn, BOARD, state, reason="test")
    state = board_store.current_state(conn, BOARD)
    card_version = int(next(c["version"] for c in state["cards"] if c["id"] == card))
    second_version = int(next(c["version"] for c in state["cards"] if c["id"] == second))
    view = _intent(
        conn,
        scope=intent_scope(
            read_card_ids=[card, second],
            group_ids=[group_id],
            allowed_actions=["group_cards", "order_group"],
            max_new_groups=1,
        ),
    )
    with bound(board_id=BOARD):
        missing = await tools["board_group_cards"].run(
            intent_id=view["intent_id"], operation_key="g1", card_ids=[card, second]
        )
        assert missing.ok is False
        assert missing.code == "expected_versions_required"
        ordered = await tools["board_order_group"].run(
            intent_id=view["intent_id"],
            operation_key="g2",
            group_id=group_id,
            member_order=[second, card],
            expected_versions={card: card_version, second: second_version},
            arrange="row",
        )
        assert ordered.ok is True, ordered.error
        assert ordered.data["after"]["group"]["members"] == [second, card]
        # 新组受 max_new_groups 限制（同一意图累计）。
        third = add_visible_card(conn, content="第三张可见材料")
        state = board_store.current_state(conn, BOARD)
        third_version = int(next(c["version"] for c in state["cards"] if c["id"] == third))
        new_group = await tools["board_group_cards"].run(
            intent_id=view["intent_id"],
            operation_key="g3",
            card_ids=[third],
            expected_versions={third: third_version},
        )
        assert new_group.ok is True, new_group.error
        assert new_group.data["result"]["created_group_id"]
        fourth = add_visible_card(conn, content="第四张可见材料")
        state = board_store.current_state(conn, BOARD)
        fourth_version = int(next(c["version"] for c in state["cards"] if c["id"] == fourth))
        exceeded = await tools["board_group_cards"].run(
            intent_id=view["intent_id"],
            operation_key="g4",
            card_ids=[fourth],
            expected_versions={fourth: fourth_version},
        )
        assert exceeded.ok is False
        assert exceeded.code == "scope_limit"


async def test_add_link_scope_and_replay(env):
    conn, _app, tools, card, _hidden = env
    second = add_visible_card(conn, content="第二张可见材料")
    view = _intent(
        conn,
        scope=intent_scope(
            read_card_ids=[card, second],
            allowed_actions=["add_link"],
            max_new_links=1,
        ),
    )
    with bound(board_id=BOARD):
        linked = await tools["board_add_link"].run(
            intent_id=view["intent_id"], operation_key="l1", src=card, dst=second, direction=True
        )
        assert linked.ok is True, linked.error
        assert linked.data["result"]["created"] is True
        replay = await tools["board_add_link"].run(
            intent_id=view["intent_id"], operation_key="l1", src=card, dst=second, direction=True
        )
        assert replay.ok is True
        assert replay.data["replayed"] is True


async def test_finish_checks_ledger_and_late_completion(env):
    conn, _app, tools, card, _hidden = env
    view = _intent(
        conn,
        scope=intent_scope(read_card_ids=[card], allowed_actions=["create_card"], max_new_cards=2),
    )
    intent_id = view["intent_id"]
    with bound(board_id=BOARD):
        first = await tools["board_create_card"].run(
            intent_id=intent_id, operation_key="k1", content="结果一"
        )
        assert first.ok is True
        blocked = await tools["board_finish_intent"].run(
            intent_id=intent_id,
            expected_version=1,
            outcome="completed",
            summary="完成",
            operation_keys=[],
        )
        assert blocked.ok is False
        assert blocked.code == "completion_blocked"
        blockers = blocked.data.get("blockers") or []
        assert any(item["kind"] == "unlisted_applied_operations" for item in blockers)
        done = await tools["board_finish_intent"].run(
            intent_id=intent_id,
            expected_version=1,
            outcome="completed",
            summary="完成",
            operation_keys=["k1"],
        )
        assert done.ok is True, done.error
        assert done.status == "completed"
        assert done.data["results"][0]["operation_key"] == "k1"
        late = await tools["board_finish_intent"].run(
            intent_id=intent_id,
            expected_version=1,
            outcome="failed",
            summary="迟到",
            operation_keys=[],
        )
        assert late.ok is False
    assert intent_service.cancel_intent(
        conn, board_id=BOARD, intent_id=intent_id, expected_version=1
    )["ok"] is False  # 已完成后不能取消（版本/状态保护）


async def test_partial_completion_tool_and_recovery(env):
    conn, _app, tools, card, _hidden = env
    view = _intent(
        conn,
        scope=intent_scope(read_card_ids=[card], allowed_actions=["create_card"], max_new_cards=2),
    )
    intent_id = view["intent_id"]
    with bound(board_id=BOARD):
        created = await tools["board_create_card"].run(
            intent_id=intent_id, operation_key="k1", content="部分成果"
        )
        assert created.ok is True
        partial = await tools["board_finish_intent"].run(
            intent_id=intent_id,
            expected_version=1,
            outcome="partially_completed",
            summary="先完成一张，等待继续",
            operation_keys=["k1"],
        )
        assert partial.ok is True
        assert partial.status == "partially_completed"
        assert partial.data["results"]
        preview = intent_service.preview_recovery(conn, intent_id)
        assert preview["can_resume"] is True
        recovered = intent_service.recover_intent(
            conn, board_id=BOARD, intent_id=intent_id, expected_version=1
        )
        assert recovered["ok"] is True
        done = await tools["board_finish_intent"].run(
            intent_id=intent_id,
            expected_version=2,
            outcome="completed",
            summary="全部完成",
            operation_keys=["k1"],
        )
        assert done.ok is True, done.error
        assert done.status == "completed"


async def test_update_intent_expands_scope_needs_reconfirmation(env):
    conn, _app, tools, card, _hidden = env
    view = _intent(
        conn,
        scope=intent_scope(read_card_ids=[card], allowed_actions=["create_card"], max_new_cards=1),
    )
    with bound(board_id=BOARD):
        changed = await tools["board_update_intent"].run(
            intent_id=view["intent_id"],
            expected_version=1,
            changes={"scope": intent_scope(read_card_ids=[card], allowed_actions=["create_card"], max_new_cards=5)},
        )
        assert changed.ok is True
        assert changed.status == "awaiting_approval"
        assert changed.data["requires_confirmation"] is True
        denied = await tools["board_create_card"].run(
            intent_id=view["intent_id"], operation_key="k1", content="旧授权不可用"
        )
        assert denied.ok is False
        assert denied.code in ("awaiting_approval", "authorization_stale")
