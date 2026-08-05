"""SSE event bus: fan-out with bounded replay."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import AsyncIterator

from agent.api.events import AgentEvent, EventType, sse_format


class EventBus:
    def __init__(self, replay_limit: int = 50) -> None:
        self._subscribers: set[asyncio.Queue[AgentEvent]] = set()
        self._history: deque[AgentEvent] = deque(maxlen=replay_limit)

    async def publish(self, event: AgentEvent) -> None:
        self._history.append(event)
        for queue in list(self._subscribers):
            queue.put_nowait(event)

    async def stream(self) -> AsyncIterator[str]:
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            for event in self._history:
                if event.type == EventType.APPROVAL_REQUIRED:
                    # interactive events must not be replayed: a stale approval
                    # would resurrect a modal on reconnect
                    continue
                queue.put_nowait(event)
            while True:
                event = await queue.get()
                yield sse_format(event)
        finally:
            self._subscribers.discard(queue)