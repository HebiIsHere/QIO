"""Dev workflow tools: create_tool + dev_write_file/read_file/run_tests/submit_tool.

The main agent develops tools like an engineer: create a workspace, write
implementation + tests, run tests, iterate on failures, then submit for
approval. All technical detail stays inside tool cards (collapsed for the
user); the approval dialog is user-friendly.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from agent.tools.base import Tool, ToolResult
from agent.tools.sandbox import SandboxExecutor
from agent.tools.spec import ToolDefinition
from agent.tools.tester import ToolTester

DEV_GUIDE = """工具开发指南：
1. 开发范式：理解需求 → 在 tool.json 写工具定义（name/description/parameters/tool_type/sync/code/tests）→ 运行测试 → 失败则读取错误、修改定义、重跑，直至全部通过 → 提交审批。
2. 需求规格必填项：工具用途（一句话）、输入输出、使用场景、是否需要凭据（访问外部服务时）、类型（function/subagent）、同步/异步。需求不完整时，先与用户澄清再开始开发。
3. 契约约束：纯函数、单文件实现、测试用例 ≥1 条且确定性断言；subagent 型需 model 与 credential_ref，可跳过确定性测试。
4. 提交时用通俗语言说明工具用途（用户不接触代码）。"""


class CreateToolTool(Tool):
    name = "create_tool"
    description = (
        "创建新工具的开发任务。当用户要求开发新工具、现有工具无法满足需求时使用。"
        "需求不完整时先向用户澄清，再调用本工具创建开发任务。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "request": {"type": "string", "description": "用户需求（含用途/输入输出/场景/凭据需求/类型）"},
        },
        "required": ["request"],
    }

    def __init__(self, workspaces) -> None:
        self.workspaces = workspaces

    async def run(self, **kwargs: Any) -> ToolResult:
        request = str(kwargs.get("request") or "").strip()
        if not request:
            return ToolResult(ok=False, error="request required")
        task = self.workspaces.create(request)
        return ToolResult(
            ok=True,
            content=f"开发任务已创建，工作区 id={task.id}。\n\n{DEV_GUIDE}",
        )


class DevWriteFileTool(Tool):
    name = "dev_write_file"
    description = "在工作区写入或修改文件（tool.json / tool.py / tests.json 等）。"
    parameters = {
        "type": "object",
        "properties": {
            "workspace": {"type": "string", "description": "工作区 id"},
            "name": {"type": "string", "description": "文件名（单文件，不允许路径）"},
            "content": {"type": "string", "description": "文件内容"},
        },
        "required": ["workspace", "name", "content"],
    }

    def __init__(self, workspaces) -> None:
        self.workspaces = workspaces

    async def run(self, **kwargs: Any) -> ToolResult:
        workspace = str(kwargs.get("workspace") or "")
        name = str(kwargs.get("name") or "")
        content = str(kwargs.get("content") or "")
        if self.workspaces.task(workspace) is None:
            return ToolResult(ok=False, error=f"workspace not found: {workspace}")
        try:
            self.workspaces.write_file(workspace, name, content)
        except (ValueError, KeyError) as exc:
            return ToolResult(ok=False, error=str(exc))
        return ToolResult(ok=True, content=f"已写入 {name}（{len(content)} 字符）")


class DevReadFileTool(Tool):
    name = "dev_read_file"
    description = "读取工作区文件内容（查看当前实现或测试）。"
    parameters = {
        "type": "object",
        "properties": {
            "workspace": {"type": "string", "description": "工作区 id"},
            "name": {"type": "string", "description": "文件名"},
        },
        "required": ["workspace", "name"],
    }

    def __init__(self, workspaces) -> None:
        self.workspaces = workspaces

    async def run(self, **kwargs: Any) -> ToolResult:
        workspace = str(kwargs.get("workspace") or "")
        name = str(kwargs.get("name") or "")
        if self.workspaces.task(workspace) is None:
            return ToolResult(ok=False, error=f"workspace not found: {workspace}")
        content = self.workspaces.read_file(workspace, name)
        if content is None:
            return ToolResult(ok=False, error=f"file not found: {name}")
        return ToolResult(ok=True, content=content)


class DevRunTestsTool(Tool):
    name = "dev_run_tests"
    description = "在工作区运行工具测试（读取 tool.json 的定义与测试，沙箱执行），返回逐条结果。失败时根据错误输出修改后重跑。"
    parameters = {
        "type": "object",
        "properties": {
            "workspace": {"type": "string", "description": "工作区 id"},
        },
        "required": ["workspace"],
    }

    def __init__(self, workspaces, sandbox: SandboxExecutor | None = None) -> None:
        self.workspaces = workspaces
        self.tester = ToolTester(sandbox or SandboxExecutor())

    async def run(self, **kwargs: Any) -> ToolResult:
        workspace = str(kwargs.get("workspace") or "")
        task = self.workspaces.task(workspace)
        if task is None:
            return ToolResult(ok=False, error=f"workspace not found: {workspace}")
        definition = self.workspaces.read_definition(workspace)
        if definition is None:
            return ToolResult(ok=False, error="tool.json 缺失或无效，请先写入工具定义")
        task.test_runs += 1
        if definition.tool_type == "subagent":
            return ToolResult(ok=True, content="subagent 型工具无需确定性测试，可直接提交审批。")
        report = await self.tester.run(definition)
        lines = [f"- {o.name}: {'通过' if o.passed else '失败'} {o.detail}" for o in report.outcomes]
        if report.passed:
            return ToolResult(ok=True, content=f"测试通过 {report.summary}\n" + "\n".join(lines))
        return ToolResult(
            ok=False,
            error=f"测试失败 {report.summary}\n" + "\n".join(lines),
        )


class DevSubmitTool(Tool):
    name = "dev_submit_tool"
    description = (
        "提交工具开发成果进入审批。definition 为工具定义 JSON（含 name/description/parameters/工具类型/实现/测试），"
        "explanation 用通俗语言说明工具用途（用户会看到）。审批通过后工具注册，工作区清理。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "workspace": {"type": "string", "description": "工作区 id"},
            "definition": {"type": "object", "description": "工具定义 JSON"},
            "explanation": {"type": "string", "description": "通俗的用途说明（≤300 字）"},
        },
        "required": ["workspace", "definition", "explanation"],
    }

    def __init__(self, workspaces, lifecycle_builder: Callable[[], Awaitable[Any]]) -> None:
        self.workspaces = workspaces
        self.lifecycle_builder = lifecycle_builder

    async def run(self, **kwargs: Any) -> ToolResult:
        workspace = str(kwargs.get("workspace") or "")
        explanation = str(kwargs.get("explanation") or "").strip()
        raw = kwargs.get("definition")
        if self.workspaces.task(workspace) is None:
            return ToolResult(ok=False, error=f"workspace not found: {workspace}")
        if not explanation:
            return ToolResult(ok=False, error="explanation required")
        if not isinstance(raw, dict):
            return ToolResult(ok=False, error="definition must be an object")
        try:
            definition = ToolDefinition(**raw)
        except Exception as exc:  # noqa: BLE001 - pydantic validation
            return ToolResult(ok=False, error=f"definition invalid: {exc}")
        try:
            lifecycle = await self.lifecycle_builder()
            outcome = await lifecycle.submit_definition(definition, explanation)
        except Exception as exc:  # noqa: BLE001 - isolation
            return ToolResult(ok=False, error=f"submit failed: {type(exc).__name__}: {exc}")
        if outcome.ok:
            self.workspaces.cleanup(workspace)
            return ToolResult(ok=True, content=f"工具 {definition.name} 已注册。")
        return ToolResult(ok=False, error=f"提交未通过（{outcome.step}）: {outcome.detail}")
