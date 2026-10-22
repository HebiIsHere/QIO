"""R9 W2 执行边界真机探针（Windows）。

契约 §3.2 要求：所有承诺用 `scripts/verify_r9_execution_boundary.py` 在真实 Windows
上取证，本脚本就是那个取证入口。它只做**真实执行**，不做源码推断：

1. Low IL 写拒绝：低完整性子进程写 Medium 文件 → PermissionError，原文件不变；
2. Job Object 收进程树：关掉 job 句柄（KILL_ON_JOB_CLOSE）后，子进程与孙进程都消失；
3. 只读模式 argv 白名单实测：run_program 的只读 argv 可执行；可写子命令与 run_shell
   被明确拒绝（中文原因），且没有走审批旁路。

输出同时写到仓库根目录 `_r9-evidence/w2-execution-boundary.txt`。
用法（在 backend 目录）：

    uv run --frozen python ..\scripts\verify_r9_execution_boundary.py

退出码：0 = 全部 PASS；1 = 有 FAIL；2 = 非 Windows（Low IL / Job 不可用）。
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

EVIDENCE_DIR = ROOT / "_r9-evidence"
EVIDENCE_FILE = EVIDENCE_DIR / "w2-execution-boundary.txt"
WINDOWS = sys.platform == "win32"

_results: list[dict] = []


def _record(name: str, status: str, detail: str) -> None:
    _results.append({"name": name, "status": status, "detail": detail})
    print(f"[{status}] {name}")
    if detail:
        for line in str(detail).splitlines():
            print("       " + line)


def _pid_alive(pid: int) -> bool:
    """内核 API 判定 pid 是否存活（不用 tasklist 文本匹配，避免误命中内存列）。"""
    if not WINDOWS:
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False
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
        return code.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def _wait_dead(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    return not _pid_alive(pid)


# --------------------------------------------------------------------------
# 1. Low IL 写拒绝
# --------------------------------------------------------------------------


def check_low_integrity_write_denied() -> str:
    from agent.tools import isolation

    if not WINDOWS:
        return "UNSUPPORTED"
    parent_integrity = isolation.integrity_of_process(os.getpid())
    if parent_integrity == isolation.LOW_INTEGRITY_SID:
        return "UNSUPPORTED: 父进程自身就是 Low 完整性，无法造出 Medium 目标文件来验证拒绝"

    base = Path(tempfile.mkdtemp(prefix="qio-r9-lowil-"))
    protected = base / "medium-protected.txt"
    protected.write_text("original", encoding="utf-8")
    scratch = base / "scratch"
    scratch.mkdir()
    child_script = (
        "import sys\n"
        "sys.stdin.readline()\n"
        "try:\n"
        "    with open(sys.argv[1], 'w', encoding='utf-8') as handle:\n"
        "        handle.write('hacked')\n"
        "except PermissionError as exc:\n"
        "    print('WRITE-DENIED', type(exc).__name__, flush=True, file=sys.stderr)\n"
        "    raise SystemExit(3)\n"
        "print('WRITE-OK', flush=True)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", child_script, str(protected)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        outcome = isolation.harden(process, scratch_dir=str(scratch), mode="read_only")
        child_integrity = isolation.integrity_of_process(process.pid)
        if process.stdin:
            process.stdin.write("go\n")
            process.stdin.flush()
        stdout, stderr = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        isolation.release(process)

    unchanged = protected.read_text(encoding="utf-8") == "original"
    detail = {
        "父进程完整性": parent_integrity,
        "子进程完整性": child_integrity,
        "要求低完整性": outcome.low_integrity_required,
        "边界核实": outcome.boundary_ok(),
        "机制": list(outcome.mechanisms),
        "子进程退出码": process.returncode,
        "stderr": stderr.strip()[-300:],
        "受保护文件未被修改": unchanged,
    }
    shutil.rmtree(base, ignore_errors=True)
    ok = (
        outcome.boundary_ok()
        and child_integrity == isolation.LOW_INTEGRITY_SID
        and process.returncode == 3
        and "PermissionError" in stderr
        and unchanged
    )
    return ("PASS" if ok else "FAIL") + "\n" + json.dumps(detail, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# 2. Job Object 收进程树
# --------------------------------------------------------------------------


def check_job_object_kills_tree() -> str:
    from agent.tools import isolation

    if not WINDOWS:
        return "UNSUPPORTED"
    child_script = (
        "import subprocess, sys, time\n"
        "sys.stdin.readline()\n"
        "grand = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "print(grand.pid, flush=True)\n"
        "time.sleep(120)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", child_script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    grandchild = 0
    try:
        outcome = isolation.harden(process)
        if process.stdin:
            process.stdin.write("go\n")
            process.stdin.flush()
        grandchild = int((process.stdout.readline() or "0").strip() or 0)
        time.sleep(0.3)
        alive_before = _pid_alive(grandchild) if grandchild else False
        # 关掉本实例持有的 job 句柄 = KILL_ON_JOB_CLOSE：内核收掉整棵树。
        isolation.release(process)
        child_dead = _wait_dead(process.pid)
        tree_dead = _wait_dead(grandchild) if grandchild else False
        with __import__("contextlib").suppress(Exception):
            process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    detail = {
        "机制": list(outcome.mechanisms),
        "limits": outcome.limits,
        "孙进程 pid": grandchild,
        "释放前孙进程存活": locals().get("alive_before", None),
        "释放后子进程消失": locals().get("child_dead", None),
        "释放后孙进程消失": locals().get("tree_dead", None),
    }
    ok = (
        "job_object" in outcome.mechanisms
        and bool(grandchild)
        and detail["释放前孙进程存活"] is True
        and detail["释放后子进程消失"] is True
        and detail["释放后孙进程消失"] is True
    )
    return ("PASS" if ok else "FAIL") + "\n" + json.dumps(detail, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# 3. 只读模式 argv 白名单实测
# --------------------------------------------------------------------------


class _AllowGateway:
    """只读模式 fake gateway：记录 effect，execute 一律 allow（执行层仍需自证只读）。"""

    def __init__(self) -> None:
        self.calls: list = []

    def mode(self):
        return "read_only"

    async def authorize(self, effect, *, ctx=None):
        self.calls.append(effect)
        from agent.tools.security_types import AuthDecision, Verdict

        return AuthDecision(verdict=Verdict.ALLOW, reason="探针：允许（执行层另有校验）")


class _NoApproval:
    async def request(self, kind, payload):  # pragma: no cover - 走到这里就是旁路
        raise AssertionError("只读模式下不应进入旧的审批旁路")


def check_readonly_argv_boundary() -> str:
    from agent.services.computer import ComputerSandbox
    from agent.tools.cmd_tools import RunCmdTool, RunProgramTool

    root = Path(tempfile.mkdtemp(prefix="qio-r9-readonly-"))
    sandbox = ComputerSandbox(resolve_root=lambda: str(root), permission_mode=lambda: "plan")
    gateway = _AllowGateway()

    async def scenario() -> dict:
        program_tool = RunProgramTool(computer=sandbox, approvals=_NoApproval(), security=gateway)
        shell_tool = RunCmdTool(computer=sandbox, approvals=_NoApproval(), security=gateway)
        readonly = await program_tool.run(program="git", args=["--version"])
        write_attempt = await program_tool.run(program="git", args=["commit", "-m", "probe"])
        shell_attempt = await shell_tool.run(cmd="echo hacked > probe.txt")
        return {"readonly": readonly, "write": write_attempt, "shell": shell_attempt}

    outcome = asyncio.run(scenario())
    created = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
    detail = {
        "只读 argv(git --version)": {
            "ok": outcome["readonly"].ok,
            "输出含 git": "git" in (outcome["readonly"].content or "").lower(),
        },
        "可写子命令(git commit)": {
            "ok": outcome["write"].ok,
            "原因": outcome["write"].error,
        },
        "自由 shell(echo > 文件)": {
            "ok": outcome["shell"].ok,
            "原因": outcome["shell"].error,
        },
        "工作区里新增的文件": created,
        "经过统一授权入口的 effect 数": len(gateway.calls),
    }
    shutil.rmtree(root, ignore_errors=True)
    ok = (
        outcome["readonly"].ok is True
        and "git" in (outcome["readonly"].content or "").lower()
        and outcome["write"].ok is False
        and "只读模式" in (outcome["write"].error or "")
        and outcome["shell"].ok is False
        and "只读模式" in (outcome["shell"].error or "")
        and created == []
        and len(gateway.calls) >= 1
    )
    return ("PASS" if ok else "FAIL") + "\n" + json.dumps(detail, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# 4. SandboxExecutor 只读拒绝（任意脚本），不启动进程
# --------------------------------------------------------------------------


def check_sandbox_refuses_scripts_in_read_only() -> str:
    from agent.tools.sandbox import SandboxExecutor

    async def scenario():
        executor = SandboxExecutor(mode_provider=lambda: "read_only")
        return await executor.execute("print('should not run')", {})

    result = asyncio.run(scenario())
    detail = {"ok": result.ok, "error": result.error, "category": result.category}
    ok = (
        result.ok is False
        and result.category == "permission"
        and "只读模式" in (result.error or "")
    )
    return ("PASS" if ok else "FAIL") + "\n" + json.dumps(detail, ensure_ascii=False, indent=2)


CHECKS = (
    ("Low IL 写拒绝（Medium 文件 PermissionError）", check_low_integrity_write_denied),
    ("Job Object 收进程树（KILL_ON_JOB_CLOSE）", check_job_object_kills_tree),
    ("只读模式 argv 白名单实测（run_program / run_shell）", check_readonly_argv_boundary),
    ("只读模式拒绝任意脚本（执行层兜底）", check_sandbox_refuses_scripts_in_read_only),
)


def main() -> int:
    started = datetime.now(timezone.utc).isoformat()
    lines = [
        "R9 W2 执行边界真机探针",
        "=" * 72,
        f"时间（UTC）: {started}",
        f"平台: {platform.platform()}",
        f"Python: {sys.version.split()[0]} ({sys.executable})",
        f"仓库根: {ROOT}",
        f"Windows: {WINDOWS}",
        "",
    ]
    print("\n".join(lines[:6]))
    if not WINDOWS:
        _record("平台检查", "UNSUPPORTED", "Low IL 与 Job Object 是 Windows 内核能力，本机不是 Windows")
    else:
        for name, fn in CHECKS:
            try:
                outcome = fn()
            except Exception:  # noqa: BLE001 - 探针要记录事实而不是崩掉
                outcome = "FAIL\n" + traceback.format_exc(limit=6)
            status, _, detail = str(outcome).partition("\n")
            _record(name, status, detail)
    passed = sum(1 for item in _results if item["status"] == "PASS")
    failed = sum(1 for item in _results if item["status"] == "FAIL")
    unsupported = sum(1 for item in _results if item["status"].startswith("UNSUPPORTED"))
    lines.append("")
    for item in _results:
        lines.append(f"[{item['status']}] {item['name']}")
        if item["detail"]:
            lines.append(item["detail"])
        lines.append("")
    lines.append(f"结论: PASS={passed} FAIL={failed} UNSUPPORTED={unsupported}")
    text = "\n".join(lines) + "\n"
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    EVIDENCE_FILE.write_text(text, encoding="utf-8")
    print(f"\n证据已写入: {EVIDENCE_FILE}")
    print(f"结论: PASS={passed} FAIL={failed} UNSUPPORTED={unsupported}")
    if failed:
        return 1
    if not WINDOWS:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
