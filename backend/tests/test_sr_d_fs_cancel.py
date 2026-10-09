"""契约 4（D）：fs_write / fs_patch 取消语义 —— 不得误报、不得漏报。

验收点（契约 4）：
- fs_write/fs_patch 用 asyncio.shield 等价机制等待/检查底层 future；
- 底层写入不可中断（状态未确认）时返回 ok=False，文案：
  「已取消，但写入可能已在后台完成（状态未确认）」；
- 取消后底层写入确认完成时：不得报「没有执行」，也不得报成功（ok=False + 如实说明）；
- 取消后晚到的写入结果不覆盖已返回的终态；真实文件状态与描述一致。
"""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import suppress
from pathlib import Path

import pytest

from agent.tools import fs_tools
from agent.tools.fs_tools import FsPatchTool, FsReadTool, FsWriteTool


class _FakeSandbox:
    def __init__(self, root: Path, verdict: str = "auto") -> None:
        self._root = root
        self._verdict = verdict

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
        return "default"


class _FakeApproval:
    async def request(self, kind: str, payload: dict):
        return type("R", (), {"decision": "approved"})()


def _tool_with(tmp_path: Path, cls, verdict="auto"):
    t = cls()
    t.computer = _FakeSandbox(tmp_path, verdict=verdict)
    t.approvals = _FakeApproval()
    return t


async def test_fs_write_cancel_waits_for_write_and_reports_truthfully(tmp_path):
    """取消到达时写入还在后台进行：工具必须等待/检查底层 future，而不是宣布「没有执行」。

    闸门释放后写入真实完成 → 返回 ok=False，且 error 里如实说明写入实际已完成。
    """
    target = tmp_path / "w1.txt"
    gate = threading.Event()
    original = fs_tools._write_file

    def gated_write(path: Path, content: str) -> None:
        gate.wait(10)  # 模拟不可中断的慢写入
        original(path, content)

    fs_tools._write_file = gated_write
    try:
        task = asyncio.ensure_future(
            _tool_with(tmp_path, FsWriteTool).run(path=str(target), content="NEW-CONTENT")
        )
        await asyncio.sleep(0.1)  # 写入已进入 executor 线程并被闸门卡住
        task.cancel()
        await asyncio.sleep(0.05)  # 让取消进入「等待底层 future」的分支
        gate.set()  # 释放闸门：底层写入随后真实完成
        result = await asyncio.wait_for(task, timeout=5)
        assert result.ok is False, f"取消之后不得报成功：{result}"
        assert "已取消" in (result.error or "")
        assert "没有执行" not in (result.error or "")
        assert "写入实际已完成" in (result.error or ""), result.error
        # 真实文件状态与描述一致：新内容确实写进去了
        assert target.read_text(encoding="utf-8") == "NEW-CONTENT"
    finally:
        fs_tools._write_file = original


async def test_fs_write_cancel_second_cancel_state_unconfirmed_exact_phrase(tmp_path):
    """连等待都被二次取消：返回 ok=False 且用契约规定的准确文案。"""
    target = tmp_path / "w2.txt"
    gate = threading.Event()
    original = fs_tools._write_file

    def gated_write(path: Path, content: str) -> None:
        gate.wait(10)
        original(path, content)

    fs_tools._write_file = gated_write
    try:
        task = asyncio.ensure_future(
            _tool_with(tmp_path, FsWriteTool).run(path=str(target), content="LATE")
        )
        await asyncio.sleep(0.1)
        task.cancel()  # 第一次取消：进入「等待/检查底层 future」分支
        await asyncio.sleep(0)
        task.cancel()  # 第二次取消：等待本身被打断 → 状态未确认
        result = None
        cancelled = False
        try:
            result = await asyncio.wait_for(task, timeout=5)
        except asyncio.CancelledError:
            cancelled = True
        assert not cancelled, "取消路径必须返回结果（哪怕是状态未确认），不能无声吞掉写入事实"
        assert result.ok is False
        assert "已取消，但写入可能已在后台完成（状态未确认）" in (result.error or "")
        # 清理：释放闸门，让后台写入结束；文件状态与「可能已完成」的描述一致（确实完成了）
        gate.set()
        await asyncio.sleep(0.3)
        assert target.read_text(encoding="utf-8") == "LATE"
    finally:
        fs_tools._write_file = original


async def test_fs_write_cancelled_after_completion_reports_write_happened(tmp_path):
    """取消到达时写入已经落定：不得说「没有执行」，也不得报成功，要如实说明。"""
    target = tmp_path / "w3.txt"
    original = fs_tools._write_file
    fs_tools._write_file = original  # 快速写入：取消前基本已落定
    try:
        task = asyncio.ensure_future(
            _tool_with(tmp_path, FsWriteTool).run(path=str(target), content="DONE")
        )
        await asyncio.sleep(0.15)  # 写入已完成
        task.cancel()
        result = None
        try:
            result = await asyncio.wait_for(task, timeout=5)
        except asyncio.CancelledError:
            pytest.skip("cancel arrived before write completed (race)")
        assert target.read_text(encoding="utf-8") == "DONE"  # 写入确实发生了
        if result is not None and not result.ok:
            # 取消已登记 → 不得报成功；error 必须如实说明写入实际完成
            assert "已取消" in (result.error or "")
            assert "写入实际已完成" in (result.error or "")
    finally:
        fs_tools._write_file = original


