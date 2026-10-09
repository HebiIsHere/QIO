"""问题一（plan §1.1）：准备阶段的**可确认取消**（真 uvicorn + 真 TCP 客户端）。

真缺陷（plan §0 第 1 条）：

* 前端只对请求做 AbortController.abort()；
* 后端在附件准备完成后**直接 activate()** —— 真实 HTTP 断连没有被当成取消证据：
  用户已经中止/断开，模型仍然被调用（1 次调用 + TURN_START + 克隆保留）；
* 也没有可确认的取消端点（今天 POST /api/turns/prepare/{prepare_id}/cancel 是 404）。

装置按契约 §3：真 uvicorn + 真 TCP；复制卡在服务内部的线程事件闸门里（独立线程池，
不占共享执行器），闸门关闭期间观察，再放行。
"""

from __future__ import annotations

import json
import socket
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import uvicorn

from agent.adapters.base import ChatMessage, Completion
from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services import attachments as attachments_mod
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

MARKER = "R7 原轮副本内容：只有这份副本里才有的标记 7c31"
#: 断连用例的消息（台账握手按它定位这一轮）
MESSAGE_DISCONNECT = "重试（准备中断开）"
#: 等待「缺陷出现 / 取消收尾」的上界（失败判定，不是等待手段）
DEADLINE = 25.0
#: 装置前置条件（服务起来、附件就绪、复制进闸门）的上界
SETUP_DEADLINE = 60.0
#: 取消之后观察「不得又开始执行」的有界窗口
QUIET_WINDOW = 0.6


class _CountingAdapter:
    """假 provider：只数调用次数（不联网、不需要凭据）。"""

    mode = "text"
    model = "fake-r7-cancel"
    supports_stream = False

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001
        self.calls += 1
        return Completion(message=ChatMessage(role="assistant", content="收到"))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _LiveServer:
    """真 uvicorn（独立线程 + 独立事件循环）+ 真 TCP 端口。"""

    def __init__(self, app, *, port: int | None = None) -> None:
        self.port = port or _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error", loop="asyncio")
        )
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        import asyncio

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._server.serve())

    def start(self) -> None:
        self._thread.start()
        deadline = time.time() + SETUP_DEADLINE
        while time.time() < deadline and not self._server.started:
            time.sleep(0.02)
        assert self._server.started, "uvicorn 没起来"

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=DEADLINE)


@pytest.fixture()
def live(tmp_path: Path, monkeypatch):
    conn = connect(tmp_path / "r7_cancel.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path / "data"), conn)
    ctx = app.state.ctx
    ctx.credentials._kr = MemoryKeyring()
    # 测试污染防护：QIO_DATA_DIR 会覆盖 Settings(data_dir=...)，再钉一次附件落点
    ctx.attachments.data_dir = tmp_path / "data"
    adapter = _CountingAdapter()
    ctx.build_adapter = AsyncMock(return_value=adapter)
    topic = ctx.topics.nodes.create_topic("准备取消").id  # 起服务之前建好（单线程）
    server = _LiveServer(app)
    server.start()
    try:
        yield server, ctx, adapter, tmp_path, topic
    finally:
        server.stop()


# -- 装置：闸门 / 强制复制退路 ------------------------------------------------


def _break_link(monkeypatch) -> None:
    """强制走复制退路（os.link 失败 → copyfile）；只影响 attachments 服务看到的 os。"""
    import os as real_os

    class _OsShim:
        def __getattr__(self, name):
            return getattr(real_os, name)

        @staticmethod
        def link(*_args, **_kwargs):
            raise OSError("跨卷 / 不支持硬链接（测试强制复制退路）")

    monkeypatch.setattr(attachments_mod, "os", _OsShim())


def _gate_copy(monkeypatch) -> tuple[threading.Event, threading.Event]:
    """把复制卡在**服务内部**的线程事件闸门里（独立线程池，不占共享执行器）。"""
    import asyncio as real_asyncio
    import os as real_os
    import shutil as real_shutil
    from concurrent.futures import ThreadPoolExecutor
    from functools import partial

    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qio-r7-test-copy")
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


# -- 观察：只读、线程安全的事实 ----------------------------------------------


def _starts(ctx) -> int:
    return sum(1 for e in list(ctx.bus._history) if e.type.value == "TURN_START")


def _journal_row(ctx, message: str):
    """按消息文本读台账行（只读 sqlite；并发写入的短暂锁只当「还没写」）。

    用来做**显式握手**：abandon 会先写台账终态（cancelled + reason），
    所以「台账里出现 cancelled 行」= 服务端确实把这次断连当成取消处理过了。
    """
    try:
        return ctx.conn.execute(
            "SELECT status, reason FROM turn_journal WHERE message = ?"
            " ORDER BY created_at DESC LIMIT 1",
            (message,),
        ).fetchone()
    except sqlite3.Error:
        return None


def _attachments(client: httpx.Client) -> list[dict]:
    data = client.get("/api/attachments").json()
    rows = data.get("attachments") if isinstance(data, dict) else data
    return list(rows or [])


def _clones_of(client: httpx.Client, source_id: str) -> list[dict]:
    return [a for a in _attachments(client) if a.get("source_attachment_id") == source_id]


def _queue(client: httpx.Client) -> dict:
    return client.get("/api/turns/queue").json()


def _wait_for(predicate, *, timeout: float = SETUP_DEADLINE, what: str = "") -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"没有在 {timeout}s 内满足：{what}")


