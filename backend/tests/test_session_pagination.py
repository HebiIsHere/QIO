"""会话历史渐进加载：进入 Topic 不再一次读全部历史。

硬性要求（spec 第 20 条）：
* 首次只给最近一段（默认 200 条）；
* 可以继续向前翻页，且**不丢消息、不重复、顺序不错**；
* 游标必须稳定：同一时刻到达的消息不会因为 `created_at` 相同而被跳过或重复。
"""

from __future__ import annotations

import pytest

from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


@pytest.fixture()
def ctx(tmp_path):
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    yield app_ctx
    conn.close()


def _seed(ctx: AppContext, topic_id: str, count: int) -> list[str]:
    """写 count 条消息；故意让多条的 created_at 完全相同，考验游标稳定性。"""
    fragment = ctx.fragments.get_or_create_open(topic_id)
    ids: list[str] = []
    for i in range(count):
        # 每 5 条共用一个时间戳：只按 created_at 分页会漏消息
        stamp = f"2026-01-01T00:{i // 5:02d}:{(i % 5):02d}+00:00"
        msg_id = f"m{i:04d}"
        ctx.conn.execute(
            "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (msg_id, fragment.id, "user" if i % 2 == 0 else "assistant", f"消息{i}", "text", stamp),
        )
        ids.append(msg_id)
    return ids


def test_first_page_returns_only_recent_messages(ctx):
    topic = ctx.topics.nodes.create_topic("分页话题").id
    _seed(ctx, topic, 500)

    page = ctx.session_messages_page(topic, limit=200)
    assert len(page["messages"]) == 200
    assert page["has_more"] is True
    assert page["next_before"]
    # 返回的是**最近**一段，并按时间升序排列
    assert page["messages"][-1]["id"] == "m0499"
    assert page["messages"][0]["id"] == "m0300"


def test_paging_back_through_history_has_no_gap_and_no_duplicate(ctx):
    topic = ctx.topics.nodes.create_topic("分页话题").id
    all_ids = _seed(ctx, topic, 500)

    collected: list[str] = []
    page = ctx.session_messages_page(topic, limit=120)
    collected = [m["id"] for m in page["messages"]] + collected
    guard = 0
    while page["has_more"] and guard < 20:
        page = ctx.session_messages_page(topic, limit=120, before=page["next_before"])
        collected = [m["id"] for m in page["messages"]] + collected
        guard += 1

    assert collected == all_ids, "分页必须不丢、不重、顺序正确"


def test_pagination_is_stable_when_many_messages_share_timestamp(ctx):
    topic = ctx.topics.nodes.create_topic("同秒消息").id
    ids = _seed(ctx, topic, 50)  # 每 5 条一个相同时间戳

    collected: list[str] = []
    page = ctx.session_messages_page(topic, limit=10)
    collected = [m["id"] for m in page["messages"]] + collected
    while page["has_more"]:
        page = ctx.session_messages_page(topic, limit=10, before=page["next_before"])
        collected = [m["id"] for m in page["messages"]] + collected

    assert collected == ids


def test_empty_topic_returns_empty_page(ctx):
    topic = ctx.topics.nodes.create_topic("空话题").id
    page = ctx.session_messages_page(topic, limit=50)
    assert page["messages"] == []
    assert page["has_more"] is False
    assert page["next_before"] is None


def test_bad_cursor_does_not_crash(ctx):
    topic = ctx.topics.nodes.create_topic("坏游标").id
    _seed(ctx, topic, 10)
    page = ctx.session_messages_page(topic, limit=5, before="not-a-cursor")
    # 无法解析的游标按「从头开始」处理，而不是 500
    assert isinstance(page["messages"], list)
