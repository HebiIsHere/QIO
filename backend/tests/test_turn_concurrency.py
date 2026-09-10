"""Concurrency regressions: rapid submits serialize, no runtime overwrite."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


class _SlowAdapter:
    mode = "native"
    model = "deepseek-v4-flash"

    def __init__(self) -> None:
        self.order: list[str] = []
        self.peak = 0
        self._running = 0

    async def complete(self, messages, tools, **kwargs):
        self._running += 1
        self.peak = max(self.peak, self._running)
        content = messages[0].content if messages else ""
        for tag in ("A", "B", "C"):
            # 当前 query 是 prompt 末尾的「用户消息」；历史里也可能含其它 tag
            if (content or "").rstrip().endswith(f"唯一{tag}"):
                self.order.append(tag)
        await asyncio.sleep(0.02)
        self._running -= 1
        return Completion(message=ChatMessage(role="assistant", content="ok"))


async def test_rapid_turns_serialize_in_submit_order(ctx: AppContext):
    node = ctx.topics.nodes.create_topic("t")
    adapter = _SlowAdapter()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ctx, "build_adapter", AsyncMock(return_value=adapter))

    await asyncio.gather(
        ctx.run_turn("唯一A", topic_id=node.id),
        ctx.run_turn("唯一B", topic_id=node.id),
        ctx.run_turn("唯一C", topic_id=node.id),
    )
    monkeypatch.undo()

    # 1. 执行顺序确定 = 提交顺序
    assert adapter.order[:3] == ["A", "B", "C"]
    # 2. 主循环最大并发数 = 1
    assert adapter.peak == 1
    # 3. 结束后无 active turn（无 runtime 残留/覆盖）
    assert ctx.turns.active is None


async def test_queue_holds_pending_turns(ctx: AppContext):
    node = ctx.topics.nodes.create_topic("t")
    started = asyncio.Event()
    release = asyncio.Event()

    class _Blocking:
        mode = "native"
        model = "m"

        async def complete(self, messages, tools, **kwargs):
            started.set()
            await release.wait()
            return Completion(message=ChatMessage(role="assistant", content="ok"))

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ctx, "build_adapter", AsyncMock(return_value=_Blocking()))

    t1 = asyncio.create_task(ctx.run_turn("第一", topic_id=node.id))
    await started.wait()
    t2 = asyncio.create_task(ctx.run_turn("第二", topic_id=node.id))
    await asyncio.sleep(0.02)

    # 第二条仍在队列里，未并发执行、未丢弃
    assert ctx.turns.active is not None
    assert ctx.turns.queued_count() >= 1

    release.set()
    await asyncio.gather(t1, t2)
    monkeypatch.undo()
    assert ctx.turns.active is None
