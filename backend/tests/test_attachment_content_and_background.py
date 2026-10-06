"""附件打开链路（问题 5）与后台/有界 I/O（问题 6）。

契约（docs/plans/2026-10-06-audit-seven-fixes.md §1.6 / §1.7）：

* GET /api/attachments/{id}/content：**只读 QIO 管理的副本**（kind=copy / state=ready /
  路径落在 attachments 根目录之下），需认证，**绝不接受任意路径**；
* 上传：request.stream() **有界分块**接收（没有 Content-Length 也强制上限），
  写临时文件 + 算 sha256 都在**工作线程**；数据库动作全部回事件循环线程；
* 重定位：同一套线程纪律（工作线程只做文件 I/O）；
* 取消之后不得提交为 ready。

本文件先于实现落地（红）：修复前 upload 先 await request.body() 整包读内存、
在事件循环里同步写盘 + 算哈希；relocate 同步复制；没有 /content 路由。
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services import attachments as attachments_mod
from agent.services.attachments import AttachmentService
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

TERMINAL = ("ready", "failed", "changed", "missing", "cancelled")
TOKEN = "test-session-token-not-a-real-secret"


@pytest.fixture()
def client(tmp_path: Path):
    conn = connect(tmp_path / "content_api.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path / "data"), conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def async_app(tmp_path: Path):
    conn = connect(tmp_path / "content_async.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path / "data"), conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    return app


@pytest.fixture()
def svc(db_conn, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


@asynccontextmanager
async def _live(app):
    """真实 lifespan + httpx ASGITransport：同一个事件循环里并发发请求（能测出「卡住循环」）。"""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
            yield ac


class _ThreadRecordingConn:
    """连接代理：记录每次 execute 的线程 id（沿用并发用例的探针形状）。"""

    def __init__(self, inner: sqlite3.Connection, seen: list[int]) -> None:
        self._inner = inner
        self._seen = seen

    def execute(self, sql: str, *args, **kwargs):  # noqa: ANN002, ANN003 - 透明转发
        self._seen.append(threading.get_ident())
        return self._inner.execute(sql, *args, **kwargs)

    @property
    def in_transaction(self) -> bool:
        return self._inner.in_transaction


def _wait_terminal(client: TestClient, attachment_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    last: dict | None = None
    while time.time() < deadline:
        last = client.get(f"/api/attachments/{attachment_id}").json()["attachment"]
        if last["state"] in TERMINAL:
            return last
        time.sleep(0.02)
    raise AssertionError(f"附件没有在 {timeout}s 内进入终态：{last}")


async def _async_wait_terminal(ac: httpx.AsyncClient, attachment_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    last: dict | None = None
    while time.time() < deadline:
        last = (await ac.get(f"/api/attachments/{attachment_id}")).json()["attachment"]
        if last["state"] in TERMINAL:
            return last
        await asyncio.sleep(0.02)
    raise AssertionError(f"附件没有在 {timeout}s 内进入终态：{last}")


def _create_copy(client: TestClient, tmp_path: Path, name: str, data: bytes = b"open me\n") -> dict:
    source = tmp_path / name
    source.write_bytes(data)
    created = client.post("/api/attachments", json={"source_path": str(source)}).json()["attachment"]
    return _wait_terminal(client, created["id"])


# -- 问题 5：GET /content（只读 QIO 管理的副本） ------------------------------


def test_content_serves_the_managed_copy(client: TestClient, tmp_path: Path):
    data = "第一行\n第二行\n".encode("utf-8")
    source = tmp_path / "打开我.txt"
    source.write_bytes(data)
    ready = _create_copy(client, tmp_path, "打开我.txt", data)

    resp = client.get(f"/api/attachments/{ready['id']}/content")

    assert resp.status_code == 200, resp.text
    assert resp.content == data
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "attachment" in resp.headers.get("content-disposition", "")
    # 只读：原文件与副本都还在，内容不变
    assert source.read_bytes() == data
    assert Path(ready["stored_path"]).read_bytes() == data


def test_content_refuses_reference_unready_and_unknown(client: TestClient, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(attachments_mod, "COPY_MAX_BYTES", 16)
    big = tmp_path / "大文件.bin"
    big.write_bytes(b"v" * 32)
    reference = client.post("/api/attachments", json={"source_path": str(big)}).json()["attachment"]
    reference = _wait_terminal(client, reference["id"])

    # 引用型没有副本：明确拒绝（不是 200 空文件，也不是 500）
    refused = client.get(f"/api/attachments/{reference['id']}/content")
    assert refused.status_code == 409
    assert "引用本地文件" in refused.json()["detail"]

    # 副本丢了（missing）：如实拒绝
    lost = _create_copy(client, tmp_path, "副本丢了.txt", b"gone")
    Path(lost["stored_path"]).unlink()
    missing = client.get(f"/api/attachments/{lost['id']}/content")
    assert missing.status_code == 409
    assert "副本" in missing.json()["detail"]

    # 不存在的 id：404
    assert client.get("/api/attachments/att_nope/content").status_code == 404


def test_content_never_accepts_a_path(client: TestClient, tmp_path: Path):
    """路由只认 id：任何像路径的东西都只会被当成「没有这个附件」。"""
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"top secret")
    for evil in [
        "..%2F..%2Fsecret.txt",
        "%2E%2E%5Csecret.txt",
        "C%3A%5CWindows%5Cwin.ini",
        "..%2Fatt_x",
    ]:
        resp = client.get(f"/api/attachments/{evil}/content")
        assert resp.status_code == 404, f"{evil} -> {resp.status_code}"


def test_content_requires_session_token(tmp_path: Path):
    conn = connect(tmp_path / "auth.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path / "data", session_token=TOKEN), conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    auth = {"Authorization": f"Bearer {TOKEN}"}
    source = tmp_path / "受保护.txt"
    source.write_bytes(b"guard me")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        created = c.post(
            "/api/attachments", json={"source_path": str(source)}, headers=auth
        ).json()["attachment"]
        deadline = time.time() + 20
        while time.time() < deadline:
            att = c.get(f"/api/attachments/{created['id']}", headers=auth).json()["attachment"]
            if att["state"] in TERMINAL:
                break
            time.sleep(0.02)
        assert att["state"] == "ready", att

        assert c.get(f"/api/attachments/{created['id']}/content").status_code == 401
        assert c.get(
            f"/api/attachments/{created['id']}/content", headers={"X-QIO-Session": TOKEN}
        ).status_code == 200
        ok = c.get(f"/api/attachments/{created['id']}/content", headers=auth)
        assert ok.status_code == 200
        assert ok.content == b"guard me"


# -- 问题 6：有界上传 + 工作线程 I/O ------------------------------------------


def test_upload_without_content_length_is_bounded(client: TestClient, tmp_path: Path):
    """没有 Content-Length（分块传输）也必须强制上限：超限 413，不留行、不留文件。"""
    ctx = client.app.state.ctx
    svc = ctx.attachments
    svc.max_upload_bytes = 64

    resp = client.post(
        "/api/attachments/upload",
        content=iter([b"x" * 200]),
        headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote("超标.bin")},
    )

    assert "content-length" not in resp.request.headers  # 真的没有 Content-Length
    assert resp.status_code == 413, resp.text
    assert svc.list(limit=50) == []  # 被拒绝的上传不留行
    leftovers = [p for p in svc.root.rglob("*") if p.is_file()] if svc.root.is_dir() else []
    assert leftovers == [], f"被拒绝的上传留下了文件：{leftovers}"


def test_upload_chunked_within_cap_succeeds_with_hash(client: TestClient, tmp_path: Path):
    svc = client.app.state.ctx.attachments
    svc.max_upload_bytes = 1024
    chunks = [b"hello ", b"world\n"]

    resp = client.post(
        "/api/attachments/upload",
        content=iter(chunks),
        headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote("分块.txt")},
    )

    assert "content-length" not in resp.request.headers
    assert resp.status_code == 200, resp.text
    att = resp.json()["attachment"]
    assert att["state"] == "ready"
    assert att["size_bytes"] == len(b"hello world\n")
    assert Path(att["stored_path"]).read_bytes() == b"hello world\n"
    assert att["sha256"] == hashlib.sha256(b"hello world\n").hexdigest()


def test_upload_writes_and_hashes_on_a_worker_thread(client: TestClient, tmp_path: Path, monkeypatch):
    """写临时文件 + 算哈希必须发生在工作线程（事件循环只负责收块与落库）。"""
    svc = client.app.state.ctx.attachments
    loop_threads: list[int] = []
    worker_threads: list[int] = []

    real_begin = svc.begin_upload

    def spy_begin(*args, **kwargs):
        loop_threads.append(threading.get_ident())
        return real_begin(*args, **kwargs)

    real_write = svc.write_upload_stream

    def spy_write(*args, **kwargs):
        worker_threads.append(threading.get_ident())
        return real_write(*args, **kwargs)

    monkeypatch.setattr(svc, "begin_upload", spy_begin)
    monkeypatch.setattr(svc, "write_upload_stream", spy_write)

    resp = client.post(
        "/api/attachments/upload",
        content=iter([b"abc"]),
        headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote("线程.txt")},
    )

    assert resp.status_code == 200, resp.text
    assert loop_threads, "登记没有发生（探针没被调用）"
    assert worker_threads, "文件写入没有发生（探针没被调用）"
    assert set(loop_threads).isdisjoint(worker_threads), (
        f"文件 I/O 与事件循环在同一个线程：loop={loop_threads} worker={worker_threads}"
    )


def test_upload_never_touches_the_shared_connection_off_the_loop(client: TestClient, monkeypatch):
    """上传期间那个共享 sqlite 连接只能被一个线程碰（CI 事故的防回归）。"""
    ctx = client.app.state.ctx
    svc = ctx.attachments
    seen: list[int] = []
    monkeypatch.setattr(svc, "conn", _ThreadRecordingConn(ctx.conn, seen))

    resp = client.post(
        "/api/attachments/upload",
        content=iter([b"z" * 1024]),
        headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote("并发.txt")},
    )

    assert resp.status_code == 200, resp.text
    victims = sorted(set(seen))
    assert len(victims) == 1, (
        f"同一个 sqlite 连接被 {len(victims)} 个线程碰过：{victims} —— "
        "工作线程只允许做纯文件 I/O"
    )


def test_relocate_file_io_runs_off_the_event_loop(client: TestClient, tmp_path: Path, monkeypatch):
    svc = client.app.state.ctx.attachments
    att = _create_copy(client, tmp_path, "重定位.txt", b"relocate me")
    moved = tmp_path / "新位置" / "改名后.txt"
    moved.parent.mkdir(parents=True, exist_ok=True)
    Path(att["stored_path"])  # 副本在位
    (tmp_path / "重定位.txt").rename(moved)

    handler_threads: list[int] = []
    copy_threads: list[int] = []

    real_plan = svc.plan_relocate

    def spy_plan(*args, **kwargs):
        handler_threads.append(threading.get_ident())
        return real_plan(*args, **kwargs)

    real_copy = svc._copy_once

    def spy_copy(*args, **kwargs):
        copy_threads.append(threading.get_ident())
        return real_copy(*args, **kwargs)

    monkeypatch.setattr(svc, "plan_relocate", spy_plan)
    monkeypatch.setattr(svc, "_copy_once", spy_copy)

    resp = client.post(
        f"/api/attachments/{att['id']}/relocate", json={"source_path": str(moved)}
    )
    assert resp.status_code == 200, resp.text
    ready = _wait_terminal(client, att["id"])
    assert ready["state"] == "ready"
    assert ready["source_path"] == str(moved)
    assert Path(ready["stored_path"]).read_bytes() == b"relocate me"
    assert handler_threads and copy_threads
    assert set(handler_threads).isdisjoint(copy_threads), (
        f"重定位的文件 I/O 与事件循环在同一个线程：{handler_threads} / {copy_threads}"
    )


async def test_relocate_keeps_the_event_loop_free(async_app, tmp_path: Path, monkeypatch):
    """受控慢 I/O：重定位复制被闸门卡住时，其它请求（GET）必须仍然能推进。"""
    svc = async_app.state.ctx.attachments
    async with _live(async_app) as ac:
        source = tmp_path / "慢复制.txt"
        source.write_bytes(b"m" * 32)
        created = (
            await ac.post("/api/attachments", json={"source_path": str(source), "topic_id": "t_slow"})
        ).json()["attachment"]
        assert (await _async_wait_terminal(ac, created["id"]))["state"] == "ready"

        moved = tmp_path / "慢复制-移动后.txt"
        source.rename(moved)

        started = threading.Event()
        release = threading.Event()
        real_copy = svc._copy_once

        def gated_copy(att, **kwargs):
            started.set()
            assert release.wait(8), "测试没有放行复制（闸门超时）"
            return real_copy(att, **kwargs)

        monkeypatch.setattr(svc, "_copy_once", gated_copy)

        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        hb = asyncio.create_task(heartbeat())
        post = asyncio.create_task(
            ac.post(f"/api/attachments/{created['id']}/relocate", json={"source_path": str(moved)})
        )
        try:
            assert await asyncio.to_thread(started.wait, 8), "重定位没有进入文件复制"
            before = ticks
            await asyncio.sleep(0.25)
            assert ticks > before, "重定位把事件循环卡住了（心跳停摆）"
            resp = await asyncio.wait_for(ac.get(f"/api/attachments/{created['id']}"), timeout=5)
            assert resp.status_code == 200, resp.text
        finally:
            release.set()
            hb.cancel()

        done = await asyncio.wait_for(post, timeout=20)
        assert done.status_code == 200, done.text
        assert (await _async_wait_terminal(ac, created["id"]))["state"] == "ready"


async def test_delete_during_upload_never_commits_ready(async_app, tmp_path: Path, monkeypatch):
    """上传进行中移除附件 = 取消：不得再提交成 ready，也不得留下副本文件。"""
    svc = async_app.state.ctx.attachments
    async with _live(async_app) as ac:
        gate_ready = threading.Event()
        release = threading.Event()
        real_write = svc.write_upload_stream

        def gated_write(att, chunks, **kwargs):
            gate_ready.set()
            assert release.wait(8), "测试没有放行上传（闸门超时）"
            return real_write(att, chunks, **kwargs)

        monkeypatch.setattr(svc, "write_upload_stream", gated_write)

        async def body():
            yield b"a" * 16
            yield b"b" * 16

        upload = asyncio.create_task(
            ac.post(
                "/api/attachments/upload",
                content=body(),
                headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote("取消我.bin")},
            )
        )
        try:
            assert await asyncio.to_thread(gate_ready.wait, 8), "上传没有进入写盘"
            rows = svc.list(limit=10, check=False)
            assert len(rows) == 1, f"上传应该只登记一行：{rows}"
            target = rows[0]
            removed = await ac.delete(f"/api/attachments/{target.id}")
            assert removed.status_code == 200, removed.text
        finally:
            release.set()

        resp = await asyncio.wait_for(upload, timeout=20)
        # 行已经没了：上传结果无处可落，如实报「已被移除」（不是 200 ready）
        assert resp.status_code in (404, 409), resp.text
        assert svc.get(target.id, check=False) is None
        assert svc.list(limit=50) == []
        leftovers = [p for p in svc.root.rglob("*") if p.is_file()] if svc.root.is_dir() else []
        assert leftovers == [], f"取消之后留下了副本文件：{leftovers}"




def test_copy_and_upload_yield_to_the_event_loop(svc: AttachmentService, tmp_path: Path, monkeypatch):
    """防回归（2026-10-07 CI 真缺陷）：写盘 / 哈希的分块循环必须定期主动让出。

    CI 共享 CPU 上，工作线程连续做 memcpy/哈希会反复抢到 GIL，把事件循环饿住
    228-459ms（停止 / SSE / 其它请求都卡住）。让出点用计数器锁住：
    以后谁把让出删掉，这条用例就会红。
    """
    yields: list[int] = []
    monkeypatch.setattr(attachments_mod, "_yield_to_event_loop", lambda: yields.append(1))
    per_yield = attachments_mod.YIELD_EVERY_BYTES
    size = per_yield * 3 + per_yield // 2  # 3.5 个让出周期

    # 复制路径：3.5 个周期必须让出 >= 3 次
    source = tmp_path / "让出.txt"
    source.write_bytes(b"y" * size)
    att = svc.prepare(str(source))
    outcome = svc.copy_to_disk(att)
    assert outcome.state == "ready", outcome
    assert len(yields) >= 3, f"复制 {size} 字节只让出了 {len(yields)} 次"

    # 上传路径：调用方一次给一整包（ASGI 客户端可能就是这么大），也要切块并让出
    yields.clear()
    upload = svc.begin_upload(name="让出-上传.bin")
    uploaded = svc.write_upload_stream(upload, iter([b"z" * size]))
    assert uploaded.state == "ready", uploaded
    assert len(yields) >= 3, (
        f"一整包 {size} 字节的上传只让出了 {len(yields)} 次："
        "单块再切与让出都不能少"
    )
def test_cancel_between_copy_and_apply_is_never_ready(svc: AttachmentService, tmp_path: Path):
    """复制线程与落库线程是两段：「复制成功、取消随后到达」也不得落成 ready。"""
    source = tmp_path / "取消.txt"
    source.write_bytes(b"cancel me")
    att = svc.prepare(str(source))
    outcome = svc.copy_to_disk(att)
    assert outcome.state == "ready"  # 文件世界：复制成功、改名提交已经发生

    svc.cancel(att.id)  # 落库之前取消
    applied = svc.apply_outcome(att.id, outcome)

    assert applied is not None
    assert applied.state == "cancelled", f"取消之后仍被落成 {applied.state}"
    assert applied.stored_path is None
    assert not Path(outcome.stored_path).exists(), "取消之后刚提交的副本必须被清掉"
