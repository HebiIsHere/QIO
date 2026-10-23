"""R9-H4 D：程序/脚本启动事实、审查内容一致性、进程操作（真实 cmd/launch/engine）。

覆盖契约 §2/§3/§6：
* 命令/脚本 effect 补真实 limit_impacts（execute/program 真实身份 + 已解析删除对象 delete 影响），
  launch spec 与 effect 事实（spec_digest/程序/argv/cwd/脚本摘要）一致；
* 审查/审批/执行前重核对使用同一份事实：脚本或程序内容在等待期间变化 → 旧许可失效、不执行；
* 固定查询、普通读取不受破坏；进程结束命令保持既有规则（只读拒绝，不操作真实用户进程）；
* 用户限制按实际程序/删除对象拒绝（引擎侧 limit_impacts 匹配属 R9-H4 B，未合入本分支的用例标 xfail）。

真实链路：RunProgramTool/RunCmdTool/ProcKillTool + ControlledLauncher + approved_operations 执行器；
故障注入用 fake 文件/假 gateway，不使用真实模型、不操作真实用户进程。
"""

from __future__ import annotations

import os
import shutil
import sys
import types
from pathlib import Path

import pytest

from agent.security import launch as launch_boundary
from agent.security.operations import ExecutionContext, PendingOperation
from agent.services.computer import ComputerSandbox
from agent.tools.approved_operations import execute_proc_run_program
from agent.tools.cmd_tools import ProcKillTool, RunCmdTool, RunProgramTool
from agent.tools.security_types import AuthDecision, Verdict

PY = sys.executable
WINDOWS = sys.platform == "win32"
PENDING_B = "R9-H4 B（引擎 limit_impacts/program 对象匹配）未合入本分支：待集成后转绿"


# --------------------------------------------------------------------------
# 轻量替身（模型不参与；只替引擎/台账接口）
# --------------------------------------------------------------------------


class FakeGateway:
    def __init__(self, *, mode="restricted", verdict="approve") -> None:
        self._mode = mode
        self.verdict = verdict
        self.effects: list = []
        self.submitted: list = []
        self.internal_data_paths: list = []

    def mode(self):
        return self._mode

    def instruction(self):
        return None

    def current_requirement_binding(self):
        return {"requirement_id": "req_test", "requirement_version": 1, "source": "binding"}

    async def authorize(self, effect, *, ctx=None):
        self.effects.append(effect)
        verdict = self.verdict(effect) if callable(self.verdict) else self.verdict
        return AuthDecision(verdict=Verdict(verdict), reason="fake:" + str(verdict))

    async def submit(self, operation, *, ctx=None):
        self.submitted.append(operation)
        return types.SimpleNamespace(
            approval_id="appr_r9h4",
            state="waiting",
            expires_at="2099-01-01T00:00:00+00:00",
            confirmations_required=operation.confirmations_required,
        )


class FakeLedger:
    def __init__(self, *, status: str = "active", version: int = 1) -> None:
        self.status = status
        self.version = version

    def by_id(self, requirement_id: str):
        if requirement_id != "req_test":
            return None
        return types.SimpleNamespace(
            requirement_id=requirement_id, status=self.status, version=self.version
        )


class FakeEngine:
    def __init__(self, ledger: FakeLedger | None = None) -> None:
        self.requirements = ledger or FakeLedger()


def _sandbox(root) -> ComputerSandbox:
    return ComputerSandbox(resolve_root=lambda: str(root), permission_mode=lambda: "restricted")


def _replay_ctx(operation: PendingOperation, *, mode: str = "restricted") -> ExecutionContext:
    return ExecutionContext(
        approval_id="appr_r9h4",
        operation=operation,
        mode_at_request="restricted",
        mode_at_approval="restricted",
        engine=FakeEngine(),
        mode_getter=lambda: mode,
    )


def _spec(**kwargs) -> launch_boundary.LaunchSpec:
    return launch_boundary.LaunchSpec(**kwargs)