def _upload_ready(client: httpx.Client, tmp_path: Path, topic: str, name: str) -> tuple[Path, str]:
    source = tmp_path / name
    source.write_text(MARKER, encoding="utf-8")
    created = client.post(
        "/api/attachments", json={"source_path": str(source), "topic_id": topic}
    ).json()["attachment"]
    state = {}
    deadline = time.time() + SETUP_DEADLINE
    while time.time() < deadline:
        state = client.get(f"/api/attachments/{created['id']}").json()["attachment"]
        if state["state"] != "prepared":
            break
        time.sleep(0.02)
    assert state["state"] == "ready", state
    return source, created["id"]


def _run_binding_turn(client: httpx.Client, ctx, topic: str, attachment_id: str) -> str:
    """跑一轮真实的（假 provider）turn，把附件绑到它名下 —— 之后才能用 retry_of 克隆它。"""
    before = _starts(ctx)
    resp = client.post(
        "/api/turns", json={"message": "原轮", "topic_id": topic, "attachment_ids": [attachment_id]}
    )
    assert resp.status_code == 200, resp.text
    turn_id = resp.json()["turn_id"]
    _wait_for(lambda: _starts(ctx) > before, what="原轮没有开始执行")
    _wait_for(
        lambda: _queue(client)["running"] is None and not _queue(client)["queued"],
        what="原轮没有结束",
    )
    return turn_id


def _raw_post(server: _LiveServer, path: str, body: dict, headers: dict) -> socket.socket:
    """用裸 TCP 发一个真 HTTP/1.1 请求（不读响应：调用方决定何时断开）。"""
    payload = json.dumps(body).encode("utf-8")
    head = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{server.port}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        + "\r\n"
    ).encode("utf-8")
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=DEADLINE)
    sock.sendall(head + payload)
    return sock


# -- 用例 --------------------------------------------------------------------


