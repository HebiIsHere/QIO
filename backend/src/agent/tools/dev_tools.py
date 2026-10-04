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
    dev_guide,
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
from agent.tools.dev_workspace import (
    RUN_SUBMIT,
    RUN_TESTS,
    STAGE_EXECUTING,
    STAGE_WAITING_APPROVAL,
)
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

# 已放弃的开发任务：工具层一律拒绝。这是**终态**，说辞必须诚实：
# 不说「已停止」（没有停止能力），只说「已经放弃、不会再执行/注册」，
# 并给出下一步（要重做就新建开发任务）。
ABANDONED_ERROR = "这个开发任务已经被放弃：不会再执行、不会再注册。要重做请新建开发任务。"
ABANDONED_FILES_ERROR = "这个开发任务已经被放弃：不能再读写它的工作区文件。要重做请新建开发任务。"


def _dev_facts(workspaces, workspace: str, tool_name: str) -> dict[str, Any]:
    """工具结果里带的开发任务事实（见 core/turn_facts.py）。

    取不到就回空字典：记账失败绝不能让工具调用本身出问题。
    """
    try:
        return workspaces.fact_for(workspace, tool_name)
    except Exception:  # noqa: BLE001 - 记账不是执行的必要条件
        return {}


def _is_abandoned(workspaces, workspace: str) -> bool:
    """任务是否已放弃（判定失败按「没放弃」处理：绝不让守卫本身弄挂工具调用）。"""
    try:
        return bool(workspaces.is_abandoned(workspace))
    except Exception:  # noqa: BLE001 - 守卫不是执行的必要条件
        return False


def _abandoned_result(
    workspaces, workspace: str, tool_name: str, *, error: str = ABANDONED_ERROR
) -> ToolResult:
    return ToolResult(
        ok=False,
        error=error,
        category="abandoned",
        recoverable=False,
        facts=_dev_facts(workspaces, workspace, tool_name),
    )


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


