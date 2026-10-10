"""上传失败 / 取消 / 断连收敛（round 4 问题三）+ 轮次绑定的回执接线（问题二路由侧）。

修复前的真缺陷（plan §0 第 3 条）：api/server.py 的 _upload_queue_put 只等队列空位、
不看工作线程是否已经结束；结束/中止哨兵还走同一个队列 —— 工作线程一死，
「队列满 + 没有消费者」就让请求永远等下去，附件停在 prepared。

本文件的时序全部用**闸门 / 事件**控制（sleep 只作为有界超时判定失败，不作为证据）：
* 写盘错误用「附件副本的目标父路径是普通文件」制造真实 OSError（mkdir 必失败）；
* 上传分块数 > 队列容量（UPLOAD_QUEUE_DEPTH），确定性暴露「消费者已退出」；
* 取消用真实 DELETE；断连让请求体生成器抛错；服务关闭用取消请求任务模拟。
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
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
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

#: 分块数必须超过队列容量，才能确定性地制造「工作线程已退出、队列没有消费者」
CHUNKS = 20
CHUNK_BYTES = 8192
#: 被测**性质**的有界期限（判定都是事件/闸门驱动；超时 = 失败）
DEADLINE = 5.0
#: **前置条件**的等待上界（工作线程起跑、请求登记）：这是环境准备不是被测性质，
#: 负载/CI 共享机器上线程起跑可能远慢于 5s，放宽它不会掩盖任何被测行为。
SETUP_DEADLINE = 20.0


def _pin_attachment_data_dir(app, tmp_path: Path) -> None:
    """把附件的真实落点钉在 tmp_path。

    已知陷阱（Lead 2026-10-07 确认的代码事实）：config.Settings.__post_init__ 会用环境变量
    QIO_DATA_DIR **覆盖**构造时显式传入的 data_dir。tests/conftest.py 会 pop 掉它，但把用例
    放在仓外跑（或 conftest 没被加载）时，Settings(data_dir=tmp_path) 就会写进用户真实数据目录。
    所以走 create_app 的附件测试必须再钉一次服务自己的 data_dir（root 由它派生）。
    """
    data_dir = tmp_path / "data"
    (data_dir / "attachments").mkdir(parents=True, exist_ok=True)
    app.state.ctx.attachments.data_dir = data_dir


@pytest.fixture()
def async_app(tmp_path: Path):
    conn = connect(tmp_path / "upload_convergence.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path / "data"), conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    _pin_attachment_data_dir(app, tmp_path)
    return app


@asynccontextmanager
async def _live(app):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
            yield ac


def _break_target_dir(ctx) -> Path:
    """把附件副本的目标父目录占成**普通文件**：工作线程的 mkdir 立刻抛真实 OSError。

    目录名与 AttachmentService.copy_path 一致（root/YYYY/MM）：用同一个 _local_month 取。
    """
    year, month = attachments_mod._local_month()
    parent = ctx.attachments.root / year / month
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text("我不是目录", "utf-8")
    return parent


def _chunked_body(
    chunks: int,
    *,
    sent: dict | None = None,
    gate: asyncio.Event | None = None,
    fail_after: int | None = None,
):
    """分块请求体：块数远大于队列容量。

    * gate：发完第一块后挂住（模拟慢发送端），由测试决定何时继续；
    * fail_after：发到第 N 块后抛错（模拟客户端断开）。
    """

    async def body():
        for index in range(chunks):
            if sent is not None:
                sent["count"] = index + 1
            yield b"x" * CHUNK_BYTES
            if fail_after is not None and index + 1 >= fail_after:
                raise RuntimeError("客户端断开（测试模拟）")
            if gate is not None and index == 0:
                await gate.wait()

    return body()


async def _wait_for_row(ctx, *, timeout: float = SETUP_DEADLINE):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = ctx.attachments.list(limit=10, check=False)
        if rows:
            return rows[0]
        await asyncio.sleep(0.01)
    raise AssertionError("上传没有登记出附件行（begin_upload 没走到）")


async def _wait_until(predicate, *, timeout: float = DEADLINE, what: str = ""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    raise AssertionError(f"没有在 {timeout}s 内满足：{what}")


def _leftovers(ctx, *ignore: Path) -> list[Path]:
    """附件根目录下的文件（可排除测试自己放的占位文件）。"""
    root = ctx.attachments.root
    if not root.is_dir():
        return []
    ignored = {str(path) for path in ignore}
    return [p for p in root.rglob("*") if p.is_file() and str(p) not in ignored]


async def _wait_worker_exit(jobs, *, timeout: float) -> bool:
    """等工作线程真的退出：优先用作业自己的退出事件（确定性），没有就退回有界轮询。"""
    waiters = [getattr(job, "wait_worker_exit", None) for job in jobs]
    if waiters and all(callable(waiter) for waiter in waiters):
        results = [await waiter(timeout=timeout) for waiter in waiters]
        return all(results)
    deadline = time.time() + timeout
    while time.time() < deadline:
        flags = [getattr(job, "worker_done", None) for job in jobs]
        if flags and all(flag is not None and flag.is_set() for flag in flags):
            return True
        await asyncio.sleep(0.01)
    return False


def _active_jobs():
    """上传作业注册表（只在实现落地后存在；测试内导入，红的时候直接报缺模块）。"""
    from agent.services.attachment_upload import active_jobs

    return active_jobs()


def _spy_jobs(monkeypatch) -> list:
    """抓住这次请求创建的作业对象（不依赖注册表 —— 修复后作业可能已经收尾注销）。

    注册表是「当前在飞」的视图，不能当前置条件用：写盘失败可能快到请求返回时作业已经注销。
    这里只做观察，断言仍然打在**产品行为**上（请求是否收尾、行状态、文件、活动作业）。
    """
    from agent.api import server as server_mod

    created: list = []
    real = server_mod.UploadJob

    def _factory(*args, **kwargs):
        job = real(*args, **kwargs)
        created.append(job)
        return job

    monkeypatch.setattr(server_mod, "UploadJob", _factory)
    return created


async def _worker_exited(created, *, timeout: float) -> bool:
    """等工作线程退出；返回「是否还能在注册表里观察到它」（**只作诊断**）。

    * 注册表里还有作业 → 等它的退出事件（退出事实由工作线程自己置位）；
    * 注册表已经空了 → 作业已收尾注销，这本身就是「工作线程已经退出」的另一种证据，
      但仍然要求它真的置过退出事件（不许把「注销」当成「退出」）。
    """
    live = _active_jobs()
    if live:
        assert await _wait_worker_exit(live, timeout=timeout), "工作线程没有退出"
        return True
    await _wait_until(
        lambda: bool(created) and all(job.worker_done.is_set() for job in created),
        timeout=timeout,
        what="作业已从注册表注销，但工作线程没有被报告为已退出",
    )
    return False


def _assert_converged(ctx, *ignore: Path) -> None:
    """收尾后的共同断言：行不停在 prepared/ready、不留文件、活动作业为 0。"""
    rows = ctx.attachments.list(limit=10, check=False)
    assert all(row.state not in ("prepared", "ready") for row in rows), (
        f"收尾后附件停在 prepared/ready：{[(r.id, r.state) for r in rows]}"
    )
    assert not _active_jobs(), "收尾后活动作业必须注销"
    leftovers = _leftovers(ctx, *ignore)
    assert leftovers == [], f"收尾后留下了文件：{leftovers}"


async def test_worker_failure_ends_the_request_while_the_client_is_paused(async_app, monkeypatch):
    """§1.2 关键断言：工作线程失败后，**客户端一个字节都不再发**，请求也必须自己失败收尾。

    修复前（api/server.py 的 async for chunk in request.stream()）接收端在等下一块网络数据时
    不观察作业终态 → 请求挂住、附件停在 prepared、活动作业仍有 1 个（客户端恢复发送才 500）。
    """
    ctx = async_app.state.ctx
    placeholder = _break_target_dir(ctx)  # 目标父路径是普通文件 → 工作线程 mkdir 真失败
    created = _spy_jobs(monkeypatch)
    gate = asyncio.Event()  # 客户端闸门：保持关闭 = 不再发任何字节
    first_sent = asyncio.Event()
    sent = {"count": 0}

    async with _live(async_app) as ac:
        task = asyncio.create_task(
            ac.post(
                "/api/attachments/upload",
                content=_paused_body(gate, first_sent, sent=sent),
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-QIO-Name": quote("暂停收尾.bin"),
                },
            )
        )
        await _wait_until(
            lambda: first_sent.is_set(), timeout=SETUP_DEADLINE, what="请求体没有开始"
        )
        await _wait_for_row(ctx)  # 上传已经登记（prepared 行已出现）
        # 「还能不能观察到作业」只作诊断：修复后写盘失败可能快到请求返回时作业已经收尾注销。
        # 观察不到作业不是失败 —— 它恰恰是「工作线程已经退出」的另一种证据（真断言在下面）。
        observed = await _worker_exited(created, timeout=SETUP_DEADLINE)
        assert not gate.is_set(), "闸门必须仍然关闭：这一刻客户端不会再发任何字节"

        try:
            resp = await asyncio.wait_for(task, timeout=DEADLINE)
        except asyncio.TimeoutError:
            raise AssertionError(
                "客户端暂停时工作线程失败，上传请求没有自己收尾"
                "（接收端还在等下一块网络数据，不看作业终态）"
            ) from None

        assert resp.status_code == 500, resp.text
        assert str(resp.json().get("detail") or "").strip(), "失败必须带人话原因"
        _assert_converged(ctx, placeholder)
        assert sent["count"] <= 1, f"客户端不该发第二块：已发 {sent['count']} 块"
        print(
            f"[paused-upload] mkdir-failure: job_observed={observed} "
            f"already_exited={not observed} sent={sent['count']} status={resp.status_code}",
            flush=True,
        )

    gate.set()  # 收尾：放行挂起的请求体生成器，避免留下未关闭的异步生成器


def _paused_body(
    gate: asyncio.Event,
    started: asyncio.Event | None = None,
    *,
    chunks: int = 1,
    sent: dict | None = None,
):
    """先发 `chunks` 块，然后等闸门：客户端**不再产生任何字节**（除非测试释放闸门）。

    `started` 在**第一块之前**置位（「请求体已经开始」）：不能等生成器第二次被拉取才置位 ——
    修复后接收端在作业终态到达时就不再拉取请求体了，那样置位会永远等不到（这正是修好的行为）。
    `sent["count"]` 记录**实际交给 ASGI 的分块数**（用来断言客户端没有发第二块）。
    """

    async def body():
        if started is not None:
            started.set()
        for index in range(max(1, int(chunks))):
            if sent is not None:
                sent["count"] = index + 1
            yield b"x" * CHUNK_BYTES
        await gate.wait()

    return body()


async def _paused_request_ends_with_failure(
    async_app, monkeypatch, name: str, *, chunks: int = 1, status: int = 500
):
    """闸门仍关闭（客户端不再发字节）时，上传请求必须自己失败收尾。

    前置只等两件**确定**的事：请求体已经开始、上传已经登记（prepared 行出现）。
    「作业还在注册表里」**不是**前置条件 —— 修复后失败可能快到请求返回时作业已收尾注销；
    观察不到作业反而是「工作线程已经退出」的另一种证据（见 _worker_exited，仍要求它真置过退出事件）。
    """
    ctx = async_app.state.ctx
    created = _spy_jobs(monkeypatch)
    gate = asyncio.Event()
    first_sent = asyncio.Event()
    sent = {"count": 0}

    async with _live(async_app) as ac:
        task = asyncio.create_task(
            ac.post(
                "/api/attachments/upload",
                content=_paused_body(gate, first_sent, chunks=chunks, sent=sent),
                headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote(name)},
            )
        )
        await _wait_until(
            lambda: first_sent.is_set(), timeout=SETUP_DEADLINE, what="请求体没有开始"
        )
        await _wait_for_row(ctx)  # 上传已经登记（prepared 行已出现）
        observed = await _worker_exited(created, timeout=SETUP_DEADLINE)
        assert not gate.is_set(), "闸门必须仍然关闭：这一刻客户端不会再发任何字节"

        try:
            resp = await asyncio.wait_for(task, timeout=DEADLINE)
        except asyncio.TimeoutError:
            raise AssertionError(
                "客户端暂停时工作线程已失败，上传请求没有自己收尾"
                "（接收端还在等下一块网络数据，不看作业终态）"
            ) from None

        assert resp.status_code == status, resp.text
        _assert_converged(ctx)
        assert sent["count"] <= chunks, f"客户端不该多发块：已发 {sent['count']} 块"
        print(
            f"[paused-upload] {name}: job_observed={observed} already_exited={not observed} "
            f"sent={sent['count']} status={resp.status_code}",
            flush=True,
        )

    gate.set()  # 收尾：放行挂起的请求体生成器
    return resp


async def test_open_failure_ends_the_request_while_the_client_is_paused(async_app, monkeypatch):
    """打开临时文件失败（权限） + 客户端暂停：请求必须自己收尾。"""
    real_open = open

    def fake_open(file, mode="r", *args, **kwargs):
        if "w" in str(mode):
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(attachments_mod, "open", fake_open, raising=False)
    resp = await _paused_request_ends_with_failure(async_app, monkeypatch, "打开失败暂停.bin")
    assert "权限" in str(resp.json().get("detail") or ""), resp.text


async def test_mid_write_failure_ends_the_request_while_the_client_is_paused(async_app, monkeypatch):
    """写入中途失败（ENOSPC） + 客户端暂停（已发两块）：请求必须自己收尾。"""
    real_open = open
    state = {"writes": 0}

    class _NoSpaceWriter:
        def __init__(self, handle) -> None:
            self._handle = handle

        def write(self, data) -> int:
            state["writes"] += 1
            if state["writes"] > 1:
                raise OSError(errno.ENOSPC, "No space left on device")
            return self._handle.write(data)

        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> bool:
            self._handle.close()
            return False

    def fake_open(file, mode="r", *args, **kwargs):
        handle = real_open(file, mode, *args, **kwargs)
        return _NoSpaceWriter(handle) if "w" in str(mode) else handle

    monkeypatch.setattr(attachments_mod, "open", fake_open, raising=False)
    resp = await _paused_request_ends_with_failure(
        async_app, monkeypatch, "途中失败暂停.bin", chunks=2
    )
    assert "磁盘空间不足" in str(resp.json().get("detail") or ""), resp.text


async def test_delete_while_the_client_is_paused_finalizes_without_more_bytes(async_app):
    """用户删除上传中的附件、客户端又暂停发送：接收任务同样必须收尾（§1.2）。"""
    ctx = async_app.state.ctx
    gate = asyncio.Event()
    first_sent = asyncio.Event()

    async with _live(async_app) as ac:
        task = asyncio.create_task(
            ac.post(
                "/api/attachments/upload",
                content=_paused_body(gate, first_sent, chunks=1),
                headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote("删除暂停.bin")},
            )
        )
        await _wait_until(
            lambda: first_sent.is_set(), timeout=SETUP_DEADLINE, what="请求体没有发出去"
        )
        row = await _wait_for_row(ctx)
        removed = await ac.delete(f"/api/attachments/{row.id}")
        assert removed.status_code == 200, removed.text
        assert not gate.is_set(), "闸门必须仍然关闭"

        try:
            resp = await asyncio.wait_for(task, timeout=DEADLINE)
        except asyncio.TimeoutError:
            raise AssertionError("删除后客户端暂停，上传请求没有收尾") from None

        assert resp.status_code in (400, 404, 409), resp.text
        assert ctx.attachments.get(row.id, check=False) is None
        assert not _active_jobs(), "收尾后活动作业必须注销"
        assert _leftovers(ctx) == [], f"留下了文件：{_leftovers(ctx)}"

    gate.set()


async def test_late_worker_result_cannot_commit_ready(async_app, monkeypatch):
    """工作线程卡在不可中断的磁盘调用（os.replace）里：不谎报已退出、如实收尾、迟到结果不提交 ready。

    收尾超时后：行转 failed、请求返回 500、副本被清；等闸门放行、工作线程真正退出后，
    它迟到的 READY 结果**不得**把行翻成 ready，也不得留下孤儿副本。
    """
    from agent.api import server as server_mod

    monkeypatch.setattr(server_mod, "UPLOAD_SETTLE_SECONDS", 3.0)
    created = _spy_jobs(monkeypatch)
    ctx = async_app.state.ctx
    gate = threading.Event()
    release = threading.Event()
    real_replace = attachments_mod.os.replace

    def gated_replace(src, dst):
        gate.set()
        assert release.wait(15), "测试没有放行 os.replace"
        return real_replace(src, dst)

    monkeypatch.setattr(attachments_mod.os, "replace", gated_replace)

    async with _live(async_app) as ac:
        task = asyncio.create_task(
            ac.post(
                "/api/attachments/upload",
                content=_chunked_body(4),
                headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote("迟到提交.bin")},
            )
        )
        # 等工作线程真的走到**不可中断的提交调用**；作业对象由 _spy_jobs 直接抓，
        # 不依赖注册表（响应返回后它会注销，不能当前置条件用）
        await _wait_until(
            lambda: gate.is_set() and bool(created),
            timeout=SETUP_DEADLINE,
            what="工作线程没有走到 os.replace",
        )
        jobs = created

        resp = await asyncio.wait_for(task, timeout=DEADLINE)
        assert resp.status_code == 500, resp.text
        rows = ctx.attachments.list(limit=10, check=False)
        assert all(row.state not in ("prepared", "ready") for row in rows), (
            f"收尾超时后附件停在 prepared/ready：{[(r.id, r.state) for r in rows]}"
        )
        # 不谎报「已退出」：工作线程还卡在磁盘调用里，退出事实只有它自己能置位
        assert jobs and not any(job.worker_done.is_set() for job in jobs), (
            "工作线程还卡在 os.replace 里，不该被报告为已退出"
        )
        assert not _active_jobs(), "响应返回后活动作业必须注销"

    release.set()  # 放行：工作线程会真的提交，但迟到的结果不得改变行状态
    await _wait_until(lambda: not _active_jobs(), timeout=SETUP_DEADLINE, what="上传作业没有收尾")
    await _wait_until(
        lambda: _leftovers(ctx) == [], timeout=SETUP_DEADLINE, what="迟到提交留下了孤儿副本"
    )
    rows = ctx.attachments.list(limit=10, check=False)
    assert all(row.state != "ready" for row in rows), (
        f"迟到的结果把行提交成 ready 了：{[(r.id, r.state) for r in rows]}"
    )


async def test_other_apis_keep_progressing_while_an_upload_is_paused(async_app):
    """上传暂停（工作线程阻塞在队列读上）时，其它 API 必须照常返回。"""
    ctx = async_app.state.ctx
    gate = asyncio.Event()
    first_sent = asyncio.Event()

    async with _live(async_app) as ac:
        task = asyncio.create_task(
            ac.post(
                "/api/attachments/upload",
                content=_paused_body(gate, first_sent, chunks=1),
                headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote("暂停推进.bin")},
            )
        )
        await _wait_until(
            lambda: first_sent.is_set(), timeout=SETUP_DEADLINE, what="请求体没有发出去"
        )
        row = await _wait_for_row(ctx)

        started = time.perf_counter()
        assert (await ac.get("/api/attachments")).status_code == 200
        assert (await ac.get("/api/runtime/state")).status_code == 200
        assert (await ac.get("/api/turns/queue")).status_code == 200
        elapsed = time.perf_counter() - started
        assert elapsed < DEADLINE, f"暂停上传时其它 API 被挡住了：{elapsed:.3f}s"

        assert (await ac.delete(f"/api/attachments/{row.id}")).status_code == 200
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=DEADLINE)

    gate.set()


async def test_upload_write_failure_finishes_within_the_deadline(async_app):
    """写盘失败（建目录失败）必须让请求在有限时间内结束 —— 修复前它会永远等一个没人消费的队列。"""
    ctx = async_app.state.ctx
    placeholder = _break_target_dir(ctx)
    sent = {"count": 0}

    async with _live(async_app) as ac:
        try:
            resp = await asyncio.wait_for(
                ac.post(
                    "/api/attachments/upload",
                    content=_chunked_body(CHUNKS, sent=sent),
                    headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote("写盘失败.bin")},
                ),
                timeout=DEADLINE,
            )
        except asyncio.TimeoutError:  # pragma: no cover - 修复前走这里
            raise AssertionError(
                "上传没有在 %.0fs 内结束：接收端在等一个已经没有消费者的满队列（问题三）" % DEADLINE
            ) from None

    assert resp.status_code >= 400, resp.text
    detail = str(resp.json().get("detail") or "")
    assert detail, "失败必须带人话原因"
    # 接收端应当**提前**停止（工作线程一失败就不该继续灌块）
    assert sent["count"] < CHUNKS, f"接收端没有提前结束：已发送 {sent['count']}/{CHUNKS} 块"

    rows = ctx.attachments.list(limit=10, check=False)
    assert all(row.state not in ("prepared", "ready") for row in rows), (
        f"上传失败后仍有附件停在 prepared/ready：{[(r.id, r.state) for r in rows]}"
    )
    assert _leftovers(ctx, placeholder) == [], f"失败后留下了文件：{_leftovers(ctx, placeholder)}"


async def test_worker_failure_releases_a_blocked_receiver(async_app):
    """工作线程失败 → 接收端的排队等待必须被解除（不是等下一个空位）。"""
    ctx = async_app.state.ctx
    placeholder = _break_target_dir(ctx)
    sent = {"count": 0}

    async with _live(async_app) as ac:
        resp = await asyncio.wait_for(
            ac.post(
                "/api/attachments/upload",
                content=_chunked_body(CHUNKS, sent=sent),
                headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote("释放.bin")},
            ),
            timeout=DEADLINE,
        )

    assert resp.status_code >= 400, resp.text
    assert sent["count"] < CHUNKS, "工作线程已经失败，接收端还在继续入队"
    assert _leftovers(ctx, placeholder) == [], f"失败后留下了文件：{_leftovers(ctx, placeholder)}"
    await _wait_until(lambda: not _active_jobs(), what="上传作业没有收尾（还有等待中的任务）")


async def test_write_failure_mid_stream_converges(async_app, monkeypatch):
    """写入途中的真实失败（ENOSPC）：行转 failed、不留临时文件、请求及时结束。"""
    ctx = async_app.state.ctx
    real_open = open
    state = {"writes": 0}

    class _NoSpaceWriter:
        def __init__(self, handle) -> None:
            self._handle = handle

        def write(self, data) -> int:
            state["writes"] += 1
            if state["writes"] > 1:
                raise OSError(errno.ENOSPC, "No space left on device")
            return self._handle.write(data)

        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> bool:
            self._handle.close()
            return False

    def fake_open(file, mode="r", *args, **kwargs):
        handle = real_open(file, mode, *args, **kwargs)
        return _NoSpaceWriter(handle) if "w" in str(mode) else handle

    monkeypatch.setattr(attachments_mod, "open", fake_open, raising=False)

    async with _live(async_app) as ac:
        resp = await asyncio.wait_for(
            ac.post(
                "/api/attachments/upload",
                content=_chunked_body(CHUNKS),
                headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote("途中失败.bin")},
            ),
            timeout=DEADLINE,
        )

    assert resp.status_code >= 400, resp.text
    assert "磁盘空间不足" in str(resp.json().get("detail") or "")
    rows = ctx.attachments.list(limit=10, check=False)
    assert all(row.state not in ("prepared", "ready") for row in rows), (
        f"失败后仍有附件停在 prepared/ready：{[(r.id, r.state) for r in rows]}"
    )
    assert _leftovers(ctx) == [], f"失败后留下了文件：{_leftovers(ctx)}"
    await _wait_until(lambda: not _active_jobs(), what="上传作业没有收尾")


async def test_cancel_unblocks_the_worker_blocked_on_the_queue(async_app, monkeypatch):
    """用户取消（DELETE）必须解除工作线程在队列上的阻塞读取，并且不留临时文件。

    CI py3.12 唯一失败就是这条：原来等的是「job.terminal 变真」，而取消当时只靠取块循环
    轮询服务侧的临时取消事件 —— delete() 置位后会把那个事件清掉，轮询落在窗口之外就永远
    看不到取消。现在判定改成**事件驱动**：
      * 先把工作线程的 poll 拉大，并等它**确实阻塞在 queue.get 上**（确定性前置条件）；
      * DELETE 之后断言作业终态 + 工作线程退出事件（都有界超时，只用于判定失败）。
    """
    from agent.services import attachment_upload as upload_mod

    monkeypatch.setitem(upload_mod.UploadJob.__init__.__kwdefaults__, "poll_seconds", 30.0)
    ctx = async_app.state.ctx
    gate = asyncio.Event()

    async with _live(async_app) as ac:
        task = asyncio.create_task(
            ac.post(
                "/api/attachments/upload",
                content=_chunked_body(CHUNKS, gate=gate),
                headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote("取消我.bin")},
            )
        )
        row = await _wait_for_row(ctx)
        jobs = _active_jobs()
        assert jobs, "上传作业没有登记进注册表"
        # 第一块已经发出去并且被消费：工作线程正阻塞在队列读取上（poll 30s，不会再自己醒）
        await _wait_until(
            lambda: all(job.consumed >= 1 and job.snapshot()["reading"] for job in jobs),
            timeout=SETUP_DEADLINE,
            what="工作线程没有进入阻塞读（还没消费第一块 / 没在等下一块）",
        )

        removed = await ac.delete(f"/api/attachments/{row.id}")
        assert removed.status_code == 200, removed.text
        assert all(job.terminal for job in jobs), "DELETE 之后作业必须立刻是终态（事件驱动，不靠轮询）"
        assert await _wait_worker_exit(jobs, timeout=DEADLINE), "取消没有解除工作线程的阻塞读取"
        assert _leftovers(ctx) == [], f"取消之后留下了文件：{_leftovers(ctx)}"

        gate.set()
        done = await asyncio.wait_for(task, timeout=DEADLINE)

    assert done.status_code in (400, 404, 409), done.text
    assert ctx.attachments.get(row.id, check=False) is None
    await _wait_until(lambda: not _active_jobs(), what="上传作业没有收尾")


async def test_delete_leaves_the_upload_job_terminal_immediately(async_app, monkeypatch):
    """DELETE 返回时上传作业必须**已经是终态**，且工作线程要被**事件**唤醒（不靠轮询的运气）。

    CI py3.12 唯一失败（test_cancel_unblocks_the_worker_blocked_on_the_queue）的真缺陷：
    services/attachments.py 的 delete() 先 cancel() 置位、随后 **pop 掉 _cancel 里的事件**。
    取块循环用 is_cancel_requested() 事后轮询 —— 轮询落在「置位之后、pop 之前」这个窗口里
    才看得见取消；窗口只有一次 SQLite DELETE+commit 的时长，Windows 上宽（通常恰好命中）、
    Linux/py3.12 上窄（轮询落在 pop 之后 → 取消信号永久消失）→ 工作线程一直阻塞在队列读上。

    这里把 poll_seconds 拉大，**让「恰好命中窗口」不可能发生**：修复前作业永远非终态、
    工作线程永不退出；修复后 DELETE 直接置作业终态并用哨兵把阻塞的读取唤醒。
    """
    from agent.services import attachment_upload as upload_mod

    # poll_seconds 是 keyword-only 默认值（定义时就绑定了），要改的是 __kwdefaults__；
    # 目的只有一个：让「工作线程恰好轮询到取消窗口」不可能发生（不是断言依据）。
    monkeypatch.setitem(upload_mod.UploadJob.__init__.__kwdefaults__, "poll_seconds", 30.0)
    ctx = async_app.state.ctx
    gate = asyncio.Event()

    async with _live(async_app) as ac:
        task = asyncio.create_task(
            ac.post(
                "/api/attachments/upload",
                content=_chunked_body(CHUNKS, gate=gate),
                headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote("删除即取消.bin")},
            )
        )
        row = await _wait_for_row(ctx)
        jobs = _active_jobs()
        assert jobs, "上传作业没有登记进注册表"

        # 先确定工作线程**已经**消费掉第一块、正阻塞在 queue.get 上（poll 已拉到 30s）：
        # 这样「DELETE 之后它还看得见取消」只剩一个可能 —— 作业终态真的留痕了。
        await _wait_until(
            lambda: all(job.consumed >= 1 and job.snapshot()["reading"] for job in jobs),
            timeout=SETUP_DEADLINE,
            what="工作线程没有进入阻塞读（还没消费第一块 / 没在等下一块）",
        )

        removed = await ac.delete(f"/api/attachments/{row.id}")
        assert removed.status_code == 200, removed.text

        # 关键 1：这一刻就必须是终态（同步、事件驱动）
        assert all(job.terminal for job in jobs), (
            "DELETE 之后上传作业还不是终态：取消只靠取块循环轮询，"
            "而 delete() 已经把 _cancel 里的事件清掉了（CI py3.12 红）"
        )
        # 关键 2：工作线程必须被**事件/哨兵**唤醒（poll 已被拉到 30s，轮询不可能救场）
        assert await _wait_worker_exit(jobs, timeout=DEADLINE), (
            "工作线程没有从阻塞读取里退出：取消没有唤醒阻塞在 queue.get 上的消费者"
        )

        gate.set()
        done = await asyncio.wait_for(task, timeout=DEADLINE)

    assert done.status_code in (400, 404, 409), done.text
    assert ctx.attachments.get(row.id, check=False) is None
    assert _leftovers(ctx) == [], f"取消之后留下了文件：{_leftovers(ctx)}"
    await _wait_until(lambda: not _active_jobs(), what="取消后上传作业没有收尾")


async def test_client_disconnect_converges(async_app):
    """客户端断开：请求体生成器抛错 → 不留临时文件、行不落成 prepared/ready。"""
    ctx = async_app.state.ctx

    async with _live(async_app) as ac:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                ac.post(
                    "/api/attachments/upload",
                    content=_chunked_body(CHUNKS, fail_after=1),
                    headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote("断连.bin")},
                ),
                timeout=DEADLINE,
            )

    rows = ctx.attachments.list(limit=10, check=False)
    assert all(row.state not in ("prepared", "ready") for row in rows), (
        f"断连后仍有附件停在 prepared/ready：{[(r.id, r.state) for r in rows]}"
    )
    assert _leftovers(ctx) == [], f"断连后留下了文件：{_leftovers(ctx)}"
    await _wait_until(lambda: not _active_jobs(), what="断连后上传作业没有收尾")


async def test_request_cancellation_converges(async_app):
    """服务关闭 / 请求被取消：工作线程要被解除阻塞并清理，行不落成 prepared/ready。"""
    ctx = async_app.state.ctx
    gate = asyncio.Event()

    async with _live(async_app) as ac:
        task = asyncio.create_task(
            ac.post(
                "/api/attachments/upload",
                content=_chunked_body(CHUNKS, gate=gate),
                headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote("关闭.bin")},
            )
        )
        await _wait_for_row(ctx)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    await _wait_until(lambda: not _active_jobs(), what="取消请求后上传作业没有收尾")
    rows = ctx.attachments.list(limit=10, check=False)
    assert all(row.state not in ("prepared", "ready") for row in rows), (
        f"取消后仍有附件停在 prepared/ready：{[(r.id, r.state) for r in rows]}"
    )
    assert _leftovers(ctx) == [], f"取消后留下了文件：{_leftovers(ctx)}"


async def test_write_permission_failure_converges(async_app, monkeypatch):
    """权限不足（EACCES）也是真实写盘失败：如实报错、行 failed、不留文件。"""
    ctx = async_app.state.ctx
    real_open = open

    def fake_open(file, mode="r", *args, **kwargs):
        if "w" in str(mode):
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(attachments_mod, "open", fake_open, raising=False)

    async with _live(async_app) as ac:
        resp = await asyncio.wait_for(
            ac.post(
                "/api/attachments/upload",
                content=_chunked_body(CHUNKS),
                headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote("没权限.bin")},
            ),
            timeout=DEADLINE,
        )

    assert resp.status_code >= 400, resp.text
    assert "权限" in str(resp.json().get("detail") or "")
    rows = ctx.attachments.list(limit=10, check=False)
    assert all(row.state not in ("prepared", "ready") for row in rows), (
        f"失败后仍有附件停在 prepared/ready：{[(r.id, r.state) for r in rows]}"
    )
    assert _leftovers(ctx) == [], f"失败后留下了文件：{_leftovers(ctx)}"
    await _wait_until(lambda: not _active_jobs(), what="权限失败后上传作业没有收尾")


async def test_shutdown_during_commit_leaves_no_orphan_copy(async_app, monkeypatch):
    """关闭竞态：工作线程已经动手提交（os.replace 被闸门卡住）时取消请求。

    必须在**不留下孤儿副本**的前提下收敛：服务侧取消标志让最后一道闸失效，
    已经提交的副本由收尾清理，行不得落成 prepared/ready。
    """
    ctx = async_app.state.ctx
    # 闸门在**工作线程**里等：必须是 threading 语义，不能占住事件循环
    gate = threading.Event()
    release = threading.Event()
    real_replace = attachments_mod.os.replace

    def gated_replace(src, dst):
        # 工作线程里执行：告诉测试「已经走到提交这一步」，等测试取消请求后再放行
        gate.set()
        assert release.wait(5), "测试没有放行 os.replace"
        return real_replace(src, dst)

    monkeypatch.setattr(attachments_mod.os, "replace", gated_replace)

    async with _live(async_app) as ac:
        task = asyncio.create_task(
            ac.post(
                "/api/attachments/upload",
                content=_chunked_body(CHUNKS),
                headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote("提交竞态.bin")},
            )
        )
        await _wait_for_row(ctx)
        # 等**工作线程**走到提交（闸门是 threading 语义：用 to_thread 等，不占事件循环）
        assert await asyncio.to_thread(lambda: gate.wait(DEADLINE)), "工作线程没有走到提交"
        task.cancel()
        await asyncio.sleep(0)  # 让取消投递到路由（它会先置取消标志、再等收尾）
        release.set()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    await _wait_until(lambda: not _active_jobs(), what="取消后上传作业没有收尾")
    rows = ctx.attachments.list(limit=10, check=False)
    assert all(row.state not in ("prepared", "ready") for row in rows), (
        f"取消后仍有附件停在 prepared/ready：{[(r.id, r.state, r.error) for r in rows]}"
    )
    assert _leftovers(ctx) == [], f"取消后留下了孤儿副本：{_leftovers(ctx)}"


async def test_upload_over_limit_is_413_and_leaves_nothing(async_app):
    """超限仍然 413、不留行/文件（保留原有语义，别被收敛改造破坏）。"""
    ctx = async_app.state.ctx
    ctx.attachments.max_upload_bytes = 64

    async with _live(async_app) as ac:
        resp = await asyncio.wait_for(
            ac.post(
                "/api/attachments/upload",
                content=_chunked_body(4, sent={"count": 0}),
                headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote("超标.bin")},
            ),
            timeout=DEADLINE,
        )

    assert resp.status_code == 413, resp.text
    assert ctx.attachments.list(limit=10, check=False) == []
    assert _leftovers(ctx) == []
    await _wait_until(lambda: not _active_jobs(), what="超限后上传作业没有收尾")


# -- 问题二路由接线：按 B 的冻结回执行事 -------------------------------------------------


async def _make_ready_attachment(ac, ctx, tmp_path, name="回执.txt") -> dict:
    """在**调用方已经打开的 lifespan** 里备好一个 ready 附件（TurnManager 不能被提前关掉）。"""
    topic = ctx.topics.nodes.create_topic("回执话题").id
    source = tmp_path / name
    source.write_bytes(b"receipt")
    created = (
        await ac.post("/api/attachments", json={"source_path": str(source), "topic_id": topic})
    ).json()["attachment"]
    meta = created
    for _ in range(200):
        meta = (await ac.get(f"/api/attachments/{created['id']}")).json()["attachment"]
        if meta["state"] != "prepared":
            break
        await asyncio.sleep(0.02)
    assert meta["state"] == "ready", meta
    return {**meta, "topic_id": topic}


async def test_turn_rejects_and_does_not_enqueue_when_receipt_has_rejects(async_app, tmp_path, monkeypatch):
    """预检通过、但绑定回执 rejected 非空（受理瞬间竞态）→ 409 结构化 detail 且**不入队**。"""
    ctx = async_app.state.ctx
    from agent.services.attachments import BindOutcome

    async with _live(async_app) as ac:
        att = await _make_ready_attachment(ac, ctx, tmp_path)
        # bind_for_turn 现在是 async（R5 §1.3）：桩也要 async，否则路由 await 不了
        async def _rejected_receipt(**_kwargs) -> BindOutcome:
            return BindOutcome(
                bound=[], rejected=[(att["id"], "附件已经绑到别的轮次（请移除后重发）")]
            )

        monkeypatch.setattr(ctx.attachments, "bind_for_turn", _rejected_receipt)

        before = ctx.turns.snapshot()
        resp = await ac.post(
            "/api/turns",
            json={"message": "重试", "topic_id": att["topic_id"], "attachment_ids": [att["id"]]},
        )
        after = ctx.turns.snapshot()

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "attachment_binding_failed"
    assert [row["id"] for row in detail["rejected"]] == [att["id"]]
    assert "别的轮次" in detail["rejected"][0]["reason"]
    assert detail["message"], "结构化失败必须带一句人话"
    assert detail["bound_attachment_ids"] == []
    assert after["running"] == before["running"], "被拒绝的请求不得开始执行"
    assert after["queued"] == before["queued"], "被拒绝的请求不得入队"


async def test_turn_response_carries_the_real_binding_receipt(async_app, tmp_path, monkeypatch):
    """受理成功必须带**实际绑定回执**（bound_attachment_ids / rejected），前端以它为准。"""
    ctx = async_app.state.ctx

    async with _live(async_app) as ac:
        att = await _make_ready_attachment(ac, ctx, tmp_path, name="回执2.txt")
        resp = await ac.post(
            "/api/turns",
            json={"message": "带上它", "topic_id": att["topic_id"], "attachment_ids": [att["id"]]},
        )
        body = resp.json()
        ctx.turns.cancel(body.get("turn_id") or "")

    assert resp.status_code == 200, resp.text
    assert body["accepted"] is True
    # 回执来自 B 的真实 bind_for_turn（不是路由自己拼的）
    assert body["bound_attachment_ids"] == [att["id"]]
    assert body["rejected"] == []
    assert [item["id"] for item in body["attachments"]] == [att["id"]]
    assert ctx.attachments.get(att["id"], check=False).turn_id == body["turn_id"]


async def test_turn_rejects_attachment_owned_by_another_turn_without_enqueue(async_app, tmp_path):
    """已被别的轮次绑定的 id（且不是 retry_of_turn_id）→ 直接拒绝，不入队、不静默丢弃。

    这条在 B 的冻结实现落地前也必须成立：路由侧的受理前校验（镜像冻结规则）先挡住。
    """
    ctx = async_app.state.ctx

    async with _live(async_app) as ac:
        att = await _make_ready_attachment(ac, ctx, tmp_path, name="先到先得.txt")
        await ctx.attachments.bind_for_turn(
            "turn_owner", [att["id"]], topic_id=att["topic_id"]
        )
        assert ctx.attachments.get(att["id"], check=False).turn_id == "turn_owner"

        before = ctx.turns.snapshot()
        resp = await ac.post(
            "/api/turns",
            json={"message": "第二轮不该抢", "topic_id": att["topic_id"], "attachment_ids": [att["id"]]},
        )

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "attachment_binding_failed"
    assert [row["id"] for row in detail["rejected"]] == [att["id"]]
    assert "别的一轮" in detail["rejected"][0]["reason"]
    after = ctx.turns.snapshot()
    assert after["queued"] == before["queued"] and after["running"] == before["running"]
    assert ctx.attachments.get(att["id"], check=False).turn_id == "turn_owner", "原归属不得被改写"