def test_disconnect_during_prepare_never_starts_the_turn(live, monkeypatch):
    """契约 §1.1/§3：准备期间**真实 TCP 断连** → 不得放行、模型 0 次、无 TURN_START。"""
    server, ctx, adapter, tmp_path, topic = live
    with httpx.Client(base_url=server.base, timeout=30.0) as client:
        source, att = _upload_ready(client, tmp_path, topic, "断连副本.txt")
        original = _run_binding_turn(client, ctx, topic, att)
        source.unlink()  # 只能用 QIO 保存的那份副本

        _break_link(monkeypatch)
        entered, release = _gate_copy(monkeypatch)
        before = _starts(ctx)
        # 原轮自己也调过模型：判据必须看**增量**，不能用绝对次数
        calls_before = adapter.calls

        sock = None
        try:
            sock = _raw_post(
                server,
                "/api/turns",
                {
                    "message": MESSAGE_DISCONNECT,
                    "topic_id": topic,
                    "attachment_ids": [att],
                    "retry_of_turn_id": original,
                },
                {"X-QIO-Prepare-Id": uuid.uuid4().hex, "Connection": "keep-alive"},
            )
            assert entered.wait(SETUP_DEADLINE), "复制没有进入服务内部的闸门"
            assert adapter.calls == calls_before, "前置：准备期间不该已经开始执行"
            assert _starts(ctx) == before, "前置：准备期间不该已经发过 TURN_START"
            # 闸门还关着：先确认这一轮**确实已经登记为准备中**（reserve 就写了台账行）——
            # 装置前置条件（不是就绪判据），保证下面的握手等的是本次那一轮。
            prepared_row = _journal_row(ctx, MESSAGE_DISCONNECT)
            assert prepared_row is not None and prepared_row["status"] == "queued", (
                "这一刻台账里应当是准备中（queued）的预留行",
                dict(prepared_row) if prepared_row is not None else None,
            )

            sock.close()  # 真实 TCP 断连（等价于用户中止 / 网络断开）
            # CI Linux 教训（2026-10-09）：**断连到达**与**复制完成**是两个独立的异步事实，
            # 谁先到不该由机器调度决定 —— 先放行磁盘闸门时，Linux 上复制常常先完成，
            # 于是这一轮被正常放行（模型 1 次调用），用例读成「断连没有成为取消证据」。
            # 所以在放行之前，先等「服务端已经看见这次断连」这个**事实**：
            # 观测点 = 本次克隆行被丢弃（取消清理在磁盘闸门仍关闭时就完成）。
            # **显式握手**（CI Linux/py3.11 教训）：等「服务端确实把这次断连当成取消」这个
            # **持久事实**再放行磁盘闸门 —— 观测点 = 台账里这一轮被 abandon 成 cancelled
            # （reason=cancelled_during_prepare）。断连到达与复制完成是两个独立的异步事实，
            # 先放行时慢一点的机器上复制会先完成 → 这一轮被正常放行（模型 1 次调用），
            # 用例就被读成「断连没有成为取消证据」。这里等的是产品状态（轮询**事实**），
            # 不是固定 sleep 去放大竞速窗口。
            disconnect_seen = False
            deadline = time.time() + DEADLINE
            while time.time() < deadline:
                row = _journal_row(ctx, MESSAGE_DISCONNECT)
                if row is not None and row["status"] == "cancelled":
                    disconnect_seen = True
                    break
                time.sleep(0.02)
            # 看不到也不在这里判红：让后面的「模型 0 次」断言给结论（修复前就是它红）
            print(f"disconnect_seen={disconnect_seen}", flush=True)
        finally:
            release.set()  # 无论断言怎么走，都要放行磁盘闸门（否则复制线程会挂住）

        # 有界观察：要么缺陷行为出现（模型被调用 / TURN_START），要么取消已经收尾（克隆被清掉）
        observed_cancel_at = None
        deadline = time.time() + DEADLINE
        while time.time() < deadline:
            if adapter.calls > calls_before or _starts(ctx) > before:
                break
            if observed_cancel_at is None and not _clones_of(client, att):
                observed_cancel_at = time.time()
            if observed_cancel_at is not None and time.time() - observed_cancel_at >= QUIET_WINDOW:
                break
            time.sleep(0.02)

        assert adapter.calls == calls_before, (
            f"用户已经断开，模型仍被调用 {adapter.calls - calls_before} 次（断连没有成为取消证据）"
        )
        assert _starts(ctx) == before, "用户已经断开，仍然发了 TURN_START"
        snapshot = _queue(client)
        assert snapshot["running"] is None and not snapshot["queued"], (
            "取消之后不得留下可执行的队列项",
            snapshot,
        )
        assert not _clones_of(client, att), "取消之后本次克隆行没有清理"
        original_row = client.get(f"/api/attachments/{att}").json()["attachment"]
        assert original_row["state"] == "ready", ("原轮副本不得被删除", original_row)


