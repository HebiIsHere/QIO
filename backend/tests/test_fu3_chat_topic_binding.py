# -*- coding: utf-8 -*-
"""FU3-A 补充：普通互动聊天续作的话题归属。

与确认/恢复/重试同一规则：续作 Turn 的话题只认原提交保存的
binding.resumeState.topicId；界面当前话题不得覆盖；原提交话题无法核对时
明确拒绝这次发送，不静默改绑、不写入当前话题。

红证据（基线 373fda40）：无 binding/无 topicId 时基线回退 current_topic，
本文件对应断言为红；修复后转绿。
"""
from __future__ import annotations

import pytest

from agent.interactive import submission

from test_p3_a_support import BOARD, add_submission, make_board, migrated_conn


def _resume_ready(conn, *, topic: str | None = "topic-original") -> str:
    """建一个等待中的提交（可选已保存的原话题），用于自动续作。"""
    make_board(conn)
    sid = add_submission(conn)
    if topic is not None:
        submission.bind_submission_turn(
            conn,
            submission_id=sid,
            turn_id="turn_first",
            resume_state={"kind": "submission", "topicId": topic},
        )
    submission.record_waiting(
        conn, board_id=BOARD, submission_id=sid, question="这次改动涉及哪张卡片？"
    )
    return sid


def test_new_interactive_message_uses_current_topic(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        make_board(conn)
        binding = submission.resolve_chat_binding(
            conn, mode="interactive", board_id=BOARD, current_topic="topic-current"
        )
        assert binding["submissionId"] == ""
        assert binding["resume"] is False
        assert binding["topicId"] == "topic-current"  # 新消息不是续作：写当前话题
    finally:
        conn.close()


def test_auto_resume_uses_saved_topic_not_current(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        sid = _resume_ready(conn, topic="topic-original")
        binding = submission.resolve_chat_binding(
            conn, mode="interactive", board_id=BOARD, current_topic="topic-current"
        )
        assert binding["submissionId"] == sid
        assert binding["resume"] is True
        assert binding["topicId"] == "topic-original"  # 原提交话题，不跟界面当前话题
    finally:
        conn.close()


def test_explicit_submission_uses_saved_topic(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        sid = _resume_ready(conn, topic="topic-original")
        binding = submission.resolve_chat_binding(
            conn,
            mode="interactive",
            board_id=BOARD,
            submission_id=sid,
            current_topic="topic-current",
        )
        assert binding["topicId"] == "topic-original"
    finally:
        conn.close()


def test_missing_saved_topic_is_refused_not_rebound(tmp_path):
    """原提交存在但没保存话题：明确拒绝，不能拿当前话题顶替。"""
    conn = migrated_conn(tmp_path)
    try:
        sid = _resume_ready(conn, topic=None)  # 有等待提交但没有绑定/topicId
        with pytest.raises(ValueError, match="原提交话题无法确认"):
            submission.resolve_chat_binding(
                conn, mode="interactive", board_id=BOARD, current_topic="topic-current"
            )
        # 显式 submissionId 同样拒绝（不能静默改绑）
        with pytest.raises(ValueError, match="原提交话题无法确认"):
            submission.resolve_chat_binding(
                conn,
                mode="interactive",
                board_id=BOARD,
                submission_id=sid,
                current_topic="topic-current",
            )
    finally:
        conn.close()


def test_binding_exists_but_topic_empty_is_refused(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        make_board(conn)
        sid = add_submission(conn)
        submission.bind_submission_turn(
            conn,
            submission_id=sid,
            turn_id="turn_first",
            resume_state={"kind": "submission"},  # 有绑定但没 topicId
        )
        submission.record_waiting(
            conn, board_id=BOARD, submission_id=sid, question="哪张卡片？"
        )
        with pytest.raises(ValueError, match="原提交话题无法确认"):
            submission.resolve_chat_binding(
                conn, mode="interactive", board_id=BOARD, current_topic="topic-current"
            )
    finally:
        conn.close()
