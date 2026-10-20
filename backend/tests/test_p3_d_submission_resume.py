# -*- coding: utf-8 -*-
"""D 独立契约测试：提交接入主循环、等待持久化与自动续作（契约 §13.3）。

覆盖：
* 提交受理不再创建独立理解记录（board_understanding 不新增），响应带主循环 turn 身份；
* 等待事项持久化（record_waiting / get_waiting / waiting_state）与跨重启恢复；
* resolve_waiting 幂等；回答不足时不清除等待；
* 聊天请求绑定：普通模式不设板面上下文；伪造 / 越界 boardId、非本板面 submissionId 拒绝；
* 等待中的板面，下一条聊天消息自动绑定为原提交的续作（board_resume）。

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


def _seed_board(conn) -> dict:
    from agent.interactive import board_store, models

    state = models.empty_state(BOARD)
    note = models.new_card("text", "这次要整理的原始笔记", checked=True)
    state["cards"] = [note]
    board_store.save_board(conn, BOARD, state, reason="p3-d-seed")
    return note


class FakeAdapter:
    """不会联网的最小适配器：主循环即使真的跑起来也不调用真实模型。"""

    mode = "native"
    model = "fake-p3-d"

    async def complete(self, messages, tools, *, temperature=None, max_tokens=None):
        from agent.adapters.base import ChatMessage, Completion

        return Completion(message=ChatMessage(role="assistant", content="（测试用回答）"))


def _fake_build_adapter(ctx):
    from unittest.mock import AsyncMock

    return AsyncMock(return_value=FakeAdapter())


def _run(value):
    if asyncio.iscoroutine(value):
        return asyncio.run(value)
    return value


def _count(conn, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"])


def _latest_submission_id(conn, board_id: str = BOARD) -> str:
    row = conn.execute(
        "SELECT id FROM board_submissions WHERE board_id = ? ORDER BY created_at DESC, id DESC"
        " LIMIT 1", (board_id,)).fetchone()
    assert row is not None, f"{CLAUSE}：没有找到提交记录"
    return str(row["id"])


def _record_waiting(conn, *, question: str = "还缺截止时间吗？",
                    submission_id: str = "", turn_id: str = "turn_wait") -> dict:
    submission = _submission()
    fn = getattr(submission, "record_waiting", None)
    if fn is None:
        pytest.fail(f"{CLAUSE}：submission.record_waiting 未实现（等待持久化缺失）",
                    pytrace=False)
    return _run(fn(conn, board_id=BOARD, submission_id=submission_id,
                   question=question, missing_fields=["deadline"], turn_id=turn_id))


# ---------------------------------------------------------------------------
# 等待持久化（A 的公开函数）
# ---------------------------------------------------------------------------


def test_waiting_record_get_and_state(db_conn):
    submission = _submission()
    for name in ("record_waiting", "resolve_waiting", "get_waiting",
                 "bind_submission_turn", "waiting_state"):
        if not hasattr(submission, name):
            pytest.fail(f"{CLAUSE}：submission.{name} 未实现（A 的等待公开接口缺失）",
                        pytrace=False)
    _seed_board(db_conn)
    binding = _run(submission.bind_submission_turn(
        db_conn, submission_id="sub_wait_1", turn_id="turn_wait_1",
        resume_state="running"))
    assert isinstance(binding, dict), f"{CLAUSE}：bind_submission_turn 必须返回 dict"
    _record_waiting(db_conn, submission_id="sub_wait_1", turn_id="turn_wait_1")
    waiting = _run(submission.get_waiting(db_conn, BOARD))
    assert isinstance(waiting, dict), f"{CLAUSE}：get_waiting 必须返回等待事项 dict"
    text = str(waiting)
    assert "还缺截止时间吗" in text, f"{CLAUSE}：等待问题必须原样持久化：{text[:300]}"
    state = _run(submission.waiting_state(db_conn, BOARD))
    assert isinstance(state, dict)
    assert any(token in str(state).lower() for token in ("wait", "true", "pending")), (
        f"{CLAUSE}：waiting_state 必须如实报告存在等待：{str(state)[:300]}")


def test_waiting_survives_restart(tmp_path):
    submission = _submission()
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations

    path = tmp_path / "p3_waiting.db"
    conn = connect(path)
    apply_migrations(conn)
    _seed_board(conn)
    _record_waiting(conn, submission_id="sub_restart", turn_id="turn_restart")
    conn.close()

    reopened = connect(path)
    try:
        apply_migrations(reopened)
        waiting = _run(submission.get_waiting(reopened, BOARD))
        assert waiting is not None and "还缺截止时间吗" in str(waiting), (
            f"{CLAUSE}：等待事项必须跨重启恢复（实际 {waiting!r}）")
    finally:
        reopened.close()


def test_resolve_waiting_is_idempotent():
    submission = _submission()
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations
    import tempfile
    from pathlib import Path

    path = Path(tempfile.mkdtemp(prefix="p3_resolve_")) / "x.db"
    conn = connect(path)
    try:
        apply_migrations(conn)
        _seed_board(conn)
        _record_waiting(conn, submission_id="sub_resolve", turn_id="turn_resolve")
        resolved = _run(submission.resolve_waiting(
            conn, board_id=BOARD, submission_id="sub_resolve", question_ids=()))
        assert int(resolved) >= 1, f"{CLAUSE}：resolve_waiting 应解除等待（实际 {resolved}）"
        remaining = _run(submission.get_waiting(conn, BOARD))
        assert not remaining or not str(remaining).lower().count("awaiting"), (
            f"{CLAUSE}：解除后不应再有等待中的问题（实际 {remaining!r}）")
        again = _run(submission.resolve_waiting(
            conn, board_id=BOARD, submission_id="sub_resolve", question_ids=()))
        assert int(again) == 0, f"{CLAUSE}：resolve_waiting 必须幂等（实际 {again}）"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 提交接入主循环（不再创建独立理解记录）
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(db_conn, settings, monkeypatch):
    from agent.api.server import create_app

    app = create_app(settings, db_conn)
    ctx = app.state.ctx
    monkeypatch.setattr(ctx, "build_adapter", _fake_build_adapter(ctx))
    with TestClient(app) as c:
        yield c, ctx


def test_submit_creates_main_loop_turn_not_understanding(client, db_conn):
    _seed_board(db_conn)
    before_understanding = _count(db_conn, "board_understanding")
    client_obj, _ctx = client
    response = client_obj.post(f"/api/interactive/boards/{BOARD}/submissions", json={})
    assert response.status_code == 200, (
        f"{CLAUSE}：提交应被受理（实际 {response.status_code}: {response.text[:300]}）")
    body = response.json()
    assert body.get("status") == "succeeded", (
        f"{CLAUSE}：有内容的提交应成功（实际 {body.get('status')}: {body.get('delivery')}）")
    after_understanding = _count(db_conn, "board_understanding")
    assert after_understanding == before_understanding, (
        f"{CLAUSE}：提交不得再创建独立理解记录（{before_understanding} -> {after_understanding}）")
    text = str(body)
    assert "turn" in text.lower(), (
        f"{CLAUSE}：提交响应必须给出主循环 turn 身份（实际 {text[:500]}）")


def test_submit_unknown_board_is_rejected(client):
    client_obj, _ctx = client
    response = client_obj.post("/api/interactive/boards/board_ghost/submissions", json={})
    assert response.status_code in (400, 404), (
        f"{CLAUSE}：不存在的板面提交必须拒绝（实际 {response.status_code}）")


# ---------------------------------------------------------------------------
# 聊天请求绑定：模式 / 板面 / 提交身份校验
# ---------------------------------------------------------------------------


def _chat_payload(**extra) -> dict:
    body = {"message": "继续整理这些材料"}
    body.update(extra)
    return body


def test_normal_mode_chat_has_no_board_binding(client, db_conn):
    client_obj, _ctx = client
    _seed_board(db_conn)
    response = client_obj.post("/api/turns", json=_chat_payload())
    assert response.status_code == 200, (
        f"{CLAUSE}：普通模式聊天应正常受理（实际 {response.status_code}: {response.text[:300]}）")
    body = response.json()
    assert not body.get("board_id") and not body.get("boardId"), (
        f"{CLAUSE}：普通模式不得绑定板面：{body}")


def test_forged_or_out_of_range_board_binding_is_rejected(client, db_conn):
    client_obj, _ctx = client
    _seed_board(db_conn)
    forged = client_obj.post("/api/turns", json=_chat_payload(
        mode="interactive", boardId="board_ghost", submissionId="sub_ghost"))
    assert forged.status_code in (400, 403, 404, 409), (
        f"{CLAUSE}：越界 boardId 必须拒绝（实际 {forged.status_code}: {forged.text[:300]}）")


def test_submission_of_another_board_is_rejected(client, db_conn):
    client_obj, _ctx = client
    _seed_board(db_conn)
    from agent.interactive import board_store, models

    other = "board_p3_d_other"
    board_store.save_board(db_conn, other, models.empty_state(other), reason="other")
    db_conn.execute(
        "INSERT INTO board_submissions (id, board_id, seq, status, before, after,"
        " visible, expressions, baseline, delivery, content_hash, created_at, updated_at)"
        " VALUES ('sub_other', ?, 1, 'succeeded', '{}', '{}', '[]', '[]', '{}', '{}', '',"
        " '2026-10-21T00:00:00+00:00', '2026-10-21T00:00:00+00:00')",
        (other,))
    db_conn.commit()
    response = client_obj.post("/api/turns", json=_chat_payload(
        mode="interactive", boardId=BOARD, submissionId="sub_other"))
    assert response.status_code in (400, 403, 404, 409), (
        f"{CLAUSE}：提交不属于该板面时必须拒绝（实际 {response.status_code}: {response.text[:300]}）")


def test_next_chat_binds_resume_of_waiting_submission(client, db_conn):
    """§13.3：等待中的板面，下一条聊天消息自动续作原提交并保留等待。"""
    submission = _submission()
    _seed_board(db_conn)
    client_obj, ctx = client
    response = client_obj.post(f"/api/interactive/boards/{BOARD}/submissions", json={})
    assert response.status_code == 200 and response.json().get("status") == "succeeded"
    submission_id = _latest_submission_id(db_conn)
    _record_waiting(db_conn, submission_id=submission_id, turn_id="turn_first")

    reply = client_obj.post("/api/turns", json=_chat_payload(
        mode="interactive", boardId=BOARD, submissionId=submission_id))
    assert reply.status_code == 200, (
        f"{CLAUSE}：等待中的续作消息应被受理（实际 {reply.status_code}: {reply.text[:300]}）")
    body = reply.json()
    text = str(body)
    assert "resume" in text.lower() or "board_resume" in text.lower() or (
        body.get("board_resume") is True), (
        f"{CLAUSE}：下一条消息必须绑定为原提交的续作（board_resume）：{text[:500]}")

    waiting = _run(submission.get_waiting(db_conn, BOARD))
    assert waiting is not None, (
        f"{CLAUSE}：回答不足 / 未由工具解除时不得机械清除等待（实际 {waiting!r}）")
