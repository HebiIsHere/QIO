r"""D 独立验证（R7 问题一，R9 适配）：准备期间的**真实 TCP 断连**必须落实为取消。

R9 适配说明（docs/r9-contracts.md §6）：
* R9 取消本地副本策略（附件 = 源文件引用），旧用例"复制卡在 os.link/copyfile 闸门"的装置
  已无对象；准备期仍真实存在：**浏览器上传的首次落盘**（write_material / begin_material →
  prepared → 由 bind_for_turn 有界等待）以及重试克隆的文件 I/O。
* 本用例用**真实在飞的首次落盘行**制造准备期（不注入已删除的 copy 缝）：真实 uvicorn +
  真 TCP 客户端，请求完整发完后关闭连接（FIN）；断言这一轮被取消（reason=
  cancelled_during_prepare）、模型 0 次、无 TURN_START、无任何绑定/队列残留。
  若断连没有被当成取消，等待会超时并以 attachment_not_ready 收尾 —— 本用例能区分这两者。

运行：cd backend; uv run --frozen pytest -q tests/test_r7_disconnect_cancel_verify.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import socket
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

REPO_ROOT = Path(__file__).resolve().parents[2]
MESSAGE = "断连那一轮（准备期间客户端关闭连接）"


def _load_provider_module():
    path = REPO_ROOT / "scripts" / "verify_stream_provider.py"
    assert path.exists(), f"验证资产缺失：{path}"
    spec = importlib.util.spec_from_file_location("verify_stream_provider", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def provider():
    module = _load_provider_module()
    server = module.StreamingProvider(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture()
def server(tmp_path: Path, provider):
    """**真 uvicorn**（独立端口）——不是 ASGITransport。"""
    import uvicorn

    conn = connect(tmp_path / "r7_disconnect.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path / "data"), conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    app.state.ctx.attachments.data_dir = tmp_path / "data"
    # 有界等待压短：断连正确时立即取消（毫秒级）；断连没被当成取消时 3s 后结构化拒绝，
    # 让用例在有限时间内拿到可区分的事实（reason 不同），而不是挂 60s。
    app.state.ctx.attachments.prepare_wait_seconds = 3.0

    port = _free_port()
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()

    import httpx

    base = "http://127.0.0.1:%d" % port
    ready = False
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        try:
            if httpx.get(base + "/api/turns/queue", timeout=2).status_code == 200:
                ready = True
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    assert ready, "uvicorn 没有在 25s 内就绪"
    try:
        yield base, port, app
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=10)


def _calls(provider) -> int:
    import httpx

    base = "http://127.0.0.1:%d" % provider.server_port
    return len(httpx.get(base + "/__log", timeout=10).json().get("requests") or [])


def _journal_row(app, message: str):
    try:
        return app.state.ctx.conn.execute(
            "SELECT turn_id, status, reason FROM turn_journal WHERE message = ? ORDER BY rowid DESC LIMIT 1",
            (message,),
        ).fetchone()
    except sqlite3.Error:
        return None


def _prepared_upload(app, *, name: str = "准备中的上传.txt") -> str:
    """真实在飞的首次落盘：浏览器上传第一步（prepared），后台尚未提交 —— 准备期存在。"""
    att = app.state.ctx.attachments.begin_material(name=name, source_kind="browser_upload")
    return str(att.id)


async def test_disconnect_during_prepare_never_starts_the_turn(server, provider):
    base, port, app = server
    import httpx

    with httpx.Client(base_url=base, timeout=60) as client:
        resp = client.post(
            "/api/credentials",
            json={
                "provider": "custom",
                "endpoint": "http://127.0.0.1:%d/v1" % provider.server_port,
                "secret": "sk-r7-fake-0001",
                "default_model": "verify-model",
                "tags": ["main-loop"],
            },
        )
        assert resp.status_code == 200 and (resp.json().get("verify") or {}).get("ok"), resp.text[:200]

    attachment_id = _prepared_upload(app)
    before_calls = _calls(provider)
    before_attachments = len(app.state.ctx.attachments.list(limit=50, check=False))

    body = json.dumps({"message": MESSAGE, "attachment_ids": [attachment_id]}).encode("utf-8")
    request = (
        "POST /api/turns HTTP/1.1\r\n"
        "Host: 127.0.0.1:%d\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: %d\r\n"
        "Connection: keep-alive\r\n"
        "X-QIO-Prepare-Id: r7-disconnect-prepare\r\n\r\n" % (port, len(body))
    ).encode("ascii") + body

    sock = socket.create_connection(("127.0.0.1", port), timeout=15)
    try:
        sock.sendall(request)
        # 装置前置：服务端已经把这一轮**预留**（台账 queued）且请求还没返回。
        deadline = time.monotonic() + 15
        row = None
        while time.monotonic() < deadline:
            row = _journal_row(app, MESSAGE)
            if row is not None and row[1] == "queued":
                break
            time.sleep(0.02)
        assert row is not None and row[1] == "queued", ("这一轮没有进入准备中（装置失效）", dict(row) if row else None)

        sock.settimeout(0.5)
        responded_early = False
        try:
            responded_early = bool(sock.recv(1))
        except socket.timeout:
            responded_early = False
        assert not responded_early, "准备期间服务端已经回了响应 —— 没有真的等首次落盘"

        calls_while_prepared = _calls(provider) - before_calls
        assert calls_while_prepared == 0, ("准备期间不该已经开始执行", calls_while_prepared)
    finally:
        sock.close()  # 真 TCP 断连（FIN），不取消任何测试端任务

    # 等待「服务端已经看见这次断连」的持久事实：台账终态 cancelled + 原因。
    deadline = time.monotonic() + 20
    row = None
    while time.monotonic() < deadline:
        row = _journal_row(app, MESSAGE)
        if row is not None and row[1] in ("cancelled", "completed", "failed", "interrupted", "running"):
            break
        time.sleep(0.02)
    assert row is not None, "断连之后台账没有这一轮的任何终态事实"
    reason = row[2]
    calls_after = _calls(provider) - before_calls
    with httpx.Client(base_url=base, timeout=30) as client:
        queue = client.get("/api/turns/queue").json()
        attachment = client.get("/api/attachments/%s" % attachment_id).json()["attachment"]
        attachments_now = len(app.state.ctx.attachments.list(limit=50, check=False))

    assert row[1] == "cancelled", ("准备期间的断连必须取消这一轮（而不是放行/失败放行）", dict(row))
    assert reason == "cancelled_during_prepare", (
        "断连必须被当成取消证据（reason 应为 cancelled_during_prepare，而不是等待超时/准备失败）",
        dict(row),
    )
    assert calls_after == 0, ("用户已经断开，模型仍被调用", calls_after)
    assert queue.get("running") is None and not queue.get("queued"), queue
    assert not attachment.get("turn_id"), ("断连的轮次不得留下绑定", attachment.get("turn_id"))
    assert str(attachment.get("state")) == "prepared", ("准备中的材料行不应被断连误删", attachment.get("state"))
    assert attachments_now == before_attachments, ("断连不得产生新的附件行/克隆", before_attachments, attachments_now)
    print("[诊断] 真断连：台账=%s；reason=%s；调用=%d；队列=%s"
          % (dict(row), reason, calls_after, queue))


async def test_explicit_empty_list_starts_normally(server, provider):
    """绿守卫：真 TCP 上显式 attachment_ids: [] 正常执行（断连用例不得连累正常路径）。"""
    base, _port, app = server
    import httpx

    with httpx.Client(base_url=base, timeout=60) as client:
        resp = client.post(
            "/api/credentials",
            json={
                "provider": "custom",
                "endpoint": "http://127.0.0.1:%d/v1" % provider.server_port,
                "secret": "sk-r7-fake-0001",
                "default_model": "verify-model",
                "tags": ["main-loop"],
            },
        )
        assert resp.status_code == 200 and (resp.json().get("verify") or {}).get("ok"), resp.text[:200]
        _reset = httpx.post("http://127.0.0.1:%d/__reset" % provider.server_port, timeout=10)
        httpx.post(
            "http://127.0.0.1:%d/__script" % provider.server_port,
            json={"steps": [{"chunks": ["正常回答。"]}]},
            timeout=10,
        )
        calls_before = _calls(provider)
        response = client.post("/api/turns", json={"message": "纯文字一轮", "attachment_ids": []})
        assert response.status_code == 200, (response.status_code, response.text[:200])
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _calls(provider) > calls_before:
                break
            time.sleep(0.1)
        assert _calls(provider) > calls_before, "显式空列表必须照常执行"
    print("[诊断] 显式空列表：HTTP=200 且照常执行")
