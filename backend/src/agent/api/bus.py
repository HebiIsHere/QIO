"""SSE event bus: fan-out with bounded replay.

重连不能重新消费历史事件：每个事件都有实例内唯一的 `event_id`，
`stream(last_event_id)` 只补发该 id 之后的事件（标准 `Last-Event-ID`）。
客户端另有有限长度的自己去重表，双保险。
"""

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

    async def stream(self, last_event_id: str | None = None) -> AsyncIterator[str]:
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            for event in self._replay(last_event_id):
                queue.put_nowait(event)
            while True:
                event = await queue.get()
                yield sse_format(event)
        finally:
            self._subscribers.discard(queue)

    def _replay(self, last_event_id: str | None) -> list[AgentEvent]:
        history = list(self._history)
        if last_event_id:
            ids = [e.id for e in history]
            if last_event_id in ids:
                history = history[ids.index(last_event_id) + 1 :]
            # 游标已被缓冲挤出（或来自上一个实例）→ 全量重放当前缓冲，
            # 由客户端按 event_id 去重，而不是猜测缺失的区间。
        return [
            event
            for event in history
            # interactive events must not be replayed: a stale approval
            # would resurrect a modal on reconnect
            if event.type != EventType.APPROVAL_REQUIRED
        ]
