"""ACC-E / F18 反例：大文件引用已消失/不可读时的重试契约。

用户可见规则：
* 重试克隆**必须重新验证当前事实**：存在、状态、**可读性**（不能只 stat 就当成可用）；
* 缺失引用**不得克隆成可用新附件**（新行必须是 missing，不是 ready；模型上下文如实说不可访问）；
* 不可读（拒读 ACL）或已变成目录 → 重试必须**结构化拒绝**，且**不新建**可用新行；
* 保留历史记录与重新定位入口（原行不动），不擅自搜索替换同名文件；
* 原文件恢复可读之后，同一条重试必须能正常得到 ready 克隆；
* 不破坏纯文字发送兼容（显式空列表不受历史失败附件阻断）。

运行：cd backend; uv run --frozen --extra dev pytest tests/test_acc_e_f18_reference_retry.py -q
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent.services import attachments as attachments_mod
from agent.services.attachments import COPY_MAX_BYTES, AttachmentService


def _sparse(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.truncate(size)
    return path


def _readable(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            handle.read(1)
    except OSError:
        return False
    return True


def _whoami() -> str:
    return subprocess.run(["whoami"], capture_output=True, text=True, check=True).stdout.strip()


def _deny_read(path: Path) -> None:
    if sys.platform == "win32":
        user = _whoami()
        subprocess.run(["icacls", str(path), "/inheritance:r"], check=True, capture_output=True)
        subprocess.run(["icacls", str(path), "/grant:r", f"{user}:(F)"], check=True, capture_output=True)
        subprocess.run(["icacls", str(path), "/deny", f"{user}:(R)"], check=True, capture_output=True)
    else:
        if os.geteuid() == 0:
            pytest.skip("root 下 chmod 不构成权限反例（chmod 无效）")
        path.chmod(0o000)
    if _readable(path):
        _allow_read(path)
        pytest.skip("本机权限装置无法真正拒读；不作为反例")


def _allow_read(path: Path) -> None:
    if sys.platform == "win32":
        user = _whoami()
        subprocess.run(["icacls", str(path), "/remove:d", user], check=False, capture_output=True)
        subprocess.run(["icacls", str(path), "/grant:r", f"{user}:(F)"], check=False, capture_output=True)
    else:
        path.chmod(0o600)


@pytest.fixture()
def svc(db_conn, tmp_path: Path, monkeypatch) -> AttachmentService:
    monkeypatch.setattr(attachments_mod, "COPY_MAX_BYTES", 16)
    return AttachmentService(db_conn, tmp_path / "data")


def _reference(svc: AttachmentService, path: Path) -> object:
    att = svc.prepare(str(path), topic_id="t1")
    att = svc.run_prepare(att.id)
    assert att.kind == "reference" and att.state == "ready"
    return att


# -- 1. 核心反例：引用不可读（stat 正常）→ 重试必须拒绝，不得产出 ready 新行 ------


async def test_unreadable_reference_retry_is_rejected(svc, tmp_path):
    path = _sparse(tmp_path / "不可读大文件.bin", 64)
    att = _reference(svc, path)
    await svc.bind_for_turn(turn_id="turn_old", attachment_ids=[att.id], topic_id="t1")

    _deny_read(path)
    try:
        assert not _readable(path), "装置自证：这一刻引用真的读不了"
        outcome = await svc.bind_for_turn(
            turn_id="turn_new", attachment_ids=[att.id], topic_id="t1", retry_of_turn_id="turn_old"
        )
    finally:
        _allow_read(path)

    assert outcome.bound == [], (
        "引用读不了，重试仍建出可用新附件（F18 缺陷）",
        {"bound": outcome.bound, "rejected": outcome.rejected},
    )
    assert [item[0] for item in outcome.rejected] == [att.id]
    reason = outcome.rejected[0][1]
    assert ("读不到" in reason or "打开" in reason or "权限" in reason), reason
    assert svc.list(turn_id="turn_new", check=False) == [], "被拒的重试不得留下任何新行"
    assert svc.get(att.id).turn_id == "turn_old", "原行归属不得被改写"


async def test_reference_retry_works_again_after_permission_restored(svc, tmp_path):
    path = _sparse(tmp_path / "恢复可读.bin", 64)
    att = _reference(svc, path)
    await svc.bind_for_turn(turn_id="turn_old", attachment_ids=[att.id], topic_id="t1")
    _deny_read(path)
    assert not _readable(path)
    _allow_read(path)
    assert _readable(path), "装置自证：恢复之后必须可读"

    outcome = await svc.bind_for_turn(
        turn_id="turn_new", attachment_ids=[att.id], topic_id="t1", retry_of_turn_id="turn_old"
    )
    assert outcome.rejected == [], outcome.rejected
    assert len(outcome.bound) == 1
    clone = svc.get(outcome.bound[0])
    assert clone.kind == "reference" and clone.state == "ready"
    assert clone.source_attachment_id == att.id


# -- 2. 缺失引用：建 missing 行（不是可用新附件），保留历史与重定位入口 ----------


async def test_missing_reference_retry_builds_non_usable_row(svc, tmp_path):
    path = _sparse(tmp_path / "消失的大文件.bin", 64)
    att = _reference(svc, path)
    await svc.bind_for_turn(turn_id="turn_old", attachment_ids=[att.id], topic_id="t1")
    path.unlink()

    outcome = await svc.bind_for_turn(
        turn_id="turn_new", attachment_ids=[att.id], topic_id="t1", retry_of_turn_id="turn_old"
    )
    assert len(outcome.bound) == 1
    clone = svc.get(outcome.bound[0])
    assert clone.state == "missing", "缺失引用不得被克隆成可用（ready）新附件"
    assert clone.kind == "reference"
    assert clone.source_path == str(path), "不得擅自搜索替换同名文件"
    payload = svc.payload(clone)
    assert payload["actions"] == ["relocate"], "必须保留重新定位入口"
    assert payload["readability"] is not None
    note = svc.turn_note("turn_new") or ""
    assert clone.id in note and "当前不可访问" in note
    # 原行历史保留
    assert svc.get(att.id).turn_id == "turn_old"


# -- 3. 变成目录：预检与绑定都必须给出原因（只读检查不得漏掉） -------------------


async def test_reference_became_directory_is_rejected(svc, tmp_path):
    path = _sparse(tmp_path / "会变成目录.bin", 64)
    att = _reference(svc, path)
    await svc.bind_for_turn(turn_id="turn_old", attachment_ids=[att.id], topic_id="t1")
    path.unlink()
    path.mkdir()

    precheck = svc.precheck_for_turn(
        attachment_ids=[att.id], topic_id="t1", retry_of_turn_id="turn_old"
    )
    assert precheck and "目录" in precheck[0][1], (
        "只读预检必须重算引用的当前事实（_clone_reason 的死代码让目录漏检）",
        precheck,
    )
    outcome = await svc.bind_for_turn(
        turn_id="turn_new", attachment_ids=[att.id], topic_id="t1", retry_of_turn_id="turn_old"
    )
    assert outcome.bound == [] and [item[0] for item in outcome.rejected] == [att.id]
    assert svc.list(turn_id="turn_new", check=False) == []


# -- 4. 绿守卫：正常引用重试与显式空列表 ---------------------------------------


async def test_healthy_reference_retry_clones_ready(svc, tmp_path):
    path = _sparse(tmp_path / "健康引用.bin", 64)
    att = _reference(svc, path)
    await svc.bind_for_turn(turn_id="turn_old", attachment_ids=[att.id], topic_id="t1")
    outcome = await svc.bind_for_turn(
        turn_id="turn_new", attachment_ids=[att.id], topic_id="t1", retry_of_turn_id="turn_old"
    )
    clone = svc.get(outcome.bound[0])
    assert clone.state == "ready" and clone.source_path == str(path)


async def test_explicit_empty_list_unaffected_by_broken_history_reference(svc, tmp_path):
    path = _sparse(tmp_path / "历史坏引用.bin", 64)
    att = _reference(svc, path)
    await svc.bind_for_turn(turn_id="turn_old", attachment_ids=[att.id], topic_id="t1")
    path.unlink()
    # 历史失败/缺失附件不得阻断纯文字发送（显式空列表）
    outcome = await svc.bind_for_turn(turn_id="turn_plain", attachment_ids=[], topic_id="t1")
    assert outcome.bound == [] and outcome.rejected == []
