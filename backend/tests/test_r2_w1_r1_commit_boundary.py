"""R2 轮 W1 / R1：旧准备任务不得覆盖更新的重定位结果（提交边界必须是真串行边界）。

冻结规则（_r2-contracts-20261010.md §5 R1）：
* 提交前的检查（代际 / 操作身份 / 目标文件身份）与最终 os.replace 必须在**同一个**
  有效串行边界内；**旧操作永不覆盖新结果**；最后一次定位必须成功收敛。

本轮在基线上仍可复现的残余缺口（本文件逐条钉住，全部用受控闸门，无 sleep）：

1. **None 身份被整段跳过**：_copy_once / write_upload_stream 的身份核对写成
   -> if target_identity is not None and current_identity != target_identity
   旧操作开始时目标**不存在**（target_identity is None）时，这段核对一次都不跑；
   旧操作随后 os.replace 就把新代际刚提交的副本换掉了。
2. **代际不是一个全序**：浏览器上传路径（write_upload_stream）**完全没有代际**；
   而 copy_to_disk(generation=None) 是契约允许的缺省（"不校验，兼容旧调用"）。
   同一条附件上两个操作因此可以共享同一个（甚至没有）代际 —— 提交边界不能只靠它。
   补齐：准备操作在**开始复制时**领取单调递增的提交票号；提交边界只在
   "自己仍是最新一张票"时才 os.replace；票号随 DiskOutcome 回到 apply_outcome，
   过期结果连行状态都不许写（否则旧操作仍会把新结果的行覆盖成自己的）。

两种交错都覆盖：① 旧操作复制完、提交前，新定位完整提交；② 旧操作先提交（它抢到
目标文件后，新定位会因身份变化被取消 —— 这正是契约描述的"随后新任务因目标身份变化
而取消"）。

运行：cd backend; uv run --frozen --extra dev pytest tests/test_r2_w1_r1_commit_boundary.py -q
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest

from agent.services.attachments import STATE_READY, AttachmentService

B_BYTES = b"B" * 4096
C_BYTES = b"C" * 4096
UP_BYTES = b"U" * 4096


@pytest.fixture()
def svc(db_conn, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parts(root: Path) -> list[Path]:
    return sorted(root.rglob("*.part")) if root.is_dir() else []


def _target(svc: AttachmentService, att_id: str) -> Path:
    return svc.copy_path(svc.get(att_id, check=False))


def _served(svc: AttachmentService, att_id: str) -> bytes:
    """按预测副本路径读磁盘字节（不看 state，用于观察磁盘事实）。"""
    path = _target(svc, att_id)
    return path.read_bytes() if path.exists() else b""


def _relocate_now(svc: AttachmentService, att_id: str, source: Path) -> int:
    """一次完整的重定位：计划 → 复制（含真实 os.replace）→ 落库（新代际）。"""
    svc.plan_relocate(att_id, str(source))
    generation = svc.prepare_generation(att_id)
    outcome = svc.copy_to_disk(svc.get(att_id, check=False), generation=generation)
    assert outcome.state == STATE_READY, outcome
    svc.apply_outcome(att_id, outcome, generation=generation)
    return generation


def _gated_copy(svc: AttachmentService, att, *, generation):
    """把一次准备钉在「复制完、提交边界之前」（产品自带的 on_commit 测试缝）。

    返回 (thread, copied, proceed, sink, errors)。
    """

    def on_commit() -> None:
        copied.set()
        if not proceed.wait(30):
            raise AssertionError("测试闸门没有被放开（30s 超时）")

    sink: list = []
    errors: list = []
    copied = threading.Event()
    proceed = threading.Event()

    def worker() -> None:
        try:
            sink.append(svc.copy_to_disk(att, generation=generation, on_commit=on_commit))
        except BaseException as exc:  # noqa: BLE001 - 线程异常带回主线程断言
            errors.append(exc)

    thread = threading.Thread(target=worker, name="r2-w1-copy", daemon=True)
    thread.start()
    return thread, copied, proceed, sink, errors


def _gated_upload(payload: bytes, split: int = 2048):
    """上传分块闸门：写完第一块就停住（受控，不 sleep）。"""
    entered = threading.Event()
    release = threading.Event()

    def chunks():
        yield payload[:split]
        entered.set()
        if not release.wait(30):
            raise AssertionError("上传闸门没有被放开（30s 超时）")
        yield payload[split:]

    return chunks(), entered, release


def _assert_settled_on_c(svc: AttachmentService, att_id: str, c_source: Path) -> None:
    row = svc.get(att_id, check=False)
    assert row.state == STATE_READY, (
        "最后一次定位没有成功收敛",
        {"state": row.state, "error": row.error, "sha256": row.sha256},
    )
    assert _served(svc, att_id) == C_BYTES, "磁盘字节不是最后一次定位（C）的内容"
    assert row.sha256 == _sha(C_BYTES), "行元数据 sha256 必须是 C"
    assert int(row.size_bytes) == len(C_BYTES), row.size_bytes
    assert str(row.source_path) == str(c_source), row.source_path
    assert _parts(svc.root) == [], "留下了无人认领的 .part 临时文件"


# -- 1. 红：缺省代际（generation=None）的旧准备覆盖更新的重定位 -------------------


def test_unbound_stale_prepare_never_overwrites_newer_relocate(svc, tmp_path):
    """旧操作开始时目标不存在（None 身份）+ 旧调用方没有绑定代际。

    generation=None 是 copy_to_disk 的契约缺省（"不校验，兼容旧调用"）；基线上它
    连身份核对都跳过，于是 os.replace 直接换掉新定位刚提交的副本，随后 apply_outcome
    又把行状态写成旧结果。
    """
    b = _write(tmp_path / "b.bin", B_BYTES)
    c = _write(tmp_path / "c.bin", C_BYTES)
    att = svc.prepare(str(b), name="f.bin", topic_id="t1")
    old_att = svc.get(att.id, check=False)
    assert not _target(svc, att.id).exists(), "装置：旧操作开始时目标必须还不存在"

    worker, copied, proceed, sink, errors = _gated_copy(
        svc, old_att, generation=None
    )
    assert copied.wait(30), "旧任务没有走到提交边界（装置失效）"

    # 旧任务钉在提交边界上：用户改主意重新定位到 C，完整提交
    _relocate_now(svc, att.id, c)
    assert _served(svc, att.id) == C_BYTES, "装置：新定位必须先提交 C"

    proceed.set()
    worker.join(timeout=60)
    assert not worker.is_alive(), "旧任务没有结束"
    assert errors == [], errors

    stale = sink[0]
    svc.apply_outcome(att.id, stale)  # 旧调用方按 generation=None 落库（契约缺省）
    assert stale.state != STATE_READY, (
        "旧操作在提交边界被放行（会覆盖更新的定位结果）",
        {"state": stale.state, "stored_path": stale.stored_path},
    )
    _assert_settled_on_c(svc, att.id, c)


# -- 2. 红：两个交错提交（旧操作先到提交边界） -------------------------------------


def test_older_prepare_that_commits_first_never_wins(svc, tmp_path):
    """两条并发 os.replace 交替：先放行**旧**操作，它绝不能抢在更新操作之前提交。

    基线上旧操作把目标文件抢先写出来，随后新定位因目标身份变化被取消 ——
    契约里那句"随后新任务因目标身份变化而取消"就是这个形状。
    """
    b = _write(tmp_path / "b.bin", B_BYTES)
    c = _write(tmp_path / "c.bin", C_BYTES)
    att = svc.prepare(str(b), name="f.bin", topic_id="t1")
    old_att = svc.get(att.id, check=False)

    old_worker, old_copied, old_proceed, old_sink, old_errors = _gated_copy(
        svc, old_att, generation=None
    )
    assert old_copied.wait(30), "旧任务没有走到提交边界"

    # 新定位：同样钉在提交边界之前（此刻目标仍然不存在）
    svc.plan_relocate(att.id, str(c))
    generation_c = svc.prepare_generation(att.id)
    new_att = svc.get(att.id, check=False)
    new_worker, new_copied, new_proceed, new_sink, new_errors = _gated_copy(
        svc, new_att, generation=generation_c
    )
    assert new_copied.wait(30), "新定位没有走到提交边界"
    assert not _target(svc, att.id).exists(), "装置：两个操作都还没提交"

    # 先放行旧操作：它不得抢在更新操作之前落地
    old_proceed.set()
    old_worker.join(timeout=60)
    assert not old_worker.is_alive(), "旧任务没有结束"
    assert old_errors == [], old_errors
    assert old_sink[0].state != STATE_READY, (
        "旧操作抢在更新操作之前提交了（新定位随后会因目标身份变化被取消）",
        {"state": old_sink[0].state, "stored_path": old_sink[0].stored_path},
    )
    assert _served(svc, att.id) != B_BYTES, "旧内容不应出现在目标文件上"

    # 再放行新定位：它必须照常收敛
    new_proceed.set()
    new_worker.join(timeout=60)
    assert not new_worker.is_alive(), "新定位没有结束"
    assert new_errors == [], new_errors
    assert new_sink[0].state == STATE_READY, new_sink[0]
    svc.apply_outcome(att.id, new_sink[0], generation=generation_c)

    # 迟到的旧结果（generation=None）不得把行状态改回旧结果
    svc.apply_outcome(att.id, old_sink[0])
    _assert_settled_on_c(svc, att.id, c)


# -- 3. 红：浏览器上传路径（完全没有代际）覆盖更新的重定位 -------------------------


def test_stale_browser_upload_never_overwrites_newer_relocate(svc, tmp_path):
    """上传在写第一块之后停住（目标当时不存在）；期间用户重新定位并完整提交。

    write_upload_stream 没有任何代际参数 —— 这是生产路径上真实存在的"旧准备任务"。
    """
    c = _write(tmp_path / "c.bin", C_BYTES)
    att = svc.begin_upload(name="upload.bin", topic_id="t1")
    chunks, entered, release = _gated_upload(UP_BYTES)
    assert not svc.copy_path(att).exists(), "装置：上传开始时目标不存在"

    uploaded: list = []
    errors: list = []

    def worker() -> None:
        try:
            uploaded.append(svc.write_upload_stream(att, chunks))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=worker, name="r2-w1-upload", daemon=True)
    thread.start()
    assert entered.wait(30), "上传没有走到闸门（装置失效）"

    _relocate_now(svc, att.id, c)
    assert _served(svc, att.id) == C_BYTES, "装置：新定位必须先提交 C"

    release.set()
    thread.join(timeout=60)
    assert not thread.is_alive(), "上传没有结束"
    assert errors == [], errors

    stale = uploaded[0]
    svc.apply_outcome(att.id, stale)  # 上传端点落库就是这条路径（没有代际）
    assert stale.state != STATE_READY, (
        "过期上传在提交边界被放行（覆盖了更新的定位结果）",
        {"state": stale.state, "stored_path": stale.stored_path},
    )
    _assert_settled_on_c(svc, att.id, c)


# -- 4. 绿守卫：单个操作照常收敛（提交票号不得把唯一一次准备也挡掉） --------------


def test_single_unbound_prepare_still_commits_ready(svc, tmp_path):
    b = _write(tmp_path / "b.bin", B_BYTES)
    att = svc.prepare(str(b), name="f.bin", topic_id="t1")
    outcome = svc.copy_to_disk(svc.get(att.id, check=False))  # generation 缺省
    assert outcome.state == STATE_READY, outcome
    applied = svc.apply_outcome(att.id, outcome)
    assert applied is not None and applied.state == STATE_READY
    assert _served(svc, att.id) == B_BYTES
    assert applied.sha256 == _sha(B_BYTES)
    assert _parts(svc.root) == []


def test_single_upload_still_converges_ready(svc, tmp_path):
    att = svc.begin_upload(name="u.bin", topic_id="t1")
    outcome = svc.write_upload_stream(att, [UP_BYTES])
    assert outcome.state == STATE_READY, outcome
    applied = svc.apply_outcome(att.id, outcome)
    assert applied is not None and applied.state == STATE_READY
    assert _served(svc, att.id) == UP_BYTES
    assert _parts(svc.root) == []


def test_normal_relocate_sequence_still_converges(svc, tmp_path):
    b = _write(tmp_path / "b.bin", B_BYTES)
    c = _write(tmp_path / "c.bin", C_BYTES)
    att = svc.run_prepare(svc.prepare(str(b), name="f.bin", topic_id="t1").id)
    assert _served(svc, att.id) == B_BYTES
    _relocate_now(svc, att.id, c)
    _assert_settled_on_c(svc, att.id, c)


# -- 5. 绿守卫：删行之后的迟到结果仍走孤儿副本补偿（提交票号不得挡住它） ---------


def test_late_stale_outcome_after_delete_still_cleans_its_own_orphan(svc, tmp_path):
    """旧操作已经提交出副本、更新的一次准备随后开始；用户把这一行移除之后，
    迟到的旧结果仍必须清掉**它自己那一份**无人认领的副本。"""
    b = _write(tmp_path / "b.bin", B_BYTES)
    att = svc.prepare(str(b), name="f.bin", topic_id="t1")
    first = svc.copy_to_disk(svc.get(att.id, check=False))  # 票号 1：提交出 B
    committed = Path(first.stored_path)
    assert committed.read_bytes() == B_BYTES

    svc.cancel(att.id)  # 更新的一次重试：已经开始（票号 2）但立刻被取消
    second = svc.copy_to_disk(svc.get(att.id, check=False))
    assert second.state != STATE_READY, second

    svc.delete(att.id)  # 用户移除了这一行（行里没有 stored_path → 删不掉副本）
    assert committed.exists(), "装置：这份副本此刻仍留在磁盘上"

    assert svc.apply_outcome(att.id, first) is None  # 迟到的旧结果（票号已过期）
    assert not committed.exists(), "迟到结果必须清掉它自己那份无人认领的副本"
    assert b.is_file(), "用户原文件永远不动"


def test_late_stale_outcome_never_deletes_the_newer_copy(svc, tmp_path):
    """同一补偿路径的另一半：目标已经是更新操作的副本时，旧结果不许删它。"""
    b = _write(tmp_path / "b.bin", B_BYTES)
    c = _write(tmp_path / "c.bin", C_BYTES)
    att = svc.prepare(str(b), name="f.bin", topic_id="t1")
    stale = svc.copy_to_disk(svc.get(att.id, check=False))  # 票号 1（结果先不落库）
    target = Path(stale.stored_path)
    assert target.read_bytes() == B_BYTES

    # 更新的定位（票号 2）：把目标换成 C，结果同样先不落库
    svc.plan_relocate(att.id, str(c))
    generation_c = svc.prepare_generation(att.id)
    newer = svc.copy_to_disk(svc.get(att.id, check=False), generation=generation_c)
    assert newer.state == STATE_READY and target.read_bytes() == C_BYTES

    svc.delete(att.id)  # 行被移除（行里没有 stored_path → 副本留在磁盘上）
    svc.apply_outcome(att.id, stale)  # 迟到的旧结果：不得删掉更新操作的那一份
    assert target.read_bytes() == C_BYTES, "旧结果的补偿删掉了更新操作的副本"
    assert _parts(svc.root) == []
    assert b.is_file() and c.is_file()
