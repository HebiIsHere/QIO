"""附件就绪后才放行执行（R6 §1.1）：预留 → 准备 → 放行。

真缺陷（plan §0 第 1 条）：turns/resend 路由**先 submit 再 await bind_for_turn** ——
bind 异步化之后，等复制会让出事件循环，于是模型可以在附件就绪前就开始执行
（TURN_START + 模型调用），甚至带着不可读的附件跑完这一轮。

本文件的时序全部用**事件闸门**控制（threading.Event 卡住工作线程里的复制；
asyncio.Event 控制请求体/放行），有限超时只用于判定失败。
"""

from __future__ import annotations

import asyncio
import contextlib
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
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

MARKER = "R6 原轮副本内容：只有这份副本里才有的标记 5b21"
#: 被测性质的有界期限（事件/闸门驱动；超时 = 失败）
DEADLINE = 15.0
#: 前置条件（线程起跑 / 状态出现）的上界
SETUP_DEADLINE = 30.0


class _CountingAdapter:
    """假 provider：记调用次数，并在被调用那一刻记录「本轮克隆是否已就绪」。"""

    mode = "text"
    model = "fake-count"
    supports_stream = False

    def __init__(self, on_call=None) -> None:
        self.calls = 0
        self.ready_at_call: bool | None = None
        self.asked: list[str] = []
        self._on_call = on_call

    async def complete(self, messages, tools, **kwargs):
        self.calls += 1
        try:
            # 取**最后一条 user 消息**（messages[-1] 往往是系统提示，不能当轮次身份用）
            self.asked.append(
                next(
                    (str(m.content) for m in reversed(messages) if getattr(m, "role", "") == "user"),
                    "?",
                )
            )
        except Exception:  # noqa: BLE001 - 诊断信息，拿不到就算了
            self.asked.append("?")
        if self._on_call is not None:
            self._on_call()
        return Completion(message=ChatMessage(role="assistant", content="收到"))


@pytest.fixture()
def async_app(tmp_path: Path):
    conn = connect(tmp_path / "r6_readiness.db")
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


