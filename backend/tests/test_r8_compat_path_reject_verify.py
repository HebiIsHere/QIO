r"""D 独立验证（R8 问题三）：兼容路径（旧客户端无 attachment_ids）**固定集合 + 整体拒绝**。

契约：docs/plans/2026-10-09-send-cancel-integrity.md §1.3（冻结）。
基线 9f5bc07 缺陷（services/attachments.py:1235-1238）：兼容分支先等首次准备，随后**重新枚举**未绑定附件；
_bindable 不满足就 continue —— 等待期间的失败/取消/删除/不可读**被静默丢掉**
（反例实测：复制在闸门期间真实 failed → 发送 200 accepted、rejected=[]、模型 1 次）。

判定规则（用户可见结果）：
* 进入本次兼容发送时固定附件集合；集合内任一附件在等待期间失败/取消/删除/不可读 → **结构化拒绝整轮**；
* 被拒后：模型/工具 **0 次**、**无 TURN_START**、**不留部分绑定**（附件不得被绑到这个被拒的轮次）；
* 显式 attachment_ids: [] 仍表示不带附件；正常路径（首次准备成功后执行/显式列表/重试）照样通过。

运行：cd backend; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/test_r8_compat_path_reject_verify.py -q
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
MARKER = "R8 兼容路径：只有这份副本里才有的标记 8c02"
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
    conn = connect(tmp_path / "r8_compat.db")
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
            "secret": "sk-r8-fake-0001",
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


def _turns_for(app, message: str) -> list[dict]:
    rows = app.state.ctx.conn.execute(
        "SELECT turn_id, status FROM turn_journal WHERE message = ? ORDER BY rowid", (message,)
    ).fetchall()
    return [{"turn_id": r[0], "status": r[1]} for r in rows]


async def _wait_state(client, attachment_id: str, states: tuple[str, ...], timeout: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = (await client.get("/api/attachments/%s" % attachment_id)).json().get("attachment") or {}
        if str(last.get("state")) in states:
            return last
        await asyncio.sleep(0.05)
    return last


def _write_source(tmp_path: Path, name: str) -> Path:
    src = tmp_path / name
    src.write_text(MARKER + "\n", encoding="utf-8")
    return src


@contextlib.contextmanager
def _fail_prepare_copy(mode: str):
    """**首次准备（登记）**的复制必然失败：真实调用点是 <目标>.part 写完后 \`os.replace\` 提交
    （不是 shutil.copyfile —— 那是重试克隆的退路；r6 的教训：Linux 上包 builtins.open 也打不到）。
    """
    import errno

    real_replace = attachments_mod.os.replace
    state = {"fired": False}

    def _boom(src, dst):  # noqa: ANN001
        state["fired"] = True
        raise OSError(errno.ENOSPC, "No space left on device（受控错误：%s）" % mode)

    attachments_mod.os.replace = _boom
    try:
        yield state
    finally:
        attachments_mod.os.replace = real_replace


@contextlib.contextmanager
def _gated_prepare_copy(gate_event):
    """把登记的提交步骤（os.replace）挂起，直到测试 release（用于构造「等待期间变化」）。"""
    real_replace = attachments_mod.os.replace

    def _gated(src, dst):  # noqa: ANN001
        gate_event.wait()  # 不设超时：只有测试显式 release 才放行
        return real_replace(src, dst)

    attachments_mod.os.replace = _gated
    try:
        yield
    finally:
        attachments_mod.os.replace = real_replace


def _bound_ids(resp) -> list[str]:
    body = resp.json() if hasattr(resp, "json") else {}
    return [str(x) for x in (body.get("bound_attachment_ids") or [])]


# ---- 1. 核心：兼容路径等待期间复制真实 failed → 整轮拒绝 -----------------------------


async def test_legacy_wait_failure_rejects_the_whole_turn(app, provider, tmp_path: Path):
    message = "旧客户端（无 attachment_ids）在等待期间复制失败"
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["我不该在这一轮被调用。"]}])
        calls_before = await _calls(provider)
        src = _write_source(tmp_path, "r8-legacy-fail.txt")

        with _fail_prepare_copy("legacy-wait-failure") as injected:
            registered = await client.post("/api/attachments", json={"source_path": str(src)})
            assert registered.status_code == 200, registered.text[:200]
            attachment_id = str(registered.json()["attachment"]["id"])
            # 旧客户端：body 里**没有** attachment_ids 字段（走兼容兜底路径）
            response = await client.post("/api/turns", json={"message": message})
            await asyncio.sleep(1.0)
            calls_during = (await _calls(provider)) - calls_before
            row = (await client.get("/api/attachments/%s" % attachment_id)).json().get("attachment") or {}
            turns = _turns_for(app, message)
            queue = (await client.get("/api/turns/queue")).json()

    assert injected["fired"], "复制失败注入没有触发（装置失效：没走到复制）"
    assert response.status_code >= 400, (
        "兼容路径等待期间附件真实失败，却仍然 200 accepted（基线缺陷：重新枚举丢掉失败事实）",
        {"status": response.status_code, "body": response.text[:240], "turns": turns},
    )
    assert calls_during == 0, ("被拒的轮次不得调用模型", calls_during)
    assert not [t for t in turns if t["status"] in ("running", "completed", "done")], (
        "被拒的轮次仍然开始执行了（TURN_START）", turns
    )
    assert str(row.get("state")) == "failed", ("附件的失败事实必须如实保留", row.get("state"), row.get("error"))
    assert str(row.get("turn_id") or "") not in {t["turn_id"] for t in turns}, (
        "被拒的轮次留下了部分绑定（附件被绑到它上面）", row.get("turn_id"), turns
    )
    print("[诊断] 兼容路径失败：HTTP=%d；模型调用=%d；台账=%s；附件=%s；原因=%s"
          % (response.status_code, calls_during, turns, row.get("state"), str(row.get("error"))[:80]))


# ---- 2. 多附件：任一失败 → 整轮拒绝且无半绑定 ----------------------------------------


async def test_legacy_multi_attachment_any_failure_rejects_all(app, provider, tmp_path: Path):
    message = "旧客户端多附件（一个失败）"
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["我不该在这一轮被调用。"]}])
        calls_before = await _calls(provider)
        ok_src = _write_source(tmp_path, "r8-multi-ok.txt")
        bad_src = _write_source(tmp_path, "r8-multi-bad.txt")
        ok_registered = await client.post("/api/attachments", json={"source_path": str(ok_src)})
        ok_id = str(ok_registered.json()["attachment"]["id"])
        ok_row = await _wait_state(client, ok_id, ("ready",) + TERMINAL)
        assert str(ok_row.get("state")) == "ready", ok_row

        with _fail_prepare_copy("multi-failure") as injected:
            bad_registered = await client.post("/api/attachments", json={"source_path": str(bad_src)})
            bad_id = str(bad_registered.json()["attachment"]["id"])
            response = await client.post("/api/turns", json={"message": message})
            await asyncio.sleep(1.0)
            calls_during = (await _calls(provider)) - calls_before
            turns = _turns_for(app, message)
            ok_after = (await client.get("/api/attachments/%s" % ok_id)).json().get("attachment") or {}
            bad_after = (await client.get("/api/attachments/%s" % bad_id)).json().get("attachment") or {}

    assert injected["fired"], "复制失败注入没有触发（装置失效）"
    assert response.status_code >= 400, (
        "多附件里有一个失败必须**整轮拒绝**", response.status_code, response.text[:240]
    )
    assert calls_during == 0, calls_during
    assert not [t for t in turns if t["status"] in ("running", "completed", "done")], turns
    turn_ids = {t["turn_id"] for t in turns}
    assert str(ok_after.get("turn_id") or "") not in turn_ids, (
        "好附件被半绑定到被拒的轮次上", ok_after.get("turn_id"), turns
    )
    assert str(bad_after.get("turn_id") or "") not in turn_ids, bad_after.get("turn_id")
    assert str(bad_after.get("state")) == "failed", bad_after
    print("[诊断] 多附件任一失败：HTTP=%d；调用=%d；台账=%s；好附件状态=%s"
          % (response.status_code, calls_during, turns, ok_after.get("state")))


# ---- 3. 等待期间副本被删（不可读）→ 整轮拒绝 ----------------------------------------


async def test_legacy_unreadable_copy_rejects(app, provider, tmp_path: Path):
    """等待期间副本变不可读 → 整轮拒绝（注意：进入时就不可读属于「历史记录」，契约 §1.3 集合政策
    明确不阻断纯文字发送，所以不可读必须发生在**快照之后**）。"""
    import os
    import threading

    message = "旧客户端（等待期间副本被删）"
    gate = threading.Event()
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["我不该在这一轮被调用。"]}])
        calls_before = await _calls(provider)
        # 附件 #1：先就绪（它的文件会在等待期间被删）
        first_src = _write_source(tmp_path, "r8-legacy-gone.txt")
        first = await client.post("/api/attachments", json={"source_path": str(first_src)})
        first_id = str(first.json()["attachment"]["id"])
        first_ready = await _wait_state(client, first_id, ("ready",) + TERMINAL)
        assert str(first_ready.get("state")) == "ready", first_ready
        stored = str(first_ready.get("stored_path") or "")
        assert stored and os.path.exists(stored), first_ready

        # 附件 #2：登记后卡在提交步骤 → 兼容路径会**等**它，给我们制造「等待期间」
        second_src = _write_source(tmp_path, "r8-legacy-wait.txt")
        with _gated_prepare_copy(gate):
            second = await client.post("/api/attachments", json={"source_path": str(second_src)})
            second_id = str(second.json()["attachment"]["id"])
            send = asyncio.create_task(client.post("/api/turns", json={"message": message}))
            await asyncio.sleep(1.0)          # 让兼容路径进入等待
            os.unlink(stored)                  # 等待期间：#1 的副本变不可读
            gate.set()                         # 放行 #2 的提交 → 触发落库前复核
            response = await asyncio.wait_for(send, timeout=120)
            calls_during = (await _calls(provider)) - calls_before
            turns = _turns_for(app, message)
            second_after = (await client.get("/api/attachments/%s" % second_id)).json().get("attachment") or {}

    assert response.status_code >= 400, (
        "等待期间副本变不可读，仍 200 accepted（集合内任何一条变了都必须整轮拒绝）",
        {"status": response.status_code, "body": response.text[:240], "turns": turns},
    )
    assert calls_during == 0, ("被拒的轮次不得调用模型", calls_during)
    assert not [t for t in turns if t["status"] in ("running", "completed", "done")], turns
    assert str(second_after.get("turn_id") or "") not in {t["turn_id"] for t in turns}, (
        "好附件被半绑定到被拒的轮次上", second_after.get("turn_id"), turns
    )
    print("[诊断] 等待期间不可读：HTTP=%d；调用=%d；台账=%s" % (response.status_code, calls_during, turns))


# ---- 4. 绿守卫：显式空列表 / 正常兼容路径 -------------------------------------------


async def test_explicit_empty_list_still_means_no_attachments(app, provider, tmp_path: Path):
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["这一轮没有附件。"]}])
        calls_before = await _calls(provider)
        response = await client.post("/api/turns", json={"message": "纯文字（显式空列表）", "attachment_ids": []})
        assert response.status_code == 200, (response.status_code, response.text[:200])
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if (await _calls(provider)) > calls_before:
                break
            await asyncio.sleep(0.1)
        assert (await _calls(provider)) > calls_before, "显式空列表必须照常执行"
    print("[诊断] 显式空列表：200 且照常执行")


async def test_legacy_happy_path_waits_then_executes(app, provider, tmp_path: Path):
    message = "旧客户端正常路径"
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["附件就绪之后才回答。"]}])
        calls_before = await _calls(provider)
        src = _write_source(tmp_path, "r8-legacy-ok.txt")
        registered = await client.post("/api/attachments", json={"source_path": str(src)})
        attachment_id = str(registered.json()["attachment"]["id"])
        ready = await _wait_state(client, attachment_id, ("ready",) + TERMINAL)
        assert str(ready.get("state")) == "ready", ready

        response = await client.post("/api/turns", json={"message": message})
        assert response.status_code == 200, (response.status_code, response.text[:240])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if (await _calls(provider)) > calls_before:
                break
            await asyncio.sleep(0.1)
        turns = _turns_for(app, message)
        content = await client.get("/api/attachments/%s/content" % attachment_id)

    assert (await _calls(provider)) > calls_before, "正常兼容路径必须执行"
    assert MARKER in content.text, ("副本内容必须正确", content.text[:60])
    assert [t for t in turns if t["status"] in ("completed", "running", "done")], turns
    print("[诊断] 兼容路径正常：HTTP=200；台账=%s；副本内容正确" % turns)
