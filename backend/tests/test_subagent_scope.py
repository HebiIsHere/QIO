"""子智能体能力边界（契约 §2.3/§2.4）：子集、不能扩权、注册不产生长期权限。

用轻量 fake gateway 记录 effect / 返回预设 decision；不复制权限判定。
"""

from __future__ import annotations

import types

import pytest

from agent.tools import security_gate
from agent.tools.base import ToolResult
from agent.tools.runtime_tools import CodeTool
from agent.tools.sandbox import SandboxResult
from agent.tools.security_types import AgentScope, AuthDecision, Verdict
from agent.tools.spec import ToolDefinition
from agent.tools.subagent_tool import SubagentTool
from agent.tools.task_manager import TaskManager


class FakeBus:
    def __init__(self) -> None:
        self.published: list = []

    async def publish(self, event) -> None:
        self.published.append(event)


def _parent_instruction():
    return types.SimpleNamespace(
        instruction_id="instr_1",
        text="修复 D:\\work 里的这个项目",
        turn_id="turn_1",
        topic_id=None,
        created_at="2026-10-22T00:00:00+00:00",
        grants=[
            types.SimpleNamespace(grant_id="grant_1", revoked=False),
            types.SimpleNamespace(grant_id="grant_2", revoked=False),
            types.SimpleNamespace(grant_id="grant_revoked", revoked=True),
        ],
    )


class FakeGateway:
    """记录 effect / verdict / 当前 AgentScope；可选实现 child_scope。"""

    def __init__(self, *, mode="restricted", verdicts=None, child_scope=None) -> None:
        self._mode = mode
        self.verdicts = dict(verdicts or {})
        self.child_scope_factory = child_scope
        self.instructions = [_parent_instruction()]
        self.calls: list = []
        self.scopes_seen: list = []
        self.submitted: list = []
        self.internal_data_paths: list = []

    def mode(self):
        return self._mode

    def instruction(self):
        return self.instructions[0] if self.instructions else None

    def child_scope(self, *, purpose: str):
        self.scopes_seen.append(purpose)
        if self.child_scope_factory is not None:
            return self.child_scope_factory(purpose)
        return None

    async def authorize(self, effect, *, ctx=None):
        self.calls.append(effect)
        self.scopes_seen.append(security_gate.current_agent_scope())
        kind = str(getattr(getattr(effect, "kind", None), "value", effect.kind))
        return AuthDecision(verdict=self.verdicts.get(kind, Verdict.ALLOW), reason=f"fake:{kind}")

    async def submit(self, operation, *, ctx=None):
        self.submitted.append(operation)
        return types.SimpleNamespace(
            approval_id="appr_fake",
            state="waiting",
            expires_at="2099-01-01T00:00:00+00:00",
            confirmations_required=operation.confirmations_required,
        )


class FakeCreds:
    def get_secret(self, ref):
        return "sk-sub"

    def get_default_secret(self):
        return None

    def get_default_meta(self):
        return None

    def get_metadata(self, ref):
        return {"id": ref, "default_model": "m1"}

    def list_tagged(self, tag):
        return [{"id": "key_sub", "tags": [tag], "budget": None, "budget_used": 0}]


class FakeAdapter:
    mode = "native"
    model = "sub-model"

    async def complete(self, messages, tools, **kwargs):  # pragma: no cover - 被 patch 掉
        raise AssertionError("真实子任务循环不应在边界测试里运行")


def _make_subagent_tool(gateway, *, sync=True):
    definition = ToolDefinition(
        name="research_x",
        description="研究任务",
        tool_type="subagent",
        credential_ref="key_sub",
        model="m1",
        sync=sync,
        subagent_budget={"max_iterations": 5, "max_tokens": 100000},
    )
    tm = TaskManager(FakeBus(), max_concurrent=2)

    async def adapter_factory(ref, model):
        return FakeAdapter()

    tool = SubagentTool(
        definition,
        credentials=FakeCreds(),
        task_manager=tm,
        adapter_factory=adapter_factory,
        bus=FakeBus(),
        security=gateway,
    )
    return tool, tm


def _stub_subagent_loop(monkeypatch, captured):
    async def fake_run_subagent(self, task_id, adapter, business, control, budget, choice, tid, tracer):
        captured["scope"] = security_gate.current_agent_scope()
        return ToolResult(ok=True, content="子任务完成", status="completed", code="ok")

    monkeypatch.setattr(SubagentTool, "_run_subagent", fake_run_subagent)