def _with_guide(text: str, step: str) -> str:
    """把这一步该知道的规范附在结果后面（分节注入，不给整份长文）。"""
    guide = dev_guide(step).strip()
    return f"{text}\n{guide}" if guide else text


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
                f"按以下指南继续开发：\n\n{dev_guide('create')}"
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
        if _is_abandoned(self.workspaces, workspace):
            # 读也拒绝：让模型（和界面）不会以为「这个任务还能继续做」。
            return _abandoned_result(
                self.workspaces, workspace, self.name, error=ABANDONED_FILES_ERROR
            )
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
        if _is_abandoned(self.workspaces, workspace):
            # 写必须拒绝：终态之后任何写入都不该发生（也不该让证据复活）。
            return _abandoned_result(
                self.workspaces, workspace, self.name, error=ABANDONED_FILES_ERROR
            )
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
        if _is_abandoned(self.workspaces, workspace):
            return _abandoned_result(
                self.workspaces, workspace, self.name, error=ABANDONED_FILES_ERROR
            )
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
            if status.get("abandoned"):
                # 已放弃的任务**不能**列成「可以继续做」的任务：它已经结束，
                # 继续写/跑/提交都会被拒绝。列出来只是为了让模型知道它存在过。
                lines.append(
                    f"- {task.id} | 已放弃（终态：不会再执行、不会再注册；要重做请新建开发任务）"
                    f" | {task.request[:40]}"
                )
                continue
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
        return ToolResult(
            ok=True,
            content=_with_guide("开发任务：\n" + "\n".join(lines), "recover"),
        )


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
        envs=None,
    ) -> None:
        self.workspaces = workspaces
        self.sandbox = sandbox or SandboxExecutor()
        self.tester = ToolTester(self.sandbox)
        self.approvals = approvals
        # 项目级专用环境（声明了第三方依赖时用）；没有就退回随包环境。
        self.envs = envs
        self.status = ToolCreateStatus(bus, turn_id_provider)

    async def run(self, **kwargs: Any) -> ToolResult:
        workspace = str(kwargs.get("workspace") or "")
        task = self.workspaces.task(workspace)
        if task is None:
            return ToolResult(ok=False, error=f"找不到工作区：{workspace}")
        # 终态守卫：已放弃的任务不会再执行任何生成代码。
        if _is_abandoned(self.workspaces, workspace):
            return _abandoned_result(self.workspaces, workspace, self.name)
        # 登记「这个任务上正在跑一次执行」：放弃接口据此区分「正在执行」与
        # 「正在等审批」。try/finally 保证任何结局（含异常）都会结束登记 ——
        # 留一条僵尸登记会让「正在执行」变成永久事实，用户再也放弃不了。
        #
        # 真正执行代码的那一步必须留在本方法里、且排在授权闸门之后：
        # tests/test_tool_creation_entrypoints.py 按源码顺序守住这条不变量。
        self.workspaces.begin_run(workspace, RUN_TESTS)
        try:
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
            # 这一步可能要在审批上等待：先把阶段标成 waiting_approval，用户此时
            # 放弃是允许的（等审批 ≠ 在执行），未决审批会被接口层一并作废。
            self.workspaces.set_run_stage(workspace, STAGE_WAITING_APPROVAL)
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
            # 授权回来的这一刻再查一次：用户可能正好在这期间点了「放弃开发」。
            # 迟到的执行结果不能让任务复活 —— 这里就是「不执行」的落点。
            if _is_abandoned(self.workspaces, workspace):
                return _abandoned_result(self.workspaces, workspace, self.name)
            self.workspaces.set_run_stage(workspace, STAGE_EXECUTING)
            await self.status.emit(
                workspace,
                PHASE_TESTING,
                label="正在测试",
                tool_name=definition.name,
            )
            # 依赖环境：由执行器决定用宿主专用环境还是容器依赖镜像（同一份判定，
            # 见 tools/tool_envs.py::resolve_execution_environment）。测试阶段可以现准备
            # （安装 / 构建镜像都会先征求许可）；没准备好就明确失败，绝不换成没有依赖的
            # 解释器跑一遍 —— 那会让缺依赖变成一个假通过。
            from agent.tools.tool_envs import resolve_execution_environment

            plan = await resolve_execution_environment(
                definition,
                self.envs,
                executor=await self.sandbox.effective_executor(),
                prepare=True,
                approvals=self.approvals,
                tool_name=definition.name,
                task_id=workspace,
            )
            if not plan.ok:
                await self.status.emit(
                    workspace,
                    PHASE_FAILED,
                    label="依赖环境没准备好",
                    detail=plan.reason or "",
                    ok=False,
                    tool_name=definition.name,
                )
                return ToolResult(
                    ok=False,
                    error=plan.reason or "依赖环境没准备好，这次没有运行测试",
                    category="missing_dependency",
                    facts=_dev_facts(self.workspaces, workspace, self.name),
                )
            # 真正跑生成代码之前最后再查一次（准备依赖环境也可能耗时很久）。
            if _is_abandoned(self.workspaces, workspace):
                return _abandoned_result(self.workspaces, workspace, self.name)
            report = await self.tester.run(
                definition, interpreter=plan.interpreter, container_image=plan.container_image
            )
            lines = [
                f"- {o.name}: {'通过' if o.passed else '失败'} {o.detail}"
                for o in report.outcomes
            ]
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
                    content=_with_guide(
                        _with_note(
                            f"测试通过 {report.summary}\n" + "\n".join(lines), definition
                        ),
                        "test",
                    ),
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
                error=_with_guide(
                    _with_note(f"测试失败 {report.summary}\n" + "\n".join(lines), definition),
                    "test",
                ),
                facts=_dev_facts(self.workspaces, workspace, self.name),
            )
        finally:
            self.workspaces.end_run(workspace, RUN_TESTS)


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
        # 终态守卫：已放弃的任务不会再执行复测，也不会注册工具。
        if _is_abandoned(self.workspaces, workspace):
            return _abandoned_result(self.workspaces, workspace, self.name)
        # 登记一次「正在提交」：与跑测试同一套语义（放弃接口据此判定
        # 「正在执行」→ 拒绝放弃）。try/finally 保证异常也不会留下僵尸登记。
        #
        # 授权闸门（ensure_test_authorization）与执行/注册点（submit_definition）
        # 都留在本方法里，且闸门在前：tests/test_tool_creation_entrypoints.py
        # 按源码顺序守住这条不变量。
        self.workspaces.begin_run(workspace, RUN_SUBMIT)
        try:
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
                        workspace,
                        PHASE_FAILED,
                        label="创建失败",
                        detail="工具定义不是对象",
                        ok=False,
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
                # 这一步可能要在审批上等待（等审批 ≠ 在执行，此时放弃是允许的）。
                self.workspaces.set_run_stage(workspace, STAGE_WAITING_APPROVAL)
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
                # 授权回来的这一刻再查一次：用户可能正好在这期间点了「放弃开发」。
                if _is_abandoned(self.workspaces, workspace):
                    return _abandoned_result(self.workspaces, workspace, self.name)
            # 从这里开始会真的跑复测、并走到注册：阶段是「正在执行」。
            self.workspaces.set_run_stage(workspace, STAGE_EXECUTING)
            digest = self.workspaces.content_digest(workspace)

            def record_retest(passed: bool, summary: str) -> None:
                """提交前的交叉复测结果写回同一个任务。

                复测是在**当前工作区内容**上跑的：它既代替了可能已失效的旧证据，
                也保证「提交用的版本」和「任务记录里的测试结论」是同一版。
                """
                self.workspaces.record_test(workspace, passed, summary)

            def abandoned_guard() -> str | None:
                """注册前守卫：已放弃的任务不允许落盘、不允许进注册表。

                为什么不能只靠工具开头的检查：用户完全可能在等审批的过程中点
                「放弃开发」。那时本调用已经越过开头那道检查，如果注册前不再查一次，
                一个用户已经放弃的工具就会被注册出来。
                """
                return ABANDONED_ERROR if _is_abandoned(self.workspaces, workspace) else None

            try:
                lifecycle = await self.lifecycle_builder()
                # 守卫**注入**到生命周期对象上（ToolLifecycle.__init__ 声明了
                # `abandon_guard`）：这样不动 submit_definition 的签名，已有调用方
                # 与测试替身不会因为多一个参数而报错。
                lifecycle.abandon_guard = abandoned_guard
                # 阶段出口同理：提交路径里「等审批」与「真的跑代码」交替发生，
                # 只有生命周期自己知道现在在哪一段（等审批时放弃是允许的，
                # 跑代码时放弃必须被拒绝）。
                lifecycle.stage_sink = lambda stage: self.workspaces.set_run_stage(
                    workspace, stage
                )
                outcome = await lifecycle.submit_definition(
                    definition,
                    explanation,
                    group_id=workspace,
                    test_sink=record_retest,
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
                    content=_with_guide(
                        f"工具 {definition.name} 已注册（内容摘要 {str(digest)[:12]}）。",
                        "submit",
                    ),
                    facts=_dev_facts(self.workspaces, workspace, self.name),
                )
            return ToolResult(
                ok=False,
                error=f"提交未通过（{outcome.step}）: {outcome.detail}",
                facts=_dev_facts(self.workspaces, workspace, self.name),
            )
        finally:
            self.workspaces.end_run(workspace, RUN_SUBMIT)
