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
from agent.tools.spec import ToolDefinition, validate_tool_proposal
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


def _subagent_proposal(name: str) -> dict:
    """subagent 型工具的定义（字段形状沿用旧提案，便于对照）。"""
    base = dict(GOOD_PROPOSAL["tool"])
    base.update(
        {
            "name": name,
            "tool_type": "subagent",
            "credential_ref": "key_sub",
            "model": "deepseek-v4-flash",
            "code": "",
        }
    )
    return base


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
    outcome = await lifecycle.submit_definition(
        ToolDefinition(**GOOD_PROPOSAL["tool"]), GOOD_PROPOSAL["explanation"]
    )
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
    outcome = await lifecycle.submit_definition(
        ToolDefinition(**GOOD_PROPOSAL["tool"]), GOOD_PROPOSAL["explanation"]
    )
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert not outcome.ok and outcome.step == "approve"
    assert registry.get("add_numbers") is None


async def test_lifecycle_test_failure_blocks(db_conn: sqlite3.Connection):
    broken = dict(GOOD_PROPOSAL["tool"])
    broken["code"] = "def run(**kwargs):\n    return {'sum': kwargs['a'] - kwargs['b']}"
    bus = EventBus()
    approvals = ApprovalService(bus, timeout_seconds=5)
    registry = ToolRegistry()
    lifecycle = ToolLifecycle(
        adapter=ScriptedAdapter(),
        approvals=approvals,
        sandbox=SandboxExecutor(executor="subprocess"),
        registry=registry,
    )
    outcome = await lifecycle.submit_definition(
        ToolDefinition(**broken), GOOD_PROPOSAL["explanation"]
    )
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
        adapter=ScriptedAdapter(),
        approvals=approvals,
        sandbox=SandboxExecutor(executor="subprocess"),
        registry=registry,
        credentials=store,
    )
    task = asyncio.create_task(_auto_approve(bus, approvals))
    await asyncio.sleep(0.05)
    outcome = await lifecycle.submit_definition(
        ToolDefinition(**proposal["tool"]), proposal["explanation"]
    )
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


# ---------- 注册与持久化的可恢复顺序 ----------

def _add_numbers_definition(name: str = "add_numbers"):
    from agent.tools.spec import ToolDefinition

    return ToolDefinition(
        name=name, description="求和", tool_type="function",
        code="def run(**kwargs):\n    return {'sum': kwargs['a'] + kwargs['b']}",
        tests=[{"name": "t", "input": {"a": 1, "b": 2}, "expect": {"sum": 3}}],
    )


class _FailingStore:
    """落库直接失败（磁盘错误），用来复现「注册成功但提交报失败」。"""

    def __init__(self) -> None:
        self.removed: list[str] = []

    def load(self, name: str):
        return None

    def save(self, definition) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    def remove(self, name: str) -> None:
        self.removed.append(name)


class _RecordingStore:
    """记录保存顺序，并保留一个「上一可用版本」。"""

    def __init__(self, previous=None) -> None:
        self.previous = previous
        self.saved: list[str] = []
        self.removed: list[str] = []

    def load(self, name: str):
        return self.previous

    def save(self, definition) -> None:
        self.saved.append(definition.name)

    def remove(self, name: str) -> None:
        self.removed.append(name)


async def _lifecycle_with_store(store, registry=None):
    from agent.tools.lifecycle import ToolLifecycle

    class ScriptedAdapter:
        mode = "native"

        async def complete(self, messages, tools, **kwargs):
            from agent.adapters.base import ChatMessage, Completion

            return Completion(message=ChatMessage(role="assistant", content="ok"))

    bus = EventBus()
    registry = registry or ToolRegistry()
    lifecycle = ToolLifecycle(
        adapter=ScriptedAdapter(),
        approvals=ApprovalService(bus, timeout_seconds=5),
        sandbox=SandboxExecutor(executor="subprocess"),
        registry=registry,
        tool_store=store,
    )
    task = asyncio.create_task(_auto_approve(bus, lifecycle.approvals))
    await asyncio.sleep(0.05)
    return lifecycle, registry, task, bus


