"""事件流重放策略：新连接不重放历史，真实重连仍补齐。

来源：上一轮穷举 UI 截图时实测到的问题 —— 新打开的页面会看到上一次留下的
错误提示、排队条、候选卡（复验脚本 scripts/ui-catalog/replay-evidence.mjs，
证据图 conv-root/conv-120-replay-residue.png）。

修复的核心不是「少发几条」，而是把两件事分开：

* **新连接**（没有 Last-Event-ID）：事件流只负责「变化」，当前状态由客户端
  自己拉权威快照（/api/turns/queue）。
* **重连**（带 Last-Event-ID）：必须补齐断线期间漏掉的事件；游标已过期则
  明确要求重新同步，而不是补几条假装连续。
"""

from __future__ import annotations

import asyncio

import pytest

from agent.api.bus import EventBus
from agent.api.events import EventType, make_event


def _payload(chunk: str) -> str:
    for line in chunk.splitlines():
        if line.startswith("data: "):
            return line[6:]
    return ""


async def test_fresh_connection_gets_no_replay():
    bus = EventBus()
    await bus.publish(make_event(EventType.ERROR, {"message": "上一条执行失败"}))
    await bus.publish(
        make_event(EventType.TURN_QUEUE, {"revision": 3, "running": None, "queued": []})
    )
    await bus.publish(make_event(EventType.KNOWLEDGE_CANDIDATE, {"knowledge_id": "k1"}))

    stream = bus.stream()  # 新连接：没有 last_event_id
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(stream.__anext__(), timeout=0.15)
    await stream.aclose()


async def test_fresh_connection_still_receives_new_events():
    bus = EventBus()
    await bus.publish(make_event(EventType.ERROR, {"message": "旧的，不该重发"}))

    stream = bus.stream()
    task = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0.05)
    await bus.publish(make_event(EventType.WARNING, {"message": "新的，应当送达"}))
    chunk = await asyncio.wait_for(task, timeout=1)

    assert "WARNING" in chunk
    assert "旧的" not in chunk
    await stream.aclose()


async def test_reconnect_with_cursor_receives_missed_events():
    bus = EventBus()
    first = make_event(EventType.TURN_QUEUE, {"revision": 1, "running": None, "queued": []})
    await bus.publish(first)
    missed = make_event(EventType.WARNING, {"message": "断线期间发生的事"})
    await bus.publish(missed)

    stream = bus.stream(last_event_id=first.id)
    chunk = await asyncio.wait_for(stream.__anext__(), timeout=1)
    assert "断线期间发生的事" in _payload(chunk)
    await stream.aclose()


async def test_unknown_cursor_asks_for_resync_instead_of_pretending():
    bus = EventBus()
    await bus.publish(make_event(EventType.WARNING, {"message": "历史里的一条"}))

    stream = bus.stream(last_event_id="evt_from_another_instance")
    chunk = await asyncio.wait_for(stream.__anext__(), timeout=1)
    assert "RESYNC" in chunk
    assert "replay_cursor_expired" in chunk
    await stream.aclose()


async def test_approval_is_never_replayed_on_reconnect():
    bus = EventBus()
    first = make_event(EventType.TURN_QUEUE, {"revision": 1})
    await bus.publish(first)
    await bus.publish(make_event(EventType.APPROVAL_REQUIRED, {"approval": {"approval_id": "a1"}}))

    stream = bus.stream(last_event_id=first.id)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(stream.__anext__(), timeout=0.15)
    await stream.aclose()
