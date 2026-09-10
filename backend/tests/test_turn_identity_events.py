"""turn_id flows through SSE events so TURN_START/TURN_END can be paired."""

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


class _Adapter:
    mode = "native"
    model = "deepseek-v4-flash"

    async def complete(self, messages, tools, **kwargs):
        return Completion(message=ChatMessage(role="assistant", content="好的"))


def _parse(items):
    out = []
    for line in items:
        for part in line.splitlines():
            if part.startswith("data: "):
                out.append(json.loads(part[6:]))
    return out


async def test_turn_start_and_end_share_turn_id(ctx: AppContext):
    node = ctx.topics.nodes.create_topic("t")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ctx, "build_adapter", AsyncMock(return_value=_Adapter()))

    collected: list[str] = []

    async def consume():
        async for chunk in ctx.bus.stream():
            collected.append(chunk)
            if len(_parse(collected)) >= 20:
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)  # let the subscriber attach
    await ctx.run_turn("你好", topic_id=node.id)
    await asyncio.sleep(0.05)
    task.cancel()
    monkeypatch.undo()

    events = _parse(collected)
    starts = [e for e in events if e["type"] == "TURN_START"]
    ends = [e for e in events if e["type"] == "TURN_END"]
    assert starts and ends
    tid = starts[0]["data"].get("turn_id")
    assert tid  # non-empty
    assert ends[0]["data"].get("turn_id") == tid
