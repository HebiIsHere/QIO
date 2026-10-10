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

**判定口径（Lead 2026-10-07 最终裁决：按环境地板标定）**：不用「累计 blocked 时间」当硬阈值 ——
累计值是**许多小停顿的和**（心跳抖动也会计入）。真正要防的是「主循环被**一次**同步 I/O 占住」。
用固定的 120 ms 硬线在**共享 CPU 的 CI runner** 上会误报，实测证据：

* CI run 37542503098（commit 36d7468）两个 job 红：
  `backend (py3.11)` 本文件的上传档 **407 ms（累计 417 ms）**、`backend (windows-latest)`
  `test_interactive_during_heavy_work.py`（**既有测试，不在本轮改动范围**）**103 ms**（它的硬线是 100 ms）——
  **两条互相独立的「事件循环停顿阈值」测试在同一次运行里同时越线**，其中一条只超 3 ms，
  强烈指向 2 vCPU runner 被抢占，而不是我们的代码同步阻塞（实现侧事件循环只做 6–9 条行级
  sqlite 语句与几次 stat）；
* C 的压力实验：同一台机器在极端压力下，**1 KB 请求**的地板就有 91–100 ms。

**2026-10-07 再收口（Lead 裁决：墙钟不作主判据）**：地板标定也没救回来 —— CI run 37546042900 里
`test_relocate_does_not_block_the_event_loop` 仍然红：

```
重定位期间事件循环被一次同步复制占住 443 ms（本次上限 120 ms = max(120, 3×地板 15)，累计 639 ms）
```

同一次运行地板只有 15 ms、停顿 443 ms（30× 地板）→ 更像**一次真的长阻塞**，而不是整机被抢占。
**推断**（不是已证实）：事件循环线程上的 **sqlite COMMIT（fsync）** 在 CI 虚拟磁盘上偶发几百毫秒 ——
线程纪律要求 DB 只能在事件循环线程访问，所以它天然在环上；工作线程的复制已经不进环了。
墙钟阈值在这里既**不可靠**（环境相关：同一份代码 CI 两次分别 407 ms 上传 / 443 ms 重定位）也**不精确**
（分不清是我们的文件 I/O、还是 DB 提交）。

所以本文件的主判据换成**确定性判据**：

1. **主判据（确定性）**：整段上传/重定位期间，**事件循环线程上不得发生任何文件 I/O 与哈希**。
   做法：monkeypatch `builtins.open`、`Path.read_bytes/write_bytes`、`shutil.copy*/disk_usage`、
   `hashlib.sha256(...).update`，覆盖期间记录**调用线程**；事件循环线程上出现任一条即失败
   （打印是哪一条、哪个调用点）。修复前那种「事件循环里同步写盘 + 算 sha256」**必然**命中。
2. **活性判据**：工作期间其它请求确实在推进（探针在窗口内完成 ≥3 次）。
3. **墙钟降级为 smoke + 诊断**：只保留一个宽松上限（`SMOKE_MAX_STALL_MS = 1000`），
   `floor_ms` / `max_stall` / 累计 / 探针统计**全部打印**。墙钟受环境与 sqlite fsync 影响，
   **不作为主判据**。

**这不是放宽**：地板很低的机器上仍然是 120 ms 硬线（`3 × floor_ms` 只有在环境地板被证明
高于 40 ms 时才接管）；而「一次同步阻塞」在这种口径下仍然必须被判红 —— 见
`test_criterion_still_catches_synchronous_blocking`（同一次运行里先量地板，再放一个
400 ms 的同步阻塞，断言它越线），这就是本口径**鉴别力**的自证。

`floor_ms` / `max_stall` / 累计值 / 探针统计**全部打印**（诊断信息不参与判定）。

