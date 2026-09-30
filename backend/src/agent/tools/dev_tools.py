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
    TOOL_DEV_LIST_TASKS_DESC,
    TOOL_DEV_READ_FILE_DESC,
    TOOL_DEV_RUN_TESTS_DESC,
    TOOL_DEV_SUBMIT_DESC,
    TOOL_DEV_WRITE_FILE_DESC,
)
from agent.tools.base import Tool, ToolResult
from agent.tools.dev_auth import ensure_test_authorization
from agent.tools.sandbox import SandboxExecutor
from agent.tools.spec import ToolDefinition
from agent.tools.tester import ToolTester

# 工具创建流程的阶段（前端按同一 `group_id` 一张卡原地推进）。
# 这些值是协议的一部分：改动要同步前端的状态文案表。
PHASE_PROPOSAL = "proposal"
PHASE_BUILDING = "building"
PHASE_TESTING = "testing"
PHASE_TESTING_PASSED = "testing_passed"
PHASE_TESTING_FAILED = "testing_failed"
PHASE_WAITING_APPROVAL = "waiting_approval"
PHASE_REGISTERING = "registering"
PHASE_READY = "ready"
PHASE_FAILED = "failed"


def _dev_facts(workspaces, workspace: str, tool_name: str) -> dict[str, Any]:
    """工具结果里带的开发任务事实（见 core/turn_facts.py）。

    取不到就回空字典：记账失败绝不能让工具调用本身出问题。
    """
    try:
        return workspaces.fact_for(workspace, tool_name)
    except Exception:  # noqa: BLE001 - 记账不是执行的必要条件
        return {}


def _simulation_note(definition: ToolDefinition) -> str | None:
    """测试不注入真实凭据时必须留痕：免得把「测试通过」当成真实链路验过。"""
    if not getattr(definition, "credential_ref", None):
        return None
    return (
        "说明：本次测试没有注入真实凭据，凭据相关的分支是模拟的 —— "
        "「测试通过」不等于真实服务链路已经验证过。"
    )


def _with_note(text: str, definition: ToolDefinition) -> str:
    note = _simulation_note(definition)
    return f"{text}\n{note}" if note else text


def ready_detail(definition: ToolDefinition | None, report) -> str:
    """「已创建」卡片上的那句话：能证实什么就说什么。

    以前写的是「现在可以使用」—— 注册成功只说明它进了注册表，既不等于
    真实服务跑通过，也不等于用户环境里验证过。这里按实际拿到的东西写：
    确定性测试结果（有就报，没有就不提），以及凭据相关的分支是不是模拟的。
    """
    tool_type = getattr(definition, "tool_type", None)
    if tool_type == "subagent":
        text = "已注册，可以调用（该类型不需要确定性测试）"
    elif report is not None:
        text = f"已注册，可以调用（{report.summary}）"
    else:
        text = "已注册，可以调用"
    if getattr(definition, "credential_ref", None):
        text += "；真实服务未验证（测试不注入凭据）"
    return text


