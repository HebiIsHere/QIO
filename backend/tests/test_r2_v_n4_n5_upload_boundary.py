"""V 组独立验证：N4（register_upload 不得绕过 R1 提交边界）与 N5（审计新发现）。

N4 闭合断言：不存在绕过提交边界的落盘入口；浏览器回退上传与路径准备受同一套
（代际 + 票号 + 目标身份）边界保护。

N5 是**本轮新发现**：上传 worker 绑定的是「它自己启动时」的代际，而落库用的是
「登记时」的代际 —— 登记在前的上传可以覆盖后发起的重定位结果，并把行与磁盘搞成不一致。
本文件把 N5 的当前行为钉成可复现证据。
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from agent.services.attachments import (
    STATE_CANCELLED,
    STATE_CHANGED,
    STATE_READY,
    AttachmentService,
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


def test_v_n4_no_bypass_entry_and_upload_obeys_boundary(svc, tmp_path):
    """N4 闭合：绕过提交边界的落盘入口已不存在；上传期间的更新准备会作废过期上传。"""
    assert not hasattr(AttachmentService, "_write_upload"), "旧的旁路落盘入口必须已被删除"

    # 浏览器回退上传仍然可用（行为没被 refactor 弄坏）
    plain = svc.register_upload(b"hello-n4", name="browser.bin")
    assert plain.state == STATE_READY, plain
    assert Path(plain.stored_path).read_bytes() == b"hello-n4"

    # 过期上传不得覆盖更新的重定位结果
    src_b = _src(tmp_path, "b.bin", b"B" * 8192)
    att = svc.begin_upload(name="up.bin")
    att_stale = svc.get(att.id, check=False)

    entered = threading.Event()
    release = threading.Event()
    original = svc._upload_stream_impl

    def gated(a, chunks, *, max_bytes, ticket, generation):
        entered.set()
        assert release.wait(10)
        return original(a, chunks, max_bytes=max_bytes, ticket=ticket, generation=generation)

    svc._upload_stream_impl = gated  # type: ignore[method-assign]

    box: dict[str, object] = {}

    def op_upload() -> None:
        box["up"] = svc.write_upload_stream(att_stale, [b"U" * 64])

    thread = threading.Thread(target=op_upload, name="v-n4-upload")
    thread.start()
    assert entered.wait(10)

    # 上传仍在途中 —— 用户重定位并完整提交
    relocated = svc.relocate(att.id, str(src_b))
    assert relocated.state == STATE_READY and relocated.size_bytes == 8192

    release.set()
    thread.join(10)
    out = box["up"]
    print("N4 upload outcome:", out.state, out.error, "bound gen:", out.commit_generation)
    assert out.state == STATE_CANCELLED, "过期上传必须在提交边界被作废"
    assert svc.copy_path(att).read_bytes() == b"B" * 8192, "上传覆盖了更新后的副本"

    svc.apply_outcome(att.id, out, generation=out.commit_generation)
    row = svc.get(att.id, check=True)
    assert row.state == STATE_READY and row.size_bytes == 8192
    assert _part_files(svc, att.id) == []


def test_v_n5_audit_upload_worker_binds_worker_start_generation(svc, tmp_path):
    """【审计 N5】上传 worker 绑定「自己启动时」的代际，而不是「登记时」的代际。

    生产形状：server.py 在登记时算好 `upload_generation`，
    attachment_upload.run_upload_worker 调 write_upload_stream **不传** generation
    （见 attachment_upload.py 的 run_upload_worker）。

    于是「登记在前、worker 启动在后」的上传会以**重定位那一代**的身份提交：
    提交边界放行、把后发起的重定位结果覆盖掉；而落库仍按登记时代际 → 结果被丢弃，
    磁盘与行记录就此不一致（读时表现为 `changed`）。

    本用例断言**当前行为**，把这条残留边界固定成可复现证据。
    """
    src_b = _src(tmp_path, "b.bin", b"B" * 8192)

    # 1) 上传登记（gen=1）
    att = svc.begin_upload(name="up.bin")
    gen_at_registration = svc.prepare_generation(att.id)

    # 2) 用户先重定位并完整提交（gen=2）：目标 = B 的字节
    relocated = svc.relocate(att.id, str(src_b))
    assert relocated.state == STATE_READY
    assert svc.copy_path(att).read_bytes() == b"B" * 8192

    # 3) 上传 worker 现在才启动（生产不传 generation）→ 它绑定当下的代际（= 2）
    out = svc.write_upload_stream(att, [b"U" * 64])
    print("N5 upload outcome:", out.state, out.error, "bound gen:", out.commit_generation)
    print("N5 registration gen:", gen_at_registration)
    print("N5 target bytes after upload:", svc.copy_path(att).read_bytes()[:16])

    # 4) 落库仍按登记时代际（路由的 apply_outcome 用 upload_generation）
    svc.apply_outcome(att.id, out, generation=gen_at_registration)
    raw = svc.get(att.id, check=False)
    checked = svc.get(att.id, check=True)
    print("N5 row(check=False):", raw.state, raw.size_bytes)
    print("N5 row(check=True):", checked.state)
    print("N5 actual file size:", svc.copy_path(att).stat().st_size)

    # 当前行为：上传被当作「重定位那一代的操作」放行并覆盖了它
    assert out.commit_generation == gen_at_registration + 1
    assert out.state == STATE_READY, "上传在提交边界没有被作废"
    assert svc.copy_path(att).read_bytes() == b"U" * 64, "重定位写下的副本被覆盖了"
    # 而落库按登记代际丢弃 → 行仍是重定位的事实（ready + 8192），磁盘却是上传的 64 字节。
    # get(check=True) 只验副本存在、不比对内容 → **界面照样显示 ready**。
    assert raw.state == STATE_READY and raw.size_bytes == 8192
    assert checked.state == STATE_READY, "行没有察觉磁盘已被换掉（_check 只验存在）"

    # 影响面：下一次绑定才在就绪判据上炸掉（大小与登记不一致）→ 这一条用不了了
    bind = asyncio.run(
        svc.bind_for_turn(turn_id="turn_v_n5", attachment_ids=[att.id], topic_id=None)
    )
    print("N5 bind rejected:", bind.rejected)
    assert bind.bound == []
    assert "大小与登记不一致" in bind.rejected[0][1]
    assert _part_files(svc, att.id) == []
