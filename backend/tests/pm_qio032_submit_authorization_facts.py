# -*- coding: utf-8 -*-
"""PM-QIO032 #27：授权失效事实（后端）。

BRIEF #27：首次未授权显示「待确认」；只有原授权因范围变化确实失效时才显示
「需重新确认」，并在按钮前给出真实变更（范围变化前后差异），不能用时间/版本变化冒充。

后端契约（只增不改）：IntentView 增加
- authorization_invalidated: bool —— 当前无有效授权，且历史上确实存在过非空授权快照；
- authorization_changes: list[str] —— 用最近一条非空授权快照与当前行比较出的真实中文差异；
  没有可核对差异时必须是 []（不猜、不编造）。

本文件先写红（字段不存在 / 语义不符），再由实现转绿。
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.api import interactive_intents as api_module
from agent.interactive import board_store, intent_service

from test_p3_b_support import (
    add_submission,
    add_visible_card,
    intent_scope,
    intent_spec,
    make_board,
    prepare,
)

BOARD = "board_p3"


def _env(db_conn):
    prepare(db_conn)
    make_board(db_conn, BOARD)
    add_submission(db_conn, BOARD)
    return db_conn


def _cards(conn) -> list[str]:
    return [str(card["id"]) for card in board_store.current_state(conn, BOARD)["cards"]]


def _propose(conn, scope: dict) -> dict:
    proposed = intent_service.propose_intents(
        conn,
        board_id=BOARD,
        submission_id="sub_p3_1",
        intents=[intent_spec("a", scope=scope)],
    )
    assert proposed["ok"] is True, proposed
    return proposed["intents"][0]


def _update_scope(conn, intent_id: str, scope: dict) -> dict:
    return intent_service.update_intent(
        conn,
        board_id=BOARD,
        intent_id=intent_id,
        expected_version=1,
        changes={"scope": scope},
    )


def test_never_authorized_is_not_invalidated(db_conn):
    """从未授权过：不能显示「需重新确认」，也没有变更事实。"""
    conn = _env(db_conn)
    card_id = add_visible_card(conn, BOARD, content="材料一")
    view = _propose(
        conn,
        intent_scope(
            read_card_ids=[card_id],
            write_card_ids=[card_id],
            allowed_actions=["update_card"],
        ),
    )
    assert view["authorization_valid"] is False
    assert view["authorization_invalidated"] is False
    assert view["authorization_changes"] == []


def test_update_before_authorization_does_not_claim_reconfirmation(db_conn):
    """首次确认之前的更新不算「原授权失效」。"""
    conn = _env(db_conn)
    card_id = add_visible_card(conn, BOARD, content="材料一")
    view = _propose(
        conn,
        intent_scope(
            read_card_ids=[card_id],
            write_card_ids=[card_id],
            allowed_actions=["update_card"],
        ),
    )
    updated = intent_service.update_intent(
        conn,
        board_id=BOARD,
        intent_id=view["intent_id"],
        expected_version=1,
        changes={"goal": "更明确的目标"},
    )
    assert updated["ok"] is True
    got = intent_service.get_intent(conn, BOARD, view["intent_id"])
    assert got["authorization_valid"] is False
    assert got["authorization_invalidated"] is False
    assert got["authorization_changes"] == []


def test_scope_change_after_confirmation_gives_real_changes(db_conn):
    """确认后范围实质扩大：标记「原授权失效」并给出可核对的真实差异。"""
    conn = _env(db_conn)
    card_a = add_visible_card(conn, BOARD, content="材料一")
    card_b = add_visible_card(conn, BOARD, content="材料二")
    view = _propose(
        conn,
        intent_scope(
            read_card_ids=[card_a],
            write_card_ids=[card_a],
            allowed_actions=["update_card"],
            max_new_cards=1,
        ),
    )
    intent_id = view["intent_id"]
    confirmed = intent_service.confirm_intents(conn, board_id=BOARD, intent_ids=[intent_id])
    assert confirmed["ok"] is True
    updated = _update_scope(
        conn,
        intent_id,
        intent_scope(
            read_card_ids=[card_a, card_b],
            write_card_ids=[card_a],
            allowed_actions=["update_card", "create_card"],
            max_new_cards=2,
        ),
    )
    assert updated["ok"] is True, updated
    assert updated["requires_confirmation"] is True

    got = intent_service.get_intent(conn, BOARD, intent_id)
    assert got["status"] == intent_service.STATUS_AWAITING
    assert got["authorization_valid"] is False
    assert got["authorization_invalidated"] is True
    changes = got["authorization_changes"]
    assert isinstance(changes, list) and changes, "失效时必须给出真实变更事实"
    joined = "\n".join(changes)
    # 真实差异：读取范围 1 张卡 → 2 张卡、允许动作增加、新建上限变化。
    assert "读取" in joined and "1 张卡" in joined and "2 张卡" in joined
    assert "允许动作" in joined and "create_card" in joined
    assert "新建卡片上限" in joined and "1" in joined and "2" in joined


def test_reconfirmation_clears_invalidation(db_conn):
    """重新确认后授权恢复有效：不再显示「需重新确认」，也不残留旧的变更列表。"""
    conn = _env(db_conn)
    card_a = add_visible_card(conn, BOARD, content="材料一")
    card_b = add_visible_card(conn, BOARD, content="材料二")
    view = _propose(
        conn,
        intent_scope(
            read_card_ids=[card_a],
            write_card_ids=[card_a],
            allowed_actions=["update_card"],
        ),
    )
    intent_id = view["intent_id"]
    intent_service.confirm_intents(conn, board_id=BOARD, intent_ids=[intent_id])
    assert _update_scope(
        conn,
        intent_id,
        intent_scope(
            read_card_ids=[card_a, card_b],
            write_card_ids=[card_a],
            allowed_actions=["update_card"],
        ),
    )["ok"] is True
    invalidated = intent_service.get_intent(conn, BOARD, intent_id)
    assert invalidated["authorization_invalidated"] is True

    reconfirmed = intent_service.confirm_intents(conn, board_id=BOARD, intent_ids=[intent_id])
    assert reconfirmed["ok"] is True
    got = intent_service.get_intent(conn, BOARD, intent_id)
    assert got["authorization_valid"] is True
    assert got["authorization_invalidated"] is False
    assert got["authorization_changes"] == []


def test_non_substantive_update_keeps_authorization_and_no_changes(db_conn):
    """只改标题（非实质）：授权仍有效，不出现「需重新确认」。"""
    conn = _env(db_conn)
    card_id = add_visible_card(conn, BOARD, content="材料一")
    view = _propose(
        conn,
        intent_scope(
            read_card_ids=[card_id],
            write_card_ids=[card_id],
            allowed_actions=["update_card"],
        ),
    )
    intent_id = view["intent_id"]
    intent_service.confirm_intents(conn, board_id=BOARD, intent_ids=[intent_id])
    updated = intent_service.update_intent(
        conn,
        board_id=BOARD,
        intent_id=intent_id,
        expected_version=1,
        changes={"title": "换个标题"},
    )
    assert updated["ok"] is True and updated["requires_confirmation"] is False
    got = intent_service.get_intent(conn, BOARD, intent_id)
    assert got["authorization_valid"] is True
    assert got["authorization_invalidated"] is False
    assert got["authorization_changes"] == []


def _client(conn) -> TestClient:
    app = FastAPI()
    app.state.ctx = SimpleNamespace(conn=conn)
    app.include_router(api_module.router)
    return TestClient(app)


def test_intent_views_api_exposes_fields_without_changing_old_semantics(db_conn):
    """列表接口（真实服务路径）逐条带上新字段，旧字段语义不变。"""
    conn = _env(db_conn)
    card_a = add_visible_card(conn, BOARD, content="材料一")
    card_b = add_visible_card(conn, BOARD, content="材料二")
    view = _propose(
        conn,
        intent_scope(
            read_card_ids=[card_a],
            write_card_ids=[card_a],
            allowed_actions=["update_card"],
        ),
    )
    intent_id = view["intent_id"]
    intent_service.confirm_intents(conn, board_id=BOARD, intent_ids=[intent_id])
    assert _update_scope(
        conn,
        intent_id,
        intent_scope(
            read_card_ids=[card_a, card_b],
            write_card_ids=[card_a],
            allowed_actions=["update_card"],
        ),
    )["ok"] is True

    response = _client(conn).get(f"/api/interactive/boards/{BOARD}/intent-views")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "service"
    item = next(
        view for view in body["intents"] if view.get("intent_id") == intent_id
    )
    assert item["authorization_valid"] is False
    assert item["authorization_invalidated"] is True
    assert item["authorization_changes"], "接口必须把真实变更事实带给前端"
    # 旧字段语义不变：status / version / scope 仍是原事实。
    assert item["status"] == intent_service.STATUS_AWAITING
    assert item["version"] == 2


def test_legacy_view_shape_keeps_new_fields_without_claiming_invalidation():
    legacy = api_module._legacy_intent_view(
        {
            "id": "in_old",
            "boardId": BOARD,
            "status": "proposed",
            "version": 1,
            "title": "旧意图",
            "goal": "目标",
        }
    )
    assert legacy["authorization_invalidated"] is False
    assert legacy["authorization_changes"] == []
