"""Tool dev workflow: DevWorkspace, dev tools, lifecycle submit path."""

from __future__ import annotations

import asyncio
import json
import pytest

from agent.tools.base import ToolResult
from agent.tools.dev_workspace import DevWorkspace
from agent.tools.spec import ToolDefinition


class _AutoApprovals:
    """自动批准的审批替身：跑测试/提交前的那一次执行确认由它放行。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def request(self, kind, payload, **kwargs):  # noqa: ANN001
        from agent.tools.approval import ApprovalResult

        self.requests.append(payload)
        return ApprovalResult("appr_test", "approved")


# ---------- DevWorkspace ----------

def test_workspace_create_write_read_cleanup(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("计算两个数的和")
    assert task.id.startswith("ws_")
    assert (tmp_path / "ws" / task.id).exists()
    ws.write_file(task.id, "tool.py", "def run(**kwargs):\n    return 1")
    assert "def run" in ws.read_file(task.id, "tool.py")
    assert ws.read_file(task.id, "ghost.py") is None
    ws.cleanup(task.id)
    assert not (tmp_path / "ws" / task.id).exists()
    assert ws.task(task.id) is None


def test_workspace_path_validation(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    with pytest.raises(ValueError):
        ws.write_file(task.id, "../evil.py", "x")
    with pytest.raises(ValueError):
        ws.write_file(task.id, "a/b.py", "x")
    with pytest.raises(ValueError):
        ws.write_file(task.id, "x", "x" * 200_001)


def test_workspace_write_definition(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    ws.write_definition(task.id, ToolDefinition(
        name="add_numbers", description="求和", tool_type="function",
        code="def run(**kwargs):\n    return {'sum': kwargs['a'] + kwargs['b']}",
        tests=[{"name": "t1", "input": {"a": 1, "b": 2}, "expect": {"sum": 3}}],
    ))
    loaded = ws.read_definition(task.id)
    assert loaded is not None and loaded.name == "add_numbers"
    assert loaded.tests[0].expect == {"sum": 3}


def test_workspace_state_survives_restart_and_is_not_inferred(tmp_path):
    """状态落盘、重启可读；没有 state.json 时测试结果标为未知（不是「通过」）。"""
    root = tmp_path / "ws"
    ws = DevWorkspace(root)
    task = ws.create("做一个计算器")
    ws.record_test(task.id, True, "2/2 tests passed")

    reborn = DevWorkspace(root)
    restored = reborn.task(task.id)
    assert restored is not None
    assert restored.phase == "testing_passed"
    assert restored.last_test_passed is True
    assert restored.last_test_summary == "2/2 tests passed"
    assert restored.test_runs == 1

    # 删掉 state.json：只能证明「文件还在」，不能推断测试通过
    (task.dir / "state.json").unlink()
    reborn2 = DevWorkspace(root)
    again = reborn2.task(task.id)
    assert again is not None
    assert again.last_test_passed is None
    assert again.test_runs == 0


def test_workspace_content_digest_changes_with_content(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    first = ws.content_digest(task.id)
    ws.write_file(task.id, "tool.json", '{"name": "a"}')
    second = ws.content_digest(task.id)
    assert first and second and first != second


async def test_dev_list_tasks_reports_status():
    ws = DevWorkspace(Path_factory())
    task = ws.create("查文献")
    ws.record_test(task.id, False, "1/2 tests passed")
    tool = DevListTasksTool(ws)
    r = await tool.run()
    assert r.ok
    assert task.id in r.content
    assert "测试失败" in r.content
    assert "查文献" in r.content


async def test_dev_run_tests_records_authoritative_state():
    ws = DevWorkspace(Path_factory())
    task = ws.create("x")
    ws.write_definition(task.id, ToolDefinition(
        name="add_numbers", description="求和", tool_type="function",
        code="def run(**kwargs):\n    return {'sum': kwargs['a'] + kwargs['b']}",
        tests=[{"name": "t1", "input": {"a": 1, "b": 2}, "expect": {"sum": 3}}],
    ))
    from agent.tools.sandbox import SandboxExecutor

    tool = DevRunTestsTool(ws, sandbox=SandboxExecutor(), approvals=_AutoApprovals())
    r = await tool.run(workspace=task.id)
    assert r.ok
    state = ws.status(task.id)
    assert state["last_test_passed"] is True
    assert state["test_runs"] == 1


# ---------- dev tools ----------

from agent.api.bus import EventBus
from agent.tools.dev_tools import (
    CreateToolTool,
    DevListFilesTool,
    DevListTasksTool,
    DevReadFileTool,
    DevRunTestsTool,
    DevSubmitTool,
    DevWriteFileTool,
)


async def test_create_tool_returns_guide():
    ws = DevWorkspace(Path_factory())
    tool = CreateToolTool(ws)
    result = await tool.run(request="帮我做一个计算工具")
    assert result.ok and "ws_" in result.content
    assert "需求规格" in result.content  # 指南含必填项


async def test_create_tool_reports_initial_files():
    """create_tool 返回应告知模型工作区初始文件（request.md + tool.json 模板）。"""
    ws = DevWorkspace(Path_factory())
    tool = CreateToolTool(ws)
    result = await tool.run(request="帮我做一个查找文献的工具")
    assert result.ok
    assert "request.md" in result.content
    assert "tool.json" in result.content
    # 工作区确实预置了 tool.json 空模板（模型可直接改写，无需"看模板"卡住）
    import re

    task_id = re.search(r"ws_[a-f0-9]+", result.content).group(0)
    files = ws.list_files(task_id)
    assert "tool.json" in files
    assert "request.md" in files


async def test_dev_list_files_lists_and_validates():
    ws = DevWorkspace(Path_factory())
    task = ws.create("x")
    tool = DevListFilesTool(ws)
    r = await tool.run(workspace=task.id)
    assert r.ok
    assert "request.md" in r.content and "tool.json" in r.content
    # 无效工作区
    r2 = await tool.run(workspace="ghost")
    assert r2.ok is False


def Path_factory():
    import tempfile
    from pathlib import Path
    return Path(tempfile.mkdtemp()) / "ws"


async def test_dev_write_read_file():
    ws = DevWorkspace(Path_factory())
    task = ws.create("x")
    wt = DevWriteFileTool(ws)
    assert (await wt.run(workspace=task.id, name="tool.py", content="code")).ok
    assert (await wt.run(workspace="ghost", name="a", content="b")).ok is False
    rt = DevReadFileTool(ws)
    r = await rt.run(workspace=task.id, name="tool.py")
    assert r.ok and r.content == "code"


@pytest.mark.requires_docker
async def test_dev_run_tests_pass_and_fail():
    ws = DevWorkspace(Path_factory())
    task = ws.create("求和工具")
    ws.write_definition(task.id, ToolDefinition(
        name="add_numbers", description="求和", tool_type="function",
        code="def run(**kwargs):\n    return {'sum': kwargs['a'] + kwargs['b']}",
        tests=[
            {"name": "positive", "input": {"a": 1, "b": 2}, "expect": {"sum": 3}},
            {"name": "negative", "input": {"a": -1, "b": 1}, "expect": {"sum": 0}},
        ],
    ))
    tool = DevRunTestsTool(ws, approvals=_AutoApprovals())
    r = await tool.run(workspace=task.id)
    assert r.ok and "2/2" in r.content
    # 失败场景
    task2 = ws.create("坏工具")
    ws.write_definition(task2.id, ToolDefinition(
        name="bad", description="x", tool_type="function",
        code="def run(**kwargs):\n    return {'sum': 999}",
        tests=[{"name": "t", "input": {}, "expect": {"sum": 1}}],
    ))
    r2 = await tool.run(workspace=task2.id)
    assert r2.ok is False and "assertion" in (r2.error or "")


async def test_dev_submit_maps_outcome_and_keeps_project():
    ws = DevWorkspace(Path_factory())
    task = ws.create("x")
    calls = {"n": 0}

    from agent.tools.lifecycle import ToolOutcome

    async def fake_submit(definition, explanation):
        calls["n"] += 1
        assert definition.name == "add_numbers"
        return ToolOutcome(True, "add_numbers", "registered", "ok")

    class FakeLifecycle:
        async def submit_definition(
            self, definition, explanation, *, group_id=None, test_sink=None
        ):
            calls["group_id"] = group_id
            # 复测发生在提交路径内部：结论必须在事件之前落回同一个任务
            if test_sink is not None:
                test_sink(True, "2/2 tests passed")
            return await fake_submit(definition, explanation)

    async def builder():
        return FakeLifecycle()

    tool = DevSubmitTool(ws, lifecycle_builder=builder, approvals=_AutoApprovals())
    definition = {
        "name": "add_numbers", "description": "求和", "tool_type": "function",
        "code": "def run(**kwargs):\n    return {'sum': 1}",
        "tests": [{"name": "t", "input": {}, "expect": {"sum": 1}}],
    }
    # 新契约：以工作区 tool.json 为准，提交前必须先写入工作区。
    ws.write_definition(task.id, ToolDefinition(**definition))
    r = await tool.run(workspace=task.id, definition=definition, explanation="这个工具计算两个数之和")
    assert r.ok
    assert calls["n"] == 1
    # 同一张卡靠 group_id 串起来：提交时必须把工作区 id 传下去
    assert calls["group_id"] == task.id
    # 成功后**保留**项目：文件、需求与测试证据都是已完成任务的一部分
    assert ws.task(task.id) is not None
    state = ws.status(task.id)
    assert state["submitted"] is True
    assert state["last_test_summary"] == "2/2 tests passed"
    assert state["test_evidence_current"] is True
    # 另留一份不可变快照，之后在工作区继续改动不会覆盖已提交版本
    assert (ws.root_dir / "archive" / task.id / "state.json").exists()
    # 提交以工作区为准：不必再传 definition 也能提交
    task3 = ws.create("y")
    ws.write_definition(task3.id, ToolDefinition(**definition))
    r3 = await tool.run(workspace=task3.id, explanation="提交时不复述定义")
    assert r3.ok and calls["n"] == 2
    # 无效工作区
    r2 = await tool.run(workspace="ghost", definition=definition, explanation="x")
    assert r2.ok is False


# ---------- lifecycle.submit_definition ----------

async def test_lifecycle_submit_definition_full_path(db_conn: sqlite3.Connection):
    from agent.api.server import EventBus as _EB
    from agent.credentials.store import MemoryKeyring
    from agent.tools.approval import ApprovalService
    from agent.tools.lifecycle import ToolLifecycle
    from agent.tools.registry import ToolRegistry
    from agent.tools.sandbox import SandboxExecutor

    class ScriptedAdapter:
        mode = "native"

        async def complete(self, messages, tools, **kwargs):
            from agent.adapters.base import ChatMessage, Completion
            return Completion(message=ChatMessage(role="assistant", content="ok"))

    bus = EventBus()
    approvals = ApprovalService(bus, timeout_seconds=5)
    registry = ToolRegistry()

    class FakeCreds:
        def get_metadata(self, ref):
            return None  # 无凭据引用，不触发段 2

    lifecycle = ToolLifecycle(
        adapter=ScriptedAdapter(),
        approvals=approvals,
        sandbox=SandboxExecutor(executor="subprocess"),
        registry=registry,
        credentials=FakeCreds(),
    )

    async def auto_approve():
        async for chunk in bus.stream():
            for line in chunk.splitlines():
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    if event["type"] == "APPROVAL_REQUIRED":
                        await approvals.respond(event["data"]["approval"]["approval_id"], "approved")

    task = asyncio.create_task(auto_approve())
    await asyncio.sleep(0.05)
    definition = ToolDefinition(
        name="add_numbers", description="求和", tool_type="function",
        code="def run(**kwargs):\n    return {'sum': kwargs['a'] + kwargs['b']}",
        tests=[{"name": "t", "input": {"a": 1, "b": 2}, "expect": {"sum": 3}}],
    )
    outcome = await lifecycle.submit_definition(definition, "计算两个数之和")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert outcome.ok and outcome.step == "registered"
    tool = registry.get("add_numbers")
    assert tool is not None


async def test_lifecycle_submit_definition_test_gate(db_conn: sqlite3.Connection):
    from agent.tools.approval import ApprovalService
    from agent.tools.lifecycle import ToolLifecycle
    from agent.tools.registry import ToolRegistry
    from agent.tools.sandbox import SandboxExecutor

    class ScriptedAdapter:
        mode = "native"

        async def complete(self, messages, tools, **kwargs):
            from agent.adapters.base import ChatMessage, Completion
            return Completion(message=ChatMessage(role="assistant", content="ok"))

    bus = EventBus()
    lifecycle = ToolLifecycle(
        adapter=ScriptedAdapter(),
        approvals=ApprovalService(bus, timeout_seconds=1),
        sandbox=SandboxExecutor(executor="subprocess"),
        registry=ToolRegistry(),
    )
    bad = ToolDefinition(
        name="bad_tool", description="x", tool_type="function",
        code="def run(**kwargs):\n    return {'sum': 999}",
        tests=[{"name": "t", "input": {}, "expect": {"sum": 1}}],
    )
    outcome = await lifecycle.submit_definition(bad, "x")
    assert not outcome.ok and outcome.step == "test"


# ---------- 跨进程存活（重启后仍然认得磁盘上的工作区） ----------

def test_workspace_survives_process_restart(tmp_path):
    """真实事故：应用重启后磁盘上工作区文件一个没少，但 dev_* 一律回
    「找不到工作区」，工具创建流程断在那里（连续 5 次失败）。"""
    root = tmp_path / "ws"
    first = DevWorkspace(root)
    task = first.create("检索原神测试服爆料")
    first.write_file(task.id, "tool.py", "def run(**kwargs):\n    return 1")

    reborn = DevWorkspace(root)  # 模拟后端 / 应用重启

    restored = reborn.task(task.id)
    assert restored is not None
    assert restored.request == "检索原神测试服爆料"
    assert reborn.list_files(task.id) == ["request.md", "tool.json", "tool.py"]
    assert "def run" in reborn.read_file(task.id, "tool.py")


def test_workspace_restore_ignores_foreign_entries(tmp_path):
    root = tmp_path / "ws"
    root.mkdir(parents=True)
    (root / "not-a-workspace").mkdir()
    (root / "ws_short").mkdir()
    (root / "loose.txt").write_text("x", encoding="utf-8")
    ws = DevWorkspace(root)
    assert ws.task("not-a-workspace") is None
    assert ws.task("ws_short") is None


def test_workspace_restore_tolerates_missing_request_file(tmp_path):
    root = tmp_path / "ws"
    root.mkdir(parents=True)
    (root / "ws_0123456789ab").mkdir()
    ws = DevWorkspace(root)
    task = ws.task("ws_0123456789ab")
    assert task is not None
    assert task.request == ""
    assert ws.list_files(task.id) == []


# ---------- 证据绑定内容：文件一变，旧结论立即失效 ----------

def test_test_evidence_is_invalidated_when_content_changes(tmp_path):
    """真实缺口：测试通过后把代码改成错的，任务列表仍显示「测试通过」。"""
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    ws.record_test(task.id, True, "1/1 tests passed")
    assert ws.status(task.id)["evidence_state"] == "current"

    ws.write_file(task.id, "tool.py", "def run(**kwargs):\n    return 999")

    state = ws.status(task.id)
    assert state["evidence_state"] == "stale"
    assert state["test_evidence_current"] is False
    assert state["last_test_passed"] is True  # 历史保留，但不再是可用证据


def test_stale_evidence_survives_restart(tmp_path):
    root = tmp_path / "ws"
    ws = DevWorkspace(root)
    task = ws.create("x")
    ws.record_test(task.id, True, "1/1 tests passed")
    ws.write_file(task.id, "tool.py", "broken")

    reborn = DevWorkspace(root)
    state = reborn.status(task.id)
    assert state["evidence_state"] == "stale"
    assert state["test_evidence_current"] is False


async def test_dev_list_tasks_does_not_claim_stale_pass():
    ws = DevWorkspace(Path_factory())
    task = ws.create("查文献")
    ws.record_test(task.id, True, "1/1 tests passed")
    ws.write_file(task.id, "tool.py", "def run(**kwargs):\n    return 999")
    tool = DevListTasksTool(ws)
    r = await tool.run()
    assert r.ok
    assert "证据失效" in r.content


# ---------- state.json 是后端权威记录，不是普通文件 ----------

def test_agent_cannot_forge_state_file(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    with pytest.raises(ValueError):
        ws.write_file(task.id, "state.json", '{"last_test_passed": true, "test_runs": 99}')
    with pytest.raises(ValueError):
        ws.write_file(task.id, "request.md", "改掉需求")


def test_restore_rejects_state_without_schema(tmp_path):
    root = tmp_path / "ws"
    ws = DevWorkspace(root)
    task = ws.create("x")
    (task.dir / "state.json").write_text(
        '{"last_test_passed": true, "test_runs": 99}', encoding="utf-8"
    )
    restored = DevWorkspace(root).task(task.id)
    assert restored is not None
    assert restored.last_test_passed is None
    assert restored.test_runs == 0
    assert restored.evidence_state == "none"


def test_restore_marks_evidence_stale_when_files_changed_out_of_band(tmp_path):
    """越权改文件（受限子进程也能做到）之后，重启不能复活旧证据。"""
    root = tmp_path / "ws"
    ws = DevWorkspace(root)
    task = ws.create("x")
    ws.write_definition(task.id, ToolDefinition(
        name="add_numbers", description="求和", tool_type="function",
        code="def run(**kwargs):\n    return {'sum': 1}",
        tests=[{"name": "t", "input": {}, "expect": {"sum": 1}}],
    ))
    ws.record_test(task.id, True, "1/1 tests passed")
    # 绕过文件工具，直接改磁盘内容（等同越权子进程的行为）
    (task.dir / "tool.json").write_text('{"name": "add_numbers"}', encoding="utf-8")

    reborn = DevWorkspace(root)
    assert reborn.status(task.id)["evidence_state"] == "stale"


# ---------- 上报给主循环的事实（core/turn_facts.py） ----------

def test_fact_for_reports_evidence_and_requires_tests(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    fact = ws.fact_for(task.id, "create_tool")["dev_task"]
    assert fact["id"] == task.id
    assert fact["tool_name"] == "create_tool"
    assert fact["phase"] == "created"
    assert fact["submitted"] is False
    assert fact["requires_tests"] is True
    assert fact["test"] == {"state": "none", "passed": None, "summary": None}
    assert fact["version"] == ws.content_digest(task.id)


def test_fact_for_marks_subagent_tools_as_not_requiring_tests(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    ws.write_definition(task.id, ToolDefinition(
        name="deep_researcher", description="调研", tool_type="subagent", code="",
    ))
    assert ws.fact_for(task.id, "dev_run_tests")["dev_task"]["requires_tests"] is False


def test_fact_for_unknown_task_is_empty(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    assert ws.fact_for("ws_ffffffffffff", "create_tool") == {}


async def test_dev_run_tests_reports_facts():
    ws = DevWorkspace(Path_factory())
    task = ws.create("x")
    ws.write_definition(task.id, ToolDefinition(
        name="add_numbers", description="求和", tool_type="function",
        code="def run(**kwargs):\n    return {'sum': kwargs['a'] + kwargs['b']}",
        tests=[{"name": "t", "input": {"a": 1, "b": 2}, "expect": {"sum": 3}}],
    ))
    from agent.tools.sandbox import SandboxExecutor

    tool = DevRunTestsTool(ws, sandbox=SandboxExecutor(), approvals=_AutoApprovals())
    r = await tool.run(workspace=task.id)
    assert r.ok
    fact = (r.facts or {})["dev_task"]
    assert fact["test"]["state"] == "current"
    assert fact["test"]["passed"] is True
    assert fact["test"]["summary"] == "1/1 tests passed"


async def test_dev_write_file_reports_stale_evidence():
    """写完文件后的状态就是事实：证据被打成 stale，主循环据此补注记。"""
    ws = DevWorkspace(Path_factory())
    task = ws.create("x")
    ws.record_test(task.id, True, "1/1 tests passed")
    tool = DevWriteFileTool(ws)
    r = await tool.run(workspace=task.id, name="tool.py", content="broken")
    assert r.ok
    fact = (r.facts or {})["dev_task"]
    assert fact["test"]["state"] == "stale"
