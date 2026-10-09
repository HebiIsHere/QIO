"""ACC-E / F16 反例：多个重试附件的后续项失败/取消时，前序克隆缺少回滚。

用户可见规则（契约 C6）：
* 重试克隆是**集合级提交**：本轮产生的中间克隆（新行 / 新副本 / preparing 状态）
  在后续项失败或整轮取消时必须**完整补偿**：删行、删副本、清 preparing；
* **保留原历史副本**与源行归属不变；
* 取消时仍在执行的线程不得稍后写回已撤销结果（落库前校验）；
* 不得留下无人认领的副本或 prepared 残留行。

反例构造：两个附件都需要重试克隆；强制走 shutil.copyfile 退路，第二份复制注入 ENOSPC
（或整轮取消）→ 检查第一份的克隆行/副本是否残留。

运行：cd backend; uv run --frozen --extra dev pytest tests/test_acc_e_f16_clone_rollback.py -q
"""

from __future__ import annotations

import asyncio
import errno
import shutil
import threading
from pathlib import Path

import pytest

from agent.services import attachments as attachments_mod
from agent.services.attachments import AttachmentService


@pytest.fixture()
def svc(db_conn, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _ready(svc: AttachmentService, path: Path) -> object:
    return svc.run_prepare(svc.prepare(str(path), topic_id="t1").id)


def _force_copy_fallback(monkeypatch) -> None:
    def refuse_link(*args, **kwargs):
        raise OSError(errno.EPERM, "hardlink not permitted")

    monkeypatch.setattr(attachments_mod.os, "link", refuse_link)


def _install_copyfile(monkeypatch, *, fail_on: int | None = None, gate_on: int | None = None):
    """安装复制退路桩：第 fail_on 次抛 ENOSPC；第 gate_on 次进闸门等 release。"""
    real = shutil.copyfile
    calls = {"n": 0}
    entered = threading.Event()
    release = threading.Event()

    def stub(src, dst, *args, **kwargs):
        calls["n"] += 1
        if gate_on is not None and calls["n"] == gate_on:
            entered.set()
            if not release.wait(10):
                raise AssertionError("测试闸门没有被放开")
        if fail_on is not None and calls["n"] == fail_on:
            raise OSError(errno.ENOSPC, "no space left on device（受控）")
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(attachments_mod.shutil, "copyfile", stub)
    return entered, release, calls


def _orphans(data_dir: Path, keep: list[str]) -> list[Path]:
    keep_set = {str(Path(p)) for p in keep}
    return [
        p
        for p in data_dir.rglob("*")
        if p.is_file() and str(p) not in keep_set
    ]


async def _two_retry_attachments(svc, tmp_path):
    a = _ready(svc, _write(tmp_path / "一.bin", b"a" * 4096))
    b = _ready(svc, _write(tmp_path / "二.bin", b"b" * 4096))
    await svc.bind_for_turn(turn_id="turn_old", attachment_ids=[a.id, b.id], topic_id="t1")
    return a, b


# -- 1. 第二份复制失败 → 第一份克隆完整回滚 --------------------------------------


async def test_second_clone_failure_rolls_back_first_clone(svc, tmp_path, monkeypatch):
    a, b = await _two_retry_attachments(svc, tmp_path)
    _force_copy_fallback(monkeypatch)
    _install_copyfile(monkeypatch, fail_on=2)

    outcome = await svc.bind_for_turn(
        turn_id="turn_new",
        attachment_ids=[a.id, b.id],
        topic_id="t1",
        retry_of_turn_id="turn_old",
    )

    assert outcome.bound == [], (
        "集合级提交：第二份失败，第一份的克隆不得留在回执里",
        {"bound": outcome.bound, "rejected": outcome.rejected},
    )
    assert [item[0] for item in outcome.rejected] == [b.id], outcome.rejected
    assert svc.list(turn_id="turn_new", check=False) == [], (
        "F16：前序克隆的新行残留（后续项失败必须完整回滚）",
        [r.id for r in svc.list(turn_id="turn_new", check=False)],
    )
    leftovers = _orphans(tmp_path / "data", [a.stored_path, b.stored_path])
    assert leftovers == [], ("不得留下无人认领的副本", leftovers)
    # 原历史副本与源行归属不变
    assert svc.get(a.id).turn_id == "turn_old" and svc.get(b.id).turn_id == "turn_old"
    assert Path(a.stored_path).read_bytes() == b"a" * 4096
    assert Path(b.stored_path).read_bytes() == b"b" * 4096


# -- 2. 整轮取消 → 第一份克隆完整回滚，且迟到线程不得写回 ------------------------


async def test_cancel_during_second_clone_rolls_back_first_clone(svc, tmp_path, monkeypatch):
    a, b = await _two_retry_attachments(svc, tmp_path)
    _force_copy_fallback(monkeypatch)
    entered, release, _calls = _install_copyfile(monkeypatch, gate_on=2)

    task = asyncio.create_task(
        svc.bind_for_turn(
            turn_id="turn_new",
            attachment_ids=[a.id, b.id],
            topic_id="t1",
            retry_of_turn_id="turn_old",
        )
    )
    assert await asyncio.to_thread(entered.wait, 10), "第二份复制没有进入闸门"
    task.cancel()
    release.set()  # 工作线程会照常写完，但结果不得提交
    with pytest.raises(asyncio.CancelledError):
        await task

    for _ in range(200):
        if svc.list(turn_id="turn_new", check=False) == []:
            break
        await asyncio.sleep(0.02)
    assert svc.list(turn_id="turn_new", check=False) == [], (
        "F16：取消后前序克隆残留",
        [r.id for r in svc.list(turn_id="turn_new", check=False)],
    )
    leftovers = _orphans(tmp_path / "data", [a.stored_path, b.stored_path])
    assert leftovers == [], ("取消后不得留下无人认领的副本", leftovers)
    assert svc.get(a.id).turn_id == "turn_old" and svc.get(b.id).turn_id == "turn_old"


# -- 3. 绿守卫：两份都成功时都提交 ------------------------------------------------


async def test_both_clones_succeed_when_no_failure(svc, tmp_path, monkeypatch):
    a, b = await _two_retry_attachments(svc, tmp_path)
    _force_copy_fallback(monkeypatch)
    _install_copyfile(monkeypatch)
    outcome = await svc.bind_for_turn(
        turn_id="turn_new",
        attachment_ids=[a.id, b.id],
        topic_id="t1",
        retry_of_turn_id="turn_old",
    )
    assert outcome.rejected == [] and len(outcome.bound) == 2
    assert {svc.get(i).turn_id for i in outcome.bound} == {"turn_new"}
