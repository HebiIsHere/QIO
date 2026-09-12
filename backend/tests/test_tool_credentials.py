from __future__ import annotations

from agent.tools.runtime_tools import CodeTool
from agent.tools.spec import ToolDefinition


class _CapturingSandbox:
    def __init__(self) -> None:
        self.last_env: dict = {}
        self.last_policy = None

    async def execute(self, code, arguments, extra_env=None, policy=None):  # noqa: ANN001
        from agent.tools.sandbox import SandboxResult

        self.last_env = dict(extra_env or {})
        self.last_policy = policy
        return SandboxResult(ok=True, value={"ok": 1}, stdout="", stderr="")


class _Creds:
    def __init__(self, secret=None, default=None) -> None:
        self._secret = secret
        self._default = default

    def get_secret(self, key_id):
        return self._secret

    def get_default_secret(self):
        return self._default


async def test_pure_tool_gets_no_credential_env():
    sb = _CapturingSandbox()
    d = ToolDefinition(name="t", description="d", code="def run(**k):\n    return {}")
    tool = CodeTool(d, sb, credentials=_Creds(secret="sk-leak"))
    res = await tool.run()
    assert res.ok
    assert sb.last_env == {}  # 未授权 → 环境里没有凭据


async def test_approved_credential_is_injected_minimally():
    sb = _CapturingSandbox()
    d = ToolDefinition(
        name="t", description="d", code="def run(**k):\n    return {}", credential_ref="weather"
    )
    tool = CodeTool(d, sb, credentials=_Creds(secret="sk-weather"))
    res = await tool.run()
    assert res.ok
    assert sb.last_env == {"QIO_KEY_WEATHER": "sk-weather"}  # 只有该工具的凭据


async def test_revoked_credential_fails_loudly():
    sb = _CapturingSandbox()
    d = ToolDefinition(
        name="t", description="d", code="def run(**k):\n    return {}", credential_ref="weather"
    )
    tool = CodeTool(d, sb, credentials=_Creds(secret=None, default=None))
    res = await tool.run()
    assert res.ok is False
    assert "不可用" in (res.error or "")


async def test_policy_passed_to_sandbox():
    sb = _CapturingSandbox()
    d = ToolDefinition(name="t", description="d", code="def run(**k):\n    return {}")
    await CodeTool(d, sb, credentials=_Creds()).run()
    assert sb.last_policy is not None
    assert sb.last_policy.is_high_risk() is False
