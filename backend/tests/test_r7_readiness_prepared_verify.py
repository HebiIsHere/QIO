r"""D 独立验证（R7 问题二）：**唯一的执行就绪条件** —— prepared（首次准备没完成）不得当成就绪。

契约：docs/plans/2026-10-08-cancel-readiness-spill.md §1.3 / §3（冻结）。
基线 966e2fc 缺陷（services/attachments.py:1293）：\`_reject_reason\` 把 \`STATE_PREPARED\` 当成可就绪，
且 \`bind_for_turn\` 对普通未绑定附件只写归属、**不等首次后台复制**也不验证副本可读
→ 附件还没就绪就 200 accepted 并开始执行（模型被调用 + TURN_START）。

判定规则（用户可见结果）：准备**闸门仍关闭**时，模型调用数必须为 0、不得出现运行中的该轮；
就绪（副本真实可读）之后才允许执行；显式空列表仍表示「不带附件」。

运行：cd backend; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/test_r7_readiness_prepared_verify.py -q
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import os
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
MARKER = "R7 就绪条件：只有这份副本里才有的标记 7a31"
TERMINAL = ("ready", "failed", "changed", "missing", "cancelled")


def _load_provider_module():
    path = REPO_ROOT / "scripts" / "verify_stream_provider.py"
    assert path.exists(), f"验证资产缺失：{path}"
    spec = importlib.util.spec_from_file_location("verify_stream_provider", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


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
def app(tmp_path: Path, provider):
    conn = connect(tmp_path / "r7_ready.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path / "data"), conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    app.state.ctx.attachments.data_dir = tmp_path / "data"
    return app


def _client(app):
    import httpx

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://qio.test", timeout=90.0)


async def _prepare_credential(client, provider) -> None:
    resp = await client.post(
        "/api/credentials",
        json={
            "provider": "custom",
            "endpoint": "http://127.0.0.1:%d/v1" % provider.server_port,
            "secret": "sk-r7-fake-0001",
            "default_model": "verify-model",
            "tags": ["main-loop"],
        },
    )
    assert resp.status_code == 200, (resp.status_code, resp.text[:200])
    assert (resp.json().get("verify") or {}).get("ok"), "假 provider 没通过验证"


async def _script(provider, steps: list[dict]) -> None:
    import httpx

    base = "http://127.0.0.1:%d" % provider.server_port
    await asyncio.to_thread(httpx.post, base + "/__reset")
    await asyncio.to_thread(httpx.post, base + "/__script", json={"steps": steps}, timeout=10)


async def _calls(provider) -> int:
    import httpx

    base = "http://127.0.0.1:%d" % provider.server_port
    body = await asyncio.to_thread(lambda: httpx.get(base + "/__log", timeout=10).json())
    return len(body.get("requests") or [])


@contextlib.contextmanager
def _gated_prepare(entered: threading.Event, gate: threading.Event):
    """把首次后台复制的**提交步骤**（os.replace）挂起：此时行仍是 prepared、stored_path=null。"""
    real_replace = attachments_mod.os.replace

    def _patched(src, dst):  # noqa: ANN001
        if str(dst).endswith("__" + Path(str(dst)).name.split("__", 1)[-1]) or True:
            entered.set()
            gate.wait(timeout=60)
        return real_replace(src, dst)

    attachments_mod.os.replace = _patched
    try:
        yield
    finally:
        attachments_mod.os.replace = real_replace
        gate.set()


async def _wait_gate(entered: threading.Event) -> None:
    got = await asyncio.to_thread(entered.wait, 45)
    assert got, "后台复制没有进入闸门（装置失效：没走到提交步骤）"


async def _row(client, attachment_id: str) -> dict:
    return (await client.get("/api/attachments/%s" % attachment_id)).json().get("attachment") or {}


async def _register(client, path: Path) -> str:
    resp = await client.post("/api/attachments", json={"source_path": str(path)})
    assert resp.status_code == 200, (resp.status_code, resp.text[:200])
    return str(resp.json()["attachment"]["id"])


def _write_source(tmp_path: Path, name: str) -> Path:
    src = tmp_path / name
    src.write_text(MARKER + "\n", encoding="utf-8")
    return src


# ---- 1. 核心：首次准备未完成（prepared）时，真实发送接口不得开始执行 -----------------


async def test_prepared_attachment_blocks_execution(app, provider, tmp_path: Path):
    entered, gate = threading.Event(), threading.Event()
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["我不该在这一轮被调用。"]}])
        calls_before = await _calls(provider)
        src = _write_source(tmp_path, "r7-prepared.txt")

        with _gated_prepare(entered, gate):
            attachment_id = await _register(client, src)
            await _wait_gate(entered)
            row = await _row(client, attachment_id)
            assert str(row.get("state")) == "prepared", ("装置前置：行必须仍在 prepared", row)
            assert not row.get("stored_path"), ("装置前置：prepared 行不该有 stored_path", row)

            response = await client.post(
                "/api/turns", json={"message": "附件还没就绪就发送", "attachment_ids": [attachment_id]}
            )
            # 闸门仍关闭：给「开始执行」一个有限的观察窗口（基线正是在这里被调用）
            calls_during = (await _calls(provider)) - calls_before
            deadline = time.monotonic() + 3.0
            queue = (await client.get("/api/turns/queue")).json()
            while calls_during == 0 and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
                calls_during = (await _calls(provider)) - calls_before
                queue = (await client.get("/api/turns/queue")).json() or queue
            still_closed = not gate.is_set()
            gate.set()
            ready = await _wait_ready(client, attachment_id)
            calls_after_release = (await _calls(provider)) - calls_before

    assert still_closed, "断言时闸门已被打开 —— 证据不成立"
    assert calls_during == 0, (
        "附件还停在 prepared（首次复制没完成）时**模型已经被调用**了 —— 违反契约 §1.3「prepared 一律不就绪」",
        {
            "calls_during": calls_during,
            "http_status": response.status_code,
            "body": response.text[:200],
            "running": (queue or {}).get("running"),
            "state": row.get("state"),
        },
    )
    assert response.status_code >= 400, (
        "未就绪附件必须结构化拒绝（attachment_not_ready），不能 200 accepted",
        response.status_code, response.text[:200],
    )
    assert "attachment_not_ready" in response.text or "没有附上" in response.text, response.text[:200]
    assert str(ready.get("state")) == "ready", ("释放闸门后副本必须真的就绪", ready)
    content = await _read_content(app, attachment_id)
    assert MARKER in content, ("就绪后副本内容必须正确", content[:60])
    assert calls_after_release == 0, ("被拒绝的那一轮不得在闸门释放后再开始执行", calls_after_release)
    print("[诊断] prepared 未就绪：HTTP=%d；闸门关闭期间调用=%d；释放后累计调用=%d"
          % (response.status_code, calls_during, calls_after_release))


async def _wait_ready(client, attachment_id: str, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = await _row(client, attachment_id)
        if str(last.get("state")) in TERMINAL:
            return last
        await asyncio.sleep(0.05)
    return last


async def _read_content(app, attachment_id: str) -> str:
    async with _client(app) as client:
        resp = await client.get("/api/attachments/%s/content" % attachment_id)
        return resp.text if resp.status_code == 200 else ""


# ---- 2. 旧客户端缺字段（兜底绑定）同样不得提前执行 -----------------------------------


async def test_old_client_missing_field_also_waits(app, provider, tmp_path: Path):
    entered, gate = threading.Event(), threading.Event()
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["我不该在这一轮被调用。"]}])
        calls_before = await _calls(provider)
        src = _write_source(tmp_path, "r7-legacy.txt")

        with _gated_prepare(entered, gate):
            attachment_id = await _register(client, src)
            await _wait_gate(entered)
            # 旧客户端：body 里**没有** attachment_ids 字段 → 走「把未绑定附件绑上来」的兜底
            response = await client.post("/api/turns", json={"message": "旧客户端发送（缺字段）"})
            calls_during = (await _calls(provider)) - calls_before
            still_closed = not gate.is_set()
            gate.set()
            await _wait_ready(client, attachment_id)

    assert still_closed, "断言时闸门已被打开 —— 证据不成立"
    assert calls_during == 0, (
        "旧客户端兜底绑定也必须等附件就绪：闸门关闭时模型已被调用",
        {"calls_during": calls_during, "http_status": response.status_code, "body": response.text[:200]},
    )
    assert response.status_code >= 400, ("未就绪附件必须结构化拒绝", response.status_code, response.text[:200])
    print("[诊断] 旧客户端缺字段：HTTP=%d；闸门关闭期间调用=%d" % (response.status_code, calls_during))


# ---- 3. 多附件：最后一个未就绪 -------------------------------------------------------


async def test_multi_attachment_last_prepared_blocks(app, provider, tmp_path: Path):
    entered, gate = threading.Event(), threading.Event()
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["我不该在这一轮被调用。"]}])
        calls_before = await _calls(provider)
        first_src = _write_source(tmp_path, "r7-multi-1.txt")
        second_src = _write_source(tmp_path, "r7-multi-2.txt")
        first_id = await _register(client, first_src)
        await _wait_ready(client, first_id)  # 第一个先就绪

        with _gated_prepare(entered, gate):
            second_id = await _register(client, second_src)
            await _wait_gate(entered)
            row = await _row(client, second_id)
            assert str(row.get("state")) == "prepared", ("装置前置", row)
            response = await client.post(
                "/api/turns",
                json={"message": "多附件最后一个未就绪", "attachment_ids": [first_id, second_id]},
            )
            calls_during = (await _calls(provider)) - calls_before
            still_closed = not gate.is_set()
            gate.set()
            await _wait_ready(client, second_id)

    assert still_closed, "断言时闸门已被打开 —— 证据不成立"
    assert calls_during == 0, (
        "多附件里最后一个还没就绪时模型已被调用",
        {"calls_during": calls_during, "http_status": response.status_code, "body": response.text[:200]},
    )
    assert response.status_code >= 400, (response.status_code, response.text[:200])
    print("[诊断] 多附件最后一个未就绪：HTTP=%d；闸门关闭期间调用=%d" % (response.status_code, calls_during))


# ---- 4. 绿守卫：显式空列表 = 不带附件；不可读副本 = 结构化拒绝 ------------------------


async def test_explicit_empty_list_means_no_attachments(app, provider, tmp_path: Path):
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["这一轮没有附件，正常回答。"]}])
        calls_before = await _calls(provider)
        response = await client.post("/api/turns", json={"message": "纯文字这一轮", "attachment_ids": []})
        assert response.status_code == 200, (response.status_code, response.text[:200])
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if (await _calls(provider)) > calls_before:
                break
            await asyncio.sleep(0.1)
        assert (await _calls(provider)) > calls_before, "显式空列表必须照常执行"
    print("[诊断] 显式空列表：HTTP=200 且照常执行")


async def test_unreadable_copy_is_rejected(app, provider, tmp_path: Path):
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["我不该在这一轮被调用。"]}])
        calls_before = await _calls(provider)
        src = _write_source(tmp_path, "r7-gone.txt")
        attachment_id = await _register(client, src)
        ready = await _wait_ready(client, attachment_id)
        assert str(ready.get("state")) == "ready", ready
        stored = str(ready.get("stored_path") or "")
        assert stored and os.path.exists(stored), ("ready 必须指向真实副本", ready)
        os.unlink(stored)  # 副本不可读

        response = await client.post(
            "/api/turns", json={"message": "副本不可读", "attachment_ids": [attachment_id]}
        )
        await asyncio.sleep(0.5)
        calls_after = (await _calls(provider)) - calls_before

    assert response.status_code >= 400, (
        "副本实际不可读必须结构化拒绝，不得静默缺附件执行", response.status_code, response.text[:200]
    )
    assert calls_after == 0, ("不可读副本不得开始执行", calls_after)
    print("[诊断] 不可读副本：HTTP=%d；调用=%d" % (response.status_code, calls_after))
