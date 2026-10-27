# -*- coding: utf-8 -*-
"""D 独立契约测试：提交接入主循环、等待持久化与自动续作（契约 §13.3）。

覆盖：
* 提交受理不再创建独立理解记录（board_understanding 不新增），响应带主循环 turn 身份；
* 等待事项持久化（record_waiting / get_waiting / waiting_state）与跨重启恢复；
* 只有明确 resolved_question_ids 才解除等待（空列表不清除）、resolve 幂等；
* 聊天请求绑定：普通模式不设板面上下文；伪造 / 越界 boardId、非本板面 submissionId 拒绝；
* 等待中的板面，下一条聊天消息自动绑定为原提交的续作（resume=true）。

装置：真实 DB + 迁移 49；聊天 / 提交路由用 TestClient + fake adapter（模型不参与）。

基线说明：本阶段实现未合入时（A 的等待函数 / 绑定接线缺失）本文件失败是预期。
运行（只跑本文件）：
    cd backend
    & 'D:/qio-dev/qio/backend/.venv/Scripts/python.exe' -m pytest tests/test_p3_d_submission_resume.py -q
"""

from __future__ import annotations

import asyncio
import importlib
import sqlite3

import pytest
from fastapi.testclient import TestClient

CLAUSE = "契约 §13.3"
BOARD = "board_p3_d_resume"

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



def _load(name: str):
    try:
        return importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            f"{CLAUSE}：契约要求的 {name} 不可导入；阶段三实现未合入时属于「未验证」：{exc!r}",
            pytrace=False,
        )


def _submission():
    return _load("agent.interactive.submission")


class FakeAdapter:
    mode = "native"
    model = "fake-p3-d"

    async def complete(self, messages, tools, *, temperature=None, max_tokens=None):
        from agent.adapters.base import ChatMessage, Completion

        return Completion(message=ChatMessage(role="assistant", content="（测试用回答）"))


async def _fake_value(value):
    return value


def _fake_build_adapter(ctx):
    from unittest.mock import AsyncMock

    return AsyncMock(return_value=FakeAdapter())


def _run(value):
    if asyncio.iscoroutine(value):
        return asyncio.run(value)
    return value


def _ensure_board(conn, board_id: str = BOARD) -> None:
    from agent.interactive import board_store

    board_store.ensure_board(conn, board_id=board_id)


def _seed_content_board(conn, board_id: str = BOARD) -> dict:
    """有内容可提交的板面：一张勾选注释（提交会求差成 intentBearing）。"""
    from agent.interactive import board_store, models

    _ensure_board(conn, board_id)
    state = models.empty_state(board_id)
    note = models.new_card("text", "这次要整理的原始笔记", checked=True)
    state["cards"] = [note]
    board_store.save_board(conn, board_id, state, reason="p3-d-seed")
    return note


def _add_submission(conn, board_id: str = BOARD, submission_id: str = "") -> str:
    """直接落一条提交记录（等待 / 绑定测试只需要记录存在）。"""
    from agent.interactive import models

    sid = submission_id or models.new_id("sub")
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM board_submissions WHERE board_id = ?",
        (board_id,)).fetchone()["n"]
    conn.execute(
        "INSERT INTO board_submissions (id, board_id, seq, status, created_at)"
        " VALUES (?, ?, ?, 'accepted', ?)",
        (sid, board_id, int(seq), models.now_iso()))
    conn.commit()
    return sid


