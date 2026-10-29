# -*- coding: utf-8 -*-
"""R5 定向：互动「提交轮」程序消息的 content_type（内部绑定文字不作为普通用户消息）。

修复 B 的写侧断言：
* 提交轮 user 消息落 content_type="board_submission" + raw 程序事实；
* content 原样保留（模型 / 工具所需的 board_id、submission_id、「【互动板面提交】」仍在）；
* 普通对话消息仍是 content_type="text"（既有行为不变）；
* 读取通道原样返回 board_submission（本轮不新增展示对象，由前端决定不渲染）。

不联网、不调用真实模型；运行：
cd backend; ./.venv/Scripts/python.exe -m pytest tests/test_release_step5_submission_message.py -q
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from agent.interactive import submission

from test_release_step2_d import _RecordingAdapter, _make_app, _wait_turn_end

BOARD = "board_r5_sub"
CARD = "c_r5_sub"


def _seed_board(client) -> dict:
    created = client.post(
        "/api/interactive/boards", json={"boardId": BOARD, "title": "R5 提交轮"}
    )
    assert created.status_code == 200, created.text[:400]
    card = {
        "id": CARD,
        "kind": "text",
        "content": "提交轮内部文字验证用的材料。",
        "meta": {"title": "材料"},
        "x": 120,
        "y": 120,
        "w": 300,
        "h": 160,
        "checked": False,
        "hidden": False,
        "folded": False,
        "bookmarked": False,
        "deleted": False,
        "participation": "included",
        "createdAt": "2026-10-29T00:00:00+00:00",
        "updatedAt": "2026-10-29T00:00:00+00:00",
    }
    state = {
        "boardId": BOARD,
        "seq": 0,
        "updatedAt": "2026-10-29T00:00:00+00:00",
        "cards": [card],
        "groups": [],
        "links": [],
        "selection": [CARD],
        "participationVersion": 1,
    }
    saved = client.put(
        f"/api/interactive/boards/{BOARD}/state", json={"state": state, "reason": "seed"}
    )
    assert saved.status_code == 200, saved.text[:400]
    return saved.json()


def _fake_app(db_conn, settings):
    app = _make_app(db_conn, settings)
    app.state.ctx.build_adapter = AsyncMock(return_value=_RecordingAdapter())
    return app


def _user_row(conn, turn_id: str):
    return conn.execute(
        "SELECT id, content, content_type, raw FROM messages"
        " WHERE turn_id = ? AND role = 'user'",
        (turn_id,),
    ).fetchone()


def test_submission_turn_user_message_is_board_submission(db_conn, settings):
    app = _fake_app(db_conn, settings)
    with TestClient(app) as client:
        _seed_board(client)
        response = client.post(f"/api/interactive/boards/{BOARD}/submissions", json={})
        assert response.status_code == 200, response.text[:400]
        body = response.json()
        assert body["status"] == "succeeded", body
        turn_id = str(body.get("turnId") or "")
        submission_id = str(body["submission"]["id"])
        assert turn_id

        _wait_turn_end(app, turn_id)

        row = _user_row(db_conn, turn_id)
        assert row is not None, "提交轮必须落一条 user 消息"
        assert row["content_type"] == submission.SUBMISSION_CONTENT_TYPE == "board_submission"
        # content 原样保留：模型上下文与 board_propose_intents 需要的 id 一个字不改。
        assert "【互动板面提交】" in row["content"]
        assert submission_id in row["content"]
        assert BOARD in row["content"]
        facts = json.loads(row["raw"])["board_submission"]
        assert facts == {"board_id": BOARD, "submission_id": submission_id}

        topic = db_conn.execute(
            "SELECT topic_id FROM fragments WHERE id ="
            " (SELECT fragment_id FROM messages WHERE id = ?)",
            (row["id"],),
        ).fetchone()["topic_id"]
        listed = client.get("/api/session/messages", params={"topic_id": topic})
        assert listed.status_code == 200, listed.text[:400]
        message = next(
            item for item in listed.json()["messages"] if item["id"] == row["id"]
        )
        # 读取侧原样返回类型（本轮不新增展示对象；前端据此不渲染普通用户气泡）。
        assert message["content_type"] == "board_submission"
        assert "board_submission" not in message or not isinstance(
            message.get("board_submission"), dict
        )


def test_normal_chat_message_still_text(db_conn, settings):
    app = _fake_app(db_conn, settings)
    with TestClient(app) as client:
        started = client.post("/api/turns", json={"message": "这是一条普通对话消息"})
        assert started.status_code == 200, started.text[:400]
        turn_id = str(started.json()["turn_id"])
        _wait_turn_end(app, turn_id)
        row = _user_row(db_conn, turn_id)
        assert row is not None
        assert row["content_type"] == "text"
        assert row["content"] == "这是一条普通对话消息"
        assert row["raw"] in (None, "", "{}")
