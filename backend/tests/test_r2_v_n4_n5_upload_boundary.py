"""V 组独立验证：N4（register_upload 不得绕过 R1 提交边界）与 N5（上传 worker 的代际接线）。

N4 闭合断言：不存在绕过提交边界的落盘入口；浏览器回退上传与路径准备受同一套
（代际 + 票号 + 目标身份）边界保护。

N5（本轮审计发现、现已闭合）：上传 worker 原先绑定的是「它自己启动时」的代际，而落库用的是
「登记时」的代际 —— 登记在前的上传可以覆盖后发起的重定位结果，并把行与磁盘搞成不一致。
本文件现在钉住**修复后**的契约：提交边界与落库必须用同一个（登记时）代际。
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
    DiskOutcome,
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


def test_v_n5_stale_upload_binds_registration_generation(svc, tmp_path):
    """N5 闭合：登记在前的上传必须以**登记时**的代际提交，不得覆盖后发起的重定位。

    生产形状：api/server.py 在登记时算好 upload_generation 并交给 UploadJob；
    attachment_upload.run_upload_worker 用同一个代际调 write_upload_stream。
    于是「登记在前、worker 启动在后」的上传在提交边界就被判为过期：
    既不能覆盖重定位写下的副本，也不会让行与磁盘不一致。
    """
    src_b = _src(tmp_path, "b.bin", b"B" * 8192)

    # 1) 上传登记（gen=1）
    att = svc.begin_upload(name="up.bin")
    gen_at_registration = svc.prepare_generation(att.id)

    # 2) 用户先重定位并完整提交（gen=2）：目标 = B 的字节
    relocated = svc.relocate(att.id, str(src_b))
    assert relocated.state == STATE_READY
    assert svc.copy_path(att).read_bytes() == b"B" * 8192

    # 3) worker 现在才启动，但用的是**登记时**的代际（N5 的修法）
    out = svc.write_upload_stream(att, [b"U" * 64], generation=gen_at_registration)
    print("N5-closed upload outcome:", out.state, out.error, "bound gen:", out.commit_generation)
    assert out.state == STATE_CANCELLED, "过期上传必须在提交边界被作废（不得覆盖更新的准备）"
    assert svc.copy_path(att).read_bytes() == b"B" * 8192, "重定位写下的副本被过期上传覆盖了"

    # 4) 落库同样按登记代际丢弃；行与磁盘保持一致（不再是「行列 ready/8192、磁盘 64 字节」）
    svc.apply_outcome(att.id, out, generation=gen_at_registration)
    raw = svc.get(att.id, check=False)
    assert raw.state == STATE_READY and raw.size_bytes == 8192
    assert svc.copy_path(att).stat().st_size == 8192
    assert _part_files(svc, att.id) == []

    # 5) 后续绑定仍能正常使用这一条（不再出现「大小与登记不一致」）
    bind = asyncio.run(
        svc.bind_for_turn(turn_id="turn_v_n5", attachment_ids=[att.id], topic_id=None)
    )
    assert bind.rejected == [], bind.rejected
    assert bind.bound == [att.id]


def test_v_n5_worker_passes_registration_generation(svc, tmp_path):
    """N5 的接线守卫：run_upload_worker 必须把 UploadJob 上的代际交给落盘入口。

    只让服务层「调用方自觉传 generation」是不够的：真正的生产调用点是
    attachment_upload.run_upload_worker —— 它必须把 job.generation 透传下去，
    否则本文件上面那条服务层用例照样绿、线上却仍会以新代际覆盖新结果。
    """
    from agent.services.attachment_upload import (
        SENTINEL_END,
        UploadJob,
        run_upload_worker,
    )

    att = svc.begin_upload(name="worker.bin")
    captured: dict[str, object] = {}

    def fake_write(a, chunks, *, max_bytes=None, generation=None):
        captured["generation"] = generation
        captured["bytes"] = b"".join(chunks)
        return DiskOutcome(state=STATE_CANCELLED, error="测试替身：不落盘")

    svc.write_upload_stream = fake_write  # type: ignore[method-assign]
    loop = asyncio.new_event_loop()
    try:
        job = UploadJob(label="v-n5", loop=loop, poll_seconds=0.01)
        # 用属性赋值而不是构造参数：接线缺失的旧实现也能跑到这里，于是红的是**行为**
        # （没有把代际交给落盘入口），而不是一个「构造参数不认识」的 TypeError。
        job.generation = 7
        try:
            job.box.put_nowait(b"abc")
            job.box.put_nowait(SENTINEL_END)
            out = run_upload_worker(svc, att, job, max_bytes=1000)
        finally:
            job.close()
    finally:
        loop.close()
    assert out.state == STATE_CANCELLED
    assert captured["generation"] == 7, (
        "run_upload_worker 没有把登记时的代际交给落盘入口（N5 回归）",
        captured,
    )
    assert captured["bytes"] == b"abc"
