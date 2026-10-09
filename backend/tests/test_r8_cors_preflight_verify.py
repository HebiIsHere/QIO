r"""D 独立验证（R8 问题一）：带附件发送（X-QIO-Prepare-Id）的**预检必须放行**。

契约：docs/plans/2026-10-09-send-cancel-integrity.md §1.1（冻结）。
基线 9f5bc07 缺陷（api/server.py:315）：CORS allow_headers = ["Authorization", "Content-Type", "X-QIO-Session"]
**缺 X-QIO-Prepare-Id** → 带附件发送（前端会写这个头）的预检被拒：400 Disallowed CORS headers。

判定规则（用户可见结果）：
* 开发来源（http://localhost:1420）与三个既有 Tauri 来源，带新头的预检 → **200** 且 allow-headers 里含该头；
* **不可信来源仍拒**（不放开全部来源）；
* 含该头的**正式请求仍走原有认证**（未授权 → 401/403）。

运行：cd backend; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/test_r8_cors_preflight_verify.py -q
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

PREPARE_HEADER = "x-qio-prepare-id"
DEFAULT_HEADERS = "authorization,content-type,x-qio-session"
WITH_PREPARE = DEFAULT_HEADERS + "," + PREPARE_HEADER
#: 契约点名的来源：开发 dev server + 三个既有 Tauri 来源
TRUSTED_ORIGINS = [
    "http://localhost:1420",
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
]
UNTRUSTED_ORIGINS = ["http://evil.example", "https://tauri.localhost.evil.example"]


def _build_app(tmp_path: Path, *, dev_insecure: bool):
    # **不碰 os.environ**：conftest 用 os.environ.setdefault("QIO_DEV_INSECURE","1") 给整个测试会话兜底，
    # 这里若 pop 掉它，同一进程里**之后**跑的用例（settings/runtime_state/turn/verify…）会因为
    # Host: testserver 不是回环地址而全部 403 host_not_loopback（CI 实测事故）。
    # Settings 的 dev_insecure 本来就是显式字段，直接传即可。
    conn = connect(tmp_path / ("r8_cors_%s.db" % ("dev" if dev_insecure else "prod")))
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path / "data", dev_insecure=dev_insecure), conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    app.state.ctx.attachments.data_dir = tmp_path / "data"
    return app


@pytest.fixture()
def dev_app(tmp_path: Path):
    return _build_app(tmp_path, dev_insecure=True)


@pytest.fixture()
def prod_app(tmp_path: Path):
    return _build_app(tmp_path, dev_insecure=False)


def _client(app):
    import httpx

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://qio.test", timeout=30.0)


async def _preflight(client, origin: str, requested: str = WITH_PREPARE):
    return await client.options(
        "/api/turns",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": requested,
        },
    )


def _allowed_headers(resp) -> str:
    return (resp.headers.get("access-control-allow-headers") or "").lower()


# ---- 1. 核心：可信来源 + 新头 → 200 且放行该头 --------------------------------------


@pytest.mark.parametrize("origin", TRUSTED_ORIGINS, ids=["dev-1420", "tauri-scheme", "tauri-http", "tauri-https"])
async def test_preflight_with_prepare_id_is_allowed_for_trusted_origins(dev_app, origin):
    async with _client(dev_app) as client:
        resp = await _preflight(client, origin)
    assert resp.status_code == 200, (
        "带 X-QIO-Prepare-Id 的预检被拒（基线：allow_headers 缺该头）—— 带附件发送会被 CORS 挡住",
        {"origin": origin, "status": resp.status_code, "body": resp.text[:200]},
    )
    assert PREPARE_HEADER in _allowed_headers(resp), (
        "allow-headers 里没有 x-qio-prepare-id", _allowed_headers(resp)
    )
    print("[诊断] %s 预检：%d；allow-headers=%s" % (origin, resp.status_code, _allowed_headers(resp)))


async def test_existing_headers_still_allowed(dev_app):
    async with _client(dev_app) as client:
        resp = await _preflight(client, "http://localhost:1420", DEFAULT_HEADERS)
    assert resp.status_code == 200 and "x-qio-session" in _allowed_headers(resp), (
        resp.status_code, _allowed_headers(resp)
    )


# ---- 2. 不可信来源仍拒 ---------------------------------------------------------------


@pytest.mark.parametrize("origin", UNTRUSTED_ORIGINS, ids=["evil", "evil-subdomain"])
async def test_untrusted_origin_still_rejected(dev_app, origin):
    async with _client(dev_app) as client:
        resp = await _preflight(client, origin)
    assert resp.status_code != 200 or "access-control-allow-origin" not in {
        k.lower() for k in resp.headers
    }, ("不可信来源不得被放行", origin, resp.status_code, dict(resp.headers))
    assert resp.headers.get("access-control-allow-origin") is None, (
        "不可信来源拿到了 allow-origin", origin, resp.headers.get("access-control-allow-origin")
    )
    print("[诊断] 不可信来源 %s：%d（未放行）" % (origin, resp.status_code))


# ---- 3. 正式请求仍走原有认证 ---------------------------------------------------------


async def test_unauthorized_real_request_still_rejected(prod_app):
    async with _client(prod_app) as client:
        resp = await client.post(
            "/api/turns",
            json={"message": "未授权的正式请求", "attachment_ids": []},
            headers={"Origin": "http://localhost:1420", "X-QIO-Prepare-Id": "r8-unauthorized"},
        )
    assert resp.status_code in (401, 403), (
        "含新头的正式请求必须仍走原有认证（未授权不得放行）", resp.status_code, resp.text[:200]
    )
    print("[诊断] 未授权正式请求：%d" % resp.status_code)
