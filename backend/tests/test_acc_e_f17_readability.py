"""ACC-E / F17 反例：副本存在且大小正确，但**实际不可读**时仍被放行。

问题（用户可见规则）：
* 执行就绪的唯一条件包含「副本**实际可打开、可读**」——stat 正常、大小一致**不等于**可读；
* 副本不可读（拒读 ACL / 占用 / I/O 错误）时，绑定必须**结构化拒绝**并给出准确、可恢复的原因；
* 恢复（去掉拒读）之后，同一条附件必须能正常绑定；
* 这是**时点检查**：不宣称消除「检查之后到最终打开之间」的全部竞态（那段由工具如实报错）。

反例构造：Windows 用 icacls /deny <user>:(R) 造真实拒读 ACL（管理员下同样生效，
本用例先自证 open 真的抛 PermissionError 才继续）；POSIX 用 chmod 000（root 下跳过）。
**不使用**管理员/root 下无效的 chmod 充当权限反例。

运行：cd backend; uv run --frozen --extra dev pytest tests/test_acc_e_f17_readability.py -q
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent.services.attachments import AttachmentService


def _whoami() -> str:
    return subprocess.run(["whoami"], capture_output=True, text=True, check=True).stdout.strip()


def _readable(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            handle.read(1)
    except OSError:
        return False
    return True


def _deny_read(path: Path) -> None:
    """造真实拒读并**自证生效**；不生效就跳过（绝不把无效装置当成权限反例）。"""
    if sys.platform == "win32":
        user = _whoami()
        subprocess.run(["icacls", str(path), "/inheritance:r"], check=True, capture_output=True)
        subprocess.run(
            ["icacls", str(path), "/grant:r", f"{user}:(F)"], check=True, capture_output=True
        )
        subprocess.run(["icacls", str(path), "/deny", f"{user}:(R)"], check=True, capture_output=True)
    else:
        if os.geteuid() == 0:
            pytest.skip("root 下 chmod 不构成权限反例（chmod 无效）")
        path.chmod(0o000)
    if _readable(path):
        _allow_read(path)
        pytest.skip("本机权限装置无法真正拒读（可能以特权/备份权限运行）；不作为反例")


def _allow_read(path: Path) -> None:
    if sys.platform == "win32":
        user = _whoami()
        subprocess.run(["icacls", str(path), "/remove:d", user], check=False, capture_output=True)
        subprocess.run(["icacls", str(path), "/grant:r", f"{user}:(F)"], check=False, capture_output=True)
    else:
        path.chmod(0o600)


@pytest.fixture()
def svc(db_conn, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


def _ready_copy(svc: AttachmentService, tmp_path: Path, body: bytes = b"readable body"):
    source = tmp_path / "f17-source.txt"
    source.write_bytes(body)
    att = svc.run_prepare(svc.prepare(str(source), topic_id="t1").id)
    assert att.state == "ready" and att.stored_path
    return att, Path(att.stored_path)


# -- 1. 核心反例：副本存在、大小正确、实际不可读 → 不得放行 --------------------


async def test_unreadable_copy_is_not_accepted_as_ready(svc, tmp_path):
    att, stored = _ready_copy(svc, tmp_path)
    assert stored.stat().st_size == int(att.size_bytes), "装置前置：大小必须是对的"

    _deny_read(stored)
    try:
        assert not _readable(stored), "装置自证：这一刻副本真的读不了"
        outcome = await svc.bind_for_turn(
            turn_id="turn_f17", attachment_ids=[att.id], topic_id="t1"
        )
    finally:
        _allow_read(stored)

    assert outcome.bound == [], (
        "副本 stat 正常但实际打不开，仍被当成 ready 放行（F17 缺陷）",
        {"bound": outcome.bound, "rejected": outcome.rejected},
    )
    assert [item[0] for item in outcome.rejected] == [att.id]
    reason = outcome.rejected[0][1]
    assert ("读不到" in reason or "打开" in reason or "权限" in reason), (
        "原因必须说清是「读不到副本」，不能只说通用文案",
        reason,
    )


# -- 2. 可恢复：去掉拒读之后同一条必须能正常绑定 -------------------------------


async def test_copy_binds_again_after_read_permission_is_restored(svc, tmp_path):
    att, stored = _ready_copy(svc, tmp_path)
    _deny_read(stored)
    assert not _readable(stored)
    _allow_read(stored)
    assert _readable(stored), "装置自证：恢复之后必须可读"

    outcome = await svc.bind_for_turn(turn_id="turn_f17b", attachment_ids=[att.id], topic_id="t1")
    assert outcome.rejected == [], outcome.rejected
    assert outcome.bound == [att.id]


# -- 3. 绿守卫：正常副本照常放行 -----------------------------------------------


async def test_normal_readable_copy_is_accepted(svc, tmp_path):
    att, _stored = _ready_copy(svc, tmp_path)
    outcome = await svc.bind_for_turn(turn_id="turn_f17c", attachment_ids=[att.id], topic_id="t1")
    assert outcome.rejected == [] and outcome.bound == [att.id]
