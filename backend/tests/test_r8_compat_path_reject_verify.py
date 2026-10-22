r"""D 独立验证（R8 问题三，R9 适配）：兼容路径（旧客户端无 attachment_ids）**固定集合 + 整体拒绝**。

R9 适配说明（docs/r9-contracts.md §6）：
* 旧装置把"首次背景复制"的提交步骤（os.replace）挂起/注入 ENOSPC 来制造"等待期间失败"；
  R9 取消本地副本策略，该注入缝已随产品逻辑一起删除，不得再使用。
* 准备期仍然真实存在：**浏览器上传的首次落盘**（begin_material → prepared → bind_for_turn
  有界等待）。用例用它制造"兼容路径真的在等待"的窗口；等待期间用**源文件引用缺失**
  （删除源文件）制造失败事实 —— 这是新契约下用户可见的等价失败（§6.2 resolve_source）。
* 判定规则不变：进入本次兼容发送时固定附件集合；集合内任一附件失败/缺失/不可用 →
  **结构化拒绝整轮**（模型 0 次、无 TURN_START、不留部分绑定）；显式 [] 与正常兼容路径照样通过。

运行：cd backend; uv run --frozen pytest -q tests/test_r8_compat_path_reject_verify.py
"""

from __future__ import annotations

import asyncio
import importlib.util
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
MARKER = "R8 兼容路径（R9 源文件引用）：只有这份源文件里才有的标记 8c02"
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
    # 让"等待在飞的首次落盘"在秒级内给出结论（判定语义不变）。
    app.state.ctx.attachments.prepare_wait_seconds = 1.5
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


async def _wait_state(client, attachment_id: str, states: tuple[str, ...], timeout: float = 30.0) -> dict:
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


async def _register(client, path: Path) -> str:
    resp = await client.post("/api/attachments", json={"source_path": str(path)})
    assert resp.status_code == 200, (resp.status_code, resp.text[:200])
    return str(resp.json()["attachment"]["id"])


def _begin_prepared_upload(app, *, name: str = "兼容路径准备中的上传.txt") -> str:
    """真实在飞的首次落盘（准备期）：兼容路径会在这里真的等一会儿。"""
    att = app.state.ctx.attachments.begin_material(name=name, source_kind="browser_upload")
    return str(att.id)


def _started(turns: list[dict]) -> list[dict]:
    return [t for t in turns if str(t["status"]) in ("running", "completed", "done", "accepted", "queued")]


# ---- 1. 核心：兼容路径等待期间附件失效 → 整轮拒绝 --------------------------------


async def test_legacy_wait_failure_rejects_the_whole_turn(app, provider, tmp_path: Path):
    """旧用例：等待期间复制 ENOSPC 失败 → 整轮拒绝。
    R9 等价：等待期间另一条附件的**源文件被删除**（resolve 失败）→ 整轮拒绝。
    """
    message = "旧客户端（无 attachment_ids）在等待期间源文件失效"
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["我不该在这一轮被调用。"]}])
        calls_before = await _calls(provider)
        ready_src = _write_source(tmp_path, "r8-legacy-ready.txt")
        ready_id = await _register(client, ready_src)
        ready_row = await _wait_state(client, ready_id, ("ready",) + TERMINAL)
        assert str(ready_row.get("state")) == "ready", ready_row
        waiting_id = _begin_prepared_upload(app)

        send = asyncio.create_task(client.post("/api/turns", json={"message": message}))
        await asyncio.sleep(0.4)  # 兼容路径已经固定集合并进入"等首次落盘"
        assert not send.done(), "准备还没结束，兼容路径不得先受理"
        ready_src.unlink()  # 等待期间：源文件缺失（resolve 失败）
        response = await asyncio.wait_for(send, timeout=60)
        await asyncio.sleep(0.2)
        calls_during = (await _calls(provider)) - calls_before
        ready_after = (await client.get("/api/attachments/%s" % ready_id)).json().get("attachment") or {}
        waiting_after = (
            await client.get("/api/attachments/%s" % waiting_id)
        ).json().get("attachment") or {}
        turns = _turns_for(app, message)
        queue = (await client.get("/api/turns/queue")).json()

    assert response.status_code >= 400, (
        "兼容路径等待期间源文件失效，却仍然 200 accepted（失败事实被静默丢掉）",
        response.status_code,
        response.text[:240],
    )
    assert calls_during == 0, ("被拒的轮次不得调用模型", calls_during)
    assert not _started(turns), ("被拒的轮次仍然开始执行了（TURN_START）", turns)
    assert not (queue.get("running") or queue.get("queued")), queue
    assert str(ready_after.get("state")) == "missing", ("缺失事实必须如实保留", ready_after.get("state"))
    assert not ready_after.get("turn_id"), ("好附件被半绑定到被拒的轮次上", ready_after.get("turn_id"))
    assert not waiting_after.get("turn_id"), waiting_after.get("turn_id")
    print("[诊断] 兼容路径等待期失效：HTTP=%d；调用=%d；台账=%s；好附件=%s"
          % (response.status_code, calls_during, turns, ready_after.get("state")))


