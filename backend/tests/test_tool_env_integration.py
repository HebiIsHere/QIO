"""专用依赖环境与测试/运行的接线：装好才跑，没装好就明说。

没有依赖声明的工具照旧走随包环境（不受影响）；声明了依赖的工具必须**用专用环境**
跑，环境没准备好就不执行 —— 不静默回落到随包解释器（那等于假装依赖装上了）。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from agent.tools.dev_tools import DevRunTestsTool
from agent.tools.dev_workspace import DevWorkspace
from agent.tools.runtime_tools import CodeTool
from agent.tools.sandbox import SandboxResult
from agent.tools.spec import ToolDefinition
from agent.tools.tool_envs import ToolEnvManager


class _RecordingSandbox:
    executor = "subprocess"
    timeout_seconds = 5.0

    def __init__(self) -> None:
        self.executions = 0
        self.interpreters: list[str | None] = []

    async def effective_executor(self) -> str:
        return "subprocess"

    async def execute(
        self, code, arguments, extra_env=None, policy=None, files=None, entry=None, interpreter=None
    ) -> SandboxResult:
        self.executions += 1
        self.interpreters.append(interpreter)
        return SandboxResult(ok=True, value={"ok": True}, stdout="", stderr="")


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def __call__(self, argv: list[str], timeout: float, cwd=None):
        self.calls.append(list(argv))
        if len(argv) >= 4 and argv[1:3] == ["-m", "venv"]:
            name = "Scripts/python.exe" if os.name == "nt" else "bin/python"
            target = Path(argv[3]) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("", encoding="utf-8")
        return True, ""


class _Approvals:
    def __init__(self, decision: str = "approved", reject_kinds: set[str] | None = None) -> None:
        self.decision = decision
        self.reject_kinds = set(reject_kinds or ())
        self.requests: list[tuple[str, dict]] = []

    async def request(self, kind: str, payload: dict, **kwargs):
        from agent.tools.approval import ApprovalResult

        self.requests.append((kind, payload))
        decision = "rejected" if kind in self.reject_kinds else self.decision
        return ApprovalResult("appr_test", decision)


def _definition(**overrides) -> ToolDefinition:
    payload = {
        "name": "fetch_weather",
        "description": "查天气",
        "code": "def run(**kwargs):\n    return {'ok': True}\n",
        "tests": [{"name": "t", "input": {}, "expect": {"ok": True}}],
    }
    payload.update(overrides)
    return ToolDefinition(**payload)


def _envs(tmp_path) -> ToolEnvManager:
    return ToolEnvManager(tmp_path / "envs", base_python="C:/python.exe", runner=_FakeRunner())


async def test_tests_run_in_the_managed_environment(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("查天气")
    ws.write_definition(task.id, _definition(requirements=["requests"]))
    sandbox = _RecordingSandbox()
    approvals = _Approvals()
    envs = _envs(tmp_path)
    tool = DevRunTestsTool(ws, sandbox=sandbox, approvals=approvals, envs=envs)

    result = await tool.run(workspace=task.id)

    assert result.ok, result.error
    assert sandbox.executions == 1
    interpreter = sandbox.interpreters[0] or ""
    assert interpreter.replace("\\", "/").endswith(envs.interpreter_name)
    assert any(kind == "dependency_install" for kind, _ in approvals.requests)


async def test_a_refused_install_means_the_tests_do_not_run(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("查天气")
    ws.write_definition(task.id, _definition(requirements=["requests"]))
    sandbox = _RecordingSandbox()
    tool = DevRunTestsTool(
        ws,
        sandbox=sandbox,
        approvals=_Approvals(reject_kinds={"dependency_install"}),
        envs=_envs(tmp_path),
    )

    result = await tool.run(workspace=task.id)

    assert result.ok is False
    assert sandbox.executions == 0
    assert "没有同意" in (result.error or "")


async def test_a_tool_without_requirements_is_unaffected(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("纯标准库工具")
    ws.write_definition(task.id, _definition())
    sandbox = _RecordingSandbox()
    tool = DevRunTestsTool(
        ws, sandbox=sandbox, approvals=_Approvals(), envs=_envs(tmp_path)
    )

    result = await tool.run(workspace=task.id)

    assert result.ok, result.error
    assert sandbox.interpreters == [None]  # 用随包环境


def test_a_registered_tool_refuses_to_run_without_its_environment(tmp_path):
    """已经注册的工具也不能偷偷用随包解释器跑：那会让缺依赖变成一个假成功。"""
    sandbox = _RecordingSandbox()
    tool = CodeTool(
        _definition(requirements=["requests"]), sandbox, envs=_envs(tmp_path)
    )

    result = asyncio.run(tool.run())

    assert result.ok is False
    assert sandbox.executions == 0
    assert "专用环境" in (result.error or "")


def test_a_registered_tool_uses_the_environment_once_ready(tmp_path):
    sandbox = _RecordingSandbox()
    envs = _envs(tmp_path)
    asyncio.run(envs.ensure(["requests"], approvals=_Approvals()))
    tool = CodeTool(_definition(requirements=["requests"]), sandbox, envs=envs)

    result = asyncio.run(tool.run())

    assert result.ok, result.error
    interpreter = sandbox.interpreters[0] or ""
    assert interpreter.replace("\\", "/").endswith(envs.interpreter_name)
