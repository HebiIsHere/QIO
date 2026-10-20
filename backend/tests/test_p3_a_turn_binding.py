# -*- coding: utf-8 -*-
"""P3-A / 聊天请求绑定：mode / boardId / submissionId → TurnContext。

验收点：
* 普通模式不带板面字段 → 不绑定、不暴露板面上下文；
* 互动模式校验板面存在、提交属于该板面、模式合法；伪造 / 越界 boardId 拒绝；
* 有等待中的提交时，用户下一条聊天消息**自动续作**原提交：board_resume=True、
  原话题、resolved_question_ids 从 waiting 恢复、board_attempt 递增；
* 响应只增字段（mode / board_id / submission_id / resume），既有字段不变。

模型一律 fake（TurnManager 的 runner 被替换成同步完成的替身），不联网。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.credentials.store import MemoryKeyring
from agent.interactive import submission
from agent.storage import migration_p3_a

from test_p3_a_support import BOARD, add_submission, make_board


def _state() -> dict:
    return {
        "boardId": BOARD,
        "seq": 0,
        "cards": [],
        "groups": [],
        "links": [],
        "selection": [],
    }


async def _fake_runner(turn_ctx) -> None:  # noqa: ANN001 - 模型替身：不调真实模型
    turn_ctx.result = {"ok": True}
    turn_ctx.final_content = "fake answer"


def _client(app_context) -> TestClient:  # noqa: ANN001
    return TestClient(app_context)


def _prepare(app, db_conn) -> None:  # noqa: ANN001
    app.state.ctx.credentials._kr = MemoryKeyring()
    app.state.ctx.turns.set_runner(_fake_runner)


def _turn(client: TestClient, app, **body) -> dict:  # noqa: ANN001
    response = client.post("/api/turns", json=body)
    return response


def _ctx_by_request(app, request_id: str):  # noqa: ANN001
    return app.state.ctx.turns.lookup_request(request_id)


def test_normal_chat_has_no_board_context(db_conn, settings, tmp_path):
    migration_p3_a.apply(db_conn)
    app = create_app(settings, db_conn)
    _prepare(app, db_conn)
    with TestClient(app) as client:
        response = _turn(client, app, message="普通聊天", client_request_id="req-normal-1")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True and body["accepted"] is True
        turn = _ctx_by_request(app, "req-normal-1")
        assert turn is not None
        assert turn.mode == "normal"
        assert turn.board_id == "" and turn.board_submission_id == ""
        assert "mode" not in body


def test_normal_mode_with_board_identity_is_rejected(db_conn, settings):
    migration_p3_a.apply(db_conn)
    app = create_app(settings, db_conn)
    _prepare(app, db_conn)
    with TestClient(app) as client:
        response = _turn(
            client, app, message="x", mode="normal", boardId=BOARD
        )
        assert response.status_code == 400
        assert "普通模式" in response.json()["detail"]


def test_unknown_board_and_bogus_mode_are_rejected(db_conn, settings):
    migration_p3_a.apply(db_conn)
    app = create_app(settings, db_conn)
    _prepare(app, db_conn)
    with TestClient(app) as client:
        unknown = _turn(client, app, message="x", mode="interactive", boardId="board_ghost")
        assert unknown.status_code == 400
        assert "没有这个板面" in unknown.json()["detail"]
        bad_mode = _turn(client, app, message="x", mode="interactivee", boardId=BOARD)
        assert bad_mode.status_code == 400
        assert "mode" in bad_mode.json()["detail"]


def test_interactive_board_without_submission_binds_board_only(db_conn, settings):
    make_board(db_conn)
    migration_p3_a.apply(db_conn)
    app = create_app(settings, db_conn)
    _prepare(app, db_conn)
    with TestClient(app) as client:
        response = _turn(
            client,
            app,
            message="看看这个板面",
            mode="interactive",
            boardId=BOARD,
            client_request_id="req-interactive-1",
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["mode"] == "interactive"
        assert body["board_id"] == BOARD
        assert body["submission_id"] == ""
        assert body["resume"] is False
        turn = _ctx_by_request(app, "req-interactive-1")
        assert turn is not None
        assert turn.mode == "interactive"
        assert turn.board_id == BOARD
        assert turn.board_submission_id == ""
        assert turn.board_resume is False


def test_next_message_auto_resumes_waiting_submission(db_conn, settings):
    make_board(db_conn)
    migration_p3_a.apply(db_conn)
    sid = add_submission(db_conn)
    submission.bind_submission_turn(
        db_conn,
        submission_id=sid,
        turn_id="turn_submission_first",
        resume_state={"topicId": "topic-original", "kind": "submission"},
    )
    waiting = submission.record_waiting(
        db_conn,
        board_id=BOARD,
        submission_id=sid,
        question="你想先整理哪几张卡片？",
        missing_fields=["card_ids"],
    )
    app = create_app(settings, db_conn)
    _prepare(app, db_conn)
    with TestClient(app) as client:
        response = _turn(
            client,
            app,
            message="前两张",
            mode="interactive",
            boardId=BOARD,
            client_request_id="req-resume-1",
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["submission_id"] == sid
        assert body["resume"] is True
        assert body["attempt"] == 2
        turn = _ctx_by_request(app, "req-resume-1")
        assert turn is not None
        assert turn.board_submission_id == sid
        assert turn.board_resume is True
        assert turn.board_topic_id == "topic-original"
        assert turn.board_resolved_question_ids == (waiting["id"],)
        assert turn.board_attempt == 2
        # 续作绑定写进历史；等待本身**不会**因普通聊天消息被清除
        binding = submission.submission_binding(db_conn, sid)
        assert binding is not None
        assert binding["turnId"] == turn.turn_id
        assert len(binding["turns"]) == 2
        assert submission.get_waiting(db_conn, BOARD)["count"] == 1


def test_explicit_submission_must_belong_to_board(db_conn, settings):
    make_board(db_conn)
    make_board(db_conn, "board_other")
    migration_p3_a.apply(db_conn)
    foreign = add_submission(db_conn, "board_other")
    app = create_app(settings, db_conn)
    _prepare(app, db_conn)
    with TestClient(app) as client:
        response = _turn(
            client,
            app,
            message="x",
            mode="interactive",
            boardId=BOARD,
            submissionId=foreign,
        )
        assert response.status_code == 400
        assert "不属于板面" in response.json()["detail"]
        missing = _turn(
            client,
            app,
            message="x",
            mode="interactive",
            boardId=BOARD,
            submissionId="sub_missing",
        )
        assert missing.status_code == 400
        assert "没有这条提交" in missing.json()["detail"]
