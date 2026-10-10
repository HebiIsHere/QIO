"""R7 §1.3：唯一的执行就绪条件 —— **prepared 一律不就绪**（先写红）。

复现的缺陷（修复前）：
* `_reject_reason` 把 `STATE_PREPARED` 当「状态允许」（attachments.py 原 1293 行）；
* `bind_for_turn` 对普通未绑定附件**只写归属**，既不等首次后台复制，也不验证副本可读。
于是「登记完立刻发送」会得到：请求 accepted、附件其实还没就绪、模型照常开始 ——
闸门仍关闭时就能看到执行发生（模型/工具/TURN_START 都出现）。

冻结规则（§1.3）：
* copy 执行就绪 = `ready` **且**副本实际存在可打开、大小与登记一致；
* reference 按既有可用性/变化规则；
* `prepared` 一律不就绪 → **等正在进行的首次准备**（事件/await，不轮询、不加固定延时；
  不启动第二份重复复制）或**结构化拒绝** `attachment_not_ready`（人话原因 + 可用操作=重试）；
* 等待**有界**（PREPARE_WAIT_MS），到点结构化拒绝；
* 显式空列表仍表示「不带附件」；旧客户端缺字段的兜底绑定同样不得绑未就绪的。

运行：cd backend; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/test_attachments_readiness.py -q
"""

from __future__ import annotations

import asyncio
import errno
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.server import create_app
from agent.credentials.store import MemoryKeyring
from agent.services import attachments as attachments_mod
from agent.services.attachments import AttachmentService, DiskOutcome
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

GATE_DEADLINE = 30.0


@pytest.fixture()
def svc(db_conn, tmp_path: Path) -> AttachmentService:
    service = AttachmentService(db_conn, tmp_path / "data")
    # 默认 60s 太长：测试收紧（生产值不动）
    service.prepare_wait_seconds = 5.0
    return service


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _gate_first_copy(monkeypatch):
    """把首次复制**卡在工作线程里**：复制循环第一次让出时进门，等 release 才继续。

    闸门放在复制内部（而不是 bind 里）→ 触发的是「首次准备还没完成」这个真实时序。
    """
    entered = threading.Event()
    release = threading.Event()
    real_yield = attachments_mod._yield_to_event_loop

    def gated_yield() -> None:
        entered.set()
        assert release.wait(GATE_DEADLINE), "测试没有放行复制闸门"
        real_yield()

    monkeypatch.setattr(attachments_mod, "_yield_to_event_loop", gated_yield)
    return entered, release


async def _first_prepare(svc: AttachmentService, attachment_id: str) -> None:
    """与路由的后台准备同构：工作线程只做文件 I/O，落库回到事件循环线程。"""
    att = svc.get(attachment_id, check=False)
    assert att is not None
    outcome = await asyncio.to_thread(svc.copy_to_disk, att)
    svc.apply_outcome(attachment_id, outcome)


def _count_copies(svc: AttachmentService, monkeypatch) -> list[str]:
    """记下 copy_to_disk 的调用（防「启动第二份重复复制」）。"""
    calls: list[str] = []
    real = svc.copy_to_disk

    def counting(att, **kwargs):
        calls.append(att.id)
        return real(att, **kwargs)

    monkeypatch.setattr(svc, "copy_to_disk", counting)
    return calls


# -- 1. 等正在进行的首次准备 -------------------------------------------------


