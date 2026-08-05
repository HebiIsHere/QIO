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
    assert wire.startswith("event: CAPABILITY\n")
    assert "\n\n" in wire


def test_event_bus_fanout():
    async def scenario():
        bus = EventBus()
        await asyncio.gather(
            bus.publish(make_event(EventType.TURN_START, {"turn": 1})),
            bus.publish(make_event(EventType.USAGE, {"tokens": 5})),
        )
        chunks = []
        async for chunk in bus.stream():
            chunks.append(chunk)
            if len(chunks) >= 2:
                break
        return chunks

    chunks = asyncio.run(scenario())
    assert "TURN_START" in chunks[0]
    assert "USAGE" in chunks[1]


def test_event_bus_replay_for_late_subscribers():
    async def scenario():
        bus = EventBus()
        await bus.publish(make_event(EventType.CAPABILITY, {"adapter": "text"}))
        chunks = []
        async for chunk in bus.stream():
            chunks.append(chunk)
            break
        return chunks

    chunks = asyncio.run(scenario())
    assert "CAPABILITY" in chunks[0]


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