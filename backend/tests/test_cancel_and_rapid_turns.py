"""取消与快速多轮（阶段 4 验收矩阵里剩下的两行）。

* 取消：一轮被取消后不得写出「看起来成功」的回答，位置不推进，绑定要记下取消终态；
* 快速多轮：同一片段里连续几轮只算各自的轮次，不会因为「发得快」被拆成多段。
"""

from __future__ import annotations

import asyncio

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


class _SlowAdapter:
    """回答前先等一会儿：给「取消」留出时间窗。"""

    mode = "native"
    model = "fake-slow"

    def __init__(self, delay: float = 0.3) -> None:
        self.delay = delay

    async def complete(self, messages, tools, **kwargs):
        await asyncio.sleep(self.delay)
        return Completion(message=ChatMessage(role="assistant", content="迟到的回答"))


class _FastAdapter:
    mode = "native"
    model = "fake-fast"

    async def complete(self, messages, tools, **kwargs):
        return Completion(message=ChatMessage(role="assistant", content="收到"))


class _AbortAwareSlowAdapter:
    """慢回答，并记录这次请求到底有没有被中断。"""

    mode = "native"
    model = "fake-abortable"

    def __init__(self, delay: float = 15.0) -> None:
        self.delay = delay
        self.started = False
        self.aborted = False

    async def complete(self, messages, tools, **kwargs):
        self.started = True
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.aborted = True
            raise
        return Completion(message=ChatMessage(role="assistant", content="迟到的回答"))


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "cancel.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


def _topic(ctx: AppContext, name: str = "话题") -> str:
    return ctx.topics.nodes.create_topic(name).id


def _assistant_messages(ctx: AppContext, topic: str) -> list[str]:
    return [
        row["content"]
        for row in ctx.conn.execute(
            "SELECT m.content FROM messages m JOIN fragments f ON f.id = m.fragment_id "
            "WHERE f.topic_id = ? AND m.role = 'assistant'",
            (topic,),
        )
    ]


async def test_cancelled_turn_writes_nothing_and_does_not_advance(ctx: AppContext):
    from unittest.mock import AsyncMock

    topic = _topic(ctx)
    ctx.memory.append_message(topic_id=topic, role="user", content="上一轮")
    ctx.memory.append_message(topic_id=topic, role="assistant", content="上一轮回答")
    before = ctx.fragments.open_fragment(topic).id
    ctx.build_adapter = AsyncMock(return_value=_SlowAdapter())

    tctx = ctx.turns.submit("这一轮会被取消", topic)
    for _ in range(100):
        if ctx.turns.active is not None:
            break
        await asyncio.sleep(0.01)
    cancelled = ctx.turns.cancel_active()
    result = await ctx.turns.wait(tctx.turn_id, timeout=10)

    assert cancelled is True
    assert result is not None and result.get("reason") == "cancelled"
    assert "迟到的回答" not in _assistant_messages(ctx, topic), "取消后不得写回迟到的回答"
    binding = ctx.bindings.binding_for(tctx.turn_id)
    assert binding is not None
    assert binding.status == "cancelled", f"绑定要记下取消终态，实际 {binding.status}"
    assert ctx.fragments.open_fragment(topic).id == before, "片段没有因为取消被切开"


async def test_cancel_aborts_the_in_flight_model_request(ctx: AppContext):
    """按「停止」要掐掉正在等待的模型请求，而不是干等它自然跑完。

    真实抱怨：取消之后请求还在继续跑 —— 继续占用等待时间，也继续占着单飞队列。
    这里要求的不是「结果被丢掉」，而是**这一次请求本身被中断**。
    """
    from unittest.mock import AsyncMock

    topic = _topic(ctx)
    adapter = _AbortAwareSlowAdapter(delay=15.0)
    ctx.build_adapter = AsyncMock(return_value=adapter)

    tctx = ctx.turns.submit("这一轮会被取消", topic)
    for _ in range(300):
        if adapter.started:
            break
        await asyncio.sleep(0.01)
    assert adapter.started, "模型调用没有开始跑，测试前提不成立"

    started = asyncio.get_running_loop().time()
    assert ctx.turns.cancel_active() is True
    result = await ctx.turns.wait(tctx.turn_id, timeout=25)
    elapsed = asyncio.get_running_loop().time() - started

    assert adapter.aborted is True, "在途的模型请求必须被真正中断"
    assert elapsed < 5, f"取消后应当立刻结束，实测 {elapsed:.1f}s"
    assert result is not None and result.get("reason") == "cancelled"
    assert "迟到的回答" not in _assistant_messages(ctx, topic)


async def test_cancel_frees_the_single_flight_queue_immediately(ctx: AppContext):
    """取消之后，排队的下一条消息要立刻开始跑，不能被旧请求继续堵着。"""
    from unittest.mock import AsyncMock

    topic = _topic(ctx)
    slow = _AbortAwareSlowAdapter(delay=15.0)
    ctx.build_adapter = AsyncMock(side_effect=[slow, _FastAdapter()])

    ctx.turns.submit("会被取消的一轮", topic)
    for _ in range(300):
        if slow.started:
            break
        await asyncio.sleep(0.01)
    second = ctx.turns.submit("排在后面的这一轮", topic)

    started = asyncio.get_running_loop().time()
    ctx.turns.cancel_active()
    await ctx.turns.wait(second.turn_id, timeout=25)
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 5, f"取消后排队消息应当立刻开始，实测 {elapsed:.1f}s"
    assert "收到" in _assistant_messages(ctx, topic), "后一条消息要真的跑完"


async def test_rapid_turns_stay_in_one_fragment(ctx: AppContext):
    from unittest.mock import AsyncMock

    topic = _topic(ctx)
    ctx.build_adapter = AsyncMock(return_value=_FastAdapter())

    turns = []
    for text in ("第一轮", "第二轮", "第三轮"):
        tctx = ctx.turns.submit(text, topic)
        await ctx.turns.wait(tctx.turn_id, timeout=10)
        turns.append(tctx.turn_id)

    fragment = ctx.fragments.open_fragment(topic)
    assert fragment is not None
    assert ctx.fragments.turn_count(fragment.id) == 3, "三轮就是三轮，不多不少"
    assert ctx.conn.execute(
        "SELECT COUNT(*) c FROM fragments WHERE topic_id = ?", (topic,)
    ).fetchone()["c"] == 1, "快速多轮不该把片段拆开"
    for turn_id in turns:
        binding = ctx.bindings.binding_for(turn_id)
        assert binding is not None and binding.fragment_id == fragment.id
        assert binding.status == "completed"
