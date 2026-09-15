"""Planet 数据接口三层（spec 第 40~44 / 77 条）。

打开星球不得返回全部 Message 原文；选中话题不得默认返回全部片段原文；
只有真正展开某段历史时才按页取原文。
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


def _seed_topic(client, name: str, *, fragments: int = 1, messages_per_fragment: int = 3) -> str:
    ctx = client.app.state.ctx
    node = ctx.topics.nodes.create_topic(name)
    for fi in range(fragments):
        frag_id = f"{node.id}-f{fi}"
        ctx.conn.execute(
            "INSERT INTO fragments (id, topic_id, summary, created_at, closed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                frag_id,
                node.id,
                f"{name} 的第 {fi} 段摘要",
                f"2026-09-0{fi + 1}T00:00:00+00:00",
                f"2026-09-0{fi + 1}T01:00:00+00:00",
            ),
        )
        for mi in range(messages_per_fragment):
            ctx.conn.execute(
                "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
                "VALUES (?, ?, 'user', ?, 'text', ?)",
                (f"{frag_id}-m{mi}", frag_id, f"原文标记-{name}-{fi}-{mi}", f"2026-09-0{fi + 1}T00:0{mi}:00+00:00"),
            )
    ctx.conn.commit()
    return node.id


def test_planet_overview_is_lightweight(client):
    _seed_topic(client, "甲", fragments=2, messages_per_fragment=5)
    _seed_topic(client, "乙", fragments=1, messages_per_fragment=5)

    body = client.get("/api/planet/overview").json()

    assert body["total"] == 2
    assert body["visible_capacity"] == 12
    first = body["topics"][0]
    assert first["title"] == "甲"
    assert first["fragment_count"] == 2
    assert "messages" not in first
    assert "原文标记" not in json.dumps(body, ensure_ascii=False)


def test_topic_detail_does_not_inline_message_bodies(client):
    topic_id = _seed_topic(client, "展开前不该给原文", fragments=2, messages_per_fragment=7)

    detail = client.get(f"/api/graph/topics/{topic_id}").json()

    assert detail["name"] == "展开前不该给原文"
    assert [f["message_count"] for f in detail["fragments"]] == [7, 7]
    for fragment in detail["fragments"]:
        assert "messages" not in fragment
    assert "原文标记" not in json.dumps(detail, ensure_ascii=False)


def test_topic_detail_reports_true_message_count_beyond_any_page_limit(client):
    topic_id = _seed_topic(client, "很多消息", fragments=1, messages_per_fragment=60)

    detail = client.get(f"/api/graph/topics/{topic_id}").json()

    assert detail["fragments"][0]["message_count"] == 60


def test_fragment_raw_is_paginated_and_on_demand(client):
    topic_id = _seed_topic(client, "分页话题", fragments=1, messages_per_fragment=7)
    detail = client.get(f"/api/graph/topics/{topic_id}").json()
    frag_id = detail["fragments"][0]["fragment_id"]

    head = client.get(f"/api/fragments/{frag_id}/messages?offset=0&limit=2").json()
    assert head["total"] == 7
    assert len(head["messages"]) == 2
    assert head["messages"][0]["content"].startswith("原文标记")

    tail = client.get(f"/api/fragments/{frag_id}/messages?offset=6&limit=2").json()
    assert len(tail["messages"]) == 1

    assert client.get("/api/fragments/ghost/messages").status_code == 404


def test_planet_browse_returns_a_next_batch_and_validates_direction(client):
    for i in range(20):
        _seed_topic(client, f"话题{i:02d}")

    resp = client.post("/api/planet/browse", json={"direction": "forward", "count": 5, "seed": 7})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 5
    assert body["total"] == 20
    assert body["next_cursor"]
    assert "原文标记" not in json.dumps(body, ensure_ascii=False)

    bad = client.post("/api/planet/browse", json={"direction": "sideways"})
    assert bad.status_code == 400


def test_planet_browse_backward_reuses_the_previous_cursor(client):
    for i in range(20):
        _seed_topic(client, f"话题{i:02d}")

    first = client.post("/api/planet/browse", json={"direction": "forward", "count": 6, "seed": 7}).json()
    second = client.post(
        "/api/planet/browse", json={"direction": "forward", "count": 6, "cursor": first["next_cursor"]}
    ).json()
    back = client.post(
        "/api/planet/browse", json={"direction": "backward", "count": 6, "cursor": second["prev_cursor"]}
    ).json()

    assert [t["topic_id"] for t in back["items"]] == [t["topic_id"] for t in first["items"]]