def _delete_command(path) -> str:
    return ("del /f /q " + str(path)) if WINDOWS else ("rm -f " + str(path))


def _replaceable_program(tmp_path: Path) -> tuple[str, list[str], str]:
    """返回 (可替换程序路径副本, 触发参数的 argv, 标记文件内容片段)。"""
    if WINDOWS:
        source = shutil.which("cmd") or os.environ.get("COMSPEC") or "cmd.exe"
        copied = tmp_path / "copied_program.exe"
        marker = tmp_path / "program.marker"
        args = ["/c", f"echo ran> \"{marker}\""]
    else:
        source = shutil.which("sh") or "/bin/sh"
        copied = tmp_path / "copied_program"
        marker = tmp_path / "program.marker"
        args = ["-c", f"echo ran > '{marker}'"]
    shutil.copy2(source, copied)
    return str(copied), args, str(marker)


async def _submit(tool, **kwargs) -> PendingOperation:
    result = await tool.run(**kwargs)
    assert result.ok is True and result.code == "pending_approval", result
    assert tool.security.submitted, "必须提交待审批操作"
    return tool.security.submitted[-1]


# --------------------------------------------------------------------------
# 1. limit_impacts：真实影响（execute/program + 已解析 delete 对象）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_program_effect_has_resolved_program_impact(tmp_path):
    gateway = FakeGateway(verdict="allow")
    tool = RunProgramTool(computer=_sandbox(tmp_path), security=gateway)

    result = await tool.run(program=PY, args=["--version"], cwd=str(tmp_path), timeout=60)

    assert result.ok is True, result.error
    effect = gateway.effects[-1]
    execution = dict(effect.detail.get("execution") or {})
    impacts = list(effect.detail.get("limit_impacts") or ())
    assert impacts, "命令 effect 必须带真实 limit_impacts"
    execute_impacts = [item for item in impacts if item.get("operation") == "execute"]
    assert execute_impacts, impacts
    program_objects = [
        obj for item in execute_impacts for obj in item.get("objects") or ()
        if obj.get("kind") == "program"
    ]
    assert program_objects, execute_impacts
    resolved = str(execution.get("program_path") or "")
    assert resolved and program_objects[0]["value"] == resolved, (
        "limit_impacts 必须是 resolve 后的真实程序身份", program_objects[0], resolved
    )
    # base effect 的 program target 与 impact 同一身份
    targets = [str(t.value) for t in effect.targets if t.kind == "program"]
    assert targets == [resolved]


