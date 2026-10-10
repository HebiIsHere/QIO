r"""D 独立验证（R7 问题二）：**唯一的执行就绪条件** —— prepared（首次准备没完成）不得当成就绪。

契约：docs/plans/2026-10-08-cancel-readiness-spill.md §1.3 / §3（冻结）。
基线 966e2fc 缺陷（services/attachments.py:1293）：_reject_reason 把 STATE_PREPARED 当成可就绪，
且 bind_for_turn 对普通未绑定附件只写归属、不等首次后台复制也不验证副本可读。

装置纪律（Lead 2026-10-08 裁决后修正）：
* 磁盘闸门 wait() **不设超时**；「我们放行」与「等待超时」用**显式 released 标志**区分，
  断言里**不得**再用 is_set() 代表「闸门仍关着」（带 timeout 的 wait 超时放行不会置位）；
* 闸门关闭期间的判据只用不可逆/无歧义事实：①模型调用 == 0 ②没有 TURN_START/运行中的该轮
  ③**请求尚未返回**（task + 短等待断言未完成）；
* 放行后按契约允许两条合法路径：等到就绪再执行（200 + ready + 恰好 1 次调用）或结构化拒绝
  （attachment_not_ready + 0 次调用）。

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
PENDING_WINDOW_S = 1.5


class _Gate:
    """磁盘闸门：只有显式 release() 才放行；wait() 不设超时（避免超时放行造成的假红）。"""

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

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://qio.test", timeout=180.0)


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
def _gated_prepare(gate: _Gate):
    """把首次后台复制的**提交步骤**（os.replace）挂起：此时行仍是 prepared、stored_path=null。"""
    real_replace = attachments_mod.os.replace

    def _patched(src, dst):  # noqa: ANN001
        gate.hold()
        return real_replace(src, dst)

    attachments_mod.os.replace = _patched
    try:
        yield
    finally:
        attachments_mod.os.replace = real_replace
        gate.release()


async def _row(client, attachment_id: str) -> dict:
    return (await client.get("/api/attachments/%s" % attachment_id)).json().get("attachment") or {}


async def _wait_ready(client, attachment_id: str, timeout: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = await _row(client, attachment_id)
        if str(last.get("state")) in TERMINAL:
            return last
        await asyncio.sleep(0.05)
    return last


async def _register(client, path: Path) -> str:
    resp = await client.post("/api/attachments", json={"source_path": str(path)})
    assert resp.status_code == 200, (resp.status_code, resp.text[:200])
    return str(resp.json()["attachment"]["id"])


def _write_source(tmp_path: Path, name: str) -> Path:
    src = tmp_path / name
    src.write_text(MARKER + "\n", encoding="utf-8")
    return src


async def _gated_send(client, gate: _Gate, provider, calls_before: int, payload: dict) -> dict:
    """闸门关闭期间发起真实发送，返回证据快照（断言交给各用例）。"""
    task = asyncio.create_task(client.post("/api/turns", json=payload))
    await asyncio.sleep(PENDING_WINDOW_S)  # 短等待：请求在这段时间内必须还没返回
    snapshot = {
        "gate_still_closed": gate.still_closed,
        "request_pending": not task.done(),
        "calls_during": (await _calls(provider)) - calls_before,
        "queue": (await client.get("/api/turns/queue")).json(),
    }
    gate.release()
    try:
        snapshot["response"] = await asyncio.wait_for(task, timeout=120)
    except asyncio.TimeoutError:
        snapshot["response"] = None
    return snapshot


def _turns_for(app, message: str) -> list[dict]:
    """台账里这一条消息对应的轮次（判断「恰好执行了一轮」用）。"""
    rows = app.state.ctx.conn.execute(
        "SELECT turn_id, status FROM turn_journal WHERE message = ? ORDER BY rowid", (message,)
    ).fetchall()
    return [{"turn_id": r[0], "status": r[1]} for r in rows]


def _started_evidence(snapshot: dict) -> list[str]:
    queue = snapshot.get("queue") or {}
    evidence = []
    if queue.get("running"):
        evidence.append("running:%s" % (queue["running"] or {}).get("turn_id"))
    for item in queue.get("queued") or []:
        evidence.append("queued:%s" % item.get("turn_id"))
    return evidence


# ---- 1. 核心：首次准备未完成（prepared）时，真实发送接口不得开始执行 -----------------


async def test_prepared_attachment_blocks_execution(app, provider, tmp_path: Path):
    gate = _Gate()
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["附件就绪之后才允许执行。"]}])
        calls_before = await _calls(provider)
        src = _write_source(tmp_path, "r7-prepared.txt")

        with _gated_prepare(gate):
            attachment_id = await _register(client, src)
            assert await asyncio.to_thread(gate.entered.wait, 45), "后台复制没有进入闸门（装置失效）"
            row = await _row(client, attachment_id)
            assert str(row.get("state")) == "prepared", ("装置前置：行必须仍在 prepared", row)
            assert not row.get("stored_path"), ("装置前置：prepared 行不该有 stored_path", row)

            snapshot = await _gated_send(
                client, gate, provider, calls_before,
                {"message": "附件还没就绪就发送", "attachment_ids": [attachment_id]},
            )
            response = snapshot["response"]
            ready = await _wait_ready(client, attachment_id)
            content = await client.get("/api/attachments/%s/content" % attachment_id)
            calls_after = (await _calls(provider)) - calls_before
            # 放行后 worker 取到这一轮可能略晚：给「启动」一个有限窗口（只用于等事实，不改变判据）
            deadline = time.monotonic() + 15
            while calls_after == 0 and time.monotonic() < deadline:
                await asyncio.sleep(0.2)
                calls_after = (await _calls(provider)) - calls_before

    assert snapshot["gate_still_closed"], "闸门被意外放行（装置失效）"
    assert snapshot["request_pending"], (
        "附件还没就绪时发送请求就已经返回了 —— 没有真的在等附件（或提前放行）",
        {"status": response.status_code if response is not None else None},
    )
    assert snapshot["calls_during"] == 0, (
        "附件还停在 prepared（首次复制没完成）时**模型已经被调用**了 —— 违反契约 §1.3「prepared 一律不就绪」",
        {"calls_during": snapshot["calls_during"],
         "body": (response.text[:200] if response is not None else None),
         "evidence": _started_evidence(snapshot)},
    )
    assert not _started_evidence(snapshot), ("闸门关闭期间出现了 TURN_START / 运行中的该轮", _started_evidence(snapshot))
    assert response is not None, "放行之后请求仍然没有返回"
    if response.status_code == 200:
        assert str(ready.get("state")) == "ready", ("等到就绪后必须真的就绪", ready)
        assert MARKER in content.text, ("就绪后副本内容必须正确", content.text[:60])
        executed = [t for t in _turns_for(app, "附件还没就绪就发送") if str(t["status"]) in ("running", "completed", "done")]
        assert calls_after >= 1, ("放行后这一轮必须真的执行（模型至少被调用一次）", calls_after)
        assert len(executed) == 1, ("放行后应当恰好执行一轮（不多不少）", executed)
    else:
        assert response.status_code >= 400, (response.status_code, response.text[:200])
        assert "attachment_not_ready" in response.text or "没有附上" in response.text, response.text[:200]
        assert calls_after == 0, ("被拒绝的轮次不得执行", calls_after)
    print("[诊断] prepared 未就绪：请求未返回=%s / 调用=%d；放行后 HTTP=%d；累计调用=%d；附件=%s"
          % (snapshot["request_pending"], snapshot["calls_during"], response.status_code, calls_after, ready.get("state")))


# ---- 2. 旧客户端缺字段（兜底绑定）同样不得提前执行 -----------------------------------


async def test_old_client_missing_field_also_waits(app, provider, tmp_path: Path):
    gate = _Gate()
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["附件就绪之后才允许执行。"]}])
        calls_before = await _calls(provider)
        src = _write_source(tmp_path, "r7-legacy.txt")

        with _gated_prepare(gate):
            attachment_id = await _register(client, src)
            assert await asyncio.to_thread(gate.entered.wait, 45), "后台复制没有进入闸门（装置失效）"
            row = await _row(client, attachment_id)
            assert str(row.get("state")) == "prepared" and not row.get("stored_path"), row
            snapshot = await _gated_send(client, gate, provider, calls_before, {"message": "旧客户端发送（缺字段）"})
            response = snapshot["response"]
            ready = await _wait_ready(client, attachment_id)
            calls_after = (await _calls(provider)) - calls_before

    assert snapshot["gate_still_closed"], "闸门被意外放行（装置失效）"
    assert snapshot["request_pending"], "旧客户端兜底绑定也必须等附件就绪（请求不该提前返回）"
    assert snapshot["calls_during"] == 0, (
        "旧客户端兜底绑定也必须等附件就绪：闸门关闭期间模型已被调用",
        {"calls_during": snapshot["calls_during"], "body": (response.text[:200] if response is not None else None)},
    )
    assert not _started_evidence(snapshot), _started_evidence(snapshot)
    assert response is not None, "放行之后请求仍然没有返回"
    if response.status_code == 200:
        assert str(ready.get("state")) == "ready", ready
        assert calls_after >= 1, ("放行后这一轮必须真的执行", calls_after)
    else:
        assert response.status_code >= 400, (response.status_code, response.text[:200])
        assert calls_after == 0, calls_after
    print("[诊断] 旧客户端缺字段：请求未返回=%s / 调用=%d；放行后 HTTP=%d；累计调用=%d"
          % (snapshot["request_pending"], snapshot["calls_during"], response.status_code, calls_after))


# ---- 3. 多附件：最后一个未就绪 -------------------------------------------------------


async def test_multi_attachment_last_prepared_blocks(app, provider, tmp_path: Path):
    gate = _Gate()
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["附件就绪之后才允许执行。"]}])
        calls_before = await _calls(provider)
        first_src = _write_source(tmp_path, "r7-multi-1.txt")
        second_src = _write_source(tmp_path, "r7-multi-2.txt")
        first_id = await _register(client, first_src)
        await _wait_ready(client, first_id)

        with _gated_prepare(gate):
            second_id = await _register(client, second_src)
            assert await asyncio.to_thread(gate.entered.wait, 45), "后台复制没有进入闸门（装置失效）"
            row = await _row(client, second_id)
            assert str(row.get("state")) == "prepared", ("装置前置", row)
            snapshot = await _gated_send(
                client, gate, provider, calls_before,
                {"message": "多附件最后一个未就绪", "attachment_ids": [first_id, second_id]},
            )
            response = snapshot["response"]
            await _wait_ready(client, second_id)
            calls_after = (await _calls(provider)) - calls_before

    assert snapshot["gate_still_closed"], "闸门被意外放行（装置失效）"
    assert snapshot["request_pending"], "多附件场景下请求不该提前返回"
    assert snapshot["calls_during"] == 0, (
        "多附件里最后一个还没就绪时模型已被调用",
        {"calls_during": snapshot["calls_during"], "body": (response.text[:200] if response is not None else None)},
    )
    assert not _started_evidence(snapshot), _started_evidence(snapshot)
    assert response is not None, "放行之后请求仍然没有返回"
    if response.status_code == 200:
        assert calls_after >= 1, ("放行后这一轮必须真的执行", calls_after)
    else:
        assert response.status_code >= 400, (response.status_code, response.text[:200])
        assert calls_after == 0, calls_after
    print("[诊断] 多附件最后一个未就绪：请求未返回=%s / 调用=%d；放行后 HTTP=%d；累计调用=%d"
          % (snapshot["request_pending"], snapshot["calls_during"], response.status_code, calls_after))


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
        os.unlink(stored)

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
