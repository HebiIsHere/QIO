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

    隔离手法：让票号校验与代际校验**都通过**（代际传 None；闸门里把票号写回旧操作那张），
    于是唯一还能挡住旧操作的就是「目标目录项与开始时不是同一份」这条身份检查。
    """
    src_a = _src(tmp_path, "a.bin", b"A" * 2048)
    att = svc.prepare(str(src_a))
    assert svc.copy_path(att).exists() is False  # 开始时目标确实不存在

    tickets: list[int] = []
    begin = svc._begin_commit_scope

    def spy(attachment_id: str) -> int:
        value = begin(attachment_id)
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
        with svc._commit_seq_lock:
            svc._commit_seq[att.id] = tickets[0]  # 故意让票号校验仍然通过
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
    """【审计 N1】票号顺序与代际顺序可以相反：后开始的**过期代际**操作会作废当前代际的操作。

    构造（完全确定，不 sleep）：
      1. 旧代际操作（gen=1）在闸门里停住，**已经领到票号 2**；
      2. 新重定位把代际推到 2，新操作（gen=2）也被闸门停住；
      3. 让一个**过期代际**（gen=1）的复制在此时开始复制 —— 它领到**更新的票号 3**；
      4. 它到提交边界：代际校验失败 → 作废（正确）；
      5. 放行 gen=2 那个操作：票号校验失败 → 也作废。

    结果：**没有任何操作提交**，附件永远停在 prepared（本轮绑定会等到超时才结构化拒绝）。
    契约要求「旧操作永不覆盖新结果」是满足的，但功能上附件再也到不了 ready。
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

    # 审计结论：当前代际的操作也被票号校验作废了
    print("N1 op_new:", out_new.state, out_new.error)
    print("N1 target exists:", svc.copy_path(att).exists())
    row = svc.get(att.id, check=False)
    print("N1 row state:", row.state, "is_preparing:", svc.is_preparing(att.id))
    assert out_new.state == STATE_CANCELLED
    assert not svc.copy_path(att).exists()
    assert row.state == "prepared"


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


def test_v_r1f_audit_n1_impact_bind_rejects_not_ready(svc, tmp_path):
    """【审计 N1 的影响面】两个操作互相作废之后，这一轮的附件绑定会等到超时才结构化拒绝。"""
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
    assert box["new"].state == STATE_CANCELLED

    outcome = asyncio.run(
        svc.bind_for_turn(turn_id="turn_new", attachment_ids=[att.id], topic_id=None)
    )
    print("N1 bind bound:", outcome.bound)
    print("N1 bind rejected:", outcome.rejected)
    print("N1 rejection code:", outcome.rejection_code_for(att.id))
    assert outcome.bound == []
    assert outcome.rejection_code_for(att.id) == "attachment_not_ready"
    assert not svc.copy_path(att).exists()
