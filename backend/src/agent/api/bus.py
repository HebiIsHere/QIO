"""SSE event bus: fan-out with bounded replay.

重连不能重新消费历史事件：每个事件都有实例内唯一的 `event_id`，
`stream(last_event_id)` 只补发该 id 之后的事件（标准 `Last-Event-ID`）。
客户端另有有限长度的自己去重表，双保险。

每个订阅者另有**有界缓冲**：慢订阅者（卡住的 SSE 连接）不能把服务端内存拖爆。
事件按语义分三类（见下面的常量，并有守卫测试保证分类完备）：

* **可合并**（`ASSISTANT` / `USAGE`）：同一 turn 的累计状态，只保留最新一条即等价，
  缓冲紧张时优先淘汰它们，且**不需要**惊动客户端；
* **关键**（状态转换：turn / tool / approval 生命周期、错误、快照、凭据、锚点等）：
  不能像流式更新那样被静默删除；
* **控制**（`RESYNC`）：由总线在事件流完整性受损时产生，不进入缓冲、不参与淘汰。

缓冲满了且里面**全是关键事件**时，无论丢哪一条，这条事件流都不再完整。
此时我们做两件事，而不是假装无事发生：

1. 仍然保持**有界内存**：丢弃最旧的那条，保留最新的（最新状态更有用）；
2. 标记该订阅者需要 resync，并在下一次取事件时**先**发一条 `RESYNC`，
   让客户端去重新拉取权威快照（`GET /api/turns/queue`）——
   禁止出现「关键事件被删除，但客户端完全不知道状态流已经不完整」。
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import AsyncIterator

from agent.api.events import AgentEvent, EventType, make_event, sse_format

DEFAULT_QUEUE_LIMIT = 256

# A. 可合并 / 可覆盖的累计状态事件：新值已含旧值，积压时可以合并或淘汰。
MERGEABLE_EVENTS: frozenset[EventType] = frozenset(
    {EventType.ASSISTANT, EventType.USAGE}
)

# B. 状态转换事件：不得静默丢失（丢了客户端会停在错误状态）。
CRITICAL_EVENTS: frozenset[EventType] = frozenset(
    {
        EventType.TURN_START,
        EventType.TURN_END,
        EventType.TURN_QUEUE,
        EventType.TOOL_START,
        EventType.TOOL_END,
        EventType.TOOL_CREATE_STATUS,
        EventType.APPROVAL_REQUIRED,
        EventType.APPROVAL_RESULT,
        EventType.SUBAGENT_STATUS,
        EventType.KNOWLEDGE_CANDIDATE,
        EventType.CREDENTIAL_STATUS,
        EventType.ANCHOR,
        EventType.TOPIC_SWITCH_SUGGESTED,
        EventType.CAPABILITY,
        EventType.FALLBACK,
        EventType.WARNING,
        EventType.ERROR,
    }
)

# C. 控制事件：由总线自己产生（不进缓冲），用于告知客户端「这一路事件流不完整」。
RESYNC_CONTROL_EVENTS: frozenset[EventType] = frozenset({EventType.RESYNC})


class _Subscriber:
    """单个订阅者的有界缓冲 + 唤醒信号。"""

    def __init__(self, limit: int) -> None:
        self._limit = max(1, limit)
        self._items: deque[AgentEvent] = deque()
        self._wake = asyncio.Event()
        # 事件流完整性受损（关键事件被迫丢弃）→ 需要让客户端重新同步
        self._resync_pending = False

    def __len__(self) -> int:
        return len(self._items)

    def offer(self, event: AgentEvent) -> None:
        """入队；满时按优先级让位，绝不无界增长。"""
        mergeable = event.type in MERGEABLE_EVENTS
        if mergeable:
            index = self._merge_target(event)
            if index is not None:
                del self._items[index]
                self._push(event)
                return
        if len(self._items) < self._limit:
            self._push(event)
            return

        victim = self._oldest_mergeable()
        if victim is not None:
            # 先牺牲累计型事件：它的信息被更新的同类型事件覆盖，淘汰是安全的
            del self._items[victim]
            self._push(event)
            return

        if mergeable:
            # 缓冲里全是关键事件：这条累计型事件本身可以被丢弃，不会丢状态
            return

        # 缓冲里全是关键事件，而这条也是关键事件：无论丢谁，事件流都不再完整。
        # 保持有界（丢最旧、留最新），但必须明确告知客户端需要重新同步。
        del self._items[0]
        self._resync_pending = True
        self._push(event)

    async def get(self) -> AgentEvent | None:
        """取下一个事件；返回 `None` 表示「这一路事件流已不完整，需要 resync」。"""
        while True:
            if self._resync_pending:
                self._resync_pending = False
                return None
            if self._items:
                return self._items.popleft()
            self._wake.clear()
            if self._resync_pending:  # pragma: no cover - 竞态兜底
                continue
            await self._wake.wait()

    # -- internals --------------------------------------------------------

    def _push(self, event: AgentEvent) -> None:
        self._items.append(event)
        self._wake.set()

    def _mark_resync(self) -> None:
        self._resync_pending = True
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

    def _oldest_mergeable(self) -> int | None:
        """最旧的可合并条目（没有则 None）。"""
        for index, item in enumerate(self._items):
            if item.type in MERGEABLE_EVENTS:
                return index
        return None


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
                event = await subscriber.get()
                if event is None:
                    # 关键事件在背压中无法保序送达：不再假装事件流是完整的，
                    # 明确要求客户端重新获取权威快照。
                    yield sse_format(
                        make_event(
                            EventType.RESYNC,
                            {
                                "reason": "subscriber_backlog_overflow",
                                "message": "事件流可能不完整，请重新同步状态",
                            },
                        )
                    )
                    continue
                yield sse_format(event)
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
