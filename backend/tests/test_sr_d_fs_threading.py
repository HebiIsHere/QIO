"""契约 4（D）：fs_* 工具的实际文件 I/O 移出事件循环。

验收点（契约 4）：
- fs_read/write/patch/list/find/info 的实际 I/O（open/read/write/replace/stat/
  iterdir/scandir/_find_files）经 run_in_executor 在**非事件循环线程**执行；
- resolve → 权限判定 → 审批的顺序与语义留在事件循环：审批回调线程 == 事件循环线程
  （!= executor 线程）；
- 输出错文案、limit、错误类别语义不变。
"""

from __future__ import annotations

import builtins
import threading
import time
from pathlib import Path

import pytest

from agent.tools import fs_tools
from agent.tools.fs_tools import (
    FsFindTool,
    FsInfoTool,
    FsListTool,
    FsPatchTool,
    FsReadTool,
    FsWriteTool,
)


class _FakeSandbox:
    def __init__(self, root: Path, verdict: str = "auto", mode: str = "default") -> None:
        self._root = root
        self._verdict = verdict
        self._mode = mode

    def root(self) -> Path:
        return self._root

    def resolve_in_root(self, path: str) -> Path:
        candidate = Path(str(path or ""))
        if not candidate.is_absolute():
            candidate = self._root / candidate
        return candidate.resolve()

    def contains(self, path) -> bool:
        return True

    def read_verdict(self, path: str) -> str:
        return self._verdict

    def write_verdict(self, path: str) -> str:
        return self._verdict

    @property
    def mode(self) -> str:
        return self._mode


class _FakeApproval:
    """记录审批回调执行线程的假审批服务。"""

    def __init__(self, decision: str = "approved") -> None:
        self._decision = decision
        self.calls: list[tuple[str, dict, int]] = []

    async def request(self, kind: str, payload: dict):
        self.calls.append((kind, payload, threading.get_ident()))
        return type("R", (), {"decision": self._decision})()


MAIN_IDENT = None


@pytest.fixture(autouse=True)
def _remember_main_thread():
    global MAIN_IDENT
    MAIN_IDENT = threading.get_ident()
    yield


def _tool_with(tmp_path: Path, cls, verdict="auto", approval=None):
    t = cls()
    t.computer = _FakeSandbox(tmp_path, verdict=verdict)
    t.approvals = approval or _FakeApproval()
    return t


# -- 线程身份：真实 I/O 不在事件循环线程 --------------------------------------