class ToolCreateStatus:
    """工具创建进度的事件出口：同一 `group_id` 就是同一张卡。

    `label` / `detail` 是要给用户看的中文短句：不放源代码、不放内部路径。
    没有 bus（老装配、或单元测试直接构造工具）时是空实现：不发事件、不抛错。
    """

    def __init__(self, bus=None, turn_id_provider=None) -> None:
        self.bus = bus
        self.turn_id_provider = turn_id_provider

    async def emit(
        self,
        group_id: str | None,
        phase: str,
        *,
        label: str,
        detail: str | None = None,
        ok: bool | None = None,
        tool_name: str | None = None,
    ) -> None:
        if self.bus is None or not group_id:
            return
        from agent.api.events import EventType, make_event

        data: dict[str, Any] = {
            "group_id": group_id,
            "phase": phase,
            "tool_name": tool_name,
            "label": label,
            "detail": detail,
            "ok": ok,
        }
        turn_id = self.turn_id_provider() if self.turn_id_provider is not None else None
        if turn_id:
            data["turn_id"] = turn_id
        await self.bus.publish(make_event(EventType.TOOL_CREATE_STATUS, data))


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

    def __init__(self, workspaces, bus=None, turn_id_provider=None) -> None:
        self.workspaces = workspaces
        self.status = ToolCreateStatus(bus, turn_id_provider)

    async def run(self, **kwargs: Any) -> ToolResult:
        request = str(kwargs.get("request") or "").strip()
        if not request:
            return ToolResult(ok=False, error="request 必填")
        task = self.workspaces.create(request)
        await self.status.emit(
            task.id,
            PHASE_PROPOSAL,
            label="已收到创建需求",
            detail="正在准备开发工作区",
        )
        files = self.workspaces.list_files(task.id)
        return ToolResult(
            ok=True,
            content=(
                f"开发任务已创建，工作区 id={task.id}。"
                f"工作区已有文件：{', '.join(files) or '(空)'}。\n"
                f"按以下指南继续开发：\n\n{DEV_GUIDE}"
            ),
            facts=_dev_facts(self.workspaces, task.id, self.name),
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
            "name": {
                "type": "string",
                "description": "工作区内的相对路径（可用子目录，如 pkg/util.py；不能绝对路径或含 ..）",
            },
            "content": {"type": "string", "description": "文件内容"},
        },
        "required": ["workspace", "name", "content"],
    }

    def __init__(self, workspaces, bus=None, turn_id_provider=None) -> None:
        self.workspaces = workspaces
        self.status = ToolCreateStatus(bus, turn_id_provider)

    async def run(self, **kwargs: Any) -> ToolResult:
        workspace = str(kwargs.get("workspace") or "")
        name = str(kwargs.get("name") or "")
        content = str(kwargs.get("content") or "")
        if self.workspaces.task(workspace) is None:
            return ToolResult(ok=False, error=f"找不到工作区：{workspace}")
        try:
            self.workspaces.write_file(workspace, name, content)
        except (ValueError, KeyError) as exc:
            return ToolResult(
                ok=False,
                error=str(exc),
                facts=_dev_facts(self.workspaces, workspace, self.name),
            )
        await self.status.emit(
            workspace,
            PHASE_BUILDING,
            label="正在构建",
            detail=f"已写入 {len(self.workspaces.list_files(workspace))} 个文件",
        )
        return ToolResult(
            ok=True,
            content=f"已写入 {name}（{len(content)} 字符）",
            facts=_dev_facts(self.workspaces, workspace, self.name),
        )


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


