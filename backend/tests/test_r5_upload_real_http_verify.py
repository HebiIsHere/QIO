r"""D 独立验证（R5 问题一，阶段二）：**真实 uvicorn HTTP**（不是 ASGI 直连）的上传收尾。

与 test_r5_upload_terminal_verify.py 的区别：那里走 httpx.ASGITransport（应用内部直连），
这里起**真的 uvicorn**（独立端口、真 HTTP/1.1 分块请求体），再验证同一批收尾规则：
* 客户端暂停发送（线程闸门）时，失败的上传请求**自己返回**，不需要客户端再发任何字节；
* 闸门全程未打开、已发送块数 ≤ 1、无 prepared / 无 .part / 无活动作业；
* 正常上传在真实 HTTP 下也能成功且内容完整（守卫）。

运行：cd backend; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/test_r5_upload_real_http_verify.py -q
"""

from __future__ import annotations

import builtins
import contextlib
import io
import socket
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

NAME = "r5-http-upload.bin"
CHUNK = b"h" * 65536
BOUND_S = 30.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def http_server(tmp_path: Path):
    """真的起一个 uvicorn（独立线程 + 独立端口），走真实 HTTP。"""
    import uvicorn

    conn = connect(tmp_path / "r5_http.db")
    apply_migrations(conn)
    settings = Settings(data_dir=tmp_path / "data")
    app = create_app(settings, conn)
    ctx = app.state.ctx
    ctx.credentials._kr = MemoryKeyring()
    ctx.attachments.data_dir = tmp_path / "data"

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    import httpx

    base = "http://127.0.0.1:%d" % port
    ready = False
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if httpx.get(base + "/api/turns/queue", timeout=2).status_code == 200:
                ready = True
                break
        except Exception:  # noqa: BLE001 - 还没起来
            time.sleep(0.2)
    assert ready, "uvicorn 没有在 20s 内就绪"
    try:
        yield base, ctx
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@contextlib.contextmanager
def _mkdir_failure(root: Path):
    """让附件根目录下的 mkdir 失败（受控）。"""
    real_mkdir = Path.mkdir
    needle = str(root).replace("\\", "/").lower()
    state = {"fired": False}

    def _patched(self, *args, **kwargs):  # noqa: ANN001
        path = str(self).replace("\\", "/").lower()
        if needle in path and not state["fired"]:
            state["fired"] = True
            raise OSError(13, "受控错误：创建目录失败（真实 HTTP 变体）")
        return real_mkdir(self, *args, **kwargs)

    Path.mkdir = _patched
    try:
        yield state
    finally:
        Path.mkdir = real_mkdir


def _states_over_http(base: str, name: str = NAME) -> list[str]:
    import httpx

    body = httpx.get(base + "/api/attachments?unbound=true", timeout=10).json()
    return [a["state"] for a in body.get("attachments", []) if a.get("name") == name]


# ---- 1. 客户端暂停 + 失败：请求自己返回（真实 HTTP） ---------------------------------


def test_paused_upload_failure_converges_over_real_http(http_server):
    """真实 HTTP：客户端暂停时，**服务端必须已经收敛**；放行后请求必须结束、收尾干净。

    说明（如实记录）：HTTP/1.1 下客户端正在发送 chunked 请求体时，**库里读不到服务端的提前响应**
    （已实测：客户端一直等到自己的 timeout 才拿到 WinError 10053）；所以「请求在客户端暂停期间就返回」
    这条硬断言留在 ASGI 直连的用例里，这里断言的是**服务端侧**的收敛（用独立连接轮询）+
    放行后请求必须结束（不许挂住）。两者合起来才是完整形态。
    """
    import httpx

    base, ctx = http_server
    gate = threading.Event()
    sent = {"count": 0}
    outcome: dict = {}

    def _body():
        sent["count"] += 1
        yield CHUNK
        gate.wait(timeout=BOUND_S)  # 客户端闸门：不再发送
        sent["count"] += 1
        yield b"z" * 1024

    def _post() -> None:
        with httpx.Client(base_url=base, timeout=BOUND_S) as client:
            try:
                resp = client.post(
                    "/api/attachments/upload",
                    content=_body(),
                    headers={"content-type": "application/octet-stream", "x-qio-name": NAME},
                )
                outcome.update({"status": resp.status_code, "text": resp.text[:200]})
            except Exception as exc:  # noqa: BLE001 - 连接被中止也算「结束了」
                outcome.update({"status": "exception", "text": "%s: %s" % (type(exc).__name__, exc)})

    with _mkdir_failure(Path(ctx.attachments.root)) as injected:
        worker = threading.Thread(target=_post, daemon=True)
        worker.start()
        # 用**独立连接**轮询服务端收敛（这也同时证明：上传挂起时其它 API 仍能推进）
        root = Path(ctx.attachments.root)
        snapshot: dict = {}
        converged = False
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            snapshot = {
                "states": _states_over_http(base),
                "active": list(upload_active_jobs()),
                "parts": sorted(p.name for p in root.rglob("*.part")) if root.exists() else [],
            }
            if not snapshot["active"] and "prepared" not in snapshot["states"] and not snapshot["parts"]:
                converged = True
                break
            time.sleep(0.2)
        gate_never_opened = not gate.is_set()
        chunks_sent_while_paused = sent["count"]
        early_response = bool(outcome)  # 诊断：暂停期间客户端有没有拿到响应
        gate.set()  # 放行客户端
        worker.join(timeout=BOUND_S)

    assert injected["fired"], "受控错误没有触发（装置失效）"
    assert converged, (
        "客户端仍在暂停（闸门关闭）时，服务端没有收敛：活动作业/临时文件/prepared 残留",
        snapshot,
    )
    assert gate_never_opened, "断言时闸门已被打开 —— 证据不成立"
    assert chunks_sent_while_paused <= 1, (
        "客户端在闸门关闭期间又发了第二块", chunks_sent_while_paused
    )
    assert not worker.is_alive(), ("放行客户端后请求仍然挂住（没有结束）", outcome)
    assert outcome, "请求没有产生任何结果"
    assert outcome.get("status") == "exception" or int(outcome.get("status", 0)) >= 400, (
        "失败的上传既没有错误码、也不是连接中止", outcome
    )
    assert "prepared" not in snapshot["states"], snapshot
    assert not snapshot["parts"], snapshot
    print(
        "[诊断] 真实 HTTP：暂停期间已收敛=%s；已发送块数=%d；客户端暂停期间是否拿到响应=%s；"
        "最终 outcome=%s；states=%s"
        % (converged, chunks_sent_while_paused, early_response, outcome.get("status"), snapshot["states"])
    )


# ---- 2. 守卫：真实 HTTP 下的正常上传成功且内容完整 -----------------------------------


def test_normal_upload_over_real_http_succeeds(http_server):
    import httpx

    base, ctx = http_server
    payload = b"ok" * 400_000  # 800 KB
    with httpx.Client(base_url=base, timeout=BOUND_S) as client:
        resp = client.post(
            "/api/attachments/upload",
            content=payload,
            headers={"content-type": "application/octet-stream", "x-qio-name": "r5-http-ok.bin"},
        )
        assert resp.status_code == 200, (resp.status_code, resp.text[:200])
        attachment_id = resp.json()["attachment"]["id"]
        content = client.get("/api/attachments/%s/content" % attachment_id)
        assert content.status_code == 200 and content.content == payload, (
            "真实 HTTP 上传后的内容与发出字节不一致", content.status_code, len(content.content)
        )
    print("[诊断] 真实 HTTP 正常上传：%d 字节，内容一致" % len(payload))
