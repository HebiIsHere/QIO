# -*- coding: utf-8 -*-
"""P73 Stage 1 定向测试：意图操作计划 / 板面预演 / 批准即原子落地。

覆盖：
1. 带 operations 的提案派生 preview 与 scope；
2. confirm 后 apply_approved_intent 在同一请求内落成卡片 / 分组 / 关系；
3. 幂等：重复确认 / 重复应用不重复落卡；
4. 批准后板面变化 → board_changed，不写入、退回待确认；
5. 预演编辑：拖动改落点、删除计划项；已确认后不可编辑；
6. 非法计划（越权操作 / 空正文）被拒绝；
7. HTTP：PATCH /plan 与 POST /confirm 的真实服务接线（含 applied / execution 返回）；
8. 不带 operations 的旧意图行为不变（不被落地，走既有续作）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.api import interactive_intents as api_module
from agent.interactive import board, board_store, intent_service

from test_p3_b_support import add_submission, add_visible_card, intent_spec, make_board, prepare

BOARD = "board_p3"


@pytest.fixture()
def env(db_conn):
    prepare(db_conn)
    make_board(db_conn)
    add_submission(db_conn)
    return db_conn


def _ops() -> list[dict]:
    return [
        {"op": "add_reply_card", "content": "第一张卡", "ref": "c1", "x": 40.0, "y": 40.0},
        {"op": "add_reply_card", "content": "第二张卡", "ref": "c2", "x": 420.0, "y": 40.0},
        {"op": "group_cards", "cardIds": ["c1", "c2"], "ref": "g1"},
        {"op": "add_link", "src": "c1", "dst": "c2", "meaning": "递进"},
    ]


def _spec(ops: list[dict] | None = None, client_ref: str = "cards") -> dict:
    spec = intent_spec(
        client_ref,
        title="落卡任务",
        goal="把两张论文卡落到板面",
        expected="板面出现两张结果卡、一个分组和一条关系",
    )
    spec["operations"] = list(ops if ops is not None else _ops())
    return spec


def _propose(conn, spec: dict | None = None) -> dict:
    proposed = intent_service.propose_intents(
        conn, board_id=BOARD, submission_id="sub_p3_1", intents=[spec or _spec()]
    )
    assert proposed["ok"] is True, proposed
    return proposed["intents"][0]


def _confirm(conn, intent_id: str) -> dict:
    result = intent_service.confirm_intents(conn, board_id=BOARD, intent_ids=[intent_id])
    assert result["ok"] is True, result
    return result


def _apply(conn, intent_id: str) -> dict:
    return intent_service.apply_approved_intent(conn, board_id=BOARD, intent_id=intent_id)


def _state(conn) -> dict:
    return board_store.current_state(conn, BOARD)


def _cards_by_content(conn) -> dict[str, dict]:
    return {str(card.get("content")): card for card in _state(conn).get("cards") or []}


def test_propose_plan_derives_preview_and_scope(env):
    view = _propose(env)
    assert view["status"] == intent_service.STATUS_AWAITING
    assert view["plan_editable"] is True
    assert len(view["operations"]) == 4
    preview = view["preview"]
    assert [card["content"] for card in preview["cards"]] == ["第一张卡", "第二张卡"]
    assert len(preview["groups"]) == 1
    assert len(preview["links"]) == 1
    scope = view["scope"]
    assert scope["allowed_actions"] == ["create_card", "group_cards", "add_link"]
    assert scope["max_new_cards"] == 2
    assert scope["max_new_groups"] == 1
    assert scope["max_new_links"] == 1
    # 无 operations 的旧提案不受影响
    plain = intent_spec("plain", title="整理材料", goal="整理", expected="一张卡")
    plain_out = intent_service.propose_intents(
        env, board_id=BOARD, submission_id="sub_p3_1", intents=[plain]
    )
    assert plain_out["ok"] is True
    plain_view = plain_out["intents"][0]
    assert plain_view["operations"] == []
    assert plain_view["preview"] == {}
    assert plain_view["plan_editable"] is True


def test_preview_group_frame_uses_member_positions(env):
    """预览组框必须由成员位置算出来（不是占位 0/0/320×240），且与落地后一致。"""
    view = _propose(env)
    preview = view["preview"]
    cards = preview["cards"]
    group = preview["groups"][0]
    left = min(float(card["x"]) for card in cards) - board.GROUP_PAD_X
    top = min(float(card["y"]) for card in cards) - board.GROUP_PAD_TOP
    right = max(float(card["x"]) + float(card["w"]) for card in cards) + board.GROUP_PAD_X
    bottom = max(float(card["y"]) + float(card["h"]) for card in cards) + board.GROUP_PAD_BOTTOM
    assert float(group["x"]) == pytest.approx(left)
    assert float(group["y"]) == pytest.approx(top)
    assert float(group["w"]) == pytest.approx(right - left)
    assert float(group["h"]) == pytest.approx(bottom - top)
    # 不能还是 models.new_group 的占位框
    assert not (
        float(group["x"]) == 0.0
        and float(group["y"]) == 0.0
        and float(group["w"]) == 320.0
        and float(group["h"]) == 240.0
    )

    intent_id = view["intent_id"]
    _confirm(env, intent_id)
    result = _apply(env, intent_id)
    assert result["ok"] is True
    landed_groups = [g for g in _state(env)["groups"] if not g.get("deleted")]
    assert len(landed_groups) == 1
    landed = landed_groups[0]
    assert float(landed["x"]) == pytest.approx(float(group["x"]))
    assert float(landed["y"]) == pytest.approx(float(group["y"]))
    assert float(landed["w"]) == pytest.approx(float(group["w"]))
    assert float(landed["h"]) == pytest.approx(float(group["h"]))


def test_confirm_then_apply_lands_cards_groups_links(env):
    view = _propose(env)
    intent_id = view["intent_id"]
    _confirm(env, intent_id)
    result = _apply(env, intent_id)
    assert result["ok"] is True
    assert result["already"] is False
    applied = result["applied"]
    assert len(applied["cardIds"]) == 2
    assert len(applied["groupIds"]) == 1
    assert len(applied["linkIds"]) == 1
    assert set(applied["previewIdMap"]) == {"c1", "c2", "g1"}

    state = _state(env)
    cards = _cards_by_content(env)
    assert "第一张卡" in cards and "第二张卡" in cards
    assert len(state["groups"]) == 1
    assert len(state["links"]) == 1
    # 预演坐标 = 落地坐标（同一 placement 实现）
    preview_cards = {card["content"]: card for card in view["preview"]["cards"]}
    for content, preview_card in preview_cards.items():
        landed = cards[content]
        assert float(landed["x"]) == pytest.approx(float(preview_card["x"]))
        assert float(landed["y"]) == pytest.approx(float(preview_card["y"]))

    done = intent_service.get_intent(env, BOARD, intent_id)
    assert done["status"] == intent_service.STATUS_COMPLETED
    assert done["operations_summary"]["applied"] == 4
    execution = env.execute(
        "SELECT status FROM board_executions WHERE intent_id = ?", (intent_id,)
    ).fetchone()
    assert execution is not None and str(execution["status"]) == "done"
    ledger = env.execute(
        "SELECT COUNT(*) AS c FROM board_operations WHERE intent_id = ? AND status = 'applied'",
        (intent_id,),
    ).fetchone()
    assert int(ledger["c"]) == 4

    # 幂等：重复确认 / 重复应用都不再落卡
    _confirm(env, intent_id)
    again = _apply(env, intent_id)
    assert again["ok"] is True and again["already"] is True
    assert len(_state(env)["cards"]) == 2


def test_apply_after_board_changed_is_rejected(env):
    view = _propose(env)
    intent_id = view["intent_id"]
    _confirm(env, intent_id)
    add_visible_card(env, content="批准之后用户新增的材料")
    seq_after_user_edit = board_store.saved_seq(env, BOARD)

    result = _apply(env, intent_id)
    assert result["ok"] is False
    assert result["reason"] == "board_changed"
    # 没有写入计划里的卡；板面只有用户自己加的那张
    cards = _cards_by_content(env)
    assert set(cards) == {"批准之后用户新增的材料"}
    assert board_store.saved_seq(env, BOARD) == seq_after_user_edit
    # 意图退回待确认，需要重新确认（旧授权不复活）
    fresh = intent_service.get_intent(env, BOARD, intent_id)
    assert fresh["status"] == intent_service.STATUS_AWAITING
    assert fresh["authorization"] == {}


def test_update_plan_moves_and_removes(env):
    view = _propose(env)
    intent_id = view["intent_id"]
    ops = [dict(item) for item in view["operations"]]
    moved = [dict(item) for item in ops]
    moved[0]["x"] = 900.0
    moved[0]["y"] = 120.0
    updated = intent_service.update_plan(
        env, board_id=BOARD, intent_id=intent_id, operations=moved
    )
    assert updated["ok"] is True, updated
    preview = updated["intent"]["preview"]
    first = next(card for card in preview["cards"] if card["content"] == "第一张卡")
    assert float(first["x"]) == pytest.approx(900.0)

    # 删掉第二张卡及引用它的分组 / 关系操作
    trimmed = [dict(item) for item in moved if item.get("ref") != "c2"]
    trimmed = [
        item for item in trimmed
        if not (item.get("op") == "group_cards") and not (item.get("op") == "add_link")
    ]
    removed = intent_service.update_plan(
        env, board_id=BOARD, intent_id=intent_id, operations=trimmed
    )
    assert removed["ok"] is True, removed
    assert [card["content"] for card in removed["intent"]["preview"]["cards"]] == ["第一张卡"]
    assert removed["intent"]["preview"]["groups"] == []
    assert removed["intent"]["preview"]["links"] == []

    _confirm(env, intent_id)
    result = _apply(env, intent_id)
    assert result["ok"] is True
    assert set(_cards_by_content(env)) == {"第一张卡"}


def test_update_plan_rejected_after_confirm(env):
    view = _propose(env)
    intent_id = view["intent_id"]
    _confirm(env, intent_id)
    updated = intent_service.update_plan(
        env, board_id=BOARD, intent_id=intent_id, operations=view["operations"]
    )
    assert updated["ok"] is False
    assert updated["code"] == "not_editable"
    assert updated["status"] == intent_service.STATUS_APPROVED


def test_invalid_plan_is_rejected_without_writing(env):
    forbidden = _ops()
    forbidden[0] = {"op": "delete_card", "cardId": "c1"}
    bad = intent_service.propose_intents(
        env, board_id=BOARD, submission_id="sub_p3_1", intents=[_spec(forbidden)]
    )
    assert bad["ok"] is False and bad["code"] == "invalid_plan"

    empty = _ops()
    empty[0] = {"op": "add_reply_card", "content": "   ", "ref": "c1"}
    bad_empty = intent_service.propose_intents(
        env, board_id=BOARD, submission_id="sub_p3_1", intents=[_spec(empty)]
    )
    assert bad_empty["ok"] is False and bad_empty["code"] == "invalid_plan"

    view = _propose(env)
    bad_edit = intent_service.update_plan(
        env, board_id=BOARD, intent_id=view["intent_id"], operations=[{"op": "delete_card"}]
    )
    assert bad_edit["ok"] is False and bad_edit["code"] == "invalid_plan"
    fresh = intent_service.get_intent(env, BOARD, view["intent_id"])
    assert len(fresh["operations"]) == 4
    assert len(fresh["preview"]["cards"]) == 2


def _api_client(conn) -> TestClient:
    app = FastAPI()
    app.include_router(api_module.router)
    app.state.ctx = SimpleNamespace(conn=conn)
    app.state.instance_id = "inst_test"
    app.state.p3_intent_service = None
    return TestClient(app)


def test_http_patch_plan_and_confirm_apply(env):
    view = _propose(env)
    intent_id = view["intent_id"]
    client = _api_client(env)
    ops = [dict(item) for item in view["operations"]]
    ops[1]["x"] = 640.0
    patch = client.patch(
        f"/api/interactive/intents/{intent_id}/plan",
        json={"boardId": BOARD, "operations": ops},
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["ok"] is True
    preview = patch.json()["intent"]["preview"]
    assert float(next(c for c in preview["cards"] if c["content"] == "第二张卡")["x"]) == pytest.approx(640.0)

    confirm = client.post(
        f"/api/interactive/intents/{intent_id}/confirm", json={"boardId": BOARD}
    )
    assert confirm.status_code == 200, confirm.text
    body = confirm.json()
    assert body["ok"] is True
    assert body["applied"][intent_id]["cardIds"]
    assert body["execution"]["status"] == "done"
    assert body["intent"]["status"] == intent_service.STATUS_COMPLETED
    assert len(_state(env)["cards"]) == 2

    # 已确认后 PATCH 预演 → 409
    rejected = client.patch(
        f"/api/interactive/intents/{intent_id}/plan",
        json={"boardId": BOARD, "operations": ops},
    )
    assert rejected.status_code == 409


def test_plain_intent_confirm_does_not_apply(env):
    plain = intent_spec("plain", title="整理材料", goal="整理", expected="一张卡")
    proposed = intent_service.propose_intents(
        env, board_id=BOARD, submission_id="sub_p3_1", intents=[plain]
    )
    assert proposed["ok"] is True
    intent_id = proposed["intents"][0]["intent_id"]
    client = _api_client(env)
    confirm = client.post(f"/api/interactive/intents/{intent_id}/confirm", json={"boardId": BOARD})
    assert confirm.status_code == 200, confirm.text
    body = confirm.json()
    assert body["ok"] is True
    assert "applied" not in body
    fresh = intent_service.get_intent(env, BOARD, intent_id)
    assert fresh["status"] == intent_service.STATUS_APPROVED
    result = _apply(env, intent_id)
    assert result["ok"] is False and result.get("reason") == "no_operations"
