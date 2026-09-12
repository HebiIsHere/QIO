from __future__ import annotations

from agent.tools.policy import (
    CapabilityLevel,
    SideEffect,
    ToolExecutionPolicy,
    default_policy_for,
    resolve_policy,
)
from agent.tools.spec import ToolDefinition
from agent.tools.sandbox import SandboxExecutor


def test_ai_generated_tool_defaults_to_pure():
    d = ToolDefinition(name="t", description="d", code="def run(**k):\n    return {}")
    p = default_policy_for(d)
    assert p.level == CapabilityLevel.PURE
    assert p.network is False and p.filesystem == () and p.credentials == ()
    assert p.side_effect == SideEffect.PURE
    assert p.is_high_risk() is False


def test_credential_ref_promotes_to_restricted_with_only_that_category():
    d = ToolDefinition(
        name="t", description="d", code="def run(**k):\n    return {}", credential_ref="weather"
    )
    p = default_policy_for(d)
    assert p.level == CapabilityLevel.RESTRICTED
    assert p.credentials == ("weather",)
    assert p.network is False  # 凭据≠联网，能力需各自申报


def test_describe_is_human_readable():
    p = ToolExecutionPolicy(
        network=True, filesystem=("/data/x",), credentials=("weather",),
        side_effect=SideEffect.WRITE,
    )
    bullets = p.describe()
    assert any("联网：是" in b for b in bullets)
    assert any("使用凭据：weather" in b for b in bullets)
    assert any("写入文件：是" in b for b in bullets)


def test_resolve_policy_prefers_explicit_attribute():
    class _T:
        execution_policy = {"network": True}

    p = resolve_policy(_T())
    assert p.network is True


async def test_high_risk_refused_without_container_isolation():
    sb = SandboxExecutor(executor="subprocess")
    risky = ToolExecutionPolicy(network=True)  # RESTRICTED, not Trusted
    res = await sb.execute("def run(**k):\n    return {'ok': 1}", {}, policy=risky)
    assert res.ok is False
    assert "拒绝执行" in (res.error or "")


async def test_trusted_high_risk_allowed_on_subprocess():
    sb = SandboxExecutor(executor="subprocess")
    trusted = ToolExecutionPolicy(network=True, level=CapabilityLevel.TRUSTED)
    res = await sb.execute(
        "def run(**k):\n    return {'ok': 1}", {}, policy=trusted
    )
    assert res.ok is True  # 用户显式批准的 Trusted 才放行


async def test_pure_tool_runs_without_credentials():
    sb = SandboxExecutor(executor="subprocess")
    code = "import os\ndef run(**k):\n    return {'has_key': any(x.startswith('QIO_KEY_') for x in os.environ)}"
    res = await sb.execute(code, {}, policy=ToolExecutionPolicy())
    assert res.ok and res.value["has_key"] is False


def test_docker_command_policy_flags():
    sb = SandboxExecutor(executor="docker")
    pure = sb._docker_command("script", ToolExecutionPolicy())
    assert "--network" in pure and pure[pure.index("--network") + 1] == "none"
    assert "--cpus" in pure and "--pids-limit" in pure and "--read-only" in pure
    net = sb._docker_command("script", ToolExecutionPolicy(network=True))
    assert net[net.index("--network") + 1] == "bridge"