def _count(conn, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


def _record_waiting(conn, *, submission_id: str, question: str = "还缺截止时间吗？",
                    turn_id: str = "turn_wait") -> dict:
    function = _submission()
    fn = getattr(function, "record_waiting", None)
    if fn is None:
        pytest.fail(f"{CLAUSE}：submission.record_waiting 未实现（等待持久化缺失）",
                    pytrace=False)
    return _run(fn(conn, board_id=BOARD, submission_id=submission_id,
                   question=question, missing_fields=["deadline"], turn_id=turn_id))


@pytest.fixture()
def client(db_conn, settings, monkeypatch):
    from agent.api.server import create_app

    app = create_app(settings, db_conn)
    ctx = app.state.ctx
    monkeypatch.setattr(ctx, "build_adapter", _fake_build_adapter(ctx))
    with TestClient(app) as c:
        yield c, ctx


# ---------------------------------------------------------------------------
# 等待持久化（A 的公开函数）
# ---------------------------------------------------------------------------


def test_waiting_record_get_bind_and_state(db_conn):
    submission = _submission()
    for name in ("record_waiting", "resolve_waiting", "get_waiting",
                 "bind_submission_turn", "waiting_state"):
        if not hasattr(submission, name):
            pytest.fail(f"{CLAUSE}：submission.{name} 未实现（A 的等待公开接口缺失）",
                        pytrace=False)
    _ensure_board(db_conn)
    submission_id = _add_submission(db_conn)
    binding = submission.bind_submission_turn(
        db_conn, submission_id=submission_id, turn_id="turn_wait_1",
        resume_state={"kind": "submission", "topicId": "topic_x"})
    assert isinstance(binding, dict) and binding["submissionId"] == submission_id
    assert binding["turnId"] == "turn_wait_1"

    item = _record_waiting(db_conn, submission_id=submission_id, turn_id="turn_wait_1")
    assert item.get("created") is True and item.get("status") == "waiting"
    waiting = submission.get_waiting(db_conn, BOARD)
    assert isinstance(waiting, dict) and waiting["count"] == 1
    assert "还缺截止时间吗" in str(waiting)
    state = submission.waiting_state(db_conn, BOARD)
    assert state["waiting"] is True and state["count"] == 1
    assert submission.get_waiting(db_conn, "board_other") is None
    assert submission.waiting_state(db_conn, "board_other")["waiting"] is False


def test_waiting_survives_restart(tmp_path):
    submission = _submission()
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations

    path = tmp_path / "p3_waiting.db"
    conn = connect(path)
    apply_migrations(conn)
    _ensure_p3_schema(conn)
    _ensure_board(conn)
    submission_id = _add_submission(conn)
    _record_waiting(conn, submission_id=submission_id, turn_id="turn_restart")
    conn.close()

    reopened = connect(path)
    try:
        apply_migrations(reopened)
        _ensure_p3_schema(reopened)
        waiting = submission.get_waiting(reopened, BOARD)
        assert waiting is not None and "还缺截止时间吗" in str(waiting), (
            f"{CLAUSE}：等待事项必须跨重启恢复（实际 {waiting!r}）")
    finally:
        reopened.close()


def test_resolve_waiting_requires_explicit_ids_and_is_idempotent(db_conn):
    submission = _submission()
    _ensure_board(db_conn)
    submission_id = _add_submission(db_conn)
    item = _record_waiting(db_conn, submission_id=submission_id, turn_id="turn_resolve")
    question_id = str(item["id"])

    # 回答不足 / 其他事项：空列表不解除任何等待
    assert submission.resolve_waiting(
        db_conn, board_id=BOARD, submission_id=submission_id, question_ids=()) == 0
    assert submission.get_waiting(db_conn, BOARD) is not None, (
        f"{CLAUSE}：没有明确 question id 时不得机械清除等待")

    resolved = submission.resolve_waiting(
        db_conn, board_id=BOARD, submission_id=submission_id,
        question_ids=[question_id])
    assert int(resolved) == 1, f"{CLAUSE}：明确解除应成功（实际 {resolved}）"
    assert submission.get_waiting(db_conn, BOARD) is None
    again = submission.resolve_waiting(
        db_conn, board_id=BOARD, submission_id=submission_id,
        question_ids=[question_id])
    assert int(again) == 0, f"{CLAUSE}：resolve_waiting 必须幂等（实际 {again}）"


# ---------------------------------------------------------------------------
# 提交接入主循环（不再创建独立理解记录）
# ---------------------------------------------------------------------------


def test_submit_creates_main_loop_turn_not_understanding(client, db_conn):
    _seed_content_board(db_conn)
    before_understanding = _count(db_conn, "board_understanding")
    client_obj, _ctx = client
    response = client_obj.post(f"/api/interactive/boards/{BOARD}/submissions", json={})
    assert response.status_code == 200, (
        f"{CLAUSE}：提交应被受理（实际 {response.status_code}: {response.text[:300]}）")
    body = response.json()
    assert body.get("status") == "succeeded", (
        f"{CLAUSE}：有内容的提交应成功（实际 {body.get('status')}: {body.get('delivery')}）")
    assert _count(db_conn, "board_understanding") == before_understanding, (
        f"{CLAUSE}：提交不得再创建独立理解记录")
    assert "turnid" in str(body).lower() or "turn_id" in str(body).lower(), (
        f"{CLAUSE}：提交响应必须给出主循环 turn 身份（实际 {str(body)[:500]}）")


def test_submit_unknown_board_creates_no_turn(client):
    """§13.3：不存在的板面 / 空内容不会创建主循环 turn，也不调用 QIO。

    既有提交路由保留「empty / duplicate 不调用 QIO」的语义；D 断言的是
    **不产生主循环 turn 身份**（turnId 为空），不假装受理成功。
    """
    client_obj, _ctx = client
    response = client_obj.post("/api/interactive/boards/board_ghost/submissions", json={})
    if response.status_code in (400, 404):
        return
    assert response.status_code == 200, (
        f"{CLAUSE}：不存在的板面提交不应 5xx（实际 {response.status_code}）")
    body = response.json()
    assert body.get("status") != "succeeded", (
        f"{CLAUSE}：空板面提交不应变成成功受理：{str(body)[:500]}")
    assert not body.get("turnId"), (
        f"{CLAUSE}：没有可提交内容时不得创建主循环 turn：{str(body)[:500]}")


# ---------------------------------------------------------------------------
# 聊天请求绑定：模式 / 板面 / 提交身份校验
# ---------------------------------------------------------------------------


def _chat_payload(**extra) -> dict:
    body = {"message": "继续整理这些材料"}
    body.update(extra)
    return body


def test_normal_mode_chat_has_no_board_binding(client, db_conn):
    client_obj, _ctx = client
    _ensure_board(db_conn)
    response = client_obj.post("/api/turns", json=_chat_payload())
    assert response.status_code == 200, (
        f"{CLAUSE}：普通模式聊天应正常受理（实际 {response.status_code}: {response.text[:300]}）")
    body = response.json()
    assert not body.get("board_id") and not body.get("boardId"), (
        f"{CLAUSE}：普通模式不得绑定板面：{body}")


def test_forged_or_out_of_range_board_binding_is_rejected(client, db_conn):
    client_obj, _ctx = client
    _ensure_board(db_conn)
    forged = client_obj.post("/api/turns", json=_chat_payload(
        mode="interactive", boardId="board_ghost", submissionId="sub_ghost"))
    assert forged.status_code in (400, 403, 404, 409), (
        f"{CLAUSE}：越界 boardId 必须拒绝（实际 {forged.status_code}: {forged.text[:300]}）")


def test_submission_of_another_board_is_rejected(client, db_conn):
    client_obj, _ctx = client
    _ensure_board(db_conn)
    other = "board_p3_d_other"
    _ensure_board(db_conn, other)
    _add_submission(db_conn, other, "sub_other")
    response = client_obj.post("/api/turns", json=_chat_payload(
        mode="interactive", boardId=BOARD, submissionId="sub_other"))
    assert response.status_code in (400, 403, 404, 409), (
        f"{CLAUSE}：提交不属于该板面时必须拒绝（实际 {response.status_code}: {response.text[:300]}）")


def test_next_chat_binds_resume_of_waiting_submission(client, db_conn):
    """§13.3：等待中的板面，下一条聊天消息自动续作原提交并保留等待与原话题。"""
    submission = _submission()
    client_obj, ctx = client
    _seed_content_board(db_conn)
    # 真实完整的原提交绑定：生产路径里提交受理时就保存了当时的话题。
    # 缺这条绑定的数据不是合法成功数据（FU3 明确拒绝），修复前本用例因此得到 400。
    original_topic = ctx.current_topic()
    # 界面当前话题故意切到另一个**真实话题**：续作只能写回原提交保存的话题，
    # 当前话题不得覆盖它（与确认 / 恢复 / 重试同一规则）。
    switched_topic = ctx.topics.nodes.create_topic("P3-D 续作时的界面当前话题")
    ctx.navigation.anchors.set_active(switched_topic.id)
    assert ctx.current_topic() == switched_topic.id

    submission_id = _add_submission(db_conn)
    submission.bind_submission_turn(
        db_conn,
        submission_id=submission_id,
        turn_id="turn_first",
        resume_state={"kind": "submission", "topicId": original_topic},
    )
    _record_waiting(db_conn, submission_id=submission_id, turn_id="turn_first")

    reply = client_obj.post("/api/turns", json=_chat_payload(
        mode="interactive", boardId=BOARD))
    assert reply.status_code == 200, (
        f"{CLAUSE}：等待中的续作消息应被受理（实际 {reply.status_code}: {reply.text[:300]}）")
    body = reply.json()
    assert body.get("mode") == "interactive", f"{CLAUSE}：响应应标出互动模式：{body}"
    assert body.get("resume") is True, (
        f"{CLAUSE}：下一条消息必须绑定为原提交的续作（resume=true）：{body}")
    assert str(body.get("submission_id") or "") == submission_id, (
        f"{CLAUSE}：续作必须绑定到原 submission_id：{body}")
    assert str(body.get("topic_id") or "") == original_topic, (
        f"{CLAUSE}：续作必须写回原提交保存的原话题，当前话题不得覆盖：{body}")
    binding = submission.submission_binding(db_conn, submission_id)
    assert binding is not None, (
        f"{CLAUSE}：续作受理后必须保留原提交的绑定（实际 {binding!r}）")
    assert str(binding["resumeState"].get("topicId") or "") == original_topic, (
        f"{CLAUSE}：续作不得改写原提交保存的原话题：{binding}")
    waiting = submission.get_waiting(db_conn, BOARD)
    assert waiting is not None, (
        f"{CLAUSE}：回答不足 / 未由工具解除时不得机械清除等待（实际 {waiting!r}）")