@pytest.mark.asyncio
async def test_subagent_scope_is_parent_grant_subset(monkeypatch):
    gateway = FakeGateway()  # 没有 child_scope → 由 security_gate 从父指令 grants 生成子集
    gateway.child_scope = None
    tool, _tm = _make_subagent_tool(gateway)
    captured: dict = {}
    _stub_subagent_loop(monkeypatch, captured)

    result = await tool.run(query="研究饮食")

    assert result.ok is True, result.error
    scope = captured["scope"]
    assert scope is not None, "子智能体循环必须处在 AgentScope 里"
    assert tuple(scope.parent_grant_ids) == ("grant_1", "grant_2"), "只包含父级未撤销 grant"
    assert scope.depth == 1, "子智能体深度只有一层"
    assert "研究任务" in scope.purpose
    assert result.data is not None and result.data["agent_scope"]["depth"] == 1


@pytest.mark.asyncio
async def test_subagent_cannot_create_when_scope_would_expand(monkeypatch):
    # W1 的权威实现返回 None = 不能生成父授权子集 → 不得创建
    gateway = FakeGateway(child_scope=lambda purpose: None)
    tool, tm = _make_subagent_tool(gateway)

    result = await tool.run(query="研究饮食")

    assert result.ok is False
    assert "不能继续扩权" in (result.error or "")
    assert not list(tm._records), "拒绝时不得创建子任务"


@pytest.mark.asyncio
async def test_subagent_depth_gt_max_is_denied(monkeypatch):
    gateway = FakeGateway()  # 走本地子集生成
    gateway.child_scope = None
    tool, _tm = _make_subagent_tool(gateway)
    captured: dict = {}
    _stub_subagent_loop(monkeypatch, captured)

    # 父上下文已经是第 2 层（达到上限）→ 不允许再创建下一层
    parent = AgentScope(scope_id="scope_parent", parent_grant_ids=("grant_0",), depth=2, max_depth=2, purpose="父级")
    with security_gate.agent_scope_context(parent):
        result = await tool.run(query="研究")

    assert result.ok is False
    assert "不能继续扩权" in (result.error or "")


@pytest.mark.asyncio
async def test_subagent_creation_needs_approval_and_submits_pending(monkeypatch):
    gateway = FakeGateway(verdicts={"subagent": Verdict.APPROVE})
    tool, tm = _make_subagent_tool(gateway)

    result = await tool.run(query="研究")

    assert result.ok is True and result.code == "pending_approval"
    assert len(gateway.submitted) == 1
    assert gateway.submitted[0].kind == "subagent.create"
    assert not list(tm._records), "待审批时不得启动子任务"


# --------------------------------------------------------------------------
# 自建工具：注册不产生长期授权，每次调用重新判定
# --------------------------------------------------------------------------


class FakeSandbox:
    def __init__(self) -> None:
        self.executed: list = []

    async def effective_executor(self):
        return "subprocess"

    async def execute(self, code, arguments, **kwargs):
        self.executed.append((code, arguments, kwargs))
        return SandboxResult(ok=True, value={"arguments": dict(arguments)}, stdout="", stderr="")


def _code_tool(gateway):
    definition = ToolDefinition(
        name="my_tool",
        description="自建工具",
        tool_type="function",
        code="def run(query):\n    return query",
        entry="main:run",
    )
    sandbox = FakeSandbox()
    return CodeTool(definition, sandbox, security=gateway), sandbox


@pytest.mark.asyncio
async def test_code_tool_reauthorizes_every_call():
    gateway = FakeGateway()
    tool, sandbox = _code_tool(gateway)
    assert gateway.calls == [], "注册本身不产生任何授权调用"

    first = await tool.run(query="a")
    second = await tool.run(query="b")

    assert first.ok and second.ok
    assert len(gateway.calls) == 2, "每次调用都要重新判定（注册不产生长期权限）"
    assert all(
        str(getattr(effect.kind, "value", effect.kind)) == "execute"
        for effect in gateway.calls
    )
    assert len(sandbox.executed) == 2


@pytest.mark.asyncio
async def test_code_tool_denied_in_read_only_mode():
    gateway = FakeGateway(mode="read_only")
    tool, sandbox = _code_tool(gateway)

    result = await tool.run(query="a")

    assert result.ok is False
    assert "只读模式" in (result.error or "")
    assert sandbox.executed == [], "只读模式下不得执行自建工具代码"
