"""fb-E / 阶段二 · 跨模块组合（后端）：R1×R2 交错 + R7 retry/resend 准入。

只控制外部时序（I/O 闸门 / 假 provider hold），用真实附件服务、真实 sqlite、真实 FastAPI。

组合一（R1×R2）：
  * 集合提交进行中：a（未绑定草稿）先被绑定，b（旧轮附件）走重试克隆并被闸门卡住；
  * 同一时刻对 b 发起**重新定位**（R1 路径：后台复制被源读取闸门卡住）；
  * 两个在途任务之间删除 a；
  * 放行：集合提交与定位各自收敛。
  断言：集合回执绝不包含已删除的 a；b 的最新一次定位必须胜出（ready + 磁盘字节 == 新来源）；
  不得留下无人认领的 .part；回执里的每个 ID 都必须仍然存在且归属本轮。

组合二（R7）：活动取消 → 动作表只给 retry；用 retry 语义（既有发送接口 + retry_of_turn_id）
  能真正创建新轮；resend 对 cancelled 行仍然拒绝（准入不变，只是不再被摆出来）。

运行：cd backend; uv run --frozen pytest -q tests/test_fb_e_combo_boundaries.py
"""

from __future__ import annotations

import asyncio
import builtins
import errno
import shutil
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent.adapters.fake import FakeStreamAdapter, StreamScript
from agent.api.server import create_app
from agent.credentials.store import MemoryKeyring
from agent.services import attachments as attachments_mod
from agent.services.attachments import STATE_READY, AttachmentService

ANSWER_MARKER = "[[QIO:ANSWER]]"


# ---------------------------------------------------------------- 组合一：R1×R2

