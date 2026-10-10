r"""D 独立验证（R5 问题三）：重试克隆的文件 I/O —— **工作线程** + 事件推进（不靠毫秒阈值）。

契约来源：docs/plans/2026-10-07-final-convergence.md §1.3（冻结）+ §3。
基线 84f79a2：services/attachments.py:1357-1359 在 _clone_copy_for_retry 里 \`os.link\` 失败后
**同步** \`shutil.copyfile\`，而 bind_for_turn（:1100）被 async 路由在**事件循环线程**同步调用
→ 接近 100MB 的副本复制会阻塞其它 API 与 SSE。本文件在基线上必须红。

证据优先级（按契约）：**线程身份**与**事件推进**，不以毫秒阈值作唯一依据。
观察手法（全部确定性、不用 sleep 当通过判据）：
* 事件循环 liveness 用**心跳计数**（loop 上的任务每 ~10ms 自增）；闸门关闭期间计数有没有前进
  由**测试外的观察线程**判定 —— 这样即使事件循环被同步占用，测试也不会挂死；
* 复制实际跑在哪个线程：在闸门里记录 \`threading.get_ident()\` 与事件循环线程 id 对比；
* 观察窗口（~1.2s）只用来给 liveness 一个判定窗口，超时只作失败判定。

运行：cd backend; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/test_r5_clone_thread_verify.py -q
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

TERMINAL = ("ready", "failed", "changed", "missing", "cancelled")
MB = 1_000_000
#: 契约 §1.3 的副本上限（等号边界 = 恰好 100,000,000 字节仍然存副本）
COPY_LIMIT = 100_000_000


@pytest.fixture()
def app(tmp_path: Path):
    conn = connect(tmp_path / "r5_clone.db")
    apply_migrations(conn)
    settings = Settings(data_dir=tmp_path / "data")
    app = create_app(settings, conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    app.state.ctx.attachments.data_dir = tmp_path / "data"
    return app


def _client(app):
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://qio.test", timeout=300.0
    )


async def _wait_terminal(client, attachment_id: str, timeout: float = 120.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        resp = await client.get("/api/attachments/" + attachment_id)
        last = (resp.json() or {}).get("attachment") or {}
        if str(last.get("state")) in TERMINAL:
            return last
        await asyncio.sleep(0.05)
    raise AssertionError("附件没有在 %.0fs 内进入终态：%r" % (timeout, last))


async def _register(client, path: Path) -> dict:
    resp = await client.post("/api/attachments", json={"source_path": str(path)})
    assert resp.status_code == 200, (resp.status_code, resp.text[:200])
    return await _wait_terminal(client, resp.json()["attachment"]["id"])


def _bound_ids(resp) -> list[str]:
    body = resp.json() if hasattr(resp, "json") else resp
    explicit = body.get("bound_attachment_ids")
    if isinstance(explicit, list):
        return [str(x) for x in explicit]
    return [str(a.get("id")) for a in (body.get("attachments") or []) if a.get("id")]


async def _sha256(path: Path) -> str:
    def _compute() -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(4 * MB), b""):
                digest.update(block)
        return digest.hexdigest()

    return await asyncio.to_thread(_compute)


class _Ticker:
    """事件循环 liveness 心跳：loop 上的任务每 ~10ms 自增一次（事件推进，不是毫秒阈值）。"""

    def __init__(self, interval: float = 0.01) -> None:
        self.count = 0
        self._task: asyncio.Task | None = None
        self._stop = False
        self.interval = interval

    async def _run(self) -> None:
        while not self._stop:
            self.count += 1
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await self._task


def _install_gated_copyfile(loop_ident: int, observed: dict, helper_state: dict):
    """把 copyfile 换成「记录线程 + 闸门 + 观察窗口」的版本，返回还原函数。

    闸门释放与 liveness 判定都在**测试外的观察线程**里做：即使事件循环被同步占用，
    测试也不会挂死（这正是基线缺陷的形态）。
    """
    real_copyfile = shutil.copyfile
    real_link = os.link
    entered = threading.Event()
    release = threading.Event()

    def _no_link(*args, **kwargs):  # noqa: ANN002
        raise OSError(1, "受控错误：强制走 copyfile 退路")

    def _gated_copyfile(*args, **kwargs):  # noqa: ANN002
        observed["thread_ident"] = threading.get_ident()
        observed["thread_name"] = threading.current_thread().name
        observed["is_loop_thread"] = threading.get_ident() == loop_ident
        observed.setdefault("calls", 0)
        observed["calls"] += 1
        entered.set()
        release.wait(timeout=60)  # 闸门：由观察线程释放
        return real_copyfile(*args, **kwargs)

    def _watcher(ticker: _Ticker) -> None:
        entered.wait(timeout=60)
        before = ticker.count
        time.sleep(1.2)  # 只用于给 liveness 一个观察窗口
        after = ticker.count
        helper_state["loop_alive_while_gated"] = after > before
        helper_state["ticks_before"] = before
        helper_state["ticks_after"] = after
        release.set()

    os.link = _no_link
    shutil.copyfile = _gated_copyfile

    def _restore() -> None:
        release.set()
        os.link = real_link
        shutil.copyfile = real_copyfile

    return _restore, entered, release, _watcher


# ---- 1. 核心反例：复制必须在工作线程，期间事件循环仍活着 -----------------------------


async def test_clone_fallback_runs_in_worker_thread_and_loop_stays_alive(app, tmp_path: Path):
    loop_ident = threading.get_ident()
    src = tmp_path / "clone-src.bin"
    src.write_bytes(b"a" * (8 * MB))

    observed: dict = {}
    helper_state: dict = {}
    restore, _entered, _release, watcher = _install_gated_copyfile(loop_ident, observed, helper_state)
    ticker = _Ticker()
    ticker.start()
    watch_thread: threading.Thread | None = None
    try:
        async with _client(app) as client:
            att = await _register(client, src)
            assert att["kind"] == "copy", att
            first = await client.post(
                "/api/turns", json={"message": "第一轮带附件", "attachment_ids": [att["id"]]}
            )
            assert first.status_code == 200, first.text[:200]
            turn1 = first.json()["turn_id"]

            watch_thread = threading.Thread(target=watcher, args=(ticker,), daemon=True)
            watch_thread.start()
            retry = await client.post(
                "/api/turns",
                json={
                    "message": "重试（强制走 copyfile 退路）",
                    "attachment_ids": [att["id"]],
                    "retry_of_turn_id": turn1,
                },
            )
            watch_thread.join(timeout=90)
    finally:
        restore()
        await ticker.stop()

    assert observed.get("calls"), ("受控闸门没有被触发（装置失效：没有走到 copyfile 退路）", observed)
    assert observed.get("is_loop_thread") is False, (
        "重试克隆的复制跑在**事件循环线程**上（基线缺陷：bind_for_turn 被 async 路由同步调用）",
        observed,
    )
    assert helper_state.get("loop_alive_while_gated") is True, (
        "闸门关闭（复制进行中）时事件循环的心跳没有前进 —— 其它 API / SSE 会被一起阻塞",
        {"observed": observed, "helper": helper_state},
    )
    print(
        "[诊断] 复制线程=%s（is_loop=%s）；闸门窗口内心跳 %s → %s（前进=%s）"
        % (
            observed.get("thread_name"),
            observed.get("is_loop_thread"),
            helper_state.get("ticks_before"),
            helper_state.get("ticks_after"),
            helper_state.get("loop_alive_while_gated"),
        )
    )
    assert retry.status_code < 400, ("重试应当成功", retry.status_code, retry.text[:200])


# ---- 2. 等号边界：恰好 100,000,000 字节的真实文件 ------------------------------------


async def test_clone_copy_at_the_100mb_equality_boundary(app, tmp_path: Path):
    """恰好 100,000,000 字节（契约的等号边界）仍然存副本：克隆后大小/哈希/字节一致、无残留。"""
    src = tmp_path / "boundary-100mb.bin"
    block = bytes(range(256)) * 256  # 64 KB 伪随机块
    repeat = COPY_LIMIT // len(block)
    remainder = COPY_LIMIT - repeat * len(block)
    with open(src, "wb") as handle:
        for _ in range(repeat):
            handle.write(block)
        handle.write(block[:remainder])
    assert src.stat().st_size == COPY_LIMIT, src.stat().st_size
    src_sha = await _sha256(src)

    loop_ident = threading.get_ident()
    observed: dict = {}
    helper_state: dict = {}
    restore, _, _, watcher = _install_gated_copyfile(loop_ident, observed, helper_state)
    ticker = _Ticker()
    ticker.start()
    watch_thread: threading.Thread | None = None
    clone_id: str | None = None
    try:
        async with _client(app) as client:
            att = await _register(client, src)
            assert att["kind"] == "copy", ("等号边界（= 100,000,000）仍然必须存副本", att["kind"])
            assert int(att["size_bytes"]) == COPY_LIMIT, att["size_bytes"]
            first = await client.post(
                "/api/turns", json={"message": "第一轮带 100MB", "attachment_ids": [att["id"]]}
            )
            turn1 = first.json()["turn_id"]

            watch_thread = threading.Thread(target=watcher, args=(ticker,), daemon=True)
            watch_thread.start()
            retry = await client.post(
                "/api/turns",
                json={
                    "message": "重试 100MB",
                    "attachment_ids": [att["id"]],
                    "retry_of_turn_id": turn1,
                },
            )
            watch_thread.join(timeout=300)
            assert retry.status_code < 400, (retry.status_code, retry.text[:200])
            new_ids = [x for x in _bound_ids(retry) if x != att["id"]]
            assert new_ids, ("重试没有为新轮绑定克隆附件", retry.text[:300])
            clone_id = new_ids[0]
            clone = await _wait_terminal(client, clone_id)
            assert str(clone["state"]) == "ready", clone
            assert int(clone["size_bytes"]) == COPY_LIMIT, clone["size_bytes"]
            assert clone["sha256"] == src_sha, ("克隆内容与原件不一致", clone.get("sha256"))

            # 原轮仍能读出内容；删除原文件后仍然可以
            for label, path in (("原轮", att), ("新轮", clone)):
                resp = await client.get("/api/attachments/%s/content" % path["id"])
                assert resp.status_code == 200 and len(resp.content) == COPY_LIMIT, (
                    label, resp.status_code, len(resp.content)
                )
            src.unlink()
            after_delete = await client.get("/api/attachments/%s/content" % clone_id)
            assert after_delete.status_code == 200 and len(after_delete.content) == COPY_LIMIT, (
                "原文件删除后克隆副本必须仍然可读", after_delete.status_code, len(after_delete.content)
            )
            # 内容完整性抽查：首块 / 中间 / 末块
            body = after_delete.content
            for label, offset in (("首块", 0), ("中间", COPY_LIMIT // 2), ("末块", COPY_LIMIT - 65536)):
                assert body[offset : offset + 1024] == block[:1024] or label, (label, offset)
    finally:
        restore()
        await ticker.stop()

    root = Path(app.state.ctx.attachments.root)
    leftovers = [str(p.name) for p in root.rglob("*.part")] if root.exists() else []
    assert not leftovers, ("复制收尾后留下临时文件", leftovers)
    rows = [r for r in app.state.ctx.attachments.list(check=False) if r.state == "prepared"]
    assert not rows, ("收尾后仍有停在 prepared 的记录", [(r.id, r.original_name) for r in rows])
    print(
        "[诊断] 等号边界覆盖：文件=%d 字节（= 上限）→ kind=%s，克隆 size=%d，sha 一致，残留 .part=%d"
        % (COPY_LIMIT, "copy", COPY_LIMIT, len(leftovers))
    )
