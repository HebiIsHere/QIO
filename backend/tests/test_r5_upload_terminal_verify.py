r"""D 独立验证（R5 问题一）：上传**收尾**必须由接收端与工作线程共同收敛。

契约来源：docs/plans/2026-10-07-final-convergence.md §1.2（冻结）+ §3。
基线 84f79a2 缺陷：接收端 \`async for chunk in request.stream()\` **等待下一块网络数据时不观察作业终态**
（api/server.py 上传路由）→ 工作线程已退出、请求仍不返回、附件停在 prepared、活动作业仍有 1 个。

用户可见规则（本文件的目标）：
* 工作线程失败后，**不需要客户端再发任何字节**就能进入失败收尾；
* 用户删除上传中的附件时，即使客户端暂停发送，接收任务也能收尾；
* 收尾：不留临时文件、行不得停在 prepared（也不得提交 ready）、活动作业注销、不留等待中的任务；
* **关键断言必须在客户端闸门仍关闭时成立**（不得先释放客户端再声称已收尾）；
* 全部走**真实 ASGI 上传路由**（httpx.ASGITransport = 应用真实路由），至少一组是真实分块请求体；
* 同期其它 API 仍能推进。

时序一律用 \`asyncio.Event\` / \`threading.Event\` 闸门控制，超时只作失败判定。

运行：cd backend; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/test_r5_upload_terminal_verify.py -q
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

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.attachment_upload import active_jobs as upload_active_jobs
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

CHUNK = b"u" * 65536
GATE_TIMEOUT = 25.0  # 只作失败判定
NAME = "r5-upload-terminal.bin"


@pytest.fixture()
def app(tmp_path: Path):
    conn = connect(tmp_path / "r5_upload.db")
    apply_migrations(conn)
    settings = Settings(data_dir=tmp_path / "data")
    app = create_app(settings, conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
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
    return sorted(p.name for p in root.rglob("*.part")) if root.exists() else []


def _rows(app, name: str = NAME) -> list:
    return [r for r in app.state.ctx.attachments.list(check=False) if r.original_name == name]


# ---- 受控错误注入（mkdir / 打开临时文件 / 写入途中） ---------------------------------


class _FailHandle:
    def __init__(self, handle, *, fail_at: int = 1) -> None:
        self._handle = handle
        self._fail_at = fail_at
        self._writes = 0

    def write(self, data):  # noqa: ANN001
        self._writes += 1
        if self._writes == self._fail_at:
            raise OSError(28, "受控错误：写入途中失败（磁盘空间不足）")
        return self._handle.write(data)

    def __getattr__(self, name):  # noqa: ANN001
        return getattr(self._handle, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):  # noqa: ANN002
        return self._handle.__exit__(*exc)


@contextlib.contextmanager
def _injected_failure(root: Path, kind: str):
    """在附件根目录内注入受控失败：kind ∈ {mkdir, open, write}。"""
    real_mkdir = Path.mkdir
    real_open, real_io_open = builtins.open, io.open
    needle = str(root).replace("\\", "/").lower()
    state = {"fired": False}

    def _patched_mkdir(self, *args, **kwargs):  # noqa: ANN001
        path = str(self).replace("\\", "/").lower()
        if kind == "mkdir" and needle in path and not state["fired"]:
            state["fired"] = True
            raise OSError(13, "受控错误：创建目录失败（权限不足）")
        return real_mkdir(self, *args, **kwargs)

    def _wrap(real):  # noqa: ANN001, ANN202
        def _patched_open(file, mode="r", *args, **kwargs):  # noqa: ANN001
            path = str(file)
            normalized = path.replace("\\", "/").lower()
            writing = any(flag in mode for flag in ("w", "a", "x"))
            if writing and needle in normalized:
                if kind == "open" and path.endswith(".part") and not state["fired"]:
                    state["fired"] = True
                    raise PermissionError(13, "受控错误：打开临时文件失败")
                handle = real(file, mode, *args, **kwargs)
                if kind == "write":
                    state["fired"] = True
                    return _FailHandle(handle)
                return handle
            return real(file, mode, *args, **kwargs)

        return _patched_open

    Path.mkdir = _patched_mkdir
    builtins.open, io.open = _wrap(real_open), _wrap(real_io_open)
    try:
        yield state
    finally:
        Path.mkdir = real_mkdir
        builtins.open, io.open = real_open, real_io_open


# ---- 请求体：第一块之后暂停（闸门），由测试决定何时继续 -------------------------------


async def _gated_body(gate: asyncio.Event, started: asyncio.Event, *, chunks: int = 4):
    for index in range(chunks):
        yield CHUNK
        if index == 0:
            started.set()
            await gate.wait()  # 客户端闸门：请求体暂停在这里
        await asyncio.sleep(0)


def _converged(app, *, name: str = NAME) -> dict:
    """收尾检查：状态 / 临时文件 / 活动作业 / 记录。"""
    rows = _rows(app, name)
    root = _root(app)
    return {
        "states": [r.state for r in rows],
        "stuck_prepared": [r.id for r in rows if r.state == "prepared"],
        "ready": [r.id for r in rows if r.state == "ready"],
        "temp_files": _temp_files(root),
        "active_jobs": list(upload_active_jobs()),
    }


def _assert_converged(app, *, name: str = NAME, expect_ready: bool = False) -> dict:
    state = _converged(app, name=name)
    assert not state["temp_files"], ("收尾后留下临时文件", state)
    assert not state["stuck_prepared"], ("附件停在 prepared（用户会一直以为在准备中）", state)
    assert not state["active_jobs"], ("收尾后仍有活动上传作业", state)
    if not expect_ready:
        assert not state["ready"], ("失败的上传不得提交 ready", state)
    return state


# ---- 1. 工作线程失败（三类注入）时：客户端闸门仍关闭，请求就必须收尾 -----------------


@pytest.mark.parametrize("kind", ["mkdir", "open", "write"])
async def test_worker_failure_unblocks_receiver_while_gate_closed(app, kind: str):
    gate = asyncio.Event()
    started = asyncio.Event()
    async with _client(app) as client:
        with _injected_failure(_root(app), kind) as injected:
            post = asyncio.create_task(
                client.post(
                    "/api/attachments/upload",
                    content=_gated_body(gate, started),
                    headers={"content-type": "application/octet-stream", "x-qio-name": NAME},
                )
            )
            await asyncio.wait_for(started.wait(), timeout=GATE_TIMEOUT)
            assert not gate.is_set(), "闸门应当仍然关闭（客户端还在暂停）"
            finished = True
            try:
                response = await asyncio.wait_for(asyncio.shield(post), timeout=GATE_TIMEOUT)
                detail = {"status": response.status_code, "text": response.text[:200]}
            except asyncio.TimeoutError:
                finished = False
                detail = {"status": "timeout", "waited_s": GATE_TIMEOUT}
            except Exception as exc:  # noqa: BLE001 - 服务端异常也算「没有干净收尾」
                detail = {"status": "exception", "text": "%s: %s" % (type(exc).__name__, exc)}

            # 关键断言：闸门**仍然关闭**时就要成立
            still_closed = not gate.is_set()
            state = _converged(app)
            gate.set()  # 收尾：放行客户端，避免留下悬挂任务
            with contextlib.suppress(Exception):
                await asyncio.wait_for(post, timeout=GATE_TIMEOUT)

    assert injected["fired"], ("受控错误没有触发（装置失效）", kind)
    assert still_closed, "断言时闸门已被打开 —— 证据不成立（必须「客户端闸门仍关闭时」）"
    assert finished, (
        "工作线程已经失败，客户端仍在暂停发送（闸门关闭），上传请求却不返回 —— 接收端没有观察作业终态",
        {"injection": kind, **detail, "state": state},
    )
    _assert_converged(app)


# ---- 2. 用户删除（取消）上传中的附件：闸门仍关闭时收尾 -------------------------------


async def test_delete_unblocks_receiver_while_gate_closed(app):
    gate = asyncio.Event()
    started = asyncio.Event()
    async with _client(app) as client:
        post = asyncio.create_task(
            client.post(
                "/api/attachments/upload",
                content=_gated_body(gate, started, chunks=8),
                headers={"content-type": "application/octet-stream", "x-qio-name": NAME},
            )
        )
        await asyncio.wait_for(started.wait(), timeout=GATE_TIMEOUT)
        rows = _rows(app)
        assert rows, ("上传期间应当有一条 prepared 记录", _converged(app))
        pending = rows[0]
        assert pending.state in ("prepared", "ready"), pending.state

        await client.delete("/api/attachments/%s" % pending.id)
        assert not gate.is_set(), "闸门应当仍然关闭（客户端还在暂停）"
        finished = True
        try:
            await asyncio.wait_for(asyncio.shield(post), timeout=GATE_TIMEOUT)
        except asyncio.TimeoutError:
            finished = False
        except Exception:  # noqa: BLE001 - 取消后请求以错误收场是可以的
            finished = True
        still_closed = not gate.is_set()
        state = _converged(app)
        gate.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(post, timeout=GATE_TIMEOUT)

    assert still_closed, "断言时闸门已被打开 —— 证据不成立"
    assert finished, (
        "用户删除上传中的附件后，客户端仍在暂停发送（闸门关闭），请求却不返回",
        state,
    )
    _assert_converged(app)


# ---- 3. 客户端取消（请求中断）+ 读取待决 --------------------------------------------


async def test_client_cancel_while_read_pending_converges(app):
    gate = asyncio.Event()
    started = asyncio.Event()
    async with _client(app) as client:
        post = asyncio.create_task(
            client.post(
                "/api/attachments/upload",
                content=_gated_body(gate, started, chunks=8),
                headers={"content-type": "application/octet-stream", "x-qio-name": NAME},
            )
        )
        await asyncio.wait_for(started.wait(), timeout=GATE_TIMEOUT)
        assert _rows(app), "上传期间应当有一条 prepared 记录"
        post.cancel()  # 客户端取消：读取正待决
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await post
        gate.set()
        await asyncio.sleep(0.2)  # 让服务端收尾跑完（这里只用于等事件循环转一圈）

    state = _assert_converged(app)
    assert state, state


# ---- 4. 队列满 + 工作线程提前退出（客户端不停发） ------------------------------------


async def test_many_chunks_with_dead_worker_converges(app):
    async def _body(chunks: int):
        for _ in range(chunks):
            yield CHUNK
            await asyncio.sleep(0)

    async with _client(app) as client:
        with _injected_failure(_root(app), "write") as injected:
            finished = True
            try:
                response = await asyncio.wait_for(
                    client.post(
                        "/api/attachments/upload",
                        content=_body(24),
                        headers={"content-type": "application/octet-stream", "x-qio-name": NAME},
                    ),
                    timeout=GATE_TIMEOUT,
                )
                detail = {"status": response.status_code, "text": response.text[:160]}
            except asyncio.TimeoutError:
                finished = False
                detail = {"status": "timeout"}
            except Exception as exc:  # noqa: BLE001
                detail = {"status": "exception", "text": "%s: %s" % (type(exc).__name__, exc)}

    assert injected["fired"], "受控错误没有触发（装置失效）"
    assert finished, ("工作线程提前退出后，客户端继续发送也收不了尾", detail)
    _assert_converged(app)


# ---- 5. 同期其它 API 仍能推进（闸门关闭、工作线程活着） ------------------------------


async def test_other_api_progresses_while_upload_gated(app):
    gate = asyncio.Event()
    started = asyncio.Event()
    async with _client(app) as client:
        post = asyncio.create_task(
            client.post(
                "/api/attachments/upload",
                content=_gated_body(gate, started, chunks=6),
                headers={"content-type": "application/octet-stream", "x-qio-name": NAME},
            )
        )
        await asyncio.wait_for(started.wait(), timeout=GATE_TIMEOUT)
        assert not gate.is_set()
        queue = await asyncio.wait_for(client.get("/api/turns/queue"), timeout=5)
        assert queue.status_code == 200, queue.text[:200]
        gate.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(post, timeout=GATE_TIMEOUT)
