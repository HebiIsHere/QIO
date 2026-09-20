"""CAPABILITY / FALLBACK / CREDENTIAL_STATUS 生产者（第三阶段 spec 第 39~46 条）。

三者的分工：

* `CAPABILITY` 是适配档位的状态（native / text / unsupported），正常状态不该刷屏；
* `FALLBACK` 只在**真的降级**那一次提示用户（文本兼容模式）；
* `CREDENTIAL_STATUS` 说明「为什么现在用不了」，只带行为语言，不带内部标识。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent.adapters.base import AdapterMode
from agent.api.bus import EventBus
from agent.config import Settings
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


class FakeAdapter:
    """模型适配器里 AppContext 只需要 mode / model。"""

    def __init__(self, mode: AdapterMode, model: str = "fake-model") -> None:
        self.mode = mode
        self.model = model


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    return AppContext(Settings(data_dir=tmp_path), conn, EventBus())


async def _collect(ctx: AppContext, names: set[str]):
    collected: list[dict] = []

    async def consumer() -> None:
        async for chunk in ctx.bus.stream():
            for line in chunk.splitlines():
                if line.startswith("data: "):
                    evt = json.loads(line[6:])
                    if evt["type"] in names:
                        collected.append(evt)

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.02)

    async def finish() -> None:
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    return collected, finish


async def test_native_adapter_announces_capability_without_fallback(ctx: AppContext):
    collected, finish = await _collect(ctx, {"CAPABILITY", "FALLBACK"})
    await ctx.announce_capability(FakeAdapter(AdapterMode.NATIVE), turn_id="t1")
    await finish()

    assert [e["type"] for e in collected] == ["CAPABILITY"]
    assert collected[0]["data"]["adapter"] == "native"
    assert collected[0]["data"]["model"] == "fake-model"
    assert collected[0]["data"]["turn_id"] == "t1"


async def test_text_adapter_falls_back_once(ctx: AppContext):
    collected, finish = await _collect(ctx, {"CAPABILITY", "FALLBACK"})
    await ctx.announce_capability(FakeAdapter(AdapterMode.TEXT), turn_id="t1")
    await ctx.announce_capability(FakeAdapter(AdapterMode.TEXT), turn_id="t2")
    await finish()

    assert [e["type"] for e in collected] == ["CAPABILITY", "FALLBACK"]
    fallback = collected[1]["data"]
    assert fallback["from"] == "native"
    assert fallback["to"] == "text"
    assert fallback["reason"] == "model_without_native_tool_calls"
    assert "不支持原生工具调用" in fallback["message"]


async def test_same_mode_is_not_renounced(ctx: AppContext):
    """模式没变就什么都不发：正常状态不在事件流里刷存在感。"""
    collected, finish = await _collect(ctx, {"CAPABILITY", "FALLBACK"})
    await ctx.announce_capability(FakeAdapter(AdapterMode.NATIVE), turn_id="t1")
    await ctx.announce_capability(FakeAdapter(AdapterMode.NATIVE), turn_id="t2")
    await finish()

    assert [e["type"] for e in collected] == ["CAPABILITY"]


async def test_unsupported_adapter_does_not_announce_fallback(ctx: AppContext):
    """unsupported 是启动期拒绝，不是「运行中降级」，不该给降级提示。"""
    collected, finish = await _collect(ctx, {"CAPABILITY", "FALLBACK"})
    await ctx.announce_capability(FakeAdapter(AdapterMode.UNSUPPORTED), turn_id="t1")
    await finish()

    assert [e["type"] for e in collected] == ["CAPABILITY"]


async def test_credential_unavailable_speaks_human_without_internal_ids(ctx: AppContext):
    collected, finish = await _collect(ctx, {"CREDENTIAL_STATUS"})
    await ctx.announce_credential_unavailable(turn_id="t9")
    await finish()

    assert len(collected) == 1
    data = collected[0]["data"]
    assert data["status"] == "unavailable"
    assert data["scope"] == "main_loop"
    assert data["reason_code"] == "no_credential"
    assert data["turn_id"] == "t9"
    assert "设置" in data["message"]
    # 内部标识不进用户可见载荷
    assert "key_id" not in data
    assert "key_" not in json.dumps(data, ensure_ascii=False)
