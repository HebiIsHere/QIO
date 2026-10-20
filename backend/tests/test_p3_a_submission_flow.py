# -*- coding: utf-8 -*-
"""P3-A / 提交接入主循环：submit_board 的 turn_acceptor 受理语义。

验收点：
* 成功提交 = 受理一个主循环 turn（turn_acceptor 收到程序写清身份的用户消息），
  响应带 turnId / waiting，delivery 保留旧键但 understandingId=None；
* 不再创建独立理解记录、不再调用 request_understanding / run_understanding
  （即使旧理解模块还在 sys.modules 里也不调用）；
* 没有注入受理器 / 受理器抛错 / 未返回 turnId → 如实 failed（不回退旧理解）；
* empty / duplicate 不创建 turn；同身份重放返回原事实、不重复受理；
* 旧 board_understanding 记录只读可查（acceptance_lookup 仍能读到）；
* 提交—turn 绑定落库（迁移 49）。
"""

from __future__ import annotations

import sys
import types

import pytest

from agent.interactive import models, submission

from test_p3_a_support import (
    BOARD,
    FakeTurnAcceptor,
    add_submission,
    make_board,
    migrated_conn,
    save_card,
    submit,
    understanding_rows,
)


@pytest.fixture()
def legacy_understanding(monkeypatch):
    """把旧 understanding 模块塞回 sys.modules：证明新提交流程**不会调用**它。"""
    calls = {"requests": [], "runs": []}
    module = types.ModuleType("agent.interactive.understanding")

    def request_understanding(conn, *, board_id, submission_id, snapshot=None):  # noqa: ANN001
        calls["requests"].append(submission_id)
        raise AssertionError("新流程不得调用 request_understanding")

    async def run_understanding(conn, understanding_id, *, adapter_provider=None):  # noqa: ANN001
        calls["runs"].append(understanding_id)
        raise AssertionError("新流程不得调用 run_understanding")

    module.request_understanding = request_understanding
    module.run_understanding = run_understanding
    monkeypatch.setitem(sys.modules, "agent.interactive.understanding", module)
    return calls


def test_success_accepts_one_main_loop_turn(tmp_path, legacy_understanding):
    conn = migrated_conn(tmp_path)
    try:
        make_board(conn)
        save_card(conn, "勾选的说明")
        acceptor = FakeTurnAcceptor(topic_id="topic-original")
        result = submit(conn, acceptor)

        assert result["status"] == "succeeded"
        # 受理 = 恰好一次主循环受理（携带程序写清身份的消息）
        assert len(acceptor.calls) == 1
        call = acceptor.calls[0]
        assert call["board_id"] == BOARD
        assert call["submission_id"] == result["submission"]["id"]
        assert call["resume"] is False
        assert call["attempt"] == 1
        assert "【互动板面提交】" in call["message"]
        assert result["submission"]["id"] in call["message"]
        assert BOARD in call["message"]
        # 响应：turnId + waiting，旧键保留，understandingId 明确为 None
        assert result["turnId"] == "turn_fake_1"
        assert result["delivery"]["delivered"] is True
        assert result["delivery"]["status"] == "accepted"
        assert result["delivery"]["understandingId"] is None
        assert result["delivery"]["turnId"] == "turn_fake_1"
        assert result["delivery"]["boardId"] == BOARD
        assert result["delivery"]["submissionId"] == result["submission"]["id"]
        assert result["delivery"]["waiting"]["waiting"] is False
        for key in ("before", "after", "expressions", "baseline", "visibleRange", "checkedCleared"):
            assert key in result
        # 不创建任何理解记录、不调用旧理解模型
        assert understanding_rows(conn) == []
        assert legacy_understanding["requests"] == []
        assert legacy_understanding["runs"] == []
        # 提交—turn 绑定落库（最近绑定 + 历史）
        sid = result["submission"]["id"]
        row = conn.execute(
            "SELECT turn_id, resume_state FROM board_submissions WHERE id = ?", (sid,)
        ).fetchone()
        assert row["turn_id"] == "turn_fake_1"
        assert "topic-original" in row["resume_state"]
        history = conn.execute(
            "SELECT turn_id FROM board_submission_turns WHERE submission_id = ?", (sid,)
        ).fetchall()
        assert [item["turn_id"] for item in history] == ["turn_fake_1"]
        # 受理记录里的 delivery 也带 turnId（列表接口可读）
        listing = conn.execute(
            "SELECT delivery FROM board_submissions WHERE id = ?", (sid,)
        ).fetchone()
        assert "turn_fake_1" in listing["delivery"]
    finally:
        conn.close()


