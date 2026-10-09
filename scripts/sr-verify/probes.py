"""独立受控反例构造器（契约 6，冻结契约 2026-10-09）。

角色：Agent F 只写验证与证据，不改生产实现。每个 `probe_*` 在临时环境里
真实复现一类问题，返回可 JSON 化的证据 dict —— 只含布尔/数值/线程号/相对
时间戳/无敏感语义的截断文本，**不含任何真实或模拟密钥原文**。

运行方式（backend 目录内）：

    uv run --frozen pytest tests/test_sr_verify_*

或独立运行（任何目录）：

    uv run --frozen python ../scripts/sr-verify/run_all.py

判定：每个 probe 的证据与 `EXPECTATIONS` 对比。所有布尔都等于期望 → 当前
行为已满足修复后的契约（PASS）；否则 FAIL。**FAIL 在未修复基线上是预期
结果**，它本身就是反例证据；Lead 在集成 worktree 重跑同一测试做前后对照。

反例只依据可观察行为（verdict、ok、字节、时间、进程存活、状态字段），
不引用任何「某 agent 会怎么修」的内部细节。
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

_HERE = Path(__file__).resolve().parent
_WORKTREE = _HERE.parent.parent
_SRC = _WORKTREE / "backend" / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

OUTPUT_DIR = _HERE / "output"

IS_WINDOWS = sys.platform.startswith("win")

# ---------------------------------------------------------------------------
# 基础设施：审批桩 / 沙箱 / 临时 git 仓库 / 进程存活探测
# ---------------------------------------------------------------------------


class DenyApprovals:
    """审批桩：每次请求都记录并直接拒绝（用于统计「是否走到审批」）。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def request(self, kind: str, payload: dict) -> Any:
        self.requests.append({"kind": kind, "action": (payload or {}).get("action")})

        class _R:
            decision = "rejected"

        return _R()


class ApproveApprovals:
    """审批桩：记录并直接批准（进程组用：让真实子进程走完完整执行路径）。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def request(self, kind: str, payload: dict) -> Any:
        self.requests.append({"kind": kind, "action": (payload or {}).get("action")})

        class _R:
            decision = "approved"

        return _R()


def make_sandbox(root: Path, mode: str = "default"):
    from agent.services.computer import ComputerSandbox

    root.mkdir(parents=True, exist_ok=True)
    return ComputerSandbox(resolve_root=lambda: str(root), permission_mode=lambda: mode)


def make_repo(base: Path) -> Path | None:
    """临时真实 git 仓库（含一次空提交，保证 tag/show 都能真实执行）。"""
    repo = base / "repo"
    repo.mkdir(parents=True, exist_ok=True)

    def git(*args: str):
        return subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    if git("init").returncode != 0:
        return None
    git(
        "-c", "user.email=verify@example.invalid",
        "-c", "user.name=SR-F-Verify",
        "commit", "--allow-empty", "-m", "init",
    )
    return repo


def git_tags(repo: Path) -> list[str]:
    r = subprocess.run(
        ["git", "tag", "--list"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return [t for t in (r.stdout or "").split() if t]


def pid_alive(pid: int) -> bool:
    """按 pid 探测进程是否仍存活（Windows 用 tasklist，POSIX 用 signal 0）。"""
    if not pid or pid <= 0:
        return False
    if IS_WINDOWS:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        out = r.stdout or ""
        if "INFO:" in out:
            return False
        return bool(re.search(rf"\b{pid}\b", out))
    try:
        import os
        import signal

        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_tree(pid: int) -> None:
    """收尾清理：只杀探针自己记录到的 pid（Windows 用 taskkill /T /F）。"""
    if not pid or pid <= 0:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                text=True,
            )
        else:
            import os
            import signal

            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001 - 清理失败不影响证据
        pass


def write_evidence(name: str, payload: dict) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _wait_for_file(path: Path, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def _read_pid(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------


async def _await_file(path: Path, timeout: float = 5.0) -> bool:
    """事件循环友好的等待：不让出循环的话，被测任务根本没机会跑。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        await asyncio.sleep(0.05)
    return path.exists()


async def _await_poll_gone(check, budget: float) -> bool:
    """异步轮询直到 check() 为 False（进程退出）或超时。"""
    start = time.perf_counter()
    while time.perf_counter() - start < budget:
        if not check():
            return True
        await asyncio.sleep(0.1)
    return not check()
