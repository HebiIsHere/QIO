"""工具创建流程的进度事件（第三阶段 spec 第 20~31 条）。

建工具是一条有阶段的流程（提案 → 构建 → 测试 → 等待确认 → 注册 → 可用），
用户要看到的是**同一张卡**在原地推进，而不是一连串互不相干的工具卡。
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from agent.api.bus import EventBus
from agent.tools.approval import ApprovalService
from agent.tools.dev_tools import (
    CreateToolTool,
    DevRunTestsTool,
    DevSubmitTool,
    DevWriteFileTool,
)
from agent.tools.dev_workspace import DevWorkspace
from agent.tools.lifecycle import ToolLifecycle
from agent.tools.registry import ToolRegistry
from agent.tools.sandbox import SandboxExecutor
from agent.tools.spec import ToolDefinition

GOOD_CODE = "def run(**kwargs):\n    return {'sum': kwargs['a'] + kwargs['b']}"
GOOD_TESTS = [{"name": "t1", "input": {"a": 1, "b": 2}, "expect": {"sum": 3}}]
BAD_CODE = "def run(**kwargs):\n    return {'sum': 0}"


class ScriptedAdapter:
    """提案/命名不该在流程测试里发生，adapter 只提供一个可用的壳。"""

    mode = "native"
    model = "fake-model"

    async def complete(self, messages, tools, **kwargs):
        from agent.adapters.base import ChatMessage, Completion

        return Completion(message=ChatMessage(role="assistant", content="ok"))


async def _collect(bus: EventBus):
    collected: list[dict] = []

    async def consumer() -> None:
        async for chunk in bus.stream():
            for line in chunk.splitlines():
                if line.startswith("data: "):
                    evt = json.loads(line[6:])
                    if evt["type"] == "TOOL_CREATE_STATUS":
                        collected.append(evt["data"])

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.02)

    async def finish() -> None:
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    return collected, finish


async def _answer_approvals(bus: EventBus, approvals: ApprovalService, decision: str):
    """监听到审批请求就代替用户作答（测试里的「用户」）。"""

    async def worker() -> None:
        async for chunk in bus.stream():
            for line in chunk.splitlines():
                if line.startswith("data: "):
                    evt = json.loads(line[6:])
                    if evt["type"] == "APPROVAL_REQUIRED":
                        await approvals.respond(
                            evt["data"]["approval"]["approval_id"], decision
                        )

    return asyncio.create_task(worker())


def _definition(name: str = "add_numbers", *, code: str = GOOD_CODE) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="求和",
        tool_type="function",
        code=code,
        tests=GOOD_TESTS,
    )


def _wiring(tmp_path, bus: EventBus, *, turn_id_provider=None):
    ws = DevWorkspace(tmp_path / "ws")
    approvals = ApprovalService(bus, timeout_seconds=5)
    registry = ToolRegistry()

    async def builder():
        return ToolLifecycle(
            adapter=ScriptedAdapter(),
            approvals=approvals,
            sandbox=SandboxExecutor(executor="subprocess"),
            registry=registry,
            bus=bus,
            turn_id_provider=turn_id_provider,
        )

    return {
        "ws": ws,
        "approvals": approvals,
        "create": CreateToolTool(ws, bus=bus, turn_id_provider=turn_id_provider),
        "write": DevWriteFileTool(ws, bus=bus, turn_id_provider=turn_id_provider),
        "run_tests": DevRunTestsTool(
            ws, sandbox=SandboxExecutor(executor="subprocess"), bus=bus,
            turn_id_provider=turn_id_provider,
        ),
        "submit": DevSubmitTool(ws, lifecycle_builder=builder, bus=bus,
                                turn_id_provider=turn_id_provider),
    }


async def test_dev_flow_progresses_one_card_through_every_phase(tmp_path):
    bus = EventBus()
    tools = _wiring(tmp_path, bus)
    collected, finish = await _collect(bus)
    approver = await _answer_approvals(bus, tools["approvals"], "approved")

    created = await tools["create"].run(request="求和工具")
    assert created.ok
    await asyncio.sleep(0.05)  # 让订阅者把刚发出的事件收下来
    group_id = collected[0]["group_id"]

    await tools["write"].run(workspace=group_id, name="tool.py", content=GOOD_CODE)
    tools["ws"].write_definition(group_id, _definition())
    tests = await tools["run_tests"].run(workspace=group_id)
    assert tests.ok
    submitted = await tools["submit"].run(
        workspace=group_id,
        definition=_definition().model_dump(),
        explanation="这个工具计算两个数之和",
    )
    assert submitted.ok

    approver.cancel()
    await finish()

    assert [e["phase"] for e in collected] == [
        "proposal",
        "building",
        "testing",
        "testing_passed",
        "waiting_approval",
        "registering",
        "ready",
    ]
    # 一张卡：整条流程共用一个 group_id
    assert {e["group_id"] for e in collected} == {group_id}
    final = collected[-1]
    assert final["tool_name"] == "add_numbers"
    assert final["label"] == "已创建"
    assert final["detail"] == "现在可以使用"
    # 用户可见文案不放内部工作区路径 / 源代码（group_id 是分组用的数据字段，不是文案）
    for event in collected:
        text = f"{event['label']} {event['detail'] or ''}"
        assert "\\" not in text
        assert "ws_" not in text
        assert "def run" not in text


async def test_failed_tests_stop_before_asking_for_approval(tmp_path):
    bus = EventBus()
    tools = _wiring(tmp_path, bus)
    collected, finish = await _collect(bus)

    await tools["create"].run(request="坏工具")
    await asyncio.sleep(0.05)
    group_id = collected[0]["group_id"]
    tools["ws"].write_definition(group_id, _definition(code=BAD_CODE))
    result = await tools["run_tests"].run(workspace=group_id)
    await finish()

    assert result.ok is False
    assert [e["phase"] for e in collected] == [
        "proposal",
        "testing",
        "testing_failed",
    ]
    failure = collected[-1]
    assert failure["ok"] is False
    assert failure["detail"]
    assert "Traceback" not in json.dumps(failure, ensure_ascii=False)


async def test_rejected_approval_marks_the_card_failed_in_human_words(tmp_path):
    bus = EventBus()
    tools = _wiring(tmp_path, bus)
    collected, finish = await _collect(bus)
    approver = await _answer_approvals(bus, tools["approvals"], "rejected")

    await tools["create"].run(request="求和工具")
    await asyncio.sleep(0.05)
    group_id = collected[0]["group_id"]
    tools["ws"].write_definition(group_id, _definition())
    await tools["submit"].run(
        workspace=group_id,
        definition=_definition().model_dump(),
        explanation="这个工具计算两个数之和",
    )

    approver.cancel()
    await finish()

    phases = [e["phase"] for e in collected]
    assert phases[-2:] == ["waiting_approval", "failed"]
    failure = collected[-1]
    assert failure["ok"] is False
    assert "同意" in failure["detail"] or "拒绝" in failure["detail"]
    assert "registering" not in phases


async def test_events_carry_the_current_turn_id(tmp_path):
    bus = EventBus()
    tools = _wiring(tmp_path, bus, turn_id_provider=lambda: "turn-42")
    collected, finish = await _collect(bus)

    await tools["create"].run(request="求和工具")
    await finish()

    assert collected[0]["turn_id"] == "turn-42"


async def test_tools_work_without_a_bus(tmp_path):
    """老装配（不传 bus）必须照常工作：不发事件，也不抛错。"""
    ws = DevWorkspace(tmp_path / "ws")
    create = CreateToolTool(ws)
    write = DevWriteFileTool(ws)
    result = await create.run(request="求和工具")
    assert result.ok
    workspace = re.search(r"ws_[a-f0-9]+", result.content).group(0)
    assert (await write.run(workspace=workspace, name="tool.py", content=GOOD_CODE)).ok
