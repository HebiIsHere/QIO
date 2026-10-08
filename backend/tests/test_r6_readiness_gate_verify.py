r"""D 独立验证（R6 问题一）：**附件就绪之后才允许执行**（真实路由 + 真实编排 + 假 provider）。

契约：docs/plans/2026-10-08-four-remaining-fixes.md §1.1（冻结）+ §3。
基线 6507c64 缺陷（api/server.py:1566→1571）：turns 路由**先 \`ctx.turns.submit(...)\` 再 await
\`attachments.bind_for_turn(...)\`** → bind 异步化后，等待复制会让出事件循环 → **模型在附件就绪前就启动**。

本文件只用**用户可见规则**判定（不看实现的新口径）：
* 复制/准备还没完成（磁盘闸门仍关闭）时：**模型调用数 == 0、工具执行数 == 0**、该轮不得出现在运行中；
* 准备失败 → 模型**始终** 0 次调用 + 精确原因（明确拒绝，不是「先执行再取消」）；
* 准备期间取消 → 释放闸门后**仍不能**开始执行；
* 就绪之后：模型才会被调用，且新轮拿到的是**内容正确**的副本；
* 同期其它 API 能推进；原轮归属与历史不变。

装置：真实 ASGI 路由（httpx.ASGITransport）+ 真实 TurnManager/TurnOrchestrator + 本机假 SSE provider
（scripts/verify_stream_provider.py，\`/__log\` 计模型调用数）。闸门放在**副本提交步骤**
（\`services/attachments.py\` 的 \`os.replace\`，工作线程内），保证事件循环仍可观测。

运行：cd backend; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/test_r6_readiness_gate_verify.py -q
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import builtins
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
MARKER = "R6 就绪闸门：只有这份副本里才有的标记 9c14"
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


def _client(app):
    import httpx

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://qio.test", timeout=90.0)


@pytest.fixture()
def app(tmp_path: Path, provider):
    conn = connect(tmp_path / "r6_gate.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path / "data"), conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    app.state.ctx.attachments.data_dir = tmp_path / "data"
    app.state.ctx.attachments.max_upload_bytes = 32 * 1024 * 1024
    app.state.provider_port = provider.server_port
    return app


async def _prepare_credential(client, provider) -> dict:
    body = {
        "provider": "custom",
        "endpoint": "http://127.0.0.1:%d/v1" % provider.server_port,
        "secret": "sk-r6-fake-0001",
        "default_model": "verify-model",
        "tags": ["main-loop"],
    }
    resp = await client.post("/api/credentials", json=body)
    assert resp.status_code == 200, (resp.status_code, resp.text[:300])
    payload = resp.json()
    assert (payload.get("verify") or {}).get("ok"), ("凭据没有通过验证（假 provider 没通）", payload)
    return payload


async def _script_provider(provider, steps: list[dict]) -> None:
    import httpx

    base = "http://127.0.0.1:%d" % provider.server_port
    await asyncio.to_thread(httpx.post, base + "/__reset")
    await asyncio.to_thread(
        httpx.post, base + "/__script", json={"steps": steps}, timeout=10
    )


async def _provider_calls(provider) -> int:
    import httpx

    base = "http://127.0.0.1:%d" % provider.server_port
    body = await asyncio.to_thread(lambda: httpx.get(base + "/__log", timeout=10).json())
    return len(body.get("requests") or [])


async def _wait_ready(client, attachment_id: str, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = (await client.get("/api/attachments/%s" % attachment_id)).json().get("attachment") or {}
        if str(last.get("state")) == "ready":
            return last
        if str(last.get("state")) in TERMINAL:
            raise AssertionError("附件没有就绪：%r" % (last,))
        await asyncio.sleep(0.05)
    raise AssertionError("附件没有在 %.0fs 内就绪：%r" % (timeout, last))


async def _register(client, path: Path) -> dict:
    resp = await client.post("/api/attachments", json={"source_path": str(path)})
    assert resp.status_code == 200, (resp.status_code, resp.text[:200])
    attachment_id = str(resp.json()["attachment"]["id"])
    return await _wait_ready(client, attachment_id)


async def _wait_idle(client, timeout: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = (await client.get("/api/turns/queue")).json()
        if not snap.get("running") and not (snap.get("queued") or []):
            return True
        await asyncio.sleep(0.1)
    return False


def _bound_ids(resp) -> list[str]:
    body = resp.json() if hasattr(resp, "json") else resp
    explicit = body.get("bound_attachment_ids")
    if isinstance(explicit, list):
        return [str(x) for x in explicit]
    return [str(a.get("id")) for a in (body.get("attachments") or []) if a.get("id")]


@contextlib.contextmanager
def _gated_clone(
    entered: threading.Event,
    gate: threading.Event,
    *,
    force_copy: bool = True,
    hold_after: int = 1,
):
    """在**副本写入开始处**放线程闸门（工作线程内）；顺带强制走复制路径。

    说明：重试克隆走的是 `_clone_copy_for_retry`（os.link 失败 → shutil.copyfile **直接写目标**，
    没有 .part / os.replace），所以闸门必须放在写入开端（模块级 open seam），而不是提交步骤。
    """
    real_open, real_io_open = builtins.open, io.open
    real_link = os.link
    sm = {"writes": 0}

    def _wrap(real):  # noqa: ANN001, ANN202
        def _patched(file, mode="r", *args, **kwargs):  # noqa: ANN001
            if "w" in str(mode):
                sm["writes"] += 1
                if sm["writes"] >= hold_after:
                    entered.set()
                    gate.wait(timeout=60)  # 磁盘闸门：由测试释放
            return real(file, mode, *args, **kwargs)

        return _patched

    def _no_link(*args, **kwargs):  # noqa: ANN002
        raise OSError(1, "受控错误：强制走复制路径（验证装置）")

    builtins.open, io.open = _wrap(real_open), _wrap(real_io_open)
    attachments_mod.open = _wrap(real_open)
    if force_copy:
        os.link = _no_link
    try:
        yield
    finally:
        builtins.open, io.open = real_open, real_io_open
        with contextlib.suppress(AttributeError):
            del attachments_mod.open
        os.link = real_link
        gate.set()


@contextlib.contextmanager
def _failing_clone():
    """让**克隆的复制退路必然失败**（跨平台确定，打在真实调用点）。

    为什么必须打在 `shutil.copyfile` 上（而不是包 `builtins.open`/`io.open`）：
    Linux 的 `shutil.copyfile` 走 `_fastcopy_sendfile`（`os.sendfile` 直接搬字节），
    包 `builtins.open` 在 Linux 上**不会命中**真正的写路径 —— 会出现「注入自认为触发了、
    但复制其实成功了」的假红（CI run 37727749922：py3.11/py3.12 三条用例 200 + 模型被调用）。
    真实调用点见 services/attachments.py：os.link(...) 失败后 shutil.copyfile(...)。
    """
    import errno as _errno

    real_link = os.link
    real_copyfile = attachments_mod.shutil.copyfile
    state = {"fired": False}

    def _no_link(*args, **kwargs):  # noqa: ANN002
        raise OSError(1, "受控错误：强制走复制路径（验证装置）")

    def _boom(src, dst, *args, **kwargs):  # noqa: ANN001
        state["fired"] = True
        raise OSError(_errno.ENOSPC, "No space left on device（受控错误：复制退路失败）")

    os.link = _no_link
    attachments_mod.shutil.copyfile = _boom
    try:
        yield state
    finally:
        os.link = real_link
        attachments_mod.shutil.copyfile = real_copyfile


async def _make_recoverable_turn(app, attachment_id: str, turn_id: str) -> str:
    """手工造一条「被掐断但可恢复」的台账行，并把 ready 副本绑到它上面（r4 验收件同款装置）。"""
    import inspect as _inspect

    ctx = app.state.ctx
    bound = ctx.attachments.bind_for_turn(turn_id, [attachment_id], topic_id=None)
    if _inspect.isawaitable(bound):
        await bound
    journal = ctx.turn_journal
    journal.accepted(turn_id=turn_id, message="被进程掐断的那一条（带附件）")
    journal.running(turn_id)
    journal.terminal(turn_id, "cancelled", reason="shutdown")
    assert journal.recoverable(turn_id) is not None, "没有造出可恢复记录（装置失效）"
    return turn_id


async def _first_turn_with_attachment(client, provider, attachment_id: str) -> str:
    """把 ready 副本绑到第一轮（真实路由），并等它跑完。"""
    await _script_provider(provider, [{"chunks": ["第一轮回答。"]}])
    resp = await client.post(
        "/api/turns", json={"message": "第一轮带附件", "attachment_ids": [attachment_id], "topic_id": None}
    )
    assert resp.status_code == 200, (resp.status_code, resp.text[:300])
    turn_id = str(resp.json()["turn_id"])
    assert await _wait_idle(client), "第一轮没有跑完"
    return turn_id


# ---- 1. 核心：复制闸门仍关闭时，模型调用数必须为 0 ----------------------------------


async def test_no_model_call_while_clone_is_gated(app, provider, tmp_path: Path):
    src = tmp_path / "r6-gate-source.txt"
    src.write_text(MARKER + "\n", encoding="utf-8")
    entered = threading.Event()
    gate = threading.Event()
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        att = await _register(client, src)
        turn1 = await _first_turn_with_attachment(client, provider, att["id"])
        calls_before = await _provider_calls(provider)
        src.unlink()  # 原文件删除：重试必须靠已保存副本（新轮克隆）

        with _gated_clone(entered, gate):
            retry = asyncio.create_task(
                client.post(
                    "/api/turns",
                    json={
                        "message": "重试（闸门关闭期间不得开始执行）",
                        "attachment_ids": [att["id"]],
                        "retry_of_turn_id": turn1,
                    },
                )
            )
            got = await asyncio.to_thread(entered.wait, 40)
            assert got, "复制没有进入闸门（装置失效：没走到副本提交）"
            still_closed = not gate.is_set()
            calls_while_gated = (await _provider_calls(provider)) - calls_before
            queue = await client.get("/api/turns/queue")
            session = await client.get("/api/session/context")
            rows = (await client.get("/api/attachments?unbound=true")).json().get("attachments", [])
            gate.set()
            response = await asyncio.wait_for(retry, timeout=60)
        # 释放闸门之后的收尾断言也要在 client 关闭前做完
        queue_json = queue.json()
        new_ids = [x for x in _bound_ids(response) if x != att["id"]]
        clone: dict = {}
        content = None
        if new_ids:
            clone = await _wait_ready(client, new_ids[0])
            content = await client.get("/api/attachments/%s/content" % clone["id"])

    assert still_closed, "断言时闸门已被打开 —— 证据不成立（必须在「磁盘闸门仍关闭」时判定）"
    assert queue.status_code == 200 and session.status_code == 200, (
        "闸门关闭期间同期 API 必须仍能推进", queue.status_code, session.status_code
    )
    assert calls_while_gated == 0, (
        "附件还没就绪（复制闸门仍关闭）时**模型已经被调用**了 —— 违反契约 §1.1「就绪后才放行」",
        {
            "calls_while_gated": calls_while_gated,
            "running": (queue_json or {}).get("running"),
            "queued": (queue_json or {}).get("queued"),
            "attachments": [(a.get("id"), a.get("state")) for a in rows],
        },
    )
    assert response.status_code == 200, (response.status_code, response.text[:300])
    assert new_ids, ("新轮没有绑定克隆附件", response.text[:300])
    assert content is not None and content.status_code == 200 and MARKER in content.text, (
        "释放闸门后新轮的副本内容不正确", content.status_code, content.text[:80]
    )
    calls_after = (await _provider_calls(provider)) - calls_before
    print(
        "[诊断] 闸门关闭期间模型调用=%d；释放后累计调用=%d；克隆=%s→%s；内容含标记=%s"
        % (calls_while_gated, calls_after, att["id"], clone["id"], MARKER in content.text)
    )


# ---- 2. 复制失败：模型始终 0 次调用 + 精确原因 ---------------------------------------


async def test_clone_failure_never_starts_the_model(app, provider, tmp_path: Path):
    src = tmp_path / "r6-fail-source.txt"
    src.write_text(MARKER + "\n", encoding="utf-8")
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        att = await _register(client, src)
        turn1 = await _first_turn_with_attachment(client, provider, att["id"])
        calls_before = await _provider_calls(provider)
        src.unlink()

        with _failing_clone() as injected:
            response = await client.post(
                "/api/turns",
                json={
                    "message": "重试（复制会失败）",
                    "attachment_ids": [att["id"]],
                    "retry_of_turn_id": turn1,
                },
            )
        await asyncio.sleep(0.5)
        calls_after = (await _provider_calls(provider)) - calls_before
        queue = (await client.get("/api/turns/queue")).json()
        rows = (await client.get("/api/attachments?unbound=true")).json().get("attachments", [])

    assert injected["fired"], "受控写入错误没有触发（装置失效）"
    assert calls_after == 0, (
        "复制失败却仍然调用了模型 —— 违反契约 §1.1「任一必需附件失败即明确拒绝，不先执行再取消」",
        {"calls": calls_after, "status": response.status_code, "body": response.text[:200], "queue": queue},
    )
    assert response.status_code >= 400, (
        "复制失败必须给结构化拒绝（HTTP 错误）", response.status_code, response.text[:200]
    )
    print("[诊断] 复制失败：模型调用=%d；HTTP=%d；原因=%s" % (calls_after, response.status_code, response.text[:120]))


# ---- 3. 准备期间取消：释放闸门后仍不能开始执行 --------------------------------------


async def test_cancel_during_preparation_never_starts_execution(app, provider, tmp_path: Path):
    src = tmp_path / "r6-cancel-source.txt"
    src.write_text(MARKER + "\n", encoding="utf-8")
    entered = threading.Event()
    gate = threading.Event()
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        att = await _register(client, src)
        turn1 = await _first_turn_with_attachment(client, provider, att["id"])
        calls_before = await _provider_calls(provider)
        src.unlink()

        with _gated_clone(entered, gate):
            retry = asyncio.create_task(
                client.post(
                    "/api/turns",
                    json={
                        "message": "重试（准备期间取消）",
                        "attachment_ids": [att["id"]],
                        "retry_of_turn_id": turn1,
                    },
                )
            )
            assert await asyncio.to_thread(entered.wait, 40), "复制没有进入闸门（装置失效）"
            retry.cancel()  # 客户端取消这一条请求
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await retry
            gate.set()  # 释放磁盘闸门：取消之后也**不得**重新开始执行
            await asyncio.sleep(1.0)
        calls_after = (await _provider_calls(provider)) - calls_before
        queue = (await client.get("/api/turns/queue")).json()

    assert calls_after == 0, (
        "准备期间取消、释放磁盘闸门之后仍然开始执行了 —— 违反契约 §1.1「取消 = 中止该请求」",
        {"calls": calls_after, "queue": queue},
    )
    print("[诊断] 取消后：模型调用=%d；queue=%s" % (calls_after, queue))


# ---- 4. 原轮归属与历史副本保持正确 --------------------------------------------------


async def test_source_turn_attribution_stays_correct(app, provider, tmp_path: Path):
    src = tmp_path / "r6-attr-source.txt"
    src.write_text(MARKER + "\n", encoding="utf-8")
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        att = await _register(client, src)
        turn1 = await _first_turn_with_attachment(client, provider, att["id"])
        context = (await client.get("/api/session/context")).json()
        messages = context.get("messages") or []
        bound = [a for m in messages for a in (m.get("attachments") or [])]
        assert any(str(a.get("id")) == att["id"] for a in bound), (
            "原轮历史里没有这份附件", [(m.get("role"), [a.get("id") for a in (m.get('attachments') or [])]) for m in messages][-3:]
        )
        src.unlink()
        ok = await client.get("/api/attachments/%s/content" % att["id"])
        assert ok.status_code == 200 and MARKER in ok.text, (
            "原轮历史副本在原文件删除后仍必须可读", ok.status_code, ok.text[:60]
        )
        print("[诊断] 原轮归属：turn=%s；副本仍可读=%s" % (turn1, ok.status_code))


# ---- 5. 多附件：**最后一个**还没就绪时不得开始执行 -----------------------------------


async def test_multi_attachment_last_not_ready_blocks_start(app, provider, tmp_path: Path):
    """两个附件都要克隆：第 2 个（最后一个）仍在复制时，模型调用必须仍为 0。"""
    src1 = tmp_path / "r6-multi-1.txt"
    src2 = tmp_path / "r6-multi-2.txt"
    src1.write_text(MARKER + "\n", encoding="utf-8")
    src2.write_text(MARKER + " second\n", encoding="utf-8")
    entered = threading.Event()
    gate = threading.Event()
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        att1 = await _register(client, src1)
        att2 = await _register(client, src2)
        await _script_provider(provider, [{"chunks": ["第一轮回答。"]}])
        first = await client.post(
            "/api/turns", json={"message": "第一轮带两个附件", "attachment_ids": [att1["id"], att2["id"]]}
        )
        assert first.status_code == 200, (first.status_code, first.text[:300])
        turn1 = str(first.json()["turn_id"])
        assert await _wait_idle(client), "第一轮没有跑完"
        calls_before = await _provider_calls(provider)
        src1.unlink()
        src2.unlink()

        with _gated_clone(entered, gate, hold_after=2):  # 第 2 个克隆才暂停
            retry = asyncio.create_task(
                client.post(
                    "/api/turns",
                    json={
                        "message": "重试（最后一个附件还没就绪）",
                        "attachment_ids": [att1["id"], att2["id"]],
                        "retry_of_turn_id": turn1,
                    },
                )
            )
            assert await asyncio.to_thread(entered.wait, 45), "第 2 个克隆没有进入闸门（装置失效）"
            still_closed = not gate.is_set()
            calls_while_gated = (await _provider_calls(provider)) - calls_before
            queue = await client.get("/api/turns/queue")
            gate.set()
            response = await asyncio.wait_for(retry, timeout=120)
        queue_json = queue.json()
        new_ids = [x for x in _bound_ids(response) if x not in (att1["id"], att2["id"])]
        contents = []
        for new_id in new_ids:
            await _wait_ready(client, new_id)
            body = await client.get("/api/attachments/%s/content" % new_id)
            contents.append(body.status_code == 200 and MARKER in body.text)

    assert still_closed, "断言时闸门已被打开 —— 证据不成立"
    assert calls_while_gated == 0, (
        "多附件里**最后一个还没就绪**时模型已经被调用了",
        {"calls_while_gated": calls_while_gated, "queue": queue_json, "new_ids": new_ids},
    )
    assert response.status_code == 200, (response.status_code, response.text[:300])
    assert len(new_ids) == 2 and all(contents), ("两个克隆都必须就绪且内容正确", new_ids, contents)
    print("[诊断] 多附件：闸门关闭期间调用=%d；释放后克隆=%s；内容校验=%s" % (calls_while_gated, new_ids, contents))


# ---- 6. 排队期间准备失败：不启动、不留孤儿队列项 -------------------------------------


async def test_prepare_failure_while_queued_starts_nothing(app, provider, tmp_path: Path):
    src = tmp_path / "r6-queued.txt"
    src.write_text(MARKER + "\n", encoding="utf-8")
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        att = await _register(client, src)
        source_turn = await _make_recoverable_turn(app, att["id"], "turn_r6_queued_source")
        src.unlink()
        # 另起一轮故意跑得慢，占住 worker（排队压力）
        await _script_provider(provider, [{"chunks": ["慢慢来。", "继续。"], "chunk_delay_ms": 3000}])
        first = await client.post("/api/turns", json={"message": "慢的占用轮", "attachment_ids": []})
        assert first.status_code == 200
        deadline = time.monotonic() + 30
        calls_first = 0
        while time.monotonic() < deadline:
            calls_first = await _provider_calls(provider)
            if calls_first >= 1:
                break
            await asyncio.sleep(0.1)
        assert calls_first >= 1, "第一轮没有启动（装置失效）"
        with _failing_clone() as injected:
            second = await client.post("/api/turns/%s/resend" % source_turn)
        calls_after_failure = await _provider_calls(provider)
        queue = (await client.get("/api/turns/queue")).json()
        queued_ids = [q.get("turn_id") for q in (queue.get("queued") or [])]
        # 被拒绝的那一轮**没有被接受**（没有 turn_id），且来源轮的 claim 没有被消耗
        rejected_body = second.json() if second.headers.get("content-type", "").startswith("application/json") else {}
        accepted_turn_id = str((rejected_body or {}).get("turn_id") or "")
        claim_intact = app.state.ctx.turn_journal.recoverable(source_turn) is not None
        await _wait_idle(client, timeout=120)

    assert injected["fired"], "受控写入错误没有触发（装置失效）"
    assert second.status_code >= 400, ("准备失败必须明确拒绝", second.status_code, second.text[:200])
    assert "attachment_binding_failed" in second.text, (
        "拒绝原因必须是附件准备失败（结构化）", second.text[:200]
    )
    assert not accepted_turn_id, ("准备失败却仍然被接受了（会去执行）", accepted_turn_id, second.text[:160])
    assert claim_intact, "准备失败把来源轮的 claim 消耗掉了（无法再次恢复）"
    assert calls_first >= 1 and calls_after_failure >= calls_first, (
        "占用轮的调用应当照常推进（诊断字段）", calls_first, calls_after_failure
    )
    assert str((second.json() or {}).get("turn_id") or "") not in queued_ids, (
        "被拒绝的那一轮留下了孤儿队列项", queued_ids
    )
    assert source_turn not in queued_ids, ("被拒绝的 resend 留下了孤儿队列项", queued_ids)
    print("[诊断] 排队期间准备失败：HTTP=%d；模型调用 %d→%d；queued=%s" % (
        second.status_code, calls_first, calls_after_failure, queued_ids))


# ---- 7. resend 准备失败后**再次恢复**（claim 不能被永久消耗） --------------------------


async def test_resend_prepare_failure_then_recovery(app, provider, tmp_path: Path):
    src = tmp_path / "r6-resend.txt"
    src.write_text(MARKER + "\n", encoding="utf-8")
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        att = await _register(client, src)
        turn1 = await _make_recoverable_turn(app, att["id"], "turn_r6_resend_source")
        src.unlink()
        calls_before = await _provider_calls(provider)

        with _failing_clone() as injected:
            first_try = await client.post("/api/turns/%s/resend" % turn1)
        assert injected["fired"], "受控写入错误没有触发（装置失效）"
        assert first_try.status_code >= 400, (
            "resend 准备失败必须明确拒绝", first_try.status_code, first_try.text[:200]
        )
        calls_after_failure = await _provider_calls(provider)
        assert calls_after_failure == calls_before, (
            "resend 准备失败却调用了模型", {"before": calls_before, "after": calls_after_failure}
        )

        second_try = await client.post("/api/turns/%s/resend" % turn1)
        assert second_try.status_code == 200, (
            "resend 第一次准备失败后 claim 被永久消耗（再次恢复失败）",
            second_try.status_code, second_try.text[:300],
        )
        new_turn = str((second_try.json() or {}).get("turn_id") or "")
        assert new_turn and new_turn != turn1, second_try.text[:200]
        assert await _wait_idle(client), "恢复的 resend 没有跑完"
        calls_after_recovery = await _provider_calls(provider)
        assert calls_after_recovery > calls_after_failure, ("恢复后的 resend 必须真的执行一次", calls_after_recovery)
        print("[诊断] resend 恢复：首次=%d；再次=%d；新轮=%s；调用 %d→%d" % (
            first_try.status_code, second_try.status_code, new_turn, calls_after_failure, calls_after_recovery))


# ---- 8. 服务关闭发生在准备期间：释放闸门后仍不能开始执行 ------------------------------


async def test_shutdown_during_preparation_never_starts_execution(app, provider, tmp_path: Path):
    src = tmp_path / "r6-shutdown.txt"
    src.write_text(MARKER + "\n", encoding="utf-8")
    entered = threading.Event()
    gate = threading.Event()
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        att = await _register(client, src)
        turn1 = await _first_turn_with_attachment(client, provider, att["id"])
        calls_before = await _provider_calls(provider)
        src.unlink()

        with _gated_clone(entered, gate):
            retry = asyncio.create_task(
                client.post(
                    "/api/turns",
                    json={
                        "message": "重试（准备期间服务关闭）",
                        "attachment_ids": [att["id"]],
                        "retry_of_turn_id": turn1,
                    },
                )
            )
            assert await asyncio.to_thread(entered.wait, 45), "复制没有进入闸门（装置失效）"
            await app.state.ctx.turns.shutdown()  # 服务关闭
            gate.set()  # 关闭之后释放磁盘闸门：不得因此开始执行
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(retry, timeout=60)
            await asyncio.sleep(1.0)
        calls_after = (await _provider_calls(provider)) - calls_before
        queue = (await client.get("/api/turns/queue")).json()

    assert calls_after == 0, (
        "服务关闭（准备期间）后释放磁盘闸门仍然开始执行了", {"calls": calls_after, "queue": queue}
    )
    print("[诊断] 准备期服务关闭：模型调用=%d；queue=%s" % (calls_after, queue))

