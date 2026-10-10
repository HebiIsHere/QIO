"""R2 轮 W1 / R2：引用附件在克隆/等待期间消失，不得再被当成已绑定。

冻结规则（_r2-contracts-20261010.md §5 R2）：
* 引用附件通过**初步复核**之后，克隆/等待期间源文件消失，最终绑定不得报成功；
* 必须在**最终接受边界**重新核实当前文件事实（存在 + 可读 + 与登记时同源）；
  缺失 / 不可读 / 被同名文件顶替时**结构化拒绝**，不得产出看似可用的克隆行；
* 仍要保留既有语义：**初步复核时就已经缺失**的历史引用按 F18 如实降级（missing 行），
  纯文字发送不受历史失败附件阻断。

基线缺口（本文件钉住的形状）：
* 引用型的落盘克隆（_clone_reference_for_retry）是同步的，但**同一批**里前一条的
  重试克隆要 await 文件 I/O（asyncio.to_thread）—— 这段时间里源文件消失/被顶替，
  引用这一条仍然按旧事实建行；放行前那次整组复核（_reverify_committed）对
  reference 直接 return None，不看源文件。于是回执 bound 非空、界面显示可用。
* 两条顺序都覆盖：引用在前（等待发生在它自己克隆之后）与引用在后（等待发生在它
  自己克隆之前）。

运行：cd backend; uv run --frozen --extra dev pytest tests/test_r2_w1_r2_reference_final_acceptance.py -q
"""

from __future__ import annotations

import asyncio
import errno
import shutil
import threading
from pathlib import Path

import pytest

from agent.services import attachments as attachments_mod
from agent.services.attachments import STATE_READY, AttachmentService

REF_BYTES = b"R" * 512
SUBSTITUTE_BYTES = b"X" * 300  # 同名但**不是同一份**（大小都不一样）
COPY_BYTES = b"C" * 32


@pytest.fixture()
def svc(db_conn, tmp_path: Path, monkeypatch) -> AttachmentService:
    # 阈值收紧成 64 字节：512 字节的源走"引用本地文件"，32 字节的走"已保存副本"
    monkeypatch.setattr(attachments_mod, "COPY_MAX_BYTES", 64)
    return AttachmentService(db_conn, tmp_path / "data")


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _reference(svc: AttachmentService, path: Path, topic: str = "t1"):
    att = svc.prepare(str(path), topic_id=topic)
    assert att.kind == "reference", att.kind
    att = svc.run_prepare(att.id)
    assert att.state == STATE_READY, (att.state, att.error)
    return att


def _ready_copy(svc: AttachmentService, path: Path, topic: str = "t1"):
    att = svc.prepare(str(path), topic_id=topic)
    assert att.kind == "copy", att.kind
    att = svc.run_prepare(att.id)
    assert att.state == STATE_READY, (att.state, att.error)
    return att


def _force_copy_fallback(monkeypatch) -> None:
    """强制走 shutil.copyfile 退路（硬链接成功的话就没有可下闸门的缝）。"""

    def refuse_link(*args, **kwargs):
        raise OSError(errno.EPERM, "hardlink not permitted")

    monkeypatch.setattr(attachments_mod.os, "link", refuse_link)


def _gated_copyfile(monkeypatch):
    """把克隆的文件 I/O 卡在闸门上：返回 (entered, release)。"""
    real = shutil.copyfile
    entered = threading.Event()
    release = threading.Event()

    def gated(src_path, dst_path, *args, **kwargs):
        entered.set()
        if not release.wait(30):
            raise AssertionError("测试闸门没有被放开（30s 超时）")
        return real(src_path, dst_path, *args, **kwargs)

    monkeypatch.setattr(attachments_mod.shutil, "copyfile", gated)
    return entered, release


async def _wait_entered(entered) -> None:
    assert await asyncio.to_thread(entered.wait, 30), "克隆没有进入文件 I/O 闸门"


def _orphans(data_dir: Path, keep: list[str | None]) -> list[Path]:
    keep_set = {str(Path(p)) for p in keep if p}
    return [p for p in data_dir.rglob("*") if p.is_file() and str(p) not in keep_set]


