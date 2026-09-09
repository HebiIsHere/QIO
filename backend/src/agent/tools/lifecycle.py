"""Tool creation lifecycle: explain -> test -> approve (x2) -> register.

Segments:
1. proposal with explanation (main model);
2. deterministic cross-testing in the sandbox;
3. approval segment 1: tool creation;
4. approval segment 2 (only when a credential is referenced): credential
   grant, which narrows the key scope to this tool;
5. registration side-by-side with builtin tools.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from agent.adapters.base import BaseAdapter
from agent.credentials.store import CredentialStore
from agent.tools.approval import ApprovalService
from agent.tools.creator import ToolCreator
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


class ToolLifecycle:
    def __init__(
        self,
        *,
        adapter: BaseAdapter,
        approvals: ApprovalService,
        sandbox: SandboxExecutor | None = None,
        registry: ToolRegistry,
        credentials: CredentialStore | None = None,
        task_manager=None,
        retriever=None,
        adapter_factory=None,
        bus=None,
        tool_store=None,
    ) -> None:
        self.creator = ToolCreator(adapter)
        self.approvals = approvals
        self.sandbox = sandbox or SandboxExecutor()
        self.tester = ToolTester(self.sandbox)
        self.registry = registry
        self.credentials = credentials
        self.task_manager = task_manager
        self.retriever = retriever
        self.adapter_factory = adapter_factory
        self.bus = bus
        self.tool_store = tool_store
        # 已注册工具的 disposer，撤销时真正从注册表移除
        self._registry_disposers: dict[str, Callable[[], None]] = {}

    async def create_from_request(
        self, user_request: str, *, context: str | None = None
    ) -> ToolOutcome:
        # 1. proposal with explanation
        proposal, error = await self.creator.propose(user_request, context)
        if proposal is None:
            return ToolOutcome(False, None, "propose", error or "proposal failed")
        definition = proposal.tool

        # 2. deterministic cross-testing (function tools only; subagent
        #    tools have a separate contract in v1.5)
        report = None
        if definition.tool_type == "function":
            report = await self.tester.run(definition)
            if not report.passed:
                return ToolOutcome(
                    False,
                    definition.name,
                    "test",
                    f"cross-test failed: {report.summary}",
                )

        # 3-5. approval (x2) + registration (shared with submit_definition)
        return await self._approve_and_register(definition, proposal.explanation, report)

    async def submit_definition(
        self,
        definition: ToolDefinition,
        explanation: str,
        *,
        skip_tests: bool = False,
    ) -> ToolOutcome:
        """Direct-submit path (dev workflow): test -> approve -> register.

        The deterministic cross-test is re-run as a double check (the
        submitting agent may have run tests itself). subagent tools skip it.
        """
        report = None
        if not skip_tests and definition.tool_type == "function":
            report = await self.tester.run(definition)
            if not report.passed:
                return ToolOutcome(
                    False,
                    definition.name,
                    "test",
                    f"cross-test failed: {report.summary}",
                )
        return await self._approve_and_register(definition, explanation, report)

    async def _approve_and_register(
        self,
        definition: ToolDefinition,
        explanation: str,
        report,
    ) -> ToolOutcome:
        """Approval segment 1 (create) + segment 2 (credential) + registration."""
        approval = await self.approvals.request(
            APPROVAL_KIND_CREATE,
            {
                "name": definition.name,
                "description": definition.description,
                "explanation": explanation,
                "tool_type": definition.tool_type,
                "credential_ref": definition.credential_ref,
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
            return ToolOutcome(
                False, definition.name, "approve", f"creation {approval.decision}"
            )
        if approval.overrides and "subagent_budget" in approval.overrides:
            from agent.tools.spec import SubagentBudget

            definition.subagent_budget = SubagentBudget(**approval.overrides["subagent_budget"])

        # approval segment 2: credential grant (only when referenced)
        if definition.credential_ref:
            if self.credentials is None:
                return ToolOutcome(
                    False, definition.name, "credential",
                    "tool references a credential but no credential store is wired",
                )
            meta = self.credentials.get_metadata(definition.credential_ref)
            if meta is None or meta["status"] != "active":
                return ToolOutcome(
                    False, definition.name, "credential",
                    f"referenced credential unavailable: {definition.credential_ref}",
                )
            grant = await self.approvals.request(
                APPROVAL_KIND_CREDENTIAL,
                {"key_id": definition.credential_ref, "tool_name": definition.name},
            )
            if grant.decision != "approved":
                return ToolOutcome(
                    False, definition.name, "credential",
                    f"credential grant {grant.decision}",
                )

        # register side-by-side + persist
        try:
            self._register(definition)
            if self.tool_store is not None:
                self.tool_store.save(definition)
        except ValueError as exc:
            return ToolOutcome(False, definition.name, "register", str(exc))
        logger.info("tool created and registered: %s", definition.name)
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
                )
        else:
            tool = CodeTool(definition, self.sandbox, credentials=self.credentials)
        self._registry_disposers[definition.name] = self.registry.register(tool)

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
