"""ACC-E / F15 反例：绑定跨 await（等待准备 / 克隆）后仍可能使用失效的归属。

用户可见规则：
* 显式清单是**集合级提交**：任一条在等待期间被删除 / 被别的轮绑定 / 变得不可读，
  整轮必须结构化拒绝，且**一个字节都不写**（不留半绑）；
* 不得**偷取**别的轮已经绑定的附件（归属只能有一个），不得复活已删记录；
* 模型不得带着「错误集合」启动（静默少带一个附件也是错误：请求要么整轮带上，要么整轮拒绝）；
* 复核必须是**当下事实**（重新读行 + 就绪/归属判定），不能拿 await 之前的旧行落库。

反例构造：第一条附件需要重试克隆（文件 I/O 被闸门卡住）→ 趁这段 await 把第二条
绑定到别的轮 / 删掉 / 弄成不可读 → 放行后检查落库与回执。

运行：cd backend; uv run --frozen --extra dev pytest tests/test_acc_e_f15_binding_boundary.py -q
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


def _gated_copyfile(monkeypatch):
    """把重试克隆的复制退路卡在闸门上：返回 (entered, release)。"""
    real = shutil.copyfile
    entered = threading.Event()
    release = threading.Event()

    def gated(src_path, dst_path, *args, **kwargs):
        entered.set()
        if not release.wait(10):
            raise AssertionError("测试闸门没有被放开")
        return real(src_path, dst_path, *args, **kwargs)

    monkeypatch.setattr(attachments_mod.shutil, "copyfile", gated)
    return entered, release


async def _wait_entered(entered) -> None:
    assert await asyncio.to_thread(entered.wait, 10), "克隆没有进入文件 I/O 闸门"


# -- 1. 克隆等待期间第二条被别的轮绑定 → 不得偷取 -------------------------------


async def test_second_item_bound_elsewhere_during_clone_is_not_stolen(svc, tmp_path, monkeypatch):
    first = _ready(svc, _write(tmp_path / "第一条.bin", b"a" * 512))
    second = _ready(svc, _write(tmp_path / "第二条.bin", b"b" * 512))
    await svc.bind_for_turn(turn_id="turn_old", attachment_ids=[first.id], topic_id="t1")

    _force_copy_fallback(monkeypatch)
    entered, release = _gated_copyfile(monkeypatch)
    task = asyncio.create_task(
        svc.bind_for_turn(
            turn_id="turn_new",
            attachment_ids=[first.id, second.id],
            topic_id="t1",
            retry_of_turn_id="turn_old",
        )
    )
    await _wait_entered(entered)

    # 克隆还在跑：第二条被**别的轮**绑定
    other = await svc.bind_for_turn(
        turn_id="turn_other", attachment_ids=[second.id], topic_id="t1"
    )
    assert other.bound == [second.id]

    release.set()
    outcome = await asyncio.wait_for(task, timeout=15)

    assert svc.get(second.id).turn_id == "turn_other", (
        "F15：等待克隆期间第二条被别的轮绑定，放行后仍被偷走",
        svc.get(second.id).turn_id,
    )
    assert outcome.bound == [], (
        "整轮必须拒绝（不得带着半截集合继续）",
        {"bound": outcome.bound, "rejected": outcome.rejected},
    )
    assert second.id in [item[0] for item in outcome.rejected], outcome.rejected
    assert svc.list(turn_id="turn_new", check=False) == [], "被拒的轮次不得留下克隆行"


# -- 2. 克隆等待期间第二条被删除 → 整轮拒绝（不得静默少带） ---------------------


async def test_second_item_deleted_during_clone_rejects_turn(svc, tmp_path, monkeypatch):
    first = _ready(svc, _write(tmp_path / "一.bin", b"a" * 512))
    second = _ready(svc, _write(tmp_path / "二.bin", b"b" * 512))
    await svc.bind_for_turn(turn_id="turn_old", attachment_ids=[first.id], topic_id="t1")

    _force_copy_fallback(monkeypatch)
    entered, release = _gated_copyfile(monkeypatch)
    task = asyncio.create_task(
        svc.bind_for_turn(
            turn_id="turn_new",
            attachment_ids=[first.id, second.id],
            topic_id="t1",
            retry_of_turn_id="turn_old",
        )
    )
    await _wait_entered(entered)
    svc.delete(second.id, purge_copy=True)  # 等待期间被删除
    release.set()
    outcome = await asyncio.wait_for(task, timeout=15)

    assert outcome.bound == [], (
        "等待期间有附件被删除，却仍然受理（F15：静默少带一个附件）",
        {"bound": outcome.bound, "rejected": outcome.rejected},
    )
    assert second.id in [item[0] for item in outcome.rejected], (
        "被删除的那一条必须进 rejected（不得静默跳过）",
        outcome.rejected,
    )
    assert svc.list(turn_id="turn_new", check=False) == []


# -- 3. 克隆等待期间第二条副本不可读 → 整轮拒绝 --------------------------------


async def test_second_item_unreadable_during_clone_rejects_turn(svc, tmp_path, monkeypatch):
    import subprocess
    import sys

    first = _ready(svc, _write(tmp_path / "甲.bin", b"a" * 512))
    second = _ready(svc, _write(tmp_path / "乙.bin", b"b" * 512))
    await svc.bind_for_turn(turn_id="turn_old", attachment_ids=[first.id], topic_id="t1")
    stored = Path(second.stored_path)

    _force_copy_fallback(monkeypatch)
    entered, release = _gated_copyfile(monkeypatch)
    task = asyncio.create_task(
        svc.bind_for_turn(
            turn_id="turn_new",
            attachment_ids=[first.id, second.id],
            topic_id="t1",
            retry_of_turn_id="turn_old",
        )
    )
    await _wait_entered(entered)

    user = subprocess.run(["whoami"], capture_output=True, text=True, check=True).stdout.strip()
    if sys.platform == "win32":
        subprocess.run(["icacls", str(stored), "/inheritance:r"], check=True, capture_output=True)
        subprocess.run(["icacls", str(stored), "/grant:r", f"{user}:(F)"], check=True, capture_output=True)
        subprocess.run(["icacls", str(stored), "/deny", f"{user}:(R)"], check=True, capture_output=True)
    else:
        stored.chmod(0o000)
        if __import__("os").geteuid() == 0:
            pytest.skip("root 下 chmod 不构成权限反例")

    def _restore() -> None:
        if sys.platform == "win32":
            subprocess.run(["icacls", str(stored), "/remove:d", user], check=False, capture_output=True)
        else:
            stored.chmod(0o600)

    try:
        release.set()
        outcome = await asyncio.wait_for(task, timeout=15)
    finally:
        _restore()

    assert outcome.bound == [], (
        "等待期间第二条变得不可读，整轮仍被受理",
        {"bound": outcome.bound, "rejected": outcome.rejected},
    )
    assert second.id in [item[0] for item in outcome.rejected], outcome.rejected
    assert svc.list(turn_id="turn_new", check=False) == []
