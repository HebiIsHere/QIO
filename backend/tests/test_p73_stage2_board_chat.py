# -*- coding: utf-8 -*-
"""P73 Stage 2 定向测试：普通对话接入板面（board 模式）/ 免提交提案 / 自动续作。

覆盖：
1. resolve_chat_binding 的 board 模式（默认板面、显式板面、拒绝 submissionId 与未知板面）；
2. 没有提交身份也能提案，并派生 preview；
3. board 模式运行上下文里 board_propose_intents 可以不传 submission_id；
4. 无操作计划意图的 apply 返回 intent + submission_id（API 据此自动续作）；
5. confirm API 在无操作计划时自动起续作轮（stub TurnManager 校验 reserve/activate 与绑定字段）；
6. update_plan 支持 Stage 2 的成组 / 连线 / 正文编辑（后端能力，防回归）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.api import interactive_intents as api_module
from agent.interactive import board_store, intent_service, models, runtime, submission, tool_models

from test_p3_b_support import (
    FakeApp,
    add_submission,
    board_tools,
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
    board_store.ensure_board(db_conn, board_id=models.DEFAULT_BOARD_ID)
    return db_conn


def _ops() -> list[dict]:
    return [{"op": "add_reply_card", "content": "测试卡", "ref": "c1", "x": 10.0, "y": 10.0}]


def _spec(client_ref: str = "a", ops: list[dict] | None = None) -> dict:
    spec = intent_spec(client_ref, title="落一张卡", goal="把一张卡落到板面", expected="出现一张卡")
    if ops is not None:
        spec["operations"] = list(ops)
    return spec


def test_resolve_board_mode_defaults_to_default_board(env):
    binding = submission.resolve_chat_binding(env, mode="board", current_topic="topic_x")
    assert binding["mode"] == submission.MODE_BOARD
    assert binding["boardId"] == models.DEFAULT_BOARD_ID
    assert binding["submissionId"] == ""
    assert binding["topicId"] == "topic_x"
    assert binding["resume"] is False
    # 显式指定板面
    explicit = submission.resolve_chat_binding(env, mode="board", board_id=BOARD)
    assert explicit["boardId"] == BOARD


def test_resolve_board_mode_rejects_bad_identity(env):
    with pytest.raises(ValueError):
        submission.resolve_chat_binding(
            env, mode="board", board_id=BOARD, submission_id=SUBMISSION
        )
    with pytest.raises(ValueError):
        submission.resolve_chat_binding(env, mode="board", board_id="board_missing")
    with pytest.raises(ValueError):
        submission.resolve_chat_binding(env, mode="unknown", board_id=BOARD)
    # 普通模式行为不变：不能携带 boardId
    with pytest.raises(ValueError):
        submission.resolve_chat_binding(env, mode="normal", board_id=BOARD)


def test_propose_without_submission_creates_preview(env):
    proposed = intent_service.propose_intents(
        env, board_id=BOARD, submission_id="", intents=[_spec(ops=_ops())]
    )
    assert proposed["ok"] is True, proposed
    view = proposed["intents"][0]
    assert view["status"] == intent_service.STATUS_AWAITING
    assert view["submission_id"] in ("", None)
    assert len(view["preview"]["cards"]) == 1
    row = env.execute(
        "SELECT submission_id FROM board_intents WHERE id = ?", (view["intent_id"],)
    ).fetchone()
    assert str(row["submission_id"]) == ""


async def test_board_mode_tool_accepts_missing_submission(env):
    tools = board_tools(FakeApp(env))
    context = runtime.BoardTurnContext(
        turn_id="turn_board_chat",
        mode=runtime.MODE_BOARD,
        board_id=BOARD,
        submission_id="",
        topic_id="topic_x",
    )
    token = runtime.set_board_turn(context)
    try:
        result = await tools["board_propose_intents"].run(intents=[_spec(ops=_ops())])
    finally:
        runtime.reset_board_turn(token)
    assert result is not None
    count = env.execute(
        "SELECT COUNT(*) AS n FROM board_intents WHERE board_id = ? AND submission_id = ''",
        (BOARD,),
    ).fetchone()
    assert int(count["n"]) == 1


def test_board_chat_tool_set_is_read_only_plus_propose():
    assert tool_models.BOARD_CHAT_TOOLS == tool_models.READ_ONLY_TOOLS + (
        "board_propose_intents",
    )
    assert runtime.BoardTurnContext(
        turn_id="t", mode=runtime.MODE_BOARD, board_id=BOARD
    ).interactive


def test_apply_without_operations_returns_intent_for_continuation(env):
    proposed = intent_service.propose_intents(
        env, board_id=BOARD, submission_id=SUBMISSION, intents=[_spec()]
    )
    assert proposed["ok"] is True
    intent_id = proposed["intents"][0]["intent_id"]
    intent_service.confirm_intents(env, board_id=BOARD, intent_ids=[intent_id])
    outcome = intent_service.apply_approved_intent(env, board_id=BOARD, intent_id=intent_id)
    assert outcome["ok"] is False
    assert outcome["reason"] == "no_operations"
    assert outcome["submission_id"] == SUBMISSION
    assert outcome["intent"]["intent_id"] == intent_id
    assert outcome["intent"]["status"] == intent_service.STATUS_APPROVED


def test_update_plan_supports_group_link_and_content_edit(env):
    proposed = intent_service.propose_intents(
        env,
        board_id=BOARD,
        submission_id="",
        intents=[
            _spec(
                ops=[
                    {"op": "add_reply_card", "content": "第一张", "ref": "c1", "x": 10.0, "y": 10.0},
                    {"op": "add_reply_card", "content": "第二张", "ref": "c2", "x": 420.0, "y": 10.0},
                ]
            )
        ],
    )
    assert proposed["ok"] is True, proposed
    intent_id = proposed["intents"][0]["intent_id"]
    edited_ops = [
        {"op": "add_reply_card", "content": "第一张（改）", "ref": "c1", "x": 10.0, "y": 10.0},
        {"op": "add_reply_card", "content": "第二张", "ref": "c2", "x": 420.0, "y": 10.0},
        {"op": "group_cards", "cardIds": ["c1", "c2"], "ref": "g1", "name": "测试组"},
        {"op": "add_link", "src": "c1", "dst": "c2", "meaning": "递进"},
    ]
    updated = intent_service.update_plan(
        env, board_id=BOARD, intent_id=intent_id, operations=edited_ops
    )
    assert updated["ok"] is True, updated
    preview = updated["intent"]["preview"]
    contents = [card["content"] for card in preview["cards"]]
    assert "第一张（改）" in contents
    assert len(preview["groups"]) == 1
    assert len(preview["links"]) == 1


class _FakeService:
    def confirm_intents(self, conn, *, board_id, intent_ids):
        return {
            "ok": True,
            "results": [
                {"intent_id": str(i), "ok": True, "status": "approved"} for i in intent_ids
            ],
            "confirmed": [str(i) for i in intent_ids],
            "conflicts": [],
            "gate": "program",
        }

    def apply_approved_intent(self, conn, *, board_id, intent_id, instance_id=None):
        return {
            "ok": False,
            "reason": "no_operations",
            "detail": "这条意图没有板面操作计划，走既有的续作执行。",
            "status": "approved",
            "intent": {"intent_id": str(intent_id), "title": "整理材料", "submission_id": ""},
            "submission_id": "",
        }


class _FakeTurns:
    def __init__(self) -> None:
        self.reserved: list[tuple[str, object]] = []
        self.activated: list[object] = []

    def reserve(self, message, topic_id=None, **kwargs):
        turn = SimpleNamespace(
            turn_id="turn_auto_1",
            mode="normal",
            board_id="",
            board_submission_id="",
            board_topic_id="",
            board_resume=False,
            board_intent_ids=(),
            board_resolved_question_ids=(),
            board_attempt=1,
        )
        self.reserved.append((str(message), turn))
        return turn

    def activate(self, turn) -> None:
        self.activated.append(turn)


def _api_client(conn):
    turns = _FakeTurns()
    ctx = SimpleNamespace(conn=conn, turns=turns, current_topic=lambda: "topic_now")
    app = FastAPI()
    app.include_router(api_module.router)
    app.state.ctx = ctx
    app.state.instance_id = "inst_test"
    app.state.p3_intent_service = _FakeService()
    return TestClient(app), turns


def test_confirm_api_enqueues_auto_continuation(env):
    client, turns = _api_client(env)
    response = client.post(
        "/api/interactive/intents/i_chat/confirm", json={"boardId": BOARD}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    continuation = body.get("continuation") or {}
    assert continuation.get("enqueued") is True
    assert continuation.get("turn_id") == "turn_auto_1"
    assert turns.activated, "续作轮没有被放行"
    turn = turns.activated[0]
    assert turn.mode == "interactive"
    assert turn.board_id == BOARD
    assert turn.board_intent_ids == ("i_chat",)
    assert turn.board_submission_id == ""
    message = turns.reserved[0][0]
    assert "i_chat" in message
    assert "board_finish_intent" in message
