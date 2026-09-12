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