def _assert_group_rejected(outcome, svc, *, ref_id: str, copy_id: str) -> None:
    assert outcome.bound == [], (
        "引用源文件已经不在/被顶替，仍产出了绑定回执",
        outcome.as_receipt(),
    )
    assert list(outcome) == [], [getattr(a, "id", a) for a in outcome]
    assert outcome.rejected, outcome.as_receipt()
    reasons = [reason for _item, reason in outcome.rejected]
    assert any("源文件" in reason for reason in reasons), reasons
    assert svc.list(turn_id="turn_new", check=False) == [], "被拒的重试不得留下任何新行"
    assert svc.get(ref_id).turn_id == "turn_old", "原行归属不得被改写"
    assert svc.get(copy_id).turn_id == "turn_old", "原行归属不得被改写"


# -- 1. 红：引用在前（它自己已经克隆完），等待期间源文件消失 ----------------------


async def test_reference_that_vanishes_during_clone_wait_rejects_group(
    svc, tmp_path, monkeypatch
):
    """引用先建好 ready 克隆行；前一条小副本的克隆被闸门卡住时删掉引用源文件。

    放行前那次整组复核必须按当下事实发现"源文件不在了"，整轮拒绝。
    """
    ref_source = _write(tmp_path / "引用大文件.bin", REF_BYTES)
    ref = _reference(svc, ref_source)
    small = _ready_copy(svc, _write(tmp_path / "小副本.bin", COPY_BYTES))
    await svc.bind_for_turn(
        turn_id="turn_old", attachment_ids=[ref.id, small.id], topic_id="t1"
    )

    _force_copy_fallback(monkeypatch)
    entered, release = _gated_copyfile(monkeypatch)
    task = asyncio.create_task(
        svc.bind_for_turn(
            turn_id="turn_new",
            attachment_ids=[ref.id, small.id],
            topic_id="t1",
            retry_of_turn_id="turn_old",
        )
    )
    await _wait_entered(entered)  # 引用的克隆已经完成；小副本正卡在文件 I/O

    ref_source.unlink()  # 等待期间源文件消失

    release.set()
    outcome = await asyncio.wait_for(task, timeout=30)

    _assert_group_rejected(outcome, svc, ref_id=ref.id, copy_id=small.id)
    assert _orphans(tmp_path / "data", [small.stored_path]) == [], (
        "整轮被拒后不得留下克隆资产",
        [str(p) for p in _orphans(tmp_path / "data", [small.stored_path])],
    )


# -- 2. 红：引用在后（等待发生在它自己克隆之前） ----------------------------------


async def test_reference_that_vanishes_before_its_own_clone_rejects_group(
    svc, tmp_path, monkeypatch
):
    """小副本的克隆被闸门卡住时删掉引用源文件；引用轮的克隆在这之后才跑。"""
    ref_source = _write(tmp_path / "引用大文件2.bin", REF_BYTES)
    ref = _reference(svc, ref_source)
    small = _ready_copy(svc, _write(tmp_path / "小副本2.bin", COPY_BYTES))
    await svc.bind_for_turn(
        turn_id="turn_old", attachment_ids=[small.id, ref.id], topic_id="t1"
    )

    _force_copy_fallback(monkeypatch)
    entered, release = _gated_copyfile(monkeypatch)
    task = asyncio.create_task(
        svc.bind_for_turn(
            turn_id="turn_new",
            attachment_ids=[small.id, ref.id],
            topic_id="t1",
            retry_of_turn_id="turn_old",
        )
    )
    await _wait_entered(entered)  # 小副本在克隆；引用还没轮到

    ref_source.unlink()

    release.set()
    outcome = await asyncio.wait_for(task, timeout=30)

    _assert_group_rejected(outcome, svc, ref_id=ref.id, copy_id=small.id)
    assert _orphans(tmp_path / "data", [small.stored_path]) == [], (
        "整轮被拒后不得留下克隆资产",
        [str(p) for p in _orphans(tmp_path / "data", [small.stored_path])],
    )


# -- 3. 红：删除后用同名文件顶替（不是同一份） ------------------------------------


