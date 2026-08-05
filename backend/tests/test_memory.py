from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from agent.memory.fragment import FragmentManager
from agent.memory.index import (
    IndexBuilder,
    estimate_tokens,
    extract_keywords,
    match_entity_ids,
)
from agent.memory.ingest import MemoryWriter
from agent.memory.summary import (
    FragmentSummary,
    summarize_fragment,
    validate_summary_text,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def topic(db_conn: sqlite3.Connection) -> str:
    db_conn.execute(
        "INSERT INTO nodes (id, type, name, created_at, updated_at) "
        "VALUES ('t1', 'topic', '饮食', ?, ?)",
        (_now(), _now()),
    )
    db_conn.execute(
        "INSERT INTO nodes (id, type, name, created_at, updated_at) "
        "VALUES ('e1', 'entity', '牛奶', ?, ?)",
        (_now(), _now()),
    )
    return "t1"


def test_fragment_open_and_close(db_conn: sqlite3.Connection, topic: str):
    fm = FragmentManager(db_conn)
    frag = fm.get_or_create_open(topic)
    assert frag.topic_id == topic
    assert frag.closed_at is None
    # same open fragment returned
    assert fm.get_or_create_open(topic).id == frag.id
    fm.close(frag.id, "summary text", summary_model="m1")
    closed = fm.get(frag.id)
    assert closed.closed_at is not None
    assert closed.summary == "summary text"
    assert closed.summary_model == "m1"


def test_append_message_and_chunk_close(db_conn: sqlite3.Connection, topic: str):
    fm = FragmentManager(db_conn, max_messages=3)
    writer = MemoryWriter(db_conn, fm)
    closed_seen = None
    for i in range(3):
        _, closed = writer.append_message(
            topic_id=topic, role="user", content=f"消息 {i}"
        )
        if closed is not None:
            closed_seen = closed
    # 3 messages with threshold 3 -> append signals the chunk close
    assert closed_seen is not None
    closed_id = closed_seen.id
    # two-step close: caller runs the summarizer at turn end
    closed = writer.close_open_fragment(topic, lambda f, m: "摘要")
    assert closed.id == closed_id
    assert closed.closed_at is not None
    # next append lands in a fresh fragment
    _, _ = writer.append_message(topic_id=topic, role="user", content="消息 3")
    open_frag = fm.get_or_create_open(topic)
    assert open_frag.id != closed_id
    assert fm.message_count(closed_id) == 3
    assert fm.message_count(open_frag.id) == 1


def test_close_open_fragment_with_summarizer(db_conn: sqlite3.Connection, topic: str):
    fm = FragmentManager(db_conn, max_messages=100)
    writer = MemoryWriter(db_conn, fm)
    writer.append_message(topic_id=topic, role="user", content="我喜欢清淡饮食")
    writer.append_message(topic_id=topic, role="assistant", content="记住了")

    def summarizer(fragment, messages):
        return "用户偏好清淡饮食"

    closed = writer.close_open_fragment(topic, summarizer, summary_model="m1")
    assert closed.closed_at is not None
    assert closed.summary == "用户偏好清淡饮食"
    assert closed.summary_model == "m1"
    assert closed.summary_version == 1


def test_close_open_fragment_degraded_when_summarizer_fails(db_conn: sqlite3.Connection, topic: str):
    fm = FragmentManager(db_conn, max_messages=100)
    writer = MemoryWriter(db_conn, fm)
    writer.append_message(topic_id=topic, role="user", content="内容")

    def failing(fragment, messages):
        return None

    closed = writer.close_open_fragment(topic, failing)
    assert closed.closed_at is not None
    assert closed.summary == ""
    assert closed.summary_version == 0


def test_validate_summary_ok():
    summary, error = validate_summary_text(
        '```json\n{"title": "饮食偏好", "summary": "用户喜欢清淡", '
        '"entities": ["牛奶"], "keywords": ["清淡", "饮食"]}\n```'
    )
    assert error is None
    assert summary is not None
    assert summary.title == "饮食偏好"
    assert summary.entities == ["牛奶"]


def test_validate_summary_rejects_bad_shapes():
    _, error = validate_summary_text("not json at all")
    assert error is not None
    _, error = validate_summary_text('{"title": "x"}')  # missing summary
    assert error is not None
    _, error = validate_summary_text('{"title": "", "summary": ""}')  # empty fields
    assert error is not None


def test_summarize_fragment_success():
    class FakeAdapter:
        mode = "native"

        async def complete(self, messages, tools, **kwargs):
            from agent.adapters.base import ChatMessage, Completion

            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content='{"title": "t", "summary": "s", "entities": [], "keywords": []}',
                )
            )

    import asyncio

    summary, error = asyncio.run(
        summarize_fragment(
            FakeAdapter(),
            [{"role": "user", "content": "hello"}],
        )
    )
    assert error is None
    assert summary is not None and summary.title == "t"


def test_summarize_fragment_degrades_on_model_failure():
    class BrokenAdapter:
        mode = "native"

        async def complete(self, messages, tools, **kwargs):
            raise RuntimeError("boom")

    import asyncio

    summary, error = asyncio.run(
        summarize_fragment(BrokenAdapter(), [{"role": "user", "content": "x"}])
    )
    assert summary is None
    assert error is not None


def test_index_builder_writes_mechanical_index(db_conn: sqlite3.Connection, topic: str):
    fm = FragmentManager(db_conn)
    frag = fm.get_or_create_open(topic)
    fm.close(frag.id, "摘要", summary_model="m1")
    builder = IndexBuilder(db_conn)
    entry = builder.build(
        fragment_id=frag.id,
        topic_id=topic,
        title="饮食偏好",
        summary_text="用户偏好清淡饮食，喜欢牛奶",
        entities=["牛奶"],
        keywords=["清淡", "饮食"],
        message_texts=["用户说喜欢清淡"],
    )
    assert entry["title"] == "饮食偏好"
    assert entry["entity_ids"] == ["e1"]
    assert "清淡" in entry["keywords"]
    assert entry["token_estimate"] > 0
    row = db_conn.execute(
        "SELECT * FROM memory_index WHERE fragment_id = ?", (frag.id,)
    ).fetchone()
    assert row is not None
    assert json.loads(row["entity_ids"]) == ["e1"]


def test_extract_keywords_and_tokens():
    keywords = extract_keywords(["用户喜欢清淡饮食", "用户不喜欢辣", "清淡饮食很重要"], top_n=5)
    assert "清淡" in keywords
    assert estimate_tokens("hello world") > 0


def test_match_entity_ids_only_existing(db_conn: sqlite3.Connection, topic: str):
    ids = match_entity_ids(db_conn, ["牛奶", "不存在的实体"])
    assert ids == ["e1"]