"""SSE event bus: fan-out with bounded replay.

重连不能重新消费历史事件：每个事件都有实例内唯一的 `event_id`，
`stream(last_event_id)` 只补发该 id 之后的事件（标准 `Last-Event-ID`）。
客户端另有有限长度的自己去重表，双保险。

每个订阅者另有**有界缓冲**：慢订阅者（卡住的 SSE 连接）不能把服务端内存拖爆。
缓冲满时按「可合并 → 可丢」的优先级让位：

* `ASSISTANT` / `USAGE` 是同一 turn 的**累计状态**，只保留最新一条即等价；
* turn 生命周期等关键事件优先保留，只有缓冲里已经没有可合并事件时才让位。
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import AsyncIterator

from agent.api.events import AgentEvent, EventType, sse_format

DEFAULT_QUEUE_LIMIT = 256

# 同一 turn 内可原地合并的事件：新值已含旧值（累计语义），旧条目没有信息量。
_MERGEABLE: frozenset[EventType] = frozenset({EventType.ASSISTANT, EventType.USAGE})


class _Subscriber:
    """单个订阅者的有界缓冲 + 唤醒信号。"""

    def __init__(self, limit: int) -> None:
        self._limit = max(1, limit)
        self._items: deque[AgentEvent] = deque()
        self._wake = asyncio.Event()

    def __len__(self) -> int:
        return len(self._items)

    def offer(self, event: AgentEvent) -> None:
        """入队；满时按优先级让位，绝不无界增长。"""
        mergeable = event.type in _MERGEABLE
        if mergeable:
            index = self._merge_target(event)
            if index is not None:
                del self._items[index]
                self._push(event)
                return
        if len(self._items) < self._limit:
            self._push(event)
            return

        victim = self._drop_target(prefer_mergeable=mergeable)
        if victim is None:
            # 缓冲里全是不可丢掉的关键事件，且这条本身也没有更高优先级 → 丢这条。
            return
        del self._items[victim]
        self._push(event)

    async def get(self) -> AgentEvent:
        while not self._items:
            self._wake.clear()
            await self._wake.wait()
        return self._items.popleft()

    # -- internals --------------------------------------------------------

    def _push(self, event: AgentEvent) -> None:
        self._items.append(event)
        self._wake.set()

    def _merge_target(self, event: AgentEvent) -> int | None:
        """同一 (类型, turn) 已有条目 → 合并到最新（保留到达顺序，旧条目移除）。"""
        for index in range(len(self._items) - 1, -1, -1):
            other = self._items[index]
            if other.type == event.type and other.data.get("turn_id") == event.data.get(
                "turn_id"
            ):
                return index
        return None

    def _drop_target(self, *, prefer_mergeable: bool) -> int | None:
        """选一个被丢弃的条目：先牺牲可合并的累计型事件。"""
        for index, item in enumerate(self._items):
            if item.type in _MERGEABLE:
                return index
        # 全是关键事件：关键事件自己让位（最新状态优先），累计型事件则宁可丢自己。
        return None if prefer_mergeable else 0


class EventBus:
    def __init__(self, replay_limit: int = 50, queue_limit: int = DEFAULT_QUEUE_LIMIT) -> None:
        self._subscribers: set[_Subscriber] = set()
        self._history: deque[AgentEvent] = deque(maxlen=replay_limit)
        self._queue_limit = max(1, queue_limit)

    async def publish(self, event: AgentEvent) -> None:
        self._history.append(event)
        for subscriber in list(self._subscribers):
            subscriber.offer(event)

    async def stream(self, last_event_id: str | None = None) -> AsyncIterator[str]:
        subscriber = _Subscriber(self._queue_limit)
        self._subscribers.add(subscriber)
        try:
            for event in self._replay(last_event_id):
                subscriber.offer(event)
            while True:
                yield sse_format(await subscriber.get())
        finally:
            self._subscribers.discard(subscriber)

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
