from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from agent.api.server import EventBus
from agent.credentials.store import CredentialStore, MemoryKeyring
from agent.tools.approval import ApprovalService
from agent.tools.lifecycle import ToolLifecycle
from agent.tools.registry import ToolRegistry
from agent.tools.sandbox import SandboxExecutor
from agent.tools.spec import validate_tool_proposal
from agent.tools.tester import ToolTester

GOOD_PROPOSAL = {
    "explanation": "计算两个数的和，作为演示工具",
    "tool": {
        "name": "add_numbers",
        "description": "Adds two numbers",
        "parameters": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
        "code": "def run(**kwargs):\n    return {'sum': kwargs['a'] + kwargs['b']}",
        "tool_type": "function",
        "sync": True,
        "tests": [
            {"name": "positive", "input": {"a": 1, "b": 2}, "expect": {"sum": 3}},
            {"name": "negative", "input": {"a": -1, "b": 1}, "expect": {"sum": 0}},
        ],
    },
}


class ScriptedAdapter:
    mode = "native"

    def __init__(self, response: str | None = None) -> None:
        self.response = response or json.dumps(GOOD_PROPOSAL, ensure_ascii=False)

    async def complete(self, messages, tools, **kwargs):
        from agent.adapters.base import ChatMessage, Completion

        return Completion(message=ChatMessage(role="assistant", content=self.response))


async def _auto_approve(bus: EventBus, approvals: ApprovalService, decision: str = "approved"):
    async for chunk in bus.stream():
        for line in chunk.splitlines():
            if line.startswith("data: "):
                event = json.loads(line[6:])
                if event["type"] == "APPROVAL_REQUIRED":
                    approval_id = event["data"]["approval"]["approval_id"]
                    await approvals.respond(approval_id, decision)


def test_validate_proposal_ok():
    proposal, error = validate_tool_proposal(json.dumps(GOOD_PROPOSAL))
    assert error is None
    assert proposal.tool.name == "add_numbers"
    assert proposal.explanation


def test_validate_proposal_rejects_bad_names_and_no_tests():
    bad_name = dict(GOOD_PROPOSAL)
    bad_name["tool"] = dict(GOOD_PROPOSAL["tool"], name="Bad Name")
    _, error = validate_tool_proposal(json.dumps(bad_name))
    assert error is not None
    no_tests = dict(GOOD_PROPOSAL)
    no_tests["tool"] = dict(GOOD_PROPOSAL["tool"], tests=[])
    _, error = validate_tool_proposal(json.dumps(no_tests))
    assert error is not None and "test" in error


async def test_sandbox_subprocess_executes_code():
    sandbox = SandboxExecutor(executor="subprocess")
    result = await sandbox.execute(
        "def run(**kwargs):\n    return {'x': kwargs['n'] * 2}", {"n": 4}
    )
    assert result.ok and result.value == {"x": 8}


async def test_sandbox_subprocess_failure():
    sandbox = SandboxExecutor(executor="subprocess")
    result = await sandbox.execute(
        "def run(**kwargs):\n    raise ValueError('boom')", {}
    )
    assert not result.ok
    assert "boom" in (result.stderr or "") or "boom" in (result.error or "")


async def test_tester_all_pass():
    from agent.tools.spec import ToolDefinition

    definition = ToolDefinition(**GOOD_PROPOSAL["tool"])
    tester = ToolTester(SandboxExecutor(executor="subprocess"))
    report = await tester.run(definition)
    assert report.passed
    assert report.summary == "2/2 tests passed"


async def test_tester_fails_on_mismatch():
    from agent.tools.spec import ToolDefinition

    bad = dict(GOOD_PROPOSAL["tool"], tests=[
        {"name": "wrong", "input": {"a": 1, "b": 2}, "expect": {"sum": 99}},
    ])
    report = await ToolTester(SandboxExecutor(executor="subprocess")).run(
        ToolDefinition(**bad)
    )
    assert not report.passed