# 权限组（1a/1b/1c）
# ---------------------------------------------------------------------------


async def probe_git_tag_write(mode: str = "default", label: str = "permission_git_tag") -> dict:
    """1a/1c：`git tag <名>` 是创建写操作，default/plan 模式都不得自动放行。

    反例（未修复基线）：`tag` 被整体列为只读子命令 → risk=LOW → auto →
    工具真实执行，repo 里真的多出一个 tag（零审批的写操作）。
    修复后期望：verdict 非 auto；拒绝之后 repo 零变化。
    """
    base = Path(tempfile.mkdtemp(prefix="sr_f_tag_"))
    try:
        repo = make_repo(base)
        if repo is None:
            return {"git_available": False}
        approvals = DenyApprovals()
        sandbox = make_sandbox(base / "root", mode)
        verdict, risk = sandbox.command_verdict_for_program("git", ["tag", "sr-f-tag"])
        from agent.tools.cmd_tools import RunProgramTool

        tool = RunProgramTool(computer=sandbox, approvals=approvals)
        result = await tool.run(program="git", args=["tag", "sr-f-tag"], cwd=str(repo))
        tags = git_tags(repo)
        ev = {
            "git_available": True,
            "mode": mode,
            "verdict_not_auto": str(verdict) != "auto",
            "verdict": str(verdict),
            "risk": str(getattr(risk, "value", risk)),
            "tool_ok": bool(result.ok),
            "tool_error": str(getattr(result, "error", "") or "")[:200],
            "tag_created": "sr-f-tag" in tags,
            "approval_requests": len(approvals.requests),
        }
        write_evidence(label, ev)
        return ev
    finally:
        shutil.rmtree(base, ignore_errors=True)


async def probe_env_read_bypass(label: str = "permission_env_bypass") -> dict:
    """1b：工作区外 .env —— FsReadTool 拒绝后，run_program `cat` 能否读到。

    反例（未修复基线）：`cat` 在只读白名单里，参数里的路径根本不参与判定 →
    verdict=auto；若系统里存在真实 cat，工作区外的 .env 内容会被直接读出
    （fs_read 拒绝的路径被命令旁路）。
    修复后期望：该路径 verdict 非 auto；内容不被读出；根内普通文件
    `cat` 的 auto 体验保持不变。
    """
    base = Path(tempfile.mkdtemp(prefix="sr_f_env_"))
    try:
        root = base / "root"
        root.mkdir()
        outside = base / "outside"
        outside.mkdir()
        env_file = outside / ".env"
        env_file.write_text("SR_F_ENV_CANARY=1", encoding="utf-8")

        approvals = DenyApprovals()
        sandbox = make_sandbox(root, "default")

        from agent.tools.fs_tools import FsReadTool

        fs = FsReadTool(computer=sandbox, approvals=approvals)
        r1 = await fs.run(path=str(env_file))

        verdict, risk = sandbox.command_verdict_for_program("cat", [str(env_file)])
        from agent.tools.cmd_tools import RunProgramTool

        run_tool = RunProgramTool(computer=sandbox, approvals=approvals)
        r2 = await run_tool.run(program="cat", args=[str(env_file)])
        content = getattr(r2, "content", "") or ""

        inside_file = root / "note.txt"
        inside_file.write_text("hello", encoding="utf-8")
        inside_verdict, _ = sandbox.command_verdict_for_program("cat", [str(inside_file)])

        ev = {
            "fs_read_denied": not r1.ok,
            "fs_read_error": str(getattr(r1, "error", "") or "")[:200],
            "run_verdict": str(verdict),
            "run_risk": str(getattr(risk, "value", risk)),
            "run_verdict_not_auto": str(verdict) != "auto",
            "run_executed": bool(r2.ok),
            "run_marker_leaked": bool(r2.ok) and ("SR_F_ENV_CANARY" in content),
            "cat_available": bool(shutil.which("cat")),
            "inside_file_auto_kept": str(inside_verdict) == "auto",
            "approval_requests": len(approvals.requests),
        }
        write_evidence(label, ev)
        return ev
    finally:
        shutil.rmtree(base, ignore_errors=True)


