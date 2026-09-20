"""封块时 Selector 只做增量更新（不再全量重建），且写入是原子的。"""

from __future__ import annotations

import pytest

from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


class _SummaryAdapter:
    mode = "native"
    model = "fake-model"

    async def complete(self, messages, tools, **kwargs):
        from agent.adapters.base import ChatMessage, Completion

        return Completion(
            message=ChatMessage(
                role="assistant",
                content='{"title":"饮食","summary":"用户偏好清淡饮食","entities":[],"keywords":["清淡","饮食"]}',
            )
        )


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


async def _seed_topic(ctx: AppContext, name: str = "话题") -> str:
    topic = ctx.topics.nodes.create_topic(name).id
    for i in range(3):
        ctx.memory.append_message(
            topic_id=topic, role="user", content=f"第{i}条：用户偏好清淡饮食"
        )
    return topic


async def test_close_fragment_updates_selector_incrementally(ctx, monkeypatch):
    topic = await _seed_topic(ctx)
    ctx._refresh_selector()  # 启动后的全量基线

    loads: list[int] = []
    real_load = ctx.selector.load

    def counting_load(*args, **kwargs):
        loads.append(1)
        return real_load(*args, **kwargs)

    monkeypatch.setattr(ctx.selector, "load", counting_load)

    closed = await ctx._close_fragment(topic, _SummaryAdapter())
    assert closed is not None

    # 关键断言：封块只允许增量 upsert，不允许全量重建 selector 索引
    assert loads == [], f"封块触发了 {len(loads)} 次全量重建"

    # 增量之后，新片段必须真的能被检索到
    hits = ctx.selector.select("清淡 饮食", top_k=3)
    assert hits, "增量更新之后新记忆必须可检索"


async def test_close_fragment_rolls_back_when_index_write_fails(ctx, monkeypatch):
    """多步写入必须原子：索引写失败不能留下「已关闭但没有记忆」的半截状态。"""
    topic = await _seed_topic(ctx)

    def boom(**kwargs):
        raise RuntimeError("index exploded")

    monkeypatch.setattr(ctx.index_builder, "build", boom)

    with pytest.raises(RuntimeError):
        await ctx._close_fragment(topic, _SummaryAdapter())

    row = ctx.conn.execute(
        "SELECT closed_at FROM fragments WHERE topic_id = ?", (topic,)
    ).fetchone()
    # 整体回滚：fragment 不能被标成已关闭
    assert row is not None and row["closed_at"] is None
    assert ctx.conn.execute("SELECT COUNT(*) c FROM memory_index").fetchone()["c"] == 0