async def test_approval_service_flow():
    bus = EventBus()
    approvals = ApprovalService(bus, timeout_seconds=5)
    task = asyncio.create_task(_auto_approve(bus, approvals, "approved"))
    await asyncio.sleep(0.05)
    result = await approvals.request("tool_create", {"name": "x"})
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert result.decision == "approved"


async def test_approval_service_reject():
    bus = EventBus()
    approvals = ApprovalService(bus, timeout_seconds=5)
    task = asyncio.create_task(_auto_approve(bus, approvals, "rejected"))
    await asyncio.sleep(0.05)
    result = await approvals.request("tool_create", {"name": "x"})
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert result.decision == "rejected"


async def test_approval_service_timeout():
    bus = EventBus()
    approvals = ApprovalService(bus, timeout_seconds=0.2)
    result = await approvals.request("tool_create", {"name": "x"})
    assert result.decision == "timeout"


async def test_lifecycle_end_to_end(db_conn: sqlite3.Connection):
    bus = EventBus()
    approvals = ApprovalService(bus, timeout_seconds=5)
    registry = ToolRegistry()
    lifecycle = ToolLifecycle(
        adapter=ScriptedAdapter(),
        approvals=approvals,
        sandbox=SandboxExecutor(executor="subprocess"),
        registry=registry,
    )
    task = asyncio.create_task(_auto_approve(bus, approvals))
    await asyncio.sleep(0.05)
    outcome = await lifecycle.create_from_request("我需要一个加法工具")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert outcome.ok
    assert outcome.tool_name == "add_numbers"
    tool = registry.get("add_numbers")
    assert tool is not None
    result = await tool.run(a=2, b=5)
    assert result.ok
    assert json.loads(result.content) == {"sum": 7}


async def test_lifecycle_rejected_not_registered(db_conn: sqlite3.Connection):
    bus = EventBus()
    approvals = ApprovalService(bus, timeout_seconds=5)
    registry = ToolRegistry()
    lifecycle = ToolLifecycle(
        adapter=ScriptedAdapter(),
        approvals=approvals,
        sandbox=SandboxExecutor(executor="subprocess"),
        registry=registry,
    )
    task = asyncio.create_task(_auto_approve(bus, approvals, "rejected"))
    await asyncio.sleep(0.05)
    outcome = await lifecycle.create_from_request("加法工具")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert not outcome.ok and outcome.step == "approve"
    assert registry.get("add_numbers") is None


async def test_lifecycle_test_failure_blocks(db_conn: sqlite3.Connection):
    broken = dict(GOOD_PROPOSAL)
    broken["tool"] = dict(
        GOOD_PROPOSAL["tool"],
        code="def run(**kwargs):\n    return {'sum': kwargs['a'] - kwargs['b']}",
    )
    bus = EventBus()
    approvals = ApprovalService(bus, timeout_seconds=5)
    registry = ToolRegistry()
    lifecycle = ToolLifecycle(
        adapter=ScriptedAdapter(json.dumps(broken, ensure_ascii=False)),
        approvals=approvals,
        sandbox=SandboxExecutor(executor="subprocess"),
        registry=registry,
    )
    outcome = await lifecycle.create_from_request("加法工具")
    assert not outcome.ok and outcome.step == "test"
    assert registry.get("add_numbers") is None