async def _wait_until(predicate, *, timeout: float = SETUP_DEADLINE, what: str = ""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    raise AssertionError(f"没有在 {timeout}s 内满足：{what}")


async def _settle_forbidden_window(ctx, adapter, *, seconds: float = 3.0) -> None:
    """把事件循环让出去一小段，让**禁止出现的副作用**（TURN_START / 模型调用）有机会发生。

    这不是就绪判据（就绪靠闸门与状态事实）；它只是让「先执行」这种缺陷能被确定性地看见。
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        if adapter.calls or _turn_starts(ctx):
            return
        await asyncio.sleep(0.01)


async def _ready_copy(ac, ctx, tmp_path: Path, topic_id: str, name: str = "原轮副本.txt") -> tuple[Path, dict]:
    """登记一份 ready 的 copy 附件（真实 HTTP + 真实 prepare）。"""
    source = tmp_path / name
    source.write_text(MARKER, encoding="utf-8")
    created = (
        await ac.post("/api/attachments", json={"source_path": str(source), "topic_id": topic_id})
    ).json()["attachment"]
    meta = created
    for _ in range(400):
        meta = (await ac.get(f"/api/attachments/{created['id']}")).json()["attachment"]
        if meta["state"] != "prepared":
            break
        await asyncio.sleep(0.02)
    assert meta["state"] == "ready", meta
    return source, meta


def _turn_starts(ctx) -> list:
    return [e for e in ctx.bus._history if e.type.value == "TURN_START"]


def _events_of(ctx, name: str, turn_id: str) -> list:
    """事件历史里的**不可逆事实**（发出去就一直在），不随瞬时状态消失。"""
    return [
        e
        for e in ctx.bus._history
        if e.type.value == name and str(e.data.get("turn_id") or "") == turn_id
    ]


def _clones_of(ctx, source_id: str) -> list:
    return [
        row
        for row in ctx.attachments.list(limit=50, check=False)
        if getattr(row, "source_attachment_id", None) == source_id
    ]


def _stored_files(ctx) -> list:
    """附件目录里的实际文件（用于「克隆副本有没有被清掉」这类收敛判据）。"""
    return [p for p in ctx.attachments.root.rglob("*") if p.is_file()]


def _readable(att) -> bool:
    return bool(att.stored_path) and Path(str(att.stored_path)).is_file()


def _break_link(monkeypatch) -> None:
    """强制走复制退路（os.link 失败 → copyfile）：只影响 attachments 服务看到的 os。"""
    import os as real_os

    class _OsShim:
        def __getattr__(self, name):
            return getattr(real_os, name)

        @staticmethod
        def link(*_args, **_kwargs):
            raise OSError("跨卷 / 不支持硬链接（测试强制复制退路）")

    monkeypatch.setattr(attachments_mod, "os", _OsShim())


def _gate_copy(monkeypatch) -> tuple[threading.Event, threading.Event]:
    """把**复制本身**卡在工作线程里（独立线程池），返回 entered / release。

    闸门放在复制里 → 能走到服务自己的取消清理路径（客户端断开时克隆会被丢弃）。
    但必须用**独立线程池**跑复制：默认执行器/重活池被占住时会连带卡住编排器的上下文装配，
    那样观测到的就不是「附件没准备好」，而是装置副作用。
    """
    import asyncio as real_asyncio
    import os as real_os
    import shutil as real_shutil
    from concurrent.futures import ThreadPoolExecutor
    from functools import partial

    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qio-test-copy")
    entered = threading.Event()
    release = threading.Event()
    real_copyfile = real_shutil.copyfile

    def gated_copyfile(src, dst, *args, **kwargs):
        entered.set()
        assert release.wait(SETUP_DEADLINE), "测试没有放行 copyfile"
        return real_copyfile(src, dst, *args, **kwargs)

    class _ShutilShim:
        copyfile = staticmethod(gated_copyfile)

        def __getattr__(self, name):
            return getattr(real_shutil, name)

    class _OsShim:
        def __getattr__(self, name):
            return getattr(real_os, name)

        @staticmethod
        def link(*_args, **_kwargs):
            raise OSError("不支持硬链接（测试强制复制退路）")

    class _AsyncioShim:
        def __getattr__(self, name):
            return getattr(real_asyncio, name)

        @staticmethod
        def to_thread(func, /, *args, **kwargs):
            loop = real_asyncio.get_running_loop()
            return loop.run_in_executor(pool, partial(func, *args, **kwargs))

    monkeypatch.setattr(attachments_mod, "shutil", _ShutilShim())
    monkeypatch.setattr(attachments_mod, "os", _OsShim())
    monkeypatch.setattr(attachments_mod, "asyncio", _AsyncioShim())
    return entered, release


def _gate_finalize(monkeypatch) -> tuple[asyncio.Event, asyncio.Event]:
    """把准备卡在**定稿前**（事件循环侧），返回 entered / release。

    复制本身照常跑完（走真实的 copyfile 退路），但 bind_for_turn 不会返回：
    行仍是 prepared、没有可读的 stored_path —— 「准备未完成」这个状态干净地持续存在。

    闸门**不放在复制线程里**：那会占住共享资源（实测会把编排器的上下文装配一起卡住），
    于是「模型有没有被提前调用」就测不出来了。放在事件循环侧的等待不占执行器、不占数据库，
    编排器与其它 API 都能照常推进。
    """
    entered = asyncio.Event()
    release = asyncio.Event()
    real = attachments_mod.AttachmentService._finish_copy_clone

    async def gated(self, plan):
        entered.set()
        await release.wait()
        return await real(self, plan)

    monkeypatch.setattr(attachments_mod.AttachmentService, "_finish_copy_clone", gated)
    return entered, release


def _gate_first_finalize(monkeypatch) -> tuple[asyncio.Event, asyncio.Event]:
    """只卡住**第一次**准备（用于 FIFO：第二份先就绪也不能先跑）。"""
    entered = asyncio.Event()
    release = asyncio.Event()
    real = attachments_mod.AttachmentService._finish_copy_clone
    state = {"used": False}

    async def gated(self, plan):
        if not state["used"]:
            state["used"] = True
            entered.set()
            await release.wait()
        return await real(self, plan)

    monkeypatch.setattr(attachments_mod.AttachmentService, "_finish_copy_clone", gated)
    return entered, release


def _break_copy(monkeypatch) -> None:
    """让复制真的失败（os.link 与 copyfile 都失败）：准备必须变成结构化拒绝。"""
    import os as real_os
    import shutil as real_shutil

    class _ShutilShim:
        @staticmethod
        def copyfile(*_args, **_kwargs):
            raise OSError("磁盘写入失败（测试注入）")

        def __getattr__(self, name):
            return getattr(real_shutil, name)

    class _OsShim:
        def __getattr__(self, name):
            return getattr(real_os, name)

        @staticmethod
        def link(*_args, **_kwargs):
            raise OSError("不支持硬链接（测试注入）")

    monkeypatch.setattr(attachments_mod, "shutil", _ShutilShim())
    monkeypatch.setattr(attachments_mod, "os", _OsShim())


async def test_activation_keeps_reservation_order(async_app, tmp_path, monkeypatch):
    """FIFO：后面那一轮先准备好也不能先跑 —— 放行按**预留顺序**。"""
    ctx = async_app.state.ctx
    topic = ctx.topics.nodes.create_topic("FIFO 话题").id
    adapter = _CountingAdapter()
    ctx.build_adapter = AsyncMock(return_value=adapter)

    async with _live(async_app) as ac:
        src_a, att_a = await _ready_copy(ac, ctx, tmp_path, topic, name="第一份.txt")
        src_b, att_b = await _ready_copy(ac, ctx, tmp_path, topic, name="第二份.txt")
        await ctx.attachments.bind_for_turn("turn_r6_a", [att_a["id"]], topic_id=topic)
        await ctx.attachments.bind_for_turn("turn_r6_b", [att_b["id"]], topic_id=topic)
        src_a.unlink()
        src_b.unlink()
        _break_link(monkeypatch)
        entered, release = _gate_first_finalize(monkeypatch)

        first = asyncio.create_task(
            ac.post(
                "/api/turns",
                json={
                    "message": "重试 A",
                    "topic_id": topic,
                    "attachment_ids": [att_a["id"]],
                    "retry_of_turn_id": "turn_r6_a",
                },
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=DEADLINE)

        # 第二个请求：准备先完成、HTTP 也先返回 —— 但它**不能**先开始执行（FIFO）
        second = asyncio.create_task(
            ac.post(
                "/api/turns",
                json={
                    "message": "重试 B",
                    "topic_id": topic,
                    "input": None,
                    "attachment_ids": [att_b["id"]],
                    "retry_of_turn_id": "turn_r6_b",
                },
            )
        )
        resp_second = await asyncio.wait_for(second, timeout=DEADLINE)
        assert resp_second.status_code == 200, resp_second.text

        await _settle_forbidden_window(ctx, adapter, seconds=0.8)
        assert adapter.calls == 0, "前面那一轮还没放行，后面的轮次不得开始执行（FIFO 乱序）"
        assert not _turn_starts(ctx), "FIFO：后预留的轮次不得先发 TURN_START"

        release.set()
        resp_first = await asyncio.wait_for(first, timeout=DEADLINE)
        assert resp_first.status_code == 200, resp_first.text
        await _wait_until(
            lambda: len(adapter.asked) >= 2, timeout=SETUP_DEADLINE, what="两轮没有都执行"
        )

    # 编排器会把注入（记忆/附件说明）拼在用户消息里，所以只看「消息文本在不在里面」
    assert "重试 A" in adapter.asked[0], ("放行顺序必须按预留顺序", adapter.asked)
    assert "重试 B" in adapter.asked[1], ("放行顺序必须按预留顺序", adapter.asked)


async def test_prepare_failure_is_rejected_before_anything_starts(async_app, tmp_path, monkeypatch):
    """准备失败 → 结构化拒绝、不入队、模型零调用、台账不留「像被中断的一轮」。"""
    ctx = async_app.state.ctx
    topic = ctx.topics.nodes.create_topic("准备失败").id
    adapter = _CountingAdapter()
    ctx.build_adapter = AsyncMock(return_value=adapter)

    async with _live(async_app) as ac:
        source, att = await _ready_copy(ac, ctx, tmp_path, topic, name="会失败的副本.txt")
        await ctx.attachments.bind_for_turn("turn_r6_fail", [att["id"]], topic_id=topic)
        source.unlink()
        _break_copy(monkeypatch)

        resp = await ac.post(
            "/api/turns",
            json={
                "message": "重试（复制会失败）",
                "topic_id": topic,
                "attachment_ids": [att["id"]],
                "retry_of_turn_id": "turn_r6_fail",
            },
        )
        await _settle_forbidden_window(ctx, adapter, seconds=0.4)

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    # 顶层 code：B 交付具体拒绝 code（attachment_not_ready）之后要透传；
    # 在他那份改动落到本 worktree 之前，保持既有的通用 code —— 两种都算「结构化拒绝」。
    assert detail["code"] in ("attachment_binding_failed", "attachment_not_ready"), detail
    assert detail["rejected"], detail
    assert adapter.calls == 0, "准备失败时模型一次都不能被调用"
    assert not _turn_starts(ctx), "准备失败时不得发 TURN_START"
    snapshot = ctx.turns.snapshot()
    assert snapshot["running"] is None and not snapshot["queued"], snapshot
    assert ctx.turn_journal.unfinished() == [], (
        "准备失败的那一轮不得留成「像被中断的一轮」",
        ctx.turn_journal.unfinished(),
    )
    clones = _clones_of(ctx, att["id"])
    assert not [c for c in clones if c.state in ("prepared", "ready")], (
        f"准备失败后不得留下可用的克隆：{[(c.id, c.state) for c in clones]}"
    )


async def test_resend_claim_survives_a_failed_prepare(async_app, tmp_path, monkeypatch):
    """resend：准备失败**不消费** claim —— 修好后还能再试一次。"""
    ctx = async_app.state.ctx
    topic = ctx.topics.nodes.create_topic("重发话题").id
    adapter = _CountingAdapter()
    ctx.build_adapter = AsyncMock(return_value=adapter)
    lost_turn = "turn_r6_lost"

    async with _live(async_app) as ac:
        source, att = await _ready_copy(ac, ctx, tmp_path, topic, name="重发副本.txt")
        await ctx.attachments.bind_for_turn(lost_turn, [att["id"]], topic_id=topic)
        source.unlink()

        # 造一条「被中断、可以重发」的台账记录
        ctx.turn_journal.accepted(
            turn_id=lost_turn, message="被中断的消息", topic_id=topic, status="queued"
        )
        ctx.turn_journal.interrupt_stale()
        assert ctx.turn_journal.recoverable(lost_turn) is not None

        # 注入只在这个上下文里生效（不用 monkeypatch.undo()：那会把同一 fixture 上
        # 别的补丁——例如 autouse 的验证桩——一起撤掉，留下隐式耦合）
        with pytest.MonkeyPatch.context() as failing:
            _break_copy(failing)
            failed = await ac.post(f"/api/turns/{lost_turn}/resend")
            assert failed.status_code == 409, failed.text
            assert ctx.turn_journal.recoverable(lost_turn) is not None, (
                "准备失败把 claim 永久消费掉了：用户再也没法重发这条消息"
            )
            assert adapter.calls == 0

        # 注入撤掉（复制恢复正常）之后：这条记录还能再试一次，并且真的跑起来
        retried = await ac.post(f"/api/turns/{lost_turn}/resend")
        assert retried.status_code == 200, retried.text
        assert retried.json()["recovered_turn_id"] == lost_turn
        await _wait_until(
            lambda: adapter.calls >= 1, timeout=SETUP_DEADLINE, what="重发成功后模型没有被调用"
        )

    assert ctx.turn_journal.recoverable(lost_turn) is None, "重发成功之后不该还能再重发"


async def test_client_disconnect_during_prepare_never_starts(async_app, tmp_path, monkeypatch):
    """准备期间客户端断开：abandon + 清理克隆；之后放行磁盘闸门也不能开始执行。"""
    ctx = async_app.state.ctx
    topic = ctx.topics.nodes.create_topic("断开话题").id
    adapter = _CountingAdapter()
    ctx.build_adapter = AsyncMock(return_value=adapter)

    async with _live(async_app) as ac:
        source, att = await _ready_copy(ac, ctx, tmp_path, topic, name="断开副本.txt")
        await ctx.attachments.bind_for_turn("turn_r6_disc", [att["id"]], topic_id=topic)
        source.unlink()
        # 闸门放在**复制内部**（独立线程池）：这样取消会走进服务自己的清理路径，
        # 克隆行/副本由 attachments 服务负责清掉 —— 路由这边只负责 abandon。
        entered, release = _gate_copy(monkeypatch)

        task = asyncio.create_task(
            ac.post(
                "/api/turns",
                json={
                    "message": "准备中断开",
                    "topic_id": topic,
                    "attachment_ids": [att["id"]],
                    "retry_of_turn_id": "turn_r6_disc",
                },
            )
        )
        # 闸门是 threading.Event（在工作线程里等）：这里必须非阻塞地等它，
        # 直接 entered.wait() 会把事件循环一起卡住（wait_for 都轮不到）。
        await _wait_until(entered.is_set, timeout=DEADLINE, what="复制没有进入工作线程闸门")
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

        release.set()  # 迟到地放行磁盘闸门：绝不能因此重新开始执行
        await _settle_forbidden_window(ctx, adapter, seconds=0.8)
        assert adapter.calls == 0, "客户端断开之后（放行闸门）仍然不能开始执行"
        assert not _turn_starts(ctx), "断开之后不得发 TURN_START"
        snapshot = ctx.turns.snapshot()
        assert snapshot["running"] is None and not snapshot["queued"], snapshot
        assert ctx.turn_journal.unfinished() == [], (
            "准备期间断开的那一轮不得留成「像被中断的一轮」",
            ctx.turn_journal.unfinished(),
        )
        await _wait_until(
            lambda: not _clones_of(ctx, att["id"]),
            timeout=SETUP_DEADLINE,
            what="断开后克隆行没有清掉",
        )
        # 副本**文件**的清理是排程的（等复制线程收尾后再删）：等它收敛，而不是断言某一瞬间
        await _wait_until(
            lambda: len(_stored_files(ctx)) == 1,
            timeout=SETUP_DEADLINE,
            what="断开后克隆副本没有被清掉（目录里应该只剩原轮那一份）",
        )

    files = _stored_files(ctx)
    assert len(files) == 1, f"断开后只该剩下原轮那份副本：{files}"


async def test_deleting_the_clone_during_prepare_is_rejected(async_app, tmp_path, monkeypatch):
    """准备期间删除附件：准备失败 → 结构化拒绝，绝不带着缺失附件执行。"""
    ctx = async_app.state.ctx
    topic = ctx.topics.nodes.create_topic("删除话题").id
    adapter = _CountingAdapter()
    ctx.build_adapter = AsyncMock(return_value=adapter)

    async with _live(async_app) as ac:
        source, att = await _ready_copy(ac, ctx, tmp_path, topic, name="删除副本.txt")
        await ctx.attachments.bind_for_turn("turn_r6_del", [att["id"]], topic_id=topic)
        source.unlink()
        _break_link(monkeypatch)
        entered, release = _gate_finalize(monkeypatch)

        task = asyncio.create_task(
            ac.post(
                "/api/turns",
                json={
                    "message": "准备期间删附件",
                    "topic_id": topic,
                    "attachment_ids": [att["id"]],
                    "retry_of_turn_id": "turn_r6_del",
                },
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=DEADLINE)
        clones = _clones_of(ctx, att["id"])
        assert clones, "没有看到这次重试的克隆行"
        removed = await ac.delete(f"/api/attachments/{clones[0].id}")
        assert removed.status_code == 200, removed.text

        release.set()
        resp = await asyncio.wait_for(task, timeout=DEADLINE)
        await _settle_forbidden_window(ctx, adapter, seconds=0.4)

    assert resp.status_code == 409, resp.text
    assert adapter.calls == 0, "准备失败时模型一次都不能被调用"
    assert not _turn_starts(ctx)
    snapshot = ctx.turns.snapshot()
    assert snapshot["running"] is None and not snapshot["queued"], snapshot
    assert ctx.turn_journal.unfinished() == []


async def test_activation_order_wait_is_bounded(async_app, monkeypatch):
    """有界等待：队首迟迟不放行时，后面已经就绪的不会被永远挡住（超时兜底放行）。

    判据只用**不可逆事实**。快照里的 queued / running 是瞬态的：兜底放行的那一轮会在
    同一个事件循环 tick 里被 worker 取走并跑完（实测：兜底触发后 runner 的 start/end
    落在同一时间戳；250ms 采样必然错过、5ms 采样也只是碰运气）—— 拿「等到看见入队」
    当判据会随机器调度随机变红（Linux CI 就是这样红的），而测不到「有没有兜底放行」。

    所以这里等的是「worker 真的开跑过」这个留在事件历史里的 TURN_START，外加：
    * 同步断言（没有 await，定时器不可能已触发）：那一刻还没入队、还没开跑；
    * 下界：兜底真的等够了时间才放行（不是立刻放行）；
    * 第一位始终没被执行 —— 兜底只放行到点的那一条，不改 FIFO 语义；
    * 第二轮如实收尾（台账不留 unfinished）—— 不可逆的收尾事实。
    """
    from agent.core import turn as turn_mod

    bound = 0.3
    monkeypatch.setattr(turn_mod, "ACTIVATION_ORDER_TIMEOUT", bound)
    ctx = async_app.state.ctx
    first = ctx.turns.reserve("第一位（永远不放行）", None)
    second = ctx.turns.reserve("第二位（就绪但被顺序挡住）", None)

    ctx.turns.activate(second)

    # ---- 同步断言：还没到兜底上界，队首没放行 → 后面的没入队、没开跑 ----
    assert second.status == "preparing", ("还没放行就不该入队", second.status)
    assert not _events_of(ctx, "TURN_START", second.turn_id), "FIFO：后预留的轮次先开跑了"

    started = time.perf_counter()
    await _wait_until(
        lambda: _events_of(ctx, "TURN_START", second.turn_id),
        timeout=SETUP_DEADLINE,
        what="兜底等待没有把就绪的预留放行（会被永远挡住）",
    )
    waited = time.perf_counter() - started
    assert waited >= bound * 0.7, (
        f"兜底等待没等够就放行了（{waited:.3f}s < {bound * 0.7:.3f}s）—— 那不是有界等待",
    )
    assert not _events_of(ctx, "TURN_START", first.turn_id), (
        "第一位从未放行，却开跑了（兜底把不该放行的也放了）"
    )

    # ---- 不可逆事实二：兜底放行的那一轮如实收尾，台账不留 unfinished ----
    await _wait_until(
        lambda: _events_of(ctx, "TURN_END", second.turn_id),
        timeout=SETUP_DEADLINE,
        what="兜底放行的那一轮没有收尾",
    )
    assert ctx.turn_journal.unfinished() == [], (
        "兜底放行的那一轮没有如实收尾",
        ctx.turn_journal.unfinished(),
    )
    ctx.turns.abandon(first, reason="test_cleanup")


async def test_retry_waits_for_the_copy_before_the_turn_starts(async_app, tmp_path, monkeypatch):
    """§1.1 关键反例：复制闸门仍关闭时，模型一次都不能被调用、不得有 TURN_START、请求不返回。

    修复前：路由先 submit（入队）再 await bind → 工作线程在复制上让出事件循环，
    TurnManager 的 worker 立刻开始执行这一轮（模型被调用），而附件还停在 prepared。
    """
    ctx = async_app.state.ctx
    topic = ctx.topics.nodes.create_topic("就绪放行").id
    original_turn = "turn_r6_original"
    adapter = _CountingAdapter()
    ctx.build_adapter = AsyncMock(return_value=adapter)

    async with _live(async_app) as ac:
        source, att = await _ready_copy(ac, ctx, tmp_path, topic)
        await ctx.attachments.bind_for_turn(original_turn, [att["id"]], topic_id=topic)
        source.unlink()  # 只能用 QIO 保存的那份副本

        _break_link(monkeypatch)
        entered, release = _gate_finalize(monkeypatch)

        def note_readiness() -> None:
            clones = _clones_of(ctx, att["id"])
            adapter.ready_at_call = bool(clones) and all(
                clone.state == "ready" and _readable(clone) for clone in clones
            )

        adapter._on_call = note_readiness

        task = asyncio.create_task(
            ac.post(
                "/api/turns",
                json={
                    "message": "重试（附件还在复制）",
                    "topic_id": topic,
                    "attachment_ids": [att["id"]],
                    "retry_of_turn_id": original_turn,
                },
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=DEADLINE)
        # ---- 闸门仍关闭：以下是 §1.1 的关键断言 ----
        # 先让「先执行」的副作用有机会暴露（基线测得模型在 submit 后约 0.01s 就被调用，
        # 所以这段窗口足够让缺陷显形；它只用于让**禁止出现的副作用**显形，不是就绪判据）
        await _settle_forbidden_window(ctx, adapter)
        assert adapter.calls == 0, (
            f"附件还在复制，模型已经被调用了 {adapter.calls} 次（先执行后等待的缺陷）"
        )
        assert not _turn_starts(ctx), "准备期间不得发 TURN_START（不伪造模型已开始）"
        snapshot = ctx.turns.snapshot()
        assert snapshot["running"] is None, ("准备期间不得有正在执行的轮次", snapshot["running"])
        assert not snapshot["queued"], ("准备期间不得入队", snapshot["queued"])
        assert not task.done(), "附件没就绪，HTTP 请求不该返回"

        clones = _clones_of(ctx, att["id"])
        assert clones, "没有看到这次重试的克隆行"
        assert all(clone.state == "prepared" for clone in clones), (
            f"克隆还没写完就不该是别的状态：{[(c.id, c.state) for c in clones]}"
        )
        assert not any(_readable(clone) for clone in clones), "克隆还没写完，不该有可读的 stored_path"

        release.set()
        resp = await asyncio.wait_for(task, timeout=DEADLINE)
        # 放行之后这一轮才开跑：必须在应用还活着的时候等它（退出 lifespan 会取消 worker）
        await _wait_until(
            lambda: adapter.calls >= 1, timeout=SETUP_DEADLINE, what="放行后模型没有被调用"
        )

    assert resp.status_code == 200, resp.text
    assert adapter.ready_at_call is True, (
        "模型被调用时本轮克隆还没就绪（必须在附件就绪后才放行执行）"
    )
    assert resp.json()["bound_attachment_ids"], resp.text
