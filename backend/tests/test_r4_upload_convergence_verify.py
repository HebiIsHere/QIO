r"""D 独立验证（R4 问题三）：上传失败/取消必须**收敛**（有限时间内结束 + 准确反馈 + 不留垃圾）。

契约来源：docs/plans/2026-10-07-three-remaining-fixes.md §1.3（冻结）。用户可见规则（plan §3）：
* 创建目录 / 打开临时文件 / 写入途中 任一处受控失败，上传都要在**有限时间内**结束并**准确反馈**；
* 状态、临时文件、任务、线程全部收敛：不得停在 prepared、不得留 .part；
* 分块数超过队列容量（UPLOAD_QUEUE_DEPTH=4）时，工作线程死了接收端也必须立刻结束（不许死等空位）；
* 取消要能解除阻塞；取消之后**绝不**提交 ready；
* 同期其它 API 仍能推进。

基线（ccb5734）现状：_upload_queue_put 只循环等空位、不观察工作线程是否已结束（server.py:103-110），
接收端把结束/哨兵也送进同一个满队列（:1243）→ 工作线程提前失败后请求一直等下去、附件停在 prepared。

时序用**事件/闸门**控制（不用 sleep 当证据；有限超时只用来判定失败）。
请求体用 httpx ASGITransport + 异步分块生成器，保证**每一块都真的是一次 request.stream()**，
分块数远超队列容量（与 phase-1 的 test_audit_attachment_io_verify.py 同一手法）。

运行：cd backend; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/test_r4_upload_convergence_verify.py -q
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import io
import threading
import time
from pathlib import Path

import pytest

from agent.api.server import UPLOAD_QUEUE_DEPTH, create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

CHUNK = b"q" * 65536
CHUNKS = UPLOAD_QUEUE_DEPTH * 4 + 8  # 远超队列容量：接收端一定会撞上「没有空位」
BOUND_S = 12.0  # 只用来判定失败，不当证据


@pytest.fixture()
def app(tmp_path: Path):
    conn = connect(tmp_path / "r4_upload.db")
    apply_migrations(conn)
    settings = Settings(data_dir=tmp_path / "data")
    app = create_app(settings, conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    # 隔离：Settings(data_dir=...) 会被全局默认覆盖（实测仍是 D:\\QIO-data），
    # 直接钉住附件服务的落盘根目录，别把测试产物写进开发者真实数据目录。
    app.state.ctx.attachments.data_dir = tmp_path / "data"
    return app


def _client(app):
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://qio.test", timeout=120.0
    )


def _root(app) -> Path:
    return Path(app.state.ctx.attachments.root)


def _temp_files(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(str(p.name) for p in root.rglob("*.part"))


def _records(app) -> list[dict]:
    return app.state.ctx.attachments.list(check=False)


async def _chunked_body(chunks: int):
    for _ in range(chunks):
        yield CHUNK
        await asyncio.sleep(0)  # 让 ASGI 把每一块当成一次 request.stream()


class _FailingHandle:
    """把工作线程里的写操作变成受控失败（或闸门）。"""

    def __init__(self, handle, state, action: str) -> None:
        self._handle = handle
        self._state = state
        self._action = action
        self._calls = 0

    def write(self, data):  # noqa: ANN001
        self._calls += 1
        self._state["writes"] += 1
        if self._action == "gate":
            self._state["inside"].set()
            self._state["release"].wait(timeout=BOUND_S)
        elif self._action == "write" and self._calls == 1:
            raise OSError(28, "受控错误：写入途中失败（磁盘空间不足）")
        return self._handle.write(data)

    def __getattr__(self, name):
        return getattr(self._handle, name)

    def __enter__(self):
        self._handle.__enter__()
        return self

    def __exit__(self, *exc):
        return self._handle.__exit__(*exc)

    def __iter__(self):
        return iter(self._handle)


@contextlib.contextmanager
def _controlled_fs(root: Path, action: str):
    """在附件根目录内制造受控的 open/write 失败（action: open | write | gate）。"""
    real_open, real_io_open = builtins.open, io.open
    needle = str(root).replace("\\", "/").lower()  # 两侧都归一化，否则永远匹配不上（实测踩过）
    state: dict = {
        "fired": False,
        "writes": 0,
        "inside": threading.Event(),
        "release": threading.Event(),
    }

    def _wrap(real):
        def _open(file, mode="r", *args, **kwargs):
            path = str(file)
            normalized = path.replace("\\", "/").lower()
            writing = any(flag in mode for flag in ("w", "a", "x"))
            if writing and needle in normalized:
                state["fired"] = True
                if action == "open" and path.endswith(".part"):
                    raise PermissionError(13, "受控错误：打开临时文件失败")
                handle = real(file, mode, *args, **kwargs)
                if action in ("write", "gate"):
                    return _FailingHandle(handle, state, action)
                return handle
            return real(file, mode, *args, **kwargs)

        return _open

    builtins.open, io.open = _wrap(real_open), _wrap(real_io_open)
    try:
        yield state
    finally:
        builtins.open, io.open = real_open, real_io_open


async def _upload(client, name: str, chunks: int = CHUNKS) -> dict:
    started = time.monotonic()
    try:
        resp = await asyncio.wait_for(
            client.post(
                "/api/attachments/upload",
                content=_chunked_body(chunks),
                headers={"content-type": "application/octet-stream", "x-qio-name": name},
            ),
            timeout=BOUND_S,
        )
        payload = {}
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001 - 非 JSON 错误体
            payload = {}
        return {
            "status": resp.status_code,
            "state": (payload.get("attachment") or {}).get("state"),
            "error": (payload.get("attachment") or {}).get("error"),
            "text": resp.text[:300],
            "elapsed": round(time.monotonic() - started, 3),
        }
    except asyncio.TimeoutError:
        return {
            "status": "timeout",
            "text": "上传请求在 %.1fs 内没有返回" % BOUND_S,
            "elapsed": round(time.monotonic() - started, 3),
        }
    except BaseException as exc:  # noqa: BLE001 - 未处理异常也算「没有准确反馈」
        return {
            "status": "exception",
            "text": "%s: %s" % (type(exc).__name__, exc),
            "elapsed": round(time.monotonic() - started, 3),
        }


def _assert_converged(outcome: dict, rows: list[dict], root: Path, label: str) -> None:
    assert outcome["status"] != "timeout", (
        label + "：工作线程早已失败，上传请求却一直不返回（接收端在满队列上死等空位）", outcome
    )
    assert outcome["status"] != "exception", (label + "：以未处理异常收场，没有准确反馈", outcome)
    failing = outcome["status"] >= 400 if isinstance(outcome["status"], int) else False
    assert failing or outcome.get("state") in ("failed", "cancelled"), (
        label + "：既没有 HTTP 错误，也没有如实给出失败状态（用户会以为上传成功）", outcome
    )
    for row in rows:
        assert row.state in ("failed", "cancelled"), (
            label + "：附件停在非终态（用户会一直以为在准备中）", row.state, row.error
        )
        assert row.error, (label + "：failed 必须带人话原因", row.state)
    assert not _temp_files(root), (label + "：失败后留下临时文件", _temp_files(root))


# ---- 1. 写入途中失败（分块数远超队列容量） ------------------------------------------


async def test_write_failure_midway_unblocks_receiver(app):
    root = _root(app)
    async with _client(app) as client:
        with _controlled_fs(root, "write") as state:
            outcome = await _upload(client, "write-fail.txt")
    assert state["fired"], "受控补丁没有拦到写盘（测试装置失效，不是产品结论）"
    _assert_converged(outcome, _records(app), root, "写入途中失败")


# ---- 2. 创建目录失败（真实文件系统制造，不打桩） ------------------------------------


async def test_mkdir_failure_is_reported_and_converges(app):
    root = _root(app)
    root.parent.mkdir(parents=True, exist_ok=True)
    root.write_text("这里本该是目录（受控错误：创建目录失败）", encoding="utf-8")
    async with _client(app) as client:
        outcome = await _upload(client, "mkdir-fail.txt", chunks=UPLOAD_QUEUE_DEPTH + 2)
    root.unlink()
    _assert_converged(outcome, _records(app), _root(app), "创建目录失败")


# ---- 3. 打开临时文件失败 -------------------------------------------------------------


async def test_temp_open_failure_is_reported_and_converges(app):
    root = _root(app)
    async with _client(app) as client:
        with _controlled_fs(root, "open") as state:
            outcome = await _upload(client, "temp-open-fail.txt")
    assert state["fired"], "受控补丁没有拦到临时文件 open（测试装置失效）"
    _assert_converged(outcome, _records(app), root, "打开临时文件失败")


# ---- 4. 取消：解除阻塞 + 绝不提交 ready（取消后提交竞态） ---------------------------


async def test_cancel_while_worker_blocked_ends_request_and_never_commits(app):
    root = _root(app)
    async with _client(app) as client:
        with _controlled_fs(root, "gate") as state:
            task = asyncio.create_task(_upload(client, "cancel-inflight.txt"))
            # 闸门：工作线程确实已经进到写盘里（事件驱动，不用 sleep 当证据）
            entered = await asyncio.to_thread(state["inside"].wait, BOUND_S)
            assert entered, "工作线程没有进入写盘（闸门没生效）"
            rows = [r for r in _records(app) if r.original_name == "cancel-inflight.txt"]
            assert rows, ("上传期间应当有一条 prepared 记录", [r.original_name for r in _records(app)])
            pending = rows[0]
            assert pending.state == "prepared", pending.state

            await client.delete("/api/attachments/%s" % pending.id)
            state["release"].set()  # 放行人工闸门，让工作线程能退出
            outcome = await asyncio.wait_for(task, timeout=BOUND_S + 5)

    assert outcome["status"] != "timeout", (
        "取消之后上传请求仍然不返回（接收端没有观察终态，一直在满队列上等空位）", outcome
    )
    after = [r for r in _records(app) if r.id == pending.id]
    assert not after or after[0].state in ("cancelled", "failed"), (
        "取消之后附件行还在（或被提交成 ready）", [(r.id, r.state) for r in after]
    )
    assert not _temp_files(root), ("取消后留下临时文件", _temp_files(root))


# ---- 5. 同期其它 API 仍能推进 -------------------------------------------------------


async def test_other_requests_progress_while_upload_is_gated(app):
    root = _root(app)
    async with _client(app) as client:
        with _controlled_fs(root, "gate") as state:
            task = asyncio.create_task(_upload(client, "gated-upload.txt"))
            entered = await asyncio.to_thread(state["inside"].wait, BOUND_S)
            assert entered, "工作线程没有进入写盘（闸门没生效）"
            started = time.monotonic()
            queue = await client.get("/api/turns/queue")
            elapsed = time.monotonic() - started
            state["release"].set()
            await asyncio.wait_for(task, timeout=BOUND_S + 5)

    assert queue.status_code == 200, queue.text[:200]
    assert elapsed < 2.0, ("上传进行中时其它 API 被卡住", round(elapsed, 3))