async def probe_plan_mode_shell_write_denied(label: str = "permission_plan_shell_write") -> dict:
    """1c 护栏：plan 模式下自由 shell 写命令必须 deny（既有正确行为，防回归）。"""
    base = Path(tempfile.mkdtemp(prefix="sr_f_plan_"))
    try:
        root = base / "root"
        sandbox = make_sandbox(root, "plan")
        verdict, risk = sandbox.shell_verdict("echo x > broken.txt")
        from agent.tools.cmd_tools import RunCmdTool

        tool = RunCmdTool(computer=sandbox, approvals=DenyApprovals())
        result = await tool.run(cmd="echo x > broken.txt")
        ev = {
            "verdict": str(verdict),
            "risk": str(getattr(risk, "value", risk)),
            "tool_ok": bool(result.ok),
        }
        write_evidence(label, ev)
        return ev
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# 进程组（2a/2b）
# ---------------------------------------------------------------------------

_SLEEVE_GRAND = """\
import sys, time
from pathlib import Path
Path(sys.argv[1]).write_text(str(__import__('os').getpid()), encoding='utf-8')
time.sleep(300)
"""

_SLEEVE_CHILD = """\
import os, subprocess, sys, time
from pathlib import Path
child_pid_file, gc_pid_file, grand_script = sys.argv[1], sys.argv[2], sys.argv[3]
Path(child_pid_file).write_text(str(os.getpid()), encoding='utf-8')
gc = subprocess.Popen([sys.executable, grand_script, gc_pid_file],
                      cwd=os.path.dirname(gc_pid_file) or '.')
time.sleep(300)
"""


def _make_sleeve_scripts(base: Path) -> tuple[Path, Path]:
    child = base / "sr_f_child_sleep.py"
    grand = base / "sr_f_grand_sleep.py"
    child.write_text(_SLEEVE_CHILD, encoding="utf-8")
    grand.write_text(_SLEEVE_GRAND, encoding="utf-8")
    return child, grand


async def probe_git_show_missing_ref(label: str = "process_git_show_missing_ref") -> dict:
    """2a：`git show 不存在的引用` 退出码非 0 —— 旧反例是 ok=True（stderr 被当成功）。

    修复后期望：ok=False 且 error 包含退出码语义；合并输出仍保留。
    """
    base = Path(tempfile.mkdtemp(prefix="sr_f_show_"))
    try:
        repo = make_repo(base)
        if repo is None:
            return {"git_available": False}
        sandbox = make_sandbox(base / "root", "default")
        from agent.tools.cmd_tools import RunProgramTool

        tool = RunProgramTool(computer=sandbox, approvals=DenyApprovals())
        result = await tool.run(
            program="git",
            args=["show", "sr-f-nonexistent-ref"],
            cwd=str(repo),
        )
        err = str(getattr(result, "error", "") or "")
        ev = {
            "git_available": True,
            "ok": bool(result.ok),
            "error": err[:200],
            "error_mentions_exit_code": "退出码" in err,
            "content_has_stderr_hint": "fatal" in (getattr(result, "content", "") or ""),
        }
        write_evidence(label, ev)
        return ev
    finally:
        shutil.rmtree(base, ignore_errors=True)


