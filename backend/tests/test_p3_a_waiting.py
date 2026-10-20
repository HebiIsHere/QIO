# -*- coding: utf-8 -*-
"""P3-A / 等待与续作持久化：record_waiting / resolve_waiting / get_waiting /
waiting_state / bind_submission_turn / submission_binding / resolve_chat_binding。

验收点：
* 等待事项持久保存，跨连接（重启）恢复；同一事项不重复记录；
* 只有明确的 resolved_question_ids 才解除等待：回答不足 / 其他事项不清除；
* get_waiting 没有等待返回 None；board_waiting 表不可用时如实抛 sqlite3.Error
  （不伪装成「没有等待」）；
* 提交—turn 绑定幂等、保留历次轮次；聊天绑定校验板面 / 提交归属 / 模式。
"""

from __future__ import annotations

import sqlite3

import pytest

from agent.interactive import submission
from agent.storage.db import connect

from test_p3_a_support import (
    BOARD,
    add_submission,
    make_board,
    migrated_conn,
)


# ---------------------------------------------------------------------------
# record_waiting / get_waiting / waiting_state
# ---------------------------------------------------------------------------


def test_record_waiting_persists_and_is_readable(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        make_board(conn)
        sid = add_submission(conn)
        item = submission.record_waiting(
            conn,
            board_id=BOARD,
            submission_id=sid,
            question="这次改动涉及哪张卡片？",
            missing_fields=["card_ids"],
            turn_id="turn_1",
        )
        assert item["created"] is True
        assert item["boardId"] == BOARD
        assert item["submissionId"] == sid
        assert item["turnId"] == "turn_1"
        assert item["status"] == "waiting"
        assert item["missingFields"] == ["card_ids"]
        assert item["resolvedQuestionIds"] == []
        assert item["id"].startswith("wait")

        state = submission.get_waiting(conn, BOARD)
        assert state is not None
        assert state["count"] == 1
        assert state["submissionId"] == sid
        assert state["questionIds"] == [item["id"]]
        assert state["items"][0]["question"] == "这次改动涉及哪张卡片？"

        view = submission.waiting_state(conn, BOARD)
        assert view["waiting"] is True
        assert view["count"] == 1
        assert view["latestSubmissionId"] == sid
        # 空板面：永远 dict，且 waiting=False
        empty = submission.waiting_state(conn, "board_other")
        assert empty["waiting"] is False and empty["count"] == 0
        assert submission.get_waiting(conn, "board_other") is None
    finally:
        conn.close()


def test_same_question_is_not_recorded_twice(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        make_board(conn)
        sid = add_submission(conn)
        first = submission.record_waiting(
            conn, board_id=BOARD, submission_id=sid, question="缺少什么信息？"
        )
        second = submission.record_waiting(
            conn, board_id=BOARD, submission_id=sid, question="  缺少什么信息？ "
        )
        assert second["created"] is False
        assert second["id"] == first["id"]
        assert submission.waiting_state(conn, BOARD)["count"] == 1
        # 不同问题 → 新事项
        third = submission.record_waiting(
            conn, board_id=BOARD, submission_id=sid, question="另一个问题？"
        )
        assert third["created"] is True
        assert submission.waiting_state(conn, BOARD)["count"] == 2
    finally:
        conn.close()


def test_record_waiting_validates_inputs(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        make_board(conn)
        sid = add_submission(conn)
        with pytest.raises(ValueError):
            submission.record_waiting(conn, board_id=BOARD, submission_id=sid, question="  ")
        with pytest.raises(ValueError):
            submission.record_waiting(conn, board_id="", submission_id=sid, question="q")
        with pytest.raises(ValueError):
            submission.record_waiting(conn, board_id=BOARD, submission_id="", question="q")
        with pytest.raises(ValueError):
            submission.record_waiting(
                conn, board_id=BOARD, submission_id="sub_missing", question="q"
            )
        # 提交不属于这个板面：拒绝伪造关联
        make_board(conn, "board_other")
        with pytest.raises(ValueError):
            submission.record_waiting(
                conn, board_id="board_other", submission_id=sid, question="q"
            )
    finally:
        conn.close()


def test_get_waiting_can_filter_by_submission(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        make_board(conn)
        first = add_submission(conn)
        second = add_submission(conn)
        w1 = submission.record_waiting(conn, board_id=BOARD, submission_id=first, question="q1")
        w2 = submission.record_waiting(conn, board_id=BOARD, submission_id=second, question="q2")
        by_first = submission.get_waiting(conn, BOARD, submission_id=first)
        assert by_first is not None and by_first["questionIds"] == [w1["id"]]
        by_second = submission.get_waiting(conn, BOARD, submission_id=second)
        assert by_second is not None and by_second["questionIds"] == [w2["id"]]
        whole = submission.get_waiting(conn, BOARD)
        assert whole is not None and whole["count"] == 2
        assert set(whole["questionIds"]) == {w1["id"], w2["id"]}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# resolve_waiting：只按明确 id 解除，不机械清除
# ---------------------------------------------------------------------------


def test_resolve_waiting_requires_explicit_ids(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        make_board(conn)
        sid = add_submission(conn)
        first = submission.record_waiting(
            conn, board_id=BOARD, submission_id=sid, question="需要什么格式？"
        )
        second = submission.record_waiting(
            conn, board_id=BOARD, submission_id=sid, question="要放哪张卡？"
        )
        # 回答不足 / 属于别的事项：空 id 不解除任何等待
        assert submission.resolve_waiting(conn, board_id=BOARD, submission_id=sid) == 0
        state = submission.get_waiting(conn, BOARD)
        assert state is not None and state["count"] == 2
        # 只解除明确的那一条
        assert (
            submission.resolve_waiting(
                conn, board_id=BOARD, submission_id=sid, question_ids=[second["id"]]
            )
            == 1
        )
        state = submission.get_waiting(conn, BOARD)
        assert state is not None and state["questionIds"] == [first["id"]]
        # 幂等：重复解除不再计数
        assert (
            submission.resolve_waiting(
                conn, board_id=BOARD, submission_id=sid, question_ids=[second["id"]]
            )
            == 0
        )
        # 解除最后一条后：没有等待
        assert (
            submission.resolve_waiting(
                conn, board_id=BOARD, submission_id=sid, question_ids=[first["id"]]
            )
            == 1
        )
        assert submission.get_waiting(conn, BOARD) is None
        row = conn.execute(
            "SELECT status, resolved_question_ids FROM board_waiting WHERE id = ?",
            (first["id"],),
        ).fetchone()
        assert row["status"] == "resolved"
        assert first["id"] in row["resolved_question_ids"]
    finally:
        conn.close()


def test_resolve_waiting_cannot_cross_submission(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        make_board(conn)
        first = add_submission(conn)
        second = add_submission(conn)
        item = submission.record_waiting(
            conn, board_id=BOARD, submission_id=second, question="问题？"
        )
        # 用第一个提交的 id 去解除第二个提交的等待：不允许
        assert (
            submission.resolve_waiting(
                conn, board_id=BOARD, submission_id=first, question_ids=[item["id"]]
            )
            == 0
        )
        assert submission.get_waiting(conn, BOARD)["count"] == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 跨重启恢复 / 缺表如实报错
# ---------------------------------------------------------------------------


def test_waiting_survives_reconnect(tmp_path):
    path = tmp_path / "p3a.db"
    conn = migrated_conn(tmp_path)
    try:
        make_board(conn)
        sid = add_submission(conn)
        item = submission.record_waiting(
            conn, board_id=BOARD, submission_id=sid, question="重启后还在吗？", turn_id="t1"
        )
        conn.close()
        reopened = connect(path)
        try:
            state = submission.get_waiting(reopened, BOARD)
            assert state is not None
            assert state["items"][0]["id"] == item["id"]
            assert state["items"][0]["turnId"] == "t1"
        finally:
            reopened.close()
    finally:
        pass


def test_missing_waiting_table_raises_instead_of_faking_none(tmp_path):
    """迁移 49 未应用时：读函数如实抛 sqlite3.Error（B 的完成核对据此降级）。"""
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations

    conn = connect(tmp_path / "no_p3a.db")
    try:
        apply_migrations(conn)  # 只到冻结基线（未登记 49）
        with pytest.raises(sqlite3.Error):
            submission.get_waiting(conn, BOARD)
        with pytest.raises(sqlite3.Error):
            submission.waiting_state(conn, BOARD)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# bind_submission_turn / submission_binding
# ---------------------------------------------------------------------------


def test_bind_submission_turn_is_idempotent_and_keeps_history(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        make_board(conn)
        sid = add_submission(conn)
        first = submission.bind_submission_turn(
            conn,
            submission_id=sid,
            turn_id="turn_a",
            resume_state={"topicId": "topic-1", "kind": "submission"},
        )
        assert first["boardId"] == BOARD
        assert first["turnId"] == "turn_a"
        assert first["resumeState"]["topicId"] == "topic-1"
        row = conn.execute(
            "SELECT turn_id, resume_state FROM board_submissions WHERE id = ?", (sid,)
        ).fetchone()
        assert row["turn_id"] == "turn_a"
        assert "topic-1" in row["resume_state"]
        # 同一 (submission, turn) 重复绑定：幂等、不新增历史行
        again = submission.bind_submission_turn(
            conn, submission_id=sid, turn_id="turn_a", resume_state={"attempt": 2}
        )
        assert again["resumeState"]["attempt"] == 2
        assert again["resumeState"]["topicId"] == "topic-1"
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM board_submission_turns WHERE submission_id = ?",
            (sid,),
        ).fetchone()["n"]
        assert count == 1
        # 第二次续作绑定：历史 +1，最近 turn 更新
        submission.bind_submission_turn(
            conn, submission_id=sid, turn_id="turn_b", resume_state={"attempt": 3}
        )
        binding = submission.submission_binding(conn, sid)
        assert binding is not None
        assert binding["turnId"] == "turn_b"
        assert [item["turnId"] for item in binding["turns"]] == ["turn_a", "turn_b"]
        assert binding["resumeState"]["attempt"] == 3
    finally:
        conn.close()


def test_bind_submission_turn_unknown_submission_raises(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        make_board(conn)
        with pytest.raises(ValueError):
            submission.bind_submission_turn(conn, submission_id="sub_missing", turn_id="t1")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# resolve_chat_binding：普通 / 互动 / 自动续作 / 越界
# ---------------------------------------------------------------------------


def test_chat_binding_normal_mode_has_no_board(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        binding = submission.resolve_chat_binding(conn, mode=None)
        assert binding["mode"] == "normal"
        assert binding["boardId"] == "" and binding["submissionId"] == ""
        assert binding["resume"] is False
        for bad in (
            {"mode": "normal", "board_id": BOARD},
            {"mode": "normal", "board_id": None, "submission_id": "sub"},
            {"mode": "bogus"},
        ):
            with pytest.raises(ValueError):
                submission.resolve_chat_binding(conn, **bad)
    finally:
        conn.close()


def test_chat_binding_interactive_requires_existing_board(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        with pytest.raises(ValueError):
            submission.resolve_chat_binding(conn, mode="interactive", board_id="board_ghost")
        with pytest.raises(ValueError):
            submission.resolve_chat_binding(conn, mode="interactive", board_id="bad!id")
        make_board(conn)
        binding = submission.resolve_chat_binding(conn, mode="interactive", board_id=BOARD)
        assert binding["mode"] == "interactive"
        assert binding["boardId"] == BOARD
        assert binding["submissionId"] == ""
        assert binding["resume"] is False
    finally:
        conn.close()


def test_chat_binding_auto_resumes_waiting_submission(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        make_board(conn)
        sid = add_submission(conn)
        submission.bind_submission_turn(
            conn,
            submission_id=sid,
            turn_id="turn_first",
            resume_state={"topicId": "topic-original", "kind": "submission"},
        )
        item = submission.record_waiting(
            conn, board_id=BOARD, submission_id=sid, question="哪一张卡片？"
        )
        # 用户下一次普通互动聊天（只带 boardId）→ 自动续作原提交
        binding = submission.resolve_chat_binding(
            conn, mode="interactive", board_id=BOARD, current_topic="topic-current"
        )
        assert binding["submissionId"] == sid
        assert binding["resume"] is True
        assert binding["topicId"] == "topic-original"  # 原话题，不跟界面当前话题
        assert binding["resolvedQuestionIds"] == (item["id"],)
        assert binding["attempt"] == 2  # 已有一次提交轮绑定
        # 显式 submissionId 也按续作处理
        explicit = submission.resolve_chat_binding(
            conn, mode="interactive", board_id=BOARD, submission_id=sid
        )
        assert explicit["submissionId"] == sid and explicit["resume"] is True
        # 提交不属于该板面：拒绝
        make_board(conn, "board_other")
        with pytest.raises(ValueError):
            submission.resolve_chat_binding(
                conn, mode="interactive", board_id="board_other", submission_id=sid
            )
        with pytest.raises(ValueError):
            submission.resolve_chat_binding(
                conn, mode="interactive", board_id=BOARD, submission_id="sub_missing"
            )
    finally:
        conn.close()
