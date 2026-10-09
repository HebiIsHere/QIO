"""ACC-E2：兼容兜底（旧客户端不带 attachment_ids）必须保住**失败事实**，不得静默少带。

回归背景（2026-10-09，r8 硬性回归在组合/负载下变红）：
旧实现把「进入时就能带」当作**快照过滤器**（services/attachments.py 兼容分支），
于是「登记后刚失败」的草稿在重新枚举时被当成历史记录排除 —— 本轮照发：
HTTP 200 / accepted / 只绑定好附件 / rejected=[] / 模型被调用 1 次。实测触发条件：
bad 附件的首次准备在 POST /api/turns 枚举之前就已经落成 failed（线程池热起来时极易发生）。

修复（产品口径）：
* 进入时本话题至少有一条**能带**的草稿 → 这一轮确实要带附件：集合 = 进入时本话题
  **全部未绑定草稿**（含进入时就已经 failed / missing / 不可读的）→ 任一条不合格整轮拒绝；
* 一条能带的都没有 → 这一轮就是纯文字发送：历史失败不得阻断（契约 §1.3 集合政策）。

本文件覆盖：
1. 失败事实**先落地**再发 turn → 整轮拒绝、0 次模型调用、无半绑定；
2. 失败在 **prepare 等待中**落地 → 整轮拒绝；
3. 缺失（副本被删）的兄弟草稿同样整轮拒绝；
4. 只剩历史失败记录（没有能带的草稿）→ 纯文字照常发送（不被阻断）；
5. 删掉失败草稿后重试 → 好附件正常绑定并执行（草稿可重试）；
6. 边界：已归属**旧轮次**的历史失败不在「未绑定草稿」集合里 → 不阻断本轮；
7. 显式 attachment_ids: [] 不受失败草稿影响（绿守卫）。

运行：cd backend; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/test_acc_e2_compat_failure_fact.py -q
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
MARKER = "ACC-E2 兼容路径失败事实标记"
TERMINAL = ("ready", "failed", "changed", "missing", "cancelled")


def _load_provider_module():
    path = REPO_ROOT / "scripts" / "verify_stream_provider.py"
    assert path.exists(), f"验证资产缺失：{path}"
    spec = importlib.util.spec_from_file_location("verify_stream_provider_e2c", path)
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
    conn = connect(tmp_path / "acc_e2.db")
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
            "secret": "sk-e2-fake-0001",
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


async def _wait_calls(provider, *, at_least: int, timeout: float = 60.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = await _calls(provider)
        if current >= at_least:
            return current
        await asyncio.sleep(0.05)
    return await _calls(provider)


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
        await asyncio.sleep(0.02)
    return last


async def _attachment(client, attachment_id: str) -> dict:
    return (await client.get("/api/attachments/%s" % attachment_id)).json().get("attachment") or {}


def _write_source(tmp_path: Path, name: str) -> Path:
    src = tmp_path / name
    src.write_text(MARKER + "\n", encoding="utf-8")
    return src


@contextlib.contextmanager
def _fail_prepare_copy(mode: str):
    """首次准备的提交步骤（os.replace）必然失败（受控 ENOSPC）。"""
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
def _gated_fail_prepare_copy(gate: threading.Event):
    """提交步骤挂起到 release，**放行后以真实失败收尾**（构造「等待期间失败」）。"""
    import errno

    real_replace = attachments_mod.os.replace
    state = {"fired": False}

    def _boom(src, dst):  # noqa: ANN001
        gate.wait()
        state["fired"] = True
        raise OSError(errno.ENOSPC, "No space left on device（受控错误：等待期间失败）")

    attachments_mod.os.replace = _boom
    try:
        yield state
    finally:
        attachments_mod.os.replace = real_replace


def _bound_ids(response) -> list[str]:
    body = response.json() if hasattr(response, "json") else {}
    return [str(x) for x in (body.get("bound_attachment_ids") or [])]


def _rejected_ids(response) -> list[str]:
    body = response.json() if hasattr(response, "json") else {}
    detail = body.get("detail")
    if isinstance(detail, dict):
        return [str(item.get("id")) for item in (detail.get("rejected") or [])]
    return [str(item.get("id")) for item in (body.get("rejected") or [])]


# ---- 1. 失败事实先落地，再发 turn → 整轮拒绝 ----------------------------------------


async def test_failure_fact_landed_before_send_rejects_whole_turn(app, provider, tmp_path: Path):
    """确定性复现 r8 负载红的触发条件：bad 附件进快照前**已经是 failed**。

    旧实现：bad 被 _entry_carriable 过滤掉 → 200 / 只绑好附件 / rejected=[] / 模型 1 次。
    修复后：集合 = 进入时全部未绑定草稿 → 整轮 409。
    """
    message = "E2 失败事实先落地"
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["我不该在这一轮被调用。"]}])
        calls_before = await _calls(provider)

        ok_src = _write_source(tmp_path, "e2-ok.txt")
        ok_registered = await client.post("/api/attachments", json={"source_path": str(ok_src)})
        ok_id = str(ok_registered.json()["attachment"]["id"])
        ok_row = await _wait_state(client, ok_id, ("ready",) + TERMINAL)
        assert str(ok_row.get("state")) == "ready", ok_row

        with _fail_prepare_copy("landed-first") as injected:
            bad_src = _write_source(tmp_path, "e2-bad.txt")
            bad_registered = await client.post("/api/attachments", json={"source_path": str(bad_src)})
            bad_id = str(bad_registered.json()["attachment"]["id"])
            # 钉死触发条件：等 bad 的失败事实**先落地**（旧实现从这里丢掉事实）
            bad_row = await _wait_state(client, bad_id, ("failed",) + TERMINAL)
            assert str(bad_row.get("state")) == "failed", bad_row

            response = await client.post("/api/turns", json={"message": message})
            calls_during = (await _calls(provider)) - calls_before
            turns = _turns_for(app, message)
            ok_after = await _attachment(client, ok_id)
            bad_after = await _attachment(client, bad_id)

    assert injected["fired"], "复制失败注入没有触发（装置失效）"
    assert response.status_code >= 400, (
        "失败事实已经落地，兼容路径仍 200 accepted（失败事实在枚举时消失）",
        response.status_code,
        response.text[:240],
    )
    assert _rejected_ids(response) == [bad_id], ("必须如实报出失败的是哪一条", response.text[:240])
    assert calls_during == 0, ("被拒的轮次不得调用模型", calls_during)
    assert not [t for t in turns if t["status"] in ("running", "completed", "done")], turns
    turn_ids = {t["turn_id"] for t in turns}
    assert str(ok_after.get("turn_id") or "") not in turn_ids, ("好附件被半绑定到被拒的轮次上", ok_after.get("turn_id"))
    assert str(bad_after.get("turn_id") or "") not in turn_ids, bad_after.get("turn_id")
    assert str(bad_after.get("state")) == "failed", bad_after
    assert str(ok_after.get("state")) == "ready" and not ok_after.get("turn_id"), "好附件必须还能重发"
    print("[诊断] E2-1 失败事实先落地：HTTP=%d；调用=%d；rejected=%s；好附件=%s"
          % (response.status_code, calls_during, _rejected_ids(response), ok_after.get("state")))


# ---- 2. 失败在 prepare 等待中落地 → 整轮拒绝 ----------------------------------------


async def test_failure_landing_during_prepare_wait_rejects_whole_turn(app, provider, tmp_path: Path):
    message = "E2 等待期间失败"
    gate = threading.Event()
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["我不该在这一轮被调用。"]}])
        calls_before = await _calls(provider)
        ok_src = _write_source(tmp_path, "e2-wait-ok.txt")
        ok_registered = await client.post("/api/attachments", json={"source_path": str(ok_src)})
        ok_id = str(ok_registered.json()["attachment"]["id"])
        ok_row = await _wait_state(client, ok_id, ("ready",) + TERMINAL)
        assert str(ok_row.get("state")) == "ready", ok_row

        with _gated_fail_prepare_copy(gate) as injected:
            bad_src = _write_source(tmp_path, "e2-wait-bad.txt")
            bad_registered = await client.post("/api/attachments", json={"source_path": str(bad_src)})
            bad_id = str(bad_registered.json()["attachment"]["id"])
            send = asyncio.create_task(client.post("/api/turns", json={"message": message}))
            await asyncio.sleep(0.5)   # 兼容路径进入有界等待（bad 仍是 prepared）
            assert not send.done(), "首次准备未结束，兼容路径不得先受理"
            gate.set()                 # 放行提交 → 以真实 failed 收尾
            response = await asyncio.wait_for(send, timeout=120)
            calls_during = (await _calls(provider)) - calls_before
            turns = _turns_for(app, message)
            ok_after = await _attachment(client, ok_id)
            bad_after = await _attachment(client, bad_id)

    assert injected["fired"], "复制失败注入没有触发（装置失效）"
    assert response.status_code >= 400, (response.status_code, response.text[:240])
    assert calls_during == 0, ("被拒的轮次不得调用模型", calls_during)
    assert not [t for t in turns if t["status"] in ("running", "completed", "done")], turns
    assert str(bad_after.get("state")) == "failed", bad_after
    assert str(ok_after.get("turn_id") or "") == "", ("整轮拒绝不得半绑好附件", ok_after.get("turn_id"))
    print("[诊断] E2-2 等待期间失败：HTTP=%d；调用=%d；台账=%s" % (response.status_code, calls_during, turns))


# ---- 3. 兄弟草稿缺失（副本被删）→ 整轮拒绝 ------------------------------------------


async def test_missing_draft_sibling_rejects_whole_turn(app, provider, tmp_path: Path):
    message = "E2 兄弟草稿缺失"
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["我不该在这一轮被调用。"]}])
        calls_before = await _calls(provider)
        ok_src = _write_source(tmp_path, "e2-miss-ok.txt")
        ok_id = str((await client.post("/api/attachments", json={"source_path": str(ok_src)})).json()["attachment"]["id"])
        assert str((await _wait_state(client, ok_id, ("ready",) + TERMINAL)).get("state")) == "ready"

        gone_src = _write_source(tmp_path, "e2-miss-gone.txt")
        gone_id = str((await client.post("/api/attachments", json={"source_path": str(gone_src)})).json()["attachment"]["id"])
        gone_row = await _wait_state(client, gone_id, ("ready",) + TERMINAL)
        assert str(gone_row.get("state")) == "ready", gone_row
        Path(str(gone_row["stored_path"])).unlink()   # 副本被删 → 当下事实 missing

        response = await client.post("/api/turns", json={"message": message})
        calls_during = (await _calls(provider)) - calls_before
        ok_after = await _attachment(client, ok_id)

    assert response.status_code >= 400, (response.status_code, response.text[:240])
    assert calls_during == 0, calls_during
    assert str(ok_after.get("turn_id") or "") == "", "整轮拒绝不得半绑"
    print("[诊断] E2-3 兄弟草稿缺失：HTTP=%d；rejected=%s" % (response.status_code, _rejected_ids(response)))


# ---- 4. 只剩历史失败记录 → 纯文字照常发送（不被阻断）-------------------------------


async def test_historical_failure_does_not_block_pure_text_send(app, provider, tmp_path: Path):
    """契约 §1.3 集合政策：进入时**没有任何可携带草稿**时，历史失败不得阻断纯文字发送。"""
    message = "E2 历史失败后纯文字"
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["纯文字回答。"]}])
        calls_before = await _calls(provider)
        with _fail_prepare_copy("history-only") as injected:
            bad_src = _write_source(tmp_path, "e2-history-bad.txt")
            bad_id = str((await client.post("/api/attachments", json={"source_path": str(bad_src)})).json()["attachment"]["id"])
            assert str((await _wait_state(client, bad_id, ("failed",) + TERMINAL)).get("state")) == "failed"

        response = await client.post("/api/turns", json={"message": message})
        calls_after = await _wait_calls(provider, at_least=calls_before + 1)
        bad_after = await _attachment(client, bad_id)

    assert injected["fired"]
    assert response.status_code == 200, ("历史失败不得阻断纯文字发送", response.status_code, response.text[:240])
    assert _bound_ids(response) == [], "这一轮不带附件"
    assert calls_after > calls_before, "纯文字发送必须照常执行"
    assert str(bad_after.get("state")) == "failed" and not bad_after.get("turn_id"), bad_after
    print("[诊断] E2-4 历史失败后纯文字：HTTP=%d；调用=%d" % (response.status_code, calls_after - calls_before))


# ---- 5. 删掉失败草稿后重试 → 好附件正常绑定并执行（草稿可重试）---------------------


async def test_after_removing_failed_draft_retry_binds_healthy_attachment(app, provider, tmp_path: Path):
    message = "E2 删除失败草稿后重试"
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["重试成功。"]}])
        calls_before = await _calls(provider)
        ok_src = _write_source(tmp_path, "e2-retry-ok.txt")
        ok_id = str((await client.post("/api/attachments", json={"source_path": str(ok_src)})).json()["attachment"]["id"])
        assert str((await _wait_state(client, ok_id, ("ready",) + TERMINAL)).get("state")) == "ready"

        with _fail_prepare_copy("retry-after-delete") as injected:
            bad_src = _write_source(tmp_path, "e2-retry-bad.txt")
            bad_id = str((await client.post("/api/attachments", json={"source_path": str(bad_src)})).json()["attachment"]["id"])
            assert str((await _wait_state(client, bad_id, ("failed",) + TERMINAL)).get("state")) == "failed"
            first = await client.post("/api/turns", json={"message": "E2 先被拒一次"})
            assert first.status_code >= 400, (first.status_code, first.text[:200])

        deleted = await client.delete("/api/attachments/%s" % bad_id)
        assert deleted.status_code in (200, 204), deleted.text[:200]

        response = await client.post("/api/turns", json={"message": message})
        calls_after = await _wait_calls(provider, at_least=calls_before + 1)
        ok_after = await _attachment(client, ok_id)

    assert injected["fired"]
    assert response.status_code == 200, (response.status_code, response.text[:240])
    assert _bound_ids(response) == [ok_id], (response.text[:240], _bound_ids(response))
    assert calls_after > calls_before, "重试必须真的执行"
    assert str(ok_after.get("turn_id") or "") != "", "好附件必须绑到这一轮"
    print("[诊断] E2-5 删掉失败草稿后重试：HTTP=%d；绑定=%s；调用=%d"
          % (response.status_code, _bound_ids(response), calls_after - calls_before))


# ---- 6. 边界：旧轮次的历史失败记录不属于本次未绑定集合 ------------------------------


async def test_historical_failure_bound_to_old_turn_does_not_block_current_draft(app, provider, tmp_path: Path):
    """边界（集合定义）：兼容兜底的集合 = 进入本次请求时本话题**尚未绑定任何轮次**的附件。

    已归属旧轮次的终态失败是**历史记录**，不在集合里：本轮只按能带的那条发送，
    不被它阻断（契约 §1.3 第一句 + 产品规则 7）。
    """
    message = "E2 旧轮次历史失败不阻断"
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["只带能带的那条。"]}])
        calls_before = await _calls(provider)

        hist_src = _write_source(tmp_path, "e2-hist-failed.txt")
        hist_id = str((await client.post("/api/attachments", json={"source_path": str(hist_src)})).json()["attachment"]["id"])
        hist_row = await _wait_state(client, hist_id, ("ready",) + TERMINAL)
        assert str(hist_row.get("state")) == "ready", hist_row
        svc = app.state.ctx.attachments
        # 先归属旧轮次（进入历史），再把副本清掉并落成终态失败
        await svc.bind_for_turn(turn_id="turn_old", attachment_ids=[hist_id])
        Path(str(hist_row["stored_path"])).unlink()
        svc._update(hist_id, state="failed", error="历史失败（旧轮次，不在本次待发送集合）")

        ok_src = _write_source(tmp_path, "e2-hist-ok.txt")
        ok_id = str((await client.post("/api/attachments", json={"source_path": str(ok_src)})).json()["attachment"]["id"])
        assert str((await _wait_state(client, ok_id, ("ready",) + TERMINAL)).get("state")) == "ready"

        response = await client.post("/api/turns", json={"message": message})
        calls_after = await _wait_calls(provider, at_least=calls_before + 1)
        hist_after = await _attachment(client, hist_id)

    assert response.status_code == 200, ("旧轮次的历史失败不得阻断本轮", response.status_code, response.text[:240])
    assert _bound_ids(response) == [ok_id], (response.text[:240], _bound_ids(response))
    assert calls_after > calls_before, "本轮必须照常执行"
    assert str(hist_after.get("turn_id") or "") == "turn_old", "历史记录的归属不得被动过"
    print("[诊断] E2-7 旧轮次历史失败：HTTP=%d；绑定=%s" % (response.status_code, _bound_ids(response)))


# ---- 7. 绿守卫：显式空列表不受失败草稿影响 ------------------------------------------


async def test_explicit_empty_list_unaffected_by_failed_draft(app, provider, tmp_path: Path):
    message = "E2 显式空列表"
    async with _client(app) as client:
        await _prepare_credential(client, provider)
        await _script(provider, [{"chunks": ["显式空列表照常回答。"]}])
        calls_before = await _calls(provider)
        with _fail_prepare_copy("explicit-empty") as injected:
            bad_src = _write_source(tmp_path, "e2-empty-bad.txt")
            bad_id = str((await client.post("/api/attachments", json={"source_path": str(bad_src)})).json()["attachment"]["id"])
            assert str((await _wait_state(client, bad_id, ("failed",) + TERMINAL)).get("state")) == "failed"

        response = await client.post("/api/turns", json={"message": message, "attachment_ids": []})
        calls_after = await _wait_calls(provider, at_least=calls_before + 1)

    assert injected["fired"]
    assert response.status_code == 200, (response.status_code, response.text[:200])
    assert _bound_ids(response) == []
    assert calls_after > calls_before, "显式空列表必须照常执行"
    print("[诊断] E2-6 显式空列表：HTTP=200；调用=%d" % (calls_after - calls_before))
