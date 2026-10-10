"""ACC-E / F24 反例：缺失副本重试时，后端不得把「已受理且正在准备」呈现为最终失败。

用户可见规则（契约 C4/C6）：
* 副本 missing、但可从原路径恢复时，重试受理后必须能看出「正在准备」；
* 只报 state=missing 会让界面把它当永久失败并停止等待，复制完成后 UI 不再更新；
* 终态（ready/failed/cancelled/missing）必须以 preparing=false 表达；
* 旧代际的迟到结果不得把新代际的在飞状态抹掉。

运行：cd backend; uv run --frozen --extra dev pytest tests/test_acc_e_f24_preparing_semantics.py -q
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


def test_accepted_retry_reports_preparing_not_final_missing(svc: AttachmentService, tmp_path: Path) -> None:
    src = _write(tmp_path / "orig.bin", b"X" * 512)
    att = svc.prepare(str(src), name="orig.bin", topic_id="t1")
    svc.run_prepare(att.id)
    first = svc.get(att.id)
    assert first.state == STATE_READY, f"首次准备失败：{first.state} / {first.error}"

    # 副本丢失（源文件也暂时不可用）：行会如实变成 missing
    Path(svc.get(att.id).stored_path).unlink()
    missing = svc.get(att.id)
    assert missing.state == "missing"
    before = svc.payload(missing, check=False)
    assert before["preparing"] is False
    assert before["phase"] == "settled"

    # 用户点了重试：受理后立刻登记在飞（真实 API 路由也是这个顺序）
    svc.mark_preparing_for_retry(att.id)
    pending = svc.payload(svc.get(att.id, check=False), check=False)
    assert pending["preparing"] is True, "重试被受理后必须能看出正在准备"
    assert pending["phase"] == "preparing"
    # state 保持历史事实（missing），由 preparing 表达在飞 —— 不伪造 ready
    assert pending["state"] == "missing"
    assert pending["retryable"] is True

    # 后台完成：终态表达为 preparing=false 且 state=ready
    generation = svc.prepare_generation(att.id)
    outcome = svc.copy_to_disk(svc.get(att.id), generation=generation)
    assert outcome.state == STATE_READY
    svc.apply_outcome(att.id, outcome, generation=generation)
    settled = svc.payload(svc.get(att.id), check=False)
    assert settled["preparing"] is False
    assert settled["phase"] == "settled"
    assert settled["state"] == STATE_READY


def test_stale_generation_does_not_clear_new_preparing(svc: AttachmentService, tmp_path: Path) -> None:
    src = _write(tmp_path / "o.bin", b"Y" * 256)
    att = svc.prepare(str(src), name="o.bin", topic_id="t1")
    svc.run_prepare(att.id)
    Path(svc.get(att.id).stored_path).unlink()

    svc.mark_preparing_for_retry(att.id)
    old_gen = svc.prepare_generation(att.id)
    svc.mark_preparing_for_retry(att.id)  # 第二次重试：新代际
    new_gen = svc.prepare_generation(att.id)
    assert new_gen > old_gen

    from agent.services.attachments import DiskOutcome, STATE_FAILED

    svc.apply_outcome(
        att.id,
        DiskOutcome(state=STATE_FAILED, error="旧代际的失败"),
        generation=old_gen,
    )
    assert svc.is_preparing(att.id), "旧代际的失败不得清掉新代际的在飞登记"
    assert svc.payload(svc.get(att.id, check=False), check=False)["preparing"] is True
