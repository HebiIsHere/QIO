# -*- coding: utf-8 -*-
"""SSE 事件总线的有界缓冲（背压）。

慢订阅者（卡住的 SSE 连接）不能把服务端内存拖爆；同时，可合并的累计型事件
（ASSISTANT / USAGE）可以在同一 turn 内只保留最新一条，而 turn 生命周期事件
（TURN_START / TURN_END …）必须优先保留 —— 丢了它前端会永远停在错误的 turn 状态。
"""

from __future__ import annotations

import asyncio

from agent.api.bus import EventBus
from agent.api.events import EventType, make_event


def _type_of(chunk: str) -> str:
    for line in chunk.splitlines():
        if line.startswith("event: "):
            return line[len("event: ") :]
    return ""


async def _slow_subscriber(bus: EventBus):
    """注册一个订阅者并只取走第一条事件，此后停止消费（模拟卡住的连接）。"""
    agen = bus.stream()
    first = asyncio.create_task(agen.__anext__())
    await asyncio.sleep(0)  # 让生成器跑起来并注册到总线
    await bus.publish(make_event(EventType.WARNING, {"warmup": True}))
    assert await first
    return agen


async def _drain(agen, timeout: float = 0.05) -> list[str]:
    """排空订阅者缓冲区：直到超时没有新事件为止。"""
    out: list[str] = []
    while True:
        try:
            out.append(await asyncio.wait_for(agen.__anext__(), timeout))
        except (asyncio.TimeoutError, StopAsyncIteration):
            return out


async def test_slow_subscriber_does_not_grow_unbounded():
    bus = EventBus(queue_limit=8)
    agen = await _slow_subscriber(bus)

    for i in range(200):
        await bus.publish(make_event(EventType.WARNING, {"n": i}))

    chunks = await _drain(agen)
    assert 0 < len(chunks) <= 8, f"慢订阅者缓冲无界：{len(chunks)} 条"
    assert '"n":199' in chunks[-1]  # 保留的是最新事件


async def test_same_turn_assistant_and_usage_keep_only_latest():
    """同一 turn 的 ASSISTANT / USAGE 是累计状态：只保留最新一条即可等价表达。"""
    bus = EventBus(queue_limit=4)
    agen = await _slow_subscriber(bus)

    for i in range(20):
        await bus.publish(
            make_event(EventType.ASSISTANT, {"turn_id": "t1", "content": f"a{i}"})
        )
        await bus.publish(
            make_event(EventType.ASSISTANT, {"turn_id": "t2", "content": f"b{i}"})
        )
    await bus.publish(make_event(EventType.USAGE, {"turn_id": "t1", "tokens": 1}))
    await bus.publish(make_event(EventType.USAGE, {"turn_id": "t1", "tokens": 2}))

    chunks = await _drain(agen)
    assistants = [c for c in chunks if _type_of(c) == "ASSISTANT"]
    usages = [c for c in chunks if _type_of(c) == "USAGE"]

    assert len(assistants) == 2, assistants  # 每个 turn 只留一条
    assert '"content":"a19"' in assistants[0]
    assert '"content":"b19"' in assistants[1]
    assert len(usages) == 1 and '"tokens":2' in usages[0]


async def test_turn_lifecycle_events_survive_backpressure():
    """累计型事件不得把 turn 生命周期事件挤出缓冲区。"""
    bus = EventBus(queue_limit=3)
    agen = await _slow_subscriber(bus)

    await bus.publish(make_event(EventType.TURN_START, {"turn_id": "t1"}))
    for i in range(50):
        await bus.publish(
            make_event(EventType.ASSISTANT, {"turn_id": "t1", "content": f"c{i}"})
        )
        await bus.publish(make_event(EventType.USAGE, {"turn_id": "t1", "tokens": i}))
    await bus.publish(
        make_event(EventType.TURN_END, {"turn_id": "t1", "status": "completed"})
    )

    chunks = await _drain(agen)
    types = [_type_of(c) for c in chunks]

    assert len(chunks) <= 3
    assert "TURN_START" in types and "TURN_END" in types, types
    assert types.index("TURN_START") < types.index("TURN_END")