@pytest.mark.asyncio
async def test_run_program_delete_command_keeps_delete_impact(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("x", encoding="utf-8")
    gateway = FakeGateway(verdict="approve")
    tool = RunProgramTool(computer=_sandbox(tmp_path), security=gateway)

    # 入口是 execute（run_program + rm/del 语义），删除影响不能丢
    if WINDOWS:
        await _submit(
            tool, program="cmd", args=["/c", _delete_command(victim)], cwd=str(tmp_path)
        )
    else:
        await _submit(
            tool, program="rm", args=["-f", str(victim)], cwd=str(tmp_path)
        )
    effect = gateway.effects[-1]
    execution = dict(effect.detail.get("execution") or {})
    intent = dict((effect.detail.get("delete_intent") or {}))
    assert effect.kind.value == "execute"
    assert execution.get("kind") == "program"
    impacts = list(effect.detail.get("limit_impacts") or ())
    delete_impacts = [item for item in impacts if item.get("operation") == "delete"]
    if intent:
        assert delete_impacts, (
            "命令入口是 execute 时，已解析的删除对象必须进入 limit_impacts.delete",
            impacts,
        )
        values = [obj.get("value") for item in delete_impacts for obj in item.get("objects") or ()]
        assert str(victim) in values, (values, intent)
    # 删除对象同时是 effect 的 path target（既有规则保持）
    path_targets = [str(t.value) for t in effect.targets if t.kind == "path"]
    if intent:
        assert str(victim) in path_targets


@pytest.mark.asyncio
async def test_run_shell_delete_command_has_delete_and_execute_impacts(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("x", encoding="utf-8")
    gateway = FakeGateway(verdict="approve")
    tool = RunCmdTool(computer=_sandbox(tmp_path), security=gateway)

    operation = await _submit(tool, cmd=_delete_command(victim), cwd=str(tmp_path))
    effect = gateway.effects[-1]
    execution = dict(effect.detail.get("execution") or {})
    impacts = list(effect.detail.get("limit_impacts") or ())
    by_operation = {str(item.get("operation")): item for item in impacts}
    assert "execute" in by_operation and "delete" in by_operation, impacts
    shell_objects = by_operation["execute"]["objects"]
    assert shell_objects[0]["kind"] == "program"
    assert shell_objects[0]["value"] == str(execution.get("program_path")), (
        "shell 的程序身份必须是 resolve 后的 shell 可执行文件",
        shell_objects[0],
        execution.get("program_path"),
    )
    delete_values = [obj.get("value") for obj in by_operation["delete"]["objects"]]
    assert str(victim) in delete_values, (delete_values, operation.payload)
    # payload 也绑定影响（审批展示/绑定共用）
    assert dict(operation.payload.get("limit_impacts"))


@pytest.mark.asyncio
async def test_effect_launch_facts_match_real_spec(tmp_path):
    gateway = FakeGateway(verdict="allow")
    tool = RunProgramTool(computer=_sandbox(tmp_path), security=gateway)
    await tool.run(program=PY, args=["--version"], cwd=str(tmp_path), timeout=60)

    effect = gateway.effects[-1]
    detail = dict(effect.detail or {})
    spec = _spec(kind="program", program=PY, argv=("--version",), cwd=str(tmp_path), timeout=60)
    expected = launch_boundary.build_launch_facts(spec, computer=_sandbox(tmp_path))
    stored = dict(detail.get("launch_facts") or {})
    assert stored, "effect.detail 必须携带 launch_facts（与 launch spec 同一份事实）"
    for key in ("kind", "program_path", "program_sha256", "argv", "cwd", "shell", "spec_digest"):
        assert stored.get(key) == expected.get(key), (key, stored.get(key), expected.get(key))
    execution = dict(detail.get("execution") or {})
    assert stored["spec_digest"] == execution.get("spec_digest")
    assert len(str(stored["spec_digest"])) == 64


@pytest.mark.asyncio
async def test_approval_fingerprint_binds_impacts_and_launch_facts(tmp_path):
    gateway = FakeGateway(verdict="approve")
    tool = RunProgramTool(computer=_sandbox(tmp_path), security=gateway)
    operation = await _submit(
        tool, program=PY, args=["-c", "print(1)"], cwd=str(tmp_path), timeout=60
    )
    fingerprint = dict(operation.fingerprint)
    assert fingerprint.get("limit_impacts"), "审批指纹必须绑定实际影响"
    launch_facts = dict(fingerprint.get("launch_facts") or {})
    assert launch_facts.get("spec_digest") and launch_facts.get("program_path")
    assert fingerprint.get("spec_digest") == launch_facts.get("spec_digest")
    assert dict(operation.payload.get("limit_impacts"))


@pytest.mark.asyncio
async def test_fixed_query_still_allowed_with_impacts(tmp_path):
    gateway = FakeGateway(mode="read_only", verdict="allow")
    tool = RunProgramTool(computer=_sandbox(tmp_path), security=gateway)
    result = await tool.run(program=PY, args=["--version"], cwd=str(tmp_path), timeout=60)
    assert result.ok is True, result.error
    effect = gateway.effects[-1]
    impacts = list(effect.detail.get("limit_impacts") or ())
    assert [item["operation"] for item in impacts] == ["execute"]


# --------------------------------------------------------------------------
# 2. 审查/执行一致性：脚本、程序内容等待期间变化 → 旧许可失效
# --------------------------------------------------------------------------


def test_recheck_rejects_script_content_change(tmp_path):
    script = tmp_path / "job.py"
    script.write_text("print('original')", encoding="utf-8")
    spec = _spec(kind="program", program=PY, argv=(str(script),), cwd=str(tmp_path))
    facts = launch_boundary.build_spec_facts(spec, computer=_sandbox(tmp_path))
    fingerprint = {
        "spec_digest": launch_boundary.compute_spec_digest(spec, computer=_sandbox(tmp_path)),
        "program_sha256": (facts.get("program_facts") or {}).get("sha256"),
    }
    unchanged, reason, _digest = launch_boundary.recheck_approved_spec(
        spec, fingerprint, computer=_sandbox(tmp_path)
    )
    assert unchanged is True, reason

    script.write_text("print('tampered')", encoding="utf-8")
    changed, reason2, _digest2 = launch_boundary.recheck_approved_spec(
        spec, fingerprint, computer=_sandbox(tmp_path)
    )
    assert changed is False, reason2
    assert "spec_digest" in reason2 or "不一致" in reason2, reason2


def test_recheck_rejects_program_content_change(tmp_path):
    program, args, _marker = _replaceable_program(tmp_path)
    spec = _spec(kind="program", program=program, argv=tuple(args), cwd=str(tmp_path))
    facts = launch_boundary.build_spec_facts(spec, computer=_sandbox(tmp_path))
    fingerprint = {
        "spec_digest": launch_boundary.compute_spec_digest(spec, computer=_sandbox(tmp_path)),
        "program_sha256": (facts.get("program_facts") or {}).get("sha256"),
    }
    assert launch_boundary.recheck_approved_spec(
        spec, fingerprint, computer=_sandbox(tmp_path)
    )[0] is True

    with open(program, "ab") as handle:
        handle.write(b"\n# tampered\n")
    changed, reason, _digest = launch_boundary.recheck_approved_spec(
        spec, fingerprint, computer=_sandbox(tmp_path)
    )
    assert changed is False, reason
    assert "不一致" in reason or "失效" in reason, reason


@pytest.mark.asyncio
async def test_approval_wait_script_change_never_executes(tmp_path):
    script = tmp_path / "waited_script.py"
    executed = tmp_path / "executed.marker"
    script.write_text(
        f"open(r'{executed}', 'w').write('ran')", encoding="utf-8"
    )
    gateway = FakeGateway(verdict="approve")
    tool = RunProgramTool(computer=_sandbox(tmp_path), security=gateway)
    operation = await _submit(tool, program=PY, args=[str(script)], cwd=str(tmp_path), timeout=60)

    script.write_text(
        f"open(r'{executed}', 'w').write('tampered')", encoding="utf-8"
    )
    ctx = _replay_ctx(operation)
    result = await execute_proc_run_program(
        operation.payload, ctx, computer=_sandbox(tmp_path), security=None
    )

    assert result.ok is False, (result.summary, result.error)
    assert result.detail.get("invalidated") is True
    assert "不一致" in (result.summary or "") or "失效" in (result.summary or ""), result.summary
    assert not executed.exists(), "等待期间被替换的脚本不得执行"


@pytest.mark.asyncio
async def test_approval_wait_program_change_never_executes(tmp_path):
    program, args, marker = _replaceable_program(tmp_path)
    gateway = FakeGateway(verdict="approve")
    tool = RunProgramTool(computer=_sandbox(tmp_path), security=gateway)
    operation = await _submit(tool, program=program, args=args, cwd=str(tmp_path), timeout=60)

    with open(program, "ab") as handle:
        handle.write(b"\n# tampered\n")
    ctx = _replay_ctx(operation)
    result = await execute_proc_run_program(
        operation.payload, ctx, computer=_sandbox(tmp_path), security=None
    )

    assert result.ok is False, (result.summary, result.error)
    assert result.detail.get("invalidated") is True
    assert not Path(marker).exists(), "等待期间被替换的程序不得执行（旧许可失效）"


# --------------------------------------------------------------------------
# 3. 进程结束命令：既有规则保持（只读拒绝，不操作真实用户进程）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proc_kill_read_only_denied_without_real_process_operation(tmp_path):
    gateway = FakeGateway(mode="read_only", verdict="allow")
    tool = ProcKillTool(computer=_sandbox(tmp_path), security=gateway)
    result = await tool.run(pid="999999")
    assert result.ok is False
    assert result.code == "readonly_execution_denied"
    assert "只读" in (result.error or "")


# --------------------------------------------------------------------------
# 4. 用户限制按真实程序/删除对象拒绝（引擎侧 limit_impacts 匹配属 B，未合入 → xfail）
# --------------------------------------------------------------------------


class _LimitLedger:
    def __init__(self, limit: dict) -> None:
        self.limit = limit

    def _requirement(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            requirement_id="req_limit",
            status="active",
            version=1,
            limits=(),
            structured_limits=(self.limit,),
            structured_limits_state="structured",
        )

    def current(self):
        return self._requirement()

    def by_id(self, requirement_id: str):
        return self._requirement() if requirement_id == "req_limit" else None


def _limit_engine(limit: dict):
    from agent.security.engine import AuthorizationEngine

    return AuthorizationEngine(
        requirements=_LimitLedger(limit), internal_data_paths=lambda: []
    )


@pytest.mark.asyncio
@pytest.mark.xfail(strict=False, reason=PENDING_B)
async def test_user_limit_on_program_blocks_real_execution(tmp_path):
    executed = tmp_path / "limit_program.marker"
    limit = {
        "operations": ["execute"],
        "objects": [{"kind": "program", "value": str(Path(PY).resolve())}],
        "reason": "用户明确禁止运行这个程序",
    }
    engine = _limit_engine(limit)
    capture = FakeGateway(verdict="approve")
    capture_tool = RunProgramTool(computer=_sandbox(tmp_path), security=capture)
    await _submit(
        capture_tool,
        program=PY,
        args=["-c", f"open(r'{executed}', 'w').write('ran')"],
        cwd=str(tmp_path),
        timeout=60,
    )
    effect = capture.effects[-1]
    decision = await engine.authorize(effect)
    assert decision.verdict == Verdict.DENY, (
        "用户限制的程序必须 DENY（实际程序身份匹配）", decision.verdict, decision.reason
    )
    assert "用户明确禁止运行这个程序" in str(decision.reason or ""), decision.reason

    tool = RunProgramTool(computer=_sandbox(tmp_path), security=engine)
    result = await tool.run(
        program=PY,
        args=["-c", f"open(r'{executed}', 'w').write('ran')"],
        cwd=str(tmp_path),
        timeout=60,
    )
    assert result.ok is False, (result.ok, result.error)
    assert not executed.exists(), "被限制的程序不得启动"


@pytest.mark.asyncio
@pytest.mark.xfail(strict=False, reason=PENDING_B)
async def test_user_limit_on_delete_object_blocks_delete_command(tmp_path):
    victim = tmp_path / "b.txt"
    victim.write_text("keep", encoding="utf-8")
    limit = {
        "operations": ["delete"],
        "objects": [{"kind": "path", "value": str(victim)}],
        "reason": "用户明确禁止删除这个文件",
    }
    engine = _limit_engine(limit)
    capture = FakeGateway(verdict="approve")
    capture_tool = RunCmdTool(computer=_sandbox(tmp_path), security=capture)
    await _submit(capture_tool, cmd=_delete_command(victim), cwd=str(tmp_path))
    effect = capture.effects[-1]
    decision = await engine.authorize(effect)
    assert decision.verdict == Verdict.DENY, (
        "用户限制的删除对象必须 DENY（不能走普通审批/模型允许）",
        decision.verdict,
        decision.reason,
    )
    assert "用户明确禁止删除这个文件" in str(decision.reason or ""), decision.reason
    assert victim.exists()
