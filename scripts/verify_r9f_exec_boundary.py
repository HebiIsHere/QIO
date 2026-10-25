#!/usr/bin/env python
"""R9-F W2 执行边界真机验证（Windows 为主）。

验证契约 §3.3 的受控启动入口在**真机**上到底做到了什么、没做到什么：

1. 范围内必要写入可用；范围外写入被 Low IL（Windows MIC）内核拒绝；
2. 解释器内部删除不能绕过写边界（Low 进程删 Medium 文件被拒）；
3. 内部运行数据未经三次确认 → launcher 拒绝启动；
4. 后代进程遵守同一隔离边界（继承 Low 令牌）且 Job Object 收树；
5. permit 与 spec 不一致（程序被换/argv 改/cwd 改/脚本内容改）→ 拒绝启动；
6. 审批重放一致 → 执行；payload/指纹不一致 → 不执行；重放网关不再无条件 ALLOW；
7. 只读模式不能被审批/许可绕过。

用法（在 backend 目录下，用项目锁定的解释器）：
    cd backend
    uv run --frozen python ../scripts/verify_r9f_exec_boundary.py

证据写到仓库 _r9f-evidence/w2-exec/（latest.json/latest.txt + 时间戳文件）。
脚本不联网、不需要真实 API Key；退出码 0 = 无 FAIL，1 = 有 FAIL。
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from agent.security import launch as launch_boundary  # noqa: E402
from agent.security.operations import ExecutionContext, PendingOperation  # noqa: E402
from agent.services.computer import ComputerSandbox  # noqa: E402
from agent.tools import security_gate  # noqa: E402
from agent.tools.approved_operations import (  # noqa: E402
    _ApprovedReplayGateway,
    execute_proc_run_program,
)

PY = sys.executable
WINDOWS = sys.platform == "win32"
EVIDENCE_DIR = REPO_ROOT / "_r9f-evidence" / "w2-exec"
# 事前置约束（Low 标签）需要目标目录的 WRITE_OWNER：系统临时目录（用户目录）满足；
# 证据仍然写回仓库 _r9f-evidence/。
RUN_TMP = Path(tempfile.mkdtemp(prefix="r9f-w2-exec-"))
RESULTS: list[dict] = []


class _Unsupported(RuntimeError):
    pass


def _record(name: str, status: str, detail: dict, *, title: str = "") -> None:
    RESULTS.append(
        {"check": name, "title": title or name, "status": status, "detail": detail}
    )
    icon = {"PASS": "[PASS]", "FAIL": "[FAIL]", "UNSUPPORTED": "[UNSUPPORTED]"}[status]
    print(icon, title or name)
    print(json.dumps(detail, ensure_ascii=False, indent=2)[:4000])


async def _check(name: str, title: str, fn) -> None:
    try:
        detail = await fn()
        _record(name, "PASS", detail or {}, title=title)
    except _Unsupported as exc:
        _record(name, "UNSUPPORTED", {"reason": str(exc)}, title=title)
    except Exception as exc:  # noqa: BLE001 - 验证脚本要如实报告失败
        _record(
            name,
            "FAIL",
            {
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc()[-3000:],
            },
            title=title,
        )


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = k32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259
    finally:
        k32.CloseHandle(handle)


def _wait_dead(pid: int, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


def contextlib_suppress():
    import contextlib

    return contextlib.suppress(Exception)


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


WRITE_SCOPE_CODE = """import os
try:
    open(r'{victim}', 'w', encoding='utf-8').write('hacked')
    print('OUTSIDE_WRITE_OK')
except PermissionError as exc:
    print('OUTSIDE_WRITE_DENIED', type(exc).__name__)
open(r'{inside}', 'w', encoding='utf-8').write('ok')
print('INSIDE_WRITE_OK')
"""

DELETE_CODE = """import os
try:
    os.remove(r'{victim}')
    print('DELETE_OK')
except PermissionError as exc:
    print('DELETE_DENIED', type(exc).__name__)
