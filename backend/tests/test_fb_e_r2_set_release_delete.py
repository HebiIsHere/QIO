"""fb-E / R2 反例：集合提交缺少「整组最终复核」——等待期间被删除的前项仍被报成已绑定。

用户可见规则（K1 + 契约 §1.3 集合级提交）：
* 集合级提交要么整组成功、要么整组拒绝（无一成员半绑）；
* 回执（outcome.bound）里的**每一个 ID 在收敛时刻都必须仍然存在、且确实归属本轮**；
* 等待期间被用户删除的成员，绝不能被当作成功绑定返回。

确定性交错：
  1) a 是本话题的草稿（未绑定）；b 已绑定在 turn_old 上（重试复用 → 走克隆，克隆里有 await）；
  2) bind_for_turn(turn_new, [a, b], retry_of_turn_id=turn_old)：
     先提交 a（普通绑定，无 await），随后轮到 b 时进入克隆 I/O 并被闸门挡住；
  3) 闸门期间删除 a（第一项）；
  4) 放行闸门，b 的克隆完成，集合提交收尾。
基线：outcome.bound 同时包含 a 与 b 的克隆 —— a 已被删除却仍被报成绑定成功。

运行：cd backend; uv run --frozen pytest -q tests/test_fb_e_r2_set_release_delete.py
"""

from __future__ import annotations

import errno
import shutil
import threading
from pathlib import Path

import pytest

from agent.services import attachments as attachments_mod
from agent.services.attachments import AttachmentService


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture()
def svc(db_conn, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


def _ready(svc: AttachmentService, path: Path):
    return svc.run_prepare(svc.prepare(str(path), topic_id="t1").id)


def _force_copy_fallback(monkeypatch) -> None:
    def refuse_link(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError(errno.EPERM, "hardlink not permitted")

    monkeypatch.setattr(attachments_mod.os, "link", refuse_link)


def _gate_first_copy(monkeypatch):
    """把克隆的文件复制卡在闸门上（只用第 1 次调用，确定性、不 sleep）。"""
    real = shutil.copyfile
    entered = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def stub(src, dst, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls["n"] += 1
        if calls["n"] == 1:
            entered.set()
            assert release.wait(15), "测试闸门没有被放开"
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(attachments_mod.shutil, "copyfile", stub)
    return entered, release, calls


async def test_member_deleted_during_clone_wait_must_not_be_reported_bound(
    svc: AttachmentService, tmp_path: Path, monkeypatch
):
    a = _ready(svc, _write(tmp_path / "a-草稿.bin", b"a" * 4096))
    b = _ready(svc, _write(tmp_path / "b-历史.bin", b"b" * 4096))
    # b 归属旧轮：本轮走「重试复用」克隆路径（那条路径里有 await）
    await svc.bind_for_turn(turn_id="turn_old", attachment_ids=[b.id], topic_id="t1")

    _force_copy_fallback(monkeypatch)
    entered, release, _calls = _gate_first_copy(monkeypatch)

    import asyncio

    task = asyncio.create_task(
        svc.bind_for_turn(
            turn_id="turn_new",
            attachment_ids=[a.id, b.id],
            topic_id="t1",
            retry_of_turn_id="turn_old",
        )
    )
    assert await asyncio.to_thread(entered.wait, 15), "装置：克隆没有进入闸门"

    # 闸门期间：用户删除了集合里的第一项（a）
    assert svc.delete(a.id) is not None, "装置：删除 a 必须成功"

    release.set()
    outcome = await asyncio.wait_for(task, timeout=30)

    bound_ids = [str(item) for item in outcome.bound]
    rejected_ids = [str(item[0]) for item in outcome.rejected]

    # ① 已删除的成员绝不能被报成「已绑定」——回执必须与落库事实一致
    assert a.id not in bound_ids, (
        "已删除的附件仍被集合回执报成绑定成功（缺整组最终复核）",
        {"bound": bound_ids, "rejected": rejected_ids, "a_exists": svc.get(a.id, check=False) is not None},
    )
    # ② 回执里的每个 ID 在收敛时刻都必须仍然存在且归属本轮
    for att_id in bound_ids:
        row = svc.get(att_id, check=False)
        assert row is not None, ("回执里的附件在库里已经不存在", {"bound": bound_ids, "missing": att_id})
        assert str(row.turn_id or "") == "turn_new", (
            "回执里的附件在收敛时刻并不归属本轮",
            {"bound": bound_ids, "turn_id": row.turn_id},
        )
    # ③ 集合级提交：成员在等待期间被删除 → 整组拒绝，不半绑
    assert not (outcome.bound and not outcome.rejected), (
        "集合里有人被删除却仍以「成功」收尾（应当整组拒绝或无已删成员）",
        {"bound": bound_ids, "rejected": rejected_ids},
    )


async def test_clean_set_release_binds_all_members(svc: AttachmentService, tmp_path: Path, monkeypatch):
    """绿守卫：没有删除交错时，重试复用必须整组成功（证明装置可达）。"""
    a = _ready(svc, _write(tmp_path / "a-草稿.bin", b"a" * 4096))
    b = _ready(svc, _write(tmp_path / "b-历史.bin", b"b" * 4096))
    await svc.bind_for_turn(turn_id="turn_old", attachment_ids=[b.id], topic_id="t1")
    _force_copy_fallback(monkeypatch)

    outcome = await svc.bind_for_turn(
        turn_id="turn_new",
        attachment_ids=[a.id, b.id],
        topic_id="t1",
        retry_of_turn_id="turn_old",
    )
    assert outcome.rejected == [], outcome.rejected
    assert len(outcome.bound) == 2, [str(item) for item in outcome.bound]
    assert {str(svc.get(str(item)).turn_id) for item in outcome.bound} == {"turn_new"}
