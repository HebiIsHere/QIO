r"""D 独立验证（R7 问题二，R9 适配）：**唯一执行就绪条件** —— 不可用的附件不得进入执行。

R9 适配说明（用户产品决定，见 docs/r9-contracts.md §6/§8 与 docs/r9-merge-reports/W6.md）：
* R9 取消"本地副本"策略：附件 = **源文件引用**（register_source 不复制、不移动），
  所以旧用例里"首次后台复制未完成（prepared）→ 发送必须等待"的装置已无对象；
* 新契约下用户可见的等价问题变成：**源文件缺失 / 不可用 → 整轮结构化拒绝**（不入队、
  模型 0 次、无 TURN_START、不留部分绑定），显式 attachment_ids: [] 照常执行，
  正常引用发送照常通过。用例按这三条重新表达，覆盖不减少（映射见 docs/r9-reports/W6.md）。

运行：cd backend; uv run --frozen pytest -q tests/test_r7_readiness_prepared_verify.py
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
MARKER = "R7 就绪条件（R9 源文件引用）：只有这份源文件里才有的标记 7a31"
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
    # 新契约下 bind 只可能等"浏览器上传首次落盘"这类在飞准备；把有界等待压短，
    # 让「等不到 → 结构化拒绝」的用例在秒级内得到结论（不改变任何判定语义）。
    app.state.ctx.attachments.prepare_wait_seconds = 1.5
    return app


def _client(app):
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://qio.test", timeout=180.0
    )


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


def _turns_for(app, message: str) -> list[dict]:
    rows = app.state.ctx.conn.execute(
        "SELECT turn_id, status FROM turn_journal WHERE message = ? ORDER BY rowid", (message,)
    ).fetchall()
    return [{"turn_id": r[0], "status": r[1]} for r in rows]


def _started(turns: list[dict]) -> list[dict]:
    return [t for t in turns if str(t["status"]) in ("running", "completed", "done", "accepted", "queued")]


async def _wait_calls(provider, before: int, timeout: float = 45.0) -> int:
    deadline = time.monotonic() + timeout
    calls = before
    while time.monotonic() < deadline:
        calls = await _calls(provider)
        if calls > before:
            return calls
        await asyncio.sleep(0.1)
    return calls


# ---- 1. 核心（R9 等价）：源文件缺失 → 整轮结构化拒绝，绝不静默少带附件执行 --------


async def test_missing_source_rejects_whole_turn(app, provider, tmp_path: Path):
    """旧用例 test_prepared_attachment_blocks_execution 的 R9 等价：
    R9 没有"等待副本"这一步，但"附件不可用就不许开始执行"的判据必须还在。
    """
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["我不该在这一轮被调用。"]}])
        calls_before = await _calls(provider)
        src = _write_source(tmp_path, "r7-missing.txt")
        attachment_id = await _register(client, src)
        assert str((await _row(client, attachment_id)).get("state")) == "ready"
        src.unlink()  # 源文件在发送前被删除/移走

        message = "源文件已缺失就发送"
        response = await client.post(
            "/api/turns", json={"message": message, "attachment_ids": [attachment_id]}
        )
        await asyncio.sleep(0.3)
        calls_after = (await _calls(provider)) - calls_before
        row = await _row(client, attachment_id)
        turns = _turns_for(app, message)
        queue = (await client.get("/api/turns/queue")).json()

    assert response.status_code == 409, (
        "源文件缺失必须结构化拒绝整轮（不得静默少带附件执行）",
        response.status_code,
        response.text[:240],
    )
    assert calls_after == 0, ("被拒的轮次不得调用模型", calls_after)
    assert not _started(turns), ("被拒的轮次不得进入队列/开始执行（无 TURN_START）", turns)
    assert not (queue.get("running") or queue.get("queued")), queue
    assert str(row.get("state")) == "missing", ("缺失事实必须如实反映在附件上", row.get("state"))
    assert not row.get("turn_id"), ("被拒的轮次不得留下部分绑定", row.get("turn_id"))
    print("[诊断] 源缺失拒绝：HTTP=%d；调用=%d；附件=%s；台账=%s"
          % (response.status_code, calls_after, row.get("state"), turns))


# ---- 2. 旧客户端缺字段：兼容路径同样不得把不可用草稿带成"已发送" ---------------------


async def test_old_client_missing_field_rejects_unavailable_draft(app, provider, tmp_path: Path):
    """旧用例 test_old_client_missing_field_also_waits 的 R9 等价。

    旧用例盯的是"旧客户端兜底绑定也必须等首次准备就绪、不得提前执行"。R9 没有副本等待；
    等价且用户可见的判据是：兼容路径（body 缺 attachment_ids）一旦把这条草稿算进本轮，
    它就**不能是不可用的** —— 源文件已缺失时整轮结构化拒绝（模型 0 次、无 TURN_START、
    不留部分绑定），绝不出现"兼容路径静默少带一个附件照发"。
    """
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["我不该在这一轮被调用。"]}])
        calls_before = await _calls(provider)
        src = _write_source(tmp_path, "r7-legacy-missing.txt")
        attachment_id = await _register(client, src)
        src.unlink()

        message = "旧客户端发送（缺字段、草稿已缺失）"
        response = await client.post("/api/turns", json={"message": message})
        await asyncio.sleep(0.3)
        calls_after = (await _calls(provider)) - calls_before
        row = await _row(client, attachment_id)
        turns = _turns_for(app, message)
        queue = (await client.get("/api/turns/queue")).json()

    assert response.status_code == 409, (
        "旧客户端兜底绑定遇到不可用草稿必须结构化拒绝整轮（不得少带一个附件照发）",
        response.status_code,
        response.text[:240],
    )
    assert calls_after == 0, ("被拒的轮次不得调用模型", calls_after)
    assert not _started(turns), ("被拒的轮次不得开始执行（无 TURN_START）", turns)
    assert not (queue.get("running") or queue.get("queued")), queue
    assert not row.get("turn_id"), ("不可用草稿不得被兜底路径绑进这一轮", row.get("turn_id"))
    assert str(row.get("state")) == "missing", row.get("state")
    print("[诊断] 旧客户端缺字段：HTTP=%d；调用=%d；草稿状态=%s"
          % (response.status_code, calls_after, row.get("state")))


# ---- 3. 多附件：任一不可用 → 整轮拒绝、不留部分绑定 --------------------------------


async def test_multi_attachment_missing_one_rejects_all(app, provider, tmp_path: Path):
    """旧用例 test_multi_attachment_last_prepared_blocks 的 R9 等价：
    "最后一个未就绪 → 整轮拒绝" → "任意一个源文件缺失 → 整轮拒绝、无半绑定"。
    """
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["我不该在这一轮被调用。"]}])
        calls_before = await _calls(provider)
        first_id = await _register(client, _write_source(tmp_path, "r7-multi-1.txt"))
        second_src = _write_source(tmp_path, "r7-multi-2.txt")
        second_id = await _register(client, second_src)
        second_src.unlink()

        message = "多附件有一个源文件缺失"
        response = await client.post(
            "/api/turns", json={"message": message, "attachment_ids": [first_id, second_id]}
        )
        await asyncio.sleep(0.3)
        calls_after = (await _calls(provider)) - calls_before
        first_row = await _row(client, first_id)
        second_row = await _row(client, second_id)
        turns = _turns_for(app, message)

    assert response.status_code == 409, (
        "多附件里任一不可用必须整轮拒绝",
        response.status_code,
        response.text[:240],
    )
    assert calls_after == 0, ("被拒的轮次不得调用模型", calls_after)
    assert not _started(turns), ("被拒的轮次不得开始执行", turns)
    assert not first_row.get("turn_id"), ("好附件不得被半绑定到被拒的轮次", first_row.get("turn_id"))
    assert not second_row.get("turn_id"), second_row.get("turn_id")
    assert str(second_row.get("state")) == "missing", second_row.get("state")
    print("[诊断] 多附件源缺失：HTTP=%d；调用=%d；台账=%s"
          % (response.status_code, calls_after, turns))


# ---- 4. 源文件变成不可用对象（目录）→ 结构化拒绝 -----------------------------------


async def test_unusable_source_is_rejected(app, provider, tmp_path: Path):
    """旧用例 test_unreadable_copy_is_rejected 的 R9 等价：
    旧"副本不可读"注入缝（已随副本策略删除）→ 新"源文件已不是可读文件"（目录）。
    """
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["我不该在这一轮被调用。"]}])
        calls_before = await _calls(provider)
        src = _write_source(tmp_path, "r7-directory.txt")
        attachment_id = await _register(client, src)
        src.unlink()
        src.mkdir()  # 同一路径现在是目录：不是可读文件
        row = await _row(client, attachment_id)
        assert str(row.get("state")) in ("failed", "missing"), ("装置前置：源文件已不可用", row)

        message = "源文件已不可用"
        response = await client.post(
            "/api/turns", json={"message": message, "attachment_ids": [attachment_id]}
        )
        await asyncio.sleep(0.3)
        calls_after = (await _calls(provider)) - calls_before
        turns = _turns_for(app, message)

    assert response.status_code == 409, (
        "源文件已不是可读文件必须结构化拒绝整轮",
        response.status_code,
        response.text[:240],
    )
    assert calls_after == 0, ("不可用源文件不得开始执行", calls_after)
    assert not _started(turns), turns
    print("[诊断] 源不可用：HTTP=%d；调用=%d；附件=%s"
          % (response.status_code, calls_after, row.get("state")))


# ---- 5. 绿守卫：显式空列表 = 不带附件；正常引用发送通过 -----------------------------


async def test_explicit_empty_list_means_no_attachments(app, provider, tmp_path: Path):
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["这一轮没有附件，正常回答。"]}])
        calls_before = await _calls(provider)
        response = await client.post("/api/turns", json={"message": "纯文字这一轮", "attachment_ids": []})
        assert response.status_code == 200, (response.status_code, response.text[:200])
        calls_after = await _wait_calls(provider, calls_before)
    assert calls_after > calls_before, "显式空列表必须照常执行"
    print("[诊断] 显式空列表：HTTP=200 且照常执行")


async def test_reference_send_still_works(app, provider, tmp_path: Path):
    """正常引用发送：源文件不复制、内容可直接读取（契约 §6.2/§6.4）。"""
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["附件内容我看到了。"]}])
        calls_before = await _calls(provider)
        src = _write_source(tmp_path, "r7-normal.txt")
        attachment_id = await _register(client, src)
        row = await _row(client, attachment_id)
        assert str(row.get("state")) == "ready", row
        assert row.get("stored_path") in (None, ""), ("新语义不再产生本地副本", row)
        assert str(row.get("source_path") or ""), ("必须引用源文件路径", row)

        message = "正常引用发送"
        response = await client.post(
            "/api/turns", json={"message": message, "attachment_ids": [attachment_id]}
        )
        assert response.status_code == 200, (response.status_code, response.text[:240])
        calls_after = await _wait_calls(provider, calls_before)
        content = await client.get("/api/attachments/%s/content" % attachment_id)
        row_after = await _row(client, attachment_id)
        turns = _turns_for(app, message)

    assert calls_after > calls_before, "正常引用发送必须执行"
    assert MARKER in content.text, ("读取必须走源文件（不复制）", content.text[:80])
    assert str(row_after.get("turn_id") or ""), ("正常发送后附件归属本轮", row_after.get("turn_id"))
    assert [t for t in turns if t["status"] in ("completed", "running", "done")], turns
    print("[诊断] 正常引用发送：HTTP=200；调用=%d；台账=%s" % (calls_after, turns))