async def test_bind_waits_for_first_preparation_then_binds_ready(
    svc: AttachmentService, tmp_path: Path, monkeypatch
):
    # 必须大于复制循环的让出粒度（YIELD_EVERY_BYTES = 4MB），否则闸门根本不会被走到
    body = "首次准备的内容".encode("utf-8") + bytes(5 * 1024 * 1024)
    att = svc.prepare(str(_write(tmp_path / "报告.txt", body)), topic_id="t1")
    assert att.state == "prepared"
    entered, release = _gate_first_copy(monkeypatch)
    copies = _count_copies(svc, monkeypatch)

    prep = asyncio.create_task(_first_prepare(svc, att.id))
    assert await asyncio.to_thread(entered.wait, GATE_DEADLINE), "复制没有进到闸门"

    bind = asyncio.create_task(
        svc.bind_for_turn(turn_id="turn_new", attachment_ids=[att.id], topic_id="t1")
    )
    await asyncio.sleep(0.3)
    assert not bind.done(), "首次准备还没完成时不得把这一轮当成就绪"
    assert svc.get(att.id, check=False).turn_id is None, "准备期间不得先写归属"
    assert len(copies) == 1, "不得启动第二份重复复制"

    release.set()
    outcome = await asyncio.wait_for(bind, timeout=10)
    await asyncio.wait_for(prep, timeout=10)

    assert outcome.rejected == [], outcome.rejected
    assert outcome.bound == [att.id]
    assert len(copies) == 1, "整个过程中只应有一份复制"
    row = svc.get(att.id, check=False)
    assert row.state == "ready"
    assert Path(str(row.stored_path)).read_bytes() == body


# -- 2. 有界等待 → 结构化拒绝 ------------------------------------------------


async def test_bind_times_out_with_structured_rejection(
    svc: AttachmentService, tmp_path: Path, monkeypatch
):
    src = _write(tmp_path / "很慢.bin", b"x" * (8 * 1024 * 1024))  # 够大到会走到让出
    att = svc.prepare(str(src), topic_id="t1")
    svc.prepare_wait_seconds = 0.2  # 有界：到点必须拒绝，不能让请求挂着
    entered, release = _gate_first_copy(monkeypatch)
    copies = _count_copies(svc, monkeypatch)

    prep = asyncio.create_task(_first_prepare(svc, att.id))
    assert await asyncio.to_thread(entered.wait, GATE_DEADLINE)

    started = time.monotonic()
    outcome = await svc.bind_for_turn(
        turn_id="turn_new", attachment_ids=[att.id], topic_id="t1"
    )
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"等待必须有界（实际 {elapsed:.2f}s）"
    assert outcome.bound == []
    assert [item[0] for item in outcome.rejected] == [att.id]
    reason = outcome.rejected[0][1]
    assert "准备" in reason, reason
    assert "重试" in reason, "可用操作=重试要写在人话原因里"
    assert outcome.rejection_code_for(att.id) == "attachment_not_ready"
    assert svc.get(att.id, check=False).turn_id is None, "被拒绝的轮次不得留下归属"
    assert len(copies) == 1, "不得为了等待而再复制一份"

    release.set()
    await asyncio.wait_for(prep, timeout=10)


# -- 3. copy 就绪的第二个条件：副本可读且大小一致 -----------------------------


async def test_ready_copy_with_wrong_size_is_not_ready(
    svc: AttachmentService, tmp_path: Path
):
    src = _write(tmp_path / "对不上.txt", b"0123456789")
    att = svc.run_prepare(svc.prepare(str(src), topic_id="t1").id)
    assert att.state == "ready"
    Path(str(att.stored_path)).write_bytes(b"012")  # 副本被截断：文件在，但内容不对

    outcome = await svc.bind_for_turn(
        turn_id="turn_new", attachment_ids=[att.id], topic_id="t1"
    )

    assert outcome.bound == []
    assert [item[0] for item in outcome.rejected] == [att.id]
    assert "大小" in outcome.rejected[0][1], outcome.rejected[0][1]
    assert svc.get(att.id, check=False).turn_id is None


async def test_ready_copy_removed_file_is_not_ready(
    svc: AttachmentService, tmp_path: Path
):
    src = _write(tmp_path / "副本没了.txt", b"abc")
    att = svc.run_prepare(svc.prepare(str(src), topic_id="t1").id)
    Path(str(att.stored_path)).unlink()

    outcome = await svc.bind_for_turn(
        turn_id="turn_new", attachment_ids=[att.id], topic_id="t1"
    )

    assert outcome.bound == []
    assert outcome.rejected, "副本不在时必须结构化拒绝，不能静默当没带附件"


# -- 4. 旧客户端缺 attachment_ids 字段的兜底绑定 ------------------------------