async def test_lifecycle_credential_grant(db_conn: sqlite3.Connection):
    store = CredentialStore(db_conn, keyring_backend=MemoryKeyring())
    store.create("weather-key", "sk-weather", tags=["research"], budget=100)
    proposal = dict(GOOD_PROPOSAL)
    proposal["tool"] = dict(
        GOOD_PROPOSAL["tool"],
        name="weather_fetch",
        credential_ref="weather-key",
        code="def run(**kwargs):\n    import os\n    return {'has_key': bool(os.environ.get('QIO_KEY_WEATHER_KEY'))}",
        tests=[{"name": "no_key_in_sandbox", "input": {}, "expect": {"has_key": False}}],
    )
    bus = EventBus()
    approvals = ApprovalService(bus, timeout_seconds=5)
    registry = ToolRegistry()
    lifecycle = ToolLifecycle(
        adapter=ScriptedAdapter(json.dumps(proposal, ensure_ascii=False)),
        approvals=approvals,
        sandbox=SandboxExecutor(executor="subprocess"),
        registry=registry,
        credentials=store,
    )
    task = asyncio.create_task(_auto_approve(bus, approvals))
    await asyncio.sleep(0.05)
    outcome = await lifecycle.create_from_request("天气工具")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert outcome.ok
    meta = store.get_metadata("weather-key")
    assert meta is not None and meta["status"] == "active"
    tool = registry.get("weather_fetch")
    result = await tool.run()
    assert result.ok and json.loads(result.content) == {"has_key": True}


class _FakeCreds:
    def __init__(self) -> None:
        self.scope: dict[str, str] = {}

    def get_metadata(self, ref: str):
        return {"status": "active", "id": ref}

    def grant_tool_scope(self, ref: str, tool: str) -> None:
        self.scope[ref] = tool


async def test_subagent_tool_stub_fallback_without_wiring(db_conn: sqlite3.Connection):
    proposal = dict(GOOD_PROPOSAL)
    proposal["tool"] = dict(
        GOOD_PROPOSAL["tool"],
        name="deep_researcher",
        tool_type="subagent",
        credential_ref="key_sub",
        model="deepseek-v4-flash",
        code="",
    )
    bus = EventBus()
    approvals = ApprovalService(bus, timeout_seconds=5)
    registry = ToolRegistry()
    lifecycle = ToolLifecycle(
        adapter=ScriptedAdapter(json.dumps(proposal, ensure_ascii=False)),
        approvals=approvals,
        sandbox=SandboxExecutor(executor="subprocess"),
        registry=registry,
        credentials=_FakeCreds(),
    )
    task = asyncio.create_task(_auto_approve(bus, approvals))
    await asyncio.sleep(0.05)
    outcome = await lifecycle.create_from_request("研究工具")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert outcome.ok
    tool = registry.get("deep_researcher")
    # 未接线（无 task_manager/adapter_factory）→ Stub 降级
    from agent.tools.runtime_tools import SubagentStubTool

    assert isinstance(tool, SubagentStubTool)
    result = await tool.run(query="x")
    assert not result.ok and "v1.5" in result.error


async def test_subagent_tool_registers_runtime_when_wired(db_conn: sqlite3.Connection):
    from agent.tools.subagent_tool import SubagentTool
    from agent.tools.task_manager import TaskManager

    proposal = dict(GOOD_PROPOSAL)
    proposal["tool"] = dict(
        GOOD_PROPOSAL["tool"],
        name="deep_researcher2",
        tool_type="subagent",
        credential_ref="key_sub",
        model="deepseek-v4-flash",
        code="",
        subagent_budget={"max_iterations": 3, "max_tokens": 50000, "output_limit_chars": 1000},
    )
    bus = EventBus()
    approvals = ApprovalService(bus, timeout_seconds=5)
    registry = ToolRegistry()
    lifecycle = ToolLifecycle(
        adapter=ScriptedAdapter(json.dumps(proposal, ensure_ascii=False)),
        approvals=approvals,
        sandbox=SandboxExecutor(executor="subprocess"),
        registry=registry,
        credentials=_FakeCreds(),
        task_manager=TaskManager(bus, max_concurrent=2),
        adapter_factory=lambda ref, model: None,
    )
    task = asyncio.create_task(_auto_approve(bus, approvals))
    await asyncio.sleep(0.05)
    outcome = await lifecycle.create_from_request("研究工具")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert outcome.ok
    tool = registry.get("deep_researcher2")
    assert isinstance(tool, SubagentTool)
    assert tool.definition.subagent_budget.max_iterations == 3
