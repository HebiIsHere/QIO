r"""D 独立验证（R8 问题二）：取消确认必须指向**本次取消的那个 turn_id**。

契约：docs/plans/2026-10-09-send-cancel-integrity.md §1.2（冻结）。
前端缺陷（Composer.vue:647-667）：already_started 分支调 stopTurn() → session.stopActiveTurn()，
停的是 **session.activeTurnId**（可能是别的任务），不是取消确认返回的 turn_id。

两种时序都覆盖（后端实构，真实 ASGI 路由 + 真实 TurnManager/编排 + 假 provider）：
* **准备中**：A 正在运行、B 因附件仍在准备 → 取消 B 必须 cancelled=true 且回执 turn_id == B；
* **已放行/入队**：B 已放行 → 回执必须 already_started=true **且带 B 自己的 turn_id**（前端据此走既有停止流程），
  按那个 id 停止 → B 被取消、A 继续跑完、B 不得随后开始执行。
禁止只断言「某个停止入口被调用」：这里核对回执 turn_id、台账状态、队列里的 running。

运行：cd backend; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/test_r8_cancel_target_verify.py -q
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
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
PREPARE_HEADER = "X-QIO-Prepare-Id"


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
    conn = connect(tmp_path / "r8_cancel.db")
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
            "secret": "sk-r8-fake-0002",
            "default_model": "verify-model",
            "tags": ["main-loop"],
        },
    )
    assert resp.status_code == 200 and (resp.json().get("verify") or {}).get("ok"), resp.text[:200]


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
def _gated_prepare_copy(gate_event: threading.Event):
    """把登记的提交步骤（os.replace）挂起：让 B 真的停在「准备中」。"""
    real_replace = attachments_mod.os.replace

    def _gated(src, dst):  # noqa: ANN001
        gate_event.wait()  # 不设超时：只有测试显式 release 才放行
        return real_replace(src, dst)

    attachments_mod.os.replace = _gated
    try:
        yield
    finally:
        attachments_mod.os.replace = real_replace


def _status(app, turn_id: str) -> str | None:
    row = app.state.ctx.conn.execute(
        "SELECT status FROM turn_journal WHERE turn_id = ? ORDER BY rowid DESC LIMIT 1", (turn_id,)
    ).fetchone()
    return row[0] if row else None


async def _wait_status(app, turn_id: str, wanted: tuple[str, ...], timeout: float = 90.0) -> str | None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = _status(app, turn_id)
        if last in wanted:
            return last
        await asyncio.sleep(0.1)
    return last


def _write_source(tmp_path: Path, name: str) -> Path:
    src = tmp_path / name
    src.write_text("R8 取消目标：附件内容。\n", encoding="utf-8")
    return src


# ---- 1. 准备中：取消只影响 B ---------------------------------------------------------


async def test_cancel_during_preparation_targets_that_turn(app, provider, tmp_path: Path):
    gate = threading.Event()
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["A 第一段。", "A 第二段。"], "chunk_delay_ms": 3000}])
        a = await client.post("/api/turns", json={"message": "A 正在运行"})
        assert a.status_code == 200, a.text[:200]
        turn_a = str(a.json()["turn_id"])
        assert await _wait_status(app, turn_a, ("running",)) == "running", "A 没有开始运行（装置失效）"

        src = _write_source(tmp_path, "r8-cancel-attach.txt")
        with _gated_prepare_copy(gate):
            registered = await client.post("/api/attachments", json={"source_path": str(src)})
            assert registered.status_code == 200, registered.text[:200]
            attachment_id = str(registered.json()["attachment"]["id"])
            b_task = asyncio.create_task(
                client.post(
                    "/api/turns",
                    json={"message": "B 附件还在准备", "attachment_ids": [attachment_id]},
                    headers={PREPARE_HEADER: "r8-prep-B"},
                )
            )
            await asyncio.sleep(1.0)  # B 正在准备（等附件）
            assert not b_task.done(), "B 不该在准备完成前返回（装置失效）"

            cancel = await client.post("/api/turns/prepare/r8-prep-B/cancel")
            receipt = cancel.json()
            assert cancel.status_code == 200, cancel.text[:200]
            assert receipt.get("cancelled") is True, ("准备中的 B 必须能取消", receipt)
            assert receipt.get("already_started") is not True, receipt
            assert str(receipt.get("turn_id")), ("取消回执必须带 turn_id（前端据此核对身份）", receipt)
            turn_b = str(receipt["turn_id"])
            assert turn_b != turn_a, ("取消的目标不能是正在跑的 A", turn_a, turn_b)

            status_a_after_cancel = _status(app, turn_a)
            gate.set()  # 放行附件提交：B 已经被取消，**不得**因此开始执行
            with contextlib.suppress(Exception):
                await asyncio.wait_for(b_task, timeout=60)
            await asyncio.sleep(0.5)
            status_b = _status(app, turn_b)
            calls_after_cancel = await _calls(provider)
            final_a = await _wait_status(app, turn_a, ("completed", "failed", "cancelled"), timeout=90)
            await asyncio.sleep(3.0)  # 观察窗：B 不得随后开始执行
            status_b_later = _status(app, turn_b)
            calls_after = await _calls(provider)
            queue_later = (await client.get("/api/turns/queue")).json()

    assert status_a_after_cancel in ("running", "queued", "accepted", "completed"), (
        "取消 B 把 A 也停了（A 不得被取消）", status_a_after_cancel
    )
    assert status_a_after_cancel != "cancelled", ("A 被错误地取消了", status_a_after_cancel)
    assert status_b == "cancelled", ("B 必须被取消", status_b)
    assert final_a == "completed", ("A 必须继续跑完", final_a)
    assert status_b_later == "cancelled", ("被取消的 B 不得随后开始执行", status_b_later)
    assert calls_after <= 2, ("被取消的 B 不得产生模型调用", {"calls": calls_after, "at_cancel": calls_after_cancel})
    print("[诊断] 准备中取消：回执 turn_id=%s（A=%s）；B=%s→%s；A=%s；模型调用=%d"
          % (turn_b, turn_a, status_b, status_b_later, final_a, calls_after))


# ---- 2. 已放行/入队：回执带自己的 turn_id，按它停止 ------------------------------------


async def test_already_released_reports_its_own_turn_id(app, provider):
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["A 第一段。", "A 第二段。"], "chunk_delay_ms": 3000}])
        a = await client.post("/api/turns", json={"message": "A 正在运行（已放行用例）"})
        turn_a = str(a.json()["turn_id"])
        assert await _wait_status(app, turn_a, ("running",)) == "running"

        b = await client.post(
            "/api/turns", json={"message": "B 已放行入队"}, headers={PREPARE_HEADER: "r8-prep-released"}
        )
        turn_b = str(b.json()["turn_id"])
        cancel = await client.post("/api/turns/prepare/r8-prep-released/cancel")
        receipt = cancel.json()
        assert receipt.get("already_started") is True, ("已放行必须如实回报 already_started", receipt)
        assert str(receipt.get("turn_id")) == turn_b, (
            "回执里的 turn_id 必须是**B 自己**（不是正在跑的 A）",
            {"got": receipt.get("turn_id"), "want": turn_b, "running": turn_a},
        )
        stop = await client.post("/api/turns/%s/cancel" % turn_b)
        assert stop.status_code < 500, stop.text[:200]
        await asyncio.sleep(0.5)
        status_b = _status(app, turn_b)
        final_a = await _wait_status(app, turn_a, ("completed", "failed", "cancelled"), timeout=90)
        status_a_end = _status(app, turn_a)

    assert status_b in ("cancelled", "interrupted"), ("按 B 的 id 停止必须真的停掉 B", status_b)
    assert final_a == "completed", ("A 必须继续跑完", final_a)
    assert status_a_end in ("completed", "running"), status_a_end
    print("[诊断] 已放行：回执=%s；B=%s；A=%s" % (receipt, status_b, final_a))


# ---- 3. 重复取消幂等 / 未知标识幂等 ---------------------------------------------------


async def test_repeated_cancel_and_unknown_id_are_idempotent(app, provider, tmp_path: Path):
    gate = threading.Event()
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["慢一点。", "再来。"], "chunk_delay_ms": 2500}])
        a = await client.post("/api/turns", json={"message": "幂等用例的占用轮"})
        turn_a = str(a.json()["turn_id"])
        assert await _wait_status(app, turn_a, ("running",)) == "running"

        src = _write_source(tmp_path, "r8-idem-attach.txt")
        with _gated_prepare_copy(gate):
            registered = await client.post("/api/attachments", json={"source_path": str(src)})
            attachment_id = str(registered.json()["attachment"]["id"])
            b_task = asyncio.create_task(
                client.post(
                    "/api/turns",
                    json={"message": "幂等用例的 B", "attachment_ids": [attachment_id]},
                    headers={PREPARE_HEADER: "r8-prep-idem"},
                )
            )
            await asyncio.sleep(1.0)
            first = (await client.post("/api/turns/prepare/r8-prep-idem/cancel")).json()
            second = (await client.post("/api/turns/prepare/r8-prep-idem/cancel")).json()
            unknown = (await client.post("/api/turns/prepare/r8-prep-no-such-id/cancel")).json()
            gate.set()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(b_task, timeout=60)
        turn_b = str(first.get("turn_id"))
        await _wait_status(app, turn_a, ("completed", "failed", "cancelled"), timeout=90)
        status_b = _status(app, turn_b)

    assert first.get("cancelled") is True, first
    assert second.get("cancelled") is True or second.get("unknown") is True, ("重复取消必须幂等", second)
    assert unknown.get("unknown") is True, ("未知标识必须幂等返回 unknown", unknown)
    assert status_b == "cancelled", ("B 不得因为重复取消/放行而开始执行", status_b)
    print("[诊断] 幂等：first=%s；second=%s；unknown=%s；B=%s" % (first, second, unknown, status_b))


# ---- 4. 身份缺失：不得取消任何轮次 ----------------------------------------------------


async def test_missing_prepare_id_cancels_nothing(app, provider):
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["正常回答。"]}])
        a = await client.post("/api/turns", json={"message": "身份缺失的一轮"})
        turn_a = str(a.json()["turn_id"])
        unknown = (await client.post("/api/turns/prepare/r8-prep-never-sent/cancel")).json()
        final = await _wait_status(app, turn_a, ("completed", "failed", "cancelled"), timeout=60)

    assert unknown.get("unknown") is True, unknown
    assert final == "completed", ("身份缺失不得取消任何轮次", final)
    print("[诊断] 身份缺失：cancel=%s；该轮最终=%s" % (unknown, final))
