"""事件流的恢复协议：RESYNC 是**边界**，不是「顺便发一条提示」。

这一组测试覆盖三条恢复语义：

1. 关键事件过载后，失真区间里的旧事件一条都不许再送达 ——
   否则客户端刚按权威快照对齐，又被旧事件改回错误状态；
2. Last-Event-ID 已经超出 replay history（或来自别的实例）时必须 RESYNC，
   不能「补发最近几条」然后假装事件连续；
3. RESYNC 自己必须是一个**合法的 replay cursor** ——
   客户端把它存成 Last-Event-ID 之后，下一次重连不能被当成未知游标（否则每次重连都再 RESYNC）。
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


def _id_of(chunk: str) -> str:
    for line in chunk.splitlines():
        if line.startswith("id: "):
            return line[len("id: ") :]
    return ""


async def _subscribe(bus: EventBus):
    """注册一个订阅者；用一条 WARNING 预热并把它取走，之后缓冲区是空的。"""
    agen = bus.stream()
    first = asyncio.create_task(agen.__anext__())
    await asyncio.sleep(0)  # 让生成器跑起来并注册到总线
    await bus.publish(make_event(EventType.WARNING, {"warmup": True}))
    assert await first
    return agen


class _Reader:
    """不打断生成器的读取器。

    注意：不能直接用 `asyncio.wait_for(agen.__anext__(), timeout)` ——
    超时会取消这个 asend，异步生成器随之被关闭（订阅者被摘除），
    后面再读就永远是空的。这里保留 pending 的 asend，超时只是「这次没读到」。
    """

    def __init__(self, agen) -> None:
        self._agen = agen
        self._pending: asyncio.Future | None = None

    async def next(self, timeout: float = 0.05) -> str | None:
        if self._pending is None:
            self._pending = asyncio.ensure_future(self._agen.__anext__())
        done, _ = await asyncio.wait({self._pending}, timeout=timeout)
        if not done:
            return None
        task, self._pending = self._pending, None
        return task.result()

    async def drain(self, timeout: float = 0.05) -> list[str]:
        out: list[str] = []
        while True:
            chunk = await self.next(timeout)
            if chunk is None:
                return out
            out.append(chunk)

    async def take(self, count: int, timeout: float = 1.0) -> list[str]:
        out: list[str] = []
        for _ in range(count):
            chunk = await self.next(timeout)
            assert chunk is not None, f"只收到 {len(out)}/{count} 条事件"
            out.append(chunk)
        return out


async def test_resync_is_a_hard_boundary_for_stale_buffered_events():
    """RESYNC 之后，失真区间里的旧事件不得再作为实时事件被处理。"""
    bus = EventBus(queue_limit=2)
    agen = await _subscribe(bus)
    reader = _Reader(agen)

    await bus.publish(make_event(EventType.TURN_START, {"turn_id": "t1"}))
    await bus.publish(make_event(EventType.TOOL_START, {"turn_id": "t1", "call_id": "c1"}))
    await bus.publish(
        make_event(EventType.TURN_END, {"turn_id": "t1", "status": "completed"})
    )

    chunks = await reader.drain()
    types = [_type_of(c) for c in chunks]

    assert types and types[0] == "RESYNC", f"边界必须先给出：{types}"
    assert not ({"TURN_START", "TOOL_START", "TURN_END"} & set(types)), (
        f"失真区间的旧事件仍在 RESYNC 之后送达：{types}"
    )

    # 边界之后的新事件照常送达
    await bus.publish(make_event(EventType.TOOL_START, {"turn_id": "t2", "call_id": "c2"}))
    after = await reader.drain()
    assert [_type_of(c) for c in after] == ["TOOL_START"], after


async def test_resync_boundary_does_not_leak_the_event_that_triggered_it():
    """触发 overflow 的那条关键事件也属于失真区间：不能既 RESYNC 又把它当新事件送出。"""
    bus = EventBus(queue_limit=1)
    agen = await _subscribe(bus)
    reader = _Reader(agen)

    await bus.publish(make_event(EventType.TURN_START, {"turn_id": "t1"}))
    await bus.publish(make_event(EventType.TURN_END, {"turn_id": "t1", "status": "failed"}))

    chunks = await reader.drain()
    types = [_type_of(c) for c in chunks]
    assert types and types[0] == "RESYNC"
    assert "TURN_END" not in types, f"边界事件本身也不该继续送达：{types}"


async def test_unknown_last_event_id_forces_resync_instead_of_partial_replay():
    """cursor 已过期 / 不存在 / 来自旧实例 → RESYNC，而不是「补发最近几条」。"""
    bus = EventBus(replay_limit=3, queue_limit=8)
    for i in range(6):
        await bus.publish(make_event(EventType.WARNING, {"n": i}))

    agen = bus.stream(last_event_id="evt_expired_cursor")
    reader = _Reader(agen)
    first = await asyncio.wait_for(agen.__anext__(), timeout=1)
    assert _type_of(first) == "RESYNC"
    assert "replay" in first or "cursor" in first, "原因必须可解释"

    rest = await reader.drain()
    assert all(_type_of(c) != "WARNING" for c in rest), (
        f"不得把 history 当成连续续传：{[_type_of(c) for c in rest]}"
    )


async def test_cursor_in_history_replays_only_what_follows():
    bus = EventBus(replay_limit=10, queue_limit=8)
    events = [make_event(EventType.WARNING, {"n": i}) for i in range(4)]
    for event in events:
        await bus.publish(event)

    agen = bus.stream(last_event_id=events[1].id)
    chunks = await _Reader(agen).drain()

    assert [_type_of(c) for c in chunks] == ["WARNING", "WARNING"]
    assert '"n":2' in chunks[0] and '"n":3' in chunks[1]


async def test_cursor_at_latest_event_waits_without_resync():
    """B 类：cursor 正好是最新事件 → 什么都不补发，安静等新事件。"""
    bus = EventBus(replay_limit=10, queue_limit=4)
    latest = make_event(EventType.WARNING, {"n": 1})
    await bus.publish(latest)

    agen = bus.stream(last_event_id=latest.id)
    pending = asyncio.create_task(agen.__anext__())
    await asyncio.sleep(0.05)
    assert not pending.done(), "最新 cursor 不该触发任何补发或 RESYNC"

    await bus.publish(make_event(EventType.WARNING, {"n": 2}))
    chunk = await asyncio.wait_for(pending, timeout=1)
    assert _type_of(chunk) == "WARNING" and '"n":2' in chunk


async def test_resync_event_is_a_valid_replay_cursor():
    """一次 RESYNC 不得制造下一次无法识别的 reconnect cursor。"""
    bus = EventBus(queue_limit=2, replay_limit=10)
    agen = await _subscribe(bus)
    reader = _Reader(agen)

    await bus.publish(make_event(EventType.TURN_START, {"turn_id": "t1"}))
    await bus.publish(make_event(EventType.TOOL_START, {"turn_id": "t1", "call_id": "c1"}))
    await bus.publish(make_event(EventType.TURN_END, {"turn_id": "t1"}))

    chunks = await reader.drain()
    resync = next(c for c in chunks if _type_of(c) == "RESYNC")
    resync_id = _id_of(resync)
    assert resync_id, "RESYNC 必须带 id（它会被客户端存成 Last-Event-ID）"

    reconnected = bus.stream(last_event_id=resync_id)
    pending = asyncio.create_task(reconnected.__anext__())
    await asyncio.sleep(0.05)
    assert not pending.done(), (
        "用 RESYNC 的 id 重连时不能被当成未知游标（那会导致每次重连都再 RESYNC）"
    )

    await bus.publish(make_event(EventType.WARNING, {"after": 1}))
    chunk = await asyncio.wait_for(pending, timeout=1)
    assert _type_of(chunk) == "WARNING"


async def test_expired_cursor_resync_is_itself_a_valid_cursor():
    """两条 RESYNC 来源必须给出一致的恢复身份。

    过载触发的 RESYNC 已经写进 history；过期游标触发的也必须一样 ——
    否则客户端把 RESYNC 的 id 存成 Last-Event-ID 之后，下一次重连又会被判成
    「未知游标」→ 再 RESYNC，形成重复同步。
    """
    bus = EventBus(replay_limit=3, queue_limit=8)
    for i in range(6):
        await bus.publish(make_event(EventType.WARNING, {"n": i}))

    first = _Reader(bus.stream(last_event_id="evt_expired_cursor"))
    (resync_chunk,) = await first.take(1)
    assert _type_of(resync_chunk) == "RESYNC"
    resync_id = _id_of(resync_chunk)
    assert resync_id

    # 再次断线：客户端带着上一次 RESYNC 的 id 重连
    second = _Reader(bus.stream(last_event_id=resync_id))
    assert await second.next(0.05) is None, (
        "RESYNC 的 id 必须能被识别，不能再触发一次无意义的 RESYNC"
    )

    await bus.publish(make_event(EventType.WARNING, {"after": 1}))
    chunk = await second.next(1.0)
    assert chunk is not None and _type_of(chunk) == "WARNING"
