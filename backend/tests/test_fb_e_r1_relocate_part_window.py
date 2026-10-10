"""fb-E / R1 反例：旧重新定位任务与最新一次定位共用同名 .part，破坏最新结果。

用户可见规则（K1 + 既有 re locate 契约）：
* 同一条附件先后被定位到 B、再定位到 C 时，**最新一次定位**（C）的内容与元数据必须胜出并达到 ready；
* 旧的在飞定位任务（B）**不得**让最新一次定位失败，也不得改写最新 ready 副本；
* 收敛后必须自洽：state=ready、磁盘字节 == 最新来源（C）、sha256 对应 C、不留无人认领的 .part。

基线实际（确定性交错，见下）：
  B 的复制在**源读取**处被闸门挡住（此时它已按同名规则打开了副本的 .part）；
  此时用户改主意定位到 C —— C 的复制因为同名 .part 被旧任务占用，在 Windows 上以
  「没有权限写入」失败；随后放行 B，B 因代际过期而取消并删掉 .part。
  收敛结果：metadata.source_path 指向 C，但磁盘副本仍是**上一份 A**，state=failed ——
  「元数据指向新来源、实际字节是旧来源」，且用户最新一次定位被旧任务毁掉。

时序控制：只用 threading.Event 闸门控制 B 的源读取（不 sleep 碰运气）；C 的复制在主线程跑。

运行：cd backend; uv run --frozen pytest -q tests/test_fb_e_r1_relocate_part_window.py
"""

from __future__ import annotations

import builtins
import threading
from pathlib import Path

import pytest

from agent.services.attachments import STATE_READY, AttachmentService

# 同长度、不同字节：大小校验抓不出「内容被换成另一份」。
A_BYTES = b"A" * 4096
B_BYTES = b"B" * 4096
C_BYTES = b"C" * 4096


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture()
def svc(db_conn, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


def _served_bytes(svc: AttachmentService, att_id: str) -> bytes:
    """按「预测副本路径」读字节（不受 state 影响，用于观察磁盘事实）。"""
    row = svc.get(att_id, check=False)
    path = svc.copy_path(row)
    return path.read_bytes() if path.exists() else b""


def _gate_source_read(monkeypatch, source: Path):
    """只对 source 的读取设确定性闸门：进入后阻塞，直到 release 被置位。"""
    entered = threading.Event()
    release = threading.Event()
    real_open = builtins.open

    def gated_open(file, mode="r", *args, **kwargs):  # noqa: ANN001
        handle = real_open(file, mode, *args, **kwargs)
        try:
            same = Path(str(file)) == source
        except Exception:  # noqa: BLE001
            same = False
        if same and "r" in str(mode):
            original_read = handle.read

            def read(*a, **kw):
                entered.set()
                assert release.wait(15), "测试闸门没有被放开"
                return original_read(*a, **kw)

            handle.read = read
        return handle

    monkeypatch.setattr(builtins, "open", gated_open)
    return entered, release


def test_stale_relocate_must_not_break_newer_relocate(svc: AttachmentService, tmp_path: Path, monkeypatch):
    a = _write(tmp_path / "a.bin", A_BYTES)
    b = _write(tmp_path / "b.bin", B_BYTES)
    c = _write(tmp_path / "c.bin", C_BYTES)

    att = svc.run_prepare(svc.prepare(str(a), name="同名.bin", topic_id="t1").id)
    assert _served_bytes(svc, att.id) == A_BYTES, "装置：初始副本必须是 A"

    # 旧定位：B 代际，复制放到后台线程；在「读源文件」处卡住（此时同名 .part 已被它打开）
    att_b = svc.plan_relocate(att.id, str(b))
    gen_b = svc.prepare_generation(att.id)
    entered, release = _gate_source_read(monkeypatch, b)
    outcome_b: dict = {}

    def run_b() -> None:
        outcome_b["out"] = svc.copy_to_disk(att_b, generation=gen_b)

    worker = threading.Thread(target=run_b, daemon=True)
    worker.start()
    assert entered.wait(15), "装置：B 没有进入源读取闸门"

    # 用户改主意：定位到 C（最新代际）；它必须在旧任务仍卡着时成功收敛到 ready
    svc.plan_relocate(att.id, str(c))
    gen_c = svc.prepare_generation(att.id)
    assert gen_c > gen_b, "装置：新一次定位必须产生更新的代际"
    outcome_c = svc.copy_to_disk(svc.get(att.id, check=False), generation=gen_c)
    svc.apply_outcome(att.id, outcome_c, generation=gen_c)

    release.set()
    worker.join(20)
    assert not worker.is_alive(), "装置：B 的复制线程没有结束"
    stale = outcome_b.get("out")
    if stale is not None:
        svc.apply_outcome(att.id, stale, generation=gen_b)  # 旧结果迟到：必须被丢弃

    row = svc.get(att.id, check=False)
    served = _served_bytes(svc, att.id)
    leftovers = [p for p in (tmp_path / "data").rglob("*.part")]

    # ① 最新一次定位必须成功（旧任务不得把它毁掉）
    assert row.state == STATE_READY, (
        "旧的在飞定位任务不得让最新一次定位失败",
        {"state": row.state, "error": row.error, "source_path": row.source_path},
    )
    # ② 磁盘字节必须是**最新来源** C（不是上一次的 A，也不是旧任务的 B）
    assert served == C_BYTES, (
        "收敛后副本字节必须对应最新一次定位（C）：元数据与磁盘不得自相矛盾",
        {"disk_first_byte": served[:1], "expected": b"C", "source_path": row.source_path},
    )
    # ③ 元数据必须与磁盘自洽（sha 对应 C、大小正确）
    assert row.sha256 and row.sha256 == __import__("hashlib").sha256(C_BYTES).hexdigest(), (
        "元数据 sha256 必须对应最新来源 C",
        {"sha256": row.sha256},
    )
    # ④ 不得留下无人认领的 .part
    assert leftovers == [], ("不得留下无人认领的 .part", [str(p) for p in leftovers])


def test_serial_relocates_converge_to_newest(svc: AttachmentService, tmp_path: Path):
    """绿守卫：没有并发旧任务时，最新一次定位必须收敛到 C（证明装置与断言本身可达）。"""
    a = _write(tmp_path / "a.bin", A_BYTES)
    c = _write(tmp_path / "c.bin", C_BYTES)
    b = _write(tmp_path / "b.bin", B_BYTES)
    att = svc.run_prepare(svc.prepare(str(a), name="同名.bin", topic_id="t1").id)
    svc.relocate(att.id, str(b))
    svc.relocate(att.id, str(c))
    row = svc.get(att.id, check=False)
    assert row.state == STATE_READY, row.state
    assert _served_bytes(svc, att.id) == C_BYTES
