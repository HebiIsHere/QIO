"""Tool creation lifecycle: test -> approve (x2) -> register.

唯一的创建入口是 submit_definition：定义来自开发工作区里的 tool.json（由模型写
文件，不是本类向模型要提案）。历史上还有一条 create_from_request（模型提案 →
交叉测试 → 审批 → 注册）：它在**审批之前**就把 AI 生成的代码真的跑了一遍
（先执行、后确认），而且生产代码里没有任何调用点 —— 已删除；回归测试锁住它
不会回来（tests/test_tool_lifecycle.py）。要接回「让模型提案」这条路，必须先
让它走 tools/dev_auth.py 的执行授权闸门，不能只把方法加回来。

Segments:
1. deterministic cross-testing in the sandbox（调用方 DevSubmitTool 在这之前必须
   拿到执行授权：tools/dev_auth.py）；
2. approval segment 1: tool creation;
3. approval segment 2 (only when a credential is referenced): credential
   grant, which narrows the key scope to this tool;
4. registration side-by-side with builtin tools.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from agent.adapters.base import BaseAdapter
from agent.credentials.store import CredentialStore
from agent.tools.approval import ApprovalService
from agent.tools.dev_tools import (
    PHASE_FAILED,
    PHASE_READY,
    PHASE_REGISTERING,
    PHASE_WAITING_APPROVAL,
    ToolCreateStatus,
    ready_detail,
)
from agent.tools.registry import ToolRegistry
from agent.tools.runtime_tools import CodeTool, SubagentStubTool
from agent.tools.sandbox import SandboxExecutor
from agent.tools.spec import ToolDefinition, ToolProposal
from agent.tools.tester import ToolTester

logger = logging.getLogger(__name__)

APPROVAL_KIND_CREATE = "tool_create"
APPROVAL_KIND_CREDENTIAL = "credential_grant"


@dataclass
class ToolOutcome:
    ok: bool
    tool_name: str | None
    step: str
    detail: str
    # 提交路径上的交叉复测结论（给上层写回任务记录用）。子 agent 型工具没有
    # 确定性测试，这两个字段保持 None。
    test_passed: bool | None = None
    test_summary: str | None = None


class ToolLifecycle:
    def __init__(
        self,
        *,
        adapter: BaseAdapter,
        approvals: ApprovalService,
        sandbox: SandboxExecutor | None = None,
        registry: ToolRegistry,
        credentials: CredentialStore | None = None,
        envs=None,
        task_manager=None,
        retriever=None,
        adapter_factory=None,
        bus=None,
        tool_store=None,
        trace_store=None,
        turn_id_provider=None,
    ) -> None:
        self.approvals = approvals
        self.sandbox = sandbox or SandboxExecutor()
        self.tester = ToolTester(self.sandbox)
        self.registry = registry
        self.credentials = credentials
        # 项目级专用依赖环境：注册后的工具跑它自己的环境（缺环境就不跑）。
        self.envs = envs
        self.task_manager = task_manager
        self.retriever = retriever
        self.adapter_factory = adapter_factory
        self.bus = bus
        self.tool_store = tool_store
        self.trace_store = trace_store
        # 工具创建流程的进度出口（同一 group_id 一张卡）
        self.status = ToolCreateStatus(bus, turn_id_provider)
        # 已注册工具的 disposer，撤销时真正从注册表移除
        self._registry_disposers: dict[str, Callable[[], None]] = {}

    async def submit_definition(
        self,
        definition: ToolDefinition,
        explanation: str,
        *,
        skip_tests: bool = False,
        group_id: str | None = None,
        test_sink: Callable[[bool, str], None] | None = None,
    ) -> ToolOutcome:
        """Direct-submit path (dev workflow): test -> approve -> register.

        The deterministic cross-test is re-run as a double check (the
        submitting agent may have run tests itself). subagent tools skip it.

        `test_sink` 在复测跑完、**任何对外事件之前**被调用一次：上层据此把
        「这一版内容的测试结论」先可靠落盘，再让审批/注册/完成事件出去 ——
        否则进程在事件之后崩掉，任务记录就会停在旧的「测试通过」上。
        """
        report = None
        if not skip_tests and definition.tool_type == "function":
            # 复测也要用「对的那个执行环境」：容器执行器下必须用按锁定清单构建的依赖镜像，
            # 否则会在默认镜像里跑一个依赖不存在的工具（测试必挂，或者更糟：假通过）。
            # 授权顺序不变：调用方（DevSubmitTool）已经先过了 ensure_test_authorization。
            from agent.tools.tool_envs import resolve_execution_environment

            plan = await resolve_execution_environment(
                definition,
                self.envs,
                executor=await self.sandbox.effective_executor(),
                prepare=True,
                approvals=self.approvals,
                tool_name=definition.name,
            )
            if not plan.ok:
                # 依赖环境没准备好就不跑复测：不换成「没有依赖的解释器」制造假结论。
                reason = plan.reason or "依赖环境没准备好，没有执行复测"
                if test_sink is not None:
                    test_sink(False, reason)
                await self.status.emit(
                    group_id,
                    PHASE_FAILED,
                    label="创建失败",
                    detail=reason,
                    ok=False,
                    tool_name=definition.name,
                )
                return ToolOutcome(
                    False,
                    definition.name,
                    "environment",
                    reason,
                    test_passed=False,
                    test_summary=reason,
                )
            report = await self.tester.run(
                definition, interpreter=plan.interpreter, container_image=plan.container_image
            )
            if test_sink is not None:
                test_sink(report.passed, report.summary)
            if not report.passed:
                await self.status.emit(
                    group_id,
                    PHASE_FAILED,
                    label="创建失败",
                    detail="测试没有通过：先让工具把测试跑绿再提交",
                    ok=False,
                    tool_name=definition.name,
                )
                return ToolOutcome(
                    False,
                    definition.name,
                    "test",
                    f"cross-test failed: {report.summary}",
                    test_passed=False,
                    test_summary=report.summary,
                )
        outcome = await self._approve_and_register(
            definition, explanation, report, group_id=group_id
        )
        if report is not None:
            outcome.test_passed = report.passed
            outcome.test_summary = report.summary
        return outcome

    async def _approve_and_register(
        self,
        definition: ToolDefinition,
        explanation: str,
        report,
        *,
        group_id: str | None = None,
    ) -> ToolOutcome:
        """Approval segment 1 (create) + segment 2 (credential) + registration."""
        from agent.tools.policy import default_policy_for, policy_fingerprint

        policy = default_policy_for(definition)
        definition.approved_policy_fingerprint = policy_fingerprint(policy)
        await self.status.emit(
            group_id,
            PHASE_WAITING_APPROVAL,
            label="等待你确认",
            detail="工具会做什么、能访问什么，都写在确认卡里",
            tool_name=definition.name,
        )
        approval = await self.approvals.request(
            APPROVAL_KIND_CREATE,
            {
                "name": definition.name,
                "description": definition.description,
                "explanation": explanation,
                "tool_type": definition.tool_type,
                "credential_ref": definition.credential_ref,
                # 让用户看懂“这个工具会访问什么”，而非内部枚举
                "capabilities": policy.describe(),
                "policy_fingerprint": policy_fingerprint(policy),
                "test_summary": report.summary if report else "n/a (subagent)",
                "test_details": (
                    [
                        {"name": o.name, "passed": o.passed, "detail": o.detail}
                        for o in report.outcomes
                    ]
                    if report
                    else []
                ),
            },
        )
        if approval.decision != "approved":
            detail = (
                "等待确认超时，这次没有创建"
                if approval.decision == "timeout"
                else "你没有同意创建这个工具"
            )
            await self.status.emit(
                group_id,
                PHASE_FAILED,
                label="创建失败",
                detail=detail,
                ok=False,
                tool_name=definition.name,
            )
            return ToolOutcome(
                False, definition.name, "approve", f"creation {approval.decision}"
            )
        if approval.overrides and "subagent_budget" in approval.overrides:
            from agent.tools.spec import SubagentBudget

            definition.subagent_budget = SubagentBudget(**approval.overrides["subagent_budget"])

        # approval segment 2: credential grant (only when referenced)
        if definition.credential_ref:
            if self.credentials is None:
                await self.status.emit(
                    group_id,
                    PHASE_FAILED,
                    label="创建失败",
                    detail="这个工具需要凭据，但当前环境没有可用的凭据库",
                    ok=False,
                    tool_name=definition.name,
                )
                return ToolOutcome(
                    False, definition.name, "credential",
                    "tool references a credential but no credential store is wired",
                )
            meta = self.credentials.get_metadata(definition.credential_ref)
            if meta is None or meta["status"] != "active":
                await self.status.emit(
                    group_id,
                    PHASE_FAILED,
                    label="创建失败",
                    detail="工具引用的凭据现在不可用",
                    ok=False,
                    tool_name=definition.name,
                )
                return ToolOutcome(
                    False, definition.name, "credential",
                    f"referenced credential unavailable: {definition.credential_ref}",
                )
            grant = await self.approvals.request(
                APPROVAL_KIND_CREDENTIAL,
                {
                    "key_id": definition.credential_ref,
                    "tool_name": definition.name,
                    "capabilities": policy.describe(),
                },
            )
            if grant.decision != "approved":
                await self.status.emit(
                    group_id,
                    PHASE_FAILED,
                    label="创建失败",
                    detail="你没有同意这个工具使用该凭据",
                    ok=False,
                    tool_name=definition.name,
                )
                return ToolOutcome(
                    False, definition.name, "credential",
                    f"credential grant {grant.decision}",
                )

        # register side-by-side + persist
        await self.status.emit(
            group_id,
            PHASE_REGISTERING,
            label="正在注册",
            tool_name=definition.name,
        )
        # 顺序：先持久化，再注册。以前是先注册再 save：save 抛磁盘错误时，
        # 提交回「失败」，但工具已经躺在内存注册表里可以调用了 —— 界面说没
        # 成功、系统里却多了一个可用工具。现在任何一步失败都回滚到未注册状态，
        # 并且持久层保留上一可用版本。
        previous: ToolDefinition | None = None
        if self.tool_store is not None:
            previous = self.tool_store.load(definition.name)
        registered = False
        try:
            if self.tool_store is not None:
                self.tool_store.save(definition)
            self._register(definition)
            registered = True
        except Exception as exc:  # noqa: BLE001 - 注册/落库失败都要回到干净状态
            if registered:
                self._unregister(definition.name)
            if self.tool_store is not None:
                try:
                    if previous is not None:
                        self.tool_store.save(previous)
                    else:
                        self.tool_store.remove(definition.name)
                except Exception:  # noqa: BLE001 - 回滚本身失败只记日志
                    logger.warning(
                        "failed to roll back persisted tool %s", definition.name,
                        exc_info=True,
                    )
            await self.status.emit(
                group_id,
                PHASE_FAILED,
                label="创建失败",
                detail=str(exc),
                ok=False,
                tool_name=definition.name,
            )
            return ToolOutcome(False, definition.name, "register", str(exc))
        logger.info("tool created and registered: %s", definition.name)
        await self.status.emit(
            group_id,
            PHASE_READY,
            label="已创建",
            detail=ready_detail(definition, report),
            ok=True,
            tool_name=definition.name,
        )
        return ToolOutcome(True, definition.name, "registered", "ok")

    def _register(self, definition: ToolDefinition) -> None:
        if definition.tool_type == "subagent":
            from agent.tools.subagent_tool import SubagentTool

            if self.task_manager is None or self.adapter_factory is None:
                # 未接线时保持 Stub 降级（不产生半成品注册）
                tool = SubagentStubTool(definition, credentials=self.credentials)
            else:
                tool = SubagentTool(
                    definition,
                    credentials=self.credentials,
                    task_manager=self.task_manager,
                    retriever=self.retriever,
                    adapter_factory=self.adapter_factory,
                    bus=self.bus,
                    trace_store=self.trace_store,
                )
        else:
            tool = CodeTool(
                definition, self.sandbox, credentials=self.credentials, envs=self.envs
            )
        self._registry_disposers[definition.name] = self.registry.register(tool)

    def _unregister(self, name: str) -> None:
        """撤销本次已经完成的注册（回滚用）。

        只认本次注册留下的 disposer：不碰别人注册的同名工具，也不做
        「按名字删一个不知道哪来的工具」这种危险兜底。
        """
        disposer = self._registry_disposers.pop(name, None)
        if disposer is not None:
            disposer()

    def revoke_tool(self, name: str) -> bool:
        """撤销工具：真正从注册表移除（disposer），并在持久层标记 removed。"""
        disposer = self._registry_disposers.pop(name, None)
        if disposer is None:
            if self.registry.get(name) is None:
                return False
            disposer = lambda: self.registry.unregister(name)  # noqa: E731
        disposer()
        if self.tool_store is not None:
            self.tool_store.remove(name)
        logger.info("tool revoked: %s", name)
        return True