class DevListTasksTool(Tool):
    """列出开发任务及权威状态：给 agent 一个「我有哪些未完成的开发」入口。

    重启、断线后模型/界面都能靠它枚举任务并恢复上下文，不再依赖对话摘要里的
    workspace id。状态来自工作区 state.json，文件存在不等于测试通过。
    """

    name = "dev_list_tasks"
    description = TOOL_DEV_LIST_TASKS_DESC
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, workspaces) -> None:
        self.workspaces = workspaces

    async def run(self, **kwargs: Any) -> ToolResult:
        tasks = self.workspaces.list_tasks()
        if not tasks:
            return ToolResult(ok=True, content="当前没有开发任务。")
        lines: list[str] = []
        for task in tasks:
            status = self.workspaces.status(task.id)
            if status.get("test_evidence_current"):
                test = "测试通过" if status["last_test_passed"] else "测试失败"
            elif status.get("last_test_passed") is None:
                test = "未测试"
            else:
                # 有结论但内容已经改过：旧结论不再是可用证据，必须照实说。
                test = (
                    "测试通过（内容已变更，证据失效，需重跑）"
                    if status["last_test_passed"]
                    else "测试失败（内容已变更，证据失效）"
                )
            phase = status.get("phase") or "未知"
            submitted = "已提交" if status.get("submitted") else "未提交"
            lines.append(
                f"- {task.id} | 阶段 {phase} | {test} | {submitted} | {task.request[:40]}"
            )
        return ToolResult(ok=True, content="开发任务：\n" + "\n".join(lines))


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

    def __init__(
        self,
        workspaces,
        sandbox: SandboxExecutor | None = None,
        approvals=None,
        bus=None,
        turn_id_provider=None,
    ) -> None:
        self.workspaces = workspaces
        self.sandbox = sandbox or SandboxExecutor()
        self.tester = ToolTester(self.sandbox)
        self.approvals = approvals
        self.status = ToolCreateStatus(bus, turn_id_provider)

    async def run(self, **kwargs: Any) -> ToolResult:
        workspace = str(kwargs.get("workspace") or "")
        task = self.workspaces.task(workspace)
        if task is None:
            return ToolResult(ok=False, error=f"找不到工作区：{workspace}")
        # 测试跑的是**完整项目**（清单 + 工作区里的其它文件），不是单段代码：
        # 否则多文件项目在测试阶段就已经 import 不到自己的模块。
        definition = self.workspaces.collect_definition(workspace)
        if definition is None:
            return ToolResult(
                ok=False,
                error="tool.json 缺失或无效，请先写入工具定义",
                facts=_dev_facts(self.workspaces, workspace, self.name),
            )
        if definition.tool_type == "subagent":
            # 子 agent 型工具没有确定性测试，直接进入确认环节
            self.workspaces.set_phase(workspace, "no_tests_required")
            return ToolResult(
                ok=True,
                content="subagent 型工具无需确定性测试，可直接提交审批。",
                facts=_dev_facts(self.workspaces, workspace, self.name),
            )
        # 执行 AI 生成的代码之前必须拿到用户对执行边界的确认（按任务一次）。
        blocked = await ensure_test_authorization(
            workspaces=self.workspaces,
            approvals=self.approvals,
            sandbox=self.sandbox,
            task_id=workspace,
            definition=definition,
        )
        if blocked is not None:
            await self.status.emit(
                workspace,
                PHASE_FAILED,
                label="需要你的确认",
                detail="没有获得执行确认，这次没有运行测试",
                ok=False,
                tool_name=definition.name,
            )
            blocked.facts = _dev_facts(self.workspaces, workspace, self.name)
            return blocked
        await self.status.emit(
            workspace,
            PHASE_TESTING,
            label="正在测试",
            tool_name=definition.name,
        )
        report = await self.tester.run(definition)
        lines = [f"- {o.name}: {'通过' if o.passed else '失败'} {o.detail}" for o in report.outcomes]
        # 权威测试记录：通过与否、摘要、时刻落盘（重启后可查，且不被文件存在推断）。
        self.workspaces.record_test(workspace, report.passed, report.summary)
        if report.passed:
            await self.status.emit(
                workspace,
                PHASE_TESTING_PASSED,
                label="测试通过",
                detail=report.summary,
                ok=True,
                tool_name=definition.name,
            )
            return ToolResult(
                ok=True,
                content=_with_note(f"测试通过 {report.summary}\n" + "\n".join(lines), definition),
                facts=_dev_facts(self.workspaces, workspace, self.name),
            )
        await self.status.emit(
            workspace,
            PHASE_TESTING_FAILED,
            label="测试失败",
            detail=report.summary,
            ok=False,
            tool_name=definition.name,
        )
        return ToolResult(
            ok=False,
            error=_with_note(f"测试失败 {report.summary}\n" + "\n".join(lines), definition),
            facts=_dev_facts(self.workspaces, workspace, self.name),
        )


