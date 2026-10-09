"""F 独立验证（阶段一 · F06）：SSE 无结束标记直接 EOF 不得算「正常完成」。

契约：docs/plans/2026-10-09-process-attachment-audit-consolidation.md §C2 ——
「结束语义由 provider 协议判定（OpenAI 兼容：finish_reason）…EOF 无终止标记 =
**不完整结束**，不是完成」「不输出「正常完成」状态」。

链路（真实跨层，真 SSE）：本机假厂商端点（scripts/verify_stream_provider.py，真 HTTP + 真
SSE，abort_after 软结束 = 干净关闭但缺 finish_reason）→ NativeAdapter → AgentLoop →
TurnManager → TURN_END 事件。

基线：软 EOF 后 adapter 给出 finish_reason=None 的 Completion，loop 按「没有工具调用 →
正文即回答」收尾，TurnManager 落 status=completed、reason_code=none（正常完成）。

冻结契约裁定（Lead 2026-10-09，§C2 对齐）：TURN_END 新增终态 incomplete，且只对
reason_code=incomplete_stream 使用；不完整结束必须给出 retry 动作。

断言：
1) 已确认正文必须保留（不得丢字、不得补后缀）—— 契约既有要求；
2) status 不得是 completed（不完整结束 ≠ 正常完成），并必须恰好是 incomplete；
3) reason_code 必须是 incomplete_stream（不得是正常停止 none）；
4) actions 必须含 retry（可恢复操作）。

运行：cd backend; uv run --frozen --extra dev pytest tests/test_acc_f_06_incomplete_stream.py -q
"""

from __future__ import annotations

import importlib.util
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent.api.server import create_app
from agent.credentials.store import MemoryKeyring

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_provider_module():
    path = REPO_ROOT / "scripts" / "verify_stream_provider.py"
    assert path.exists(), "验证资产缺失：%s" % path
    spec = importlib.util.spec_from_file_location("verify_stream_provider_accf", path)
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


def _native_adapter(server):
    from openai import AsyncOpenAI

    from agent.adapters.native import NativeAdapter

    client = AsyncOpenAI(
        base_url="http://127.0.0.1:%d/v1" % server.server_port,
        api_key="sk-accf-fake-0006",
    )
    return NativeAdapter(client=client, model="verify-model")


def _client(app):
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://qio.test", timeout=120.0
    )


def _turn_end(app, turn_id: str) -> dict | None:
    for event in list(app.state.ctx.bus._history):
        if event.type.value == "TURN_END" and str(event.data.get("turn_id")) == turn_id:
            return event.data
    return None


async def _wait_turn_end(app, turn_id: str, timeout: float = 60.0) -> dict | None:
    import asyncio

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        end = _turn_end(app, turn_id)
        if end is not None:
            return end
        await asyncio.sleep(0.05)
    return _turn_end(app, turn_id)


@pytest.fixture()
def app(db_conn, settings):
    application = create_app(settings, db_conn)
    application.state.ctx.credentials._kr = MemoryKeyring()
    return application


async def test_sse_eof_without_finish_reason_is_not_a_normal_completion(app, provider):
    app.state.ctx.build_adapter = AsyncMock(return_value=_native_adapter(provider))
    # 两片正文后「干净收束但内容不完整」：传输层正常结束、缺 finish_reason。
    provider.script.set(
        [{"abort_after": 2, "chunks": ["前两句。", "第二句。", "永远不会发出的后缀"]}]
    )

    async with _client(app) as client:
        resp = await client.post("/api/turns", json={"message": "请回答"})
        assert resp.status_code == 200, resp.text[:200]
        turn_id = str(resp.json()["turn_id"])
        end = await _wait_turn_end(app, turn_id)

    assert end is not None, "TURN_END 没有到达"
    confirmed = "前两句。第二句。"
    final_text = str(end.get("final_content") or "")
    # ① 已确认正文保留（既有契约）
    assert confirmed in final_text, ("无 finish_reason 的 EOF 也必须保留已确认正文", final_text)
    assert "永远不会发出的后缀" not in final_text, ("未确认的后缀不得凭空出现", final_text)
    # ② / ③：不得输出 done / 正常停止原因
    assert end.get("status") != "completed", (
        "EOF 缺 finish_reason = 不完整结束，不得标成正常完成",
        {"status": end.get("status"), "reason_code": end.get("reason_code")},
    )
    assert end.get("reason_code") != "none", (
        "不完整结束必须给出明确结束原因，不得是正常停止 none",
        {"status": end.get("status"), "reason_code": end.get("reason_code"), "reason": end.get("reason")},
    )
    # 冻结契约裁定（Lead 2026-10-09）：不完整结束的终态与原因码是固定值，
    # 且必须给「重试」入口。以下三条是在上面「不得是 completed / none」之上的加严，
    # 不是放宽 —— 上面两条断言原样保留。
    assert end.get("status") == "incomplete", (
        "缺 finish_reason 的 EOF 必须以终态 incomplete 收口",
        {"status": end.get("status"), "reason_code": end.get("reason_code")},
    )
    assert end.get("reason_code") == "incomplete_stream", (
        "不完整结束的 reason_code 必须是 incomplete_stream",
        {"status": end.get("status"), "reason_code": end.get("reason_code")},
    )
    actions = [str(x) for x in end.get("actions") or []]
    assert "retry" in actions, (
        "不完整结束必须给「重试」入口",
        {"actions": end.get("actions"), "status": end.get("status")},
    )
    print(
        "[诊断] F06 不完整流：status=%s reason_code=%s 保留已确认正文=%s"
        % (end.get("status"), end.get("reason_code"), confirmed in final_text)
    )
