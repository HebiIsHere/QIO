"""问题一（plan §1.1，R9 适配）：准备阶段的**可确认取消**（真 uvicorn + 真 TCP 客户端）。

R9 适配说明（docs/r9-contracts.md §6）：
* 旧装置把"首次后台复制"卡在 os.link/copyfile 闸门里制造准备期；R9 取消本地副本策略
  （附件 = 源文件引用，register_source 不复制、不移动），该注入缝已随产品逻辑删除。
* 准备期仍真实存在：**浏览器上传的首次落盘**（begin_material → prepared → bind_for_turn
  有界等待）。用例用它让轮次真的停在准备中，不再注入任何已删除的 copy 缝。
* 覆盖不减少：准备期间①真实 TCP 断连、②显式取消端点都必须让这一轮**不启动**
  （模型 0 次、无 TURN_START、无队列/绑定残留）；重复取消幂等；已放行如实 already_started。

装置按契约 §3：真 uvicorn + 真 TCP；准备行由服务内部首次落盘在飞造成（不是测试端假等待）。
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
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

#: 断连用例的消息（台账握手按它定位这一轮）
MESSAGE_DISCONNECT = "重试（准备中断开）"
#: 取消用例的消息
MESSAGE_CANCEL = "重试（会被取消）"
#: 等待「缺陷出现 / 取消收尾」的上界（失败判定，不是等待手段）
DEADLINE = 25.0
#: 装置前置条件（服务起来、准备行建好）的上界
SETUP_DEADLINE = 60.0
#: 取消之后观察「不得又开始执行」的有界窗口
QUIET_WINDOW = 0.6


class _CountingAdapter:
    """假 provider：只数调用次数（不联网、不需要凭据）。"""

    mode = "text"
    model = "fake-r9-cancel"
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
def live(tmp_path: Path):
    conn = connect(tmp_path / "r7_cancel.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path / "data"), conn)
    ctx = app.state.ctx
    ctx.credentials._kr = MemoryKeyring()
    # 测试污染防护：QIO_DATA_DIR 会覆盖 Settings(data_dir=...)，再钉一次附件落点
    ctx.attachments.data_dir = tmp_path / "data"
    # 有界等待压短：取消/断连正确时立即取消；没被当成取消时 3s 后结构化拒绝（可区分）。
    ctx.attachments.prepare_wait_seconds = 3.0
    adapter = _CountingAdapter()
    ctx.build_adapter = AsyncMock(return_value=adapter)
    topic = ctx.topics.nodes.create_topic("准备取消").id  # 起服务之前建好（单线程）
    server = _LiveServer(app)
    server.start()
    try:
        yield server, ctx, adapter, tmp_path, topic
    finally:
        server.stop()


# -- 装置：真实在飞的首次落盘（准备期） ---------------------------------------


def _prepared_upload(ctx, topic: str, *, name: str) -> str:
    """浏览器上传第一步：行 prepared，字节尚未提交 —— 发送这一轮会真的等在这。"""
    att = ctx.attachments.begin_material(name=name, source_kind="browser_upload", topic_id=topic)
    return str(att.id)


# -- 观察：只读、线程安全的事实 ----------------------------------------------


def _starts(ctx) -> int:
    return sum(1 for e in list(ctx.bus._history) if e.type.value == "TURN_START")


def _journal_row(ctx, message: str):
    """按消息文本读台账行（只读 sqlite；并发写入的短暂锁只当「还没写」）。"""
    try:
        return ctx.conn.execute(
            "SELECT status, reason FROM turn_journal WHERE message = ? ORDER BY created_at DESC LIMIT 1",
            (message,),
        ).fetchone()
    except sqlite3.Error:
        return None


def _wait_for(predicate, *, timeout: float = SETUP_DEADLINE, what: str = "") -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"没有在 {timeout}s 内满足：{what}")


def _queue(client: httpx.Client) -> dict:
    return client.get("/api/turns/queue").json()


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


def _wait_journal(ctx, message: str, wanted: tuple[str, ...], timeout: float = 20.0):
    deadline = time.time() + timeout
    row = None
    while time.time() < deadline:
        row = _journal_row(ctx, message)
        if row is not None and row[0] in wanted:
            return row
        time.sleep(0.02)
    return row


# -- 用例 --------------------------------------------------------------------


def test_disconnect_during_prepare_never_starts_the_turn(live):
    """契约 §1.1/§3：准备期间**真实 TCP 断连** → 不得放行、模型 0 次、无 TURN_START。"""
    server, ctx, adapter, tmp_path, topic = live
    with httpx.Client(base_url=server.base, timeout=30.0) as client:
        att = _prepared_upload(ctx, topic, name="断连用例准备中的上传.txt")
        before = _starts(ctx)
        calls_before = adapter.calls
        attachments_before = len(client.get("/api/attachments").json().get("attachments") or [])

        sock = None
        try:
            sock = _raw_post(
                server,
                "/api/turns",
                {"message": MESSAGE_DISCONNECT, "topic_id": topic, "attachment_ids": [att]},
                {"X-QIO-Prepare-Id": uuid.uuid4().hex, "Connection": "keep-alive"},
            )
            # 装置前置：这一轮已经**预留**（台账 queued），且请求尚未返回。
            row = _wait_journal(ctx, MESSAGE_DISCONNECT, ("queued",))
            assert row is not None and row[0] == "queued", (
                "这一刻台账里应当是准备中（queued）的预留行", dict(row) if row else None
            )
            sock.settimeout(0.5)
            responded_early = False
            try:
                responded_early = bool(sock.recv(1))
            except socket.timeout:
                responded_early = False
            assert not responded_early, "准备期间服务端已经回了响应 —— 没有真的等首次落盘"
            assert adapter.calls == calls_before, "前置：准备期间不该已经开始执行"
            assert _starts(ctx) == before, "前置：准备期间不该已经发过 TURN_START"

            sock.close()  # 真实 TCP 断连（FIN）
        finally:
            if sock is not None:
                sock.close()

        row = _wait_journal(ctx, MESSAGE_DISCONNECT, ("cancelled", "completed", "running", "failed"))
        time.sleep(QUIET_WINDOW)

        assert row is not None, "断连之后台账没有这一轮的任何终态事实"
        assert row[0] == "cancelled", ("准备期间的断连必须取消这一轮", dict(row))
        assert row[1] == "cancelled_during_prepare", (
            "断连必须被当成取消证据（不是等待超时/准备失败）", dict(row)
        )
        assert adapter.calls == calls_before, (
            f"用户已经断开，模型仍被调用 {adapter.calls - calls_before} 次（断连没有成为取消证据）"
        )
        assert _starts(ctx) == before, "用户已经断开，仍然发了 TURN_START"
        snapshot = _queue(client)
        assert snapshot["running"] is None and not snapshot["queued"], (
            "取消之后不得留下可执行的队列项", snapshot
        )
        attachment = client.get(f"/api/attachments/{att}").json()["attachment"]
        attachments_now = len(client.get("/api/attachments").json().get("attachments") or [])
        assert not attachment.get("turn_id"), ("断连的轮次不得留下绑定", attachment.get("turn_id"))
        assert str(attachment.get("state")) == "prepared", ("准备中的材料行不应被断连误删", attachment.get("state"))
        assert attachments_now == attachments_before, ("断连不得产生新的附件行/克隆", attachments_before, attachments_now)


def test_cancel_endpoint_stops_a_preparing_turn(live):
    """显式取消端点：准备期间取消 → 已取消 + 不放行 + 重复取消幂等。"""
    server, ctx, adapter, tmp_path, topic = live
    with httpx.Client(base_url=server.base, timeout=30.0) as client:
        att = _prepared_upload(ctx, topic, name="取消用例准备中的上传.txt")
        before = _starts(ctx)
        calls_before = adapter.calls
        prepare_id = uuid.uuid4().hex
        body = {"message": MESSAGE_CANCEL, "topic_id": topic, "attachment_ids": [att]}

        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(
                client.post, "/api/turns", json=body, headers={"X-QIO-Prepare-Id": prepare_id}
            )
            row = _wait_journal(ctx, MESSAGE_CANCEL, ("queued",))
            assert row is not None and row[0] == "queued", ("准备行没有出现（装置失效）", row)
            assert not pending.done(), "B 不该在准备完成前返回（装置失效）"

            cancelled = client.post(f"/api/turns/prepare/{prepare_id}/cancel")
            assert cancelled.status_code == 200, cancelled.text
            payload = cancelled.json()
            assert payload.get("ok") is True, payload
            assert payload.get("cancelled") is True, (
                "准备期间取消没有被确认（用户中止后模型仍会被调用）", payload
            )
            turn_id = payload.get("turn_id")
            assert turn_id, payload

            # 重复取消：幂等，仍然回「已取消」（不是未知、不是报错）
            again = client.post(f"/api/turns/prepare/{prepare_id}/cancel")
            assert again.status_code == 200, again.text
            assert again.json().get("cancelled") is True, again.json()
            assert again.json().get("turn_id") == turn_id, again.json()

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
        attachment = client.get(f"/api/attachments/{att}").json()["attachment"]
        assert not attachment.get("turn_id"), ("取消的轮次不得留下绑定", attachment.get("turn_id"))


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
            "已经放行的轮次必须如实回 already_started（不得假装没发送）", payload
        )
        assert payload.get("cancelled") is False, payload
        assert payload.get("turn_id") == turn_id, payload

        unknown = client.post("/api/turns/prepare/不存在的标识/cancel")
        assert unknown.status_code == 200, unknown.text
        assert unknown.json() == {"ok": True, "unknown": True}, unknown.json()
