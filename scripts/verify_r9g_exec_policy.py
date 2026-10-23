"""R9-G W2 真实机器验证：固定查询 / 永久删除 / 命令执行审批绑定与重核对。

不联网、不调用任何模型（网关/台账都是本脚本内的 fake，模拟引擎与审批记录）；
真实拉起本机程序（python/git --version 等）与真实删除命令（Windows del / POSIX rm），
把结果与结构化事实写入 _r9g-evidence/w2-exec/verify_r9g_exec_policy.json。

运行（Windows PowerShell）：
    cd backend
    uv run --frozen python ../scripts/verify_r9g_exec_policy.py

退出码 0 = 全部检查通过；1 = 有检查失败（逐项打印）。
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))
EVIDENCE_DIR = ROOT / "_r9g-evidence" / "w2-exec"

from agent.security import deletion, fixed_queries  # noqa: E402
from agent.security.operations import ExecutionContext  # noqa: E402
from agent.services.computer import ComputerSandbox  # noqa: E402
from agent.tools.approved_operations import (  # noqa: E402
    execute_proc_run_program,
    execute_proc_shell,
)
from agent.tools.cmd_tools import RunCmdTool, RunProgramTool  # noqa: E402
from agent.tools.security_types import AuthDecision, Verdict  # noqa: E402

WINDOWS = sys.platform == "win32"
PY = sys.executable
RESULTS: list[dict] = []


def check(name: str, ok: bool, detail: dict | None = None) -> bool:
    RESULTS.append({"check": name, "ok": bool(ok), "detail": detail or {}})
    print(("PASS  " if ok else "FAIL  ") + name)
    return ok


class FakeGateway:
    def __init__(self, *, mode: str = "restricted", verdict: str = "allow") -> None:
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
        return {"requirement_id": "req_real_verify", "requirement_version": 1, "source": "binding"}

    async def authorize(self, effect, *, ctx=None):
        self.effects.append(effect)
        return AuthDecision(verdict=Verdict(self.verdict), reason="verify:" + self.verdict)

    async def submit(self, operation, *, ctx=None):
        self.submitted.append(operation)
        return types.SimpleNamespace(
            approval_id="appr_real_verify",
            state="waiting",
            expires_at="2099-01-01T00:00:00+00:00",
            confirmations_required=operation.confirmations_required,
        )


class FakeLedger:
    def __init__(self, *, status: str = "active", version: int = 1) -> None:
        self.status = status
        self.version = version

    def by_id(self, requirement_id: str):
        if requirement_id != "req_real_verify":
            return None
        return types.SimpleNamespace(
            requirement_id=requirement_id, status=self.status, version=self.version
        )


class FakeEngine:
    def __init__(self, ledger: FakeLedger | None = None) -> None:
        self.requirements = ledger or FakeLedger()


class FakeRecheckService:
    def __init__(self, *, ok: bool = True, reason: str = "") -> None:
        self.ok = ok
        self.reason = reason
        self.calls: list = []

    async def recheck(self, binding, *, effect):
        self.calls.append({"binding": dict(binding or {}), "effect": dict(effect or {})})
        return types.SimpleNamespace(ok=self.ok, code="", reason=self.reason)


def sandbox(root) -> ComputerSandbox:
    return ComputerSandbox(
        resolve_root=lambda: str(root), permission_mode=lambda: "restricted"
    )


def which(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    if WINDOWS:
        for base in (
            Path("C:/Program Files/Git/usr/bin"),
            Path("C:/Program Files/Git/bin"),
            Path("C:/Windows/System32"),
        ):
            candidate = base / (name + ".exe")
            if candidate.is_file():
                return str(candidate)
    return None


def delete_command(path) -> str:
    return ("del /f /q " + str(path)) if WINDOWS else ("rm -f " + str(path))


def replay_ctx(operation, *, mode: str = "restricted", ledger=None, recheck=None) -> ExecutionContext:
    ctx = ExecutionContext(
        approval_id="appr_real_verify",
        operation=operation,
        mode_at_request="restricted",
        mode_at_approval="restricted",
        engine=FakeEngine(ledger),
        mode_getter=lambda: mode,
    )
    if recheck is not None:
        ctx.recheck = recheck
    return ctx


async def submit_operation(tool, **kwargs):
    result = await tool.run(**kwargs)
    if result.code != "pending_approval" or not tool.security.submitted:
        raise RuntimeError("没有产生待审批操作: " + str(result))
    return result, tool.security.submitted[-1]


# --------------------------------------------------------------------------
# 1. 固定查询：真实程序 + 真实执行 + 引擎可重算
# --------------------------------------------------------------------------


async def verify_fixed_queries(workspace: Path) -> None:
    evidence: dict = {}

    python_facts = fixed_queries.FixedQueryFacts(
        kind="program",
        program_path=PY,
        program_name="python",
        argv=("--version",),
        cwd=None,
        shell=False,
    )
    classified = fixed_queries.classify_fixed_query(python_facts)
    evidence["python_version"] = None if classified is None else classified.kind
    check("fixed_query/python --version 被识别为 version", evidence["python_version"] == "version")

    python_script = fixed_queries.classify_fixed_query(
        fixed_queries.FixedQueryFacts(
            kind="program",
            program_path=PY,
            program_name="python",
            argv=("-c", "print(1)"),
            cwd=None,
            shell=False,
        )
    )
    check("fixed_query/python -c 不是固定查询", python_script is None)

    git_path = which("git")
    if git_path:
        git_result = fixed_queries.classify_fixed_query(
            fixed_queries.FixedQueryFacts(
                kind="program",
                program_path=git_path,
                program_name="git",
                argv=("--version",),
                cwd=None,
                shell=False,
            )
        )
        evidence["git_version"] = None if git_result is None else git_result.kind
        check("fixed_query/git --version 被识别为 version", evidence["git_version"] == "version")

    fake_dir = workspace / "impostor"
    fake_dir.mkdir()
    fake_python = fake_dir / ("python.exe" if WINDOWS else "python")
    fake_python.write_bytes(b"fake")
    impostor = fixed_queries.classify_fixed_query(
        fixed_queries.FixedQueryFacts(
            kind="program",
            program_path=str(fake_python),
            program_name="python",
            argv=("--version",),
            cwd=None,
            shell=False,
        )
    )
    check("fixed_query/同名替身（临时目录）不被放行", impostor is None)

    gateway = FakeGateway(mode="restricted", verdict="allow")
    tool = RunProgramTool(computer=sandbox(workspace), security=gateway)
    result = await tool.run(program=PY, args=["--version"], cwd=str(workspace), timeout=60)
    execution = dict((gateway.effects[0].detail or {}).get("execution") or {}) if gateway.effects else {}
    evidence["real_execution_ok"] = bool(result.ok)
    evidence["execution_program_path"] = execution.get("program_path")
    evidence["execution_spec_digest_len"] = len(str(execution.get("spec_digest") or ""))
    check(
        "fixed_query/受限模式真实执行 python --version（ALLOW 网关、无审批）",
        result.ok is True and not gateway.submitted,
        {"stdout_head": (result.content or "")[:120]},
    )
    engine_facts = fixed_queries.fixed_query_facts_from_detail(
        {"execution": execution} if execution else {}
    )
    engine_kind = None if engine_facts is None else getattr(
        fixed_queries.classify_fixed_query(engine_facts), "kind", None
    )
    evidence["engine_reclassify_kind"] = engine_kind
    check("fixed_query/引擎仅凭 effect.detail 可重新算出 version", engine_kind == "version")
    check(
        "fixed_query/effect.detail 不携带工具自报 fixed_query 许可",
        "fixed_query" not in (gateway.effects[0].detail if gateway.effects else {}),
    )

    ro_gateway = FakeGateway(mode="read_only", verdict="allow")
    ro_tool = RunProgramTool(computer=sandbox(workspace), security=ro_gateway)
    ro_fixed = await ro_tool.run(program=PY, args=["--version"], cwd=str(workspace), timeout=60)
    ro_effects_after_fixed = len(ro_gateway.effects)
    ro_other = await ro_tool.run(program=PY, args=["-c", "print(1)"], cwd=str(workspace), timeout=60)
    check(
        "read_only/固定查询可执行、非固定查询被拒绝且不进入引擎",
        ro_fixed.ok is True
        and ro_other.ok is False
        and ro_other.code == "readonly_execution_denied"
        and len(ro_gateway.effects) == ro_effects_after_fixed,
        {"denied_error": (ro_other.error or "")[:120]},
    )
    RESULTS[-1]["detail"]["readonly_denied_content"] = (ro_other.content or "")[:200]
    EVIDENCE_EXTRA["fixed_queries"] = evidence


# --------------------------------------------------------------------------
# 2. 永久删除：三种模式 + 确认绑定 + 重放重核对（真实删除）
# --------------------------------------------------------------------------


async def verify_permanent_delete(workspace: Path) -> None:
    evidence: dict = {}

    restricted_gateway = FakeGateway(mode="restricted", verdict="allow")
    victim = workspace / "restricted-victim.txt"
    victim.write_text("keep", encoding="utf-8")
    tool = RunCmdTool(computer=sandbox(workspace), security=restricted_gateway)
    pending, operation = await submit_operation(tool, cmd=delete_command(victim))
    payload = dict(operation.payload)
    binding = dict(operation.fingerprint.get("permanent_delete") or {})
    evidence["restricted_pending"] = {
        "payload_explicit": payload.get("explicit_confirmation_required"),
        "label": operation.label,
        "identity_set": binding.get("identity_set"),
        "victim_still_exists": victim.exists(),
    }
    check(
        "permanent_delete/restricted+模型 ALLOW 只提交明确确认、不删除",
        pending.code == "pending_approval"
        and payload.get("explicit_confirmation_required") is True
        and payload.get("recycle_bin") is False
        and bool(binding.get("identity_set"))
        and victim.exists(),
    )

    ctx = replay_ctx(operation)
    replay = await execute_proc_shell(
        operation.payload, ctx, computer=sandbox(workspace), security=None
    )
    evidence["approved_replay"] = {
        "ok": replay.ok,
        "summary": replay.summary[:200],
        "recheck": dict(replay.detail.get("recheck") or {}),
        "victim_deleted": not victim.exists(),
    }
    check(
        "permanent_delete/明确人工确认后真实执行删除（不进回收站）",
        replay.ok is True and not victim.exists(),
        {"permit": (replay.detail.get("permit") or {}).get("source")},
    )
    check(
        "permanent_delete/执行前重核对记录 local_fallback 检查项",
        str((replay.detail.get("recheck") or {}).get("mode")) == "local_fallback"
        and "delete_object_identity" in ((replay.detail.get("recheck") or {}).get("checks") or []),
    )

    read_only_gateway = FakeGateway(mode="read_only", verdict="allow")
    ro_victim = workspace / "readonly-victim.txt"
    ro_victim.write_text("keep", encoding="utf-8")
    ro_tool = RunCmdTool(computer=sandbox(workspace), security=read_only_gateway)
    ro_result = await ro_tool.run(cmd=delete_command(ro_victim))
    check(
        "permanent_delete/read_only 拒绝且文件保留、不提交审批",
        ro_result.ok is False
        and ro_result.code == "readonly_execution_denied"
        and ro_victim.exists()
        and not read_only_gateway.submitted,
        {"error": (ro_result.error or "")[:120]},
    )

    unrestricted_gateway = FakeGateway(mode="unrestricted", verdict="allow")
    un_victim = workspace / "unrestricted-victim.txt"
    un_victim.write_text("delete me", encoding="utf-8")
    un_tool = RunCmdTool(computer=sandbox(workspace), security=unrestricted_gateway)
    un_result = await un_tool.run(cmd=delete_command(un_victim), timeout=60)
    check(
        "permanent_delete/unrestricted 允许直接永久删除",
        un_result.ok is True and not un_victim.exists() and not unrestricted_gateway.submitted,
        {"error": (un_result.error or "")[:120]},
    )

    # 对象身份变化 → 旧确认失效
    id_gateway = FakeGateway(mode="restricted", verdict="allow")
    id_victim = workspace / "identity-victim.txt"
    id_victim.write_text("original", encoding="utf-8")
    id_tool = RunCmdTool(computer=sandbox(workspace), security=id_gateway)
    _, id_operation = await submit_operation(id_tool, cmd=delete_command(id_victim))
    id_victim.unlink()
    id_victim.mkdir()
    id_ctx = replay_ctx(id_operation)
    id_result = await execute_proc_shell(
        id_operation.payload, id_ctx, computer=sandbox(workspace), security=None
    )
    evidence["identity_change"] = {
        "ok": id_result.ok,
        "summary": id_result.summary[:200],
        "path_still_directory": id_victim.is_dir(),
    }
    check(
        "permanent_delete/对象身份变化后旧审批失效、不执行",
        id_result.ok is False and id_result.detail.get("invalidated") is True and id_victim.is_dir(),
    )

    # 要求版本变化 → 失效
    ver_gateway = FakeGateway(mode="restricted", verdict="allow")
    ver_victim = workspace / "version-victim.txt"
    ver_victim.write_text("x", encoding="utf-8")
    ver_tool = RunCmdTool(computer=sandbox(workspace), security=ver_gateway)
    _, ver_operation = await submit_operation(ver_tool, cmd=delete_command(ver_victim))
    ver_ctx = replay_ctx(ver_operation, ledger=FakeLedger(status="active", version=2))
    ver_result = await execute_proc_shell(
        ver_operation.payload, ver_ctx, computer=sandbox(workspace), security=None
    )
    check(
        "replay/要求版本变化后旧审批失效、文件保留",
        ver_result.ok is False and ver_result.detail.get("invalidated") is True and ver_victim.exists(),
        {"summary": ver_result.summary[:160]},
    )

    # 注入式 RecheckService
    svc_gateway = FakeGateway(mode="restricted", verdict="allow")
    svc_victim = workspace / "service-victim.txt"
    svc_victim.write_text("x", encoding="utf-8")
    svc_tool = RunCmdTool(computer=sandbox(workspace), security=svc_gateway)
    _, svc_operation = await submit_operation(svc_tool, cmd=delete_command(svc_victim))
    service = FakeRecheckService(ok=False, reason="要求已撤回（verify 注入）")
    svc_result = await execute_proc_shell(
        svc_operation.payload,
        replay_ctx(svc_operation, recheck=service),
        computer=sandbox(workspace),
        security=None,
    )
    check(
        "replay/注入 RecheckService 否决后不执行",
        svc_result.ok is False
        and "要求已撤回" in (svc_result.summary or "")
        and bool(service.calls)
        and svc_victim.exists(),
        {"service_calls": len(service.calls)},
    )

    # 讨论文字不绑定永久删除
    disc_gateway = FakeGateway(mode="restricted", verdict="approve")
    disc_tool = RunCmdTool(computer=sandbox(workspace), security=disc_gateway)
    _, disc_operation = await submit_operation(disc_tool, cmd="echo rm -rf /tmp/x")
    check(
        "permanent_delete/讨论文字（echo rm -rf）不产生永久删除绑定",
        not disc_operation.payload.get("permanent_delete")
        and not disc_operation.fingerprint.get("explicit_confirmation_required"),
    )

    EVIDENCE_EXTRA["permanent_delete"] = evidence


# --------------------------------------------------------------------------
# 3. 模式切换 + 历史审批（无要求绑定）
# --------------------------------------------------------------------------


async def verify_mode_and_legacy(workspace: Path) -> None:
    evidence: dict = {}

    gateway = FakeGateway(mode="unrestricted", verdict="approve")
    tool = RunProgramTool(computer=sandbox(workspace), security=gateway)
    _, operation = await submit_operation(
        tool, program=PY, args=["-c", "print(1)"], cwd=str(workspace), timeout=60
    )
    ctx = replay_ctx(operation, mode="restricted")
    result = await execute_proc_run_program(
        operation.payload, ctx, computer=sandbox(workspace), security=None
    )
    evidence["unrestricted_to_restricted"] = {
        "ok": result.ok,
        "summary": result.summary[:180],
    }
    check(
        "replay/无限制模式批准的操作在切回受限后失效",
        result.ok is False and result.detail.get("invalidated") is True,
    )

    legacy_gateway = FakeGateway(mode="restricted", verdict="approve")
    legacy_tool = RunCmdTool(computer=sandbox(workspace), security=legacy_gateway)
    _, legacy_operation = await submit_operation(legacy_tool, cmd="echo hi")
    legacy_operation.fingerprint.pop("requirement", None)
    legacy_operation.fingerprint.pop("operation_binding", None)
    legacy_ctx = replay_ctx(legacy_operation)
    legacy_result = await execute_proc_shell(
        legacy_operation.payload, legacy_ctx, computer=sandbox(workspace), security=None
    )
    evidence["legacy_without_requirement"] = {
        "ok": legacy_result.ok,
        "summary": legacy_result.summary[:200],
    }
    check(
        "replay/历史审批缺 requirement 绑定 → 不沿用旧许可",
        legacy_result.ok is False and "要求" in (legacy_result.summary or ""),
    )

    EVIDENCE_EXTRA["mode_and_legacy"] = evidence


# --------------------------------------------------------------------------
# 4. 环境接线状态（诚实记录）
# --------------------------------------------------------------------------


def verify_environment_status() -> None:
    status: dict = {
        "platform": platform.platform(),
        "python": sys.version,
        "windows": WINDOWS,
        "context_module": False,
        "require_binding_names": [],
        "recheck_module_present": False,
        "requirements_validate_binding": False,
    }
    try:
        from agent.security import context as security_context

        status["context_module"] = True
        status["require_binding_names"] = [
            name
            for name in ("current_requirement", "current_requirement_binding", "requirement_binding")
            if hasattr(security_context, name)
        ]
    except Exception as exc:  # noqa: BLE001
        status["context_error"] = type(exc).__name__
    try:
        from agent.security import recheck as recheck_module

        status["recheck_module_present"] = hasattr(recheck_module, "RecheckService")
    except Exception:  # noqa: BLE001
        status["recheck_module_present"] = False
    try:
        from agent.security.requirements import RequirementLedger

        status["requirements_validate_binding"] = hasattr(RequirementLedger, "validate_binding")
    except Exception:  # noqa: BLE001
        status["requirements_validate_binding"] = False
    EVIDENCE_EXTRA["environment"] = status
    check(
        "environment/接线状态已如实记录（缺失项在报告中列明）",
        True,
        status,
    )


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

EVIDENCE_EXTRA: dict = {}


async def main() -> int:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="r9g-w2-verify-"))
    workspace = temp_dir / "workspace"
    workspace.mkdir()
    print("R9-G W2 real-machine verify")
    print("workspace: " + str(workspace))
    print("python: " + PY)
    try:
        verify_environment_status()
        await verify_fixed_queries(workspace)
        await verify_permanent_delete(workspace)
        await verify_mode_and_legacy(workspace)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    failed = [item for item in RESULTS if not item["ok"]]
    payload = {
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "platform": platform.platform(),
        "machine": platform.node(),
        "total": len(RESULTS),
        "failed": len(failed),
        "results": RESULTS,
        "environment": EVIDENCE_EXTRA.get("environment"),
        "fixed_queries": EVIDENCE_EXTRA.get("fixed_queries"),
        "permanent_delete": EVIDENCE_EXTRA.get("permanent_delete"),
        "mode_and_legacy": EVIDENCE_EXTRA.get("mode_and_legacy"),
        "boundaries": [
            "解释器内联代码（python -c os.remove）、xargs、sudo、嵌套 shell 内的删除不属于可解析的永久删除：走普通审批流程（无法结构性证明其副作用）。",
            "固定查询只覆盖精确程序身份+精确 argv；任何未知参数/重定向/串联/同名替身都退出固定查询。",
            "本脚本的网关/台账是 fake，仅用于验证 W2 组件行为；引擎接线（contextvar、RecheckService、RequirementLedger.validate_binding）由 Lead 集成树提供。",
        ],
    }
    out_path = EVIDENCE_DIR / "verify_r9g_exec_policy.json"
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print("")
    print("EVIDENCE: " + str(out_path))
    print("TOTAL " + str(len(RESULTS)) + " checks, FAILED " + str(len(failed)))
    for item in failed:
        print("FAILED: " + item["check"] + " :: " + json.dumps(item["detail"], ensure_ascii=False)[:300])
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
