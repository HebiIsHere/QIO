"""R9-F W2：审批后执行同一边界（问题 A / 契约 §3.3）。

覆盖：
* _ApprovedReplayGateway.authorize() **不再**无条件 ALLOW：操作类型/argv/命令/cwd
  与审批记录不一致、没有审批记录、当前模式切到只读 → DENY；
* 重放前重核对 payload / 指纹：程序被换、argv 改、cwd 改、脚本内容改 → 不执行并
  返回「审批失效」的明确结果（不是重新弹一次审批）；
* 一致时正常执行，且 permit 来自审批 payload 重核对（source=approval / approval_id）；
* 真实指挥链：cmd_tools 提交审批时写入 spec_digest/program_sha256/payload_digest 指纹。
"""

from __future__ import annotations

import hashlib
import json
import sys

import pytest

from _r9_auth_support import make_security

from agent.security import launch as launch_boundary
from agent.security.operations import ExecutionContext, PendingOperation
from agent.services.computer import ComputerSandbox
from agent.tools import security_gate
from agent.tools.approved_operations import (
    _ApprovedReplayGateway,
    execute_proc_run_program,
    execute_proc_shell,
    register_approved_operations,
)

PY = sys.executable


def _security(tmp_path, db_conn, *, mode="restricted"):
    security = make_security(tmp_path, db_conn, mode=mode)
    computer = ComputerSandbox(
        resolve_root=lambda: str(security.workspace),
        permission_mode=lambda: security.store.get("computer.permission_mode", "restricted"),
    )
    register_approved_operations(
        security.operations,
        computer=computer,
        security=security.engine,
        tool_lookup=lambda name: None,
        attachments_getter=None,
    )
    return security, computer


