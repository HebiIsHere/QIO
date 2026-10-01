from __future__ import annotations

from agent.tools.policy import (
    CapabilityLevel,
    IsolationLevel,
    SideEffect,
    ToolExecutionPolicy,
    default_policy_for,
    isolation_for_executor,
    isolation_label,
    resolve_policy,
    unprotected_surfaces,
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


def test_policy_fingerprint_changes_when_capability_widens():
    from agent.tools.policy import policy_fingerprint

    base = ToolExecutionPolicy()
    widened = ToolExecutionPolicy(network=True)
    assert policy_fingerprint(base) == policy_fingerprint(ToolExecutionPolicy())
    assert policy_fingerprint(base) != policy_fingerprint(widened)


def test_effective_concurrency_legacy_and_policy():
    from agent.tools.policy import Concurrency, effective_concurrency

    class Legacy:
        is_concurrency_safe = False

    class PolicyParallel:
        execution_policy = ToolExecutionPolicy(concurrency=Concurrency.PARALLEL)

    assert effective_concurrency(Legacy()) == Concurrency.SERIALIZED
    assert effective_concurrency(PolicyParallel()) == Concurrency.PARALLEL

# ---------- D3：隔离说法必须与真实执行器一致（不许把受限子进程叫成安全沙箱） ----------


def test_isolation_level_follows_the_real_executor():
    assert isolation_for_executor("docker") == IsolationLevel.CONTAINER
    assert isolation_for_executor("subprocess") == IsolationLevel.SUBPROCESS
    # 拿不到执行器时按受限子进程记：不声称一个可能不存在的容器
    assert isolation_for_executor(None) == IsolationLevel.SUBPROCESS
    assert isolation_for_executor("") == IsolationLevel.SUBPROCESS


def test_policy_can_be_derived_from_the_real_executor():
    d = ToolDefinition(name="t", description="d", code="def run(**k):\n    return {}")
    assert default_policy_for(d, executor="docker").isolation == IsolationLevel.CONTAINER
    assert (
        default_policy_for(d, executor="subprocess").isolation == IsolationLevel.SUBPROCESS
    )


def test_the_environment_is_part_of_the_policy_fingerprint():
    """容器没了 / 换回受限子进程都是「执行环境变了」：指纹必须跟着变，
    旧授权不得沿用。"""
    from agent.tools.policy import policy_fingerprint

    d = ToolDefinition(name="t", description="d", code="def run(**k):\n    return {}")
    inside = policy_fingerprint(default_policy_for(d, executor="docker"))
    outside = policy_fingerprint(default_policy_for(d, executor="subprocess"))
    assert inside != outside
    # 同一环境内必须稳定（否则每个任务都会重新弹窗）
    assert outside == policy_fingerprint(default_policy_for(d, executor="subprocess"))


def test_the_subprocess_label_never_claims_a_security_sandbox():
    label = isolation_label("subprocess")
    assert "受限子进程" in label
    assert "不是安全沙箱" in label
    assert "隔离执行" not in label
    container = isolation_label("docker")
    assert "容器" in container and "受限子进程" not in container


def test_unprotected_surfaces_are_enumerated_for_a_plain_subprocess():
    surfaces = unprotected_surfaces("subprocess")
    joined = " ".join(surfaces)
    for expected in ("用户文件", "网络", "任意进程", "环境", "QIO 数据目录", "凭据"):
        assert expected in joined
    # 容器路径：容器本身是边界，这里不声称「什么都没保护」
    assert unprotected_surfaces("docker") == ()
