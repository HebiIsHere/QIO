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

        # 关键事件过载：这一路事件流从此刻起**无法再保证完整**。
        # 于是这里建立硬边界 —— 丢掉失真区间里的全部缓冲（包括触发本次溢出的这条），
        # 只标记「需要 resync」。客户端会据此拉取权威快照，
        # 而快照已经覆盖了被丢掉的这些事件所表达的状态。
        #
        # 关键点：边界之后不得再送出任何属于失真区间的旧事件，
        # 否则客户端刚按快照对齐，又会被旧事件改回错误状态。
        self._items.clear()
        self._mark_resync()

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
            replay, continuity_lost = self._replay(last_event_id)
            if continuity_lost:
                # 客户端的游标已经超出 replay 缓冲（或来自另一个实例）：
                # 我们已经无法补发它漏掉的那一段。此时**不能**只补发最近几条
                # 然后假装事件是连续的 —— 直接建立边界，让客户端拉权威快照。
                subscriber.offer(
                    self._control_event(
                        {
                            "reason": "replay_cursor_expired",
                            "message": "断线期间的事件已超出服务端缓冲，请重新同步状态",
                        }
                    )
                )
            for event in replay:
                subscriber.offer(event)
            while True:
                event = await subscriber.get()
                if event is None:
                    # 关键事件在背压中无法保序送达：不再假装事件流是完整的，
                    # 明确要求客户端重新获取权威快照。
                    yield sse_format(
                        self._record_control(
                            {
                                "reason": "subscriber_backlog_overflow",
                                "message": "事件流可能不完整，请重新同步状态",
                            }
                        )
                    )
                    continue
                yield sse_format(event)
        finally:
            self._subscribers.discard(subscriber)

    # -- control events ---------------------------------------------------

    @staticmethod
    def _control_event(payload: dict) -> AgentEvent:
        return make_event(EventType.RESYNC, payload)

    def _record_control(self, payload: dict) -> AgentEvent:
        """生成一条控制事件并**写进 history**。

        RESYNC 会被客户端存成 Last-Event-ID：如果它不在 history 里，
        下一次重连就会变成「未知游标」→ 再 RESYNC → 永远在 resync。
        所以它必须是可被识别的合法游标。
        """
        event = self._control_event(payload)
        self._history.append(event)
        return event

    def _replay(self, last_event_id: str | None) -> tuple[list[AgentEvent], bool]:
        """(补发的事件, 是否已经无法保证连续性)。

        三种游标状态必须区分：

        * 游标在 history 里 → 从下一条开始补发；
        * 游标正好是最新一条 → 什么都不补发，安静等新事件；
        * 游标不存在 / 已被缓冲挤出 / 来自另一个实例 → **连续性已丢失**，
          必须走 RESYNC，而不是补发最近几条假装连续。
        """
        history = list(self._history)
        if last_event_id is not None:
            ids = [e.id for e in history]
            if last_event_id in ids:
                history = history[ids.index(last_event_id) + 1 :]
            else:
                return [], True
        return [
            event
            for event in history
            # interactive events must not be replayed: a stale approval
            # would resurrect a modal on reconnect
            if event.type != EventType.APPROVAL_REQUIRED
        ], False