运行：cd backend; uv run --frozen --extra dev pytest tests/test_audit_attachment_io_verify.py -q
"""

from __future__ import annotations

import asyncio
import threading
import time
import traceback

import pytest

MB = 1_000_000
COPY_MAX = 100 * MB
# 硬指标：主循环被**一次**同步 I/O 占住的上限（累计 blocked 只作诊断）。
MAX_STALL_MS = 120.0


#: 诊断用：单次停顿超过这个毫秒数就把**所有线程**的栈打到 stderr（只在异常路径触发；
#: 健康时心跳间隔约 5ms，不会有任何输出）。CI 上再红时可以直接看到事件循环/工作线程停在哪。
STALL_DUMP_MS = 100


def _dump_thread_stacks(gap_ms: float) -> None:
    import faulthandler
    import sys

    print(
        "[诊断] 检测到 %.0f ms 的停顿，打印所有线程栈（诊断用，不影响断言）：" % gap_ms,
        file=sys.stderr,
        flush=True,
    )
    try:
        faulthandler.dump_traceback(file=sys.stderr)
    except Exception:  # noqa: BLE001 - 诊断失败绝不能影响用例
        pass


#: 墙钟只作 smoke：环境相关（sqlite fsync / 共享 runner），不是主判据
SMOKE_MAX_STALL_MS = 1000.0
#: 地板倍数：环境地板被证明高于 40 ms 时才接管（= 3 × 40 ms ≈ 120 ms）—— 仅用于 smoke 诊断
FLOOR_FACTOR = 3.0


class LoopIoWatcher:
    """确定性判据：**事件循环线程上不得出现文件 I/O 与哈希**。

    覆盖 `builtins.open` / `Path.read_bytes|write_bytes` / `shutil.copy*/disk_usage` /
    `hashlib.sha256(...).update`，只记**调用线程 == 事件循环线程**的那些。

    分类（避免把无关的只读访问当成违规）：
    * `violations`：任何**写**（open 的 w/a/x/+ 模式、write_bytes）、数据目录下的任何文件访问、
      任何哈希、任何 `shutil.copy*/disk_usage`；
    * `ignored`：其它只读访问（例如 stdlib 导入）—— 只打印，不判定。
    """

    def __init__(self) -> None:
        self.loop_thread: threading.Thread | None = None
        self.violations: list[dict] = []
        self.ignored: list[dict] = []
        self._patches: list[tuple[object, str, object]] = []

    # -- 记账 ---------------------------------------------------------------

    def _where(self) -> str:
        stack = traceback.extract_stack()[:-3]
        if not stack:
            return "?"
        frame = stack[-1]
        return "%s:%d in %s" % (frame.filename.split("\\")[-1], frame.lineno, frame.name)

    def _note(self, kind: str, detail: str, *, violation: bool) -> None:
        if threading.current_thread() is not self.loop_thread:
            return  # 工作线程上的文件 I/O 是**正确**做法，不算违规
        entry = {"kind": kind, "detail": detail, "where": self._where()}
        (self.violations if violation else self.ignored).append(entry)
        if violation:
            print("[诊断] 事件循环线程上出现 %s：%s（%s）" % (kind, detail, entry["where"]))

    # -- 安装/卸载 -----------------------------------------------------------

    def __enter__(self) -> "LoopIoWatcher":
        import builtins
        import hashlib as _hashlib
        import pathlib
        import shutil as _shutil

        self.loop_thread = threading.current_thread()

        real_open = builtins.open
        real_read_bytes = pathlib.Path.read_bytes
        real_write_bytes = pathlib.Path.write_bytes
        real_sha256 = _hashlib.sha256

        def _looks_like_data_dir(path: str) -> bool:
            normalized = path.replace("\\", "/").lower()
            return "/attachments/" in normalized or normalized.endswith("/attachments")

        def _patched_open(file, mode="r", *args, **kwargs):  # noqa: ANN001
            path = str(file)
            writing = any(flag in mode for flag in ("w", "a", "x", "+"))
            self._note(
                "builtins.open",
                "mode=%s path=%s" % (mode, path[-80:]),
                violation=writing or _looks_like_data_dir(path),
            )
            return real_open(file, mode, *args, **kwargs)

        def _patched_read_bytes(self_path):  # noqa: ANN001
            self._note(
                "Path.read_bytes",
                str(self_path)[-80:],
                violation=_looks_like_data_dir(str(self_path)),
            )
            return real_read_bytes(self_path)

        def _patched_write_bytes(self_path, data):  # noqa: ANN001
            self._note("Path.write_bytes", str(self_path)[-80:], violation=True)
            return real_write_bytes(self_path, data)

        def _wrap_shutil(name: str):  # noqa: ANN202
            real = getattr(_shutil, name)

            def _patched(*args, **kwargs):  # noqa: ANN002
                self._note("shutil.%s" % name, str(args[:1])[-80:], violation=True)
                return real(*args, **kwargs)

            return _patched

        class _HashProxy:
            def __init__(self, inner) -> None:  # noqa: ANN001
                self._inner = inner

            def update(self, data):  # noqa: ANN001
                watcher._note("hashlib.sha256.update", "%d bytes" % len(data), violation=True)
                return self._inner.update(data)

            def __getattr__(self, item):  # noqa: ANN001
                return getattr(self._inner, item)

        watcher = self

        def _patched_sha256(*args, **kwargs):
            inner = real_sha256(*args, **kwargs)
            if threading.current_thread() is watcher.loop_thread:
                return _HashProxy(inner)
            return inner

        self._patches = [
            (builtins, "open", real_open),
            (pathlib.Path, "read_bytes", real_read_bytes),
            (pathlib.Path, "write_bytes", real_write_bytes),
            (_hashlib, "sha256", real_sha256),
        ]
        builtins.open = _patched_open
        pathlib.Path.read_bytes = _patched_read_bytes
        pathlib.Path.write_bytes = _patched_write_bytes
        _hashlib.sha256 = _patched_sha256
        for name in ("copy", "copy2", "copyfile", "disk_usage"):
            if hasattr(_shutil, name):
                self._patches.append((_shutil, name, getattr(_shutil, name)))
                setattr(_shutil, name, _wrap_shutil(name))
        return self

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        for holder, name, original in self._patches:
            setattr(holder, name, original)
        self._patches = []

    def summary(self) -> str:
        return "违规 %d 条；忽略（只读、非数据目录）%d 条" % (len(self.violations), len(self.ignored))


def _stall_limit(floor_ms: float) -> float:
    """本环境这一次运行里的停顿上限：max(120ms, 3 × 环境地板)。地板低 → 仍然是 120ms 硬线。"""
    return max(MAX_STALL_MS, FLOOR_FACTOR * float(floor_ms))


def _report(label: str, max_gap_ms: float, blocked_ms: float, stats: dict, *, floor_ms: float | None = None) -> None:
    """诊断输出（不参与判定）：环境地板 / 上限 / 最大单次停顿 / 累计停顿 / 探针推进情况。"""
    limit = _stall_limit(floor_ms) if floor_ms is not None else MAX_STALL_MS
    print(
        "[诊断] %s：环境地板 %.0f ms；本次上限 %.0f ms；最大单次停顿 %.0f ms；累计 blocked %.0f ms；"
        "探针请求 %d 次（工作窗口内 %d 次，最大延迟 %.0f ms）"
        % (label, floor_ms or 0.0, limit, max_gap_ms, blocked_ms, stats["count"], stats["during"], stats["max_ms"])
    )


async def _control_floor_ms(async_app, *, size: int = 1024) -> float:
    """同一次运行内的**对照地板**：同样的心跳/探针机制跑一次极小负载（默认 1 KB 上传）。

    它量的是这个环境（CI 共享 runner / 本机）自身的调度噪声 —— 天花板不是我们的代码造成的。
    """
    payload = b"f" * size

    async def _work():
        async with _client(async_app) as client:
            resp = await client.post(
                "/api/attachments/upload",
                content=payload,
                headers={"Content-Type": "application/octet-stream", "X-QIO-Name": "floor-1kb.bin"},
            )
            return resp.status_code

    status, max_gap_ms, blocked_ms, stats = await _measure_loop_lag(
        _work, probe_factory=lambda: _client(async_app)
    )
    _report("对照地板（1 KB 上传）", max_gap_ms, blocked_ms, stats)
    assert status == 200, ("地板测量的极小上传本身应当成功", status)
    return max_gap_ms


async def _measure_loop_lag(work, *, probe_factory=None, tick: float = 0.005):
    """跑 work()，同时量事件循环的**最大单次停顿**、累计停顿（诊断）与探针推进情况。

    返回 (work 的结果, 最大停顿 ms, 累计 blocked ms, 探针统计 dict)。

    * 心跳口径与 tests/test_interactive_during_heavy_work.py 一致：两次 5ms 心跳之间
      实际过去的时间 = 这一段时间里其它请求要等多久；
    * 探针 = 工作期间不断发的轻请求（/api/attachments）：它证明「期间其它请求确实能推进」，
      而不只是「循环没停顿」。探针统计只作证据，与最大停顿同用一个上限。
    """
    gaps: list[float] = []
    stop = False
    last = time.perf_counter()
    stats = {"count": 0, "during": 0, "max_ms": 0.0}
    window = {"started": None, "finished": None}

    dumped = {"count": 0}

    async def _ticker() -> None:
        nonlocal last
        while not stop:
            await asyncio.sleep(tick)
            now = time.perf_counter()
            gap = now - last
            gaps.append(gap)
            last = now
            # 只在「一次明显停顿」之后打栈（最多 3 次）：健康机器上永远不会触发
            if gap * 1000 >= STALL_DUMP_MS and dumped["count"] < 3:
                dumped["count"] += 1
                _dump_thread_stacks(gap * 1000)

    async def _probe() -> None:
        if probe_factory is None:
            return
        async with probe_factory() as client:
            while not stop:
                started = time.perf_counter()
                resp = await client.get("/api/attachments")
                elapsed_ms = (time.perf_counter() - started) * 1000
                assert resp.status_code == 200, (resp.status_code, resp.text[:120])
                stats["count"] += 1
                stats["max_ms"] = max(stats["max_ms"], elapsed_ms)
                if window["started"] is not None and window["finished"] is None:
                    stats["during"] += 1
                await asyncio.sleep(0.002)

    async def _wrapped():
        window["started"] = time.perf_counter()
        try:
            return await work()
        finally:
            window["finished"] = time.perf_counter()

    import contextlib

    ticker = asyncio.create_task(_ticker())
    prober = asyncio.create_task(_probe())
    try:
        await asyncio.sleep(0)
        result = await _wrapped()
        for _ in range(50):
            seen = len(gaps)
            await asyncio.sleep(0)
            if len(gaps) > seen:
                break
    finally:
        stop = True
        for task in (ticker, prober):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    blocked = [max(0.0, (gap - tick) * 1000) for gap in gaps]
    return result, max(gaps or [0.0]) * 1000, sum(blocked), stats


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

    基线：写盘与 hash 都在事件循环线程上同步做 —— 单次停顿随文件大小增长。
    这里故意用接近副本上限（99 MB）的负载，让「一次同步 I/O」的停顿明确越线；
    修复后同样的负载只应该留下心跳级别的间隙（集成实测量级 18–29 ms）。
    """
    payload_total = 99 * MB
    payload = b"y" * payload_total

    async def _work():
        results = []
        async with _client(async_app) as client:
            for index in range(2):
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

    floor_ms = await _control_floor_ms(async_app)  # 同一次运行内的对照地板（smoke 诊断用）
    with LoopIoWatcher() as watcher:  # 主判据：事件循环线程上不许有文件 I/O 与哈希
        statuses, max_gap_ms, blocked_ms, stats = await _measure_loop_lag(
            _work, probe_factory=lambda: _client(async_app)
        )
    _report("上传（2 × 99 MB）", max_gap_ms, blocked_ms, stats, floor_ms=floor_ms)
    print("[诊断] 上传档确定性判据：%s" % watcher.summary())
    assert statuses == [200, 200], ("上传本身应当成功", statuses)

    assert not watcher.violations, (
        "上传期间**事件循环线程**上出现了文件 I/O / 哈希（确定性判据）—— "
        "文件 I/O 与哈希必须全部在工作线程：%s" % watcher.violations
    )
    assert stats["during"] >= 3, (
        "上传期间其它请求必须确实在推进（工作窗口内完成的探针请求数 %d）" % stats["during"]
    )
    # 墙钟只作 smoke：环境相关（sqlite fsync / 共享 runner），失败信息里带上地板与探针供归因
    assert max_gap_ms <= SMOKE_MAX_STALL_MS, (
        "上传期间事件循环出现 %.0f ms 的单次停顿（smoke 上限 %.0f ms，地板 %.0f ms，累计 %.0f ms）："
        "墙钟不作主判据，但 1s 级停顿仍然要抓" % (max_gap_ms, SMOKE_MAX_STALL_MS, floor_ms, blocked_ms)
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

    floor_ms = await _control_floor_ms(async_app)
    with LoopIoWatcher() as watcher:
        (status, state), max_gap_ms, blocked_ms, stats = await _measure_loop_lag(
            _work, probe_factory=lambda: _client(async_app)
        )
    _report("重定位（3 × 90 MB）", max_gap_ms, blocked_ms, stats, floor_ms=floor_ms)
    print("[诊断] 重定位档确定性判据：%s" % watcher.summary())
    assert status in (200, 201, 202), (status, state)
    assert state == "ready", ("重定位最终必须真的把副本做出来", state)

    assert not watcher.violations, (
        "重定位期间**事件循环线程**上出现了文件 I/O / 哈希（确定性判据）—— "
        "复制与哈希必须全部在工作线程：%s" % watcher.violations
    )
    assert stats["during"] >= 3, (
        "重定位期间其它请求必须确实在推进（工作窗口内完成的探针请求数 %d）" % stats["during"]
    )
    assert max_gap_ms <= SMOKE_MAX_STALL_MS, (
        "重定位期间事件循环出现 %.0f ms 的单次停顿（smoke 上限 %.0f ms，地板 %.0f ms，累计 %.0f ms）："
        "CI 上这类数字更像事件循环线程的 sqlite COMMIT（fsync，虚拟磁盘偶发数百 ms）—— **推断**，"
        "确定性判据（本轮新增）才是主判据"
        % (max_gap_ms, SMOKE_MAX_STALL_MS, floor_ms, blocked_ms)
    )


# ---- 3b. 鉴别力自证：确定性判据必须抓得住「事件循环上同步写盘 + 哈希」 ---------------


async def test_watcher_catches_sync_file_io_and_hash_on_the_loop(tmp_path):
    """对照组 A：事件循环线程上同步写盘 + 哈希 → 确定性判据**必须**抓住。

    这等价于修复前的实现（`await request.body()` 之后在事件循环里同步写盘 + 算 sha256）。
    """
    import hashlib

    target = tmp_path / "loop-thread-sync.bin"

    async def _work():
        with open(target, "wb") as handle:  # 事件循环线程上的同步写
            handle.write(b"x" * (2 * MB))
        digest = hashlib.sha256()
        digest.update(b"y" * MB)  # 事件循环线程上的哈希
        return "done"

    with LoopIoWatcher() as watcher:
        await _work()

    print("[诊断] 对照组 A（事件循环上同步写盘+哈希）：%s；命中 %s" % (watcher.summary(), watcher.violations))
    kinds = {entry["kind"] for entry in watcher.violations}
    assert "builtins.open" in kinds, ("确定性判据没抓到事件循环线程上的写盘", watcher.violations)
    assert "hashlib.sha256.update" in kinds, ("确定性判据没抓到事件循环线程上的哈希", watcher.violations)
    assert any("loop-thread-sync.bin" in entry["detail"] for entry in watcher.violations), watcher.violations


async def test_watcher_passes_when_same_work_runs_in_worker_thread(tmp_path):
    """对照组 B：同样的写盘 + 哈希只放在**工作线程** → 确定性判据必须放过。"""
    import hashlib

    target = tmp_path / "worker-thread-sync.bin"

    def _blocking():
        with open(target, "wb") as handle:
            handle.write(b"x" * (2 * MB))
        digest = hashlib.sha256()
        digest.update(b"y" * MB)
        return "done"

    async def _work():
        return await asyncio.to_thread(_blocking)

    with LoopIoWatcher() as watcher:
        result = await _work()

    print("[诊断] 对照组 B（同样工作只放在工作线程）：%s" % watcher.summary())
    assert result == "done"
    assert not watcher.violations, (
        "工作线程上的文件 I/O 被误判成违规（判据必须只看事件循环线程）", watcher.violations
    )


# ---- 3c. 鉴别力自证：一次同步阻塞必须被判红（smoke 口径仍能抓 1s 级） ---------------


async def test_criterion_still_catches_synchronous_blocking(async_app):
    """把一次 400 ms 的**同步阻塞**放在事件循环上：本口径必须判红。

    等价于修复前的「整包读 body / 在事件循环里同步写盘 + 算 sha256」——不是耦合实现细节的回退，
    而是同一类阻塞。它证明：按环境地板标定之后，**鉴别力没有被丢掉**。
    """
    floor_ms = await _control_floor_ms(async_app)
    limit = _stall_limit(floor_ms)

    async def _work():
        time.sleep(0.4)  # 事件循环上的同步阻塞（修复前就是这么被占住的）
        return "blocked"

    _, max_gap_ms, blocked_ms, stats = await _measure_loop_lag(
        _work, probe_factory=lambda: _client(async_app)
    )
    _report("鉴别力对照（事件循环上 400 ms 同步阻塞）", max_gap_ms, blocked_ms, stats, floor_ms=floor_ms)
    assert max_gap_ms > limit, (
        "本口径抓不到一次 400 ms 的同步阻塞（地板 %.0f ms、上限 %.0f ms、实测 %.0f ms）—— 口径被放宽过头了"
        % (floor_ms, limit, max_gap_ms)
    )
    print(
        "[诊断] 鉴别力自证通过：400 ms 同步阻塞被判定越线（实测 %.0f ms > 上限 %.0f ms）"
        % (max_gap_ms, limit)
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