async def test_store_failure_does_not_leave_tool_registered(db_conn: sqlite3.Connection):
    store = _FailingStore()
    lifecycle, registry, task, bus = await _lifecycle_with_store(store)
    try:
        outcome = await lifecycle.submit_definition(
            _add_numbers_definition(), "计算两个数之和", skip_tests=True
        )
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert not outcome.ok and outcome.step == "register"
    # 关键：内存注册表里不能留下一个「界面说没成功、实际能调用」的工具
    assert registry.get("add_numbers") is None


async def test_register_failure_restores_previous_persisted_version(
    db_conn: sqlite3.Connection,
):
    previous = _add_numbers_definition()
    registry = ToolRegistry()
    registry.register(_DummyTool("add_numbers"))
    store = _RecordingStore(previous=previous)
    lifecycle, registry, task, bus = await _lifecycle_with_store(store, registry)
    try:
        outcome = await lifecycle.submit_definition(
            _add_numbers_definition(), "计算两个数之和", skip_tests=True
        )
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert not outcome.ok
    # 先写了新版本，注册失败（重名）后必须把上一可用版本写回去
    assert store.saved == ["add_numbers", "add_numbers"]
    assert store.removed == []


class _DummyTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = "dummy"
        self.parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs):
        from agent.tools.base import ToolResult

        return ToolResult(ok=True, content="{}")


async def test_subagent_tool_stub_fallback_without_wiring(db_conn: sqlite3.Connection):
    proposal = _subagent_proposal("deep_researcher")
    bus = EventBus()
    approvals = ApprovalService(bus, timeout_seconds=5)
    registry = ToolRegistry()
    lifecycle = ToolLifecycle(
        adapter=ScriptedAdapter(),
        approvals=approvals,
        sandbox=SandboxExecutor(executor="subprocess"),
        registry=registry,
        credentials=_FakeCreds(),
    )
    task = asyncio.create_task(_auto_approve(bus, approvals))
    await asyncio.sleep(0.05)
    outcome = await lifecycle.submit_definition(
        ToolDefinition(**proposal), "研究工具"
    )
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

    proposal = _subagent_proposal("deep_researcher2")
    proposal["subagent_budget"] = {
        "max_iterations": 3,
        "max_tokens": 50000,
        "output_limit_chars": 1000,
    }
    bus = EventBus()
    approvals = ApprovalService(bus, timeout_seconds=5)
    registry = ToolRegistry()
    lifecycle = ToolLifecycle(
        adapter=ScriptedAdapter(),
        approvals=approvals,
        sandbox=SandboxExecutor(executor="subprocess"),
        registry=registry,
        credentials=_FakeCreds(),
        task_manager=TaskManager(bus, max_concurrent=2),
        adapter_factory=lambda ref, model: None,
    )
    task = asyncio.create_task(_auto_approve(bus, approvals))
    await asyncio.sleep(0.05)
    outcome = await lifecycle.submit_definition(
        ToolDefinition(**proposal), "研究工具"
    )
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert outcome.ok
    tool = registry.get("deep_researcher2")
    assert isinstance(tool, SubagentTool)
    assert tool.definition.subagent_budget.max_iterations == 3

# ---------- D1 回归：审批之前执行生成代码的旧创建路径不得复活 ----------


def test_the_ungated_legacy_creation_path_is_gone():
    """历史缺口（复现）：create_from_request 在用户确认**之前**就把 AI 生成的
    代码真的跑了一遍 —— 复现里用户点了拒绝，沙箱执行次数仍然是 2（两条用例
    各一次）。它当时在生产代码里没有任何调用点（只有测试用），但一直留着。

    现在删掉，并用这条用例锁住：没有执行授权闸门的创建入口不得重新出现。
    要接回「让模型提案」，必须先走 tools/dev_auth.py 的闸门。
    """
    assert not hasattr(ToolLifecycle, "create_from_request")


def test_the_lifecycle_no_longer_asks_the_model_for_a_proposal():
    """唯一的创建入口是 submit_definition（定义来自工作区的 tool.json）。

    生命周期不再自己向模型要提案：那条路就是「先执行、后确认」的来源。
    """
    lifecycle = ToolLifecycle(
        adapter=ScriptedAdapter(),
        approvals=None,
        sandbox=SandboxExecutor(executor="subprocess"),
        registry=ToolRegistry(),
    )
    assert not hasattr(lifecycle, "creator")
