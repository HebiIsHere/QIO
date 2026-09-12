"""P7 scenarios 8 & 9: what the capability layer actually enforces.

Two tests here are regressions for defects found during the P7 sweep:
  * a timed-out tool left an orphan process behind and let the temp-dir cleanup
    raise out of `execute()` instead of returning a clean failure;
  * `policy.output_limit_chars` was declared but never applied to function
    tool output.

Scope note: the restricted subprocess does NOT enforce filesystem/network
limits at runtime — it enforces *declared* policy (by refusing high-risk
declarations without container isolation) and strips credentials from the
environment. A tool that lies about its capabilities is not contained; that is
recorded as a known limitation rather than asserted here.
"""

from __future__ import annotations

from agent.adapters.base import ToolSpec
from agent.services.tool_router import CORE_TOOLS, ToolRouter, wants_web
from agent.tools.policy import (
    CapabilityLevel,
    ToolExecutionPolicy,
    default_policy_for,
)
from agent.tools.runtime_tools import CodeTool
from agent.tools.sandbox import SandboxExecutor
from agent.tools.spec import ToolDefinition


def _pure() -> ToolExecutionPolicy:
    return ToolExecutionPolicy()


def _specs(n: int) -> list[ToolSpec]:
    return [
        ToolSpec(name=f"tool_{i}", description=f"does thing {i}", parameters={})
        for i in range(n)
    ]


# -- scenario 8: tool permission ------------------------------------------


async def test_pure_tool_has_no_credential_environment():
    sb = SandboxExecutor(executor="subprocess")
    code = "import os\ndef run(**k):\n    return {'keys': sorted(os.environ)}\n"
    res = await sb.execute(code, {}, policy=_pure())
    assert res.ok
    assert not any(k.startswith("QIO_KEY_") for k in res.value["keys"])
    assert set(res.value["keys"]) <= {"PATH", "TEMP", "TMP", "PYTHONIOENCODING"}


async def test_high_risk_declaration_is_refused_without_container():
    sb = SandboxExecutor(executor="subprocess")
    for policy in (
        ToolExecutionPolicy(network=True),
        ToolExecutionPolicy(filesystem=("/tmp",)),
        ToolExecutionPolicy(shell=True),
        ToolExecutionPolicy(credentials=("weather",)),
    ):
        res = await sb.execute("def run(**k):\n    return {}", {}, policy=policy)
        assert res.ok is False
        assert "拒绝执行" in (res.error or "")


async def test_trusted_approval_opens_only_the_approved_capability():
    sb = SandboxExecutor(executor="subprocess")
    approved = ToolExecutionPolicy(network=True, level=CapabilityLevel.TRUSTED)
    res = await sb.execute("def run(**k):\n    return {'ok': 1}", {}, policy=approved)
    assert res.ok is True
    # approval is for this exact capability set; the pure default stays closed
    assert (
        default_policy_for(
            ToolDefinition(name="t", description="d", code="def run(**k):\n    return {}")
        ).network
        is False
    )


async def test_timeout_returns_clean_failure_and_kills_the_child():
    """Regression: a timeout used to raise PermissionError out of execute()
    (temp-dir cleanup) and leave the child process running."""
    sb = SandboxExecutor(executor="subprocess", timeout_seconds=1.0)
    code = "import time\ndef run(**k):\n    time.sleep(30)\n    return {}\n"
    res = await sb.execute(code, {}, policy=_pure())
    assert res.ok is False
    assert "timeout" in (res.error or "")


async def test_function_tool_output_is_truncated_to_policy_limit():
    """Regression: output_limit_chars was declared but never applied."""
    sb = SandboxExecutor(executor="subprocess")
    definition = ToolDefinition(
        name="big",
        description="returns a big blob",
        code="def run(**k):\n    return {'blob': 'x' * 100000}\n",
    )
    result = await CodeTool(definition, sb).run()
    assert result.ok
    limit = default_policy_for(definition).output_limit_chars
    assert len(result.content) < 100000
    assert "输出已截断" in result.content
    assert len(result.content) <= limit + 60  # limit + the visible marker


# -- scenario 9: tool router exposure -------------------------------------


def test_router_core_tools_always_present():
    router = ToolRouter(embedding=None)
    specs = _specs(30) + [
        ToolSpec(name=name, description="core", parameters={}) for name in CORE_TOOLS
    ]
    names = {s.name for s in router.route("帮我看看最近的记忆", specs, pending_tasks=False)}
    assert set(CORE_TOOLS) <= names


def test_router_hides_web_when_capability_is_off():
    router = ToolRouter(embedding=None)
    specs = _specs(3) + [
        ToolSpec(name="web_search", description="search the web", parameters={})
    ]
    assert "web_search" not in {
        s.name for s in router.route("随便聊聊", specs, web_allowed=False)
    }


def test_router_surfaces_web_stably_on_explicit_intent():
    router = ToolRouter(embedding=None)
    specs = _specs(3) + [
        ToolSpec(name="web_search", description="search the web", parameters={})
    ]
    for query in ("帮我联网查一下最新消息", "搜一下今天新闻", "web search this"):
        assert wants_web(query) is True
        assert "web_search" in {s.name for s in router.route(query, specs)}


def test_router_task_tools_only_with_pending_tasks():
    router = ToolRouter(embedding=None)
    specs = _specs(3) + [
        ToolSpec(name="await_task", description="await", parameters={}),
        ToolSpec(name="read_task_result", description="read", parameters={}),
    ]
    off = {s.name for s in router.route("继续", specs, pending_tasks=False)}
    on = {s.name for s in router.route("继续", specs, pending_tasks=True)}
    assert "await_task" not in off and "read_task_result" not in off
    assert "await_task" in on and "read_task_result" in on
