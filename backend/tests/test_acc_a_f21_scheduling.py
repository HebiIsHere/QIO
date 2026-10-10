"""A 组反例：F21 read_attachment 同步 I/O 和解析阻塞事件循环，超时不能及时生效。

契约来源：docs/plans/2026-10-09-process-attachment-audit-consolidation.md C7 +
AGENTS.md（线程纪律：数据库只能在事件循环线程访问）。基线现状：ReadAttachmentTool.run
是 async，但方法体里全是同步 open/read/parse —— registry 用 asyncio.wait_for 包的
超时在同步阻塞期间**根本没有机会触发**，事件循环心跳与其它 API 一起被拖住。

反例（基线应当红）：
* 慢文件读取期间，事件循环线程上不得发生文件读取（用**线程身份**做确定性判据）；
* 慢读取期间真实心跳仍能推进（墙钟作为 smoke，容差放宽，不作主判据）；
* 取消 / 超时后工作真的停止（不是只停止等待），且不在工作线程碰数据库。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from agent.services.attachments import AttachmentService
from agent.tools import attachment_tools as tool_mod
from agent.tools.attachment_tools import ReadAttachmentTool


@pytest.fixture()
def svc(db_conn, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


def _tool(svc: AttachmentService) -> ReadAttachmentTool:
    return ReadAttachmentTool(svc, active_turn_id=lambda: "t")


def _prepare(svc: AttachmentService, path: Path):
    att = svc.prepare(str(path))
    return svc.run_prepare(att.id)


async def test_file_read_runs_off_the_event_loop_thread(svc: AttachmentService, tmp_path: Path, monkeypatch):
    source = tmp_path / "slow.txt"
    source.write_text("慢读取的内容\n第二行\n", encoding="utf-8")
    att = _prepare(svc, source)

    loop_thread = threading.get_ident()
    read_threads: list[int] = []
    work_threads: list[int] = []
    original = tool_mod._head_bytes

    def slow_head(path: Path, count: int) -> bytes:
        read_threads.append(threading.get_ident())
        time.sleep(0.3)
        return original(path, count)

    monkeypatch.setattr(tool_mod, "_head_bytes", slow_head)

    original_avail = svc.availability

    def spy_avail(att_obj):
        work_threads.append(threading.get_ident())
        return original_avail(att_obj)

    monkeypatch.setattr(svc, "availability", spy_avail)

    result = await _tool(svc).run(attachment_id=att.id, offset=0, limit=5)
    assert result.ok is True, result.error
    assert read_threads, "文件读取必须发生"
    assert all(tid != loop_thread for tid in read_threads), (
        "文件 I/O 不能在事件循环线程上做",
        read_threads,
        loop_thread,
    )
    assert work_threads and all(tid == loop_thread for tid in work_threads), (
        "数据库/服务状态只能在事件循环线程访问",
        work_threads,
        loop_thread,
    )


async def test_slow_read_keeps_loop_responsive(svc: AttachmentService, tmp_path: Path, monkeypatch):
    source = tmp_path / "slow2.txt"
    source.write_text("内容\n", encoding="utf-8")
    att = _prepare(svc, source)
    original = tool_mod._head_bytes

    def slow_head(path: Path, count: int) -> bytes:
        time.sleep(0.3)
        return original(path, count)

    monkeypatch.setattr(tool_mod, "_head_bytes", slow_head)

    ticks: list[float] = []
    stop = False

    async def heartbeat() -> None:
        while not stop:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.005)

    task = asyncio.create_task(heartbeat())
    try:
        result = await _tool(svc).run(attachment_id=att.id, offset=0, limit=5)
    finally:
        stop = True
        await task
    assert result.ok is True, result.error
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert gaps, "心跳必须真的跑过"
    # 墙钟只作 smoke（容差放宽）：主判据是上面的线程身份
    assert max(gaps) < 0.25, ("慢读取期间事件循环不应停摆", max(gaps))


async def test_worker_is_actually_stopped_not_just_abandoned(svc: AttachmentService):
    tool = _tool(svc)
    started = threading.Event()
    stopped = threading.Event()

    def cooperative(_cancel) -> str:
        started.set()
        while not _cancel.is_set():
            time.sleep(0.005)
        stopped.set()
        return "done"

    task = asyncio.create_task(tool._run_worker(cooperative))
    assert await asyncio.to_thread(started.wait, 5.0), "工作线程没有启动"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set(), "取消必须让工作真的停止，而不是只停止等待"


async def test_timeout_completes_and_worker_really_stops(svc: AttachmentService, tmp_path: Path, monkeypatch):
    source = tmp_path / "timeout.txt"
    source.write_text("内容\n", encoding="utf-8")
    att = _prepare(svc, source)
    original = tool_mod._head_bytes
    finished = threading.Event()

    def slow_head(path: Path, count: int) -> bytes:
        try:
            time.sleep(0.4)
            return original(path, count)
        finally:
            finished.set()

    monkeypatch.setattr(tool_mod, "_head_bytes", slow_head)

    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(_tool(svc).run(attachment_id=att.id, offset=0, limit=5), timeout=0.12)
    elapsed = time.monotonic() - started
    assert finished.is_set(), "超时后不能留下仍在后台跑的工作"
    assert elapsed < 3.0, ("超时应在有界时间内完成（含等待工作真正停止）", elapsed)
