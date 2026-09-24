"""工具调用历史的读取路径：历史接口附带预览、按 id 取全文。

预览随消息一起回来（历史页始终轻），全文只在用户展开某张卡片时才取。
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.storage.tool_records import record_tool_call


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def ctx(client) -> AppContext:
    return client.app.state.ctx


def test_history_page_carries_tool_records(ctx: AppContext):
    topic = ctx.topics.nodes.create_topic("工具历史话题").id
    ctx.memory.append_message(topic_id=topic, role="user", content="帮我查", turn_id="turn_t1")
    ctx.memory.append_message(
        topic_id=topic, role="assistant", content="查完了", turn_id="turn_t1"
    )
    record_tool_call(
        ctx.conn,
        turn_id="turn_t1",
        topic_id=topic,
        call_id="c1",
        seq=1,
        tool_name="fs_list",
        arguments={"path": "."},
        output="很早的输出",
        status="failed",
        error="列目录失败：找不到路径",
        duration_ms=7,
    )

    page = ctx.session_messages_page(topic, limit=50)

    assert [m["role"] for m in page["messages"]] == ["user", "assistant"]
    records = page["tool_records"]
    assert len(records) == 1
    assert records[0]["call_id"] == "c1"
    assert records[0]["status"] == "failed"
    assert records[0]["error"] == "列目录失败：找不到路径"
    assert records[0]["duration_ms"] == 7
    assert records[0]["title"] == "列出目录"     # 中文展示名现算
    assert "output" not in records[0]             # 预览里没有全文
    assert records[0]["output_chars"] == len("很早的输出")
    assert records[0]["preview"] == "很早的输出"


def test_history_page_without_messages_has_no_records(ctx: AppContext):
    topic = ctx.topics.nodes.create_topic("空话题").id
    assert ctx.session_messages_page(topic, limit=50)["tool_records"] == []


def test_records_of_other_topics_are_not_attached(ctx: AppContext):
    """别的轮次的工具记录不能串到这一页里来。"""
    topic = ctx.topics.nodes.create_topic("本话题").id
    ctx.memory.append_message(topic_id=topic, role="user", content="问", turn_id="turn_here")
    record_tool_call(
        ctx.conn, turn_id="turn_elsewhere", call_id="c_other", seq=1,
        tool_name="fs_list", arguments={}, output="别处的输出", status="success",
    )
    assert ctx.session_messages_page(topic, limit=50)["tool_records"] == []


def test_full_record_endpoint_returns_arguments_and_output(client, db_conn):
    record_id = record_tool_call(
        db_conn,
        turn_id="turn_api",
        call_id="c9",
        seq=1,
        tool_name="fs_read",
        arguments={"path": "C:/x.txt"},
        output="文件正文",
        status="success",
    )
    body = client.get(f"/api/tool-records/{record_id}").json()
    assert body["arguments"] == {"path": "C:/x.txt"}
    assert body["output"] == "文件正文"
    assert body["title"] == "读取文件"


def test_full_record_endpoint_reports_cleared_output(client, db_conn):
    record_id = record_tool_call(
        db_conn, turn_id="turn_r", call_id="c_r", seq=1, tool_name="fs_list",
        arguments={}, output="会被清掉的输出", status="success",
    )
    db_conn.execute(
        "UPDATE tool_records SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
        (record_id,),
    )
    from agent.storage.tool_records import prune_outputs

    prune_outputs(db_conn, 90)

    body = client.get(f"/api/tool-records/{record_id}").json()
    assert body["output"] == ""
    assert body["output_missing"] is True
    assert body["missing_reason"] == "retention"


def test_full_record_endpoint_404(client):
    assert client.get("/api/tool-records/tr_missing").status_code == 404


def test_session_context_carries_tool_records(client, ctx: AppContext, db_conn):
    topic = ctx.topics.nodes.create_topic("上下文话题").id
    ctx.memory.append_message(topic_id=topic, role="user", content="问", turn_id="turn_ctx")
    record_tool_call(
        db_conn, turn_id="turn_ctx", call_id="c_ctx", seq=1, tool_name="now",
        arguments={}, output="2026-09-24", status="success",
    )
    ctx.navigation.anchors.set_active(topic)   # 当前话题 = 这一条

    body = client.get("/api/session/context").json()

    assert [r["call_id"] for r in body["tool_records"]] == ["c_ctx"]


def test_session_messages_carries_tool_records(client, ctx: AppContext, db_conn):
    topic = ctx.topics.nodes.create_topic("翻页话题").id
    ctx.memory.append_message(topic_id=topic, role="user", content="问", turn_id="turn_page")
    record_tool_call(
        db_conn, turn_id="turn_page", call_id="c_page", seq=1, tool_name="now",
        arguments={}, output="2026-09-24", status="success",
    )

    body = client.get("/api/session/messages", params={"topic_id": topic}).json()

    assert [r["call_id"] for r in body["tool_records"]] == ["c_page"]
