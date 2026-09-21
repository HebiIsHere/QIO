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


async def test_index_failure_does_not_undo_the_seal_and_is_retryable(ctx, monkeypatch):
    """阶段 2：索引写失败不再回滚「封存」，而是变成可重试的派生任务。

    旧行为是「封存 + 写索引」绑成一个事务：索引写不进去就连封存一起回滚。
    新契约把两者分开 —— 封存是对话状态（立刻、持久），索引是派生数据（可重试）。
    代价与保障要同时成立：

    * 封存不会因为一次索引失败而回退（对话不卡在「还没封」的半截状态）；
    * 失败被记成 retryable，失败修好之后再跑一次就能补齐索引（幂等）；
    * 期间原文仍然可读（片段不是「不存在」）。
    """
    from agent.services import derived_tasks

    topic = await _seed_topic(ctx)

    def boom(**kwargs):
        raise RuntimeError("index exploded")

    monkeypatch.setattr(ctx.index_builder, "build", boom)

    closed = await ctx._close_fragment(topic, _SummaryAdapter())

    assert closed is not None
    row = ctx.conn.execute(
        "SELECT closed_at, content_version FROM fragments WHERE topic_id = ?", (topic,)
    ).fetchone()
    assert row["closed_at"] is not None, "封存必须留下"
    assert int(row["content_version"]) > 0, "封存时固定内容版本，供派生任务校验"
    assert ctx.conn.execute("SELECT COUNT(*) c FROM memory_index").fetchone()["c"] == 0

    task = derived_tasks.task_for(
        ctx.conn, derived_tasks.KIND_SUMMARY, closed.id, int(row["content_version"])
    )
    assert task is not None
    assert task.state == derived_tasks.STATE_FAILED
    assert task.attempts == 1
    assert "index exploded" in (task.last_error or "")
    # 原文仍然可读：片段不是「不存在」
    assert ctx.fragments.messages(closed.id), "封存后原文必须仍然读得到"

    # 把故障修好，再跑一次：索引补齐，任务完成（幂等）
    monkeypatch.undo()
    ctx.conn.execute(
        "UPDATE derived_tasks SET run_after = NULL WHERE id = ?", (task.id,)
    )
    done = await ctx.memory_lifecycle.drain_derived_tasks(_SummaryAdapter(), limit=5)
    assert done == 1
    assert ctx.conn.execute("SELECT COUNT(*) c FROM memory_index").fetchone()["c"] == 1
    assert derived_tasks.task_for(
        ctx.conn, derived_tasks.KIND_SUMMARY, closed.id, int(row["content_version"])
    ).state == derived_tasks.STATE_COMPLETED
