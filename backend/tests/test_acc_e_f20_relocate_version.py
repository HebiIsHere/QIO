"""ACC-E / F20 反例：重新定位的旧后台结果覆盖较新的定位内容。

用户可见规则（契约 C6）：
* 同一条附件先后被定位到 B、再定位到 C 时，**最新一次定位**的内容与元数据必须胜出；
* 旧代际（B）即使晚到，也不得更新数据库、不得覆盖磁盘副本、不得清掉新代际的在飞登记
  （否则等新代际的人会被过期结果提前放醒）；
* 旧代际已写出的临时文件必须清理，不留无人认领的 .part。

反例构造：先 plan_relocate(B) 再 plan_relocate(C)，用旧代际的拷贝结果晚到的顺序调用
copy_to_disk/apply_outcome，断言落库与磁盘内容仍对应 C。

运行：cd backend; uv run --frozen --extra dev pytest tests/test_acc_e_f20_relocate_version.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.services.attachments import AttachmentService, STATE_READY


@pytest.fixture()
def svc(db_conn, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _content(svc: AttachmentService, att_id: str) -> bytes:
    path, _name = svc.content_target(att_id)
    return Path(path).read_bytes()


def test_stale_relocate_outcome_is_discarded(svc: AttachmentService, tmp_path: Path) -> None:
    a = _write(tmp_path / "a.bin", b"A" * 2048)
    b = _write(tmp_path / "b.bin", b"B" * 4096)
    c = _write(tmp_path / "c.bin", b"C" * 8192)

    att = svc.prepare(str(a), name="a.bin", topic_id="t1")
    svc.run_prepare(att.id)
    assert _content(svc, att.id) == b"A" * 2048

    # 第一次定位：B（旧代际）
    att_b = svc.plan_relocate(att.id, str(b))
    gen_b = svc.prepare_generation(att.id)
    # 第二次定位：C（新代际）—— 用户改了主意，C 才是最终事实
    svc.plan_relocate(att.id, str(c))
    gen_c = svc.prepare_generation(att.id)
    assert gen_c > gen_b, "新一次定位必须产生更新的代际"
    assert svc.is_preparing(att.id), "定位之后到落库之前必须处于在飞状态"

    # 旧代际的复制结果（晚到）：必须在提交前被作废
    stale = svc.copy_to_disk(att_b, generation=gen_b)
    assert stale.state != STATE_READY, "旧代际不得产出 ready 结果"
    # 旧代际落库：直接丢弃，且不得清掉新代际的在飞登记
    svc.apply_outcome(att.id, stale, generation=gen_b)
    assert svc.is_preparing(att.id), "旧代际的落库不得清掉新代际的在飞登记"
    current = svc.get(att.id)
    assert current is not None
    assert current.source_path == str(c), "元数据必须保持最新一次定位（C）"
    assert int(current.size_bytes) == 8192

    # 新代际完成：内容与元数据都必须对应 C
    fresh_outcome = svc.copy_to_disk(svc.get(att.id), generation=gen_c)
    applied = svc.apply_outcome(att.id, fresh_outcome, generation=gen_c)
    assert applied is not None and applied.state == STATE_READY
    assert _content(svc, att.id) == b"C" * 8192
    assert not svc.is_preparing(att.id)


def test_stale_outcome_never_touches_disk_target(svc: AttachmentService, tmp_path: Path) -> None:
    """旧代际落库时**绝不能删同名目标文件** —— 那会删掉新代际刚提交的副本。"""
    a = _write(tmp_path / "a.bin", b"A" * 1024)
    b = _write(tmp_path / "b.bin", b"B" * 1024)
    c = _write(tmp_path / "c.bin", b"C" * 1024)

    att = svc.prepare(str(a), name="a.bin", topic_id="t1")
    svc.run_prepare(att.id)
    att_b = svc.plan_relocate(att.id, str(b))
    gen_b = svc.prepare_generation(att.id)
    svc.plan_relocate(att.id, str(c))
    gen_c = svc.prepare_generation(att.id)

    # 新代际先完成并落库
    svc.apply_outcome(att.id, svc.copy_to_disk(svc.get(att.id), generation=gen_c), generation=gen_c)
    assert _content(svc, att.id) == b"C" * 1024

    # 旧代际随后带着一个「ready + stored_path」的结果到（模拟它已经写了自己的副本）
    from agent.services.attachments import DiskOutcome

    target, _ = svc.content_target(att.id)
    svc.apply_outcome(
        att.id,
        DiskOutcome(state=STATE_READY, error=None, stored_path=str(target), size_bytes=1024, mtime=None),
        generation=gen_b,
    )
    assert _content(svc, att.id) == b"C" * 1024, "旧代际不得覆盖/删除新代际的副本"
    assert svc.get(att.id).state == STATE_READY