class DevSubmitTool(Tool):
    name = "dev_submit_tool"
    description = TOOL_DEV_SUBMIT_DESC
    parameters = {
        "type": "object",
        "properties": {
            "workspace": {"type": "string", "description": "工作区 id"},
            "definition": {
                "type": "object",
                "description": "可选。仅在需要核对时传入，必须与工作区 tool.json 一致；提交以工作区文件为准",
            },
            "explanation": {"type": "string", "description": "通俗的用途说明（≤300 字）"},
        },
        "required": ["workspace", "explanation"],
    }

    def __init__(
        self,
        workspaces,
        lifecycle_builder: Callable[[], Awaitable[Any]],
        sandbox: SandboxExecutor | None = None,
        approvals=None,
        bus=None,
        turn_id_provider=None,
    ) -> None:
        self.workspaces = workspaces
        self.lifecycle_builder = lifecycle_builder
        self.sandbox = sandbox or SandboxExecutor()
        self.approvals = approvals
        self.status = ToolCreateStatus(bus, turn_id_provider)

    async def run(self, **kwargs: Any) -> ToolResult:
        workspace = str(kwargs.get("workspace") or "")
        explanation = str(kwargs.get("explanation") or "").strip()
        raw = kwargs.get("definition")
        if self.workspaces.task(workspace) is None:
            return ToolResult(ok=False, error=f"找不到工作区：{workspace}")
        if not explanation:
            await self.status.emit(
                workspace, PHASE_FAILED, label="创建失败", detail="缺少用途说明", ok=False
            )
            return ToolResult(
                ok=False, error="explanation 必填",
                facts=_dev_facts(self.workspaces, workspace, self.name),
            )
        # 以工作区 tool.json 为唯一权威：模型不必再复述整份定义（重复输出容易与
        # 工作区文件、测试对象不一致）。若仍传了 definition，只用于核对一致性。
        # 清单（tool.json）是模型写的；完整定义 = 清单 + 工作区里的项目文件。
        # 注册的必须是完整定义，否则注册后的工具 import 不到自己的模块。
        manifest = self.workspaces.read_definition(workspace)
        definition = self.workspaces.collect_definition(workspace)
        if manifest is None or definition is None:
            await self.status.emit(
                workspace,
                PHASE_FAILED,
                label="创建失败",
                detail="工作区里的 tool.json 缺失或无效",
                ok=False,
            )
            return ToolResult(
                ok=False,
                error="工作区里的 tool.json 缺失或无效，请先写入工具定义",
                facts=_dev_facts(self.workspaces, workspace, self.name),
            )
        if raw is not None:
            if not isinstance(raw, dict):
                await self.status.emit(
                    workspace, PHASE_FAILED, label="创建失败", detail="工具定义不是对象", ok=False
                )
                return ToolResult(
                    ok=False, error="definition 必须是对象",
                    facts=_dev_facts(self.workspaces, workspace, self.name),
                )
            try:
                provided = ToolDefinition(**raw)
            except Exception as exc:  # noqa: BLE001 - pydantic validation
                await self.status.emit(
                    workspace,
                    PHASE_FAILED,
                    label="创建失败",
                    detail=f"工具定义不合法：{exc}",
                    ok=False,
                )
                return ToolResult(
                    ok=False, error=f"definition 不合法：{exc}",
                    facts=_dev_facts(self.workspaces, workspace, self.name),
                )
            # 只核对模型写的清单：项目文件是后端从工作区收集的，不在模型职责内。
            if provided.model_dump() != manifest.model_dump():
                await self.status.emit(
                    workspace,
                    PHASE_FAILED,
                    label="创建失败",
                    detail="传入的定义与工作区 tool.json 不一致",
                    ok=False,
                )
                return ToolResult(
                    ok=False,
                    error=(
                        "definition 与工作区 tool.json 不一致；提交以工作区为准。"
                        "请先用 dev_read_file 读取 tool.json 核对，或改为省略 definition。"
                    ),
                    facts=_dev_facts(self.workspaces, workspace, self.name),
                )
        if definition.tool_type == "function":
            # 提交时的交叉复测同样会执行生成代码：没拿到执行确认就不能往下走。
            blocked = await ensure_test_authorization(
                workspaces=self.workspaces,
                approvals=self.approvals,
                sandbox=self.sandbox,
                task_id=workspace,
                definition=definition,
            )
            if blocked is not None:
                await self.status.emit(
                    workspace,
                    PHASE_FAILED,
                    label="需要你的确认",
                    detail="没有获得执行确认，这次没有提交",
                    ok=False,
                    tool_name=definition.name,
                )
                blocked.facts = _dev_facts(self.workspaces, workspace, self.name)
                return blocked
        digest = self.workspaces.content_digest(workspace)

        def record_retest(passed: bool, summary: str) -> None:
            """提交前的交叉复测结果写回同一个任务。

            复测是在**当前工作区内容**上跑的：它既代替了可能已失效的旧证据，
            也保证「提交用的版本」和「任务记录里的测试结论」是同一版。
            """
            self.workspaces.record_test(workspace, passed, summary)

        try:
            lifecycle = await self.lifecycle_builder()
            outcome = await lifecycle.submit_definition(
                definition, explanation, group_id=workspace, test_sink=record_retest
            )
        except Exception as exc:  # noqa: BLE001 - isolation
            await self.status.emit(
                workspace,
                PHASE_FAILED,
                label="创建失败",
                detail="提交时发生错误，请稍后重试",
                ok=False,
            )
            return ToolResult(
                ok=False,
                error=f"提交失败：{type(exc).__name__}: {exc}",
                facts=_dev_facts(self.workspaces, workspace, self.name),
            )
        if outcome.ok:
            # 完成的任务必须留下来：项目文件、需求与测试证据是重启、更新和
            # 修复的依据。以前这里直接 rmtree，工具注册了但工作区没了，
            # 任务再也枚举不到。
            self.workspaces.mark_submitted(workspace)
            self.workspaces.archive(workspace)
            return ToolResult(
                ok=True,
                content=f"工具 {definition.name} 已注册（内容摘要 {str(digest)[:12]}）。",
                facts=_dev_facts(self.workspaces, workspace, self.name),
            )
        return ToolResult(
            ok=False,
            error=f"提交未通过（{outcome.step}）: {outcome.detail}",
            facts=_dev_facts(self.workspaces, workspace, self.name),
        )
