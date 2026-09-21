"""Subagent runtime: contract, task manager, subagent tool, notify, overrides."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from agent.tools.base import ToolResult
from agent.tools.spec import SubagentBudget, ToolDefinition, validate_tool_proposal


# ---------- spec ----------

def test_subagent_definition_defaults_budget_without_ref_or_model():
    # 子任务不再要求 credential_ref/model：改由 subagent tag 在运行时选钥。
    d = ToolDefinition(name="t", description="d", tool_type="subagent")
    assert d.credential_ref is None
    assert d.model is None
    assert d.subagent_budget is not None
    assert d.subagent_budget.max_iterations == 5
    assert d.subagent_budget.max_tokens == 100_000
    d = ToolDefinition(
        name="research_x",
        description="d",
        tool_type="subagent",
        credential_ref="k1",
        model="m1",
    )
    assert d.subagent_budget is not None
    assert d.subagent_budget.max_iterations == 5
    assert d.subagent_budget.max_tokens == 100_000
    assert d.subagent_budget.output_limit_chars == 2000


def test_subagent_budget_validation():
    with pytest.raises(Exception):
        SubagentBudget(max_iterations=0)
    with pytest.raises(Exception):
        SubagentBudget(max_tokens=100)


# ---------- task manager ----------

from agent.api.bus import EventBus
from agent.api.events import EventType


def _collect_events(bus: EventBus, limit: int = 20):
    out = []

    async def consume():
        async for chunk in bus.stream():
            out.append(chunk)
            if len(out) >= limit:
                return

    task = asyncio.create_task(consume())
    return out, task


async def test_task_manager_concurrency_and_results():
    bus = EventBus()
    from agent.tools.task_manager import TaskManager

    tm = TaskManager(bus, max_concurrent=2)
    running = 0
    peak = 0

    async def slow(n: int):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return ToolResult(ok=True, content=f"result-{n}")

    ids = [tm.submit("t", lambda n=i: slow(n)) for i in range(4)]
    assert len(ids) == 4
    # 全部完成后 peak <= 2
    await asyncio.sleep(0.5)
    assert peak <= 2, f"peak concurrency {peak} > 2"
    status, result = await tm.await_result(ids[0], timeout=1)
    assert status == "done" and result is not None and "result-0" in result.content
    assert tm.record_info(ids[3]).status == "done"


async def test_task_manager_await_timeout_and_failure():
    bus = EventBus()
    from agent.tools.task_manager import TaskManager

    tm = TaskManager(bus, max_concurrent=4)

    async def slow():
        await asyncio.sleep(0.2)
        return ToolResult(ok=True, content="late")

    async def boom():
        raise RuntimeError("exploded")

    tid = tm.submit("t", slow)
    status, result = await tm.await_result(tid, timeout=0.01)
    assert status == "running" and result is None  # 超时返回仍在运行
    status, result = await tm.await_result(tid, timeout=1)
    assert status == "done" and result.content == "late"

    tid2 = tm.submit("t", boom)
    status, result = await tm.await_result(tid2, timeout=1)
    assert status == "failed" and result is not None and not result.ok
    assert tm.record_info("ghost") is None
    status, result = await tm.await_result("ghost", timeout=0.1)
    assert status == "not_found"


async def test_queued_task_timeout_reports_queued_not_running():
    """timeout 只说明「等待窗口内没有进入终态」，不代表任务正在运行。

    真实缺陷：`await_result` 超时统一返回 "running"，把排队中的任务报成正在执行，
    状态语义在等待路径上又被破坏了一次。
    """
    from agent.tools.task_manager import TaskManager

    tm = TaskManager(_RecordingBus(), max_concurrent=1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking():
        started.set()
        await release.wait()
        return ToolResult(ok=True, content="first")

    first = tm.submit("t", blocking)
    second = tm.submit("t", lambda: _ok("second"))
    await started.wait()
    await asyncio.sleep(0.01)
    assert tm.record_info(second).status == "queued"

    status, result = await tm.await_result(second, timeout=0.02)
    assert status == "queued", "排队中的任务超时后必须报 queued，不能报 running"
    assert result is None
    release.set()
    await tm.await_result(first, timeout=1)
    await tm.await_result(second, timeout=1)


async def test_running_task_timeout_reports_running():
    """真正在跑的任务超时后仍然是 running（这条语义不变）。"""
    from agent.tools.task_manager import TaskManager

    tm = TaskManager(_RecordingBus(), max_concurrent=4)
    release = asyncio.Event()
    started = asyncio.Event()

    async def blocking():
        started.set()
        await release.wait()
        return ToolResult(ok=True, content="late")

    tid = tm.submit("t", blocking)
    await started.wait()
    status, result = await tm.await_result(tid, timeout=0.02)
    assert status == "running" and result is None
    release.set()
    status, result = await tm.await_result(tid, timeout=1)
    assert status == "done" and result.content == "late"


async def test_timeout_reports_status_at_the_boundary():
    """queued → running 恰好发生在等待窗口内时，返回的状态必须与当前 record 一致。"""
    from agent.tools.task_manager import TaskManager

    tm = TaskManager(_RecordingBus(), max_concurrent=1)
    gate_first = asyncio.Event()
    gate_second = asyncio.Event()

    async def first():
        await gate_first.wait()
        return ToolResult(ok=True, content="first")

    async def second():
        await gate_second.wait()
        return ToolResult(ok=True, content="second")

    tm.submit("t", first)
    queued = tm.submit("t", second)
    await asyncio.sleep(0.01)
    assert tm.record_info(queued).status == "queued"

    waiter = asyncio.create_task(tm.await_result(queued, timeout=0.2))
    await asyncio.sleep(0.01)
    gate_first.set()  # 释放名额：queued 立刻变成 running
    await asyncio.sleep(0.05)
    assert tm.record_info(queued).status == "running"

    status, result = await waiter  # 窗口内没进终态 → 超时
    assert status == tm.record_info(queued).status == "running"
    assert result is None

    gate_second.set()
    status, result = await tm.await_result(queued, timeout=1)
    assert status == "done" and result.content == "second"


async def test_task_manager_notify_callback():
    bus = EventBus()
    from agent.tools.task_manager import TaskManager

    tm = TaskManager(bus, max_concurrent=4)
    notified: list[str] = []

    async def work():
        await asyncio.sleep(0.05)
        return ToolResult(ok=True, content="done")

    tid = tm.submit("t", work)
    tm.register_notify(tid, lambda tid_, rec: notified.append(tid_))
    await asyncio.sleep(0.3)
    assert notified == [tid]


async def test_waiter_keeps_its_result_when_record_is_evicted_right_after():
    """任务刚完成 → 记录被 retention 回收 → waiter 还没被调度读结果。

    `_run` 的顺序是「先兑现 waiter，再 prune」，两者之间没有 await 点，
    所以 waiter 一定是在 prune 之后才真正读到结果。如果兑现时传的是**记录对象**
    而不是当时的结局，prune 里的 `release()` 会把结果清空，waiter 就拿到一个
    「done 但没有内容」的假结论 —— 这比返回 not_found 更糟。
    """
    from agent.tools.task_manager import TaskManager

    tm = TaskManager(_RecordingBus(), max_concurrent=4, max_records=1)
    gate = asyncio.Event()

    async def work():
        await gate.wait()
        return ToolResult(ok=True, content="value")

    tid = tm.submit("t", work)

    async def evict(_tid, _record):
        # 通知回调恰好发生在「waiter 已兑现、还没被调度」之后、「prune 之前」
        tm.submit("t2", lambda: _ok("other"))

    tm.register_notify(tid, evict)
    waiter = asyncio.create_task(tm.await_result(tid, timeout=2))
    await asyncio.sleep(0)
    # 确认 waiter 已经把自己的 future 挂上（走 future 路径，而不是「已完成」快速路径）
    assert tm._waiters.get(tid)
    gate.set()

    status, result = await asyncio.wait_for(waiter, timeout=2)
    assert status == "done"
    assert result is not None and result.content == "value", (
        "waiter 已经拿到的结局不能被 retention 回收掉"
    )


async def test_evicted_record_lookup_is_not_a_fake_done():
    """记录被回收之后，查询必须如实说 not_found，不能给一个「done + 空结果」。"""
    from agent.tools.task_manager import TaskManager

    tm = TaskManager(_RecordingBus(), max_concurrent=4, max_records=1)
    first = tm.submit("t", lambda: _ok("value"))
    status, result = await tm.await_result(first, timeout=2)
    assert status == "done" and result.content == "value"

    tm.submit("t2", lambda: _ok("other"))  # 触发 prune，把上一条挤出去
    await asyncio.sleep(0.05)
    assert tm.record_info(first) is None
    status, result = await tm.await_result(first, timeout=0.05)
    assert (status, result) == ("not_found", None)


class _RecordingBus:
    """只记录事件的假总线：用于断言状态事件序列（真 EventBus 另有测试）。"""

    def __init__(self) -> None:
        self.events: list = []

    async def publish(self, event) -> None:
        self.events.append(event)

    def statuses(self, task_id: str) -> list[str]:
        return [
            e.data.get("status") for e in self.events if e.data.get("task_id") == task_id
        ]


async def _ok(content: str) -> ToolResult:
    return ToolResult(ok=True, content=content)


async def test_queued_task_not_marked_running():
    """并发额度占满时，排队任务必须仍是 queued，且不得发出 running 事件。

    真实缺陷：`_run` 在抢到 semaphore 之前就把状态设成 running 并广播，
    前端因此看到「排队中的任务已经在跑」。
    """
    from agent.tools.task_manager import TaskManager

    bus = _RecordingBus()
    tm = TaskManager(bus, max_concurrent=1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking():
        started.set()
        await release.wait()
        return ToolResult(ok=True, content="first")

    async def quick():
        return ToolResult(ok=True, content="second")

    first = tm.submit("t", blocking)
    second = tm.submit("t", quick)
    await started.wait()
    await asyncio.sleep(0.01)  # 让第二个任务跑到 semaphore 之前

    assert tm.record_info(first).status == "running"
    assert tm.record_info(second).status == "queued"
    assert bus.statuses(second) == ["queued"]

    release.set()
    await tm.await_result(first, timeout=2)
    status, result = await tm.await_result(second, timeout=2)
    assert status == "done" and result is not None and result.content == "second"
    assert bus.statuses(second) == ["queued", "running", "done"]


async def test_wait_timeout_cleans_waiter():
    """await_result 超时后必须摘掉自己注册的 waiter，否则每次超时都留下永久残留。"""
    from agent.tools.task_manager import TaskManager

    tm = TaskManager(_RecordingBus(), max_concurrent=1)
    release = asyncio.Event()

    async def slow():
        await release.wait()
        return ToolResult(ok=True, content="late")

    tid = tm.submit("t", slow)
    status, result = await tm.await_result(tid, timeout=0.01)
    assert status == "running" and result is None
    assert tm._waiters.get(tid, []) == []

    # 摘除 waiter 不能影响后续等待：任务完成后仍要能取回结果
    release.set()
    status, result = await tm.await_result(tid, timeout=2)
    assert status == "done" and result is not None and result.content == "late"
    assert tm._waiters.get(tid, []) == []


async def test_task_records_bounded_and_released():
    """已完成记录有上限：超出上限的老任务被回收，并真正释放 result / full_content。"""
    from agent.tools.task_manager import TaskManager

    tm = TaskManager(_RecordingBus(), max_concurrent=4, max_records=3)
    records = []
    ids = []
    for i in range(6):
        tid = tm.submit("t", lambda i=i: _ok(f"r{i}"))
        ids.append(tid)
        await tm.await_result(tid, timeout=2)
        record = tm.record_info(tid)
        record.full_content = f"full-{i}" * 100  # 只有长结果任务才会写这个字段
        records.append(record)

    assert len(tm._records) <= 3
    assert tm.record_info(ids[-1]) is not None
    assert tm.record_info(ids[0]) is None
    assert records[0].full_content is None and records[0].result is None


async def test_finished_records_expire_by_ttl():
    """TTL 到期后已完成记录不再驻留内存（长期运行的后端不会越跑越大）。"""
    from agent.tools.task_manager import TaskManager

    tm = TaskManager(_RecordingBus(), max_concurrent=2, record_ttl_seconds=0.05)
    tid = tm.submit("t", lambda: _ok("done"))
    await tm.await_result(tid, timeout=2)
    record = tm.record_info(tid)
    record.full_content = "x" * 500

    await asyncio.sleep(0.06)
    tm.prune()

    assert tm.record_info(tid) is None
    assert record.full_content is None and record.result is None


async def test_bounded_records_never_evict_live_tasks():
    """记录上限只回收已完成任务：排队 / 运行中的任务永远不能被顶掉。"""
    from agent.tools.task_manager import TaskManager

    tm = TaskManager(_RecordingBus(), max_concurrent=1, max_records=1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking():
        started.set()
        await release.wait()
        return ToolResult(ok=True, content="first")

    async def quick():
        return ToolResult(ok=True, content="second")

    running = tm.submit("t", blocking)
    queued = tm.submit("t", quick)
    await started.wait()
    tm.prune()

    assert tm.record_info(running) is not None
    assert tm.record_info(queued) is not None

    release.set()
    status, _ = await tm.await_result(queued, timeout=2)
    assert status == "done"


async def test_subagent_status_event_emitted():
    bus = EventBus()
    events, consumer = _collect_events(bus, limit=5)
    from agent.tools.task_manager import TaskManager

    tm = TaskManager(bus, max_concurrent=2)

    async def work():
        return ToolResult(ok=True, content="ok")

    tm.submit("sub_tool", work)
    await asyncio.sleep(0.3)
    consumer.cancel()
    types = []
    for chunk in events:
        if '"type": "SUBAGENT_STATUS"' in chunk or "SUBAGENT_STATUS" in chunk:
            types.append("SUBAGENT_STATUS")
    assert types, "expected SUBAGENT_STATUS events"


# ---------- SubagentTool + await_task + read_task_result ----------

from agent.adapters.base import ChatMessage, Completion
from agent.tools.base import Tool
from agent.tools.spec import ToolDefinition


class FakeReplyAdapter:
    mode = "native"
    model = "sub-model"

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def complete(self, messages, tools, **kwargs):
        self.calls += 1
        return Completion(message=ChatMessage(role="assistant", content=self.content))


class FakeBus:
    def __init__(self) -> None:
        self.published: list = []

    async def publish(self, event) -> None:
        self.published.append(event)


def _make_tool(
    bus,
    content: str = "子任务完成，结论是清淡饮食。",
    *,
    sync: bool = True,
    output_limit_chars: int = 2000,
    secret: str | None = "sk-sub",
):
    from agent.tools.task_manager import TaskManager
    from agent.tools.subagent_tool import SubagentTool

    class FakeCreds:
        def get_secret(self, ref):
            return secret

        def get_default_secret(self):
            return None

        def get_default_meta(self):
            return None

        def get_metadata(self, ref):
            return {"id": ref, "default_model": "m1"} if secret else None

        def list_tagged(self, tag):
            if not secret:
                return []
            return [{"id": "key_sub", "tags": [tag], "budget": None, "budget_used": 0}]

    tm = TaskManager(bus, max_concurrent=4)
    definition = ToolDefinition(
        name="research_x",
        description="研究任务",
        tool_type="subagent",
        credential_ref="key_sub",
        model="m1",
        sync=sync,
        subagent_budget={"max_iterations": 5, "max_tokens": 100000,
                         "output_limit_chars": output_limit_chars},
    )
    adapter = FakeReplyAdapter(content)

    async def adapter_factory(ref_key, model):
        return adapter

    tool = SubagentTool(
        definition,
        credentials=FakeCreds(),
        task_manager=tm,
        retriever=None,
        adapter_factory=adapter_factory,
        bus=bus,
    )
    return tool, tm, adapter


async def test_subagent_tool_sync_execution():
    bus = FakeBus()
    tool, tm, adapter = _make_tool(bus, content="子任务完成，结论是清淡饮食。")
    result = await tool.run(query="研究饮食")
    assert result.ok
    assert "清淡饮食" in result.content
    assert adapter.calls >= 1


async def test_subagent_tool_async_pending_then_await():
    bus = FakeBus()
    tool, tm, adapter = _make_tool(bus, sync=False, content="异步结果")
    result = await tool.run(query="研究")
    assert result.ok and "task_" in result.content
    status, res = await tm.await_result(result.content.split("task_")[1][:16] and
                                        [t for t in tm._records][0], timeout=2)
    assert status == "done" and res is not None and "异步结果" in res.content


async def test_subagent_tool_long_result_pointer_and_read():
    bus = FakeBus()
    long_text = "数据" * 1500  # 3000 字符 > 2000 软上限
    tool, tm, _ = _make_tool(bus, content=long_text, output_limit_chars=2000)
    result = await tool.run(query="研究")
    assert result.ok
    assert "read_task_result" in result.content
    assert "共 3000 字符" in result.content or "3000" in result.content
    task_id = [t for t in tm._records][0]
    # 完整读取
    from agent.tools.subagent_tool import ReadTaskResultTool
    rtr = ReadTaskResultTool(tm)
    r1 = await rtr.run(task_id=task_id, offset=0, limit=2000)
    assert r1.ok and r1.content == long_text[:2000]
    r2 = await rtr.run(task_id=task_id, offset=2000, limit=4000)
    assert r2.ok and r2.content == long_text[2000:]
    # 越界
    r3 = await rtr.run(task_id=task_id, offset=99999, limit=100)
    assert r3.ok and r3.content == ""


async def test_subagent_tool_missing_credential():
    bus = FakeBus()
    tool, tm, _ = _make_tool(bus, secret=None)
    result = await tool.run(query="x")
    assert result.ok is False
    assert "凭据" in (result.error or "")


async def test_await_task_tool_notify_and_timeout():
    bus = FakeBus()
    from agent.tools.task_manager import TaskManager
    from agent.tools.subagent_tool import AwaitTaskTool

    tm = TaskManager(bus, max_concurrent=4)
    notified: list[str] = []

    async def slow():
        await asyncio.sleep(0.1)
        return ToolResult(ok=True, content="late")

    async def handler(tid, rec):
        notified.append(tid)

    await_tool = AwaitTaskTool(tm, notify_handler=handler)
    tid = tm.submit("t", slow)
    r = await await_tool.run(task_id=tid, notify=True, timeout=60)
    assert r.ok and "通知" in r.content
    await asyncio.sleep(0.3)
    assert notified == [tid]
    # 任务已完成后再 await 直接返回
    r2 = await await_tool.run(task_id=tid, timeout=1)
    assert r2.ok and "late" in r2.content
    # 不存在
    r3 = await await_tool.run(task_id="ghost")
    assert r3.ok is False