async def test_legacy_fallback_waits_for_preparation(
    svc: AttachmentService, tmp_path: Path, monkeypatch
):
    body = "兜底路径的内容".encode("utf-8") + bytes(5 * 1024 * 1024)
    att = svc.prepare(str(_write(tmp_path / "兜底.txt", body)), topic_id="t1")
    entered, release = _gate_first_copy(monkeypatch)

    prep = asyncio.create_task(_first_prepare(svc, att.id))
    assert await asyncio.to_thread(entered.wait, GATE_DEADLINE)

    bind = asyncio.create_task(svc.bind_for_turn(turn_id="turn_new", attachment_ids=None, topic_id="t1"))
    await asyncio.sleep(0.3)
    assert not bind.done(), "兜底绑定也不得把还没就绪的附件先绑上"

    release.set()
    outcome = await asyncio.wait_for(bind, timeout=10)
    await asyncio.wait_for(prep, timeout=10)

    assert outcome.bound == [att.id]
    row = svc.get(att.id, check=False)
    assert row.state == "ready" and Path(str(row.stored_path)).read_bytes() == body


async def test_legacy_fallback_skips_still_preparing_after_timeout(
    svc: AttachmentService, tmp_path: Path, monkeypatch
):
    """兜底路径等不到就不绑（旧客户端的语义是「能绑的才绑」），但绝不把未就绪的绑上。"""
    src = _write(tmp_path / "等不到.bin", b"y" * (8 * 1024 * 1024))
    att = svc.prepare(str(src), topic_id="t1")
    svc.prepare_wait_seconds = 0.2
    entered, release = _gate_first_copy(monkeypatch)

    prep = asyncio.create_task(_first_prepare(svc, att.id))
    assert await asyncio.to_thread(entered.wait, GATE_DEADLINE)

    outcome = await svc.bind_for_turn(turn_id="turn_new", attachment_ids=None, topic_id="t1")

    assert outcome.bound == [], "还没就绪的附件不得被兜底绑上"
    assert (svc.get(att.id, check=False).turn_id or None) is None

    release.set()
    await asyncio.wait_for(prep, timeout=10)


# -- 5. 显式空列表仍然是「不带附件」（不因为就绪规则被改坏） -------------------


async def test_explicit_empty_list_is_still_no_attachments(
    svc: AttachmentService, tmp_path: Path
):
    att = svc.prepare(str(_write(tmp_path / "不想带.txt", b"z")), topic_id="t1")

    outcome = await svc.bind_for_turn(turn_id="turn_new", attachment_ids=[], topic_id="t1")

    assert outcome.bound == [] and outcome.rejected == []
    assert svc.get(att.id, check=False).turn_id is None


# -- 6. 真实 HTTP：闸门关闭时不得放行（Lead 的复现装置） ------------------------


class _CountingAdapter:
    """假 provider：只记调用次数（模型一旦被调用就说明「先执行了」）。"""

    mode = "text"
    model = "fake-count"
    supports_stream = False

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, tools, **kwargs):
        self.calls += 1
        return Completion(message=ChatMessage(role="assistant", content="收到"))