async def _run_sleeper_tree_probe(label: str, use_cancel: bool) -> dict:
    """2b 共用装置：受控 python 子进程（写 PID 文件 + sleep + 孙进程）+ 同名诱饵。"""
    base = Path(tempfile.mkdtemp(prefix="sr_f_proc_"))
    child_pid = gc_pid = decoy_pid = 0
    decoy_proc = None
    try:
        root = base / "root"
        root.mkdir()
        child_script, grand_script = _make_sleeve_scripts(base)
        child_pid_file = base / "child_pid.txt"
        gc_pid_file = base / "grand_pid.txt"
        decoy_pid_file = base / "decoy_pid.txt"

        # 诱饵：同一程序名（真实 python 可执行文件）的其它实例，必须毫发无损。
        decoy_proc = subprocess.Popen(
            [sys.executable, str(grand_script), str(decoy_pid_file)],
            cwd=str(base),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_for_file(decoy_pid_file)
        decoy_pid = _read_pid(decoy_pid_file)

        sandbox = make_sandbox(root, "default")
        from agent.tools.cmd_tools import RunProgramTool

        # run_program("python") 不在只读白名单 → HIGH → 审批；用批准桩放行，
        # 让真实子进程走完完整的执行/超时/取消路径（判定层在权限组覆盖）。
        tool = RunProgramTool(computer=sandbox, approvals=ApproveApprovals())
        kwargs: dict[str, Any] = {
            "program": str(sys.executable),
            "args": [str(child_script), str(child_pid_file), str(gc_pid_file), str(grand_script)],
            "cwd": str(base),
        }

        if use_cancel:
            task = asyncio.create_task(tool.run(**kwargs))
            started = await _await_file(child_pid_file)
            child_pid = _read_pid(child_pid_file)
            await _await_file(gc_pid_file, 3.0)
            gc_pid = _read_pid(gc_pid_file)
            task.cancel()
            cancelled_error = False
            try:
                await task
            except asyncio.CancelledError:
                cancelled_error = True
            child_gone = await _await_poll_gone(lambda: pid_alive(child_pid), 2.0)
            gc_gone = await _await_poll_gone(lambda: pid_alive(gc_pid), 2.0)
            ev = {
                "child_pid_started": bool(started and child_pid > 0),
                "cancelled_error": cancelled_error,
                "child_gone_after_cancel": child_gone,
                "grandchild_gone_after_cancel": gc_gone,
                "decoy_alive": pid_alive(decoy_pid),
            }
        else:
            kwargs["timeout"] = 2
            result = await tool.run(**kwargs)
            child_pid = _read_pid(child_pid_file)
            gc_pid = _read_pid(gc_pid_file)
            child_alive_immediately = pid_alive(child_pid)
            start = time.perf_counter()
            gone_poll = lambda: pid_alive(child_pid) or pid_alive(gc_pid)
            gone_within = await _await_poll_gone(gone_poll, 1.5)
            elapsed = round(time.perf_counter() - start, 3)
            # 整树宽窗口（5s）：taskkill 的实际耗时受并行负载影响。
            started_wide = time.perf_counter()
            gone_wide = await _await_poll_gone(gone_poll, 5.0 - elapsed)
            ev = {
                "tool_ok": bool(result.ok),
                "tool_failed": not result.ok,
                "tool_error": str(getattr(result, "error", "") or "")[:200],
                "child_pid_started": child_pid > 0,
                "child_alive_immediately": child_alive_immediately,
                "child_gone_within_1s": bool(gone_within and elapsed <= 1.0),
                "child_gone_within_5s": bool(gone_wide),
                "probe_wait_seconds_wide": 5.0,
                "grandchild_gone": not pid_alive(gc_pid),
                "decoy_alive": pid_alive(decoy_pid),
                "probe_wait_seconds": elapsed,
            }
        write_evidence(label, ev)
        return ev
    finally:
        for pid in (child_pid, gc_pid, decoy_pid):
            kill_tree(pid)
        if decoy_proc is not None:
            try:
                decoy_proc.kill()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(base, ignore_errors=True)


async def probe_timeout_tree(label: str = "process_timeout_tree") -> dict:
    """2b：工具超时返回后，本次启动的进程树必须已退出；同名诱饵必须存活。

    反例（未修复基线）：`asyncio.wait_for` 超时只放弃等待，不终止进程 ——
    返回后子进程与孙进程继续存活（sleep 300）。
    """
    return await _run_sleeper_tree_probe(label, use_cancel=False)


async def probe_cancel_tree(label: str = "process_cancel_tree") -> dict:
    """2b（取消）：执行协程被 cancel → 进程树退出 + CancelledError 重新抛出。

    反例（未修复基线）：取消时只放弃等待，进程树存活。
    """
    return await _run_sleeper_tree_probe(label, use_cancel=True)


# ---------------------------------------------------------------------------
# 打码组（3）
# ---------------------------------------------------------------------------


def _fake_secret_value() -> str:
    """无特殊前缀、不命中任何形状正则的模拟密钥（hex + 前缀 sec_）。

    必须真的不命中 redact.py 的兜底正则：这样「事件/重放是否打码」才只能
    依赖登记表（这是契约 3 的主防线），反例才站得住。
    """
    return "sec_" + uuid.uuid4().hex + uuid.uuid4().hex[:8]


async def probe_redaction_bundle(label: str = "redaction_bus_sse") -> dict:
    """3：注册假密钥 → 假 provider 异常 → 直接经 bus + sse_format 抓字节。

    反例（未修复基线）：bus.publish 不打码 event.data —— ERROR 事件原文
    （含异常文本里的密钥、结构化 api_key 字段）原样出现在 SSE 字节与
    重连重放里。日志路径（LogRecord 工厂）基线已覆盖。

    证据只含布尔：原始值绝不写入证据文件、也绝不出现在断言消息里。
    """
    from agent.api.bus import EventBus
    from agent.api.events import EventType, make_event
    from agent.trace import redact
    from agent.trace.redact import REDACTED

    value = _fake_secret_value()
    redact.clear_registered_secrets()
    ev: dict[str, Any] = {}
    try:
        registered = redact.register_secret(value)
        redacted_version = redact.redact_text(f"x {value} y")
        ev["registry_works"] = bool(registered) and (value not in redacted_version)

        class FakeProviderError(RuntimeError):
            pass

        exc = FakeProviderError(f"provider call failed: {value}")
        bus = EventBus(replay_limit=50)
        events = [
            make_event(EventType.ERROR, {"turn_id": "t-f1", "error": str(exc), "attempt": 1}),
            make_event(EventType.ERROR, {"turn_id": "t-f1", "api_key": value, "attempt": 2}),
        ]

        async def _publish_two() -> None:
            await bus.publish(events[0])
            await bus.publish(events[1])

        blob = ""
        blob_replay = ""

        async def _main() -> None:
            nonlocal blob, blob_replay
            agen = bus.stream(None)
            pub = asyncio.create_task(_publish_two())
            first = await agen.__anext__()
            second = await agen.__anext__()
            await pub
            await agen.aclose()
            blob = first + second
            # 重连重放：带上第一条事件的 id，第二条事件必须按同一路径补发。
            agen2 = bus.stream(events[0].id)
            replay_first = await agen2.__anext__()
            await agen2.aclose()
            blob_replay = replay_first

        await _main()

        ev["sse_raw_leaked"] = value in blob
        ev["sse_has_marker"] = REDACTED in blob
        ev["api_key_raw_leaked"] = value in blob
        ev["replay_raw_leaked"] = value in blob_replay
        ev["replay_has_marker"] = REDACTED in blob_replay
        ev["event_ids_present"] = ("t-f1" in blob) and ("attempt" in blob)

        # 日志路径同语义（独立 handler）：日志文本也不得含原文。
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        lg = logging.getLogger("agent.sr_f_verify")
        lg.addHandler(handler)
        lg.setLevel(logging.INFO)
        try:
            lg.error("provider call failed: %s", value)
        finally:
            lg.removeHandler(handler)
        ev["caplog_raw_leaked"] = value in buf.getvalue()
        return ev
    finally:
        redact.clear_registered_secrets()
        write_evidence(label, {k: v for k, v in ev.items()})


# ---------------------------------------------------------------------------
# 响应性组（5）
# ---------------------------------------------------------------------------


async def _probe_responsiveness_gate(label: str = "responsiveness_event_loop_gate") -> dict:
    """5：embed/文件扫描被闸门方式暂停时，同期挂起的事件循环工作被推迟？

    反例（未修复基线）：`route()` 在事件循环上同步调用 `embed_texts`（阻塞
    等待闸门），`fs_find` 的目录扫描也在循环上 —— 心跳（健康检查模拟）在
    整个闸门期间一次都推进不了，且耗时调用发生在主线程。
    修复后期望：route_async / fs_find 把耗时计算移出事件循环 —— 心跳照常
    推进，线程 id 不是主线程。

    证据：线程 id + 相对时间戳。
    """
    from agent.adapters.base import ToolSpec
    import agent.tools.fs_tools as fs_mod
    from agent.tools.fs_tools import FsFindTool
    from agent.services.tool_router import ToolRouter

    main_tid = threading.get_ident()
    records: list[dict] = []
    gate_open = threading.Event()
    gate_open.set()

    def gated_embed(texts: list[str]) -> list[list[float]]:
        records.append({"op": "embed_texts", "thread_id": threading.get_ident(), "t": round(time.perf_counter(), 4)})
        gate_open.wait(2.0)
        records.append({"op": "embed_texts_done", "thread_id": threading.get_ident(), "t": round(time.perf_counter(), 4)})
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    class FakeEmbedding:
        name = "fake-emb"
        model_name = "fake"
        dims = 4

        def available(self) -> bool:
            return True

        def embed_texts(self, texts):
            return gated_embed(texts)

    async def _heartbeat_run(progress: dict) -> None:
        while True:
            await asyncio.sleep(0.02)
            progress["beats"] += 1
            progress["timestamps"].append(round(time.perf_counter(), 4))

    async def _measure(awaitable) -> dict:
        """闸门期间的事件循环活跃度度量。

        心跳时间戳必须**严格落在闸门窗口内**才算推进 —— 边界上解除阻塞
        之后的第一次心跳不算（asyncio 本来就会在 awaitable 完成后立刻调度
        一次心跳，那是边界效应，不是「闸门期间没被推迟」的证据）。
        """
        progress: dict[str, Any] = {"beats": 0, "timestamps": []}
        hb = asyncio.create_task(_heartbeat_run(progress))
        await asyncio.sleep(0.05)
        if progress["beats"] <= 0:
            raise RuntimeError("心跳未启动（环境问题）")
        gate_open.clear()
        await asyncio.sleep(0.03)
        t0 = time.perf_counter()
        try:
            await awaitable
        finally:
            gate_open.set()
        t1 = time.perf_counter()
        beats_in_window = [ts for ts in progress["timestamps"] if t0 <= ts <= t1]
        hb.cancel()
        try:
            await hb
        except asyncio.CancelledError:
            pass
        return {
            "beats_in_window": len(beats_in_window),
            "window_secs": round(t1 - t0, 3),
            "beats_total": progress["beats"],
        }

    ev: dict[str, Any] = {
        "route_async_exists": hasattr(ToolRouter, "route_async"),
        "main_thread_id": main_tid,
    }

    router = ToolRouter(embedding=FakeEmbedding())
    specs = [ToolSpec(name=f"tool_{i}", description=f"desc {i}", parameters={}) for i in range(3)]

    # 基线取证（信息项，不进期望表：契约要求保留同步 route()，
    # 接线由 Lead 换成 route_async）。
    # 注意绝不能走 _measure：awaitable 前后的任何 yield 都会打开边界窗口，
    # 让取消阻塞后心跳的「解渴回补」一步冒充窗口内推进。这里用**零 yield**
    # 的纯同步调用夹住窗口：窗口内其它协程根本没机会执行，判定是确定性的。
    progress0: dict[str, Any] = {"beats": 0, "timestamps": []}
    hb0 = asyncio.create_task(_heartbeat_run(progress0))
    await asyncio.sleep(0.05)
    if progress0["beats"] <= 0:
        raise RuntimeError("心跳未启动（环境问题）")
    gate_open.clear()
    t0 = time.perf_counter()
    router.route("查询", specs)
    t1 = time.perf_counter()
    gate_open.set()
    beats_in_window0 = [ts for ts in progress0["timestamps"] if t0 <= ts <= t1]
    hb0.cancel()
    try:
        await hb0
    except asyncio.CancelledError:
        pass
    ev["sync_route_blocks_loop"] = len(beats_in_window0) == 0
    ev["sync_route_beats_in_window"] = len(beats_in_window0)
    ev["sync_route_window_secs"] = round(t1 - t0, 3)
    n0 = len(records)  # 此后的记录才属于 route_async 段（同步基线在主线程产生过 embed 记录）

    # 期望主体：异步路由不得阻塞事件循环。
    if ev["route_async_exists"]:
        m = await _measure(
            router.route_async("查询", specs, pending_tasks=False, web_allowed=True)
        )
        ev["route_async_blocks_loop"] = m["beats_in_window"] == 0
        ev["route_async_beats_in_window"] = m["beats_in_window"]
        ev["route_async_window_secs"] = m["window_secs"]
        async_records = records[n0:]
        ev["embed_thread_id"] = next(
            (r["thread_id"] for r in async_records if r["op"] == "embed_texts"), 0
        )
    ev["embed_off_main_thread"] = bool(
        ev.get("embed_thread_id") and ev["embed_thread_id"] != main_tid
    )

    # fs_find：目录扫描闸门。
    base = Path(tempfile.mkdtemp(prefix="sr_f_resp_"))
    original_find = fs_mod._find_files
    try:
        root = base / "root"
        root.mkdir()
        for i in range(150):
            (root / f"f{i}.txt").write_text("x", encoding="utf-8")

        def gated_find(root_dir, query, *, limit):
            records.append({"op": "_find_files", "thread_id": threading.get_ident(), "t": round(time.perf_counter(), 4)})
            gate_open.wait(2.0)
            records.append({"op": "_find_files_done", "thread_id": threading.get_ident(), "t": round(time.perf_counter(), 4)})
            return original_find(root_dir, query, limit=limit)

        fs_mod._find_files = gated_find
        tool = FsFindTool(computer=make_sandbox(root, "default"), approvals=DenyApprovals())
        m = await _measure(tool.run(query="f0", dir=str(root)))
        ev["fs_find_blocks_loop"] = m["beats_in_window"] == 0
        ev["fs_find_beats_in_window"] = m["beats_in_window"]
        ev["fs_find_window_secs"] = m["window_secs"]
        ev["fs_find_thread_id"] = next(
            (r["thread_id"] for r in records if r["op"] == "_find_files"), 0
        )
        ev["fs_find_off_main_thread"] = bool(
            ev.get("fs_find_thread_id") and ev["fs_find_thread_id"] != main_tid
        )
    finally:
        fs_mod._find_files = original_find
        shutil.rmtree(base, ignore_errors=True)

    ev["records"] = records
    write_evidence(label, ev)
    return ev


# ---------------------------------------------------------------------------
# 期望表与判定（契约 6：明确 PASS/FAIL + 中文结论）
# ---------------------------------------------------------------------------

EXPECTATIONS: dict[str, dict[str, bool]] = {
    "permission_git_tag_default": {
        "git_available": True,
        "verdict_not_auto": True,
        "tag_created": False,
    },
    "permission_git_tag_plan": {
        "git_available": True,
        "verdict_not_auto": True,
        "tag_created": False,
    },
    "permission_env_bypass": {
        "fs_read_denied": True,
        "run_verdict_not_auto": True,
        "run_marker_leaked": False,
        "inside_file_auto_kept": True,
    },
    "permission_plan_shell_write": {
        "tool_ok": False,
    },
    "process_git_show_missing_ref": {
        "git_available": True,
        "ok": False,
        "error_mentions_exit_code": True,
    },
    "process_timeout_tree": {
        "tool_failed": True,
        "child_pid_started": True,
        "child_alive_immediately": False,
        "child_gone_within_1s": True,
        "grandchild_gone": True,
        "decoy_alive": True,
    },
    "process_cancel_tree": {
        "child_pid_started": True,
        "cancelled_error": True,
        "child_gone_after_cancel": True,
        "grandchild_gone_after_cancel": True,
        "decoy_alive": True,
    },
    "redaction_bus_sse": {
        "registry_works": True,
        "sse_raw_leaked": False,
        "sse_has_marker": True,
        "api_key_raw_leaked": False,
        "replay_raw_leaked": False,
        "replay_has_marker": True,
        "event_ids_present": True,
        "caplog_raw_leaked": False,
    },
    "responsiveness_event_loop_gate": {
        "route_async_exists": True,
        "route_async_blocks_loop": False,
        "embed_off_main_thread": True,
        "fs_find_blocks_loop": False,
        "fs_find_off_main_thread": True,
    },
}


def judge(label: str, evidence: dict) -> tuple[str, str]:
    """返回 (PASS|FAIL|SKIP, 中文结论)。git_available=False 视为环境跳过。"""
    if evidence.get("git_available") is False:
        return "SKIP", "本机没有可用的 git，无法做真实反例（环境问题，不计入失败）"
    expected = EXPECTATIONS.get(label, {})
    problems = []
    for key, want in expected.items():
        if key not in evidence:
            problems.append(f"{key} 缺失（本次未测到）")
            continue
        actual = evidence[key]
        if actual is None or bool(actual) != bool(want):
            problems.append(f"{key} 实际={actual!r} 期望={bool(want)}")
    if problems:
        return "FAIL", "；".join(problems)
    return "PASS", "当前行为已满足修复后的期望"


ALL_PROBES: dict[str, Callable[[], Any]] = {
    "permission_git_tag_default": lambda: probe_git_tag_write(mode="default", label="permission_git_tag_default"),
    "permission_git_tag_plan": lambda: probe_git_tag_write(mode="plan", label="permission_git_tag_plan"),
    "permission_env_bypass": lambda: probe_env_read_bypass(label="permission_env_bypass"),
    "permission_plan_shell_write": lambda: probe_plan_mode_shell_write_denied(label="permission_plan_shell_write"),
    "process_git_show_missing_ref": lambda: probe_git_show_missing_ref(label="process_git_show_missing_ref"),
    "process_timeout_tree": lambda: probe_timeout_tree(label="process_timeout_tree"),
    "process_cancel_tree": lambda: probe_cancel_tree(label="process_cancel_tree"),
    "redaction_bus_sse": lambda: probe_redaction_bundle(label="redaction_bus_sse"),
    "responsiveness_event_loop_gate": lambda: _probe_responsiveness_gate(label="responsiveness_event_loop_gate"),
}