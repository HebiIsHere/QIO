"""B 独立验证：正式回答流式事件接入统一脱敏（审计 F07 / 冻结契约 C3）。

**反例（基线应为红）**：把一个合成敏感值登记进 redact 登记表后，独立脱敏器能移除它，
但流式 ASSISTANT 事件与真实 SSE 序列化仍然包含原值。

覆盖：
* 敏感值完整出现、跨多个分块位置切开、逐字符累计快照；
* 末尾不足缓冲长度的尾部、非敏感相似前缀、异常结束；
* 每个实际事件 + 线上的 SSE（sse_format）+ 最终 / 历史输出均无原值。

测试日志只输出通过状态，绝不打印敏感值本身；不使用任何真实密钥。

运行：cd backend; uv run --frozen pytest tests/test_acc_b_redact_stream.py -q
"""

from __future__ import annotations

import contextlib
import json
import threading

import pytest

from agent.api.bus import EventBus
from agent.api.events import sse_format
from agent.core.loop import AgentLoop
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry

from _acc_b_provider import AccSseProvider

DECL = "[[QIO:ANSWER]]"
# 合成测试值：随机字母数字，形状上不像任何真实密钥，只能靠登记表精确匹配。
SECRET = "Zx9Qw8Er7Ty6Ui5Op4As3Df2Gh1Jk0Lm"


class _EchoTool(Tool):
    name = "echo"
    description = "echo（验证用）"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}
    is_concurrency_safe = True

    async def run(self, **kwargs):
        return ToolResult(ok=True, content="echo")


