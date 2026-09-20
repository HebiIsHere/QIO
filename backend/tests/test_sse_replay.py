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


async def test_unknown_cursor_replays_buffer_for_client_side_dedup():
    bus = EventBus()
    for i in range(3):
        await bus.publish(make_event(EventType.WARNING, {"n": i}))
    replayed = await _take(bus, 3, last_event_id="evt_from_a_previous_process")
    assert len(replayed) == 3


async def test_approval_required_is_never_replayed():
    bus = EventBus()
    await bus.publish(
        make_event(
            EventType.APPROVAL_REQUIRED,
            {"approval": {"approval_id": "appr_1", "kind": "computer", "payload": {}}},
        )
    )
    await bus.publish(make_event(EventType.WARNING, {"n": 1}))

    replayed = await _take(bus, 1)
    assert [e["type"] for e in replayed] == ["WARNING"]
