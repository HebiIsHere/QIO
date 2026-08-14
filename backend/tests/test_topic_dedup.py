# -*- coding: utf-8 -*-
"""create_topic 防重复硬限制 + 二次申请人工审批。"""
import sqlite3
from types import SimpleNamespace

import pytest

from agent.tools.topic_tools import CreateTopicTool


class FakeApprovals:
    def __init__(self, decision: str):
        self.decision = decision
        self.calls = []

    async def request(self, kind, payload):
        self.calls.append((kind, payload))
        return SimpleNamespace(decision=self.decision)


class FakePredictor:
    def __init__(self, scores=None):
        self.scores = scores or {}

    def predict(self, name, current_topic_id=None):
        top = max(self.scores, key=self.scores.get) if self.scores else None
        return SimpleNamespace(
            main_topic_id=top,
            scores=self.scores,
            is_new_topic_candidate=True,
        )


def _topic(db_conn, tid, name):
    now = "2026-08-14T00:00:00+00:00"
    db_conn.execute("INSERT INTO nodes VALUES (?, 'topic', ?, '{}', ?, ?)", (tid, name, now, now))


def test_name_similar_blocks_creation(db_conn: sqlite3.Connection):
    _topic(db_conn, "t_sql", "SQLite学习")
    tool = CreateTopicTool(db_conn, predictor=FakePredictor({}))
    result = __import__("asyncio").run(tool.run(name="SQLite学习2"))
    assert not result.ok
    assert "相似话题" in result.content
    assert "SQLite学习" in result.content


def test_embedding_fuzzy_band_blocks(db_conn: sqlite3.Connection):
    _topic(db_conn, "t_sql", "SQLite数据库设计")
    tool = CreateTopicTool(
        db_conn,
        predictor=FakePredictor({"t_sql": 0.6}),  # ∈ [0.5, 0.7)
    )
    result = __import__("asyncio").run(tool.run(name="SQLite迁移学习"))
    assert not result.ok
    assert "相似度 0.60" in result.content


def test_second_attempt_requires_approval_and_creates_when_approved(db_conn: sqlite3.Connection):
    _topic(db_conn, "t_sql", "SQLite学习")
    approvals = FakeApprovals("approved")
    tool = CreateTopicTool(db_conn, predictor=FakePredictor({}), approvals=approvals)
    first = __import__("asyncio").run(tool.run(name="SQLite学习2"))
    assert not first.ok
    second = __import__("asyncio").run(tool.run(name="SQLite学习2"))
    assert second.ok
    assert approvals.calls and approvals.calls[0][0] == "create_topic"
    assert db_conn.execute("SELECT COUNT(*) FROM nodes WHERE type='topic' AND name='SQLite学习2'").fetchone()[0] == 1


def test_second_attempt_rejected_does_not_create(db_conn: sqlite3.Connection):
    _topic(db_conn, "t_sql", "SQLite学习")
    approvals = FakeApprovals("rejected")
    tool = CreateTopicTool(db_conn, predictor=FakePredictor({}), approvals=approvals)
    __import__("asyncio").run(tool.run(name="SQLite学习2"))  # 首次拦截
    second = __import__("asyncio").run(tool.run(name="SQLite学习2"))
    assert not second.ok
    assert db_conn.execute("SELECT COUNT(*) FROM nodes WHERE type='topic' AND name='SQLite学习2'").fetchone()[0] == 0
