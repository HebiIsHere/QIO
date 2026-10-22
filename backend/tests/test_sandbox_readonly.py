"""只读/受限模式的执行层约束（契约 §2.3/§3.1/§3.2）。

覆盖：
* read_only 下 SandboxExecutor 拒绝任意脚本（不启动任何进程）；
* read_only 下 run_program 只放行可证明只读的 argv（白名单 + shell=False）；
* read_only 下 run_shell 明确拒绝；
* restricted 下无写范围授权的脚本强制 Low IL；核实不了就不执行（暂不可用）；
* 内部运行数据在文件边界进入三次确认（提交待审批，不执行）；
* Windows 真机 Low IL 探针：低完整性子孙进程写 Medium 文件被拒（PermissionError）。
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from agent.services.computer import ComputerSandbox
from agent.tools import isolation, security_gate
from agent.tools.cmd_tools import RunCmdTool, RunProgramTool
from agent.tools.fs_tools import FsWriteTool
from agent.tools.sandbox import SandboxExecutor
from agent.tools.security_types import AuthDecision, Verdict


class FakeGateway:
    """轻量 fake gateway：记录 effect，返回预设 decision（不复制任何权限判定）。"""

    def __init__(self, mode="restricted", verdicts=None, internal_paths=None) -> None:
        self._mode = mode
        self.verdicts = dict(verdicts or {})
        self.calls: list = []
        self.submitted: list = []
        self.internal_data_paths = list(internal_paths or [])
        self.child_scopes: list = []

    def mode(self):
        return self._mode

    async def authorize(self, effect, *, ctx=None):
        self.calls.append(effect)
        kind = str(getattr(getattr(effect, "kind", None), "value", effect.kind))
        verdict = self.verdicts.get(kind, Verdict.ALLOW)
        return AuthDecision(verdict=verdict, reason=f"fake:{kind}")

    async def submit(self, operation, *, ctx=None):
        self.submitted.append(operation)
        return types.SimpleNamespace(
            approval_id="appr_fake",
            state="waiting",
            expires_at="2099-01-01T00:00:00+00:00",
            confirmations_required=operation.confirmations_required,
        )

    def instruction(self):
        return None


class _FakeProcess:
    def __init__(self) -> None:
        self.pid = 424242
        self.returncode = None
        self.killed = False
        self.stdin = types.SimpleNamespace(write=lambda data: None, drain=_noop, close=lambda: None, wait_closed=_noop)
        self.stdout = types.SimpleNamespace(read=_eof)
        self.stderr = types.SimpleNamespace(read=_eof)

    async def wait(self):
        return 0

    def kill(self):
        self.killed = True


async def _noop(*args, **kwargs):
    return None


async def _eof(*args, **kwargs):
    return b""


def _sandbox(root, mode="default") -> ComputerSandbox:
    return ComputerSandbox(resolve_root=lambda: str(root), permission_mode=lambda: mode)


# --------------------------------------------------------------------------
# read_only：任意脚本一律拒绝（执行层兜底，不依赖引擎是否先拒绝）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_only_sandbox_refuses_arbitrary_script_without_spawning(
    tmp_path, monkeypatch
):
    gateway = FakeGateway(mode="read_only", verdicts={"execute": Verdict.DENY})
    executor = SandboxExecutor(mode_provider=lambda: "read_only", security=gateway)
    spawned: list = []

    async def _never(*args, **kwargs):
        spawned.append((args, kwargs))
        raise AssertionError("只读模式不得启动任何脚本进程")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _never)
    result = await executor.execute("print('should not run')", {})

    assert result.ok is False
    assert result.category == "permission"
    assert "只读模式" in (result.error or "")
    assert "没有任何代码被执行" in (result.error or "")
    assert spawned == [], "只读模式拒绝必须发生在启动进程之前"


@pytest.mark.asyncio
async def test_restricted_script_without_write_scope_requires_low_integrity(
    tmp_path, monkeypatch
):
    gateway = FakeGateway(mode="restricted")
    executor = SandboxExecutor(mode_provider=lambda: "restricted", security=gateway)
    captured: dict = {}

    def _fake_harden(process, **kwargs):
        captured.update(kwargs)
        return isolation.IsolationOutcome(
            applied=True,
            mechanisms=("job_object",),
            low_integrity_required=True,
        )

    async def _fake_create(*args, **kwargs):
        return _FakeProcess()

    async def _fake_kill(process):
        process.killed = True

    monkeypatch.setattr(isolation, "harden", _fake_harden)
    monkeypatch.setattr(isolation, "release", lambda process: None)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    monkeypatch.setattr("agent.tools.sandbox._kill_process_tree", _fake_kill)

    result = await executor.execute("print(1)", {})

    assert captured.get("mode") == "restricted"
    assert captured.get("authorized_write_scope") is False
    assert result.ok is False, "低完整性边界核实不了时不得执行脚本"
    assert "暂不可用" in (result.error or "")
    assert result.isolation is not None and result.isolation["low_integrity_required"] is True


def test_mode_aware_low_integrity_requirements():
    assert isolation.low_integrity_required_for_mode("read_only") is True
    assert isolation.low_integrity_required_for_mode("restricted") is True
    assert (
        isolation.low_integrity_required_for_mode(
            "restricted", authorized_write_scope=True
        )
        is False
    )
    assert isolation.low_integrity_required_for_mode("unrestricted") is False
    assert isolation.low_integrity_required_for_mode(None) is False
    # 旧模式值先 normalize：plan → read_only，default/accept-edits → restricted
    assert isolation.low_integrity_required_for_mode("plan") is True
    assert isolation.low_integrity_required_for_mode("default") is True


# --------------------------------------------------------------------------
# read_only：run_program 白名单 / run_shell 拒绝
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_only_run_program_allows_provably_readonly_argv(tmp_path):
    gateway = FakeGateway(mode="read_only", verdicts={"execute": Verdict.ALLOW})
    tool = RunProgramTool(computer=_sandbox(tmp_path, "plan"), security=gateway)

    result = await tool.run(program="git", args=["--version"])

    assert result.ok is True, result.error
    assert "git" in (result.content or "").lower()
    assert len(gateway.calls) == 1, "允许执行前仍然要经过统一授权入口"


@pytest.mark.asyncio
async def test_read_only_run_program_rejects_unprovable_write_command(tmp_path):
    gateway = FakeGateway(mode="read_only", verdicts={"execute": Verdict.ALLOW})
    tool = RunProgramTool(computer=_sandbox(tmp_path, "plan"), security=gateway)

    result = await tool.run(program="git", args=["commit", "-m", "x"])

    assert result.ok is False
    assert "只读模式" in (result.error or "")
    assert gateway.calls == [], "执行层无法证明只读时，不应把可写命令交给引擎放行"


@pytest.mark.asyncio
async def test_read_only_run_shell_is_refused(tmp_path):
    gateway = FakeGateway(mode="read_only")
    approvals = types.SimpleNamespace(calls=[])

    async def _request(kind, payload):
        approvals.calls.append(payload)
        return types.SimpleNamespace(decision="approved")

    approvals.request = _request
    tool = RunCmdTool(computer=_sandbox(tmp_path, "plan"), approvals=approvals, security=gateway)

    result = await tool.run(cmd="ls")

    assert result.ok is False
    assert "只读模式" in (result.error or "")
    assert approvals.calls == []
    assert gateway.calls == []


# --------------------------------------------------------------------------
# 内部运行数据：进入三次确认（提交待审批，不直接执行）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_internal_data_write_requires_three_confirmations(tmp_path):
    data_dir = tmp_path / "qio-data"
    data_dir.mkdir()
    target = data_dir / "qio.db"
    gateway = FakeGateway(
        mode="restricted",
        verdicts={"internal_data": Verdict.APPROVE},
        internal_paths=[data_dir],
    )
    tool = FsWriteTool(computer=_sandbox(tmp_path), security=gateway)

    result = await tool.run(path=str(target), content="hacked")

    assert result.ok is True and result.code == "pending_approval"
    assert result.data["confirmations_required"] == 3
    assert result.data["pending_approval"] is True
    assert "连续确认三次" in (result.content or "")
    assert len(gateway.submitted) == 1
    operation = gateway.submitted[0]
    assert operation.kind == "fs.write"
    assert operation.confirmations_required == 3
    assert not target.exists(), "待审批操作不得先执行"


# --------------------------------------------------------------------------
# Windows 真机 Low IL 探针（契约 §3.2：承诺必须用真实内核行为取证）
# --------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Low IL 是 Windows 内核能力")
@pytest.mark.asyncio
async def test_real_low_integrity_child_cannot_write_medium_file(tmp_path):
    """真实进程：低完整性子进程写本进程创建的 Medium 文件 → PermissionError。"""
    protected = tmp_path / "protected-medium.txt"
    protected.write_text("original", encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    # 子进程先阻塞读 stdin：父进程完成降级后再放行，避免「还没来得及降级就写完了」。
    script = (
        "import sys\n"
        "sys.stdin.readline()\n"
        "try:\n"
        "    open(sys.argv[1], 'w', encoding='utf-8').write('hacked')\n"
        "except PermissionError as exc:\n"
        "    print('WRITE-DENIED', type(exc).__name__, file=sys.stderr)\n"
        "    raise SystemExit(3)\n"
        "print('WRITE-OK')\n"
    )
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(protected),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        outcome = isolation.harden(child, scratch_dir=str(scratch), mode="read_only")
        assert outcome.boundary_ok(), f"本机无法核实 Low IL 生效：{outcome.as_dict()}"
        child.stdin.write(b"go\n")  # type: ignore[union-attr]
        await child.stdin.drain()  # type: ignore[union-attr]
        stdout, stderr = await asyncio.wait_for(child.communicate(), timeout=60)
    finally:
        with __import__("contextlib").suppress(Exception):
            if child.returncode is None:
                child.kill()
                await child.wait()
        isolation.release(child)

    assert child.returncode == 3, (child.returncode, stdout, stderr)
    assert b"WRITE-DENIED" in stderr and b"PermissionError" in stderr
    assert protected.read_text(encoding="utf-8") == "original", "低完整性进程不得写 Medium 文件"
