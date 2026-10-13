r"""互动模式「添加图片 / 添加文件」的上传预检必须放行（2026-10-13 线上故障）。

**现象**：互动模式点「添加图片」→ 选一张图 → 界面报
「附件保存失败：…；卡片已保留，可在卡片编辑里重新关联」，而且**一直**失败；
库里连一条附件记录都没有（说明请求根本没发出去）。

**根因**：前端 `AddMenu` → `uploadAttachment` 走 `POST /api/attachments/upload`，带
`Content-Type: application/octet-stream` 与 `X-QIO-Name`（有 topic 时还有 `X-QIO-Topic-Id`）。
这两者都不在 CORS 安全名单里（octet-stream 不是安全 content-type，X-QIO-Name 不是安全头）
→ 浏览器**先发预检**；后端 `allow_headers` 白名单里没有它们 → 预检被
`400 Disallowed CORS headers` 拒掉 → POST 被浏览器拦下，前端 catch 到异常。

**为什么难查**：CORS 是浏览器侧强制的。curl / httpx / PowerShell 不做预检，同一发请求
永远成功 —— 所以「后端手工测是好的」不能证明前端能用。上次 `X-QIO-Prepare-Id` 就是同一个坑
（见 tests/test_r8_cors_preflight_verify.py 的文件头），这次是流量更小的上传路径。

**守两件事**：
1. 上传路径真正用到的那几个头必须放行；
2. 前端发出去的**每一个** `X-QIO-*` 头都在放行名单里 —— 枚举式守护：以后再加头却忘记改
   `allow_headers`，会在这里直接红，而不是等用户在界面上看到「保存失败」。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

TAURI_ORIGIN = "http://tauri.localhost"
#: 上传路径真实会带的自定义头（小写，CORS 头名大小写不敏感）
UPLOAD_HEADERS = ["content-type", "x-qio-name", "x-qio-topic-id"]
#: 既有安全头，必须继续放行（别为了修这个把白名单搬成 "*"）
EXISTING_HEADERS = ["authorization", "content-type", "x-qio-session", "x-qio-prepare-id"]

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"


def _build_app(tmp_path: Path):
    conn = connect(tmp_path / "upload_cors.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path / "data", dev_insecure=True), conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    app.state.ctx.attachments.data_dir = tmp_path / "data"
    return app


@pytest.fixture()
def app(tmp_path: Path):
    return _build_app(tmp_path)


def _client(app):
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://qio.test", timeout=30.0
    )


async def _preflight(client, path: str, requested: str, origin: str = TAURI_ORIGIN):
    return await client.options(
        path,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": requested,
        },
    )


def _allowed_headers(resp) -> set[str]:
    raw = resp.headers.get("access-control-allow-headers") or ""
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


# ---- 1. 上传路径的预检必须放行（这就是本次故障的直接复现）----------------------------


async def test_upload_preflight_allows_its_own_headers(app):
    async with _client(app) as client:
        resp = await _preflight(client, "/api/attachments/upload", ",".join(UPLOAD_HEADERS))

    assert resp.status_code == 200, (
        "上传附件的预检被拒 —— 浏览器会拦掉 POST，界面只会说「附件保存失败」，"
        "而后端一行日志都没有（请求根本没发出来）",
        {"status": resp.status_code, "body": resp.text[:200], "allow": sorted(_allowed_headers(resp))},
    )
    missing = [h for h in UPLOAD_HEADERS if h not in _allowed_headers(resp)]
    assert not missing, ("allow-headers 缺上传路径用的头", missing, sorted(_allowed_headers(resp)))
    print("[诊断] 上传预检 %d；allow-headers=%s" % (resp.status_code, sorted(_allowed_headers(resp))))


async def test_existing_custom_headers_still_allowed(app):
    async with _client(app) as client:
        resp = await _preflight(client, "/api/attachments/upload", ",".join(EXISTING_HEADERS))

    assert resp.status_code == 200, (resp.status_code, resp.text[:200])
    missing = [h for h in EXISTING_HEADERS if h not in _allowed_headers(resp)]
    assert not missing, ("既有头被弄丢了", missing, sorted(_allowed_headers(resp)))


# ---- 2. 枚举式守护：前端发出去的每个 X-QIO-* 头都必须在放行名单里 --------------------


def _frontend_custom_headers() -> dict[str, str]:
    """从真实前端源码里收集所有 `"X-QIO-…"` 请求头字面量（写成 for 循环而不是硬编码清单）。"""
    found: dict[str, str] = {}
    pattern = re.compile(r'"(X-QIO-[A-Za-z0-9-]+)"')
    for path in FRONTEND_SRC.rglob("*"):
        if path.suffix not in {".ts", ".vue"} or path.name.endswith(".test.ts"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in pattern.finditer(text):
            found.setdefault(match.group(1).lower(), str(path.relative_to(REPO_ROOT)))
    return found


async def test_every_frontend_custom_header_is_allowed(app):
    headers = _frontend_custom_headers()
    assert headers, "没扫到任何 X-QIO-* 头 —— 扫描逻辑或前端布局变了，别让这条守护悄悄失效"

    async with _client(app) as client:
        resp = await _preflight(client, "/api/attachments/upload", ",".join(sorted(headers)))

    allowed = _allowed_headers(resp)
    missing = {h: where for h, where in headers.items() if h not in allowed}
    assert resp.status_code == 200 and not missing, (
        "前端会发这些自定义头，但 CORS 白名单没放行 —— 浏览器会先发预检并被 400 拒掉，"
        "对应功能在界面上表现为「一直失败」，而后端看不到任何请求",
        {"status": resp.status_code, "missing": missing, "allow": sorted(allowed)},
    )
    print("[诊断] 前端自定义头 %s 全部放行" % sorted(headers))