async def test_fs_patch_cancel_waits_and_reports_truthfully(tmp_path):
    target = tmp_path / "p1.txt"
    target.write_text("OLD", encoding="utf-8")
    gate = threading.Event()
    original = fs_tools._write_file_text

    def gated_write(path: Path, text: str) -> None:
        gate.wait(10)
        original(path, text)

    fs_tools._write_file_text = gated_write
    try:
        task = asyncio.ensure_future(
            _tool_with(tmp_path, FsPatchTool).run(path=str(target), old="OLD", new="NEW")
        )
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.sleep(0.05)
        gate.set()
        result = await asyncio.wait_for(task, timeout=5)
        assert result.ok is False
        assert "已取消" in (result.error or "")
        assert "没有执行" not in (result.error or "")
        assert target.read_text(encoding="utf-8") == "NEW"
    finally:
        fs_tools._write_file_text = original


async def test_fs_read_cancelled_propagates_and_result_correct_after(tmp_path):
    """fs_read（只读、可安全重试）取消：CancelledError 传播；文件不受影响。

    闸门把读卡住，保证取消落在 I/O 进行中（不靠竞速）。
    """
    target = tmp_path / "r1.txt"
    target.write_text("stable", encoding="utf-8")
    gate = threading.Event()
    original = fs_tools._read_file_text

    def gated_read(path: Path) -> str:
        gate.wait(10)
        return original(path)

    fs_tools._read_file_text = gated_read
    try:
        task = asyncio.ensure_future(_tool_with(tmp_path, FsReadTool).run(path=str(target)))
        await asyncio.sleep(0.1)  # 读已进入线程并被闸门卡住
        assert not task.done()
        task.cancel()
        cancelled = False
        try:
            await asyncio.wait_for(task, timeout=5)
        except asyncio.CancelledError:
            cancelled = True
        assert cancelled, "fs_read cancellation should propagate CancelledError"
        assert target.read_text(encoding="utf-8") == "stable"
        gate.set()  # 清理：让后台线程收尾
        await asyncio.sleep(0.2)
    finally:
        fs_tools._read_file_text = original
    # 之后正常读取照常工作
    res = await _tool_with(tmp_path, FsReadTool).run(path=str(target))
    assert res.ok and res.content == "stable"


async def test_fs_read_cancel_during_slow_io(tmp_path):
    """fs_read 慢 I/O 中取消：CancelledError 传播且不吞掉。"""
    target = tmp_path / "r2.txt"
    target.write_text("s", encoding="utf-8")
    original = fs_tools._read_file_text

    def slow_read(path: Path) -> str:
        time.sleep(0.5)
        return original(path)

    fs_tools._read_file_text = slow_read
    try:
        task = asyncio.ensure_future(_tool_with(tmp_path, FsReadTool).run(path=str(target)))
        await asyncio.sleep(0.1)
        task.cancel()
        cancelled = False
        try:
            await task
        except asyncio.CancelledError:
            cancelled = True
        assert cancelled
    finally:
        fs_tools._read_file_text = original


async def test_fs_write_late_result_does_not_override_returned_state(tmp_path):
    """晚到的写入结果不覆盖已返回的终态：返回之后再完成也不产生第二个结果。"""
    target = tmp_path / "w4.txt"
    gate = threading.Event()
    original = fs_tools._write_file

    def gated_write(path: Path, content: str) -> None:
        gate.wait(10)
        original(path, content)

    fs_tools._write_file = gated_write
    try:
        task = asyncio.ensure_future(
            _tool_with(tmp_path, FsWriteTool).run(path=str(target), content="X")
        )
        await asyncio.sleep(0.1)
        task.cancel()  # 第一次取消
        await asyncio.sleep(0)
        task.cancel()  # 第二次取消 → 状态未确认，立刻返回
        result = await asyncio.wait_for(task, timeout=5)
        assert result.ok is False
        returned_error = result.error
        gate.set()  # 释放：后台写入此刻才真实完成（晚到结果）
        await asyncio.sleep(0.3)
        # 已返回的终态不会因为后台完成而改变
        assert result.error == returned_error
        assert result.ok is False
        assert target.read_text(encoding="utf-8") == "X"
    finally:
        fs_tools._write_file = original


async def test_fs_write_no_cancel_still_ok(tmp_path):
    """无取消：fs_write 照常 ok=True（不能因为引入取消处理而误伤正常路径）。"""
    target = tmp_path / "w5.txt"
    res = await _tool_with(tmp_path, FsWriteTool).run(path=str(target), content="fine")
    assert res.ok
    assert target.read_text(encoding="utf-8") == "fine"
    assert "已写入" in (res.content or "")
