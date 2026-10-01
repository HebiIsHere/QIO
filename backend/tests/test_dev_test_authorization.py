"""测试前授权：执行 AI 生成代码之前必须先拿到用户确认，且授权有明确生命周期。"""

from __future__ import annotations

import json

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
    """只记录请求并返回预设结论的审批服务（可带上用户选的生命周期）。"""

    def __init__(self, decision: str = "approved", *, overrides: dict | None = None) -> None:
        self.decision = decision
        self.overrides = overrides
        self.requests: list[tuple[str, dict]] = []

    async def request(self, kind: str, payload: dict, **kwargs) -> ApprovalResult:
        self.requests.append((kind, payload))
        return ApprovalResult("appr_test", self.decision, None, self.overrides)


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


async def test_approval_detail_declares_the_project_shape_and_dependencies(tmp_path):
    """审批说明要说清「跑的是什么项目、依赖谁」——依赖本机不装，缺了会明确报错。"""
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("会联网的工具")
    approvals = _FakeApprovals()
    definition = ToolDefinition(
        name="weather_fetch",
        description="查天气",
        code="import requests\n\ndef run(**kwargs):\n    return {'ok': True}",
        requirements=["requests>=2.31"],
        files={"pkg/__init__.py": "", "pkg/util.py": "X = 1\n"},
        tests=[{"name": "t", "input": {}, "expect": {"ok": True}}],
    )

    await dev_auth.ensure_test_authorization(
        workspaces=ws,
        approvals=approvals,
        sandbox=SandboxExecutor(executor="subprocess"),
        task_id=task.id,
        definition=definition,
    )

    detail = approvals.requests[0][1]["detail"]
    assert "项目文件：2 个" in detail
    assert "requests>=2.31" in detail
    assert "征求安装许可" in detail


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


def _fingerprint(definition, *, executor: str = "subprocess") -> str:
    """按生产同一口径算指纹：隔离等级来自**真实执行器**（见 policy.py）。"""
    from agent.tools.policy import default_policy_for, policy_fingerprint

    return policy_fingerprint(default_policy_for(definition, executor=executor))


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

    async def execute(
        self,
        code,
        arguments,
        extra_env=None,
        policy=None,
        files=None,
        entry=None,
        interpreter=None,
    ) -> SandboxResult:
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
    # 授权记录走真实闸门：它绑定的是这次执行的身份（含凭据范围），不是只绑指纹。
    granted = await _authorize_through_the_gate(ws, task.id, definition)
    assert len(granted.requests) == 1
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

# ---------- 授权的生命周期与身份绑定（D2） ----------


async def _authorize_through_the_gate(
    ws, task_id, definition, *, decision="approved", overrides=None
):
    """走真实的授权闸门记一次授权（测试里不自己拼身份，免得与生产逻辑分叉）。"""
    approvals = _FakeApprovals(decision, overrides=overrides)
    result = await dev_auth.ensure_test_authorization(
        workspaces=ws,
        approvals=approvals,
        sandbox=SandboxExecutor(executor="subprocess"),
        task_id=task_id,
        definition=definition,
    )
    assert result is None, getattr(result, "error", "")
    return approvals


