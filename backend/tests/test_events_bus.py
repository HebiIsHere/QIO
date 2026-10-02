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
    """并行分发必须明显快于串行 —— 用**同一台机器上的串行基线**比较，不用绝对墙钟。

    原来的写法是 `assert dt < 0.09`（两个 0.05s 的 handler）：机器一忙（本轮实测：同时 6 个
    pytest + PyInstaller）就抖红过一次，而它并不是要证明「这台机器有多快」，要证明的是
    「parallel 明显快于 serial」。所以先量一次串行基线，再比相对值；两边都取 3 次里的最小值，
    负载尖峰只会抬高单次测量，最小值接近没被抢 CPU 时的真实成本。

    区分度没有被削弱：如果 parallel 其实是顺序执行，两个数字会几乎相等 → 断言仍然红。
    """

    async def slow():
        await asyncio.sleep(0.05)

    bus.on("p", slow)
    bus.on("p", slow)

    def now() -> float:
        return asyncio.get_event_loop().time()

    serials: list[float] = []
    parallels: list[float] = []
    for _ in range(3):
        start = now()
        await slow()
        await slow()
        serials.append(now() - start)

        start = now()
        await bus.parallel("p")
        parallels.append(now() - start)

    serial, parallel = min(serials), min(parallels)
    assert parallel < serial * 0.75, (
        f"并行没有明显快于串行：parallel={parallel:.4f}s serial={serial:.4f}s"
        f"（比值 {parallel / serial:.2f}，要求 < 0.75）"
    )


async def test_disposer_removes_listener(bus):
    calls = []
    def h():
        calls.append(1)
    dispose = bus.on("evt", h)
    await bus.emit("evt")
    dispose()
    await bus.emit("evt")
    assert calls == [1]
