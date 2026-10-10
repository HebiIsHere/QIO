"""W4b（R6 后端残余）· 上传收尾的两个边界：过期收尾 + 临时文件归属。

来源：W1 在集成前指出的 api/server.py 两处同族残余（本轮只改 server.py，attachments.py 不动）：

A) _converge_upload 的**合成** DiskOutcome 没有代际/票号 → 旧上传的取消/超时收尾会把
   这条附件**更新的 ready 行**改写成 failed/cancelled（apply_outcome 对 ticket=None 明确不校验）。
   冻结要求：上传流程携带自己的代际 —— 过期收尾被拒绝，而**真正当前**的收尾仍然如实落库
   （不能因为加了准入就一律不写）。
B) _purge_uncommitted_copy 只删旧的固定临时名 <目标>.part，而 R1 之后真实临时名是
   <目标>.<token>.part；取消路径因此会残留临时文件。
   冻结要求：清掉**本操作自己的**临时文件；绝不删另一个在飞操作的临时文件；
   也绝不删更新操作已经提交的正式副本（R1 纪律）。

机制（本文件钉住的时序，全部受控闸门 / 事件，不靠 sleep 猜）：
请求被取消后，BaseHTTPMiddleware 的 anyio 取消作用域会在每个 await 点重复投递
CancelledError —— 不屏蔽时收尾等待 0.02s 就被打断，工作线程被丢在后台继续跑，
它刚建的 .part 留在磁盘上；同一次取消还会用「无代际的合成结果」盖掉更新的 ready 行。

运行：cd backend; uv run --frozen --extra dev pytest tests/test_r2_w4_upload_settle_boundary.py -q
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services import attachments as attachments_mod
from agent.services.attachment_upload import active_jobs
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

#: 分块数远大于作业队列容量：制造「工作线程阻塞读、请求被取消」的真实时序
CHUNKS = 20
CHUNK_BYTES = 8192
#: **前置条件**的等待上界（线程起跑 / 闸门不是被测性质）；被测性质的判定靠事件与状态
SETUP_DEADLINE = 20.0
#: 「请求不该在工作线程清理完成之前返回」的观察窗口（闸门钉着，收尾不该在这段时间内结束）
NO_EARLY_RETURN_WINDOW = 1.0


@pytest.fixture()
def async_app(tmp_path: Path):
    conn = connect(tmp_path / "w4b_upload_boundary.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path / "data"), conn)
    ctx = app.state.ctx
    ctx.credentials._kr = MemoryKeyring()
    # 落点钉在 tmp_path（QIO_DATA_DIR 会覆盖 Settings.data_dir，见 tests 里同款防护）
    data_dir = tmp_path / "data"
    (data_dir / "attachments").mkdir(parents=True, exist_ok=True)
    ctx.attachments.data_dir = data_dir
    return app


@asynccontextmanager
async def _live(app):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
            yield ac


def _chunked_body(gate: asyncio.Event):
    """分块请求体：发完第一块后挂住（慢发送端），由测试决定何时继续。"""

    async def body():
        for index in range(CHUNKS):
            yield b"x" * CHUNK_BYTES
            if index == 0:
                await gate.wait()

    return body()


async def _upload(ac: httpx.AsyncClient, gate: asyncio.Event, name: str):
    return await ac.post(
        "/api/attachments/upload",
        content=_chunked_body(gate),
        headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote(name)},
    )


def _leftovers(root: Path, *ignore: Path) -> list[Path]:
    if not root.is_dir():
        return []
    ignored = {str(path) for path in ignore}
    return sorted(p for p in root.rglob("*") if p.is_file() and str(p) not in ignored)


async def _wait_row(ctx, *, timeout: float = SETUP_DEADLINE):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = ctx.attachments.list(limit=10, check=False)
        if rows:
            return rows[0]
        await asyncio.sleep(0.01)
    raise AssertionError("上传没有登记出附件行（begin_upload 没走到）")


async def _wait_meta(
    ac: httpx.AsyncClient, attachment_id: str, state: str, *, timeout: float = SETUP_DEADLINE
):
    deadline = time.time() + timeout
    meta: dict = {}
    while time.time() < deadline:
        meta = (await ac.get(f"/api/attachments/{attachment_id}")).json()["attachment"]
        if meta["state"] == state:
            return meta
        await asyncio.sleep(0.02)
    raise AssertionError("附件没有在 %.1fs 内变成 %s：%s" % (timeout, state, meta))


async def _wait_temp_file(target: Path, *, timeout: float = SETUP_DEADLINE) -> Path:
    """等**确定性的前置事实**：工作线程已经建出本操作的 <目标>.<token>.part。

    只有它存在时，「取消后不得残留」才有意义 —— 否则测的是「还没来得及建」。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if target.parent.is_dir():
            found = sorted(target.parent.glob(target.name + ".*.part"))
            if found:
                return found[0]
        await asyncio.sleep(0.01)
    raise AssertionError("工作线程没有建出临时文件（装置失效：没走到写盘）")


