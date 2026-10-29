"""F06：旧 tool_create 子智能体创建审批的预算调整必须真实生效。

历史路径（老 /respond 接口 + overrides.subagent_budget）一直是产品能力：
用户在审批卡里改预算后批准，创建出来的子智能体工具必须使用**用户批准的值**，
而不是提案里的旧值。这里直接验证执行器读取 overrides 的位置
（agent/tools/lifecycle.py）确实把批准值写进了注册的工具定义。

只用 fake/scripted adapter 与进程内沙箱，不联网、不调用真实模型。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from agent.api.server import EventBus
from agent.tools.approval import ApprovalService
from agent.tools.lifecycle import ToolLifecycle
from agent.tools.registry import ToolRegistry
from agent.tools.sandbox import SandboxExecutor
from agent.tools.spec import ToolDefinition


def _subagent_proposal(name: str, budget: dict | None = None) -> dict:
    proposal = {
        "name": name,
        "description": "按主题检索资料的子智能体",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        "code": "",
        "tool_type": "subagent",
        "sync": True,
        "tests": [],
    }
    if budget is not None:
        proposal["subagent_budget"] = dict(budget)
    return proposal


class _ScriptedAdapter:
    mode = "native"

    async def complete(self, messages, tools, **kwargs):
        from agent.adapters.base import ChatMessage, Completion

        return Completion(message=ChatMessage(role="assistant", content="ok"))


class _FakeCreds:
    def get_metadata(self, ref: str):
        return {"status": "active", "id": ref}

    def grant_tool_scope(self, ref: str, tool: str) -> None:
        return None


async def _auto_respond(
    bus: EventBus,
    approvals: ApprovalService,
    *,
    overrides: dict | None = None,
    seen: list | None = None,
):
    """替用户点「允许」：tool_create 带 overrides，其它审批直接批准。"""
    async for chunk in bus.stream():
        for line in chunk.splitlines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            if event["type"] != "APPROVAL_REQUIRED":
                continue
            approval = event["data"]["approval"]
            if seen is not None:
                seen.append(approval)
            if approval.get("kind") == "tool_create" and overrides is not None:
                await approvals.respond(
                    approval["approval_id"], "approved", overrides=overrides
                )
            else:
                await approvals.respond(approval["approval_id"], "approved")


async def _create_subagent_tool(
    db_conn,
    *,
    proposal: dict,
    overrides: dict | None,
) -> tuple[object, list]:
    from agent.tools.subagent_tool import SubagentTool
    from agent.tools.task_manager import TaskManager

    bus = EventBus()
    approvals = ApprovalService(bus, timeout_seconds=5)
    registry = ToolRegistry()
    lifecycle = ToolLifecycle(
        adapter=_ScriptedAdapter(),
        approvals=approvals,
        sandbox=SandboxExecutor(executor="subprocess"),
        registry=registry,
        credentials=_FakeCreds(),
        task_manager=TaskManager(bus, max_concurrent=2),
        adapter_factory=lambda ref, model: None,
    )
    seen: list = []
    task = asyncio.create_task(
        _auto_respond(bus, approvals, overrides=overrides, seen=seen)
    )
    await asyncio.sleep(0.05)
    try:
        outcome = await lifecycle.submit_definition(
            ToolDefinition(**proposal), "按主题检索资料"
        )
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert outcome.ok, outcome.detail
    tool = registry.get(proposal["name"])
    assert isinstance(tool, SubagentTool), type(tool)
    return tool, seen


async def test_user_approved_budget_overrides_are_actually_used(db_conn):
    """用户批准 7/70000/1500 → 注册出来的子智能体工具就是这组值。"""
    tool, seen = await _create_subagent_tool(
        db_conn,
        proposal=_subagent_proposal(
            "budget_override_agent",
            {"max_iterations": 3, "max_tokens": 50000, "output_limit_chars": 1000},
        ),
        overrides={
            "subagent_budget": {
                "max_iterations": 7,
                "max_tokens": 70000,
                "output_limit_chars": 1500,
            }
        },
    )
    budget = tool.definition.subagent_budget
    assert budget.max_iterations == 7, "执行器必须采用用户批准的预算，而不是提案里的 3"
    assert budget.max_tokens == 70000
    assert budget.output_limit_chars == 1500
    # 审批载荷本身给出了可修改的预算输入（前端据此渲染编辑控件）
    create_approvals = [item for item in seen if item.get("kind") == "tool_create"]
    assert create_approvals and create_approvals[0].get("payload", {}).get("tool_type") == "subagent"


async def test_without_override_the_proposal_budget_is_kept(db_conn):
    """不改预算就批准 → 保持提案值（说明上一条的变化确实来自用户批准值）。"""
    tool, _ = await _create_subagent_tool(
        db_conn,
        proposal=_subagent_proposal(
            "budget_default_agent",
            {"max_iterations": 4, "max_tokens": 60000, "output_limit_chars": 1200},
        ),
        overrides=None,
    )
    budget = tool.definition.subagent_budget
    assert budget.max_iterations == 4
    assert budget.max_tokens == 60000
    assert budget.output_limit_chars == 1200


async def test_old_respond_path_still_accepts_overrides(db_conn):
    """老 /respond（ApprovalService.respond）必须继续携带 overrides —— 不能被新接口吞掉。"""
    from agent.api.server import EventBus as _Bus

    bus = _Bus()
    approvals = ApprovalService(bus, timeout_seconds=5)
    seen: list = []
    task = asyncio.create_task(
        _auto_respond(
            bus,
            approvals,
            overrides={"subagent_budget": {"max_iterations": 9}},
            seen=seen,
        )
    )
    await asyncio.sleep(0.05)
    result = await approvals.request("tool_create", {"tool_type": "subagent"})
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert result.decision == "approved"
    assert result.overrides == {"subagent_budget": {"max_iterations": 9}}