@pytest.fixture()
def async_app(tmp_path: Path):
    conn = connect(tmp_path / "readiness_http.db")
    apply_migrations(conn)
    from agent.config import Settings

    app = create_app(Settings(data_dir=tmp_path / "data"), conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    # 测试污染防护：QIO_DATA_DIR 会覆盖 Settings(data_dir=...)，再钉一次附件落点
    app.state.ctx.attachments.data_dir = tmp_path / "data"
    return app


@asynccontextmanager
async def _live(app):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
            yield ac


def _turn_starts(ctx) -> list:
    return [e for e in ctx.bus._history if e.type.value == "TURN_START"]


async def test_http_send_waits_for_first_preparation_instead_of_running(
    async_app, tmp_path: Path, monkeypatch
):
    """真实 POST /api/attachments（本地路径）+ 真实 POST /api/turns：

    复制闸门仍关闭时 —— 请求不得受理、模型 0 次调用、无 TURN_START、附件行不得被绑；
    释放闸门后 —— 受理成功、附件 ready、模型才被调用。
    """
    ctx = async_app.state.ctx
    topic = ctx.topics.nodes.create_topic("就绪话题").id
    adapter = _CountingAdapter()
    ctx.build_adapter = AsyncMock(return_value=adapter)

    source = tmp_path / "大文件.bin"
    source.write_bytes(bytes(5 * 1024 * 1024))  # > 复制循环的让出粒度，闸门才会被走到
    entered, release = _gate_first_copy(monkeypatch)

    async with _live(async_app) as ac:
        created = (
            await ac.post("/api/attachments", json={"source_path": str(source), "topic_id": topic})
        ).json()["attachment"]
        assert created["state"] == "prepared"
        assert await asyncio.to_thread(entered.wait, GATE_DEADLINE), "复制没有进到闸门"

        send = asyncio.create_task(
            ac.post(
                "/api/turns",
                json={
                    "message": "带附件发送",
                    "topic_id": topic,
                    "attachment_ids": [created["id"]],
                },
            )
        )
        # 让「先执行」这种缺陷有机会发生（闸门仍关着）
        await asyncio.sleep(1.0)

        assert adapter.calls == 0, "附件还没就绪，模型一次都不能被调用"
        assert _turn_starts(ctx) == [], "附件还没就绪，不得发 TURN_START"
        assert not send.done(), "附件还没就绪，请求不得被受理"
        row = ctx.attachments.get(created["id"], check=False)
        assert row is not None and row.turn_id is None, "准备期间不得先把归属写上"

        release.set()
        resp = await asyncio.wait_for(send, timeout=GATE_DEADLINE)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["accepted"] is True
        assert body["bound_attachment_ids"] == [created["id"]]
        row = ctx.attachments.get(created["id"], check=False)
        assert row.state == "ready" and row.turn_id == body["turn_id"]

        # 放行之后模型才开始（假 provider 只回答一次）
        for _ in range(400):
            if adapter.calls:
                break
            await asyncio.sleep(0.02)
        assert adapter.calls == 1, "放行之后应当正常执行一轮"

        # 收尾：别把这一轮留在队列里
        ctx.turns.cancel(body["turn_id"])
        for _ in range(200):
            if ctx.turns.active is None:
                break
            await asyncio.sleep(0.02)


# -- 7. 真实 HTTP：旧客户端兜底 / 多附件最后一个未就绪（同样的闸门，**不设超时**）-------


async def test_http_old_client_missing_field_waits_for_preparation(
    async_app, tmp_path: Path, monkeypatch
):
    """缺 attachment_ids 字段的兜底路径：闸门关闭时不得开始执行，放行后必须真的绑上。"""
    ctx = async_app.state.ctx
    topic = ctx.topics.nodes.create_topic("兜底话题").id
    adapter = _CountingAdapter()
    ctx.build_adapter = AsyncMock(return_value=adapter)

    source = tmp_path / "兜底大文件.bin"
    source.write_bytes(bytes(5 * 1024 * 1024))
    entered, release = _gate_first_copy(monkeypatch)

    async with _live(async_app) as ac:
        created = (
            await ac.post("/api/attachments", json={"source_path": str(source), "topic_id": topic})
        ).json()["attachment"]
        assert await asyncio.to_thread(entered.wait, GATE_DEADLINE)

        send = asyncio.create_task(
            ac.post("/api/turns", json={"message": "旧客户端发送（缺字段）", "topic_id": topic})
        )
        await asyncio.sleep(1.0)
        assert adapter.calls == 0, "兜底路径也不得在附件就绪前开始执行"
        assert _turn_starts(ctx) == []
        assert not send.done(), "兜底路径同样要等首次准备"

        release.set()
        resp = await asyncio.wait_for(send, timeout=GATE_DEADLINE)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["bound_attachment_ids"] == [created["id"]], "放行后必须真的绑上"
        assert ctx.attachments.get(created["id"], check=False).state == "ready"

        for _ in range(400):
            if adapter.calls:
                break
            await asyncio.sleep(0.02)
        assert adapter.calls == 1
        ctx.turns.cancel(body["turn_id"])
        for _ in range(200):
            if ctx.turns.active is None:
                break
            await asyncio.sleep(0.02)


async def test_http_multi_attachment_last_preparing_blocks(
    async_app, tmp_path: Path, monkeypatch
):
    """多附件里**最后一个**还没就绪：整轮不得开始执行，也不能半绑。"""
    ctx = async_app.state.ctx
    topic = ctx.topics.nodes.create_topic("多附件话题").id
    adapter = _CountingAdapter()
    ctx.build_adapter = AsyncMock(return_value=adapter)

    small = tmp_path / "已就绪.txt"
    small.write_text("早就准备好了", encoding="utf-8")
    big = tmp_path / "还在准备.bin"
    big.write_bytes(bytes(5 * 1024 * 1024))

    entered, release = _gate_first_copy(monkeypatch)
    async with _live(async_app) as ac:
        # 第一条：正常准备到 ready
        first = (
            await ac.post("/api/attachments", json={"source_path": str(small), "topic_id": topic})
        ).json()["attachment"]
        for _ in range(400):
            row = (await ac.get(f"/api/attachments/{first['id']}")).json()["attachment"]
            if row["state"] == "ready":
                break
            await asyncio.sleep(0.02)
        assert row["state"] == "ready"

        # 第二条：闸门卡在首次复制里
        second = (
            await ac.post("/api/attachments", json={"source_path": str(big), "topic_id": topic})
        ).json()["attachment"]
        assert await asyncio.to_thread(entered.wait, GATE_DEADLINE)

        send = asyncio.create_task(
            ac.post(
                "/api/turns",
                json={
                    "message": "多附件最后一个未就绪",
                    "topic_id": topic,
                    "attachment_ids": [first["id"], second["id"]],
                },
            )
        )
        await asyncio.sleep(1.0)
        assert adapter.calls == 0, "最后一条没就绪时整轮不得开始执行"
        assert _turn_starts(ctx) == []
        assert not send.done()
        assert ctx.attachments.get(first["id"], check=False).turn_id is None, "也不得先半绑第一条"

        release.set()
        resp = await asyncio.wait_for(send, timeout=GATE_DEADLINE)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["bound_attachment_ids"] == [first["id"], second["id"]]
        for attachment_id in (first["id"], second["id"]):
            assert ctx.attachments.get(attachment_id, check=False).turn_id == body["turn_id"]

        for _ in range(400):
            if adapter.calls:
                break
            await asyncio.sleep(0.02)
        assert adapter.calls == 1
        ctx.turns.cancel(body["turn_id"])
        for _ in range(200):
            if ctx.turns.active is None:
                break
            await asyncio.sleep(0.02)


# -- 8. 兼容路径（旧客户端缺 attachment_ids）：固定集合 + 整轮拒绝（R8 §1.3） -----


def _fail_copy_at_commit(monkeypatch, *, errno_value: int = errno.ENOSPC) -> None:
    """让首次复制的**提交步骤**以真实 OSError 失败 → 服务自己走 failed 收尾（不是造状态）。"""
    real_replace = attachments_mod.os.replace

    def failing_replace(src, dst):
        raise OSError(errno_value, "no space left on device")

    monkeypatch.setattr(attachments_mod.os, "replace", failing_replace)
    return real_replace


async def _compat_bind(svc: AttachmentService, topic_id: str = "t1", turn_id: str = "turn_new"):
    return await svc.bind_for_turn(turn_id=turn_id, attachment_ids=None, topic_id=topic_id)


async def test_compat_failure_during_wait_rejects_whole_turn(
    svc: AttachmentService, tmp_path: Path, monkeypatch
):
    """等待期间首次准备**真实 failed** → 整轮结构化拒绝（修复前：静默丢掉、照常执行）。"""
    src = _write(tmp_path / "会失败.bin", b"x" * (5 * 1024 * 1024))
    att = svc.prepare(str(src), topic_id="t1")
    entered, release = _gate_first_copy(monkeypatch)
    _fail_copy_at_commit(monkeypatch)

    prep = asyncio.create_task(_first_prepare(svc, att.id))
    assert await asyncio.to_thread(entered.wait, GATE_DEADLINE)

    bind = asyncio.create_task(_compat_bind(svc))
    await asyncio.sleep(0.3)
    assert not bind.done(), "准备还没结束，兼容路径也不得先放行"

    release.set()  # 复制以真实 failed 收尾
    outcome = await asyncio.wait_for(bind, timeout=10)
    await asyncio.wait_for(prep, timeout=10)

    assert outcome.bound == [], "失败的附件不得被当成「这一轮没有附件」"
    assert [item[0] for item in outcome.rejected] == [att.id]
    reason = outcome.rejected[0][1]
    assert reason and ("失败" in reason or "磁盘" in reason or "空间" in reason), reason
    assert svc.get(att.id, check=False).turn_id is None, "被拒的轮次不得留下归属"
    assert svc.get(att.id, check=False).state == "failed"


async def test_compat_cancelled_during_wait_rejects(svc: AttachmentService, tmp_path: Path, monkeypatch):
    src = _write(tmp_path / "被取消.bin", b"x" * (5 * 1024 * 1024))
    att = svc.prepare(str(src), topic_id="t1")
    entered, release = _gate_first_copy(monkeypatch)
    prep = asyncio.create_task(_first_prepare(svc, att.id))
    assert await asyncio.to_thread(entered.wait, GATE_DEADLINE)

    bind = asyncio.create_task(_compat_bind(svc))
    await asyncio.sleep(0.3)
    svc.apply_outcome(att.id, DiskOutcome(state="cancelled", error="已取消（可以重试）"))
    outcome = await asyncio.wait_for(bind, timeout=10)

    assert outcome.bound == []
    assert [item[0] for item in outcome.rejected] == [att.id]
    assert "取消" in outcome.rejected[0][1], outcome.rejected[0][1]

    release.set()
    await asyncio.wait_for(prep, timeout=10)


async def test_compat_deleted_during_wait_rejects(svc: AttachmentService, tmp_path: Path, monkeypatch):
    src = _write(tmp_path / "被删除.bin", b"x" * (5 * 1024 * 1024))
    att = svc.prepare(str(src), topic_id="t1")
    entered, release = _gate_first_copy(monkeypatch)
    prep = asyncio.create_task(_first_prepare(svc, att.id))
    assert await asyncio.to_thread(entered.wait, GATE_DEADLINE)

    bind = asyncio.create_task(_compat_bind(svc))
    await asyncio.sleep(0.3)
    svc.delete(att.id)
    outcome = await asyncio.wait_for(bind, timeout=10)

    assert outcome.bound == []
    assert [item[0] for item in outcome.rejected] == [att.id]
    assert "删除" in outcome.rejected[0][1], outcome.rejected[0][1]

    release.set()
    await asyncio.wait_for(prep, timeout=10)


async def test_compat_timeout_rejects_structurally(svc: AttachmentService, tmp_path: Path, monkeypatch):
    src = _write(tmp_path / "等不到.bin", b"x" * (5 * 1024 * 1024))
    att = svc.prepare(str(src), topic_id="t1")
    svc.prepare_wait_seconds = 0.2
    entered, release = _gate_first_copy(monkeypatch)
    prep = asyncio.create_task(_first_prepare(svc, att.id))
    assert await asyncio.to_thread(entered.wait, GATE_DEADLINE)

    started = time.monotonic()
    outcome = await _compat_bind(svc)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"等待必须有界（实际 {elapsed:.2f}s）"
    assert outcome.bound == []
    assert [item[0] for item in outcome.rejected] == [att.id]
    assert "准备" in outcome.rejected[0][1]
    assert outcome.rejection_code_for(att.id) == "attachment_not_ready"

    release.set()
    await asyncio.wait_for(prep, timeout=10)


async def test_compat_unreadable_sibling_during_wait_rejects(
    svc: AttachmentService, tmp_path: Path, monkeypatch
):
    """等待期间另一条附件变得不可读（副本被删）→ 整轮拒绝（不得静默少带一个）。"""
    broken = svc.run_prepare(svc.prepare(str(_write(tmp_path / "副本没了.txt", b"abc")), topic_id="t1").id)
    assert broken.state == "ready"
    src = _write(tmp_path / "慢.bin", b"x" * (5 * 1024 * 1024))
    slow = svc.prepare(str(src), topic_id="t1")
    entered, release = _gate_first_copy(monkeypatch)
    prep = asyncio.create_task(_first_prepare(svc, slow.id))
    assert await asyncio.to_thread(entered.wait, GATE_DEADLINE)

    bind = asyncio.create_task(_compat_bind(svc))
    await asyncio.sleep(0.3)
    Path(str(broken.stored_path)).unlink()  # 等待期间副本消失 → 不可读
    release.set()  # 慢的那条准备成功；但集合里另一条已经不可读 → 整轮拒绝
    outcome = await asyncio.wait_for(bind, timeout=10)
    await asyncio.wait_for(prep, timeout=10)

    assert outcome.bound == [], "整轮拒绝时不得半绑另一条"
    assert broken.id in {item[0] for item in outcome.rejected}, outcome.rejected
    assert svc.get(slow.id, check=False).turn_id is None


async def test_compat_ownership_change_during_wait_rejects(
    svc: AttachmentService, tmp_path: Path, monkeypatch
):
    """等待期间另一条被别的轮次占用 → 整轮拒绝（快照内的附件必须是本次能带的）。"""
    other = svc.run_prepare(svc.prepare(str(_write(tmp_path / "别的轮.txt", b"o")), topic_id="t1").id)
    src = _write(tmp_path / "慢2.bin", b"x" * (5 * 1024 * 1024))
    slow = svc.prepare(str(src), topic_id="t1")
    entered, release = _gate_first_copy(monkeypatch)
    prep = asyncio.create_task(_first_prepare(svc, slow.id))
    assert await asyncio.to_thread(entered.wait, GATE_DEADLINE)

    bind = asyncio.create_task(_compat_bind(svc))
    await asyncio.sleep(0.3)
    await svc.bind_for_turn(turn_id="turn_other", attachment_ids=[other.id], topic_id="t1")
    release.set()  # 慢的那条准备成功；但集合里另一条已被别的轮次占用 → 整轮拒绝
    outcome = await asyncio.wait_for(bind, timeout=10)
    await asyncio.wait_for(prep, timeout=10)

    assert outcome.bound == [], "整轮拒绝时不得半绑"
    assert other.id in {item[0] for item in outcome.rejected}, outcome.rejected
    assert "别的一轮" in dict(outcome.rejected)[other.id], dict(outcome.rejected)[other.id]
    assert svc.get(other.id, check=False).turn_id == "turn_other", "原来那条的归属不动"


async def test_compat_multi_one_fails_rejects_whole_turn_without_half_binding(
    svc: AttachmentService, tmp_path: Path, monkeypatch
):
    healthy = svc.run_prepare(svc.prepare(str(_write(tmp_path / "好的.txt", b"ok")), topic_id="t1").id)
    src = _write(tmp_path / "坏的.bin", b"x" * (5 * 1024 * 1024))
    doomed = svc.prepare(str(src), topic_id="t1")
    entered, release = _gate_first_copy(monkeypatch)
    _fail_copy_at_commit(monkeypatch)

    prep = asyncio.create_task(_first_prepare(svc, doomed.id))
    assert await asyncio.to_thread(entered.wait, GATE_DEADLINE)
    bind = asyncio.create_task(_compat_bind(svc))
    await asyncio.sleep(0.3)
    release.set()
    outcome = await asyncio.wait_for(bind, timeout=10)
    await asyncio.wait_for(prep, timeout=10)

    assert outcome.bound == [], "有任何一条不合格就整轮拒绝，不得半绑"
    assert [item[0] for item in outcome.rejected] == [doomed.id]
    assert svc.get(healthy.id, check=False).turn_id is None, "好的那条必须还能重发"
    # 好的那条单独（显式列表）重发：正常绑上
    again = await svc.bind_for_turn(turn_id="turn_next", attachment_ids=[healthy.id], topic_id="t1")
    assert again.bound == [healthy.id] and again.rejected == []


async def test_compat_ignores_historical_terminal_failures(
    svc: AttachmentService, tmp_path: Path
):
    """进入时就已经 failed / cancelled / missing 的**历史记录**不得阻断纯文字发送。"""
    src = _write(tmp_path / "早就失败.txt", b"abc")
    failed = svc.run_prepare(svc.prepare(str(src), topic_id="t1").id)
    # 副本也清掉：否则「副本在且 sha256 一致」会按 R6 §1.4 的可验证恢复变成 ready
    Path(str(failed.stored_path)).unlink()
    svc._update(failed.id, state="failed", error="早先就失败了")
    gone = svc.run_prepare(svc.prepare(str(_write(tmp_path / "早就丢了.txt", b"abc")), topic_id="t1").id)
    Path(str(gone.stored_path)).unlink()

    outcome = await _compat_bind(svc)

    assert outcome.rejected == [], f"历史失败不应阻断：{outcome.rejected}"
    assert outcome.bound == [], "它们也不是「本次应携带的附件」"


async def test_compat_empty_snapshot_runs_without_attachments(
    svc: AttachmentService, tmp_path: Path
):
    outcome = await _compat_bind(svc)
    assert outcome.bound == [] and outcome.rejected == []


# -- 9. 真实 API：旧客户端发送 + 首次准备真实失败 → 整轮拒绝 ---------------------


async def test_http_legacy_send_rejects_when_preparation_really_fails(
    async_app, tmp_path: Path, monkeypatch
):
    """Lead 的反例：登记附件 → 首次复制卡在闸门 → 旧客户端发送（不带 attachment_ids）
    → 等待期间复制以**真实服务结果 failed** 收尾。

    修复前：HTTP 200 / accepted=true / rejected=[] / bound=[] / 模型 1 次（失败附件被静默丢掉）。
    修复后：结构化拒绝整轮 —— 无模型调用、无 TURN_START、无绑定；草稿可重试。
    """
    ctx = async_app.state.ctx
    topic = ctx.topics.nodes.create_topic("兼容失败话题").id
    adapter = _CountingAdapter()
    ctx.build_adapter = AsyncMock(return_value=adapter)

    source = tmp_path / "会失败.bin"
    source.write_bytes(bytes(5 * 1024 * 1024))  # > 让出粒度：闸门才会被走到
    entered, release = _gate_first_copy(monkeypatch)
    _fail_copy_at_commit(monkeypatch)

    async with _live(async_app) as ac:
        created = (
            await ac.post("/api/attachments", json={"source_path": str(source), "topic_id": topic})
        ).json()["attachment"]
        assert created["state"] == "prepared"
        assert await asyncio.to_thread(entered.wait, GATE_DEADLINE)

        send = asyncio.create_task(
            ac.post("/api/turns", json={"message": "旧客户端发送（缺字段）", "topic_id": topic})
        )
        await asyncio.sleep(1.0)
        assert not send.done(), "首次准备还没结束，兼容路径不得先受理"
        assert adapter.calls == 0, "准备未完成时模型一次都不能被调用"
        assert _turn_starts(ctx) == []

        release.set()  # 复制以真实 OSError 失败收尾
        resp = await asyncio.wait_for(send, timeout=GATE_DEADLINE)

        # 失败时把**执行事实**一并打印出来（修复前正是「200 + 模型 1 次」这种形态）
        assert resp.status_code == 409, {
            "http_status": resp.status_code,
            "body": resp.text[:220],
            "model_calls": adapter.calls,
            "turn_starts": len(_turn_starts(ctx)),
        }
        detail = resp.json()["detail"]
        assert [item["id"] for item in detail["rejected"]] == [created["id"]], detail
        assert detail["rejected"][0]["reason"], "必须有人话原因"
        assert adapter.calls == 0, "被拒绝的轮次不得调用模型"
        assert _turn_starts(ctx) == [], "被拒绝的轮次不得发 TURN_START"
        row = ctx.attachments.get(created["id"], check=False)
        assert row.state == "failed" and row.turn_id is None, "失败附件不得被静默吞掉"

        # 草稿可重试：这条失败已经是**历史记录**，不阻断后续发送
        monkeypatch.undo()  # 撤掉闸门与提交故障（本轮要验证的是重试路径）
        again = await ac.post("/api/turns", json={"message": "旧客户端重试", "topic_id": topic})
        assert again.status_code == 200, again.text
        assert again.json()["bound_attachment_ids"] == []
        for _ in range(400):
            if adapter.calls:
                break
            await asyncio.sleep(0.02)
        assert adapter.calls == 1, "重试应当正常执行一轮"
        ctx.turns.cancel(again.json()["turn_id"])
        for _ in range(200):
            if ctx.turns.active is None:
                break
            await asyncio.sleep(0.02)