def _payload_digest(payload: dict) -> str:
    raw = json.dumps(dict(payload or {}), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _fingerprint(computer, *, kind: str, payload: dict, program=None, args=(), cwd=None, cmd=None):
    """与 cmd_tools._pending_facts 同源的审批指纹（测试里按提交时的真实事实构造）。"""
    spec = launch_boundary.LaunchSpec(
        kind=kind,
        program=program,
        argv=tuple(args),
        cwd=cwd,
        shell_cmd=cmd,
        timeout=float(payload.get("timeout") or 30),
    )
    facts = launch_boundary.build_spec_facts(spec, computer=computer)
    return {
        "kind": "proc.run_program" if kind == "program" else "proc.shell",
        "argv": [str(program or ""), *[str(a) for a in args]] if kind == "program" else None,
        "cwd": cwd,
        "spec_digest": launch_boundary.compute_spec_digest(spec, computer=computer),
        "program_sha256": (facts.get("program_facts") or {}).get("sha256"),
        "allowed_roots": list(launch_boundary.write_roots_for(computer, "restricted")),
        "payload_digest": _payload_digest(payload),
        "internal_data_roots": [],
    }


def _ctx(security, *, kind, payload, fingerprint=None, confirmations=1):
    operation = PendingOperation(
        kind=kind,
        label="测试操作",
        payload=dict(payload),
        effect=security_gate.effect_to_dict(
            security_gate.execute_effect(
                "run_program" if kind == "proc.run_program" else "run_shell",
                str(payload.get("program") or "shell"),
                list(payload.get("args") or ([payload.get("cmd")] if payload.get("cmd") else [])),
                cwd=payload.get("cwd"),
                shell=kind == "proc.shell",
                purpose="测试",
                detail=(
                    {"risk": "low"}
                    if kind == "proc.run_program"
                    else {"risk": "low", "command": str(payload.get("cmd") or "")}
                ),
            )
        ),
        fingerprint=dict(fingerprint or {}),
        confirmations_required=confirmations,
        mode_at_request="restricted",
        source="tool",
    )
    return ExecutionContext(
        approval_id="appr_r9f",
        operation=operation,
        mode_at_request="restricted",
        mode_at_approval="restricted",
        engine=security.engine,
        mode_getter=security.approvals.current_mode_value,
    )


# --------------------------------------------------------------------------
# 重放网关：不再无条件 ALLOW
# --------------------------------------------------------------------------


async def test_replay_gateway_is_not_unconditional_allow():
    payload = {"program": "git", "args": ["--version"], "cwd": None}
    effect = security_gate.execute_effect(
        "run_program", "git", ["--version"], purpose="测试"
    )
    operation = PendingOperation(
        kind="proc.run_program",
        label="运行程序 git",
        payload=payload,
        effect=security_gate.effect_to_dict(effect),
    )
    gateway = _ApprovedReplayGateway(None, operation=operation, approval_id="appr_1")

    matching = await gateway.authorize(effect)
    assert security_gate.verdict_of(matching) == "allow"
    assert "审批记录一致" in (matching.reason or "")

    mismatched = security_gate.execute_effect(
        "run_program", "git", ["commit", "-m", "x"], purpose="测试"
    )
    denied = await gateway.authorize(mismatched)
    assert security_gate.verdict_of(denied) == "deny"
    assert "审批失效" in (denied.reason or "")
    assert "argv" in (denied.reason or "")


async def test_replay_gateway_denies_different_effect_kind():
    effect = security_gate.execute_effect("run_program", "git", ["--version"], purpose="x")
    operation = PendingOperation(
        kind="proc.run_program",
        label="运行程序 git",
        payload={"program": "git", "args": ["--version"]},
        effect=security_gate.effect_to_dict(effect),
    )
    gateway = _ApprovedReplayGateway(None, operation=operation)
    other = security_gate.make_effect(
        "read", [security_gate.path_target("some.txt")], purpose="x"
    )

    denied = await gateway.authorize(other)

    assert security_gate.verdict_of(denied) == "deny"
    assert "操作类型" in (denied.reason or "")


async def test_replay_gateway_without_approval_record_denies():
    gateway = _ApprovedReplayGateway(None)
    effect = security_gate.execute_effect("run_program", "git", ["--version"], purpose="x")

    denied = await gateway.authorize(effect)

    assert security_gate.verdict_of(denied) == "deny"
    assert "没有审批记录" in (denied.reason or "")


async def test_replay_gateway_denies_when_mode_switched_to_read_only(tmp_path, db_conn):
    security, _computer = _security(tmp_path, db_conn, mode="read_only")
    effect = security_gate.execute_effect("run_program", "git", ["--version"], purpose="x")
    operation = PendingOperation(
        kind="proc.run_program",
        label="运行程序 git",
        payload={"program": "git", "args": ["--version"]},
        effect=security_gate.effect_to_dict(effect),
    )
    gateway = _ApprovedReplayGateway(security.engine, operation=operation, approval_id="appr_1")

    denied = await gateway.authorize(effect)

    assert security_gate.verdict_of(denied) == "deny"
    assert "只读" in (denied.reason or "")


# --------------------------------------------------------------------------
# execute_proc_*：payload / 指纹 / 程序身份 / cwd 变化 → 不执行
# --------------------------------------------------------------------------


async def _assert_not_launched(monkeypatch):
    async def _never(self, spec, permit, **kwargs):
        raise AssertionError("审批失效时不得进入受控启动（不得执行任何程序）")

    monkeypatch.setattr(launch_boundary.ControlledLauncher, "launch", _never)


async def test_proc_run_program_rejects_changed_payload(tmp_path, db_conn, monkeypatch):
    security, computer = _security(tmp_path, db_conn)
    payload = {"program": "git", "args": ["--version"], "cwd": str(security.workspace)}
    ctx = _ctx(
        security,
        kind="proc.run_program",
        payload=payload,
        fingerprint=_fingerprint(
            computer, kind="program", payload=payload, program="git", args=["--version"],
            cwd=payload["cwd"],
        ),
    )
    await _assert_not_launched(monkeypatch)
    tampered = dict(payload, args=["commit", "-m", "x"])

    result = await execute_proc_run_program(tampered, ctx, computer=computer, security=security.engine)

    assert result.ok is False
    assert result.detail.get("invalidated") is True
    assert "payload" in (result.error or "")
    assert "审批失效" in (result.summary or "")


async def test_proc_run_program_rejects_fingerprint_argv_mismatch(tmp_path, db_conn, monkeypatch):
    security, computer = _security(tmp_path, db_conn)
    payload = {"program": "git", "args": ["--version"], "cwd": str(security.workspace)}
    fingerprint = _fingerprint(
        computer, kind="program", payload=payload, program="git", args=["--version"],
        cwd=payload["cwd"],
    )
    fingerprint["spec_digest"] = "deadbeef" * 8
    ctx = _ctx(security, kind="proc.run_program", payload=payload, fingerprint=fingerprint)
    await _assert_not_launched(monkeypatch)

    result = await execute_proc_run_program(payload, ctx, computer=computer, security=security.engine)

    assert result.ok is False
    assert result.detail.get("invalidated") is True
    assert "spec_digest" in (result.error or "")


async def test_proc_run_program_rejects_program_identity_change(tmp_path, db_conn, monkeypatch):
    security, computer = _security(tmp_path, db_conn)
    payload = {"program": PY, "args": ["-c", "print(1)"], "cwd": str(security.workspace)}
    fingerprint = _fingerprint(
        computer, kind="program", payload=payload, program=PY, args=["-c", "print(1)"],
        cwd=payload["cwd"],
    )
    fingerprint["program_sha256"] = "0" * 64
    ctx = _ctx(security, kind="proc.run_program", payload=payload, fingerprint=fingerprint)
    await _assert_not_launched(monkeypatch)

    result = await execute_proc_run_program(payload, ctx, computer=computer, security=security.engine)

    assert result.ok is False
    assert result.detail.get("invalidated") is True
    assert "程序文件" in (result.error or "")


async def test_proc_run_program_rejects_cwd_change(tmp_path, db_conn, monkeypatch):
    security, computer = _security(tmp_path, db_conn)
    payload = {"program": "git", "args": ["--version"], "cwd": str(security.workspace)}
    fingerprint = _fingerprint(
        computer, kind="program", payload=payload, program="git", args=["--version"], cwd=None
    )
    ctx = _ctx(security, kind="proc.run_program", payload=payload, fingerprint=fingerprint)
    await _assert_not_launched(monkeypatch)

    result = await execute_proc_run_program(payload, ctx, computer=computer, security=security.engine)

    assert result.ok is False
    assert result.detail.get("invalidated") is True


async def test_proc_replay_rejects_mode_switch_to_read_only(tmp_path, db_conn, monkeypatch):
    security, computer = _security(tmp_path, db_conn, mode="restricted")
    payload = {"program": "git", "args": ["--version"], "cwd": str(security.workspace)}
    ctx = _ctx(
        security,
        kind="proc.run_program",
        payload=payload,
        fingerprint=_fingerprint(
            computer, kind="program", payload=payload, program="git", args=["--version"],
            cwd=payload["cwd"],
        ),
    )
    security.store.set("computer.permission_mode", "read_only")
    await _assert_not_launched(monkeypatch)

    result = await execute_proc_run_program(payload, ctx, computer=computer, security=security.engine)

    assert result.ok is False
    assert result.detail.get("invalidated") is True
    assert "只读" in (result.summary or "") or "只读" in (result.error or "")


# --------------------------------------------------------------------------
# 一致时正常执行，且 permit 来自审批 payload 重核对
# --------------------------------------------------------------------------


async def test_proc_run_program_executes_with_approval_permit(tmp_path, db_conn, monkeypatch):
    security, computer = _security(tmp_path, db_conn)
    payload = {"program": "git", "args": ["--version"], "cwd": str(security.workspace)}
    fingerprint = _fingerprint(
        computer, kind="program", payload=payload, program="git", args=["--version"],
        cwd=payload["cwd"],
    )
    ctx = _ctx(security, kind="proc.run_program", payload=payload, fingerprint=fingerprint)
    captured: dict = {}
    original = launch_boundary.ControlledLauncher.launch

    async def _spy(self, spec, permit, **kwargs):
        captured["spec"] = spec
        captured["permit"] = permit
        return await original(self, spec, permit, **kwargs)

    monkeypatch.setattr(launch_boundary.ControlledLauncher, "launch", _spy)

    result = await execute_proc_run_program(payload, ctx, computer=computer, security=security.engine)

    assert result.ok is True, result.error
    assert "git" in (result.detail.get("content") or "").lower()
    permit = captured["permit"]
    assert permit.source == "approval"
    assert permit.approval_id == ctx.approval_id
    assert permit.spec_digest == fingerprint["spec_digest"]
    assert permit.allowed_operations == ("execute",)
    assert result.detail["permit"]["source"] == "approval"


async def test_proc_shell_executes_and_rejects_changed_command(tmp_path, db_conn, monkeypatch):
    security, computer = _security(tmp_path, db_conn)
    payload = {"cmd": "echo r9f-approved-shell", "cwd": str(security.workspace)}
    fingerprint = _fingerprint(
        computer, kind="shell", payload=payload, cmd=payload["cmd"], cwd=payload["cwd"]
    )
    ctx = _ctx(security, kind="proc.shell", payload=payload, fingerprint=fingerprint)

    result = await execute_proc_shell(payload, ctx, computer=computer, security=security.engine)
    assert result.ok is True, result.error
    assert "r9f-approved-shell" in (result.detail.get("content") or "")

    tampered = dict(payload, cmd="echo r9f-tampered")
    await _assert_not_launched(monkeypatch)
    changed = await execute_proc_shell(
        tampered, ctx, computer=computer, security=security.engine
    )
    assert changed.ok is False
    assert changed.detail.get("invalidated") is True
    assert "审批失效" in (changed.summary or "")


# --------------------------------------------------------------------------
# 提交侧：cmd_tools 的审批指纹必须包含重核对所需的全部事实
# --------------------------------------------------------------------------


class _ApproveGateway:
    """轻量 fake：让工具走「提交待审批」路径并记录 operation。"""

    def __init__(self, *, mode="restricted") -> None:
        self._mode = mode
        self.submitted: list = []
        self.internal_data_paths: list = []

    def mode(self):
        return self._mode

    async def authorize(self, effect, *, ctx=None):
        from agent.tools.security_types import AuthDecision, Verdict

        return AuthDecision(verdict=Verdict.APPROVE, reason="fake:approve")

    async def submit(self, operation, *, ctx=None):
        import types

        self.submitted.append(operation)
        return types.SimpleNamespace(
            approval_id="appr_fake",
            state="waiting",
            expires_at="2099-01-01T00:00:00+00:00",
            confirmations_required=operation.confirmations_required,
        )

    def instruction(self):
        return None


@pytest.mark.parametrize("kind", ["program", "shell"])
async def test_cmd_tools_submit_approval_with_recheck_fingerprint(
    tmp_path, db_conn, kind
):
    from agent.tools.cmd_tools import RunCmdTool, RunProgramTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    computer = ComputerSandbox(
        resolve_root=lambda: str(workspace),
        permission_mode=lambda: "restricted",
    )
    gateway = _ApproveGateway()
    if kind == "program":
        tool = RunProgramTool(computer=computer, security=gateway)
        result = await tool.run(program="git", args=["--version"], cwd=str(workspace))
    else:
        tool = RunCmdTool(computer=computer, security=gateway)
        result = await tool.run(cmd="echo hi", cwd=str(workspace))

    assert result.ok is True and result.code == "pending_approval"
    assert gateway.submitted, "必须提交待审批操作"
    fingerprint = gateway.submitted[0].fingerprint
    for key in (
        "spec_digest",
        "program_sha256",
        "payload_digest",
        "allowed_roots",
        "internal_data_roots",
    ):
        assert key in fingerprint, f"审批指纹缺少 {key}"
    assert len(str(fingerprint["spec_digest"])) == 64
    assert fingerprint["payload_digest"] == _payload_digest(gateway.submitted[0].payload)


async def test_cmd_tools_internal_data_requires_three_confirmations(tmp_path, db_conn):
    """命令里明确提到内部数据路径：提交三次确认，不直接执行。"""
    from agent.tools.cmd_tools import RunCmdTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    internal = tmp_path / "qio-data"
    internal.mkdir()
    computer = ComputerSandbox(
        resolve_root=lambda: str(workspace),
        permission_mode=lambda: "restricted",
    )
    gateway = _ApproveGateway()
    gateway.internal_data_paths = [internal]
    tool = RunCmdTool(computer=computer, security=gateway)

    result = await tool.run(cmd=f"type {internal / 'qio.db'}", cwd=str(workspace))

    assert result.ok is True and result.code == "pending_approval"
    assert result.data["confirmations_required"] == 3
    operation = gateway.submitted[0]
    assert operation.confirmations_required == 3
    assert operation.fingerprint.get("internal_data_roots")


# --------------------------------------------------------------------------
# 批准后脚本内容被换（argv 不变）：旧许可必须失效，且篡改后的脚本不得执行
# --------------------------------------------------------------------------


async def test_script_content_change_between_approval_and_replay_invalidated(
    tmp_path, db_conn
):
    security, computer = _security(tmp_path, db_conn)
    workspace = security.workspace
    script = tmp_path / "job.py"
    original_marker = tmp_path / "original.marker"
    tampered_marker = tmp_path / "tampered.marker"
    script.write_text(
        "open(r'" + str(original_marker) + "', 'w').write('original')",
        encoding="utf-8",
    )
    payload = {"program": PY, "args": [str(script)], "cwd": str(workspace)}
    fingerprint = _fingerprint(
        computer,
        kind="program",
        payload=payload,
        program=PY,
        args=[str(script)],
        cwd=payload["cwd"],
    )
    # 批准之前把脚本内容换掉：argv 完全不变，只有「被执行的脚本内容」变了
    script.write_text(
        "open(r'" + str(tampered_marker) + "', 'w').write('tampered')",
        encoding="utf-8",
    )
    ctx = _ctx(
        security, kind="proc.run_program", payload=payload, fingerprint=fingerprint
    )

    result = await execute_proc_run_program(
        payload, ctx, computer=computer, security=security.engine
    )

    assert result.ok is False, "批准后脚本内容变化仍然被执行"
    assert result.detail.get("invalidated") is True
    assert not tampered_marker.exists(), "篡改后的脚本内容不得被执行"
    assert not original_marker.exists(), "原脚本也不得因为旧许可被执行"
    assert "spec_digest" in (result.error or "") or "许可失效" in (result.error or "")


async def test_cmd_tools_approval_fingerprint_covers_script_content(tmp_path, db_conn):
    """提交审批时指纹必须覆盖 argv 里脚本文件的内容（否则批准后替换无法发现）。"""
    from agent.tools.cmd_tools import RunProgramTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = tmp_path / "job.py"
    script.write_text("print('one')", encoding="utf-8")
    computer = ComputerSandbox(
        resolve_root=lambda: str(workspace), permission_mode=lambda: "restricted"
    )
    gateway = _ApproveGateway()
    tool = RunProgramTool(computer=computer, security=gateway)

    first = await tool.run(program=PY, args=[str(script)], cwd=str(workspace))
    assert first.ok is True and first.code == "pending_approval"
    first_digest = gateway.submitted[0].fingerprint["spec_digest"]

    script.write_text("print('two-tampered')", encoding="utf-8")
    second = await tool.run(program=PY, args=[str(script)], cwd=str(workspace))
    assert second.ok is True and second.code == "pending_approval"
    second_digest = gateway.submitted[1].fingerprint["spec_digest"]

    assert first_digest != second_digest, "脚本内容变化必须反映到审批指纹里"