def test_cancel_endpoint_stops_a_preparing_turn(live, monkeypatch):
    """显式取消端点：准备期间取消 → 已取消 + 不放行 + 重复取消幂等。"""
    server, ctx, adapter, tmp_path, topic = live
    with httpx.Client(base_url=server.base, timeout=30.0) as client:
        source, att = _upload_ready(client, tmp_path, topic, "取消副本.txt")
        original = _run_binding_turn(client, ctx, topic, att)
        source.unlink()

        _break_link(monkeypatch)
        entered, release = _gate_copy(monkeypatch)
        before = _starts(ctx)
        calls_before = adapter.calls  # 原轮自己也调过模型：判据只看增量
        prepare_id = uuid.uuid4().hex
        body = {
            "message": "重试（会被取消）",
            "topic_id": topic,
            "attachment_ids": [att],
            "retry_of_turn_id": original,
        }

        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(
                client.post,
                "/api/turns",
                json=body,
                headers={"X-QIO-Prepare-Id": prepare_id},
            )
            assert entered.wait(SETUP_DEADLINE), "复制没有进入服务内部的闸门"

            cancelled = client.post(f"/api/turns/prepare/{prepare_id}/cancel")
            assert cancelled.status_code == 200, cancelled.text
            payload = cancelled.json()
            assert payload.get("ok") is True, payload
            assert payload.get("cancelled") is True, (
                "准备期间取消没有被确认（今天直接 activate，用户中止后模型仍会被调用）",
                payload,
            )
            turn_id = payload.get("turn_id")
            assert turn_id, payload

            # 重复取消：幂等，仍然回「已取消」（不是未知、不是报错）
            again = client.post(f"/api/turns/prepare/{prepare_id}/cancel")
            assert again.status_code == 200, again.text
            assert again.json().get("cancelled") is True, again.json()
            assert again.json().get("turn_id") == turn_id, again.json()

            release.set()
            response = pending.result(timeout=DEADLINE)

        assert response.status_code == 200, response.text
        receipt = response.json()
        assert receipt["turn_id"] == turn_id, receipt
        assert receipt.get("accepted") is False, ("取消之后不得声称已经受理执行", receipt)
        assert receipt.get("cancelled") is True, receipt

        time.sleep(QUIET_WINDOW)  # 取消之后的有界观察窗：不得又开始执行
        assert adapter.calls == calls_before, (
            f"取消之后模型仍被调用 {adapter.calls - calls_before} 次（取消没有拦住放行）"
        )
        assert _starts(ctx) == before, "取消之后仍然发了 TURN_START"
        snapshot = _queue(client)
        assert snapshot["running"] is None and not snapshot["queued"], snapshot
        assert not _clones_of(client, att), "取消之后本次克隆行没有清理"
        assert client.get(f"/api/attachments/{att}").json()["attachment"]["state"] == "ready"


def test_cancel_endpoint_reports_already_started_and_unknown(live):
    """已放行 → already_started（前端走既有停止流程）；未知标识 → unknown（幂等不报错）。"""
    server, ctx, adapter, tmp_path, topic = live
    with httpx.Client(base_url=server.base, timeout=30.0) as client:
        prepare_id = uuid.uuid4().hex
        resp = client.post(
            "/api/turns",
            json={"message": "不带附件的普通一轮", "topic_id": topic},
            headers={"X-QIO-Prepare-Id": prepare_id},
        )
        assert resp.status_code == 200, resp.text
        turn_id = resp.json()["turn_id"]

        started = client.post(f"/api/turns/prepare/{prepare_id}/cancel")
        assert started.status_code == 200, started.text
        payload = started.json()
        assert payload.get("ok") is True, payload
        assert payload.get("already_started") is True, (
            "已经放行的轮次必须如实回 already_started（不得假装没发送）",
            payload,
        )
        assert payload.get("cancelled") is False, payload
        assert payload.get("turn_id") == turn_id, payload

        unknown = client.post("/api/turns/prepare/不存在的标识/cancel")
        assert unknown.status_code == 200, unknown.text
        assert unknown.json() == {"ok": True, "unknown": True}, unknown.json()