"""

INTERNAL_CODE = """print('INTERNAL_DATA_REACHED')
print(r'{target}')
"""

CHILD_BOUNDARY_CODE = """import pathlib, sys
try:
    pathlib.Path(sys.argv[1]).write_text('child-hacked', encoding='utf-8')
    print('CHILD_WRITE_OK')
except PermissionError as exc:
    print('CHILD_WRITE_DENIED', type(exc).__name__)
"""

PARENT_BOUNDARY_CODE = """import subprocess, sys
child_code = {child_code!r}
child = [sys.executable, '-c', child_code, r'{victim}']
out = subprocess.run(child, capture_output=True, text=True)
print('PARENT_SAW', out.stdout.strip())
"""

REAP_CODE = """import subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])
open(r'{pid_file}', 'w', encoding='utf-8').write(str(child.pid))
print('CHILD_PID', child.pid, flush=True)
time.sleep(300)
"""


async def check_write_scope() -> dict:
    if not WINDOWS:
        raise _Unsupported("Low IL 是 Windows 内核能力；POSIX 上 launcher 明确标注无强制隔离")
    root = RUN_TMP / "write_scope"
    allowed = root / "allowed"
    protected = root / "protected"
    allowed.mkdir(parents=True)
    protected.mkdir(parents=True)
    victim = protected / "medium.txt"
    victim.write_text("original", encoding="utf-8")
    inside = allowed / "inside.txt"
    code = WRITE_SCOPE_CODE.format(victim=victim, inside=inside)
    spec = launch_boundary.LaunchSpec(
        kind="script", program=PY, script_source=code, script_name="write_scope.py", timeout=60
    )
    permit = launch_boundary.build_permit(spec, mode="restricted", allowed_roots=(str(allowed),))
    launcher = launch_boundary.ControlledLauncher(
        mode_provider=lambda: "restricted", guard_enabled=False
    )
    result = await launcher.launch(spec, permit, enforce_write_scope=True)
    info = result.isolation or {}
    write_scope = info.get("write_scope") or {}
    assert result.ok is True, (result.error, result.stderr)
    assert "OUTSIDE_WRITE_DENIED" in result.stdout, result.stdout
    assert "INSIDE_WRITE_OK" in result.stdout, result.stdout
    assert victim.read_text(encoding="utf-8") == "original", "范围外文件被改写"
    assert inside.read_text(encoding="utf-8") == "ok", "范围内写入不可用"
    assert "low_integrity" in info.get("mechanisms", []), info
    assert write_scope.get("pre_enforced") is True
    return {
        "allowed_root": str(allowed),
        "outside_target": str(victim),
        "stdout": result.stdout.strip().splitlines(),
        "isolation_mechanisms": info.get("mechanisms"),
        "write_scope": {
            "pre_enforced": write_scope.get("pre_enforced"),
            "labeled_low_during_execution": write_scope.get("labeled_low_during_execution"),
            "restored_after_execution": write_scope.get("restored_after_execution"),
        },
        "capability_boundaries": info.get("capability_boundaries"),
    }


async def check_delete_not_bypass() -> dict:
    if not WINDOWS:
        raise _Unsupported("Low IL 是 Windows 内核能力；POSIX 无强制写边界")
    root = RUN_TMP / "delete"
    allowed = root / "allowed"
    protected = root / "protected"
    allowed.mkdir(parents=True)
    protected.mkdir(parents=True)
    victim = protected / "keep.txt"
    victim.write_text("keep", encoding="utf-8")
    spec = launch_boundary.LaunchSpec(
        kind="script", program=PY, script_source=DELETE_CODE.format(victim=victim),
        script_name="delete.py",
    )
    permit = launch_boundary.build_permit(spec, mode="restricted", allowed_roots=(str(allowed),))
    launcher = launch_boundary.ControlledLauncher(
        mode_provider=lambda: "restricted", guard_enabled=False
    )
    result = await launcher.launch(spec, permit, enforce_write_scope=True)
    assert result.ok is True, (result.error, result.stderr)
    assert "DELETE_DENIED" in result.stdout, result.stdout
    assert victim.exists() and victim.read_text(encoding="utf-8") == "keep"
    return {
        "target": str(victim),
        "child_output": result.stdout.strip().splitlines(),
        "note": (
            "Low IL 阻止写向上（含删除高完整性对象）；若文件本身是 Low 标签则删除会成功，"
            "这属于如实标注的能力边界（获准写范围内可以 unlink，不能强制进回收站）"
        ),
    }


async def check_internal_data_confirmation() -> dict:
    data_dir = RUN_TMP / "internal_data"
    data_dir.mkdir(parents=True)
    target = data_dir / "qio.db"
    target.write_text("db", encoding="utf-8")
    launcher = launch_boundary.ControlledLauncher(
        internal_data_paths=lambda: [data_dir], guard_enabled=False
    )
    spec = launch_boundary.LaunchSpec(
        kind="script", program=PY,
        script_source=INTERNAL_CODE.format(target=target), script_name="internal.py",
    )
    unconfirmed = launch_boundary.build_permit(spec, mode="unrestricted")
    refused = await launcher.launch(spec, unconfirmed)
    assert refused.ok is False, "未确认内部数据时不得启动"
    assert refused.category == "permission", refused.category
    assert "三次确认" in (refused.error or ""), refused.error
    confirmed = launch_boundary.build_permit(
        spec, mode="unrestricted", internal_data_confirmed=True
    )
    allowed = await launcher.launch(spec, confirmed)
    assert allowed.ok is True, (allowed.error, allowed.stderr)
    assert "INTERNAL_DATA_REACHED" in allowed.stdout
    return {
        "internal_root": str(data_dir),
        "unconfirmed": {"ok": refused.ok, "category": refused.category, "error": refused.error},
        "confirmed": {"ok": allowed.ok, "stdout": allowed.stdout.strip().splitlines()},
    }


async def check_descendants_same_boundary() -> dict:
    if not WINDOWS:
        raise _Unsupported("Low IL 是 Windows 内核能力")
    root = RUN_TMP / "descendants"
    allowed = root / "allowed"
    protected = root / "protected"
    allowed.mkdir(parents=True)
    protected.mkdir(parents=True)
    victim = protected / "medium.txt"
    victim.write_text("original", encoding="utf-8")
    code = PARENT_BOUNDARY_CODE.format(child_code=CHILD_BOUNDARY_CODE, victim=victim)
    spec = launch_boundary.LaunchSpec(
        kind="script", program=PY, script_source=code, script_name="descendants.py", timeout=60
    )
    permit = launch_boundary.build_permit(spec, mode="restricted", allowed_roots=(str(allowed),))
    launcher = launch_boundary.ControlledLauncher(
        mode_provider=lambda: "restricted", guard_enabled=False
    )
    result = await launcher.launch(spec, permit, enforce_write_scope=True)
    assert result.ok is True, (result.error, result.stderr)
    assert "CHILD_WRITE_DENIED" in result.stdout, result.stdout
    assert victim.read_text(encoding="utf-8") == "original"
    return {
        "victim": str(victim),
        "stdout": result.stdout.strip().splitlines(),
        "note": "子进程继承父进程的 Low 完整性令牌；隔离对后代同样生效",
    }


async def check_job_object_reaps_tree() -> dict:
    root = RUN_TMP / "reap"
    root.mkdir(parents=True, exist_ok=True)
    pid_file = root / "child.pid"
    spec = launch_boundary.LaunchSpec(
        kind="script", program=PY,
        script_source=REAP_CODE.format(pid_file=pid_file),
        script_name="reap.py", timeout=2.0,
    )
    permit = launch_boundary.build_permit(spec, mode="unrestricted")
    launcher = launch_boundary.ControlledLauncher(mode_provider=lambda: "unrestricted")
    result = await launcher.launch(spec, permit)
    assert result.category == "timeout", result.category
    child_pid = None
    if pid_file.is_file():
        with contextlib_suppress():
            child_pid = int(pid_file.read_text(encoding="utf-8").strip())
    if not child_pid:
        for line in (result.stdout or "").splitlines():
            if line.startswith("CHILD_PID"):
                child_pid = int(line.split()[1])
    assert child_pid, (result.stdout, result.stderr, result.error)
    assert _wait_dead(child_pid, timeout=8.0), f"后代进程 {child_pid} 在超时收尾后仍存活"
    return {
        "child_pid": child_pid,
        "timeout_error": result.error,
        "isolation_mechanisms": (result.isolation or {}).get("mechanisms"),
        "note": "script 启动带握手：Job Object 在放行脚本前已指派，后代必然在 job 内",
    }


async def check_spec_mismatch_rejected() -> dict:
    marker = RUN_TMP / "should_not_run.marker"
    base_code = "open(r'{marker}', 'w').write('ran')\nprint('RAN')\n".format(marker=marker)
    spec = launch_boundary.LaunchSpec(
        kind="script", program=PY, script_source=base_code, script_name="mismatch.py"
    )
    permit = launch_boundary.build_permit(spec, mode="unrestricted")
    launcher = launch_boundary.ControlledLauncher()
    mutations = {
        "argv_changed": launch_boundary.LaunchSpec(
            kind="script", program=PY, script_source=base_code, argv=("--evil",),
            script_name="mismatch.py",
        ),
        "cwd_changed": launch_boundary.LaunchSpec(
            kind="script", program=PY, script_source=base_code, cwd=str(RUN_TMP),
            script_name="mismatch.py",
        ),
        "script_content_changed": launch_boundary.LaunchSpec(
            kind="script", program=PY, script_source="print('EVIL')\n",
            script_name="mismatch.py",
        ),
        "program_changed": launch_boundary.LaunchSpec(
            kind="script", program=str(RUN_TMP / "other-interpreter.exe"),
            script_source=base_code, script_name="mismatch.py",
        ),
    }
    outcomes: dict[str, dict] = {}
    for name, mutated in mutations.items():
        result = await launcher.launch(mutated, permit)
        outcomes[name] = {"ok": result.ok, "category": result.category, "error": result.error}
        assert result.ok is False, f"{name} 竟然启动了"
        assert result.category in ("permit_mismatch", "startup"), result.category
    assert not marker.exists(), "许可失效时不得有任何脚本被执行"

    program_copy = RUN_TMP / ("python-copy.exe" if WINDOWS else "python-copy")
    shutil.copyfile(PY, program_copy)
    replaced_spec = launch_boundary.LaunchSpec(kind="program", program=str(program_copy))
    replaced_permit = launch_boundary.build_permit(replaced_spec, mode="unrestricted")
    with open(program_copy, "ab") as handle:
        handle.write(b"\n# changed after approval\n")
    replaced = await launcher.launch(replaced_spec, replaced_permit)
    outcomes["program_file_replaced"] = {
        "ok": replaced.ok, "category": replaced.category, "error": replaced.error
    }
    assert replaced.ok is False, "程序文件被替换后仍然启动"
    return outcomes


async def check_post_exec_identity_recheck() -> dict:
    program_copy = RUN_TMP / ("python-post.exe" if WINDOWS else "python-post")
    shutil.copyfile(PY, program_copy)
    spec = launch_boundary.LaunchSpec(kind="program", program=str(program_copy))
    permit = launch_boundary.build_permit(spec, mode="unrestricted")
    with open(program_copy, "ab") as handle:
        handle.write(b"\n# changed during execution\n")
    post = launch_boundary.ControlledLauncher().postflight(spec, permit)
    assert post.get("changed") is True, post
    return {
        "changed": post.get("changed"),
        "expected": post.get("expected"),
        "actual": post.get("digest"),
        "detail": post.get("detail"),
    }


def _approval_fingerprint(computer, payload: dict, spec) -> dict:
    facts = launch_boundary.build_spec_facts(spec, computer=computer)
    return {
        "kind": "proc.run_program",
        "argv": [str(payload.get("program") or ""), *[str(a) for a in payload.get("args") or []]],
        "cwd": payload.get("cwd"),
        "spec_digest": launch_boundary.compute_spec_digest(spec, computer=computer),
        "program_sha256": (facts.get("program_facts") or {}).get("sha256"),
        "allowed_roots": list(launch_boundary.write_roots_for(computer, "restricted")),
        "payload_digest": security_gate.text_sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True)
        ),
        "internal_data_roots": [],
    }


async def check_approval_replay() -> dict:
    workspace = RUN_TMP / "workspace"
    workspace.mkdir(parents=True)
    computer = ComputerSandbox(
        resolve_root=lambda: str(workspace), permission_mode=lambda: "restricted"
    )
    program = PY
    args = ["-c", "print('R9F_APPROVED_REPLAY_OK')"]
    payload = {"program": program, "args": args, "cwd": str(workspace)}
    spec = launch_boundary.LaunchSpec(
        kind="program", program=program, argv=tuple(args), cwd=payload["cwd"], timeout=60
    )
    fingerprint = _approval_fingerprint(computer, payload, spec)
    effect = security_gate.execute_effect(
        "run_program", program, args, cwd=payload["cwd"], purpose="验证审批重放"
    )
    operation = PendingOperation(
        kind="proc.run_program", label="运行验证程序", payload=dict(payload),
        effect=security_gate.effect_to_dict(effect), fingerprint=dict(fingerprint),
        mode_at_request="restricted", source="tool",
    )
    ctx = ExecutionContext(
        approval_id="appr_verify", operation=operation,
        mode_at_request="restricted", mode_at_approval="restricted",
        engine=None, mode_getter=lambda: "restricted",
    )
    executed = await execute_proc_run_program(payload, ctx, computer=computer, security=None)
    assert executed.ok is True, (executed.error, executed.summary)
    assert "R9F_APPROVED_REPLAY_OK" in (executed.detail.get("content") or "")
    permit_info = executed.detail.get("permit") or {}
    assert permit_info.get("source") == "approval"
    assert permit_info.get("approval_id") == "appr_verify"

    tampered_payload = dict(payload, args=["-c", "print('TAMPERED')"])
    rejected_payload = await execute_proc_run_program(
        tampered_payload, ctx, computer=computer, security=None
    )
    assert rejected_payload.ok is False and rejected_payload.detail.get("invalidated") is True

    bad_ctx = ExecutionContext(
        approval_id="appr_verify",
        operation=PendingOperation(
            kind="proc.run_program", label="运行验证程序", payload=dict(payload),
            effect=security_gate.effect_to_dict(effect),
            fingerprint=dict(fingerprint, spec_digest="deadbeef" * 8),
            mode_at_request="restricted", source="tool",
        ),
        mode_at_request="restricted", mode_at_approval="restricted",
        engine=None, mode_getter=lambda: "restricted",
    )
    rejected_fingerprint = await execute_proc_run_program(
        payload, bad_ctx, computer=computer, security=None
    )
    assert rejected_fingerprint.ok is False

    gateway = _ApprovedReplayGateway(None, operation=operation, approval_id="appr_verify")
    allowed = await gateway.authorize(effect)
    denied = await gateway.authorize(
        security_gate.execute_effect(
            "run_program", program, ["-c", "print('OTHER')"], cwd=payload["cwd"], purpose="x"
        )
    )
    no_record = await _ApprovedReplayGateway(None).authorize(effect)
    assert security_gate.verdict_of(allowed) == "allow"
    assert security_gate.verdict_of(denied) == "deny"
    assert security_gate.verdict_of(no_record) == "deny"
    return {
        "executed": {
            "ok": executed.ok,
            "content": (executed.detail.get("content") or "").strip(),
            "permit_source": permit_info.get("source"),
            "approval_id": permit_info.get("approval_id"),
            "spec_digest_match": permit_info.get("spec_digest") == fingerprint["spec_digest"],
        },
        "payload_changed": {"ok": rejected_payload.ok, "summary": rejected_payload.summary},
        "fingerprint_changed": {"ok": rejected_fingerprint.ok},
        "gateway": {
            "matching": security_gate.verdict_of(allowed),
            "mismatched_argv": security_gate.verdict_of(denied),
            "no_approval_record": security_gate.verdict_of(no_record),
        },
    }


async def check_script_change_invalidates_approval() -> dict:
    """批准后、执行前把「作为 argv 传入的脚本文件」内容换掉 → 旧许可失效，篡改脚本不执行。"""
    workspace = RUN_TMP / "script_change"
    workspace.mkdir(parents=True)
    computer = ComputerSandbox(
        resolve_root=lambda: str(workspace), permission_mode=lambda: "restricted"
    )
    script = workspace / "job.py"
    original_marker = workspace / "original.marker"
    tampered_marker = workspace / "tampered.marker"
    script.write_text(
        "open(r'" + str(original_marker) + "', 'w').write('original')",
        encoding="utf-8",
    )
    payload = {"program": PY, "args": [str(script)], "cwd": str(workspace)}
    spec = launch_boundary.LaunchSpec(
        kind="program", program=PY, argv=(str(script),), cwd=payload["cwd"], timeout=60
    )
    fingerprint = _approval_fingerprint(computer, payload, spec)
    effect = security_gate.execute_effect(
        "run_program", PY, [str(script)], cwd=payload["cwd"], purpose="验证脚本内容重核对"
    )
    operation = PendingOperation(
        kind="proc.run_program", label="运行脚本", payload=dict(payload),
        effect=security_gate.effect_to_dict(effect), fingerprint=dict(fingerprint),
        mode_at_request="restricted", source="tool",
    )
    ctx = ExecutionContext(
        approval_id="appr_script_change", operation=operation,
        mode_at_request="restricted", mode_at_approval="restricted",
        engine=None, mode_getter=lambda: "restricted",
    )
    # 批准之前：脚本内容换成写 tampered.marker（argv 不变）
    script.write_text(
        "open(r'" + str(tampered_marker) + "', 'w').write('tampered')",
        encoding="utf-8",
    )
    rejected = await execute_proc_run_program(
        payload, ctx, computer=computer, security=None
    )
    assert rejected.ok is False, "脚本内容变化后旧许可仍被执行"
    assert rejected.detail.get("invalidated") is True
    assert not tampered_marker.exists(), "篡改后的脚本不得执行"
    assert not original_marker.exists(), "旧脚本也不得借旧许可执行"

    # 正对照：内容不变时同一流程正常执行
    control_script = workspace / "control.py"
    control_marker = workspace / "control.marker"
    control_script.write_text(
        "open(r'" + str(control_marker) + "', 'w').write('control')",
        encoding="utf-8",
    )
    control_payload = {"program": PY, "args": [str(control_script)], "cwd": str(workspace)}
    control_spec = launch_boundary.LaunchSpec(
        kind="program", program=PY, argv=(str(control_script),), cwd=str(workspace), timeout=60
    )
    control_ctx = ExecutionContext(
        approval_id="appr_script_control",
        operation=PendingOperation(
            kind="proc.run_program", label="运行脚本", payload=dict(control_payload),
            effect=security_gate.effect_to_dict(
                security_gate.execute_effect(
                    "run_program", PY, [str(control_script)], cwd=str(workspace),
                    purpose="正对照",
                )
            ),
            fingerprint=_approval_fingerprint(computer, control_payload, control_spec),
            mode_at_request="restricted", source="tool",
        ),
        mode_at_request="restricted", mode_at_approval="restricted",
        engine=None, mode_getter=lambda: "restricted",
    )
    executed = await execute_proc_run_program(
        control_payload, control_ctx, computer=computer, security=None
    )
    assert executed.ok is True, (executed.error, executed.summary)
    assert control_marker.exists(), "内容未变时应当正常执行"
    return {
        "script": str(script),
        "rejected": {
            "ok": rejected.ok,
            "invalidated": rejected.detail.get("invalidated"),
            "summary": rejected.summary,
        },
        "tampered_marker_exists": tampered_marker.exists(),
        "control_executed": executed.ok,
        "control_marker_exists": control_marker.exists(),
        "note": "spec_digest 覆盖 argv 中脚本文件的内容 sha256；批准后内容变化 → 许可失效，不执行",
    }


async def check_read_only_cannot_be_bypassed() -> dict:
    workspace = RUN_TMP / "workspace_ro"
    workspace.mkdir(parents=True)
    computer = ComputerSandbox(
        resolve_root=lambda: str(workspace), permission_mode=lambda: "read_only"
    )
    payload = {"program": "git", "args": ["--version"], "cwd": str(workspace)}
    spec = launch_boundary.LaunchSpec(
        kind="program", program="git", argv=("--version",), cwd=str(workspace)
    )
    fingerprint = _approval_fingerprint(computer, payload, spec)
    effect = security_gate.execute_effect(
        "run_program", "git", ["--version"], cwd=payload["cwd"], purpose="只读验证"
    )
    operation = PendingOperation(
        kind="proc.run_program", label="只读验证", payload=dict(payload),
        effect=security_gate.effect_to_dict(effect), fingerprint=dict(fingerprint),
        mode_at_request="restricted",
    )
    ctx = ExecutionContext(
        approval_id="appr_ro", operation=operation,
        mode_at_request="restricted", mode_at_approval="restricted",
        engine=None, mode_getter=lambda: "read_only",
    )
    result = await execute_proc_run_program(payload, ctx, computer=computer, security=None)
    assert result.ok is False, "只读模式下批准过的程序仍然被执行"
    assert result.detail.get("invalidated") is True
    assert "只读" in (result.summary or "") or "只读" in (result.error or "")

    class _ReadOnlySecurity:
        def mode(self):
            return "read_only"

    gateway = _ApprovedReplayGateway(
        _ReadOnlySecurity(), operation=operation, approval_id="appr_ro"
    )
    denied = await gateway.authorize(effect)
    assert security_gate.verdict_of(denied) == "deny"
    return {
        "replay": {"ok": result.ok, "summary": result.summary},
        "gateway": {"verdict": security_gate.verdict_of(denied), "reason": denied.reason},
    }


async def check_network_boundary_honesty() -> dict:
    spec = launch_boundary.LaunchSpec(kind="program", program=PY, argv=("-c", "print('NET')"))
    with_hosts = launch_boundary.build_permit(
        spec, mode="unrestricted", network_hosts=("example.com",)
    )
    refused = await launch_boundary.ControlledLauncher().launch(spec, with_hosts)
    assert refused.ok is False and refused.category == "unsupported"
    plain = launch_boundary.build_permit(spec, mode="unrestricted")
    allowed = await launch_boundary.ControlledLauncher().launch(spec, plain)
    assert allowed.ok is True, allowed.error
    boundaries = (allowed.isolation or {}).get("capability_boundaries") or []
    assert any("network_not_isolated" in item for item in boundaries), boundaries
    return {
        "declared_hosts": {
            "ok": refused.ok, "category": refused.category, "error": refused.error
        },
        "not_declared": {
            "ok": allowed.ok,
            "capability_boundaries": boundaries,
            "note": "网络没有强制隔离：能做的是如实标注 + 需要主机白名单时明确「暂不可用」",
        },
    }


def _git(args: list[str]) -> str:
    try:
        done = subprocess.run(
            ["git", *args], cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=15
        )
        return done.stdout.strip()
    except Exception:  # noqa: BLE001 - 证据缺失不阻断验证
        return ""


async def main() -> int:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    RUN_TMP.mkdir(parents=True, exist_ok=True)
    header = {
        "title": "R9-F W2 统一程序执行边界真机验证",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "python": sys.version,
        "executable": sys.executable,
        "repo_root": str(REPO_ROOT),
        "branch": _git(["rev-parse", "--abbrev-ref", "HEAD"]),
        "head": _git(["rev-parse", "HEAD"]),
        "windows": WINDOWS,
    }
    print("=" * 78)
    print(header["title"])
    print(json.dumps(header, ensure_ascii=False, indent=2))
    print("=" * 78)

    checks = [
        ("write_scope", "范围内必要写入可用；范围外写入被 Low IL 内核拒绝", check_write_scope),
        ("delete_boundary", "解释器内部删除不能绕过写边界（Low 删 Medium 被拒）", check_delete_not_bypass),
        ("descendants_boundary", "后代进程遵守同一 Low IL 边界", check_descendants_same_boundary),
        ("job_object_reap", "Job Object 收树：超时后后代进程全部消失", check_job_object_reaps_tree),
        ("internal_data", "内部运行数据未经三次确认 → launcher 拒绝", check_internal_data_confirmation),
        ("spec_mismatch", "permit 与 spec 不一致（程序/argv/cwd/脚本内容/程序文件）→ 拒绝启动", check_spec_mismatch_rejected),
        ("post_identity", "执行后重算 spec_digest（身份变化 → 标记变化）", check_post_exec_identity_recheck),
        ("approval_replay", "审批重放：一致→执行 / payload·指纹不一致→不执行 / 网关不再无条件 ALLOW", check_approval_replay),
        ("script_change_invalidates", "批准后脚本内容变化 → 旧许可失效、篡改脚本不执行", check_script_change_invalidates_approval),
        ("read_only_no_bypass", "只读模式不能被审批/许可绕过", check_read_only_cannot_be_bypassed),
        ("network_honesty", "网络边界如实标注（白名单明确不可用；不假装有强制隔离）", check_network_boundary_honesty),
    ]
    for name, title, fn in checks:
        await _check(name, title, fn)

    counts = {"PASS": 0, "FAIL": 0, "UNSUPPORTED": 0}
    for item in RESULTS:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    passed = counts.get("PASS", 0)
    failed = counts.get("FAIL", 0)
    unsupported = counts.get("UNSUPPORTED", 0)
    summary = {
        **header,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "counts": {"pass": passed, "fail": failed, "unsupported": unsupported},
        "conclusion": "PASS" if failed == 0 else "FAIL",
        "results": RESULTS,
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    _write_json(EVIDENCE_DIR / f"verify-{stamp}.json", summary)
    _write_json(EVIDENCE_DIR / "latest.json", summary)
    lines = [
        summary["title"],
        "=" * 78,
        f"时间（UTC）: {summary['finished_at']}",
        f"平台: {summary['platform']}",
        f"Python: {summary['python']}",
        f"仓库: {summary['repo_root']}  分支: {summary['branch']}  HEAD: {summary['head']}",
        "",
    ]
    for item in RESULTS:
        lines.append(f"[{item['status']}] {item['title']}")
        lines.append(json.dumps(item["detail"], ensure_ascii=False, indent=2)[:6000])
        lines.append("")
    lines.append(f"结论: PASS={passed} FAIL={failed} UNSUPPORTED={unsupported}")
    text = "\n".join(lines)
    (EVIDENCE_DIR / f"verify-{stamp}.txt").write_text(text, encoding="utf-8")
    (EVIDENCE_DIR / "latest.txt").write_text(text, encoding="utf-8")
    print("=" * 78)
    print(f"结论: PASS={passed} FAIL={failed} UNSUPPORTED={unsupported}")
    print(f"证据: {EVIDENCE_DIR}")
    shutil.rmtree(RUN_TMP, ignore_errors=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