def test_grant_records_an_explicit_lifetime(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    ws.grant_test_authorization(task.id, policy_fingerprint="p1", executor="subprocess")
    row = ws.authorizations()[0]
    assert row["lifetime"] == "task"
    assert row["lifetime_label"] == "当前开发任务"
    assert row["state"] == "valid"
    assert row["applies_to_all_tasks"] is False


def test_no_hardcoded_default_expiry(tmp_path):
    """有效期由用户选的种类决定，不是拍一个「24 小时」。"""
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    ws.grant_test_authorization(task.id, policy_fingerprint="p1", executor="subprocess")
    row = ws.authorizations()[0]
    assert row["expires_at"] == ""
    assert row["lifetime"] == "task"


def test_grant_without_a_lifetime_is_not_trusted(tmp_path):
    """旧格式记录（升级前留下的、没有生命周期字段）：覆盖范围无法证明，
    下一次执行必须重新确认 —— 宁可多问一次，也不沿用一条说不清的授权。"""
    root = tmp_path / "ws"
    ws = DevWorkspace(root)
    task = ws.create("x")
    ws.grant_test_authorization(task.id, policy_fingerprint="p1", executor="subprocess")
    state_file = task.dir / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    del state["test_authorization"]["lifetime"]
    state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    reborn = DevWorkspace(root)

    assert reborn.test_authorized(task.id, policy_fingerprint="p1", executor="subprocess") is False
    assert reborn.authorizations()[0]["state"] == "legacy"


def test_task_lifetime_ends_when_the_task_is_submitted(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    ws.grant_test_authorization(task.id, policy_fingerprint="p1", executor="subprocess")
    assert ws.test_authorized(task.id, policy_fingerprint="p1", executor="subprocess") is True

    ws.mark_submitted(task.id)

    assert ws.test_authorized(task.id, policy_fingerprint="p1", executor="subprocess") is False
    assert ws.authorizations()[0]["state"] == "ended"
    assert ws.status(task.id)["test_authorized"] is False


def test_widening_the_scope_never_reuses_the_old_grant(tmp_path):
    """范围只允许收窄或相等：现在要的更大就是「没有覆盖」，必须重新确认。"""
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    ws.grant_test_authorization(
        task.id,
        policy_fingerprint="p1",
        executor="subprocess",
        scope={
            "filesystem": [],
            "network": False,
            "network_allow": [],
            "credentials": ["a"],
        },
    )
    base = {
        "policy_fingerprint": "p1",
        "executor": "subprocess",
        "filesystem": [],
        "network": False,
        "network_allow": [],
        "credentials": ["a"],
    }
    assert ws.test_authorized(task.id, identity=dict(base)) is True
    # 收窄：仍被覆盖
    assert ws.test_authorized(task.id, identity=dict(base, credentials=[])) is True
    # 变大：一律不算数
    wider_creds = dict(base, credentials=["a", "b"])
    assert ws.test_authorized(task.id, identity=wider_creds) is False
    assert ws.test_authorization_state(task.id, identity=wider_creds)["state"] == "narrowed"
    assert ws.test_authorized(task.id, identity=dict(base, network=True)) is False
    assert ws.test_authorized(task.id, identity=dict(base, filesystem=["C:/secret"])) is False


def test_an_expired_grant_is_not_valid(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    ws.grant_test_authorization(
        task.id,
        policy_fingerprint="p1",
        executor="subprocess",
        expires_at="2000-01-01T00:00:00+00:00",
    )
    assert ws.test_authorized(task.id, policy_fingerprint="p1", executor="subprocess") is False
    assert ws.authorizations()[0]["state"] == "expired"


async def test_once_lifetime_is_consumed_by_the_first_execution(tmp_path):
    """「本次执行」放行一次就用掉：下一次必须重新问，不会悄悄变成长期授权。"""
    ws, task, definition = _workspace_with_tool(tmp_path)
    await _authorize_through_the_gate(
        ws, task.id, definition, overrides={"lifetime": "once"}
    )
    assert ws.authorizations()[0]["lifetime"] == "once"

    # 第一次放行：授权在这里被用掉（它授权的就是紧接着的这一次执行）
    first = _FakeApprovals()
    admitted = await dev_auth.ensure_test_authorization(
        workspaces=ws,
        approvals=first,
        sandbox=SandboxExecutor(executor="subprocess"),
        task_id=task.id,
        definition=definition,
    )
    assert admitted is None
    assert first.requests == [], "还没用掉之前不该重复问"
    assert ws.authorizations()[0]["state"] == "consumed"

    # 第二次：一次性授权已经用掉 → 必须重新问
    second = _FakeApprovals()
    again = await dev_auth.ensure_test_authorization(
        workspaces=ws,
        approvals=second,
        sandbox=SandboxExecutor(executor="subprocess"),
        task_id=task.id,
        definition=definition,
    )
    assert again is None
    assert len(second.requests) == 1, "用掉之后必须重新确认"


async def test_once_grant_only_covers_the_approved_version(tmp_path):
    """「本次执行」绑定被批准的那一版内容：内容一变就不再算数。"""
    ws, task, definition = _workspace_with_tool(tmp_path)
    await _authorize_through_the_gate(
        ws, task.id, definition, overrides={"lifetime": "once"}
    )
    same = dev_auth.execution_identity(
        task_id=task.id,
        definition=definition,
        executor="subprocess",
        content_digest=ws.content_digest(task.id),
    )
    assert ws.test_authorization_state(task.id, identity=same.as_request())["state"] == "valid"

    ws.write_file(task.id, "tool.py", "# 改了内容\n")

    moved = dev_auth.execution_identity(
        task_id=task.id,
        definition=definition,
        executor="subprocess",
        content_digest=ws.content_digest(task.id),
    )
    assert ws.test_authorization_state(task.id, identity=moved.as_request())["state"] == "stale"


async def test_long_term_must_be_explicitly_chosen_and_covers_other_tasks(tmp_path):
    ws, task, definition = _workspace_with_tool(tmp_path)
    other = ws.create("另一个任务")
    third = ws.create("第三个任务")

    # 用户没选 → 当前开发任务；不会因为「没说」就升级成长期
    await _authorize_through_the_gate(ws, task.id, definition)
    assert ws.authorizations()[0]["lifetime"] == "task"
    assert ws.test_authorized(
        third.id, policy_fingerprint=_fingerprint(definition), executor="subprocess"
    ) is False

    # 显式选长期 → 跨任务生效
    await _authorize_through_the_gate(
        ws, other.id, definition, overrides={"lifetime": "long_term"}
    )
    row = [r for r in ws.authorizations() if r["lifetime"] == "long_term"][0]
    assert row["applies_to_all_tasks"] is True
    assert ws.test_authorized(
        third.id, policy_fingerprint=_fingerprint(definition), executor="subprocess"
    ) is True


async def test_an_unknown_lifetime_value_never_upgrades_the_grant(tmp_path):
    ws, task, definition = _workspace_with_tool(tmp_path)
    await _authorize_through_the_gate(
        ws, task.id, definition, overrides={"lifetime": "forever"}
    )
    assert ws.authorizations()[0]["lifetime"] == "task"


def test_revoking_a_task_also_revokes_its_long_term_grant(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    ws.grant_test_authorization(
        task.id, policy_fingerprint="p1", executor="subprocess", lifetime="long_term"
    )
    assert ws.test_authorized("ws_ffffffffffff", policy_fingerprint="p1", executor="subprocess") is True

    assert ws.revoke_test_authorization(task.id) is True

    assert ws.test_authorized("ws_ffffffffffff", policy_fingerprint="p1", executor="subprocess") is False
    assert ws.authorizations() == []


def test_long_term_grant_survives_a_restart(tmp_path):
    root = tmp_path / "ws"
    ws = DevWorkspace(root)
    task = ws.create("x")
    ws.grant_test_authorization(
        task.id, policy_fingerprint="p1", executor="subprocess", lifetime="long_term"
    )

    reborn = DevWorkspace(root)

    assert reborn.test_authorized("ws_ffffffffffff", policy_fingerprint="p1", executor="subprocess") is True
    assert reborn.authorizations()[0]["lifetime"] == "long_term"


async def test_the_approval_declares_the_identity_and_the_lifetime(tmp_path):
    """用户看到的说明 = 机器可读身份：任务 / 指纹 / 环境 / 目录 / 网络 /
    凭据 / 内容摘要，以及三种生命周期。"""
    ws, task, definition = _workspace_with_tool(tmp_path)
    approvals = await _authorize_through_the_gate(ws, task.id, definition)
    _, payload = approvals.requests[0]
    identity = payload["execution_identity"]
    assert identity["task_id"] == task.id
    assert identity["policy_fingerprint"] == _fingerprint(definition)
    assert identity["executor"] == "subprocess"
    assert identity["filesystem"] == []
    assert identity["network"] is False
    assert identity["credentials"] == []
    assert identity["content_digest"] == ws.content_digest(task.id)
    assert payload["grant"]["lifetime"] == "task"
    assert {opt["value"] for opt in payload["grant"]["options"]} == {
        "once",
        "task",
        "long_term",
    }
    assert "授权时长" in payload["detail"]
    assert "收回" in payload["detail"]


async def test_a_wider_policy_asks_again(tmp_path):
    ws, task, definition = _workspace_with_tool(tmp_path)
    await _authorize_through_the_gate(ws, task.id, definition)

    wider = ToolDefinition(
        name="add_numbers",
        description="求和",
        code=definition.code,
        tests=definition.tests,
        credential_ref="weather-key",
    )
    ws.write_definition(task.id, wider)

    approvals = _FakeApprovals()
    result = await dev_auth.ensure_test_authorization(
        workspaces=ws,
        approvals=approvals,
        sandbox=SandboxExecutor(executor="subprocess"),
        task_id=task.id,
        definition=wider,
    )
    assert result is None
    assert len(approvals.requests) == 1, "范围变大必须重新确认"

# ---------- D3：隔离说法必须与真实执行器一致 ----------


async def test_the_approval_never_calls_a_plain_process_a_sandbox(tmp_path):
    """受限子进程不是安全沙箱：文案不许含糊，「没有保护什么」也要写出来。"""
    ws, task, definition = _workspace_with_tool(tmp_path)
    approvals = await _authorize_through_the_gate(ws, task.id, definition)
    _, payload = approvals.requests[0]

    assert payload["isolation"]["executor"] == "subprocess"
    assert payload["isolation"]["isolated"] is False
    assert "不是安全沙箱" in payload["isolation"]["label"]
    assert "不是安全沙箱" in payload["detail"]
    assert "隔离执行" not in payload["detail"]
    assert payload["code_boundary"]["isolated"] is False

    # 没有被保护的面逐条列出：用户文件 / 网络 / 任意进程 / 环境 / QIO 数据目录 / 凭据
    limits = payload["limits"]
    joined = " ".join(limits)
    for surface in ("用户文件", "网络", "任意进程", "环境", "QIO 数据目录", "凭据"):
        assert surface in joined
    assert "不" in payload["detail"]


async def test_a_container_run_is_labeled_as_a_container(tmp_path):
    """真的探测到容器时才说容器 —— 而且不再列「没有保护」的面。"""
    ws, task, definition = _workspace_with_tool(tmp_path)
    approvals = _FakeApprovals()
    result = await dev_auth.ensure_test_authorization(
        workspaces=ws,
        approvals=approvals,
        sandbox=SandboxExecutor(executor="docker"),
        task_id=task.id,
        definition=definition,
    )
    assert result is None
    _, payload = approvals.requests[0]
    assert payload["isolation"]["executor"] == "docker"
    assert payload["isolation"]["isolated"] is True
    assert "容器" in payload["isolation"]["label"]
    assert payload["limits"] == []


async def test_the_authorization_fingerprint_follows_the_real_environment(tmp_path):
    """容器 ↔ 受限子进程属于「执行环境变了」：指纹与授权记录都必须跟着变。"""
    ws, task, definition = _workspace_with_tool(tmp_path)
    await _authorize_through_the_gate(ws, task.id, definition)
    granted = ws.authorizations()[0]
    assert granted["executor"] == "subprocess"

    approvals = _FakeApprovals()
    result = await dev_auth.ensure_test_authorization(
        workspaces=ws,
        approvals=approvals,
        sandbox=SandboxExecutor(executor="docker"),
        task_id=task.id,
        definition=definition,
    )
    assert result is None
    assert len(approvals.requests) == 1, "换执行环境必须重新确认"
    identity = approvals.requests[0][1]["execution_identity"]
    assert identity["policy_fingerprint"] != granted["policy_fingerprint"]
