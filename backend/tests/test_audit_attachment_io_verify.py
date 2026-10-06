"""D 独立验证：附件 I/O 后台化与有界接收（审计问题 6 / plan §1.7）。

契约来源：docs/plans/2026-10-06-audit-seven-fixes.md §0 第 6 条 + §1.7。
只依据产品规则：

* 上传用 request.stream() **有界分块**接收：**没有 Content-Length 也要强制上限**，
  不能先整包读进内存再说；
* 落临时文件 + 哈希都在**工作线程**；事件循环线程只提交状态；
* 重新定位/复制的文件 I/O 同样不能占住事件循环（上传/重定位期间其它请求仍可推进）；
* 数据库访问必须始终在同一个线程（不得把整个 service 方法丢进线程）。

基线（e428bb9）现状：api/server.py:1122-1153 先 await request.body() 整包读入
（Content-Length 缺失即无上限），再在事件循环里同步写盘 + 算 sha256；
relocate 的 run_prepare 也在事件循环里同步复制 —— 因此本文件在修复前应当是**红的**。

本文件用 httpx.ASGITransport 把应用跑在**同一个事件循环**里：只有这样，
「同步文件 I/O 占住事件循环」才能被真实测到（TestClient 的 portal 线程测不到）。

运行：cd backend; uv run --frozen --extra dev pytest tests/test_audit_attachment_io_verify.py -q
"""

from __future__ import annotations

import asyncio
import time

import pytest

MB = 1_000_000
COPY_MAX = 100 * MB


async def _measure_loop_lag(work, *, tick: float = 0.005):
    """跑 work()，同时测事件循环的停顿：返回 (结果, 最大停顿 ms, 累计停顿 ms)。

    口径与 tests/test_interactive_during_heavy_work.py 一致：两次 5ms 心跳之间实际
    过去的时间就是「这一段时间里其它请求要等多久」。同步文件 I/O 占住事件循环时，
    停顿就等于那段 I/O 的时长。
    """
    gaps: list[float] = []
    stop = False
    last = time.perf_counter()

    async def _ticker() -> None:
        nonlocal last
        while not stop:
            await asyncio.sleep(tick)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    ticker = asyncio.create_task(_ticker())
    try:
        await asyncio.sleep(0)
        result = await work()
        for _ in range(50):
            seen = len(gaps)
            await asyncio.sleep(0)
            if len(gaps) > seen:
                break
    finally:
        stop = True
        ticker.cancel()
        import contextlib

        with contextlib.suppress(asyncio.CancelledError):
            await ticker
    # 心跳本身 5ms：只把明显超过一个心跳的间隔算成「被占住」
    blocked = [max(0.0, (gap - tick) * 1000) for gap in gaps]
    return result, max(gaps or [0.0]) * 1000, sum(blocked)


@pytest.fixture()
def async_app(db_conn, settings):
    from agent.api.server import create_app
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    return app


def _client(app):
    import httpx

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://qio.test", timeout=120.0)


async def _bounded_body(total: int, *, chunk: int = MB, counter: dict | None = None):
    """分块产出的请求体：httpx 用 Transfer-Encoding: chunked（**没有 Content-Length**）。"""
    sent = 0
    block = b"x" * chunk
    while sent < total:
        piece = min(chunk, total - sent)
        sent += piece
        if counter is not None:
            counter["sent"] = sent
        yield block[:piece]
        await asyncio.sleep(0)


# ---- 1. 没有 Content-Length 也必须有字节上限（核心红点） ---------------------------


async def test_upload_without_content_length_is_bounded(async_app):
    """Content-Length 缺失时不能无上限地把请求体读进内存。

    证据方式：客户端一共**愿意**发 105 MB，但服务器必须在读到上限附近就停下来拒绝
    （客户端记录的已发字节数明显小于 105 MB）；基线会把它全部吃掉再回 400 —— 红。
    """
    counter = {"sent": 0}
    async with _client(async_app) as client:
        before = len(async_app.state.ctx.attachments.list(check=False))
        resp = await client.post(
            "/api/attachments/upload",
            content=_bounded_body(105 * MB, counter=counter),
            headers={"Content-Type": "application/octet-stream", "X-QIO-Name": "too-big.bin"},
        )
        after = len(async_app.state.ctx.attachments.list(check=False))
        status = resp.status_code
        detail = resp.text[:300]

    assert 400 <= status < 500, (
        "超过上限的上传必须明确拒绝（不能装作成功）",
        status,
        detail,
    )
    assert counter["sent"] < 105 * MB, (
        "服务器把整个请求体都读完了（没有 Content-Length 时无字节上限）："
        "客户端已发 %d 字节" % counter["sent"],
    )
    assert counter["sent"] <= COPY_MAX + 8 * MB, (
        "有界接收的边界应当与「浏览器上传只用于 <= 100 MB」一致：客户端已发 %d 字节"
        % counter["sent"],
    )
    assert after == before, ("被拒绝的上传不得留下附件记录", before, after)