@pytest.fixture()
def provider():
    server = AccSseProvider(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture()
def secret():
    from agent.trace import redact

    assert redact.register_secret(SECRET) is True
    try:
        yield SECRET
    finally:
        redact.clear_registered_secrets()


@contextlib.asynccontextmanager
async def _native_loop(server: AccSseProvider, steps: list[dict], registry: ToolRegistry):
    from openai import AsyncOpenAI

    from agent.adapters.native import NativeAdapter

    server.set(steps)
    client = AsyncOpenAI(
        base_url="http://127.0.0.1:%d/v1" % server.server_port,
        api_key="sk-acc-b-fake-0001",
        timeout=20.0,
    )
    try:
        bus = EventBus()
        loop = AgentLoop(
            NativeAdapter(client=client, model="acc-b-native"), registry, bus, turn_id="turn_accb_red"
        )
        yield loop, bus
    finally:
        await client.close()


def _assert_events_clean(bus: EventBus) -> None:
    """每个实际事件与它的线上 SSE 序列化都不得出现原值。"""
    for event in bus._history:
        blob = json.dumps(event.data, ensure_ascii=False, default=str)
        assert SECRET not in blob, ("事件载荷泄露原值", event.type.value)
        wire = sse_format(event)
        assert SECRET not in wire, ("线上 SSE 泄露原值", event.type.value)


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    return registry


# ---- 1. 跨分块切开：每个位置都不得泄露 --------------------------------------------


@pytest.mark.parametrize("split", [1, 3, 5, 11, 16, 31])
async def test_secret_split_across_chunks_never_leaks(provider, secret, split):
    steps = [{"chunks": [DECL + "\n", "前缀A", SECRET[:split], SECRET[split:], "后缀B"]}]
    async with _native_loop(provider, steps, _registry()) as (loop, bus):
        result = await loop.run("回答我")

    assert result.final_content == "前缀A***redacted***后缀B", result.final_content
    _assert_events_clean(bus)


# ---- 2. 逐字符累计快照：不许「先发原文再覆盖」 ----------------------------------------


async def test_many_cumulative_snapshots_never_leak(provider, secret):
    pieces = [SECRET[i : i + 4] for i in range(0, len(SECRET), 4)]
    steps = [{"chunks": [DECL + "\n", "前缀"] + pieces + ["后缀"], "chunk_delay_ms": 60}]
    async with _native_loop(provider, steps, _registry()) as (loop, bus):
        result = await loop.run("回答我")

    assert result.final_content == "前缀***redacted***后缀", result.final_content
    _assert_events_clean(bus)
    answer = [e.data for e in bus._history if e.type.value == "ASSISTANT" and not e.data.get("interim")]
    assert any(e.get("streaming") is True and str(e.get("content") or "") for e in answer), (
        "不得退化为整段生成后一次性显示",
        [e.get("content") for e in answer],
    )


# ---- 3. 尾部 / 相似前缀 / 异常结束 --------------------------------------------------


async def test_partial_tail_is_released_but_full_value_never(provider, secret):
    steps = [{"chunks": [DECL + "\n", "尾部", SECRET[:6]]}]
    async with _native_loop(provider, steps, _registry()) as (loop, bus):
        result = await loop.run("回答我")

    assert SECRET not in result.final_content
    assert result.final_content == "尾部" + SECRET[:6], result.final_content
    _assert_events_clean(bus)


async def test_similar_non_secret_prefix_is_not_over_redacted(provider, secret):
    near = SECRET[:10] + "!"  # 与敏感值共享前缀但已经分歧：不是敏感值的开头
    steps = [{"chunks": [DECL + "\n", near]}]
    async with _native_loop(provider, steps, _registry()) as (loop, bus):
        result = await loop.run("回答我")

    assert result.final_content == near, result.final_content
    _assert_events_clean(bus)


async def test_incomplete_stream_with_split_secret_never_leaks(provider, secret):
    steps = [{"chunks": [DECL + "\n", "前缀", SECRET[:9], SECRET[9:]], "abort": True}]
    async with _native_loop(provider, steps, _registry()) as (loop, bus):
        result = await loop.run("回答我")

    assert result.stop_reason_code == "incomplete_stream"
    assert SECRET not in result.final_content
    assert result.final_content == "前缀***redacted***", result.final_content
    _assert_events_clean(bus)


async def test_replayed_history_events_are_clean(provider, secret):
    """重连重放用的总线历史：逐条 SSE 序列化都不得出现原值。"""
    steps = [{"chunks": [DECL + "\n", "重连前缀", SECRET, "重连后缀"]}]
    async with _native_loop(provider, steps, _registry()) as (loop, bus):
        result = await loop.run("回答我")

    assert SECRET not in result.final_content
    replayed = "".join(sse_format(event) for event in bus._history)
    assert SECRET not in replayed


# ---- 4. 历史落库输出 ---------------------------------------------------------------


class _SecretTextAdapter:
    """不支持流式的兼容路径：一次性正文里带敏感值（验证文本路径同样脱敏）。"""

    mode = "text"
    model = "acc-b-text"
    supports_stream = False

    async def complete(self, messages, tools, **kwargs):
        from agent.adapters.base import ChatMessage, Completion

        return Completion(
            message=ChatMessage(role="assistant", content=DECL + "\n历史正文" + SECRET + "收尾")
        )


@pytest.fixture()
def app_client(db_conn, settings, tmp_path):
    from fastapi.testclient import TestClient

    from agent.api.server import create_app
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    app.state.ctx.attachments.data_dir = tmp_path / "data"
    with TestClient(app) as client:
        yield client, app


def test_persisted_history_content_and_raw_are_redacted(app_client, secret):
    import time
    from unittest.mock import AsyncMock

    client, app = app_client
    app.state.ctx.build_adapter = AsyncMock(return_value=_SecretTextAdapter())
    resp = client.post("/api/turns", json={"message": "回答我", "attachment_ids": []})
    assert resp.status_code in (200, 201, 202), (resp.status_code, resp.text)
    turn_id = str(resp.json()["turn_id"])

    end = None
    deadline = time.time() + 20
    while time.time() < deadline:
        for event in app.state.ctx.bus._history:
            if event.type.value == "TURN_END" and str(event.data.get("turn_id")) == turn_id:
                end = event.data
                break
        if end is not None:
            break
        time.sleep(0.02)
    assert end is not None, "没有等到 TURN_END"
    assert SECRET not in str(end.get("final_content") or ""), "TURN_END 泄露原值"

    row = app.state.ctx.conn.execute(
        "SELECT content, raw FROM messages WHERE role='assistant' AND content_type='text' "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert row is not None, "历史里没有落库的助手消息"
    assert SECRET not in str(row["content"] or ""), "历史正文泄露原值"
    assert SECRET not in str(row["raw"] or ""), "历史 raw 泄露原值"