def test_missing_acceptor_fails_without_legacy_fallback(tmp_path, legacy_understanding):
    conn = migrated_conn(tmp_path)
    try:
        make_board(conn)
        save_card(conn, "勾选的说明")
        result = submit(conn, None)
        assert result["status"] == "failed"
        assert result["delivery"]["delivered"] is False
        assert result["delivery"]["status"] == "failed"
        assert "主循环受理器不可用" in result["delivery"]["reason"]
        assert result["turnId"] is None
        assert understanding_rows(conn) == []
        assert legacy_understanding["requests"] == []
        # 失败不更新基准：提交记录落库但 status=failed
        row = conn.execute("SELECT status FROM board_submissions").fetchone()
        assert row["status"] == "failed"
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM board_submissions WHERE status = 'succeeded'"
        ).fetchone()["n"] == 0
    finally:
        conn.close()


def test_acceptor_exception_or_missing_turn_id_fails(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        make_board(conn)
        save_card(conn, "勾选的说明")

        class Boom:
            def __call__(self, **kwargs):
                raise RuntimeError("turn manager closed")

        result = submit(conn, Boom())
        assert result["status"] == "failed"
        assert "RuntimeError" in result["delivery"]["reason"]
        assert result["turnId"] is None

        no_id = FakeTurnAcceptor(include_turn_id=False)
        result = submit(conn, no_id)
        assert result["status"] == "failed"
        assert "turnId" in result["delivery"]["reason"]
        assert result["turnId"] is None
    finally:
        conn.close()


def test_empty_and_duplicate_do_not_create_turn(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        make_board(conn)
        acceptor = FakeTurnAcceptor()
        # 空板面：没有可提交内容 → 不调用受理器
        empty = submit(conn, acceptor)
        assert empty["status"] == "empty"
        assert acceptor.calls == []
        # 一次成功提交后再提交同样内容：duplicate，不重复受理
        save_card(conn, "勾选的说明")
        first = submit(conn, acceptor)
        assert first["status"] == "succeeded"
        second = submit(conn, acceptor)
        assert second["status"] == "duplicate"
        assert len(acceptor.calls) == 1
        assert understanding_rows(conn) == []
    finally:
        conn.close()


def test_same_request_identity_replays_without_new_turn(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        make_board(conn)
        save_card(conn, "勾选的说明")
        acceptor = FakeTurnAcceptor()
        first = submit(
            conn, acceptor, client_request_id="crid-p3a-1", base_state_version=1
        )
        assert first["status"] == "succeeded" and len(acceptor.calls) == 1
        second = submit(
            conn, acceptor, client_request_id="crid-p3a-1", base_state_version=1
        )
        assert second["replayed"] is True
        assert second["submission"]["id"] == first["submission"]["id"]
        assert second["turnId"] == first["turnId"]
        assert second["delivery"]["turnId"] == first["delivery"]["turnId"]
        assert len(acceptor.calls) == 1, "重放不得创建第二个主循环 turn"
        # 同身份不同内容 → 冲突，不落新提交、不创建 turn
        with pytest.raises(submission.RequestIdentityConflict):
            submit(
                conn,
                acceptor,
                client_request_id="crid-p3a-1",
                base_state_version=1,
                note="不同备注",
            )
        assert len(acceptor.calls) == 1
    finally:
        conn.close()


def test_old_understanding_history_stays_readable(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        make_board(conn)
        sid = add_submission(conn)
        stamp = models.now_iso()
        conn.execute(
            "INSERT INTO board_understanding (id, board_id, submission_id, request_id, status,"
            " created_at, updated_at) VALUES ('u_old', ?, ?, 'req:old', 'understood', ?, ?)",
            (BOARD, sid, stamp, stamp),
        )
        facts = submission.acceptance_lookup(conn, BOARD, "crid-none")
        assert facts is None  # 没有这个请求身份
        conn.execute(
            "UPDATE board_submissions SET client_request_id = 'crid-old' WHERE id = ?", (sid,)
        )
        found = submission.acceptance_lookup(conn, BOARD, "crid-old")
        assert found is not None
        assert found["understandingId"] == "u_old"
        assert found["understandingStatus"] == "understood"
        # 新流程成功一次：旧记录原样保留、不被删除
        save_card(conn, "勾选的说明")
        submit(conn, FakeTurnAcceptor())
        assert len(understanding_rows(conn)) == 1
    finally:
        conn.close()
