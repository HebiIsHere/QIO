"""Dev workflow tools: create_tool + dev_write_file/read_file/run_tests/submit_tool.

The main agent develops tools like an engineer: create a workspace, write
implementation + tests, run tests, iterate on failures, then submit for
approval. All technical detail stays inside tool cards (collapsed for the
user); the approval dialog is user-friendly.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from agent.prompts import (
    DEV_GUIDE,
    TOOL_CREATE_TOOL_DESC,
    TOOL_DEV_LIST_FILES_DESC,
    TOOL_DEV_READ_FILE_DESC,
    TOOL_DEV_RUN_TESTS_DESC,
    TOOL_DEV_SUBMIT_DESC,
    TOOL_DEV_WRITE_FILE_DESC,
)
from agent.tools.base import Tool, ToolResult
from agent.tools.sandbox import SandboxExecutor
from agent.tools.spec import ToolDefinition
from agent.tools.tester import ToolTester


class CreateToolTool(Tool):
    name = "create_tool"
    description = TOOL_CREATE_TOOL_DESC
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
            return ToolResult(ok=False, error="request 必填")
        task = self.workspaces.create(request)
        files = self.workspaces.list_files(task.id)
        return ToolResult(
            ok=True,
            content=(
                f"开发任务已创建，工作区 id={task.id}。"
                f"工作区已有文件：{', '.join(files) or '(空)'}。\n"
                f"按以下指南继续开发：\n\n{DEV_GUIDE}"
            ),
        )


class DevListFilesTool(Tool):
    name = "dev_list_files"
    description = TOOL_DEV_LIST_FILES_DESC
    parameters = {
        "type": "object",
        "properties": {
            "workspace": {"type": "string", "description": "工作区 id"},
        },
        "required": ["workspace"],
    }

    def __init__(self, workspaces) -> None:
        self.workspaces = workspaces

    async def run(self, **kwargs: Any) -> ToolResult:
        workspace = str(kwargs.get("workspace") or "")
        if self.workspaces.task(workspace) is None:
            return ToolResult(ok=False, error=f"找不到工作区：{workspace}")
        files = self.workspaces.list_files(workspace)
        if not files:
            return ToolResult(ok=True, content="工作区为空，尚无文件。")
        return ToolResult(
            ok=True,
            content="工作区文件：\n" + "\n".join(f"- {name}" for name in files),
        )


class DevWriteFileTool(Tool):
    name = "dev_write_file"
    description = TOOL_DEV_WRITE_FILE_DESC
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
            return ToolResult(ok=False, error=f"找不到工作区：{workspace}")
        try:
            self.workspaces.write_file(workspace, name, content)
        except (ValueError, KeyError) as exc:
            return ToolResult(ok=False, error=str(exc))
        return ToolResult(ok=True, content=f"已写入 {name}（{len(content)} 字符）")


class DevReadFileTool(Tool):
    name = "dev_read_file"
    description = TOOL_DEV_READ_FILE_DESC
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
            return ToolResult(ok=False, error=f"找不到工作区：{workspace}")
        content = self.workspaces.read_file(workspace, name)
        if content is None:
            return ToolResult(ok=False, error=f"找不到文件：{name}")
        return ToolResult(ok=True, content=content)


class DevRunTestsTool(Tool):
    name = "dev_run_tests"
    description = TOOL_DEV_RUN_TESTS_DESC
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
            return ToolResult(ok=False, error=f"找不到工作区：{workspace}")
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
    description = TOOL_DEV_SUBMIT_DESC
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
            return ToolResult(ok=False, error=f"找不到工作区：{workspace}")
        if not explanation:
            return ToolResult(ok=False, error="explanation 必填")
        if not isinstance(raw, dict):
            return ToolResult(ok=False, error="definition 必须是对象")
        try:
            definition = ToolDefinition(**raw)
        except Exception as exc:  # noqa: BLE001 - pydantic validation
            return ToolResult(ok=False, error=f"definition 不合法：{exc}")
        try:
            lifecycle = await self.lifecycle_builder()
            outcome = await lifecycle.submit_definition(definition, explanation)
        except Exception as exc:  # noqa: BLE001 - isolation
            return ToolResult(ok=False, error=f"提交失败：{type(exc).__name__}: {exc}")
        if outcome.ok:
            self.workspaces.cleanup(workspace)
            return ToolResult(ok=True, content=f"工具 {definition.name} 已注册。")
        return ToolResult(ok=False, error=f"提交未通过（{outcome.step}）: {outcome.detail}")
