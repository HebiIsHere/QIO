# -*- coding: utf-8 -*-
"""R2 契约：互动意图执行记录（写入程序事实 + 读取派生展示对象）。

契约：D:/qio-dev/r2-contract-intent-execution.md（Lead 固定）
      D:/qio-dev/r2-brief-d-backend.md

基线行为（实现前，本案先红）：
* 互动续作 Turn 的 user 消息落库 content_type='text'，内容就是
  interactive_turn_message() 拼的内部工作文字（含板面 / 提交 / 意图编号）；
* GET /api/session/messages、/api/fragments/{id}/messages 只回 id/role/content/
  content_type/created_at/raw/turn_id，没有任何结构化 intent_execution 对象，
  前端只能把它当普通用户消息原样显示。

本案钉住：
1. 写入：content_type='intent_execution' + raw={"intent_execution": 程序事实}，
   content 原样保留（模型侧上下文不变）；普通用户消息仍是 'text'；
2. 读取：服务端派生展示就绪对象（title/status/status_label/scope/intents/
   attempts/result_entry/authorization_versions/diagnostics/verified/record_head）；
3. record_key 只按 submission + 排序后的意图集；同 record 只有最早一条 record_head；
4. 7 档状态全部只依据真实记录；旧消息原样返回。

全部用 fake provider / 直接落库夹具：不联网、不用真实 Key。
运行：cd backend; & ./.venv/Scripts/python.exe -m pytest tests/test_release_step2_d.py -q
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.credentials.store import MemoryKeyring
from agent.interactive import intent_service, models, submission

from test_p3_b_support import add_submission, intent_scope, intent_spec, make_board

BOARD = "board_r2_d"
SUBMISSION = "sub_r2_d"
RETRY_LIMIT = 3

# 契约 §三：7 档状态与中文标签（服务端给，前端不再自己算）
STATUS_LABELS = {
    "confirmed": "已确认，等待处理",
    "queued": "等待处理",
    "running": "正在处理",
    "partial": "部分完成",
    "failed": "处理失败",
    "completed": "已完成",
    "unknown": "状态无法核对",
}


# ---------------------------------------------------------------------------
# 脚手架
# ---------------------------------------------------------------------------


def _records():
    """被测模块：互动意图执行记录（写入事实 / 读取派生）。"""
    from agent.interactive import records

    return records


def _make_app(db_conn, settings):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    return app


class _RecordingAdapter:
    """脚本化 fake provider：记录收到的模型消息，返回一段普通正文（不联网）。"""

    mode = "native"
    model = "fake-r2-d"

    def __init__(self, text: str = "（测试用回答）") -> None:
        self.text = text
        self.calls: list[list] = []

    async def complete(self, messages, tools, *, temperature=None, max_tokens=None):
        from agent.adapters.base import ChatMessage, Completion

        self.calls.append(list(messages))
        return Completion(message=ChatMessage(role="assistant", content=self.text))


def _topic(ctx, title: str = "R2 记录话题") -> tuple[str, str]:
    topic = ctx.topics.nodes.create_topic(title).id
    fragment = ctx.fragments.get_or_create_open(topic).id
    ctx.navigation.enter_topic(topic, fragment_id=fragment)
    return topic, fragment


def _wait_turn_end(app, turn_id: str, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in list(app.state.ctx.bus._history):
            if event.type.value == "TURN_END" and str(event.data.get("turn_id")) == turn_id:
                return event.data
        time.sleep(0.02)
    raise AssertionError(f"turn {turn_id} 没有在 {timeout}s 内结束")


def _seed_approved_intents(
    conn, *, titles: tuple[str, ...] = ("整理这组资料",)
) -> list[str]:
    """真实入口建意图：propose + confirm（真实 authorization / version）。"""
    make_board(conn, board_id=BOARD)
    add_submission(conn, board_id=BOARD, submission_id=SUBMISSION)
    proposed = intent_service.propose_intents(
        conn,
        board_id=BOARD,
        submission_id=SUBMISSION,
        intents=[
            intent_spec(
                f"r{index}",
                title=title,
                scope=intent_scope(allowed_actions=["create_card"]),
            )
            for index, title in enumerate(titles)
        ],
    )
    assert proposed.get("ok") is True, proposed
    ids = [str(item["intent_id"]) for item in proposed["intents"]]
    confirmed = intent_service.confirm_intents(conn, board_id=BOARD, intent_ids=ids)
    assert confirmed.get("ok") is True, confirmed
    return ids


def _set_intent(
    conn,
    intent_id: str,
    *,
    status: str | None = None,
    reason: str | None = None,
    applied: dict | None = None,
    outcome: str | None = None,
) -> None:
    fields: list[str] = []
    values: list = []
    if status is not None:
        fields.append("status = ?")
        values.append(status)
    if reason is not None:
        fields.append("reason = ?")
        values.append(reason)
    if applied is not None:
        fields.append("applied = ?")
        values.append(json.dumps(applied, ensure_ascii=False))
    if outcome is not None:
        fields.append("outcome = ?")
        values.append(outcome)
    if not fields:
        return
    fields.append("updated_at = ?")
    values.append(models.now_iso())
    values.append(intent_id)
    conn.execute("UPDATE board_intents SET " + ", ".join(fields) + " WHERE id = ?", values)


def _claim(
    conn,
    intent_id: str,
    *,
    status: str,
    turn_id: str = "",
    reason: str = "",
    attempt: int = 1,
    source: str = "confirm",
) -> None:
    stamp = models.now_iso()
    conn.execute(
        "INSERT OR REPLACE INTO board_intent_turns"
        " (intent_id, board_id, submission_id, turn_id, attempt, source, status, reason,"
        "  created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (intent_id, BOARD, SUBMISSION, turn_id, attempt, source, status, reason, stamp, stamp),
    )


def _retry(
    conn,
    intent_id: str,
    *,
    status: str = "pending",
    attempts: int = 0,
    manual_attempts: int = 0,
    last_error: str = "",
    retryable: int = 1,
    next_action: str = "auto",
) -> str:
    series_id = "series_" + intent_id
    stamp = models.now_iso()
    conn.execute(
        "INSERT OR REPLACE INTO board_intent_retries"
        " (id, series_id, board_id, submission_id, intent_id, authorization_version,"
        "  attempts, manual_attempts, retry_limit, status, last_error, next_action,"
        "  retryable, turn_id, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)",
        (
            series_id,
            series_id,
            BOARD,
            SUBMISSION,
            intent_id,
            attempts,
            manual_attempts,
            RETRY_LIMIT,
            status,
            last_error,
            next_action,
            retryable,
            stamp,
            stamp,
        ),
    )
    return series_id


def _journal(conn, turn_id: str, status: str, reason: str = "") -> None:
    stamp = models.now_iso()
    conn.execute(
        "INSERT OR REPLACE INTO turn_journal"
        " (turn_id, message, topic_id, notify, status, created_at, updated_at, reason)"
        " VALUES (?, '', NULL, 0, ?, ?, ?, ?)",
        (turn_id, status, stamp, stamp, reason),
    )


def _append_record_message(
    ctx,
    topic: str,
    *,
    intent_ids,
    turn_id: str,
    attempt: int = 1,
    source: str = "confirm",
    record_key: str | None = None,
    submission_id: str = SUBMISSION,
    board_id: str = BOARD,
    created_at: str | None = None,
    retry_attempt: int | None = None,
    retry_limit: int | None = None,
    versions: dict | None = None,
) -> str:
    """落一条互动续作的 user 消息（写侧事实；读取派生只认这些真实字段）。"""
    records = _records()
    key = record_key if record_key is not None else records.build_record_key(
        submission_id, intent_ids
    )
    facts = {
        "board_id": board_id,
        "submission_id": submission_id,
        "topic_id": topic,
        "intent_ids": [str(item) for item in intent_ids],
        "source": source,
        "turn_id": turn_id,
        "attempt": attempt,
        "retry_attempt": retry_attempt,
        "retry_limit": retry_limit,
        "authorization_versions": versions or {str(item): 1 for item in intent_ids},
        "record_key": key,
    }
    message_id, _fragment = ctx.memory.append_message(
        topic_id=topic,
        role="user",
        content="【互动意图续作】这是程序绑定的一次真实续作，不是新的用户提交。",
        content_type=records.CONTENT_TYPE,
        raw={records.RAW_KEY: facts},
        turn_id=turn_id,
    )
    if created_at is not None:
        ctx.conn.execute(
            "UPDATE messages SET created_at = ? WHERE id = ?", (created_at, message_id)
        )
    return message_id


def _get_messages(client, topic_id: str, **params) -> list[dict]:
    response = client.get("/api/session/messages", params={"topic_id": topic_id, **params})
    assert response.status_code == 200, response.text[:400]
    return response.json()["messages"]


def _record_of(messages: list[dict], message_id: str) -> dict:
    message = next(item for item in messages if item["id"] == message_id)
    assert "intent_execution" in message, f"缺少结构化字段：{message}"
    return message["intent_execution"]


# ---------------------------------------------------------------------------
# 写侧：程序事实只来自真实绑定
# ---------------------------------------------------------------------------


def _fake_turn_submitter():
    calls: list[dict] = []

    def submit(message, topic_id=None):
        turn = {
            "turn_id": f"turn_r2_{len(calls) + 1}",
            "message": message,
            "topic_id": topic_id,
        }
        calls.append({"message": message, "topic": topic_id, "turn": turn})
        return turn

    return submit, calls


def test_start_interactive_turn_binds_real_execution_facts(db_conn):
    """start_interactive_turn 把真实程序事实写到 TurnContext（不碰 message 文本）。"""
    records = _records()
    ids = _seed_approved_intents(db_conn, titles=("整理这组资料",))
    db_conn.execute(
        "UPDATE board_submissions SET resume_state = ? WHERE id = ?",
        (json.dumps({"kind": "submission", "topicId": "topic-r2"}), SUBMISSION),
    )
    submit, calls = _fake_turn_submitter()

    started = submission.start_interactive_turn(
        db_conn,
        turn_submitter=submit,
        board_id=BOARD,
        intent_ids=ids,
        submission_id=SUBMISSION,
        source="confirm",
    )
    assert started["ok"] is True, started
    assert started["turnId"] == "turn_r2_1"
    assert len(calls) == 1

    expected_message = submission.interactive_turn_message(
        board_id=BOARD,
        submission_id=SUBMISSION,
        intent_ids=ids,
        attempt=1,
        source="confirm",
        authorization_versions={ids[0]: 1},
    )
    # content 原样保留：模型上下文 / 授权版本 / 恢复信息一个字都不动
    assert calls[0]["message"] == expected_message == started["message"]

    facts = calls[0]["turn"].get("board_intent_execution")
    assert isinstance(facts, dict), "续作 Turn 必须绑定可核对的执行记录事实"
    assert facts["record_key"] == records.build_record_key(SUBMISSION, ids)
    assert facts["record_key"] == "ie:" + SUBMISSION + ":" + ids[0]
    assert facts["board_id"] == BOARD
    assert facts["submission_id"] == SUBMISSION
    assert facts["topic_id"] == "topic-r2"
    assert facts["intent_ids"] == ids
    assert facts["source"] == "confirm"
    assert facts["turn_id"] == "turn_r2_1"
    assert facts["attempt"] == 1
    assert facts["retry_attempt"] is None
    assert facts["authorization_versions"] == {ids[0]: 1}


def test_batch_confirm_binds_all_confirmed_intents_in_real_order(db_conn):
    """批量确认：事实里的意图 id 就是本次真实认领的顺序，record_key 排序稳定。"""
    records = _records()
    ids = _seed_approved_intents(db_conn, titles=("整理资料", "导出表格"))
    db_conn.execute(
        "UPDATE board_submissions SET resume_state = ? WHERE id = ?",
        (json.dumps({"kind": "submission", "topicId": "topic-r2"}), SUBMISSION),
    )
    submit, calls = _fake_turn_submitter()
    started = submission.start_interactive_turn(
        db_conn,
        turn_submitter=submit,
        board_id=BOARD,
        intent_ids=ids,
        submission_id=SUBMISSION,
        source="batch_confirm",
    )
    assert started["ok"] is True, started
    facts = calls[0]["turn"]["board_intent_execution"]
    assert facts["intent_ids"] == ids
    assert facts["source"] == "batch_confirm"
    assert facts["authorization_versions"] == {ids[0]: 1, ids[1]: 1}
    assert facts["record_key"] == "ie:" + SUBMISSION + ":" + ",".join(sorted(ids))


# ---------------------------------------------------------------------------
# 写侧端到端：一次确认真的跑一轮以后，messages 里的 user 行是什么
# ---------------------------------------------------------------------------


def test_confirmed_interactive_turn_message_is_recorded_as_intent_execution(
    db_conn, settings
):
    """一次单条确认跑完：user 行 content_type=intent_execution + 真实 raw 事实；
    读取接口附带展示就绪对象；模型侧仍拿到完整的内部指令原文。"""
    records = _records()
    app = _make_app(db_conn, settings)
    adapter = _RecordingAdapter()
    app.state.ctx.build_adapter = AsyncMock(return_value=adapter)
    with TestClient(app) as client:
        ctx = app.state.ctx
        ids = _seed_approved_intents(db_conn, titles=("整理这组资料",))
        topic, _fragment = _topic(ctx)
        db_conn.execute(
            "UPDATE board_submissions SET resume_state = ? WHERE id = ?",
            (json.dumps({"kind": "submission", "topicId": topic}), SUBMISSION),
        )
        response = client.post(
            f"/api/interactive/intents/{ids[0]}/confirm", json={"boardId": BOARD}
        )
        assert response.status_code == 200, response.text[:400]
        body = response.json()
        assert body.get("started") is True, body
        turn_id = str(body["turnId"])
        _wait_turn_end(app, turn_id)

        row = db_conn.execute(
            "SELECT id, content, content_type, raw FROM messages"
            " WHERE turn_id = ? AND role = 'user'",
            (turn_id,),
        ).fetchone()
        assert row is not None, "续作 Turn 必须落一条 user 消息"
        assert row["content_type"] == "intent_execution"
        assert row["content"].startswith("【互动意图续作】")
        raw = json.loads(row["raw"])
        facts = raw["intent_execution"]
        assert facts["record_key"] == "ie:" + SUBMISSION + ":" + ids[0]
        assert facts["submission_id"] == SUBMISSION
        assert facts["intent_ids"] == ids
        assert facts["source"] == "confirm"
        assert facts["turn_id"] == turn_id
        assert facts["authorization_versions"] == {ids[0]: 1}

        # 模型侧上下文没有缩水：这一轮真的把内部指令原文发给了模型
        sent = [
            str(getattr(message, "content", "") or "")
            for call in adapter.calls
            for message in call
        ]
        assert any("【互动意图续作】" in text for text in sent), "模型侧上下文不得丢失续作指令"

        # 读取通道：服务端派生展示对象
        messages = _get_messages(client, topic)
        record = _record_of(messages, row["id"])
        assert record["record_head"] is True
        assert record["title"] == "整理这组资料"
        assert record["status"] in STATUS_LABELS
        assert record["status_label"] == STATUS_LABELS[record["status"]]
        assert record["diagnostics"]["turn_ids"] == [turn_id]
        assert record["verified"] is True

        # 刷新 / 重新进入历史：同一条记录、同一真实状态
        again = _record_of(_get_messages(client, topic), row["id"])
        assert again["status"] == record["status"]


def _bind_submission_topic(conn, topic: str) -> None:
    conn.execute(
        "UPDATE board_submissions SET resume_state = ? WHERE id = ?",
        (json.dumps({"kind": "submission", "topicId": topic}), SUBMISSION),
    )


def test_manual_retry_adds_attempt_to_same_record(db_conn, settings):
    """真实手动重试：同一 record_key 增加 attempt（第二段不冒充 head），不新增独立记录。"""
    app = _make_app(db_conn, settings)
    adapter = _RecordingAdapter()
    app.state.ctx.build_adapter = AsyncMock(return_value=adapter)
    with TestClient(app) as client:
        ctx = app.state.ctx
        ids = _seed_approved_intents(db_conn, titles=("整理这组资料",))
        topic, _fragment = _topic(ctx)
        _bind_submission_topic(db_conn, topic)

        confirmed = client.post(
            f"/api/interactive/intents/{ids[0]}/confirm", json={"boardId": BOARD}
        ).json()
        assert confirmed.get("started") is True, confirmed
        _wait_turn_end(app, str(confirmed["turnId"]))

        retried = client.post(
            f"/api/interactive/intents/{ids[0]}/retry", json={"boardId": BOARD}
        )
        assert retried.status_code == 200, retried.text[:400]
        retry_body = retried.json()
        assert retry_body.get("ok") is True, retry_body
        _wait_turn_end(app, str(retry_body["turnId"]))

        records = [
            message
            for message in _get_messages(client, topic)
            if message.get("content_type") == "intent_execution"
        ]
        assert len(records) == 2, records
        keys = {message["intent_execution"]["record_key"] for message in records}
        assert keys == {"ie:" + SUBMISSION + ":" + ids[0]}
        heads = [message["intent_execution"]["record_head"] for message in records]
        assert heads == [True, False], heads
        latest = records[-1]["intent_execution"]
        assert [item["source"] for item in latest["attempts"]] == ["confirm", "retry"]
        assert len(latest["attempts"]) == 2
        assert latest["intents"][0]["attempts"] == 2
        assert latest["diagnostics"]["turn_ids"] == [
            str(confirmed["turnId"]),
            str(retry_body["turnId"]),
        ]


def test_record_without_metadata_is_unknown_unverified(record_env):
    """声称是记录但缺程序事实（老坏行）：unknown + verified=false + 一句实话，不猜。"""
    client, ctx = record_env
    topic, _fragment = _topic(ctx)
    message_id, _f = ctx.memory.append_message(
        topic_id=topic,
        role="user",
        content="一条没有元数据的记录消息",
        content_type="intent_execution",
        raw={},
    )
    record = _record_of(_get_messages(client, topic), message_id)
    assert record["status"] == "unknown"
    assert record["status_label"] == STATUS_LABELS["unknown"]
    assert record["verified"] is False
    assert record["verified_note"]
    assert record["record_head"] is True


def test_plain_user_message_stays_text_and_has_no_record(db_conn, settings):
    """普通用户消息不被误处理：content_type 仍是 text，读取时没有结构化记录。"""
    app = _make_app(db_conn, settings)
    adapter = _RecordingAdapter()
    app.state.ctx.build_adapter = AsyncMock(return_value=adapter)
    with TestClient(app) as client:
        ctx = app.state.ctx
        topic, _fragment = _topic(ctx)
        response = client.post("/api/turns", json={"message": "一段普通的用户消息"})
        assert response.status_code == 200, response.text[:400]
        turn_id = str(response.json()["turn_id"])
        _wait_turn_end(app, turn_id)
        row = db_conn.execute(
            "SELECT content, content_type, raw FROM messages"
            " WHERE turn_id = ? AND role = 'user'",
            (turn_id,),
        ).fetchone()
        assert row is not None
        assert row["content"] == "一段普通的用户消息"
        assert row["content_type"] == "text"
        assert "intent_execution" not in json.loads(row["raw"] or "{}")
        messages = _get_messages(client, topic)
        message = next(item for item in messages if item["role"] == "user")
        assert "intent_execution" not in message


# ---------------------------------------------------------------------------
# 读侧：单条记录 / 批量 / 合并 / 旧记录
# ---------------------------------------------------------------------------


@pytest.fixture()
def record_env(db_conn, settings):
    app = _make_app(db_conn, settings)
    with TestClient(app) as client:
        yield client, app.state.ctx


def test_single_completed_record_shape(record_env):
    """单条确认 + 真实完成：title 复用意图名，状态 completed，结果入口来自真实 applied。"""
    client, ctx = record_env
    ids = _seed_approved_intents(ctx.conn, titles=("整理这组资料",))
    intent_id = ids[0]
    _set_intent(
        ctx.conn,
        intent_id,
        status="completed",
        reason="已完成：批准的内容已经成为正式板面内容。",
        outcome="completed",
        applied={"cardIds": ["card_r2_1"], "groupIds": [], "linkIds": []},
    )
    topic, _fragment = _topic(ctx)
    message_id = _append_record_message(ctx, topic, intent_ids=ids, turn_id="turn_r2_1")
    _journal(ctx.conn, "turn_r2_1", "completed")

    record = _record_of(_get_messages(client, topic), message_id)
    assert record["record_key"] == "ie:" + SUBMISSION + ":" + intent_id
    assert record["record_head"] is True
    assert record["title"] == "整理这组资料"
    assert record["status"] == "completed"
    assert record["status_label"] == "已完成"
    assert record["verified"] is True
    assert record["verified_note"] == ""
    assert record["scope"] == {
        "board_name": "互动板面",
        "intent_titles": ["整理这组资料"],
        "source": "confirm",
    }
    assert record["intents"] == [
        {
            "id": intent_id,
            "title": "整理这组资料",
            "status": "completed",
            "status_label": "已完成",
            "attempts": 1,
            "failure_reason": "",
            "result_ref": {
                "kind": "board",
                "label": "板面结果：1 张卡片",
                "target": BOARD,
            },
        }
    ]
    assert record["attempts"] == [
        {
            "attempt": 1,
            "source": "confirm",
            "turn_id": "turn_r2_1",
            "status": "completed",
            "status_label": "已完成",
            "reason": "",
            "started_at": record["attempts"][0]["started_at"],
        }
    ]
    assert record["attempts"][0]["started_at"]
    assert record["result_entry"] == {
        "kind": "board",
        "label": "板面结果：1 张卡片",
        "target": BOARD,
    }
    assert record["authorization_versions"] == {intent_id: 1}
    assert record["diagnostics"] == {
        "board_id": BOARD,
        "submission_id": SUBMISSION,
        "intent_ids": [intent_id],
        "turn_ids": ["turn_r2_1"],
        "record_key": "ie:" + SUBMISSION + ":" + intent_id,
    }


def test_batch_partial_record_title_and_items(record_env):
    """批量确认：title 用「首个名称 + 另有 N 项」，逐项完整名称在 intents[]。"""
    client, ctx = record_env
    ids = _seed_approved_intents(ctx.conn, titles=("整理这组资料", "导出对照表"))
    _set_intent(
        ctx.conn,
        ids[0],
        status="completed",
        outcome="completed",
        applied={"cardIds": ["card_a"], "groupIds": ["group_a"], "linkIds": []},
    )
    _set_intent(
        ctx.conn,
        ids[1],
        status="failed",
        reason="模型没有给出可核对的完成结果",
        outcome="failed",
    )
    _retry(ctx.conn, ids[1], status="exhausted", attempts=3, last_error="temporary timeout")
    topic, _fragment = _topic(ctx)
    message_id = _append_record_message(
        ctx, topic, intent_ids=ids, turn_id="turn_r2_batch", source="batch_confirm"
    )
    _journal(ctx.conn, "turn_r2_batch", "completed")

    record = _record_of(_get_messages(client, topic), message_id)
    assert record["title"] == "整理这组资料（另有 1 项）"
    assert record["status"] == "partial"
    assert record["status_label"] == "部分完成"
    assert [item["title"] for item in record["intents"]] == ["整理这组资料", "导出对照表"]
    assert [item["status"] for item in record["intents"]] == ["completed", "failed"]
    assert record["intents"][1]["failure_reason"] == "模型没有给出可核对的完成结果"
    assert record["intents"][0]["result_ref"]["target"] == BOARD
    assert record["scope"]["source"] == "confirm"
    assert record["scope"]["intent_titles"] == ["整理这组资料", "导出对照表"]
    assert record["result_entry"]["label"] == "板面结果：1 张卡片、1 个分组"
    assert record["verified"] is True


def test_batch_all_completed_is_completed_not_partial(record_env):
    """批量确认全部真实完成：record 级必须是 completed（不能因为有多条就显示部分完成）。"""
    client, ctx = record_env
    ids = _seed_approved_intents(ctx.conn, titles=("整理这组资料", "导出对照表"))
    for intent_id in ids:
        _set_intent(ctx.conn, intent_id, status="completed", outcome="completed")
    topic, _fragment = _topic(ctx)
    message_id = _append_record_message(
        ctx, topic, intent_ids=ids, turn_id="turn_r2_all", source="batch_confirm"
    )
    record = _record_of(_get_messages(client, topic), message_id)
    assert record["status"] == "completed"
    assert record["status_label"] == "已完成"
    assert all(item["status"] == "completed" for item in record["intents"])
    # 这条没有任何真实结果对象（applied 为空）→ 结果入口必须是 null，不能编造
    assert record["result_entry"] is None
    assert all(item["result_ref"] is None for item in record["intents"])


def test_same_record_auto_retry_keeps_one_head_and_adds_attempt(record_env):
    """自动重试（意图集不变）：同一 record_key，只有最早一条 record_head，attempts 增加。"""
    client, ctx = record_env
    ids = _seed_approved_intents(ctx.conn, titles=("整理这组资料",))
    intent_id = ids[0]
    _set_intent(ctx.conn, intent_id, status="completed", outcome="completed")
    _retry(ctx.conn, intent_id, status="completed", attempts=1)
    topic, _fragment = _topic(ctx)
    first_id = _append_record_message(
        ctx, topic, intent_ids=ids, turn_id="turn_r2_first", created_at="2026-10-29T02:00:00+00:00"
    )
    second_id = _append_record_message(
        ctx,
        topic,
        intent_ids=ids,
        turn_id="turn_r2_retry",
        attempt=2,
        source="retry",
        created_at="2026-10-29T02:01:00+00:00",
        retry_attempt=1,
        retry_limit=RETRY_LIMIT,
    )
    _journal(ctx.conn, "turn_r2_first", "completed", reason="第一轮没有完成")
    _journal(ctx.conn, "turn_r2_retry", "completed")

    messages = _get_messages(client, topic)
    first = _record_of(messages, first_id)
    second = _record_of(messages, second_id)
    assert first["record_key"] == second["record_key"] == "ie:" + SUBMISSION + ":" + intent_id
    assert first["record_head"] is True
    assert second["record_head"] is False
    assert first["status"] == "completed"
    assert [item["attempt"] for item in second["attempts"]] == [1, 2]
    assert [item["source"] for item in second["attempts"]] == ["confirm", "retry"]
    assert second["attempts"][1]["turn_id"] == "turn_r2_retry"
    assert second["intents"][0]["attempts"] == 2
    assert second["diagnostics"]["turn_ids"] == ["turn_r2_first", "turn_r2_retry"]


def test_batch_subset_retry_does_not_merge_records(record_env):
    """不同意图集不得合并：批量 [A,B] 与单条重试 [A] 是两个 record_key。"""
    client, ctx = record_env
    ids = _seed_approved_intents(ctx.conn, titles=("整理这组资料", "导出对照表"))
    topic, _fragment = _topic(ctx)
    batch_id = _append_record_message(
        ctx,
        topic,
        intent_ids=ids,
        turn_id="turn_r2_batch",
        source="batch_confirm",
        created_at="2026-10-29T03:00:00+00:00",
    )
    retry_id = _append_record_message(
        ctx,
        topic,
        intent_ids=[ids[0]],
        turn_id="turn_r2_retry",
        attempt=2,
        source="retry",
        created_at="2026-10-29T03:01:00+00:00",
    )
    messages = _get_messages(client, topic)
    batch = _record_of(messages, batch_id)
    retry = _record_of(messages, retry_id)
    assert batch["record_key"] == "ie:" + SUBMISSION + ":" + ",".join(sorted(ids))
    assert retry["record_key"] == "ie:" + SUBMISSION + ":" + ids[0]
    assert batch["record_head"] is True and retry["record_head"] is True


def test_different_submissions_are_not_merged(record_env):
    """不同 submission 即使意图集合相同也不得合并。"""
    client, ctx = record_env
    ids = _seed_approved_intents(ctx.conn, titles=("整理这组资料",))
    topic, _fragment = _topic(ctx)
    other_submission = "sub_r2_other"
    add_submission(ctx.conn, board_id=BOARD, submission_id=other_submission)
    first_id = _append_record_message(
        ctx, topic, intent_ids=ids, turn_id="turn_r2_a", created_at="2026-10-29T04:00:00+00:00"
    )
    second_id = _append_record_message(
        ctx,
        topic,
        intent_ids=ids,
        turn_id="turn_r2_b",
        submission_id=other_submission,
        created_at="2026-10-29T04:01:00+00:00",
    )
    messages = _get_messages(client, topic)
    first = _record_of(messages, first_id)
    second = _record_of(messages, second_id)
    assert first["record_key"] != second["record_key"]
    assert first["record_head"] is True and second["record_head"] is True


def test_record_head_is_computed_across_pages(record_env):
    """同 record 的第二条即使单独成页，也不得冒充 head。"""
    client, ctx = record_env
    ids = _seed_approved_intents(ctx.conn, titles=("整理这组资料",))
    topic, _fragment = _topic(ctx)
    first_id = _append_record_message(
        ctx, topic, intent_ids=ids, turn_id="turn_r2_first", created_at="2026-10-29T05:00:00+00:00"
    )
    second_id = _append_record_message(
        ctx,
        topic,
        intent_ids=ids,
        turn_id="turn_r2_second",
        attempt=2,
        source="retry",
        created_at="2026-10-29T05:01:00+00:00",
    )
    latest_page = _get_messages(client, topic, limit=1)
    assert [item["id"] for item in latest_page] == [second_id]
    assert _record_of(latest_page, second_id)["record_head"] is False
    cursor = latest_page[0]["created_at"] + "|" + latest_page[0]["id"]
    older_page = _get_messages(client, topic, limit=1, before=cursor)
    assert [item["id"] for item in older_page] == [first_id]
    assert _record_of(older_page, first_id)["record_head"] is True


def test_status_follows_real_records_across_all_seven_states(record_env):
    """7 档状态只依据真实记录：状态随真实意图 / 认领 / 重试记录变化。"""
    client, ctx = record_env
    ids = _seed_approved_intents(ctx.conn, titles=("整理这组资料",))
    intent_id = ids[0]
    topic, _fragment = _topic(ctx)
    message_id = _append_record_message(ctx, topic, intent_ids=ids, turn_id="turn_r2_live")

    def current() -> dict:
        return _record_of(_get_messages(client, topic), message_id)

    # confirmed：已确认、还没有任何执行尝试
    record = current()
    assert record["status"] == "confirmed"
    assert record["status_label"] == STATUS_LABELS["confirmed"]

    # queued：已被主循环认领（claiming）但还没开始跑
    _claim(ctx.conn, intent_id, status="claiming", source="confirm")
    assert current()["status"] == "queued"

    # running：认领已落到真实 turn_id（active）
    _claim(ctx.conn, intent_id, status="active", turn_id="turn_r2_live", source="confirm")
    row = current()
    assert row["status"] == "running"
    assert row["status_label"] == STATUS_LABELS["running"]

    # completed：真实意图记录变成 completed
    ctx.conn.execute(
        "UPDATE board_intents SET status = 'completed', outcome = 'completed' WHERE id = ?",
        (intent_id,),
    )
    assert current()["status"] == "completed"


def test_failed_record_uses_real_failure_reason(record_env):
    """failed：没有任何 completed，最后一次尝试失败 / 重试耗尽。"""
    client, ctx = record_env
    ids = _seed_approved_intents(ctx.conn, titles=("整理这组资料",))
    intent_id = ids[0]
    _set_intent(
        ctx.conn,
        intent_id,
        status="failed",
        reason="执行失败：目标卡片已被删除。不会自动重试。",
        outcome="failed",
    )
    _retry(
        ctx.conn,
        intent_id,
        status="exhausted",
        attempts=RETRY_LIMIT,
        last_error="turn_ended_without_completion",
    )
    topic, _fragment = _topic(ctx)
    message_id = _append_record_message(ctx, topic, intent_ids=ids, turn_id="turn_r2_fail")
    _journal(ctx.conn, "turn_r2_fail", "failed", reason="本轮执行失败")
    record = _record_of(_get_messages(client, topic), message_id)
    assert record["status"] == "failed"
    assert record["status_label"] == "处理失败"
    assert record["intents"][0]["failure_reason"] == "执行失败：目标卡片已被删除。不会自动重试。"
    # 没有真实完成 → 不得给结果入口；逐项也没有 result_ref
    assert record["result_entry"] is None
    assert record["intents"][0]["result_ref"] is None
    assert record["attempts"][0]["status"] == "failed"
    assert record["attempts"][0]["status_label"] == "处理失败"
    assert record["attempts"][0]["reason"] == "本轮执行失败"


def test_missing_intent_rows_are_unknown_and_unverified(record_env):
    """记录缺失 / 无法核对：unknown + verified=false + 一句实话，不猜。"""
    client, ctx = record_env
    ids = _seed_approved_intents(ctx.conn, titles=("整理这组资料",))
    topic, _fragment = _topic(ctx)
    message_id = _append_record_message(ctx, topic, intent_ids=ids, turn_id="turn_r2_gone")
    # 模拟「记录已不可核对」：连子表（版本快照）一起删干净，不能让 FK 挡住
    ctx.conn.execute("DELETE FROM board_intent_versions WHERE intent_id = ?", (ids[0],))
    ctx.conn.execute("DELETE FROM board_intents WHERE id = ?", (ids[0],))
    record = _record_of(_get_messages(client, topic), message_id)
    assert record["status"] == "unknown"
    assert record["status_label"] == STATUS_LABELS["unknown"]
    assert record["verified"] is False
    assert record["verified_note"]
    assert record["intents"][0]["status"] == "unknown"


def test_legacy_messages_are_returned_unchanged(record_env):
    """旧消息（没有 intent_execution 元数据）：原样返回，不猜、不隐藏、不改写。"""
    client, ctx = record_env
    topic, fragment = _topic(ctx)
    legacy_id, _f = ctx.memory.append_message(
        topic_id=topic,
        role="user",
        content="这是旧版本写入的普通用户消息",
        content_type="text",
        raw={},
    )
    messages = _get_messages(client, topic)
    legacy = next(item for item in messages if item["id"] == legacy_id)
    assert legacy["content"] == "这是旧版本写入的普通用户消息"
    assert legacy["content_type"] == "text"
    assert "intent_execution" not in legacy
    fragment_page = client.get(f"/api/fragments/{fragment}/messages").json()["messages"]
    legacy_fragment = next(item for item in fragment_page if item["id"] == legacy_id)
    assert "intent_execution" not in legacy_fragment


def test_fragment_messages_include_intent_execution(record_env):
    """fragment messages 通道同样附带展示对象。"""
    client, ctx = record_env
    ids = _seed_approved_intents(ctx.conn, titles=("整理这组资料",))
    _set_intent(ctx.conn, ids[0], status="completed", outcome="completed")
    topic, fragment = _topic(ctx)
    message_id = _append_record_message(ctx, topic, intent_ids=ids, turn_id="turn_r2_frag")
    page = client.get(f"/api/fragments/{fragment}/messages").json()["messages"]
    message = next(item for item in page if item["id"] == message_id)
    record = message["intent_execution"]
    assert record["title"] == "整理这组资料"
    assert record["status"] == "completed"
    assert record["record_head"] is True


def test_internal_ids_only_in_diagnostics(record_env):
    """内部编号只出现在 diagnostics：默认展示字段里不出现。"""
    client, ctx = record_env
    ids = _seed_approved_intents(ctx.conn, titles=("整理这组资料",))
    _set_intent(ctx.conn, ids[0], status="completed", outcome="completed")
    topic, _fragment = _topic(ctx)
    message_id = _append_record_message(ctx, topic, intent_ids=ids, turn_id="turn_r2_diag")
    record = _record_of(_get_messages(client, topic), message_id)
    shown = {
        key: record[key]
        for key in ("title", "status", "status_label", "scope", "result_entry")
    }
    dumped = json.dumps(shown, ensure_ascii=False)
    for internal in (SUBMISSION, "turn_r2_diag", record["record_key"]):
        assert internal not in dumped, f"内部编号不得出现在默认展示字段：{internal}"
    assert record["diagnostics"]["submission_id"] == SUBMISSION
    assert record["diagnostics"]["turn_ids"] == ["turn_r2_diag"]


def test_record_object_matches_json_in_message(record_env):
    """展示对象必须是普通 JSON（前端可直接消费），且 content_type 保持契约值。"""
    client, ctx = record_env
    ids = _seed_approved_intents(ctx.conn, titles=("整理这组资料",))
    topic, _fragment = _topic(ctx)
    message_id = _append_record_message(ctx, topic, intent_ids=ids, turn_id="turn_r2_json")
    messages = _get_messages(client, topic)
    message = next(item for item in messages if item["id"] == message_id)
    assert message["content_type"] == "intent_execution"
    json.dumps(message["intent_execution"], ensure_ascii=False)
    record = message["intent_execution"]
    assert set(record) == {
        "record_key",
        "record_head",
        "title",
        "status",
        "status_label",
        "scope",
        "intents",
        "attempts",
        "result_entry",
        "authorization_versions",
        "diagnostics",
        "verified",
        "verified_note",
    }
