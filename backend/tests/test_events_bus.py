# -*- coding: utf-8 -*-
"""内部事件总线：emit/waterfall/parallel/serial 四种分发。"""
import asyncio

import pytest

from agent.core.events_bus import InternalEventBus


@pytest.fixture()
def bus():
    return InternalEventBus()


async def test_emit_calls_all_listeners_in_order(bus):
    calls = []
    bus.on("evt", lambda: calls.append("a"))
    bus.on("evt", lambda: calls.append("b"))
    await bus.emit("evt")
    assert calls == ["a", "b"]


async def test_emit_with_args(bus):
    seen = []
    async def h(x):
        seen.append(x)
    bus.on("evt", h)
    await bus.emit("evt", 42)
    assert seen == [42]


async def test_waterfall_passes_value_and_next(bus):
    async def add_one(v, *, next):
        return await next(v + 1)
    async def double(v, *, next):
        return await next(v * 2)
    bus.on("wf", add_one)
    bus.on("wf", double)
    result = await bus.waterfall("wf", 10)
    assert result == 22  # (10+1)*2


async def test_waterfall_short_circuit(bus):
    async def intercept(v, *, next):
        return "blocked"  # 不调 next → 短路
    async def never(v, *, next):
        raise AssertionError("should not run")
    bus.on("wf", intercept)
    bus.on("wf", never)
    assert await bus.waterfall("wf", 10) == "blocked"


async def test_serial_passes_value_in_order(bus):
    bus.on("s", lambda v: v + 1)
    bus.on("s", lambda v: v * 2)
    assert await bus.serial("s", 5) == 12


async def test_parallel_waits_for_all(bus):
    async def slow():
        await asyncio.sleep(0.05)
    bus.on("p", slow)
    bus.on("p", slow)
    t0 = asyncio.get_event_loop().time()
    await bus.parallel("p")
    dt = asyncio.get_event_loop().time() - t0
    assert dt < 0.09  # 并行而非串行(0.10+)


async def test_disposer_removes_listener(bus):
    calls = []
    def h():
        calls.append(1)
    dispose = bus.on("evt", h)
    await bus.emit("evt")
    dispose()
    await bus.emit("evt")
    assert calls == [1]