async def _cancel_and_join(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


# ------------------------------------------------ A：过期收尾不得改写更新的 ready 行


async def test_r2_w4_stale_upload_settle_keeps_newer_ready_row(async_app, tmp_path: Path):
    """上传期间用户重新定位并已 ready：旧上传的取消收尾是过期的，行与副本都必须原样。"""
    ctx = async_app.state.ctx
    gate = asyncio.Event()

    async with _live(async_app) as ac:
        task = asyncio.create_task(_upload(ac, gate, "被重定位的上传.bin"))
        row = await _wait_row(ctx)

        # 更新的准备：重新定位到一个真实来源，并等它真的 ready（代际 +1、票号更新）
        newer = tmp_path / "更新的来源.bin"
        newer.write_bytes(b"NEWER-CONTENT" * 512)
        planned = await ac.post(
            f"/api/attachments/{row.id}/relocate", json={"source_path": str(newer)}
        )
        assert planned.status_code == 200, planned.text[:300]
        ready = await _wait_meta(ac, row.id, "ready")
        ready_path = str(ready["stored_path"])
        assert ready_path, ready

        # 旧上传此刻才收到取消：它的收尾属于更早的代际
        await _cancel_and_join(task)
        after = (await ac.get(f"/api/attachments/{row.id}")).json()["attachment"]
        leftovers = _leftovers(ctx.attachments.root, Path(ready_path))

    assert after["state"] == "ready", (
        "过期的上传收尾把更新的 ready 行改写成了 %s（代际准入没有生效）" % after["state"],
        after,
    )
    assert after["stored_path"] == ready_path, after
    assert Path(ready_path).is_file(), "更新操作提交的正式副本被过期收尾删掉了"
    assert leftovers == [], "取消后留下了本操作自己的临时文件：%s" % [p.name for p in leftovers]


async def test_r2_w4_current_upload_settle_still_converges(async_app):
    """对照：没有更新的准备时，取消收尾必须**照写**（准入不能变成「一律不写」）。"""
    ctx = async_app.state.ctx
    gate = asyncio.Event()

    async with _live(async_app) as ac:
        task = asyncio.create_task(_upload(ac, gate, "当前失败.bin"))
        row = await _wait_row(ctx)
        await _cancel_and_join(task)
        current = ctx.attachments.get(row.id, check=False)

    assert current is not None, "取消收尾把附件行弄丢了"
    assert current.state not in ("prepared", "ready"), (
        "没有更新的准备时，取消收尾仍然必须如实落库（%s）" % current.state,
        current.state,
    )
    assert "取消" in (current.error or ""), current.error


# ------------------------------------------------ B：临时文件只清自己的、且要等它清完


async def test_r2_w4_cancel_settle_waits_for_worker_cleanup(async_app, monkeypatch):
    """取消收尾必须**等本操作的工作线程退出**（有界）：否则它刚建的 .part 会留在磁盘上。

    受控闸门把工作线程钉在「清理自己的临时文件」之前 —— 闸门没放行时，取消的请求
    **不得**已经返回（提前返回 = 工作线程被丢在后台，临时文件无人清理）。
    """
    ctx = async_app.state.ctx
    root = ctx.attachments.root
    gate = asyncio.Event()
    cleanup_gate = threading.Event()
    cleanup_entered = threading.Event()
    real_unlink = attachments_mod._unlink_quiet

    def gated_unlink(path):
        # 工作线程里执行（threading 语义，不占事件循环）
        cleanup_entered.set()
        cleanup_gate.wait(SETUP_DEADLINE)
        return real_unlink(path)

    monkeypatch.setattr(attachments_mod, "_unlink_quiet", gated_unlink)

    async with _live(async_app) as ac:
        task = asyncio.create_task(_upload(ac, gate, "取消不留临时文件.bin"))
        row = await _wait_row(ctx)
        target = ctx.attachments.copy_path(ctx.attachments.get(row.id, check=False))
        our_temp = await _wait_temp_file(target)

        task.cancel()
        assert await asyncio.to_thread(cleanup_entered.wait, SETUP_DEADLINE), (
            "工作线程没有走到清理自己的临时文件（装置失效）"
        )
        finished_early = False
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=NO_EARLY_RETURN_WINDOW)
            finished_early = True
        cleanup_gate.set()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        leftovers = _leftovers(root)

    assert not finished_early, (
        "取消的请求在工作线程清理完临时文件之前就返回了（收尾被外层取消打断，"
        "工作线程被丢在后台）：%s 会留在磁盘上" % our_temp.name
    )
    assert leftovers == [], "取消后留下了本操作自己的临时文件：%s" % [p.name for p in leftovers]


async def test_r2_w4_settle_never_deletes_another_operations_temp(async_app):
    """另一个在飞操作（同目标的别的 token）的 <目标>.<token>.part 必须原样保留。"""
    ctx = async_app.state.ctx
    root = ctx.attachments.root
    gate = asyncio.Event()

    async with _live(async_app) as ac:
        task = asyncio.create_task(_upload(ac, gate, "并发目标.bin"))
        row = await _wait_row(ctx)
        target = ctx.attachments.copy_path(ctx.attachments.get(row.id, check=False))
        our_temp = await _wait_temp_file(target)
        jobs = list(active_jobs())
        # 模拟「同一条目标上另一个在飞操作」的临时文件：只差 token
        sibling = target.with_name(target.name + ".deadbeef.part")
        sibling.parent.mkdir(parents=True, exist_ok=True)
        sibling.write_bytes(b"other-operation-in-flight")
        await _cancel_and_join(task)
        if jobs:
            assert await asyncio.to_thread(jobs[0].worker_done.wait, SETUP_DEADLINE), (
                "工作线程没有退出"
            )
        leftovers = _leftovers(root)

    assert sibling.is_file(), (
        "收尾删掉了别的在飞操作的临时文件（违反 R1：临时文件身份逐操作独立）"
    )
    assert our_temp not in leftovers, (
        "本操作自己的临时文件没有被清掉：%s" % [p.name for p in leftovers]
    )
    assert leftovers == [sibling], (
        "工作线程退出后盘面应当只剩别人的临时文件：%s" % [p.name for p in leftovers]
    )
