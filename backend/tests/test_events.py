from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from agent.api.events import EventType, make_event, sse_format
from agent.api.server import EventBus, create_app
from agent.config import Settings


def test_sse_format_envelope():
    event = make_event(EventType.CAPABILITY, {"adapter": "native", "model": "m"})
    payload = json.loads(event.model_dump_json())
    assert set(payload) == {"type", "id", "ts", "data"}
    wire = sse_format(event)
    # id 行是重连游标（Last-Event-ID）：客户端据此只补发之后的事件
    assert wire.startswith(f"id: {event.id}\n")
    assert "\nevent: CAPABILITY\n" in wire
    assert f'"id":"{event.id}"' in wire
    assert "\n\n" in wire


def test_event_bus_fanout():
    async def scenario():
        bus = EventBus()
        chunks = []
        stream = bus.stream()
        # 先订阅再发布：事件流的职责是「广播变化」，不是「重放历史」
        async def publish():
            await bus.publish(make_event(EventType.TURN_START, {"turn": 1}))
            await bus.publish(make_event(EventType.USAGE, {"tokens": 5}))

        task = asyncio.create_task(publish())
        async for chunk in stream:
            chunks.append(chunk)
            if len(chunks) >= 2:
                break
        await task
        return chunks

    chunks = asyncio.run(scenario())
    assert "TURN_START" in chunks[0]
    assert "USAGE" in chunks[1]


def test_event_bus_does_not_replay_for_new_subscribers():
    """新连接不再收到历史事件（阶段 2 的重放策略）。

    旧行为是「谁连上来都把最近一批事件重发一遍」，于是新打开的页面会看到
    上一次的错误提示与排队条。现在新页面改为自己拉权威快照。
    """

    async def scenario():
        bus = EventBus()
        await bus.publish(make_event(EventType.CAPABILITY, {"adapter": "text"}))
        chunks = []
        stream = bus.stream()  # 没有 Last-Event-ID：不该补发任何东西
        try:
            async with asyncio.timeout(0.2):
                async for chunk in stream:
                    chunks.append(chunk)
                    break
        except TimeoutError:
            pass
        await stream.aclose()
        return chunks

    chunks = asyncio.run(scenario())
    assert chunks == [], f"新订阅者不该收到历史事件，实际收到 {len(chunks)} 条"


def test_event_bus_replays_only_for_real_reconnects():
    """带 Last-Event-ID 的重连仍然要补齐断线期间漏掉的事件。"""

    async def scenario():
        bus = EventBus()
        first = make_event(EventType.CAPABILITY, {"adapter": "text"})
        await bus.publish(first)
        await bus.publish(make_event(EventType.WARNING, {"message": "断线期间发生的"}))

        stream = bus.stream(last_event_id=first.id)
        chunks = []
        async for chunk in stream:
            chunks.append(chunk)
            break
        await stream.aclose()
        return chunks

    chunks = asyncio.run(scenario())
    assert len(chunks) == 1
    assert "断线期间发生的" in chunks[0]
    assert "CAPABILITY" not in chunks[0], "游标之前的事件不该再发"


def test_health_and_test_publish(db_conn, settings: Settings):
    app = create_app(settings, db_conn)
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        resp = client.post(
            "/api/events/test?event_type=WARNING",
            json={"code": "parse_failed"},
        )
        assert resp.status_code == 200
        assert resp.json()["type"] == "WARNING"


def test_all_event_types_serialize():
    for event_type in EventType:
        event = make_event(event_type, {"k": "v"})
        payload = json.loads(event.model_dump_json())
        assert payload["type"] == event_type.value
