"""Topic Detail 的信息完整性（第三阶段 spec 第 53~56 条）。

详情要够用户看懂「这个长期话题是什么、最近发生了什么」：一句摘要、关键词、
最近活动、真实消息数、片段清单——但**不内联 Message 原文**（原文属于第三层，
按需分页读取）。字段全部来自真实数据，没有就不显示，不编造。
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _seed(client, name: str = "记忆浏览") -> str:
    """两个片段：老片段 3 条消息，新片段 2 条消息，各自带机械索引关键词。"""
    ctx = client.app.state.ctx
    topic = ctx.topics.nodes.create_topic(name)
    plan = [
        (
            "f0",
            "第一段摘要。后面还有别的句子。",
            ["星球", "记忆"],
            "2026-09-01T00:00:00+00:00",
            "2026-09-02",
            3,
        ),
        (
            "f1",
            "最近一段摘要。别的句子。",
            ["记忆", "锚点"],
            "2026-09-02T00:00:00+00:00",
            "2026-09-03",
            2,
        ),
    ]
    for suffix, summary, keywords, created_at, message_day, count in plan:
        frag_id = f"{topic.id}-{suffix}"
        ctx.conn.execute(
            "INSERT INTO fragments (id, topic_id, summary, summary_version, created_at, closed_at) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            (frag_id, topic.id, summary, created_at, created_at),
        )
        ctx.conn.execute(
            "INSERT INTO memory_index (id, fragment_id, topic_id, entity_ids, keywords, title, "
            "token_estimate, created_at) VALUES (?, ?, ?, '[]', ?, ?, 10, ?)",
            (
                f"{frag_id}-idx",
                frag_id,
                topic.id,
                json.dumps(keywords, ensure_ascii=False),
                summary,
                created_at,
            ),
        )
        for i in range(count):
            ctx.conn.execute(
                "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
                "VALUES (?, ?, 'user', ?, 'text', ?)",
                (
                    f"{frag_id}-m{i}",
                    frag_id,
                    f"原文标记-{suffix}-{i}",
                    f"{message_day}T10:0{i}:00+00:00",
                ),
            )
    ctx.conn.commit()
    return topic.id


def test_topic_detail_carries_summary_keywords_activity_and_true_count(client):
    topic_id = _seed(client)

    detail = client.get(f"/api/graph/topics/{topic_id}").json()

    assert detail["topic_id"] == topic_id
    assert detail["name"] == "记忆浏览"
    # 摘要取最近一个片段的**首句**，不是整段
    assert detail["summary"] == "最近一段摘要。"
    # 关键词来自机械索引，去重后按出现次数排序
    assert detail["keywords"][0] == "记忆"
    assert set(detail["keywords"]) == {"记忆", "星球", "锚点"}
    assert len(detail["keywords"]) == len(set(detail["keywords"]))
    # 最近活动是真实的最新消息时间
    assert detail["last_activity"] == "2026-09-03T10:01:00+00:00"
    # 话题总消息数是真实计数，不是片段数
    assert detail["message_count"] == 5


def test_topic_detail_keeps_fragments_lightweight(client):
    topic_id = _seed(client)

    detail = client.get(f"/api/graph/topics/{topic_id}").json()

    assert [f["message_count"] for f in detail["fragments"]] == [3, 2]
    for fragment in detail["fragments"]:
        assert "messages" not in fragment
    assert "原文标记" not in json.dumps(detail, ensure_ascii=False)
    assert set(detail) >= {"topic_id", "name", "fragments", "entities", "knowledge"}


def test_topic_detail_without_summary_does_not_invent_one(client):
    ctx = client.app.state.ctx
    topic = ctx.topics.nodes.create_topic("空话题")

    detail = client.get(f"/api/graph/topics/{topic.id}").json()

    assert detail["summary"] is None
    assert detail["keywords"] == []
    assert detail["message_count"] == 0
    assert detail["last_activity"] is None


def test_unknown_topic_is_404(client):
    assert client.get("/api/graph/topics/topic_missing").status_code == 404
