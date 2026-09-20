"""Turn lifecycle protocol.

一个被后端受理的 turn，必须恰好产生一个 TURN_START 和一个 TURN_END；
TURN_END 必须带上真实终态与权威最终回答。任何异常路径都不允许让 turn
停在 running（前端会因此永久处于运行中）。
"""

from __future__ import annotations

import asyncio
import json
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


class _FinalAdapter:
    mode = "native"
    model = "test-model"

    async def complete(self, messages, tools, **kwargs):
        return Completion(message=ChatMessage(role="assistant", content="最终回答"))


class _FailingAdapter:
    mode = "native"
    model = "test-model"

    async def complete(self, messages, tools, **kwargs):
        raise RuntimeError("provider exploded")


class _BlockingAdapter:
    """卡在模型调用里，直到测试放行 —— 用来在「等待模型」时取消。"""

    mode = "native"
    model = "test-model"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, messages, tools, **kwargs):
        self.started.set()
        await self.release.wait()
        return Completion(message=ChatMessage(role="assistant", content="取消后不该被保存"))


def _parse(chunks: list[str]) -> list[dict]:
    events: list[dict] = []
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


async def _run_and_collect(ctx: AppContext, action, timeout: float = 8.0) -> list[dict]:
    collected: list[str] = []

    async def consume():
        async for chunk in ctx.bus.stream():
            collected.append(chunk)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)  # 让订阅先挂上
    await asyncio.wait_for(action(), timeout=timeout)
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return _parse(collected)


def _begin_end(events: list[dict]) -> tuple[list[dict], list[dict]]:
    return (
        [e for e in events if e["type"] == "TURN_START"],
        [e for e in events if e["type"] == "TURN_END"],
    )


async def test_completed_turn_has_exactly_one_turn_end(ctx: AppContext, monkeypatch):
    monkeypatch.setattr(ctx, "build_adapter", AsyncMock(return_value=_FinalAdapter()))
    topic = ctx.topics.nodes.create_topic("完成话题").id

    events = await _run_and_collect(ctx, lambda: ctx.run_turn("你好", topic_id=topic))
    starts, ends = _begin_end(events)

    assert len(starts) == 1
    assert len(ends) == 1
    end = ends[0]["data"]
    assert end["status"] == "completed"
    assert end["final_content"] == "最终回答"
    assert end["error"] is None
    assert end["turn_id"] == starts[0]["data"]["turn_id"]


async def test_model_failure_still_ends_the_turn(ctx: AppContext, monkeypatch):
    monkeypatch.setattr(ctx, "build_adapter", AsyncMock(return_value=_FailingAdapter()))
    topic = ctx.topics.nodes.create_topic("失败话题").id

    events = await _run_and_collect(ctx, lambda: ctx.run_turn("会失败", topic_id=topic))
    ends = _begin_end(events)[1]

    assert len(ends) == 1
    assert ends[0]["data"]["status"] == "failed"
    assert ends[0]["data"]["final_content"] is None
    assert ends[0]["data"]["error"]
    # ERROR 只是「出错了」，turn 的结束只认 TURN_END；ERROR 也必须带 turn_id
    errors = [e for e in events if e["type"] == "ERROR"]
    assert errors
    assert errors[-1]["data"]["turn_id"] == ends[0]["data"]["turn_id"]


async def test_no_credential_ends_as_unavailable(ctx: AppContext):
    """没有任何可用凭据时：不发 TURN_START 之后就没有下文，必须给出终态。"""
    topic = ctx.topics.nodes.create_topic("无凭据话题").id

    events = await _run_and_collect(ctx, lambda: ctx.run_turn("有人吗", topic_id=topic))
    starts, ends = _begin_end(events)

    assert len(starts) == 1
    assert len(ends) == 1
    assert ends[0]["data"]["status"] == "unavailable"
    assert any(e["type"] == "WARNING" for e in events)


async def test_exception_outside_loop_still_ends_the_turn(ctx: AppContext, monkeypatch):
    """收尾阶段（例如上下文组装）抛错时也要有且只有一个 TURN_END。"""
    monkeypatch.setattr(ctx, "build_adapter", AsyncMock(return_value=_FinalAdapter()))
    topic = ctx.topics.nodes.create_topic("异常话题").id

    async def boom(turn_ctx, adapter):
        raise RuntimeError("context build exploded")

    monkeypatch.setattr(ctx.turn_orchestrator, "build_context", boom)

    events = await _run_and_collect(ctx, lambda: ctx.run_turn("炸", topic_id=topic))
    starts, ends = _begin_end(events)

    assert len(starts) == 1
    assert len(ends) == 1
    assert ends[0]["data"]["status"] == "failed"


async def test_cancel_stops_the_turn_and_does_not_persist_final(ctx: AppContext, monkeypatch):
    adapter = _BlockingAdapter()
    monkeypatch.setattr(ctx, "build_adapter", AsyncMock(return_value=adapter))
    topic = ctx.topics.nodes.create_topic("取消话题").id

    collected: list[str] = []

    async def consume():
        async for chunk in ctx.bus.stream():
            collected.append(chunk)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)

    run = asyncio.create_task(ctx.run_turn("取消我", topic_id=topic))
    await asyncio.wait_for(adapter.started.wait(), timeout=5)
    assert ctx.turns.cancel_active() is True
    adapter.release.set()  # 模型请求无法物理中断：返回值必须被丢弃
    result = await asyncio.wait_for(run, timeout=8)
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    ends = [e for e in _parse(collected) if e["type"] == "TURN_END"]
    assert len(ends) == 1
    assert ends[0]["data"]["status"] == "cancelled"
    assert ends[0]["data"]["final_content"] is None
    assert result == {"ok": False, "reason": "cancelled"}

    rows = ctx.conn.execute("SELECT role, content FROM messages").fetchall()
    assert [r["role"] for r in rows] == ["user"], "取消后不得保存助手最终回答"
