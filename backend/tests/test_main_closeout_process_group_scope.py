# -*- coding: utf-8 -*-
"""CI 收尾验收（2026-10-11）：进程树清理不许误伤调用方自己的进程组。

事故（从安全/响应性线 `0baaa9b` 起，`7ff5e2e` 上仍在）

    「真实退出码 + 进程树清理」上线后，Linux backend 任务**连续整份失联**：
    GitHub 注解是 `The hosted runner lost communication with the server`，
    任务日志根本没有上传（runner agent 被杀），同一份代码的 Windows backend 一直绿。

    根因：`tools/sandbox.py` 的 **docker 路径**启动 `docker run` 客户端时没有
    `start_new_session`，而清理走 `os.killpg(os.getpgid(pid), SIGKILL)`。
    对「与调用方同组」的子进程，`os.getpgid(pid)` 返回的正是**调用方自己的组 id**：
    SIGKILL 于是打到整个进程组上 —— 在 CI 里就是 runner agent 所在的组。
    受限子进程路径一直带着 `_spawn_kwargs()`，所以只有容器路径中招；
    Windows 用 taskkill /T 按 pid 遍历，所以只有 Linux 中招。

本文件钉住两件事

    1. docker 客户端与受限子进程一样**自立进程组**（POSIX；Windows 上等于不进这个参数）；
    2. `_kill_process_tree` 只在子进程**确实是组长**时才按组收割，否则只杀那一个 pid
       —— 这条判定是纯逻辑，跨平台可验。

运行：cd backend; uv run --frozen pytest tests/test_main_closeout_process_group_scope.py -q
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from agent.tools import sandbox as sandbox_mod
from agent.tools.sandbox import SandboxExecutor

TOOL = "def run(**kwargs):\n    return {}\n"


# ---------------------------------------------------------------------------
# 1. 判定函数：只有「自己就是组长」才允许按组收割
# ---------------------------------------------------------------------------


def test_group_target_is_none_when_child_shares_our_group(monkeypatch):
    """老 docker 路径的形状（与调用方同组）：绝不允许返回组 id。"""
    monkeypatch.setattr(sandbox_mod.os, "getpgrp", lambda: 4242, raising=False)
    monkeypatch.setattr(sandbox_mod.os, "getpgid", lambda pid: 4242, raising=False)
    assert sandbox_mod._own_process_group(9999) is None


def test_group_target_is_the_children_own_group(monkeypatch):
    """`start_new_session` 的形状（自己就是组长）：返回它自己的组 id。"""
    monkeypatch.setattr(sandbox_mod.os, "getpgrp", lambda: 4242, raising=False)
    monkeypatch.setattr(sandbox_mod.os, "getpgid", lambda pid: pid, raising=False)
    assert sandbox_mod._own_process_group(9999) == 9999


def test_group_target_is_none_when_the_process_is_gone(monkeypatch):
    def _boom(pid):  # noqa: ANN001
        raise ProcessLookupError(pid)

    monkeypatch.setattr(sandbox_mod.os, "getpgid", _boom, raising=False)
    assert sandbox_mod._own_process_group(9999) is None


# ---------------------------------------------------------------------------
# 1b. 同一纪律：scripts/sr-verify/probes.py 的探针收尾（CI 上真正打死 runner 的那处）
# ---------------------------------------------------------------------------


def _load_sr_probes():
    """加载 scripts/sr-verify/probes.py（它不是包，测试里按路径导入）。"""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "sr-verify" / "probes.py"
    assert path.exists(), f"验证资产缺失：{path}"
    spec = importlib.util.spec_from_file_location("sr_verify_probes_closeout", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _record_kills(monkeypatch) -> list[tuple]:
    """把 os.killpg / os.kill 换成记录器（绝不真的发信号）。"""
    recorded: list[tuple] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: recorded.append(("killpg", pgid)), raising=False)
    monkeypatch.setattr(os, "kill", lambda pid, sig: recorded.append(("kill", pid)), raising=False)
    return recorded


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 走 taskkill /T 按 pid 遍历")
def test_probe_kill_tree_never_targets_the_callers_own_group(monkeypatch):
    """探针收尾只杀自己的 pid：目标与调用方同组时绝不允许 killpg。

    这就是 2026-10-11 Linux CI 整份失联的直接原因 —— `probes.kill_tree(decoy_pid)`
    对同组的诱饵执行 `killpg(os.getpgid(pid))`，把 pytest 和 runner agent 一起杀掉。
    """
    probes = _load_sr_probes()
    recorded = _record_kills(monkeypatch)
    monkeypatch.setattr(os, "getpgrp", lambda: 777, raising=False)
    monkeypatch.setattr(os, "getpgid", lambda pid: 777, raising=False)  # 与调用方同组

    probes.kill_tree(4321)

    assert recorded == [("kill", 4321)], ("对同组 pid 用了 killpg（会打死调用方自己）", recorded)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 走 taskkill /T 按 pid 遍历")
def test_probe_kill_tree_still_uses_group_kill_for_a_group_leader(monkeypatch):
    """正对照：目标自己就是组长时照样按组收割（清理语义没有被削弱）。"""
    probes = _load_sr_probes()
    recorded = _record_kills(monkeypatch)
    monkeypatch.setattr(os, "getpgrp", lambda: 777, raising=False)
    monkeypatch.setattr(os, "getpgid", lambda pid: pid, raising=False)  # 自己是组长

    probes.kill_tree(4321)

    assert recorded == [("killpg", 4321)], recorded


# ---------------------------------------------------------------------------
# 2. docker 客户端必须自立进程组（重演事故的装置：抓启动参数）
# ---------------------------------------------------------------------------


async def test_docker_client_spawn_uses_its_own_session(monkeypatch):
    seen: list[dict] = []

    async def _fake_exec(*argv, **kwargs):
        seen.append(kwargs)
        raise OSError("docker 命令行起不来（本用例只关心启动参数）")

    monkeypatch.setattr(sandbox_mod.asyncio, "create_subprocess_exec", _fake_exec)
    executor = SandboxExecutor(executor="subprocess")
    result = await executor._execute_docker(TOOL, {})

    assert result.launch_failed is True, result
    assert seen, "docker 客户端必须真的被尝试启动（否则这条用例没有证明力）"
    if sys.platform == "win32":
        # Windows 不支持 start_new_session，清理走 taskkill /T（按 pid 遍历）
        assert "start_new_session" not in seen[0], seen[0]
    else:
        assert seen[0].get("start_new_session") is True, (
            "docker 客户端必须自立进程组：否则 _kill_process_tree 的按组收割会打到"
            "调用方自己的组（CI 上就是 runner agent）",
            seen[0],
        )


# ---------------------------------------------------------------------------
# 3. 真子进程（复现事故形状）：清理时绝不向调用方自己的组发信号
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 进程组语义；Windows 走 taskkill")
async def test_cleanup_never_signals_the_callers_own_group(monkeypatch):
    recorded: list[int] = []

    def _recording_killpg(pgid, sig):  # noqa: ANN001
        recorded.append(pgid)  # 只记录，绝不真的发信号（否则会杀掉测试进程自己）

    monkeypatch.setattr(sandbox_mod.os, "killpg", _recording_killpg)

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)"
    )
    try:
        assert os.getpgid(proc.pid) == os.getpgrp(), "场景前提：子进程与调用方同组"
        await asyncio.wait_for(sandbox_mod._kill_process_tree(proc), timeout=10)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    assert recorded == [], (
        "清理打到了调用方自己的进程组（真实事故里就是 runner agent 被杀）", recorded
    )
    assert proc.returncode is not None, "这次调用的子进程必须真的被结束"


def test_is_our_child_rejects_a_pid_that_is_not_ours():
    """存在的陌生 pid（init / 自己）不是我们的孩子；没有 /proc 时保持既有行为。"""
    if sys.platform.startswith("linux"):
        assert sandbox_mod._is_our_child(os.getpid()) is False
        assert sandbox_mod._is_our_child(1) is False
    else:
        # 没有 /proc：按改动前的行为返回 True（不新增不确定性）—— 这条边界是诚实的，
        # 该兜底只在 Linux 上真正生效，所以它也是 Linux CI 要跑这条用例的原因。
        assert sandbox_mod._is_our_child(os.getpid()) is True
        assert sandbox_mod._is_our_child(1) is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 子进程语义")
async def test_is_our_child_accepts_a_real_child():
    from agent.tools.sandbox import _spawn_kwargs

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(5)", **_spawn_kwargs()
    )
    try:
        assert sandbox_mod._is_our_child(proc.pid) is True
    finally:
        proc.kill()
        await proc.wait()


async def test_cleanup_signals_nothing_for_a_pid_that_is_not_our_child(monkeypatch):
    """接线：既不是组长、又不是自己的孩子（测试桩的假 pid 形状）→ 一个信号都不发。"""
    signaled: list[tuple] = []
    monkeypatch.setattr(
        sandbox_mod.os, "kill", lambda pid, sig: signaled.append(("kill", pid)), raising=False
    )
    monkeypatch.setattr(
        sandbox_mod.os, "killpg", lambda pgid, sig: signaled.append(("killpg", pgid)), raising=False
    )
    monkeypatch.setattr(sandbox_mod.os, "getpgid", lambda pid: 999999, raising=False)
    monkeypatch.setattr(sandbox_mod, "_is_our_child", lambda pid: False)

    class _FakeProc:
        pid = 4321

        async def wait(self) -> int:
            return 0

    await sandbox_mod._kill_process_tree(_FakeProc())
    assert signaled == [], ("对不是自己孩子的 pid 发了信号", signaled)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 进程组语义")
async def test_cleanup_does_group_kill_when_the_child_owns_its_group():
    """正对照：`start_new_session` 的子进程照样按组收割（语义没有被削弱）。"""
    from agent.tools.sandbox import _spawn_kwargs

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)", **_spawn_kwargs()
    )
    try:
        assert os.getpgid(proc.pid) == proc.pid, "场景前提：子进程是它自己进程组的组长"
        await asyncio.wait_for(sandbox_mod._kill_process_tree(proc), timeout=10)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    assert proc.returncode is not None