# ---- 2. 多附件：任一失败 → 整轮拒绝且无半绑定 --------------------------------------


async def test_legacy_multi_attachment_any_failure_rejects_all(app, provider, tmp_path: Path):
    """旧用例：多附件里一个复制失败 → 整轮拒绝。
    R9 等价：进入时集合内已有一条源文件缺失（历史失败事实），整轮拒绝、无半绑定。
    """
    message = "旧客户端多附件（一个源文件缺失）"
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["我不该在这一轮被调用。"]}])
        calls_before = await _calls(provider)
        ok_id = await _register(client, _write_source(tmp_path, "r8-multi-ok.txt"))
        bad_src = _write_source(tmp_path, "r8-multi-bad.txt")
        bad_id = await _register(client, bad_src)
        bad_src.unlink()

        response = await client.post("/api/turns", json={"message": message})
        await asyncio.sleep(0.3)
        calls_during = (await _calls(provider)) - calls_before
        turns = _turns_for(app, message)
        ok_after = (await client.get("/api/attachments/%s" % ok_id)).json().get("attachment") or {}
        bad_after = (await client.get("/api/attachments/%s" % bad_id)).json().get("attachment") or {}

    assert response.status_code >= 400, (
        "多附件里有一个失败必须**整轮拒绝**", response.status_code, response.text[:240]
    )
    assert calls_during == 0, calls_during
    assert not _started(turns), turns
    assert not ok_after.get("turn_id"), ("好附件被半绑定到被拒的轮次上", ok_after.get("turn_id"))
    assert not bad_after.get("turn_id"), bad_after.get("turn_id")
    assert str(bad_after.get("state")) == "missing", bad_after.get("state")
    print("[诊断] 多附件任一失败：HTTP=%d；调用=%d；台账=%s；好附件状态=%s"
          % (response.status_code, calls_during, turns, ok_after.get("state")))


# ---- 3. 集合内附件不可用（源文件已不是可读文件）→ 整轮拒绝 -------------------------


async def test_legacy_unusable_source_rejects(app, provider, tmp_path: Path):
    """旧用例：等待期间副本被删（不可读）→ 整轮拒绝。
    R9 等价：集合内一条附件的源文件已不是可读文件（同一路径变成目录）→ 整轮拒绝。
    """
    message = "旧客户端（集合内源文件不可用）"
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["我不该在这一轮被调用。"]}])
        calls_before = await _calls(provider)
        first_id = await _register(client, _write_source(tmp_path, "r8-legacy-ok.txt"))
        second_src = _write_source(tmp_path, "r8-legacy-gone.txt")
        second_id = await _register(client, second_src)
        second_src.unlink()
        second_src.mkdir()

        response = await client.post("/api/turns", json={"message": message})
        await asyncio.sleep(0.3)
        calls_during = (await _calls(provider)) - calls_before
        turns = _turns_for(app, message)
        first_after = (await client.get("/api/attachments/%s" % first_id)).json().get("attachment") or {}
        second_after = (
            await client.get("/api/attachments/%s" % second_id)
        ).json().get("attachment") or {}

    assert response.status_code >= 400, (
        "集合内源文件不可用，仍 200 accepted（集合内任何一条变了都必须整轮拒绝）",
        {"status": response.status_code, "body": response.text[:240], "turns": turns},
    )
    assert calls_during == 0, ("被拒的轮次不得调用模型", calls_during)
    assert not _started(turns), turns
    assert not first_after.get("turn_id"), ("好附件被半绑定到被拒的轮次上", first_after.get("turn_id"))
    assert not second_after.get("turn_id"), second_after.get("turn_id")
    assert str(second_after.get("state")) in ("failed", "missing"), second_after.get("state")
    print("[诊断] 集合内不可用：HTTP=%d；调用=%d；台账=%s" % (response.status_code, calls_during, turns))


# ---- 4. 绿守卫：显式空列表 / 正常兼容路径 -----------------------------------------


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


async def test_legacy_happy_path_executes(app, provider, tmp_path: Path):
    """旧用例：正常兼容路径等首次准备完成后执行 → R9 等价：正常引用发送通过。"""
    message = "旧客户端正常路径"
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["附件就绪之后才回答。"]}])
        calls_before = await _calls(provider)
        attachment_id = await _register(client, _write_source(tmp_path, "r8-legacy-ok.txt"))
        ready = await _wait_state(client, attachment_id, ("ready",) + TERMINAL)
        assert str(ready.get("state")) == "ready", ready

        response = await client.post("/api/turns", json={"message": message})
        assert response.status_code == 200, (response.status_code, response.text[:240])
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if (await _calls(provider)) > calls_before:
                break
            await asyncio.sleep(0.1)
        turns = _turns_for(app, message)
        content = await client.get("/api/attachments/%s/content" % attachment_id)

    assert (await _calls(provider)) > calls_before, "正常兼容路径必须执行"
    assert MARKER in content.text, ("读取必须走源文件", content.text[:80])
    assert [t for t in turns if t["status"] in ("completed", "running", "done")], turns
    print("[诊断] 兼容路径正常：HTTP=200；台账=%s；源文件内容正确" % turns)
