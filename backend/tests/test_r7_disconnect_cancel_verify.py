r"""D 独立验证（R7 问题一）：准备期间的**真实 TCP 断连**必须落实为取消。

契约：docs/plans/2026-10-08-cancel-readiness-spill.md §1.1 / §3（冻结）。
基线 966e2fc 缺陷：turns/resend 在 \`await\` 附件绑定后**直接 activate()**，
没有把「请求断开」落实为取消 → 客户端断连 + 释放磁盘闸门之后，这一轮**照样开始执行**
（模型被调用 + TURN_START + 克隆留下）。

装置（契约禁止的替代品一律不用：不取消测试端 ASGI Task、不直接调 abandon、不只看 signal）：
* **真 uvicorn**（独立端口）+ **真 TCP 客户端**（asyncio.open_connection，不是 ASGITransport）；
* 请求**完整发完**（含 Content-Length 的 JSON body）之后，复制卡在**线程事件闸门**里；
* 复制开始后 \`writer.close()\` —— 真 TCP 断连（FIN）；
* 释放闸门后看结果：模型调用数（假 provider /__log）、台账里是否出现被放行的轮次、是否留下无人认领的克隆。

运行：cd backend; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/test_r7_disconnect_cancel_verify.py -q
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services import attachments as attachments_mod
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

REPO_ROOT = Path(__file__).resolve().parents[2]
MARKER = "R7 断连取消：只有这份副本里才有的标记 2b60"
TERMINAL = ("ready", "failed", "changed", "missing", "cancelled")


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

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
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
        server.should_exit = True
        thread.join(timeout=10)


def _calls(provider) -> int:
    import httpx

    base = "http://127.0.0.1:%d" % provider.server_port
    return len(httpx.get(base + "/__log", timeout=10).json().get("requests") or [])


def _script(provider, steps: list[dict]) -> None:
    import httpx

    base = "http://127.0.0.1:%d" % provider.server_port
    httpx.post(base + "/__reset", timeout=10)
    httpx.post(base + "/__script", json={"steps": steps}, timeout=10)


def _ready(client, base: str, attachment_id: str, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = (client.get(base + "/api/attachments/%s" % attachment_id, timeout=10).json() or {}).get("attachment") or {}
        if str(last.get("state")) in TERMINAL:
            return last
        time.sleep(0.05)
    return last


def _journal_rows(app, message: str) -> list[dict]:
    rows = app.state.ctx.conn.execute(
        "SELECT turn_id, status, message FROM turn_journal WHERE message = ? ORDER BY rowid", (message,)
    ).fetchall()
    return [{"turn_id": r[0], "status": r[1], "message": r[2]} for r in rows]


def _unbound_ready(app) -> list[tuple[str, str]]:
    return [
        (a.id, str(a.state))
        for a in app.state.ctx.attachments.list(check=False)
        if str(a.state) == "ready"
    ]


class _Gate:
    """磁盘闸门：只有显式 release() 才放行；wait() 不设超时（避免超时放行造成的假红）。

    Lead 2026-10-08 裁决：带 timeout 的 wait 超时放行**不会**置位 is_set()，
    会把「已被超时放行」误读成「仍关着」。这里用显式 released 标志区分两件事。
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.released = threading.Event()
        self._open = threading.Event()

    def hold(self) -> None:
        self.entered.set()
        self._open.wait()  # 不设超时：只有 release() 能放行

    def release(self) -> None:
        self.released.set()
        self._open.set()

    @property
    def still_closed(self) -> bool:
        return not self.released.is_set()

@contextlib.contextmanager
def _gated_clone(gate: _Gate):
    """复制卡在闸门内：打在**真实调用点** \`shutil.copyfile\` 上（跨平台确定）。"""
    real_link = attachments_mod.os.link
    real_copyfile = attachments_mod.shutil.copyfile

    def _no_link(*args, **kwargs):  # noqa: ANN002
        raise OSError(1, "受控错误：强制走复制路径（验证装置）")

    def _gated_copyfile(src, dst, *args, **kwargs):  # noqa: ANN001
        gate.hold()
        return real_copyfile(src, dst, *args, **kwargs)

    attachments_mod.os.link = _no_link
    attachments_mod.shutil.copyfile = _gated_copyfile
    try:
        yield
    finally:
        attachments_mod.os.link = real_link
        attachments_mod.shutil.copyfile = real_copyfile
        gate.release()