async def test_fs_read_io_off_event_loop_thread(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello", encoding="utf-8")
    real_open = builtins.open
    io_threads: list[int] = []

    def tracking_open(file, mode="r", *args, **kw):
        io_threads.append(threading.get_ident())
        time.sleep(0.15)  # 慢 I/O：若在事件循环线程上会阻塞测试可感知的时间
        return real_open(file, mode, *args, **kw)

    t = _tool_with(tmp_path, FsReadTool)
    t.approvals = _FakeApproval()
    monkey_target = builtins
    monkey_target.open = tracking_open  # type: ignore[misc]
    try:
        res = await t.run(path=str(target))
    finally:
        monkey_target.open = real_open  # type: ignore[misc]
    assert res.ok and res.content == "hello"
    assert io_threads, "fs_read should perform real open() calls"
    assert all(ident != MAIN_IDENT for ident in io_threads), (
        "fs_read I/O must not run on the event loop thread"
    )


async def test_fs_read_slow_io_does_not_block_loop(tmp_path):
    """慢读期间，同一事件循环上的其他协程能推进（不是被 sleep 卡住）。

    用时间戳证明：ticker 的前几次 tick 落在读完成之前 —— 若 I/O 占住事件
    循环，所有 tick 都会被推迟到读结束之后。
    """
    import asyncio

    target = tmp_path / "slow.txt"
    target.write_text("slow", encoding="utf-8")
    real_read = fs_tools._read_file_text

    def slow_read(path: Path) -> str:
        time.sleep(0.3)
        return real_read(path)

    t = _tool_with(tmp_path, FsReadTool)
    fs_tools._read_file_text = slow_read
    tick_times: list[float] = []

    async def ticker():
        for _ in range(6):
            tick_times.append(time.perf_counter())
            await asyncio.sleep(0.05)

    tick_task = asyncio.ensure_future(ticker())
    res = await t.run(path=str(target))
    done_at = time.perf_counter()
    await tick_task
    fs_tools._read_file_text = real_read
    assert res.ok
    # 至少 2 次 tick 发生在读完成前的明显窗口（read 耗时 0.3s，tick 间隔 0.05s）
    early = [t0 for t0 in tick_times if t0 < done_at - 0.15]
    assert len(early) >= 2, (
        f"event loop was blocked by file I/O: ticks={tick_times}, done_at={done_at}"
    )


async def test_fs_write_io_off_event_loop_thread(tmp_path):
    target = tmp_path / "w.txt"
    real_open = builtins.open
    io_threads: list[int] = []

    def tracking_open(file, mode="r", *args, **kw):
        io_threads.append(threading.get_ident())
        time.sleep(0.1)
        return real_open(file, mode, *args, **kw)

    builtins.open = tracking_open  # type: ignore[misc]
    try:
        res = await _tool_with(tmp_path, FsWriteTool).run(path=str(target), content="hi")
    finally:
        builtins.open = real_open  # type: ignore[misc]
    assert res.ok
    assert target.read_text(encoding="utf-8") == "hi"
    assert io_threads
    assert all(ident != MAIN_IDENT for ident in io_threads)


async def test_fs_patch_io_off_event_loop_thread(tmp_path):
    target = tmp_path / "p.txt"
    target.write_text("old", encoding="utf-8")
    ident_out: list[int] = []
    original_read = fs_tools._read_file_text
    original_write = fs_tools._write_file_text

    def slow_read(path: Path) -> str:
        ident_out.append(threading.get_ident())
        time.sleep(0.1)
        return original_read(path)

    def slow_write(path: Path, text: str) -> None:
        ident_out.append(threading.get_ident())
        time.sleep(0.1)
        original_write(path, text)

    fs_tools._read_file_text = slow_read
    fs_tools._write_file_text = slow_write
    try:
        res = await _tool_with(tmp_path, FsPatchTool).run(
            path=str(target), old="old", new="NEW"
        )
    finally:
        fs_tools._read_file_text = original_read
        fs_tools._write_file_text = original_write
    assert res.ok
    assert target.read_text(encoding="utf-8") == "NEW"
    assert ident_out
    assert all(ident != MAIN_IDENT for ident in ident_out)


async def test_fs_list_io_off_event_loop_thread(tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    ident_out: list[int] = []
    original = fs_tools._list_dir

    def slow_list(path: Path) -> list[str]:
        ident_out.append(threading.get_ident())
        time.sleep(0.1)
        return original(path)

    fs_tools._list_dir = slow_list
    try:
        res = await _tool_with(tmp_path, FsListTool).run(path=str(tmp_path))
    finally:
        fs_tools._list_dir = original
    assert res.ok and "a.txt" in res.content
    assert ident_out and ident_out[0] != MAIN_IDENT


async def test_fs_find_io_off_event_loop_thread(tmp_path):
    (tmp_path / "needle.txt").write_text("n", encoding="utf-8")
    ident_out: list[int] = []
    original = fs_tools._find_files

    def slow_find(root, query, *, limit):
        ident_out.append(threading.get_ident())
        time.sleep(0.1)
        return original(root, query, limit=limit)

    fs_tools._find_files = slow_find
    try:
        res = await _tool_with(tmp_path, FsFindTool).run(query="needle", dir=str(tmp_path))
    finally:
        fs_tools._find_files = original
    assert res.ok and "needle.txt" in res.content
    assert ident_out and ident_out[0] != MAIN_IDENT


async def test_fs_info_io_off_event_loop_thread(tmp_path):
    target = tmp_path / "info.txt"
    target.write_text("x" * 10, encoding="utf-8")
    ident_out: list[int] = []
    original = fs_tools._stat_info

    def slow_stat(path: Path):
        ident_out.append(threading.get_ident())
        time.sleep(0.1)
        return original(path)

    fs_tools._stat_info = slow_stat
    try:
        res = await _tool_with(tmp_path, FsInfoTool).run(path=str(target))
    finally:
        fs_tools._stat_info = original
    assert res.ok and "info.txt" in res.content
    assert ident_out and ident_out[0] != MAIN_IDENT


# -- 审批留在事件循环；审批线程 != executor 线程 -------------------------------


async def test_approval_callback_runs_on_loop_thread_not_executor_thread(tmp_path):
    """根外读需要审批：审批回调在事件循环线程上；executor 线程 != 审批线程。"""
    target = tmp_path / "outside.txt"
    target.write_text("outside", encoding="utf-8")
    real_open = builtins.open
    io_threads: list[int] = []

    def tracking_open(file, mode="r", *args, **kw):
        io_threads.append(threading.get_ident())
        return real_open(file, mode, *args, **kw)

    builtins.open = tracking_open  # type: ignore[misc]
    appr = _FakeApproval()
    try:
        res = await _tool_with(tmp_path, FsReadTool, verdict="approve", approval=appr).run(
            path=str(target)
        )
    finally:
        builtins.open = real_open  # type: ignore[misc]
    assert res.ok and res.content == "outside"
    assert appr.calls, "approval should have been requested"
    for _kind, _payload, ident in appr.calls:
        assert ident == MAIN_IDENT, "approval callback must run on the event loop thread"
    assert io_threads
    assert all(ident != MAIN_IDENT for ident in io_threads), (
        "I/O threads must differ from the approval (event loop) thread"
    )
    # 审批线程集合与 I/O 线程集合不相交
    assert not {ident for _, _, ident in appr.calls} & set(io_threads)


# -- 语义回归：错文案 / limit / 拒绝路径不变 -----------------------------------


async def test_fs_semantics_unchanged_after_threading(tmp_path):
    t = _tool_with(tmp_path, FsReadTool, verdict="deny")
    res = await t.run(path=str(tmp_path / ".env"))
    assert not res.ok and "拒绝" in res.error


async def test_fs_find_limit_unchanged(tmp_path):
    for i in range(8):
        (tmp_path / f"hit_{i}.txt").write_text("x", encoding="utf-8")
    res = await _tool_with(tmp_path, FsFindTool).run(query="hit_", dir=str(tmp_path))
    assert res.ok
    assert len(res.content.splitlines()) <= 50  # FIND_LIMIT=50
    assert len(res.content.splitlines()) == 8


async def test_fs_patch_missing_old_error_unchanged(tmp_path):
    target = tmp_path / "e.txt"
    target.write_text("aaa\n", encoding="utf-8")
    res = await _tool_with(tmp_path, FsPatchTool).run(path=str(target), old="zzz", new="x")
    assert not res.ok and res.error == "未找到待替换的内容 old"


async def test_fs_rejection_means_no_io(tmp_path):
    """拒绝路径不得触发任何真实 I/O。"""
    t = _tool_with(tmp_path, FsWriteTool, verdict="deny")
    res = await t.run(path=str(tmp_path / "nope.txt"), content="x")
    assert not res.ok and "拒绝" in res.error
    assert not (tmp_path / "nope.txt").exists()
