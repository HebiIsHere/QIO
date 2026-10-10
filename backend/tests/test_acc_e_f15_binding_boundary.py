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
import os
import shutil
import subprocess
import sys
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


def _decode_output(raw: bytes) -> str:
    """解码 icacls 的字节输出：解码本身绝不能再抛异常（否则掩盖真正的装置故障）。"""
    for encoding in ("utf-8", "mbcs", "latin-1"):
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _whoami() -> str:
    """当下解析执行身份；解析不出来就没有可用的主体，必须大声失败。"""
    done = subprocess.run(["whoami"], capture_output=True)
    if done.returncode != 0:
        raise AssertionError(
            "whoami 解析失败（拒读装置无法确定主体）："
            f"退出码 {done.returncode}\nSTDOUT:\n{_decode_output(done.stdout)}\nSTDERR:\n{_decode_output(done.stderr)}"
        )
    user = _decode_output(done.stdout).strip()
    if not user:
        raise AssertionError("whoami 没有输出主体标识，拒读装置不可用")
    return user


def _icacls(*args: str):
    """执行 icacls 并强制检查返回码；失败必须带退出码与 stdout/stderr。"""
    cmd = ["icacls", *[str(part) for part in args]]
    done = subprocess.run(cmd, capture_output=True)
    if done.returncode != 0:
        raise AssertionError(
            "icacls 调用失败：\n"
            f"命令：{' '.join(cmd)}\n退出码：{done.returncode}\n"
            f"STDOUT:\n{_decode_output(done.stdout)}\nSTDERR:\n{_decode_output(done.stderr)}"
        )
    return done


def _readable(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            handle.read(1)
    except OSError:
        return False
    return True


def _deny_read(path: Path) -> None:
    """造真实拒读并**自证生效**；不生效就跳过（绝不把无效装置当成权限反例）。

    Windows 上**不动继承**：直接 icacls <path> /deny <user>:(RD)。

    为什么是 (RD) 而不是 (R)：icacls 的 (R) 连 READ_CONTROL 一起拒；pytest 的 tmp_path
    在 Windows 上是「只有 OWNER RIGHTS」的 ACL，而普通用户 token 里 Administrators 是
    仅用于拒绝（UAC 过滤），此时 (R) 会让 icacls 自己都读不到安全描述符、/remove:d 返回
    rc=5 Access denied —— 装置再也恢复不了（本机实测）。只拒 FILE_READ_DATA(RD) 精确地
    让 open/read 抛 PermissionError，而恢复动作始终可行。
    上一版先 /inheritance:r + /grant:r <user>:(F) 再 /deny：icacls 会把那条 grant
    改写成去掉 R 的 (W,D,WDAC,WO,X,DC)，于是只 /remove:d 仍读不了，而"再 grant 回来"
    在部分 runner 上没有生效（check=False 吞掉失败）→ run 38033354514 的 F17/F18 两条红。
    """
    if sys.platform == "win32":
        user = _whoami()  # 当下解析；诊断信息里带上这个主体
        _icacls(str(path), "/deny", f"{user}:(RD)")  # 只拒 FILE_READ_DATA，恢复始终可行
    else:
        if os.geteuid() == 0:
            pytest.skip("root 下 chmod 不构成权限反例（chmod 无效）")
        path.chmod(0o000)
    if _readable(path):
        _allow_read(path)
        pytest.skip("本机权限装置无法真正拒读（可能以特权/备份权限运行）；不作为反例")


def _allow_read(path: Path) -> None:
    """Windows 恢复只做 icacls <path> /remove:d <user>（继承从未被破坏，读权限自动回来）。

    恢复必须**复核可读**：恢复不了要大声失败（带 icacls 输出），绝不静默放过，
    也绝不把装置故障当成产品缺陷或"测试通过"；/reset 只作兜底。
    """
    if sys.platform == "win32":
        user = _whoami()
        _icacls(str(path), "/remove:d", user)
        if not _readable(path):
            _icacls(str(path), "/reset")  # 兜底：重建继承
            if not _readable(path):
                raise AssertionError(
                    f"拒读装置无法恢复可读：{path}（主体 {user}）；"
                    "icacls /remove:d 与 /reset 之后仍然读不到。这是装置故障，不是产品缺陷。"
                )
        return
    path.chmod(0o600)
    if not _readable(path):
        raise AssertionError(f"拒读装置无法恢复可读：{path}（chmod 600 之后仍然读不到）")


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

    _deny_read(stored)  # 真拒读并自证：Windows 上只 /deny，不动继承

    try:
        release.set()
        outcome = await asyncio.wait_for(task, timeout=15)
    finally:
        _allow_read(stored)  # 恢复只做 /remove:d，并复核可读；恢复不了大声失败

    assert outcome.bound == [], (
        "等待期间第二条变得不可读，整轮仍被受理",
        {"bound": outcome.bound, "rejected": outcome.rejected},
    )
    assert second.id in [item[0] for item in outcome.rejected], outcome.rejected
    assert svc.list(turn_id="turn_new", check=False) == []