async def test_recreated_same_name_file_never_substitutes(svc, tmp_path, monkeypatch):
    """删掉源文件、再写一个同名文件 —— 不得擅自把它当成原来的那一份。"""
    ref_source = _write(tmp_path / "会被顶替.bin", REF_BYTES)
    ref = _reference(svc, ref_source)
    small = _ready_copy(svc, _write(tmp_path / "小副本3.bin", COPY_BYTES))
    await svc.bind_for_turn(
        turn_id="turn_old", attachment_ids=[small.id, ref.id], topic_id="t1"
    )

    _force_copy_fallback(monkeypatch)
    entered, release = _gated_copyfile(monkeypatch)
    task = asyncio.create_task(
        svc.bind_for_turn(
            turn_id="turn_new",
            attachment_ids=[small.id, ref.id],
            topic_id="t1",
            retry_of_turn_id="turn_old",
        )
    )
    await _wait_entered(entered)

    ref_source.unlink()
    _write(ref_source, SUBSTITUTE_BYTES)  # 同名，但不是同一份

    release.set()
    outcome = await asyncio.wait_for(task, timeout=30)

    _assert_group_rejected(outcome, svc, ref_id=ref.id, copy_id=small.id)
    assert svc.get(ref.id).source_path == str(ref_source), "原行位置不得被改写"


# -- 4. 绿守卫：源文件恢复存在（同一份文件）之后，同一条重试必须能正常克隆 --------


async def test_reference_retry_works_after_source_restored(svc, tmp_path, monkeypatch):
    ref_source = _write(tmp_path / "会搬走再搬回.bin", REF_BYTES)
    ref = _reference(svc, ref_source)
    small = _ready_copy(svc, _write(tmp_path / "小副本4.bin", COPY_BYTES))
    await svc.bind_for_turn(
        turn_id="turn_old", attachment_ids=[small.id, ref.id], topic_id="t1"
    )

    hidden = tmp_path / "搬走.bin"

    _force_copy_fallback(monkeypatch)
    entered, release = _gated_copyfile(monkeypatch)
    task = asyncio.create_task(
        svc.bind_for_turn(
            turn_id="turn_new",
            attachment_ids=[small.id, ref.id],
            topic_id="t1",
            retry_of_turn_id="turn_old",
        )
    )
    await _wait_entered(entered)

    ref_source.replace(hidden)  # 等待期间"不在了"（同一份文件被搬走）

    release.set()
    rejected = await asyncio.wait_for(task, timeout=30)
    _assert_group_rejected(rejected, svc, ref_id=ref.id, copy_id=small.id)

    hidden.replace(ref_source)  # 恢复存在（同一份文件、同样的 size/mtime）

    again = await svc.bind_for_turn(
        turn_id="turn_new",
        attachment_ids=[small.id, ref.id],
        topic_id="t1",
        retry_of_turn_id="turn_old",
    )
    assert again.rejected == [], again.rejected
    assert len(again.bound) == 2, again.as_receipt()
    clones = [svc.get(item) for item in again.bound]
    by_source = {clone.source_attachment_id: clone for clone in clones}
    assert set(by_source) == {ref.id, small.id}, by_source
    assert by_source[ref.id].state == STATE_READY, by_source[ref.id]
    assert by_source[ref.id].source_path == str(ref_source)


# -- 5. 绿守卫：F18 既有降级语义不得被 R2 收紧 ------------------------------------


async def test_history_missing_reference_still_degrades_as_before(svc, tmp_path):
    """**初步复核时就已经缺失**的历史引用：仍按 F18 如实登记 missing 新行。"""
    ref_source = _write(tmp_path / "历史就缺失.bin", REF_BYTES)
    ref = _reference(svc, ref_source)
    await svc.bind_for_turn(turn_id="turn_old", attachment_ids=[ref.id], topic_id="t1")
    ref_source.unlink()  # 进入 bind 之前就已经不在了

    outcome = await svc.bind_for_turn(
        turn_id="turn_new",
        attachment_ids=[ref.id],
        topic_id="t1",
        retry_of_turn_id="turn_old",
    )
    assert len(outcome.bound) == 1, outcome.as_receipt()
    clone = svc.get(outcome.bound[0])
    assert clone.kind == "reference"
    assert clone.state == "missing", "缺失引用不得被克隆成可用（ready）新附件"
    assert svc.get(ref.id).turn_id == "turn_old"


async def test_healthy_reference_retry_still_clones_ready(svc, tmp_path):
    ref_source = _write(tmp_path / "健康引用.bin", REF_BYTES)
    ref = _reference(svc, ref_source)
    await svc.bind_for_turn(turn_id="turn_old", attachment_ids=[ref.id], topic_id="t1")

    outcome = await svc.bind_for_turn(
        turn_id="turn_new",
        attachment_ids=[ref.id],
        topic_id="t1",
        retry_of_turn_id="turn_old",
    )
    assert outcome.rejected == [], outcome.rejected
    clone = svc.get(outcome.bound[0])
    assert clone.state == STATE_READY and clone.source_path == str(ref_source)