async def test_disconnect_does_not_start_the_turn_after_gate_release(server, provider, tmp_path: Path):
    import httpx

    base, port, app = server
    gate = _Gate()
    message = "断连那一轮（准备期间客户端关闭连接）"

    with httpx.Client(base_url=base, timeout=60) as client:
        # 凭据（假 provider）
        cred = client.post(
            "/api/credentials",
            json={
                "provider": "custom",
                "endpoint": "http://127.0.0.1:%d/v1" % provider.server_port,
                "secret": "sk-r7-fake-0001",
                "default_model": "verify-model",
                "tags": ["main-loop"],
            },
        )
        assert cred.status_code == 200 and (cred.json().get("verify") or {}).get("ok"), cred.text[:200]

        # 原轮：登记 ready 副本并绑定到第一轮
        src = tmp_path / "r7-disconnect.txt"
        src.write_text(MARKER + "\n", encoding="utf-8")
        registered = client.post("/api/attachments", json={"source_path": str(src)})
        assert registered.status_code == 200, registered.text[:200]
        attachment_id = str(registered.json()["attachment"]["id"])
        ready = _ready(client, base, attachment_id)
        assert str(ready.get("state")) == "ready", ready

        _script(provider, [{"chunks": ["第一轮回答。"]}])
        first = client.post("/api/turns", json={"message": "第一轮带附件", "attachment_ids": [attachment_id]})
        assert first.status_code == 200, first.text[:200]
        turn1 = str(first.json()["turn_id"])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            snap = client.get("/api/turns/queue", timeout=10).json()
            if not snap.get("running") and not (snap.get("queued") or []):
                break
            time.sleep(0.2)
        calls_before = _calls(provider)
        src.unlink()  # 用户原文件删除：重试必须靠已保存副本
        ready_before = {aid for aid, _ in _unbound_ready(app)}

    # ── 真 TCP 客户端：完整发完请求 → 复制进入闸门 → 关闭连接（真断连）→ 释放闸门 ──
    body = json.dumps(
        {"message": message, "attachment_ids": [attachment_id], "retry_of_turn_id": turn1}
    ).encode("utf-8")
    request = (
        "POST /api/turns HTTP/1.1\r\n"
        "Host: 127.0.0.1:%d\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: %d\r\n"
        "Connection: close\r\n\r\n" % (port, len(body))
    ).encode("ascii") + body

    reader = writer = None
    with _gated_clone(gate):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(request)
        await writer.drain()  # 请求**完整发完**
        got = await asyncio.to_thread(gate.entered.wait, 45)
        assert got, "复制没有进入闸门（装置失效：没走到克隆复制）"
        calls_while_gated = _calls(provider) - calls_before
        # 无歧义事实：闸门关闭期间服务端**还没有回应**（真 TCP 上短探测一次）
        responded_early = False
        try:
            responded_early = bool(await asyncio.wait_for(reader.read(1), timeout=0.3))
        except asyncio.TimeoutError:
            responded_early = False
        still_closed = gate.still_closed

        # 真断连：关闭这条 TCP 连接（FIN），不取消任何测试端任务
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        writer = None

        gate.release()  # 释放磁盘闸门
        # 给「迟到的复制成功 → 放行」一个有限的观察窗口
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        calls_after = _calls(provider) - calls_before

    with httpx.Client(base_url=base, timeout=30) as client:
        queue = client.get("/api/turns/queue").json()
        rows = _journal_rows(app, message)
        ready_after = {aid for aid, _ in _unbound_ready(app)}
        cancel_probe = client.post("/api/turns/prepare/not-a-real-prepare-id/cancel")

    orphan_clones = sorted(ready_after - ready_before)
    started_rows = [r for r in rows if str(r.get("status")) in ("queued", "running", "accepted", "done")]
    print(
        "[诊断] 真断连：闸门关闭期间调用=%d；释放后累计调用=%d；台账=%s；多余 ready 克隆=%s；取消端点={status:%d}"
        % (calls_while_gated, calls_after, rows, orphan_clones, cancel_probe.status_code)
    )

    assert not responded_early, (
        "闸门关闭期间服务端已经回了响应 —— 说明没有真的等复制（请求不该提前返回）", responded_early
    )
    assert still_closed, "断言时闸门已被放行 —— 证据不成立（必须在「复制仍在闸门内」时判定）"
    assert calls_while_gated == 0, ("断连前（闸门仍关闭）不该有模型调用", calls_while_gated)
    assert calls_after == 0, (
        "客户端**真 TCP 断连** + 释放磁盘闸门之后，这一轮仍然开始执行了（模型被调用）—— "
        "违反契约 §1.1「准备期间的断连也是取消」",
        {"calls_after": calls_after, "journal": rows, "orphan_clones": orphan_clones, "queue": queue},
    )
    assert not started_rows, (
        "断连之后仍有被放行/开始执行的轮次（TURN_START 不该发生）", started_rows
    )
    assert not orphan_clones, ("断连之后留下了无人认领的克隆副本", orphan_clones)
    assert cancel_probe.status_code in (200, 404), (
        "取消端点的现状记录（基线不存在时 404，属现状，不算断言失败）", cancel_probe.status_code
    )
