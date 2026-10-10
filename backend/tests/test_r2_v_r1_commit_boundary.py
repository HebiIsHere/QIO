"""V 组独立验证：R1（提交边界：旧准备任务永不覆盖更新的重新定位结果）。

独立装置：不使用 r2-w1 / fb_a / fb_e / acc_* 任何既有用例的断言或夹具，
只用 conftest 的 db_conn 与 Settings/AttachmentService 的公开接口 + 受控闸门。

时序一律用 threading.Event / 受控回调，不 sleep。
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from agent.services.attachments import (
    STATE_CANCELLED,
    STATE_READY,
    AttachmentService,
    _identity_of,
)


@pytest.fixture()
def svc(db_conn, tmp_path: Path) -> AttachmentService:
    data = tmp_path / "data"
    data.mkdir()
    return AttachmentService(db_conn, data)


def _src(tmp_path: Path, name: str, payload: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(payload)
    return p


def _part_files(svc: AttachmentService, att_id: str) -> list[Path]:
    target = svc.copy_path(svc.get(att_id, check=False))
    if not target.parent.exists():
        return []
    return [p for p in target.parent.iterdir() if p.name.endswith(".part")]


def test_v_r1a_old_op_parked_before_commit_loses_to_new_relocate(svc, tmp_path):
    """契约时序 1：旧任务复制完、停在提交边界之前；新重定位完整跑完并提交；放行旧任务。

    断言：最终文件内容 = 新内容、行状态 = 新结果、旧结果连行都不许写。
    """
    src_a = _src(tmp_path, "a.bin", b"A" * 4096)
    src_b = _src(tmp_path, "b.bin", b"B" * 8192)
    att = svc.prepare(str(src_a))
    gen_old = svc.prepare_generation(att.id)
    att_old = svc.get(att.id, check=False)

    entered = threading.Event()
    release = threading.Event()

    def gate() -> None:
        entered.set()
        assert release.wait(10), "测试闸门未被放行"

    box: dict[str, object] = {}

    def op_old() -> None:
        box["out"] = svc.copy_to_disk(att_old, generation=gen_old, on_commit=gate)

    thread = threading.Thread(target=op_old, name="v-r1-old")
    thread.start()
    assert entered.wait(10), "旧操作没有到达 on_commit"

    # 新重定位完整跑完并提交（同步编排，走的就是生产同一套 plan_relocate + run_prepare）
    relocated = svc.relocate(att.id, str(src_b))
    assert relocated.state == STATE_READY, relocated
    assert relocated.size_bytes == len(b"B" * 8192)

    release.set()
    thread.join(10)
    assert not thread.is_alive()

    out_old = box["out"]
    assert out_old.state == STATE_CANCELLED, f"旧操作不应提交：{out_old}"

    # 迟到的结果落库也必须被丢弃（不能把新定位的状态改写回去）
    svc.apply_outcome(att.id, out_old, generation=gen_old)
    row = svc.get(att.id, check=True)
    assert row.state == STATE_READY
    assert row.size_bytes == len(b"B" * 8192)
    target = Path(row.stored_path)
    assert target.read_bytes() == b"B" * 8192, "旧任务的字节覆盖了新结果"

    # 提交边界串行：同一时刻新提交的身份仍是磁盘上的那一份
    assert _identity_of(target) == out_old.identity or out_old.identity is None
    assert _part_files(svc, att.id) == []


def test_v_r1b_identity_guard_alone_blocks_overwrite(svc, tmp_path):
    """契约时序 2：目标在旧任务开始时**不存在**（target_identity is None）→ 身份检查必须自己挡住。

    隔离手法：让代际闸与票号闸**都通过**（调用方不给代际 → 现场代际即为当前；
    闸门里把该代际桶里的最新票号写回本操作那张），
    于是唯一还能挡住旧操作的就是「目标目录项与开始时不是同一份」这条身份检查。
    """
    src_a = _src(tmp_path, "a.bin", b"A" * 2048)
    att = svc.prepare(str(src_a))
    assert svc.copy_path(att).exists() is False  # 开始时目标确实不存在

    gen0 = svc.prepare_generation(att.id)
    tickets: list[int] = []
    begin = svc._begin_commit_scope

    def spy(attachment_id: str, generation: int) -> int:
        value = begin(attachment_id, generation)
        tickets.append(value)
        return value

    svc._begin_commit_scope = spy  # type: ignore[method-assign]

    entered = threading.Event()
    release = threading.Event()
    newer = b"N" * 3000

    def gate() -> None:
        target = svc.copy_path(att)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(newer)  # 模拟「更新的提交」把目标写出来了
        # 故意让代际闸与票号闸都通过（票号按 (附件, 代际) 分桶）
        with svc._commit_seq_lock:
            svc._commit_bucket[(att.id, gen0)] = tickets[0]
        entered.set()
        assert release.wait(10)

    box: dict[str, object] = {}

    def op_old() -> None:
        box["out"] = svc.copy_to_disk(
            svc.get(att.id, check=False), generation=None, on_commit=gate
        )

    thread = threading.Thread(target=op_old, name="v-r1-identity")
    thread.start()
    assert entered.wait(10)
    release.set()
    thread.join(10)

    out = box["out"]
    assert out.state == STATE_CANCELLED, f"身份检查没有挡住旧操作：{out}"
    assert svc.copy_path(att).read_bytes() == newer, "旧任务覆盖了更新的目标副本"
    assert _part_files(svc, att.id) == []


def test_v_r1c_two_ops_never_share_a_temp_file(svc, tmp_path):
    """R1 的不变量形状：两个并发准备各自持有**独立**临时文件，绝不共用同名 .part。"""
    src_a = _src(tmp_path, "a.bin", b"A" * 4096)
    att = svc.prepare(str(src_a))
    gen_a = svc.prepare_generation(att.id)
    att_a = svc.get(att.id, check=False)

    entered_a = threading.Event()
    entered_b = threading.Event()
    release = threading.Event()
    seen: list[set[str]] = []

    def gate_a() -> None:
        entered_a.set()
        assert release.wait(10)

    def gate_b() -> None:
        # 在 B 的提交边界之前拍下目录里的 .part 集合：此刻 A 与 B 都还没提交
        target = svc.copy_path(att)
        seen.append({p.name for p in target.parent.iterdir() if p.name.endswith(".part")})
        entered_b.set()
        assert release.wait(10)

    def op_a() -> None:
        svc.copy_to_disk(att_a, generation=gen_a, on_commit=gate_a)

    def op_b() -> None:
        svc.copy_to_disk(att_a, generation=gen_a, on_commit=gate_b)

    ta = threading.Thread(target=op_a, name="v-r1-tmp-a")
    tb = threading.Thread(target=op_b, name="v-r1-tmp-b")
    ta.start()
    assert entered_a.wait(10)
    tb.start()
    assert entered_b.wait(10)
    release.set()
    ta.join(10)
    tb.join(10)

    assert seen, "闸门 B 没有拍到临时文件集合"
    assert len(seen[0]) == 2, f"两个并发操作应当各有自己的 .part，实际 {seen[0]}"
    assert _part_files(svc, att.id) == []


def test_v_r1d_audit_stale_generation_op_started_later_deadlocks_both(svc, tmp_path):
    """【N1 闭合】过期代际的操作**后启动、领到更大票号**，也不得作废当前代际的操作。

    构造（完全确定，不 sleep）：
      1. 当前代际操作（gen=2）先启动并停在提交边界之前；
      2. 一个**过期代际**（gen=1）的复制在此时才开始 —— 它会领到更大的票号；
      3. 过期操作到提交边界：代际闸挡住它（正确）；
      4. 放行当前代际操作：它必须**照常提交**（目标文件存在、行不再停在 prepared）。

    修复前这里两个操作会互相作废：都 cancelled、目标不存在、行停在 prepared。
    """
    src_a = _src(tmp_path, "a.bin", b"A" * 4096)
    src_b = _src(tmp_path, "b.bin", b"B" * 8192)
    att = svc.prepare(str(src_a))
    gen_stale = svc.prepare_generation(att.id)  # = 1（prepare 的 _mark_preparing）
    att_stale = svc.get(att.id, check=False)  # 快照：source_path = a.bin

    entered_new = threading.Event()
    release_new = threading.Event()

    def gate_new() -> None:
        entered_new.set()
        assert release_new.wait(10)

    # 新重定位：代际 → 2
    svc.plan_relocate(att.id, str(src_b))
    assert svc.prepare_generation(att.id) == gen_stale + 1
    att_new = svc.get(att.id, check=False)

    box: dict[str, object] = {}
    tickets: list[tuple[str, int, int]] = []
    begin = svc._begin_commit_scope

    def spy(attachment_id: str, generation: int) -> int:
        value = begin(attachment_id, generation)
        tickets.append((attachment_id, generation, value))
        return value

    svc._begin_commit_scope = spy  # type: ignore[method-assign]

    def op_new() -> None:
        box["new"] = svc.copy_to_disk(att_new, generation=gen_stale + 1, on_commit=gate_new)

    def op_stale() -> None:
        box["stale"] = svc.copy_to_disk(att_stale, generation=gen_stale)

    tn = threading.Thread(target=op_new, name="v-r1-new")
    tn.start()
    assert entered_new.wait(10)

    # 过期代际的操作在此时此刻才开始：它领到更新的票号
    ts = threading.Thread(target=op_stale, name="v-r1-stale")
    ts.start()
    ts.join(10)
    assert box["stale"].state == STATE_CANCELLED  # 代际校验挡住它（正确）

    release_new.set()
    tn.join(10)
    out_new = box["new"]

    # 证明时序确实是 N1 那种反转：过期代际的操作领到了更大的票号
    stale_tickets = [t for (_aid, gen, t) in tickets if gen == gen_stale]
    new_tickets = [t for (_aid, gen, t) in tickets if gen == gen_stale + 1]
    assert stale_tickets and new_tickets
    assert max(stale_tickets) > max(new_tickets), tickets

    print("N1-closed op_new:", out_new.state, out_new.error)
    print("N1-closed target exists:", svc.copy_path(att).exists())
    assert out_new.state == STATE_READY, "当前代际的操作必须照常提交"
    assert svc.copy_path(att).read_bytes() == b"B" * 8192

    svc.apply_outcome(att.id, out_new, generation=gen_stale + 1)
    svc.apply_outcome(att.id, box["stale"], generation=gen_stale)  # 迟到结果不得写行
    row = svc.get(att.id, check=True)
    print("N1-closed row:", row.state, row.size_bytes, "is_preparing:", svc.is_preparing(att.id))
    assert row.state == STATE_READY and row.size_bytes == 8192
    assert not svc.is_preparing(att.id)
    assert _part_files(svc, att.id) == []


def test_v_r1e_two_ops_adversarial_release_order_never_overwrites_newer(svc, tmp_path):
    """契约时序 4：两条并发准备的提交交替 —— 新操作先落地后，旧操作绝不许覆盖它。"""
    src_a = _src(tmp_path, "a.bin", b"A" * 4096)
    src_b = _src(tmp_path, "b.bin", b"B" * 8192)
    att = svc.prepare(str(src_a))
    gen = svc.prepare_generation(att.id)
    att_old = svc.get(att.id, check=False)

    enter_old = threading.Event()
    release_old = threading.Event()
    enter_new = threading.Event()
    release_new = threading.Event()
    observed: list[bytes | None] = []

    def gate_old() -> None:
        enter_old.set()
        assert release_old.wait(10)

    def gate_new() -> None:
        enter_new.set()
        assert release_new.wait(10)

    box: dict[str, object] = {}

    # 旧代际的操作先开始复制（票号更小），停在提交边界之前
    t_old = threading.Thread(
        target=lambda: box.__setitem__(
            "old", svc.copy_to_disk(att_old, generation=gen, on_commit=gate_old)
        )
    )
    t_old.start()
    assert enter_old.wait(10)

    # 新重定位（代际 +1）随后开始复制（票号更大），也停在提交边界之前
    svc.plan_relocate(att.id, str(src_b))
    gen_new = svc.prepare_generation(att.id)
    att_new = svc.get(att.id, check=False)
    t_new = threading.Thread(
        target=lambda: box.__setitem__(
            "new", svc.copy_to_disk(att_new, generation=gen_new, on_commit=gate_new)
        )
    )
    t_new.start()
    assert enter_new.wait(10)

    target = svc.copy_path(att)
    # 新操作先提交
    release_new.set()
    t_new.join(10)
    observed.append(target.read_bytes() if target.exists() else None)
    # 旧操作随后放行：绝不许覆盖
    release_old.set()
    t_old.join(10)
    observed.append(target.read_bytes() if target.exists() else None)

    assert box["new"].state == STATE_READY, box["new"]
    assert box["old"].state == STATE_CANCELLED, box["old"]
    assert observed[-1] == b"B" * 8192, "旧操作覆盖了新结果"
    assert _part_files(svc, att.id) == []
    # 落库也要以新结果为准：先落新操作的，再落旧操作的（旧结果必须被丢弃）
    svc.apply_outcome(att.id, box["new"], generation=gen_new)
    svc.apply_outcome(att.id, box["old"], generation=gen)
    row = svc.get(att.id, check=True)
    assert row.size_bytes == 8192 and row.state == STATE_READY
    assert Path(row.stored_path).read_bytes() == b"B" * 8192


def test_v_r1f_n1_closed_bind_succeeds_after_stale_generation_op(svc, tmp_path):
    """N1 闭合（端到端）：过期代际操作后启动之后，这一轮的附件绑定仍然成功（不再卡在 prepared）。"""
    src_a = _src(tmp_path, "a.bin", b"A" * 4096)
    src_b = _src(tmp_path, "b.bin", b"B" * 8192)
    att = svc.prepare(str(src_a))
    gen_stale = svc.prepare_generation(att.id)
    att_stale = svc.get(att.id, check=False)
    svc.prepare_wait_seconds = 0.05  # 收紧等待上限（测试可收紧，见 __init__ 注释）

    enter_new = threading.Event()
    release_new = threading.Event()

    svc.plan_relocate(att.id, str(src_b))
    att_new = svc.get(att.id, check=False)
    box: dict[str, object] = {}

    def gate_new() -> None:
        enter_new.set()
        assert release_new.wait(10)

    t_new = threading.Thread(
        target=lambda: box.__setitem__(
            "new", svc.copy_to_disk(att_new, generation=gen_stale + 1, on_commit=gate_new)
        )
    )
    t_new.start()
    assert enter_new.wait(10)
    t_stale = threading.Thread(
        target=lambda: box.__setitem__(
            "stale", svc.copy_to_disk(att_stale, generation=gen_stale)
        )
    )
    t_stale.start()
    t_stale.join(10)
    release_new.set()
    t_new.join(10)

    assert box["stale"].state == STATE_CANCELLED
    assert box["new"].state == STATE_READY, "当前代际的操作必须提交成功"

    svc.apply_outcome(att.id, box["new"], generation=gen_stale + 1)
    svc.apply_outcome(att.id, box["stale"], generation=gen_stale)  # 迟到结果不得写行

    outcome = asyncio.run(
        svc.bind_for_turn(turn_id="turn_new", attachment_ids=[att.id], topic_id=None)
    )
    print("N1-closed bind bound:", outcome.bound)
    print("N1-closed bind rejected:", outcome.rejected)
    print("N1-closed bind is_preparing:", svc.is_preparing(att.id))
    assert outcome.rejected == [], "修复前这里会是 attachment_not_ready"
    assert outcome.bound == [att.id]
    assert svc.copy_path(att).read_bytes() == b"B" * 8192


def test_v_r1g_no_generation_caller_is_now_generation_gated(svc, tmp_path):
    """N1 修复带来的新不变量：**没有代际信息**的旧调用方（上传 / 老调用路径）在开始那一刻
    绑定当下代际 —— 之后任何更新的准备都会让它过期，它不再"跳过代际校验"。"""
    src_a = _src(tmp_path, "a.bin", b"A" * 4096)
    src_b = _src(tmp_path, "b.bin", b"B" * 8192)
    att = svc.prepare(str(src_a))
    att_old = svc.get(att.id, check=False)

    entered = threading.Event()
    release = threading.Event()

    def gate() -> None:
        entered.set()
        assert release.wait(10)

    box: dict[str, object] = {}

    def op_old() -> None:
        box["old"] = svc.copy_to_disk(att_old, generation=None, on_commit=gate)

    thread = threading.Thread(target=op_old, name="v-r1g")
    thread.start()
    assert entered.wait(10)

    # 新重定位完整跑完并提交
    svc.relocate(att.id, str(src_b))
    release.set()
    thread.join(10)

    out = box["old"]
    print("r1g out:", out.state, out.error, "commit_generation:", out.commit_generation)
    assert out.commit_generation is not None, "不给代际的调用方也必须在开始时绑定当下代际"
    assert out.state == STATE_CANCELLED, "旧调用方不得覆盖更新的重定位结果"

    # 不带 generation 落库：apply_outcome 用结果自带的代际作废它
    svc.apply_outcome(att.id, out)
    row = svc.get(att.id, check=True)
    assert row.state == STATE_READY and row.size_bytes == 8192
    assert Path(row.stored_path).read_bytes() == b"B" * 8192
    assert _part_files(svc, att.id) == []
