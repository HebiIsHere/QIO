"""工具执行管线：pre/execute/post/result + 超时/审批/审计策略。"""
from __future__ import annotations

import asyncio

from agent.adapters.base import ToolCall
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry


class FakeApproval:
    def __init__(self, decision: str):
        self.decision = decision


class FakeApprovals:
    def __init__(self, decision: str = "approved"):
        self.decision = decision
        self.requests: list[tuple[str, dict]] = []

    async def request(self, kind: str, payload: dict):
        self.requests.append((kind, payload))
        return FakeApproval(self.decision)


def make_call(name: str = "echo", arguments: dict | None = None) -> ToolCall:
    return ToolCall(id="c1", name=name, arguments=arguments or {})


class EchoTool(Tool):
    name = "echo"
    description = "echo"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs):
        return ToolResult(ok=True, content="echo:" + str(kwargs.get("text", "")))


class SleepTool(Tool):
    name = "sleep"
    description = "sleep"
    parameters = {"type": "object", "properties": {}}
    timeout_ms = 50

    async def run(self, **kwargs):
        await asyncio.sleep(10)
        return ToolResult(ok=True, content="done")


class ApprovalTool(EchoTool):
    name = "approve_me"
    requires_approval = True


async def test_basic_execute_ok():
    reg = ToolRegistry()
    reg.register(EchoTool())
    result = await reg.execute(make_call("echo", {"text": "hi"}))
    assert result.ok
    assert result.content == "echo:hi"


async def test_unknown_tool_fails_isolated():
    reg = ToolRegistry()
    result = await reg.execute(make_call("ghost"))
    assert not result.ok
    assert "ghost" in result.error


async def test_pre_execute_reject_short_circuits():
    reg = ToolRegistry()
    reg.register(EchoTool())

    def reject(ctx, *, next):
        return ToolResult(ok=False, error="blocked by policy")

    reg.register_policy("tool/pre-execute", reject)
    result = await reg.execute(make_call("echo", {"text": "hi"}))
    assert not result.ok
    assert result.error == "blocked by policy"


async def test_pre_execute_replaces_arguments():
    reg = ToolRegistry()
    reg.register(EchoTool())

    async def rewrite(ctx, *, next):
        ctx["arguments"] = {"text": "rewritten"}
        return await next(ctx)

    reg.register_policy("tool/pre-execute", rewrite)
    result = await reg.execute(make_call("echo", {"text": "original"}))
    assert result.ok
    assert result.content == "echo:rewritten"


async def test_execute_policy_wraps_default_executor():
    reg = ToolRegistry()
    reg.register(EchoTool())
    order: list[str] = []

    async def wrapper(ctx, *, next):
        order.append("before")
        result = await next(ctx)
        order.append("after")
        return result

    reg.register_policy("tool/execute", wrapper)
    result = await reg.execute(make_call("echo", {"text": "x"}))
    assert result.ok
    assert order == ["before", "after"]


async def test_execute_policy_can_short_circuit():
    reg = ToolRegistry()
    reg.register(EchoTool())

    async def fake_run(ctx, *, next):
        return ToolResult(ok=True, content="fake")

    reg.register_policy("tool/execute", fake_run)
    result = await reg.execute(make_call("echo", {"text": "x"}))
    assert result.ok
    assert result.content == "fake"


async def test_post_execute_transforms_result():
    reg = ToolRegistry()
    reg.register(EchoTool())

    async def decorate(result, *, next):
        result.content = f"[{result.content}]"
        return await next(result)

    reg.register_policy("tool/post-execute", decorate)
    result = await reg.execute(make_call("echo", {"text": "x"}))
    assert result.content == "[echo:x]"


async def test_result_and_end_observers_see_final():
    reg = ToolRegistry()
    reg.register(EchoTool())
    seen_result: list[ToolResult] = []
    seen_end: list[dict] = []

    def on_result(data):
        seen_result.append(data["result"])

    async def on_end(data):
        seen_end.append(data)

    reg.register_policy("tool/result", on_result)
    reg.register_policy("tool/end", on_end)
    await reg.execute(make_call("echo", {"text": "y"}))
    assert len(seen_result) == 1
    assert seen_result[0].content == "echo:y"
    assert seen_end[0]["tool"] == "echo"
    assert seen_end[0]["ok"] is True


async def test_timeout_marks_failure():
    reg = ToolRegistry()
    reg.register(SleepTool())
    result = await reg.execute(make_call("sleep"))
    assert not result.ok
    assert "timed out" in result.error


async def test_approval_approved_proceeds():
    approvals = FakeApprovals("approved")
    reg = ToolRegistry(approvals=approvals)
    reg.register(ApprovalTool())
    result = await reg.execute(make_call("approve_me"))
    assert result.ok
    assert approvals.requests and approvals.requests[0][0] == "tool_execution"
    assert approvals.requests[0][1]["tool"] == "approve_me"


async def test_approval_rejected_short_circuits():
    approvals = FakeApprovals("rejected")
    reg = ToolRegistry(approvals=approvals)
    reg.register(ApprovalTool())
    result = await reg.execute(make_call("approve_me"))
    assert not result.ok
    assert "rejected" in result.error


async def test_approval_timeout_fails_closed():
    approvals = FakeApprovals("timeout")
    reg = ToolRegistry(approvals=approvals)
    reg.register(ApprovalTool())
    result = await reg.execute(make_call("approve_me"))
    assert not result.ok
    assert "timed out" in result.error


async def test_non_approval_tool_skips_approval():
    approvals = FakeApprovals("rejected")
    reg = ToolRegistry(approvals=approvals)
    reg.register(EchoTool())
    result = await reg.execute(make_call("echo", {"text": "x"}))
    assert result.ok
    assert approvals.requests == []


async def test_audit_listener_receives_trace_fields():
    reg = ToolRegistry()
    reg.register(EchoTool())
    traces: list[dict] = []

    async def audit(data):
        result = data["result"]
        traces.append(
            {"tool": data["tool"], "args": data["call"].arguments, "ok": result.ok}
        )

    reg.register_policy("tool/result", audit)
    await reg.execute(make_call("echo", {"text": "z"}))
    assert traces == [{"tool": "echo", "args": {"text": "z"}, "ok": True}]


async def test_register_policy_returns_disposer():
    reg = ToolRegistry()
    reg.register(EchoTool())
    calls: list[str] = []

    def h(data):
        calls.append(data["tool"])

    dispose = reg.register_policy("tool/result", h)
    await reg.execute(make_call("echo"))
    assert len(calls) == 1
    dispose()
    await reg.execute(make_call("echo"))
    assert len(calls) == 1