def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture()
def svc(db_conn, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


def _ready(svc: AttachmentService, path: Path):
    return svc.run_prepare(svc.prepare(str(path), topic_id="t1").id)


def _force_copy_fallback(monkeypatch) -> None:
    def refuse_link(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError(errno.EPERM, "hardlink not permitted")

    monkeypatch.setattr(attachments_mod.os, "link", refuse_link)


def _gate_first_copy(monkeypatch):
    """卡住第一次克隆复制（集合提交里的 await 点）。"""
    real = shutil.copyfile
    entered = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def stub(src, dst, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls["n"] += 1
        if calls["n"] == 1:
            entered.set()
            assert release.wait(15), "克隆闸门没有被放开"
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(attachments_mod.shutil, "copyfile", stub)
    return entered, release


def _gate_source_read(monkeypatch, source: Path):
    """只对 source 的读取设闸门（R1：旧任务在同名 .part 上卡住）。"""
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
                assert release.wait(15), "源读取闸门没有被放开"
                return original_read(*a, **kw)

            handle.read = read
        return handle

    monkeypatch.setattr(builtins, "open", gated_open)
    return entered, release


async def test_combo_set_commit_and_relocate_with_delete(
    svc: AttachmentService, tmp_path: Path, monkeypatch
):
    a = _ready(svc, _write(tmp_path / "a-草稿.bin", b"a" * 4096))
    b = _ready(svc, _write(tmp_path / "b-历史.bin", b"b" * 4096))
    b2 = _write(tmp_path / "b-新位置.bin", b"B2" * 2048)
    await svc.bind_for_turn(turn_id="turn_old", attachment_ids=[b.id], topic_id="t1")

    _force_copy_fallback(monkeypatch)
    clone_entered, clone_release = _gate_first_copy(monkeypatch)

    bind_task = asyncio.create_task(
        svc.bind_for_turn(
            turn_id="turn_new",
            attachment_ids=[a.id, b.id],
            topic_id="t1",
            retry_of_turn_id="turn_old",
        )
    )
    assert await asyncio.to_thread(clone_entered.wait, 15), "装置：集合提交没有进入克隆闸门"

    # 同一时刻：对 b 发起重新定位（R1 路径），后台复制被源读取闸门卡住
    att_b = svc.plan_relocate(b.id, str(b2))
    gen_b2 = svc.prepare_generation(b.id)
    read_entered, read_release = _gate_source_read(monkeypatch, b2)
    relocate_out: dict = {}

    def run_relocate() -> None:
        relocate_out["out"] = svc.copy_to_disk(svc.get(b.id, check=False), generation=gen_b2)

    reloc_thread = threading.Thread(target=run_relocate, daemon=True)
    reloc_thread.start()
    assert read_entered.wait(15), "装置：定位复制没有进入源读取闸门"

    # 两个在途任务之间：删除集合里的第一项 a
    assert svc.delete(a.id) is not None

    clone_release.set()
    outcome = await asyncio.wait_for(bind_task, timeout=40)
    read_release.set()
    reloc_thread.join(20)
    if relocate_out.get("out") is not None:
        svc.apply_outcome(b.id, relocate_out["out"], generation=gen_b2)

    bound_ids = [str(x) for x in outcome.bound]
    # ① 已删除的成员绝不能被报成已绑定
    assert a.id not in bound_ids, (
        "集合回执里出现了已删除的成员",
        {"bound": bound_ids, "rejected": [str(x[0]) for x in outcome.rejected]},
    )
    assert svc.get(a.id, check=False) is None, "装置：a 必须已经被删除"
    # ② 回执里的每个 ID 在收敛时刻都必须存在且归属本轮
    for att_id in bound_ids:
        row = svc.get(att_id, check=False)
        assert row is not None, ("回执里的附件在库里不存在", att_id)
        assert str(row.turn_id or "") == "turn_new", ("回执里的附件不归属本轮", row.turn_id)
    # ③ 最新一次定位必须胜出：b ready 且磁盘字节 == 新来源
    row_b = svc.get(b.id, check=False)
    served = Path(svc.copy_path(row_b)).read_bytes() if Path(svc.copy_path(row_b)).exists() else b""
    assert row_b.state == STATE_READY, ("b 的最新一次定位没有收敛到 ready", row_b.state, row_b.error)
    assert served == b2.read_bytes(), (
        "b 的最新一次定位没有胜出（磁盘字节与最新来源不一致）",
        {"disk_first": served[:2], "expected_first": b2.read_bytes()[:2], "source_path": row_b.source_path},
    )
    leftovers = list((tmp_path / "data").rglob("*.part"))
    assert leftovers == [], ("组合交错不得留下无人认领的 .part", [str(p) for p in leftovers])


# ---------------------------------------------------------------- 组合二：R7

def _client(app):
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://qio.test", timeout=120.0
    )


def _of(app, kind: str, turn_id: str | None = None) -> list:
    out = []
    for event in list(app.state.ctx.bus._history):
        if event.type.value != kind:
            continue
        if turn_id is not None and str(event.data.get("turn_id")) != turn_id:
            continue
        out.append(event)
    return out


async def _wait(predicate, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return bool(predicate())


@pytest.fixture()
def app(db_conn, settings):
    application = create_app(settings, db_conn)
    application.state.ctx.credentials._kr = MemoryKeyring()
    return application


async def test_combo_cancel_then_retry_new_turn_and_resend_admission(app):
    hold = asyncio.Event()
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                text_chunks=[ANSWER_MARKER + "\n", "第一段。", "第二段。"],
                hold_after=2,
                hold=hold,
                gap_ms=60,
            ),
            StreamScript(text_chunks=[ANSWER_MARKER + "\n", "重试的新回答。"]),
        ]
    )
    app.state.ctx.build_adapter = AsyncMock(return_value=adapter)

    async with _client(app) as client:
        first = await client.post("/api/turns", json={"message": "第一轮"})
        turn_first = str(first.json()["turn_id"])
        assert await _wait(
            lambda: any(
                str(e.data.get("turn_id")) == turn_first and str(e.data.get("content") or "")
                for e in _of(app, "ASSISTANT")
            ),
            timeout=20,
        ), "装置：没有流式增量"

        await client.post("/api/turns/%s/cancel" % turn_first)
        assert await _wait(lambda: _of(app, "TURN_END", turn_first), timeout=30)
        end = _of(app, "TURN_END", turn_first)[0].data
        actions = [str(x) for x in end.get("actions") or []]
        assert actions == ["retry"], ("活动取消只应给 retry", actions)
        assert "resend" not in actions

        # retry 语义：用既有发送接口 + retry_of_turn_id 创建**新轮**，真实可跑
        retried = await client.post(
            "/api/turns",
            json={"message": "重试这一轮", "retry_of_turn_id": turn_first},
        )
        assert retried.status_code == 200, retried.text[:200]
        turn_retry = str(retried.json()["turn_id"])
        assert turn_retry != turn_first, "retry 必须是新 turn"
        assert await _wait(lambda: _of(app, "TURN_END", turn_retry), timeout=40), "重试轮没有结束"
        end_retry = _of(app, "TURN_END", turn_retry)[0].data
        assert end_retry.get("status") == "completed", end_retry.get("status")

        # resend 准入不变：cancelled 行仍然拒绝（只是不再被动作表摆出来）
        resend = await client.post("/api/turns/%s/resend" % turn_first)
        assert resend.status_code == 409, (
            "resend 对 cancelled 行的准入应当保持拒绝（仅不再作为可用动作展示）",
            resend.status_code,
        )