# ---- 2. 上传期间事件循环仍可推进 ---------------------------------------------------


async def test_upload_does_not_block_the_event_loop(async_app, tmp_path):
    """一次真实上传（含落盘 + 哈希）期间，事件循环不能被占住。

    基线：写盘与 hash 都在事件循环线程上同步做 —— 停顿随文件大小线性增长。
    """
    payload_total = 60 * MB
    payload = b"y" * payload_total

    async def _work():
        results = []
        async with _client(async_app) as client:
            for index in range(3):
                resp = await client.post(
                    "/api/attachments/upload",
                    content=payload,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "X-QIO-Name": "big-%d.bin" % index,
                    },
                )
                results.append(resp.status_code)
        return results

    statuses, max_gap_ms, blocked_ms = await _measure_loop_lag(_work)
    assert statuses == [200, 200, 200], ("上传本身应当成功", statuses)
    assert blocked_ms < 120, (
        "上传期间事件循环被同步文件 I/O 占住的总时长 %.0f ms（最大单次停顿 %.0f ms）："
        "期间其它请求/SSE/停止都会卡住" % (blocked_ms, max_gap_ms)
    )


# ---- 3. 重新定位期间事件循环仍可推进 -----------------------------------------------


async def test_relocate_does_not_block_the_event_loop(async_app, tmp_path):
    """重定位（重新复制 + 哈希）同样不能占住事件循环。"""
    source = tmp_path / "被引用的.bin"
    with open(source, "wb") as handle:
        handle.truncate(COPY_MAX + 1)  # 稀疏：只登记引用，不复制

    async with _client(async_app) as client:
        created = (
            await client.post("/api/attachments", json={"source_path": str(source)})
        ).json()["attachment"]
        attachment_id = str(created["id"])
        # 等到引用登记完成
        for _ in range(200):
            meta = (await client.get("/api/attachments/" + attachment_id)).json()["attachment"]
            if str(meta.get("state")) != "prepared":
                break
            await asyncio.sleep(0.02)
        assert str(meta.get("state")) == "ready", meta

    moved = tmp_path / "移动后的.bin"
    with open(moved, "wb") as handle:
        handle.write(b"z" * (90 * MB))

    async def _work():
        status = 0
        state = ""
        async with _client(async_app) as client:
            for _ in range(3):
                resp = await client.post(
                    "/api/attachments/%s/relocate" % attachment_id,
                    json={"source_path": str(moved)},
                )
                status = resp.status_code
                for _ in range(300):
                    meta = (
                        await client.get("/api/attachments/" + attachment_id)
                    ).json()["attachment"]
                    state = str(meta.get("state"))
                    if state != "prepared":
                        break
                    await asyncio.sleep(0.02)
        return status, state

    (status, state), max_gap_ms, blocked_ms = await _measure_loop_lag(_work)
    assert status in (200, 201, 202), (status, state)
    assert state == "ready", ("重定位最终必须真的把副本做出来", state)
    assert blocked_ms < 120, (
        "重定位期间事件循环被同步复制占住的总时长 %.0f ms（最大单次停顿 %.0f ms）"
        % (blocked_ms, max_gap_ms)
    )


# ---- 4. 数据库不许跨线程访问（防「把整个 service 丢进线程」的修法） -----------------


async def test_database_access_stays_on_one_thread(async_app):
    """文件 I/O 可以进工作线程，但数据库访问必须始终在同一个线程。

    这是 2026-10-06 CI 抓到的真缺陷（共享 sqlite 连接被两个线程并发使用 →
    InterfaceError 与幻影 404）。本用例是**防回归**：修复方案不得反向破坏它。
    """
    service = async_app.state.ctx.attachments
    source = b"w" * (2 * MB)

    async with _client(async_app) as client:
        resp = await client.post(
            "/api/attachments/upload",
            content=source,
            headers={"Content-Type": "application/octet-stream", "X-QIO-Name": "thread.bin"},
        )
        assert resp.status_code == 200, (resp.status_code, resp.text)
        attachment_id = str(resp.json()["attachment"]["id"])
        for _ in range(200):
            meta = (await client.get("/api/attachments/" + attachment_id)).json()["attachment"]
            if str(meta.get("state")) != "prepared":
                break
            await asyncio.sleep(0.02)

    assert service._db_thread is not None, "服务根本没有记录数据库线程"
    assert service._db_thread_warned is False, (
        "附件服务的数据库访问出现在第二个线程：同一个 sqlite 连接不能被两个线程同时使用"
    )
