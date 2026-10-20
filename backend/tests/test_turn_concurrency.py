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
        # 契约 §3.6：当前用户原文是请求里最后一条 user 消息（上下文在 system 侧）；
        # 不再用「【用户消息】」标题拼接，所以按结构化消息角色取当前 query。
        current = ""
        for message in reversed(messages):
            if getattr(message, "role", None) == "user":
                current = message.content or ""
                break
        for tag in ("A", "B", "C"):
            if current.rstrip().endswith(f"唯一{tag}"):
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

    # 1. 执行顺序确定 = 提交顺序。
    #    契约 §1.1 变更：回答由 [[QIO:ANSWER]] 声明，未声明走降级交付 ——
    #    合规的一轮只有 1 次调用 → 每个 tag 各出现一次。
    assert adapter.order == ["A", "B", "C"]
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
