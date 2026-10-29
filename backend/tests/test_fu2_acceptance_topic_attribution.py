# -*- coding: utf-8 -*-
"""F 独立验收（基线 373fda40）· 反例 1/5：互动续作的话题归属。

契约（COMMON §4.1）：确认 / 恢复 / 重试启动续作 Turn 时，话题必须优先取**原提交保存**的
binding.resumeState.topicId；当前选中话题不得覆盖它。binding 缺失或 topicId 为空时
必须明确失败（code 建议 topic_unresolved），不回退当前话题、不启动 Turn。

基线预期（红）：API 层 _start_intent_turn 把 _current_topic(request) 直接传给
submission.start_interactive_turn，后者只在 topic_id 为空时才读 binding，
因此「当前选中话题」会覆盖「原提交话题」；无绑定时也会拿当前话题起 Turn。
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.api import interactive_intents as api_module
from agent.interactive import intent_service, submission

from test_p3_b_support import add_submission, intent_scope, intent_spec, make_board, prepare

BOARD = "board_fu2_topic"
SUBMISSION = "sub_fu2_topic"
SAVED_TOPIC = "topic-saved"
CURRENT_TOPIC = "topic-current"


class FakeTurn:
    def __init__(self, turn_id: str, message: str, topic_id: str | None) -> None:
        self.turn_id = turn_id
        self.message = message
        self.board_topic_id = topic_id


class FakeTurns:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def submit(self, message: str, topic_id: str | None = None, **kwargs):  # noqa: ANN003
        turn = FakeTurn(f"turn_fu2_{len(self.calls) + 1}", message, topic_id)
        self.calls.append({"message": message, "topicId": topic_id, "turn": turn})
        return turn


def _board_env(db_conn):
    prepare(db_conn)
    make_board(db_conn, board_id=BOARD)
    add_submission(db_conn, board_id=BOARD, submission_id=SUBMISSION)
    return db_conn


def _approved_intent(conn) -> str:
    proposed = intent_service.propose_intents(
        conn,
        board_id=BOARD,
        submission_id=SUBMISSION,
        intents=[intent_spec(scope=intent_scope(allowed_actions=["create_card"]))],
    )
    assert proposed["ok"], proposed
    intent_id = proposed["intents"][0]["intent_id"]
    confirmed = intent_service.confirm_intents(conn, board_id=BOARD, intent_ids=[intent_id])
    assert confirmed["ok"], confirmed
    return intent_id


def _api_client(conn, turns: FakeTurns, current_topic: str | None) -> TestClient:
    app = FastAPI()
    app.state.ctx = SimpleNamespace(
        conn=conn,
        turns=turns,
        current_topic=(lambda: current_topic),
    )
    app.include_router(api_module.router)
    return TestClient(app)


# --- 1) 原提交保存的话题优先（直接调用真实续作入口） ------------------------------


def test_saved_submission_topic_wins_over_current_selection(db_conn):
    conn = _board_env(db_conn)
    intent_id = _approved_intent(conn)
    submission.bind_submission_turn(
        conn,
        submission_id=SUBMISSION,
        turn_id="turn_previous",
        resume_state={"kind": "intent_resume", "topicId": SAVED_TOPIC},
    )
    turns = FakeTurns()
    started = submission.start_interactive_turn(
        conn,
        turn_submitter=turns.submit,
        board_id=BOARD,
        intent_ids=[intent_id],
        submission_id=SUBMISSION,
        source="confirm",
        resume=True,
        # 与 API 当前实现一致：把「当前选中话题」显式传进来
        topic_id=CURRENT_TOPIC,
    )
    assert started["ok"] is True, started
    assert turns.calls, "应当真实提交了一次续作 Turn"
    turn = turns.calls[0]["turn"]
    assert turn.board_topic_id == SAVED_TOPIC, (
        "续作 Turn 用了当前选中话题而不是原提交保存的话题："
        f"expected {SAVED_TOPIC!r}, got {turn.board_topic_id!r}"
    )
    binding = submission.submission_binding(conn, SUBMISSION)
    assert binding is not None
    assert binding["resumeState"].get("topicId") == SAVED_TOPIC


# --- 2) 经真实 API 的确认路径：原提交话题优先 ------------------------------------


def test_confirm_api_uses_saved_topic_not_current_selection(db_conn):
    conn = _board_env(db_conn)
    intent_id = _approved_intent(conn)
    submission.bind_submission_turn(
        conn,
        submission_id=SUBMISSION,
        turn_id="turn_previous",
        resume_state={"kind": "intent_resume", "topicId": SAVED_TOPIC},
    )
    turns = FakeTurns()
    client = _api_client(conn, turns, CURRENT_TOPIC)
    response = client.post(
        f"/api/interactive/intents/{intent_id}/confirm", json={"boardId": BOARD}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body.get("started") is True, body
    assert turns.calls, "应当真实提交了一次续作 Turn"
    assert turns.calls[0]["turn"].board_topic_id == SAVED_TOPIC, (
        "确认接口把当前选中话题覆盖了原提交保存的话题："
        f"expected {SAVED_TOPIC!r}, got {turns.calls[0]['turn'].board_topic_id!r}"
    )


# --- 3) 无绑定 / 话题为空：明确失败，不回退当前话题 --------------------------------


def test_confirm_api_without_saved_topic_fails_explicitly(db_conn):
    conn = _board_env(db_conn)
    intent_id = _approved_intent(conn)
    turns = FakeTurns()
    client = _api_client(conn, turns, CURRENT_TOPIC)
    response = client.post(
        f"/api/interactive/intents/{intent_id}/confirm", json={"boardId": BOARD}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    turn_start = body.get("turnStart") or {}
    assert body.get("started") is False, (
        "没有原提交话题却启动了续作 Turn（回退到当前话题）："
        + str({k: body.get(k) for k in ("ok", "started", "turnId", "turnStart")})
    )
    assert turn_start.get("code") == "topic_unresolved", (
        "缺少原提交话题时必须明确返回 topic_unresolved，实际："
        + str(turn_start.get("code"))
    )
    assert body.get("turnId") in (None, ""), "失败时不得返回 turnId"
    assert turns.calls == [], "缺少原提交话题时不得启动 Turn"
