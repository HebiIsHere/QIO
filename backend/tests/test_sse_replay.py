"""SSE 重连协议：每个事件有实例内唯一 id，重连只补发游标之后的事件。"""

from __future__ import annotations

import asyncio
import json

from agent.api.bus import EventBus
from agent.api.events import EventType, make_event, sse_format


def _parse(chunks: list[str]) -> list[dict]:
    events = []
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


async def _take(bus: EventBus, n: int, *, last_event_id: str | None = None) -> list[dict]:
    chunks: list[str] = []
    stream = bus.stream(last_event_id)
    try:
        while len(chunks) < n:
            chunks.append(await asyncio.wait_for(stream.__anext__(), timeout=1.0))
    except (TimeoutError, asyncio.TimeoutError):
        pass
    finally:
        await stream.aclose()
    return _parse(chunks)


async def test_event_ids_are_unique_and_serialized_on_the_wire():
    bus = EventBus()
    ids = []
    for i in range(50):
        event = make_event(EventType.WARNING, {"i": i})
        ids.append(event.id)
        await bus.publish(event)
    assert len(set(ids)) == len(ids)

    wire = sse_format(make_event(EventType.WARNING, {"i": 1}))
    assert wire.splitlines()[0].startswith("id: ")


async def test_reconnect_replays_only_events_after_the_cursor():
    bus = EventBus()
    published = []
    for i in range(4):
        event = make_event(EventType.WARNING, {"n": i + 1})
        published.append(event)
        await bus.publish(event)

    # 客户端已处理 1、2；重连只能拿到 3、4
    replayed = await _take(bus, 2, last_event_id=published[1].id)
    assert [e["data"]["n"] for e in replayed] == [3, 4]


async def test_cursor_for_turn_events_does_not_replay_them():
    bus = EventBus()
    start = make_event(EventType.TURN_START, {"turn_id": "turn_1"})
    end = make_event(EventType.TURN_END, {"turn_id": "turn_1", "status": "completed"})
    await bus.publish(start)
    await bus.publish(end)

    after = await _take(bus, 1, last_event_id=start.id)
    assert [e["type"] for e in after] == ["TURN_END"]


async def test_unknown_cursor_requires_resync_instead_of_partial_replay():
    """游标来自旧进程 / 已被缓冲挤出 → 连续性已丢失，必须 RESYNC。

    旧实现会「把当前缓冲全量补发，让客户端按 event_id 去重」——
    那会让客户端以为事件是连续的，而中间那一段（这里就是 n=0..2 之前的空档）
    其实永远补不回来了。
    """
    bus = EventBus()
    for i in range(3):
        await bus.publish(make_event(EventType.WARNING, {"n": i}))
    replayed = await _take(bus, 3, last_event_id="evt_from_a_previous_process")
    assert [e["type"] for e in replayed] == ["RESYNC"]
    assert replayed[0]["data"]["reason"] == "replay_cursor_expired"


async def test_approval_required_is_never_replayed():
    """审批永不被补发；新连接则一条历史都不补发（阶段 2 的重放策略）。

    旧断言是「新连接拿到 WARNING、拿不到审批」——那依赖「新连接会重放历史」，
    而重放历史正是新页面看到上一次提示与排队的原因。现在：

    * 新连接（无游标）：什么都不补发；
    * 重连（带游标）：补发游标之后的非审批事件，审批仍然永不补发。
    """
    bus = EventBus()
    await bus.publish(
        make_event(
            EventType.APPROVAL_REQUIRED,
            {"approval": {"approval_id": "appr_1", "kind": "computer", "payload": {}}},
        )
    )
    cursor = make_event(EventType.TURN_QUEUE, {"revision": 1, "running": None, "queued": []})
    await bus.publish(cursor)
    await bus.publish(make_event(EventType.WARNING, {"n": 1}))

    fresh = await _take(bus, 1)  # 新连接：不该拿到任何历史
    assert fresh == []

    replayed = await _take(bus, 1, last_event_id=cursor.id)
    assert [e["type"] for e in replayed] == ["WARNING"]
