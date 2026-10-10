"""最终边界 R1（P1）：旧重新定位任务用**公共临时名 + 检查/提交之间的窗口**破坏最新副本。

用户可见不变量（Lead 冻结：最后一次定位必须成功，不许被旧任务毁掉）：
* 同一条附件先后被定位到 B、再定位到 C 时，收敛后必须是
  **state=ready、磁盘完整字节 == C、sha256 对应 C、无任何 .part 残留**；
* 旧代际（B）不得让最新一次定位失败，也不得在提交前/清理阶段覆盖或删除新代际
  （C）刚提交的正式副本，更不得把状态改回旧结果；
* 每个操作/代际必须用自己的**独立临时文件**（绝不共用同名 .part，绝不共用句柄）：
  旧操作只清理自己的资产，既不写坏/删除新代际的临时文件，也不留自己的 .part；
* 取消 / 删除 / 重试 / 重新定位共享同一套保护；阻塞 I/O 不进事件循环，
  提交边界的锁不得覆盖整段复制。

基线实测（fb-e 独立复现 + 本文件复现，2026-10-10，确定性交错）：
旧任务按同名规则打开 `<目标>.part` 后卡在**源读取**；用户改主意定位到 C，C 要写**同一个**
.part —— 在 Windows 上直接以 EACCES/WinError 32 失败（或被旧任务句柄挡住 os.replace），
于是**最新一次定位变成 failed**：元数据 source_path=C，磁盘仍是上一份 A。
另一种交错（旧任务已经复制完、只差提交）下，旧任务会把新代际刚提交的 C 换成 B。
本文件两种交错都覆盖。

反例装置（确定性，不 sleep 碰运气）：
* 真实文件（A/B/C 同长度、不同字节）、真实 AttachmentService、真实两线程；
* 只用**确定性闸门**：① 卡住旧任务对 B 源的第一次 read（旧任务已打开自己的 .part）；
  ② 用产品提供的 on_commit 测试缝把旧任务钉在「复制完、提交前」；
* 新代际在旧任务被钉住期间完整提交 C，然后才放行旧任务。

运行：cd backend; uv run --frozen pytest -q -p no:warnings tests/test_fb_a_r1_stale_relocate_overwrite.py
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

import pytest

from agent.services import attachments as attachments_mod
from agent.services.attachments import STATE_READY, AttachmentService

A_BYTES = b"A" * 4096
B_BYTES = b"B" * 4096
C_BYTES = b"C" * 4096


@pytest.fixture()
def svc(db_conn, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _parts(root: Path) -> list[Path]:
    return sorted(root.rglob("*.part")) if root.is_dir() else []


def _row(svc: AttachmentService, att_id: str) -> dict:
    return svc.payload(svc.get(att_id, check=False), check=False)


def _gate_source_read(source: Path, monkeypatch):
    """只对**这一份源文件**的读设闸门：旧任务打开自己的 .part、真正 read 时被钉住。

    这正是基线事故的形状：旧任务已经按**同名规则**打开了 <目标>.part，
    新代际要写同一个 .part → Windows 上拿不到（EACCES / WinError 32）而失败。
    返回 (entered, release)。
    """
    import builtins

    entered = threading.Event()
    release = threading.Event()
    real_open = builtins.open

    def gated_open(file, mode="r", *args, **kwargs):
        handle = real_open(file, mode, *args, **kwargs)
        try:
            same = Path(str(file)) == source
        except (TypeError, ValueError):
            same = False
        if same and "r" in str(mode):
            original_read = handle.read

            def read(*read_args, **read_kwargs):
                entered.set()
                assert release.wait(30), "测试闸门没有被放开（30s 超时）"
                return original_read(*read_args, **read_kwargs)

            handle.read = read
        return handle

    monkeypatch.setattr(builtins, "open", gated_open)
    return entered, release


def _final_bytes(svc: AttachmentService, att_id: str) -> bytes:
    """走唯一取路径入口（content_target）读最终副本。"""
    path, _name = svc.content_target(att_id)
    return Path(path).read_bytes()


def _served_bytes(svc: AttachmentService, att_id: str) -> bytes:
    """按预测副本路径读磁盘字节（不受 state 影响，用于观察磁盘事实）。"""
    path = svc.copy_path(svc.get(att_id, check=False))
    return path.read_bytes() if path.exists() else b""


def _assert_settled_on_c(svc: AttachmentService, att_id: str, c_source: Path) -> None:
    """冻结不变量：最后一次定位必须成功 ready，磁盘/元数据必须都是 C，且无 .part 残留。"""
    row = _row(svc, att_id)
    served = _served_bytes(svc, att_id)
    assert row.get("state") == STATE_READY, (
        "最后一次定位没有成功收敛（旧任务把它打失败 / 把状态改回旧结果）",
        {"state": row.get("state"), "error": row.get("error"), "source_path": row.get("source_path")},
    )
    assert served == C_BYTES, (
        "磁盘完整字节不是最后一次定位（C）的内容",
        {"first_byte": served[:1], "len": len(served), "row": row},
    )
    assert _final_bytes(svc, att_id) == C_BYTES, "content_target 读到的字节必须是 C"
    assert str(row.get("source_path")) == str(c_source), row
    assert int(row.get("size_bytes") or 0) == len(C_BYTES), row
    assert row.get("sha256") == hashlib.sha256(C_BYTES).hexdigest(), (
        "元数据 sha256 必须对应最新来源 C（元数据与磁盘不得自相矛盾）",
        row.get("sha256"),
    )


def _stale_commit_race(
    svc: AttachmentService,
    att_id: str,
    *,
    b_source: Path,
    c_source: Path,
    stale_att,
    stale_gen: int,
    outcome_sink: list,
    error_sink: list,
):
    """把旧任务钉在「复制完、提交前」；新代际完整提交 C 之后才放行。

    返回 threading.Thread（已启动，调用方负责 join）。
    """
    copied = threading.Event()
    proceed = threading.Event()
    chunk = len(B_BYTES)

    def on_commit() -> None:
        # 产品提供的测试缝：此刻旧任务**字节已经复制完、句柄已经关闭**，
        # 只剩「再校验代际 + 提交」这一小段（R1 的窗口形状）。
        copied.set()
        if not proceed.wait(30):
            raise AssertionError("测试闸门没有被放开（30s 超时）")

    def _worker() -> None:
        try:
            outcome_sink.append(
                svc.copy_to_disk(
                    stale_att,
                    generation=stale_gen,
                    chunk_size=chunk,
                    on_commit=on_commit,
                )
            )
        except BaseException as exc:  # noqa: BLE001 - 线程异常带回主线程断言
            error_sink.append(exc)

    thread = threading.Thread(target=_worker, name="fb-a-stale-copy", daemon=True)
    thread.start()
    assert copied.wait(30), ("旧任务没有走到提交边界（装置失效）", [repr(item) for item in error_sink])
    return thread, proceed


def _commit_c(svc: AttachmentService, att_id: str, c_source: Path) -> None:
    """新代际（用户改主意定位到 C）完整提交：计划 → 复制（含真实 os.replace）→ 落库。"""
    svc.plan_relocate(att_id, str(c_source))
    gen = svc.prepare_generation(att_id)
    outcome = svc.copy_to_disk(svc.get(att_id), generation=gen)
    assert outcome.state == STATE_READY, outcome
    svc.apply_outcome(att_id, outcome, generation=gen)


# -- 1. 红：旧任务复制完 B、提交前，新代际已经提交 C -------------------------------


def test_stale_task_never_overwrites_the_latest_committed_copy(
    svc: AttachmentService, tmp_path: Path
):
    a = _write(tmp_path / "a.bin", A_BYTES)
    b = _write(tmp_path / "b.bin", B_BYTES)
    c = _write(tmp_path / "c.bin", C_BYTES)

    att = svc.run_prepare(svc.prepare(str(a), name="a.bin", topic_id="t1").id)
    assert _final_bytes(svc, att.id) == A_BYTES

    # 旧代际：定位到 B
    att_b = svc.plan_relocate(att.id, str(b))
    gen_b = svc.prepare_generation(att.id)

    stale_outcome: list = []
    errors: list = []
    thread, proceed = _stale_commit_race(
        svc,
        att.id,
        b_source=b,
        c_source=c,
        stale_att=att_b,
        stale_gen=gen_b,
        outcome_sink=stale_outcome,
        error_sink=errors,
    )

    # 旧任务此刻「已经复制完 B、最后一道代际检查还没跑」：新代际完整提交 C
    _commit_c(svc, att.id, c)
    gen_c = svc.prepare_generation(att.id)
    assert gen_c > gen_b, "第二次定位必须产生更新的代际"
    assert _final_bytes(svc, att.id) == C_BYTES, "新代际必须先提交 C"

    proceed.set()
    thread.join(timeout=60)
    assert not thread.is_alive(), "旧任务没有结束"
    assert errors == [], errors

    stale = stale_outcome[0]
    assert stale.state != STATE_READY, (
        "旧代际在提交前已被取代，却仍产出 ready（旧任务会替换掉新代际的文件）",
        {"state": stale.state, "stored_path": stale.stored_path},
    )
    svc.apply_outcome(att.id, stale, generation=gen_b)  # 旧结果落库：必须被丢弃

    _assert_settled_on_c(svc, att.id, c)
    assert _parts(svc.root) == [], "留下了无人认领的 .part 临时文件"
    assert not svc.is_preparing(att.id), "落定之后不得还挂着在飞准备"


# -- 1b. 旧任务卡在源读取（已打开同名 .part）时，最新一次定位必须照常成功 ----------


def test_newer_relocate_succeeds_while_stale_task_holds_shared_temp(
    svc: AttachmentService, tmp_path: Path, monkeypatch
):
    """基线实测形状：旧任务打开 <目标>.part 后卡在读 B 源；C 要写同一个 .part → failed。"""
    a = _write(tmp_path / "a.bin", A_BYTES)
    b = _write(tmp_path / "b.bin", B_BYTES)
    c = _write(tmp_path / "c.bin", C_BYTES)

    att = svc.run_prepare(svc.prepare(str(a), name="same.bin", topic_id="t1").id)
    assert _served_bytes(svc, att.id) == A_BYTES

    att_b = svc.plan_relocate(att.id, str(b))
    gen_b = svc.prepare_generation(att.id)

    entered, release = _gate_source_read(b, monkeypatch)
    stale: dict = {}
    errors: list = []

    def _run_stale() -> None:
        try:
            stale["outcome"] = svc.copy_to_disk(att_b, generation=gen_b)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    worker = threading.Thread(target=_run_stale, name="fb-a-stale-read", daemon=True)
    worker.start()
    assert entered.wait(30), "旧任务没有进入源读取（装置失效）"

    # 旧任务仍卡着（它的 .part 句柄开着）：用户改主意定位到 C，必须成功收敛
    _commit_c(svc, att.id, c)
    gen_c = svc.prepare_generation(att.id)
    assert gen_c > gen_b

    release.set()
    worker.join(timeout=60)
    assert not worker.is_alive(), "旧任务没有结束"
    assert errors == [], errors
    stale_outcome = stale.get("outcome")
    assert stale_outcome is not None
    assert stale_outcome.state != STATE_READY, (
        "旧代际被取代后仍产出 ready",
        {"state": stale_outcome.state, "stored_path": stale_outcome.stored_path},
    )
    svc.apply_outcome(att.id, stale_outcome, generation=gen_b)

    _assert_settled_on_c(svc, att.id, c)
    assert _parts(svc.root) == [], "留下了无人认领的 .part 临时文件"
    assert not svc.is_preparing(att.id)


# -- 1c. 补偿：文件已经提交、落库失败 → 回滚本操作副本、不误删新代际 ---------------


def test_db_write_failure_after_commit_rolls_back_own_copy(svc, tmp_path, monkeypatch):
    """「文件已提交但落库失败」必须补偿：清掉**本操作**刚写的副本，行留 failed（可重试）。"""
    a = _write(tmp_path / "a.bin", A_BYTES)
    c = _write(tmp_path / "c.bin", C_BYTES)
    att = svc.run_prepare(svc.prepare(str(a), name="a.bin", topic_id="t1").id)

    svc.plan_relocate(att.id, str(c))
    gen = svc.prepare_generation(att.id)
    outcome = svc.copy_to_disk(svc.get(att.id), generation=gen)
    assert outcome.state == STATE_READY
    committed = Path(outcome.stored_path)
    assert committed.read_bytes() == C_BYTES

    real_update = svc._update

    def broken_update(attachment_id, **fields):
        if fields.get("state") == STATE_READY:
            raise RuntimeError("落库失败（受控：模拟数据库错误）")
        return real_update(attachment_id, **fields)

    monkeypatch.setattr(svc, "_update", broken_update)
    with pytest.raises(RuntimeError):
        svc.apply_outcome(att.id, outcome, generation=gen)
    monkeypatch.setattr(svc, "_update", real_update)

    row = svc.get(att.id, check=False)
    assert row.state == "failed", row.state
    assert committed.exists() is False, "落库失败必须回滚本操作刚提交的副本"
    assert _parts(svc.root) == []
    assert not svc.is_preparing(att.id), "落库失败也必须注销在飞登记（不让人干等）"


def test_db_write_failure_never_deletes_newer_generation_copy(svc, tmp_path, monkeypatch):
    """同类补偿的另一半：目标已经被**新代际**换成新文件时，旧操作的补偿不得删它。"""
    a = _write(tmp_path / "a.bin", A_BYTES)
    b = _write(tmp_path / "b.bin", B_BYTES)
    c = _write(tmp_path / "c.bin", C_BYTES)
    att = svc.run_prepare(svc.prepare(str(a), name="a.bin", topic_id="t1").id)

    # 旧代际的磁盘结果（B），但先不落库
    att_b = svc.plan_relocate(att.id, str(b))
    gen_b = svc.prepare_generation(att.id)
    stale = svc.copy_to_disk(att_b, generation=gen_b)
    assert stale.state == STATE_READY

    # 新代际完整提交 C（用户改主意）
    _commit_c(svc, att.id, c)
    assert _served_bytes(svc, att.id) == C_BYTES

    # 旧结果落库：代际已过期 → 丢弃，且**绝不能**删掉新代际的 C。
    # 这里不要绕过代际检查（那会让旧结果真的覆盖 C 的元数据）：要测的是
    # 「丢弃路径即便带着 stored_path 也不得动别人的文件」。
    assert not svc._generation_current(att.id, gen_b), "装置：旧代际必须已经过期"
    svc.apply_outcome(att.id, stale, generation=gen_b)

    _assert_settled_on_c(svc, att.id, c)


# -- 2. 绿守卫：正常的一次定位必须照常提交 ready ------------------------------------


def test_single_relocate_still_commits_ready(svc: AttachmentService, tmp_path: Path):
    a = _write(tmp_path / "a.bin", A_BYTES)
    c = _write(tmp_path / "c.bin", C_BYTES)
    att = svc.run_prepare(svc.prepare(str(a), name="a.bin", topic_id="t1").id)

    svc.plan_relocate(att.id, str(c))
    gen = svc.prepare_generation(att.id)
    applied = svc.apply_outcome(att.id, svc.copy_to_disk(svc.get(att.id), generation=gen), generation=gen)
    assert applied is not None and applied.state == STATE_READY
    assert _final_bytes(svc, att.id) == C_BYTES
    assert _parts(svc.root) == []


# -- 3. 每个操作有自己的临时文件（绝不共用同名 .part） -----------------------------


def test_each_operation_uses_its_own_temp_file(svc: AttachmentService, tmp_path: Path):
    b = _write(tmp_path / "b.bin", B_BYTES)
    c = _write(tmp_path / "c.bin", C_BYTES)

    import builtins

    att = svc.run_prepare(svc.prepare(str(b), name="same.bin", topic_id="t1").id)
    seen: list[Path] = []
    real_open = builtins.open

    def recording_open(file, *args, **kwargs):
        path = Path(str(file))
        if path.name.endswith(attachments_mod.TEMP_SUFFIX) and path not in seen:
            seen.append(path)
        return real_open(file, *args, **kwargs)

    attachments_mod.open = recording_open
    try:
        att_b = svc.plan_relocate(att.id, str(b))
        gen_b = svc.prepare_generation(att.id)
        svc.copy_to_disk(att_b, generation=gen_b)
        svc.plan_relocate(att.id, str(c))
        gen_c = svc.prepare_generation(att.id)
        svc.apply_outcome(att.id, svc.copy_to_disk(svc.get(att.id), generation=gen_c), generation=gen_c)
    finally:
        del attachments_mod.open  # noqa: B909 - 撤销补丁，回到内建 open

    assert seen, "装置失效：没有观察到任何临时文件"
    assert _final_bytes(svc, att.id) == C_BYTES
    target_name = Path(svc.copy_path(svc.get(att.id, check=False))).name
    temp_names = {p.name for p in seen}
    assert all(name.startswith(target_name) for name in temp_names), (
        "临时文件必须在目标名旁边（可被重启清理规则认出来）", temp_names, target_name
    )
    assert len(temp_names) >= 1
    assert _parts(svc.root) == [], "两次操作都不得留下临时文件"


# -- 4. 取消的旧操作不得影响新代际，也不得留下自己的临时文件 ----------------------


def test_cancelled_stale_operation_leaves_new_generation_intact(
    svc: AttachmentService, tmp_path: Path
):
    b = _write(tmp_path / "b.bin", B_BYTES)
    c = _write(tmp_path / "c.bin", C_BYTES)

    att = svc.run_prepare(svc.prepare(str(b), name="a.bin", topic_id="t1").id)
    att_b = svc.plan_relocate(att.id, str(b))
    gen_b = svc.prepare_generation(att.id)

    stale_outcome: list = []
    errors: list = []
    thread, proceed = _stale_commit_race(
        svc,
        att.id,
        b_source=b,
        c_source=c,
        stale_att=att_b,
        stale_gen=gen_b,
        outcome_sink=stale_outcome,
        error_sink=errors,
    )

    # 用户取消这次（旧）操作，同时新代际定位到 C 并提交
    svc.cancel(att.id)
    svc._clear_cancel(att.id)  # 新代际是用户新发起的准备：旧操作的取消标志不该拖住它
    _commit_c(svc, att.id, c)
    assert _final_bytes(svc, att.id) == C_BYTES

    proceed.set()
    thread.join(timeout=60)
    assert not thread.is_alive()
    assert errors == [], errors

    svc.apply_outcome(att.id, stale_outcome[0], generation=gen_b)
    content = _final_bytes(svc, att.id)
    row = _row(svc, att.id)
    assert content == C_BYTES, ("被取消的旧操作改写了新代际的副本", content[:1])
    assert str(row.get("source_path")) == str(c), row
    assert _parts(svc.root) == [], "被取消的旧操作留下了自己的临时文件"


# -- 5. 旧操作晚到：新代际已提交，旧结果连字节都不许落地 --------------------------


def test_late_stale_operation_is_discarded_before_any_write(svc: AttachmentService, tmp_path: Path):
    b = _write(tmp_path / "b.bin", B_BYTES)
    c = _write(tmp_path / "c.bin", C_BYTES)

    att = svc.run_prepare(svc.prepare(str(b), name="a.bin", topic_id="t1").id)
    att_b = svc.plan_relocate(att.id, str(b))
    gen_b = svc.prepare_generation(att.id)

    svc.plan_relocate(att.id, str(c))
    gen_c = svc.prepare_generation(att.id)
    svc.apply_outcome(att.id, svc.copy_to_disk(svc.get(att.id), generation=gen_c), generation=gen_c)

    late = svc.copy_to_disk(att_b, generation=gen_b)
    assert late.state != STATE_READY, "晚到的旧代际不得产出 ready"
    svc.apply_outcome(att.id, late, generation=gen_b)

    _assert_settled_on_c(svc, att.id, c)
    assert _parts(svc.root) == []
    # 文件句柄必须都关掉了：Windows 上还能改名就是证据（句柄泄漏会挡住这个操作）
    moved = Path(svc.content_target(att.id)[0])
    renamed = moved.with_name(moved.name + ".identity-probe")
    os.replace(moved, renamed)
    os.replace(renamed, moved)
    assert moved.read_bytes() == C_BYTES
