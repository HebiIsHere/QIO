"""V 组独立验证：R2（引用型附件在最终接受边界必须复核当下文件事实）。

独立装置：受控 asyncio 闸门（把「重试克隆的文件 I/O」停住），在闸门里改动引用源文件。
不使用任何既有 r2-w1 / fb_* / acc_* 用例的断言。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from agent.services import attachments as att_mod
from agent.services.attachments import AttachmentService

TOPIC = "topic-v-r2"
TURN_OLD = "turn-v-r2-old"
TURN_NEW = "turn-v-r2-new"


@pytest.fixture()
def svc(db_conn, tmp_path: Path, monkeypatch):
    # 把阈值收紧：这样小文件也能走「引用本地文件」这条链，无需造 100MB
    monkeypatch.setattr(att_mod, "COPY_MAX_BYTES", 16)
    data = tmp_path / "data"
    data.mkdir()
    return AttachmentService(db_conn, data)


def _mk(tmp_path: Path, name: str, payload: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(payload)
    return p


def _reference_pair(svc: AttachmentService, tmp_path: Path):
    """一条 copy（8 字节）+ 一条 reference（64 字节），都归属 TURN_OLD，均已 ready。"""
    copy_src = _mk(tmp_path, "copy.bin", b"C" * 8)
    ref_src = _mk(tmp_path, "ref.bin", b"R" * 64)
    att_copy = svc.prepare(str(copy_src), topic_id=TOPIC, turn_id=TURN_OLD)
    att_copy = svc.run_prepare(att_copy.id)
    att_ref = svc.prepare(str(ref_src), topic_id=TOPIC, turn_id=TURN_OLD)
    att_ref = svc.run_prepare(att_ref.id)
    assert att_copy.kind == "copy" and att_copy.state == "ready"
    assert att_ref.kind == "reference" and att_ref.state == "ready"
    return att_copy, att_ref, copy_src, ref_src


def _install_clone_gate(svc: AttachmentService):
    """把 copy 克隆的文件 I/O 停住（受控闸门）：返回 (reached, release)。

    闸门覆盖的正是契约说的「克隆/等待期间」——引用型的最终接受边界排在这段等待之后。
    """
    reached = asyncio.Event()
    release = asyncio.Event()
    original = svc._finish_copy_clone

    async def gated(plan):
        reached.set()
        await release.wait()
        return await original(plan)

    svc._finish_copy_clone = gated  # type: ignore[method-assign]
    return reached, release


async def _bind(svc: AttachmentService, ids: list[str]):
    return await svc.bind_for_turn(
        turn_id=TURN_NEW,
        attachment_ids=ids,
        topic_id=TOPIC,
        retry_of_turn_id=TURN_OLD,
    )


def test_v_r2a_reference_source_vanishing_in_wait_is_rejected(svc, tmp_path):
    """契约：引用源在克隆等待期消失 → 最终接受边界结构化拒绝，bound == []。"""
    att_copy, att_ref, _copy_src, ref_src = _reference_pair(svc, tmp_path)
    reached, release = _install_clone_gate(svc)

    async def scenario():
        task = asyncio.create_task(_bind(svc, [att_copy.id, att_ref.id]))
        await asyncio.wait_for(reached.wait(), timeout=10)
        # 等待期间源文件消失
        ref_src.unlink()
        release.set()
        return await asyncio.wait_for(task, timeout=10)

    outcome = asyncio.run(scenario())

    print("bound:", outcome.bound)
    print("rejected:", outcome.rejected)
    assert outcome.bound == [], "源文件消失了却仍然报成功"
    assert [item[0] for item in outcome.rejected] == [att_ref.id]
    assert "源文件" in outcome.rejected[0][1]
    # 拒绝理由必须说清「源文件不在了」
    assert "不在" in outcome.rejected[0][1]
    # 不得留下任何本轮行（克隆必须整轮回滚）
    assert svc.list(turn_id=TURN_NEW) == []
    # 原行归属不变
    assert svc.get(att_copy.id).turn_id == TURN_OLD
    assert svc.get(att_ref.id).turn_id == TURN_OLD
    # 不留无人认领的克隆副本
    clone_orphans = [
        p for p in (svc.root).rglob("*") if p.is_file()
    ]
    assert [p.name for p in clone_orphans] == [
        p.name for p in [svc.copy_path(att_copy)] if p.exists()
    ]


def test_v_r2b_recreated_same_name_file_is_not_the_same_source(svc, tmp_path):
    """契约：删除后重新创建同名文件 ≠ 同一份，不得擅自用同名文件顶替。"""
    att_copy, att_ref, _copy_src, ref_src = _reference_pair(svc, tmp_path)
    recorded_mtime = att_ref.mtime
    reached, release = _install_clone_gate(svc)

    async def scenario():
        task = asyncio.create_task(_bind(svc, [att_copy.id, att_ref.id]))
        await asyncio.wait_for(reached.wait(), timeout=10)
        ref_src.unlink()
        ref_src.write_bytes(b"R" * 64)  # 同名、同大小、新内容/新时间
        os.utime(ref_src, (recorded_mtime + 5.0, recorded_mtime + 5.0))
        release.set()
        return await asyncio.wait_for(task, timeout=10)

    outcome = asyncio.run(scenario())

    print("bound:", outcome.bound)
    print("rejected:", outcome.rejected)
    assert outcome.bound == []
    assert [item[0] for item in outcome.rejected] == [att_ref.id]
    assert svc.list(turn_id=TURN_NEW) == []


def test_v_r2c_intact_reference_clones_normally(svc, tmp_path):
    """对照：源文件一直在 → 正常克隆两条（copy + reference），原行归属不动。"""
    att_copy, att_ref, _copy_src, ref_src = _reference_pair(svc, tmp_path)
    reached, release = _install_clone_gate(svc)

    async def scenario():
        task = asyncio.create_task(_bind(svc, [att_copy.id, att_ref.id]))
        await asyncio.wait_for(reached.wait(), timeout=10)
        assert ref_src.exists()
        release.set()
        return await asyncio.wait_for(task, timeout=10)

    outcome = asyncio.run(scenario())

    print("bound:", outcome.bound)
    bound_ids = set(outcome.bound)
    assert len(bound_ids) == 2, outcome.bound
    rows = svc.list(turn_id=TURN_NEW)
    assert {r.id for r in rows} == bound_ids
    assert {r.kind for r in rows} == {"copy", "reference"}
    # 克隆复用：新 id，指向源行
    by_source = {r.source_attachment_id: r for r in rows}
    assert by_source[att_copy.id].kind == "copy"
    assert by_source[att_ref.id].kind == "reference"
    assert by_source[att_ref.id].state == "ready"
    # 原行归属与历史不动
    assert svc.get(att_copy.id).turn_id == TURN_OLD
    assert svc.get(att_ref.id).turn_id == TURN_OLD


def test_v_r2d_audit_identity_swap_with_same_size_and_mtime_is_accepted(svc, tmp_path):
    """【审计 N2】同名顶替如果**大小与 mtime 都一致**，最终接受边界看不出来。

    引用型的「同源」判据只有 stat（size + mtime）+ 读取探针，没有记录文件身份（inode/NTFS 索引）。
    把一个**不同文件**放到同一路径、把时间戳按登记值恢复，就会通过复核并被当作可用克隆。
    本用例断言**当前行为**（通过），用来把这条残留边界固定成可复现的证据。
    """
    att_copy, att_ref, _copy_src, ref_src = _reference_pair(svc, tmp_path)
    recorded_mtime = float(att_ref.mtime)
    recorded_size = int(att_ref.size_bytes)

    # 先把路径换成另一个文件，再精确恢复登记事实
    ref_src.unlink()
    ref_src.write_bytes(b"X" * recorded_size)
    os.utime(ref_src, (recorded_mtime, recorded_mtime))
    stat_now = ref_src.stat()
    assert int(stat_now.st_size) == recorded_size
    assert abs(float(stat_now.st_mtime) - recorded_mtime) <= 1e-6

    reached, release = _install_clone_gate(svc)

    async def scenario():
        task = asyncio.create_task(_bind(svc, [att_copy.id, att_ref.id]))
        await asyncio.wait_for(reached.wait(), timeout=10)
        release.set()
        return await asyncio.wait_for(task, timeout=10)

    outcome = asyncio.run(scenario())

    print("N2 bound:", outcome.bound)
    print("N2 rejected:", outcome.rejected)
    rows = svc.list(turn_id=TURN_NEW)
    ref_clones = [r for r in rows if r.kind == "reference"]
    print("N2 reference clone rows:", [(r.id, r.state, r.size_bytes) for r in ref_clones])
    # 当前行为：顶替后的「另一份文件」被当成可用引用接受
    assert outcome.rejected == []
    assert len(ref_clones) == 1 and ref_clones[0].state == "ready"


def test_v_r2e_recovery_after_rejection_clones_normally(svc, tmp_path):
    """契约：源文件恢复存在之后可以正常克隆（拒绝不是粘性的）。"""
    att_copy, att_ref, _copy_src, ref_src = _reference_pair(svc, tmp_path)
    recorded = (int(att_ref.size_bytes), float(att_ref.mtime))
    reached, release = _install_clone_gate(svc)

    async def first():
        task = asyncio.create_task(_bind(svc, [att_copy.id, att_ref.id]))
        await asyncio.wait_for(reached.wait(), timeout=10)
        ref_src.unlink()
        release.set()
        return await asyncio.wait_for(task, timeout=10)

    first_outcome = asyncio.run(first())
    assert first_outcome.bound == []
    assert svc.list(turn_id=TURN_NEW) == []

    # 源文件恢复（同一份内容与登记事实）
    ref_src.write_bytes(b"R" * recorded[0])
    os.utime(ref_src, (recorded[1], recorded[1]))

    second_outcome = asyncio.run(_bind(svc, [att_copy.id, att_ref.id]))
    print("recovered bound:", second_outcome.bound)
    print("recovered rejected:", second_outcome.rejected)
    assert second_outcome.rejected == []
    rows = svc.list(turn_id=TURN_NEW)
    assert {r.kind for r in rows} == {"copy", "reference"}
    ref_clone = next(r for r in rows if r.kind == "reference")
    assert ref_clone.state == "ready"
    assert ref_clone.source_attachment_id == att_ref.id
