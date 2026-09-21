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
    buffered = [c for c in chunks if _type_of(c) != "RESYNC"]
    assert 0 < len(buffered) <= 8, f"慢订阅者缓冲无界：{len(buffered)} 条"
    assert '"n":199' in buffered[-1]  # 保留的是最新事件
    # 200 条关键事件挤进 8 格缓冲 → 一定丢过关键事件，因此必须明确要求客户端 resync
    assert "RESYNC" in [_type_of(c) for c in chunks]


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


# ---------------------------------------------------------------------------
# 反例：缓冲里**全是关键事件**时的 overflow
#
# 上一轮的策略在这种情况下会直接丢掉最旧的关键事件，而且客户端毫不知情 ——
# 「关键事件被删除，但客户端完全不知道状态流已经不完整」是最危险的情况。
# 正确做法：有界内存 + 明确告知需要 resync，绝不假装事件序列是完整的。
# ---------------------------------------------------------------------------


def _resync_reason(chunks: list[str]) -> str:
    for chunk in chunks:
        if _type_of(chunk) == "RESYNC":
            return chunk
    return ""


async def test_full_buffer_of_critical_events_never_silently_loses_one():
    """queue_limit=2：TURN_START → TOOL_START → TURN_END 不得静默缺一条。"""
    bus = EventBus(queue_limit=2)
    agen = await _slow_subscriber(bus)

    await bus.publish(make_event(EventType.TURN_START, {"turn_id": "t1"}))
    await bus.publish(make_event(EventType.TOOL_START, {"turn_id": "t1", "call_id": "c1"}))
    await bus.publish(
        make_event(EventType.TURN_END, {"turn_id": "t1", "status": "completed"})
    )

    chunks = await _drain(agen)
    types = [_type_of(c) for c in chunks]

    complete = {"TURN_START", "TOOL_START", "TURN_END"} <= set(types)
    assert complete or "RESYNC" in types, (
        f"关键生命周期事件被静默丢弃、客户端却以为序列完整：{types}"
    )
    if not complete:
        # 告知必须是**可解释**的，而不是一个没有理由的魔改事件
        reason = _resync_reason(chunks)
        assert "backlog" in reason or "overflow" in reason
        # 边界语义：失真区间里的事件一条都不许再送达 ——
        # 只发 RESYNC，客户端据此拉权威快照（旧事件继续发会污染刚恢复的状态）
        assert types == ["RESYNC"], types


async def test_approval_and_error_survive_critical_overflow():
    """审批与错误也属于不可静默丢失的转换事件。"""
    bus = EventBus(queue_limit=2)
    agen = await _slow_subscriber(bus)

    await bus.publish(make_event(EventType.TURN_START, {"turn_id": "t1"}))
    await bus.publish(
        make_event(EventType.APPROVAL_REQUIRED, {"approval": {"approval_id": "a1"}})
    )
    await bus.publish(make_event(EventType.ERROR, {"turn_id": "t1", "message": "boom"}))
    await bus.publish(
        make_event(EventType.TURN_END, {"turn_id": "t1", "status": "failed"})
    )

    chunks = await _drain(agen)
    types = [_type_of(c) for c in chunks]
    expected = {"TURN_START", "APPROVAL_REQUIRED", "ERROR", "TURN_END"}

    assert expected <= set(types) or "RESYNC" in types, (
        f"关键转换事件被静默丢弃：{types}"
    )
    assert len([c for c in chunks if _type_of(c) != "RESYNC"]) <= 2


async def test_mergeable_burst_never_triggers_a_resync():
    """可合并事件的淘汰是设计内的：不该因此惊动客户端去 resync。"""
    bus = EventBus(queue_limit=4)
    agen = await _slow_subscriber(bus)

    for i in range(100):
        await bus.publish(
            make_event(EventType.ASSISTANT, {"turn_id": "t1", "content": f"a{i}"})
        )
        await bus.publish(make_event(EventType.USAGE, {"turn_id": "t1", "tokens": i}))

    chunks = await _drain(agen)
    types = [_type_of(c) for c in chunks]

    assert "RESYNC" not in types, "累计型事件的合并/淘汰不需要 resync"
    assert len(chunks) <= 4


def test_every_event_type_is_classified():
    """分类必须显式且完备：新增事件类型时不允许「悄悄默认成可丢」。"""
    from agent.api.bus import CRITICAL_EVENTS, MERGEABLE_EVENTS, RESYNC_CONTROL_EVENTS

    classified = MERGEABLE_EVENTS | CRITICAL_EVENTS | RESYNC_CONTROL_EVENTS
    assert classified == set(EventType), (
        f"未分类事件：{sorted(e.value for e in set(EventType) - classified)}"
    )
    assert not (MERGEABLE_EVENTS & CRITICAL_EVENTS)
