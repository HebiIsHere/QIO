"""测试前授权：执行 AI 生成代码之前必须先拿到用户确认（按任务一次）。"""

from __future__ import annotations

from agent.tools.approval import ApprovalResult
from agent.tools.dev_workspace import DevWorkspace
from agent.tools.spec import ToolDefinition


def _definition(*, credential_ref: str | None = None) -> ToolDefinition:
    return ToolDefinition(
        name="weather_fetch",
        description="查天气",
        code="def run(**kwargs):\n    return {'ok': True}",
        tests=[{"name": "t", "input": {}, "expect": {"ok": True}}],
        credential_ref=credential_ref,
    )


class _FakeApprovals:
    """只记录请求并返回预设结论的审批服务。"""

    def __init__(self, decision: str = "approved") -> None:
        self.decision = decision
        self.requests: list[tuple[str, dict]] = []

    async def request(self, kind: str, payload: dict, **kwargs) -> ApprovalResult:
        self.requests.append((kind, payload))
        return ApprovalResult("appr_test", self.decision)


def _grant(ws: DevWorkspace, task_id: str, *, policy="p1", executor="subprocess") -> None:
    ws.grant_test_authorization(task_id, policy_fingerprint=policy, executor=executor)


def test_workspace_starts_without_authorization(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    assert ws.test_authorized(task.id, policy_fingerprint="p1", executor="subprocess") is False


def test_granted_authorization_covers_the_same_policy_and_executor(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    _grant(ws, task.id)
    assert ws.test_authorized(task.id, policy_fingerprint="p1", executor="subprocess") is True


def test_authorization_is_invalidated_by_policy_or_executor_change(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    _grant(ws, task.id)
    # 能力变大 → 必须重新确认
    assert ws.test_authorized(task.id, policy_fingerprint="p2", executor="subprocess") is False
    # 执行环境变了（有容器了 / 容器没了）→ 也必须重新确认
    assert ws.test_authorized(task.id, policy_fingerprint="p1", executor="docker") is False


def test_authorization_survives_restart(tmp_path):
    root = tmp_path / "ws"
    ws = DevWorkspace(root)
    task = ws.create("x")
    _grant(ws, task.id)
    reborn = DevWorkspace(root)
    assert reborn.test_authorized(task.id, policy_fingerprint="p1", executor="subprocess") is True


def test_unknown_task_is_never_authorized(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    assert ws.test_authorized("ws_ffffffffffff", policy_fingerprint="p1", executor="x") is False


# ---------- 执行前的授权闸门（dev_auth.ensure_test_authorization） ----------

from agent.tools import dev_auth
from agent.tools.sandbox import SandboxExecutor


async def test_without_an_approval_service_nothing_executes(tmp_path):
    """拿不到授权服务就没有授权 —— 明确阻断，而不是照旧执行。"""
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    result = await dev_auth.ensure_test_authorization(
        workspaces=ws,
        approvals=None,
        sandbox=SandboxExecutor(executor="subprocess"),
        task_id=task.id,
        definition=_definition(),
    )
    assert result is not None and result.ok is False
    assert result.category == "permission"
    assert "授权" in result.error or "审批" in result.error


async def test_already_authorized_does_not_ask_again(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    sandbox = SandboxExecutor(executor="subprocess")
    ws.grant_test_authorization(
        task.id, policy_fingerprint=_fingerprint(_definition()), executor="subprocess"
    )
    approvals = _FakeApprovals()
    result = await dev_auth.ensure_test_authorization(
        workspaces=ws, approvals=approvals, sandbox=sandbox,
        task_id=task.id, definition=_definition(),
    )
    assert result is None
    assert approvals.requests == []


async def test_first_run_asks_once_and_records_the_grant(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    approvals = _FakeApprovals("approved")
    sandbox = SandboxExecutor(executor="subprocess")

    first = await dev_auth.ensure_test_authorization(
        workspaces=ws, approvals=approvals, sandbox=sandbox,
        task_id=task.id, definition=_definition(),
    )
    assert first is None
    assert len(approvals.requests) == 1
    assert ws.test_authorized(
        task.id, policy_fingerprint=_fingerprint(_definition()), executor="subprocess"
    )

    # 同任务的第二次（含提交复测）不再打扰用户
    second = await dev_auth.ensure_test_authorization(
        workspaces=ws, approvals=approvals, sandbox=sandbox,
        task_id=task.id, definition=_definition(),
    )
    assert second is None
    assert len(approvals.requests) == 1


async def test_rejected_approval_blocks_and_does_not_record(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    approvals = _FakeApprovals("rejected")
    result = await dev_auth.ensure_test_authorization(
        workspaces=ws, approvals=approvals, sandbox=SandboxExecutor(executor="subprocess"),
        task_id=task.id, definition=_definition(),
    )
    assert result is not None and result.ok is False
    assert result.category == "permission"
    assert ws.test_authorized(
        task.id, policy_fingerprint=_fingerprint(_definition()), executor="subprocess"
    ) is False


async def test_timeout_blocks_too(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    result = await dev_auth.ensure_test_authorization(
        workspaces=ws, approvals=_FakeApprovals("timeout"),
        sandbox=SandboxExecutor(executor="subprocess"),
        task_id=task.id, definition=_definition(),
    )
    assert result is not None and result.ok is False
    assert result.category == "permission"


async def test_authorization_is_bound_to_the_executor(tmp_path):
    """同一个任务换了执行环境（容器 ↔ 受限子进程）必须重新确认。"""
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    ws.grant_test_authorization(
        task.id, policy_fingerprint=_fingerprint(_definition()), executor="subprocess"
    )
    approvals = _FakeApprovals("approved")
    result = await dev_auth.ensure_test_authorization(
        workspaces=ws, approvals=approvals, sandbox=SandboxExecutor(executor="docker"),
        task_id=task.id, definition=_definition(),
    )
    assert result is None
    assert len(approvals.requests) == 1


# ---------- 授权说明必须说准执行边界 ----------

async def test_boundary_statement_is_honest_about_a_plain_process(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    approvals = _FakeApprovals("approved")
    await dev_auth.ensure_test_authorization(
        workspaces=ws, approvals=approvals, sandbox=SandboxExecutor(executor="subprocess"),
        task_id=task.id, definition=_definition(),
    )
    kind, payload = approvals.requests[0]
    assert kind == "tool_execution"
    assert payload["tool"] == "dev_run_tests"
    joined = " ".join(payload.get("access") or []) + " " + str(payload.get("detail") or "")
    assert "受限子进程" in joined
    assert "不是安全沙箱" in joined
    assert payload["code_boundary"]["executor"] == "subprocess"
    assert payload["code_boundary"]["isolated"] is False


async def test_boundary_statement_mentions_credentials_are_simulated(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    approvals = _FakeApprovals("approved")
    await dev_auth.ensure_test_authorization(
        workspaces=ws, approvals=approvals, sandbox=SandboxExecutor(executor="subprocess"),
        task_id=task.id, definition=_definition(credential_ref="weather-key"),
    )
    _, payload = approvals.requests[0]
    assert any("weather-key" in cap for cap in payload["capabilities"])
    assert payload["code_boundary"]["credentials_simulated"] is True
    assert "不注入" in str(payload.get("detail") or "")


def _fingerprint(definition) -> str:
    from agent.tools.policy import default_policy_for, policy_fingerprint

    return policy_fingerprint(default_policy_for(definition))


# ---------- 接进 dev 工具：跑测试与提交复测都要先过授权 ----------

from agent.tools.dev_tools import DevRunTestsTool, DevSubmitTool
from agent.tools.sandbox import SandboxResult


class _RecordingSandbox:
    """记录「代码到底有没有被执行」的沙箱替身。"""

    executor = "subprocess"
    timeout_seconds = 5.0

    def __init__(self) -> None:
        self.executions = 0

    async def effective_executor(self) -> str:
        return "subprocess"

    async def execute(self, code, arguments, extra_env=None, policy=None) -> SandboxResult:
        self.executions += 1
        return SandboxResult(ok=True, value={"ok": True}, stdout="", stderr="")


def _workspace_with_tool(tmp_path, *, credential_ref: str | None = None):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("工具")
    definition = ToolDefinition(
        name="add_numbers",
        description="求和",
        code="def run(**kwargs):\n    return {'ok': True}",
        tests=[{"name": "t", "input": {}, "expect": {"ok": True}}],
        credential_ref=credential_ref,
    )
    ws.write_definition(task.id, definition)
    return ws, task, definition


async def test_dev_run_tests_blocks_without_any_authorization(tmp_path):
    ws, task, definition = _workspace_with_tool(tmp_path)
    sandbox = _RecordingSandbox()
    tool = DevRunTestsTool(ws, sandbox=sandbox, approvals=None)
    r = await tool.run(workspace=task.id)
    assert r.ok is False
    assert r.category == "permission"
    assert sandbox.executions == 0, "没有授权就不该执行生成代码"


async def test_dev_run_tests_asks_then_runs_after_approval(tmp_path):
    ws, task, definition = _workspace_with_tool(tmp_path)
    sandbox = _RecordingSandbox()
    approvals = _FakeApprovals("approved")
    tool = DevRunTestsTool(ws, sandbox=sandbox, approvals=approvals)

    first = await tool.run(workspace=task.id)
    assert first.ok is True
    assert sandbox.executions == 1
    assert len(approvals.requests) == 1
    assert ws.test_authorized(
        task.id,
        policy_fingerprint=_fingerprint(definition),
        executor="subprocess",
    )

    second = await tool.run(workspace=task.id)
    assert second.ok is True
    assert sandbox.executions == 2
    assert len(approvals.requests) == 1, "同一个任务只需要确认一次"


async def test_dev_run_tests_rejected_never_executes(tmp_path):
    ws, task, definition = _workspace_with_tool(tmp_path)
    sandbox = _RecordingSandbox()
    tool = DevRunTestsTool(ws, sandbox=sandbox, approvals=_FakeApprovals("rejected"))
    r = await tool.run(workspace=task.id)
    assert r.ok is False
    assert r.category == "permission"
    assert sandbox.executions == 0
    # 没有执行就不该留下「测试过」的记录
    assert ws.status(task.id)["last_test_passed"] is None


async def test_dev_run_tests_says_credentials_were_simulated(tmp_path):
    ws, task, definition = _workspace_with_tool(tmp_path, credential_ref="weather-key")
    ws.grant_test_authorization(
        task.id, policy_fingerprint=_fingerprint(definition), executor="subprocess"
    )
    tool = DevRunTestsTool(ws, sandbox=_RecordingSandbox(), approvals=None)
    r = await tool.run(workspace=task.id)
    assert r.ok is True
    assert "模拟" in r.content
    assert "凭据" in r.content


async def test_dev_submit_blocks_until_the_test_was_authorized(tmp_path):
    ws, task, definition = _workspace_with_tool(tmp_path)
    built = {"n": 0}

    async def builder():
        built["n"] += 1
        raise AssertionError("没有授权就不该走到提交审批")

    tool = DevSubmitTool(ws, lifecycle_builder=builder, approvals=None)
    r = await tool.run(workspace=task.id, explanation="求和工具")
    assert r.ok is False
    assert r.category == "permission"
    assert built["n"] == 0
