"""F5: 取消语义的回归验证（只验证，不重写实现）。

实现已经在 main 上：取消会把在途的模型请求放进独立 task 并与取消事件竞速，
取消时立刻取消那个 task（底层 HTTP 连接随之断开）。这里只把**能验证的**性质固定住：

1. 在途的 HTTP client task 真的被取消（adapter 侧收到 CancelledError）；
2. turn 进 cancelled 终态（TURN_END status + trace status），队列不被堵；
3. **绝不保存假 final answer**（没有 assistant 消息、TURN_END 不带 final_content）；
4. 用户取消的排队消息 ≠ 「没执行完」：重启后不会被提示重发。

诚实边界（无法由 QIO 保证，也不在这里断言）：供应商服务端是否立刻停止生成与计费，
只断开「客户端的等待」，不保证对方停止。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


class _HangingAdapter:
    """回答永不返回，但会把「请求被取消」如实记下来。"""

    mode = "native"
    model = "fake-hanging"

    def __init__(self) -> None:
        self.started = False
        self.aborted = False

    async def complete(self, messages, tools, **kwargs):
        self.started = True
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.aborted = True
            raise
        return Completion(message=ChatMessage(role="assistant", content="迟到的回答"))


def _ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "cancel_semantics.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


def _assistant_rows(ctx: AppContext, turn_id: str) -> int:
    return int(
        ctx.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE turn_id = ? AND role = 'assistant'",
            (turn_id,),
        ).fetchone()[0]
    )


async def test_cancelled_turn_has_no_fake_final_answer(tmp_path):
    ctx = _ctx(tmp_path)
    topic = ctx.topics.nodes.create_topic("取消话题").id
    adapter = _HangingAdapter()

    async def _build():
        return adapter

    ctx.build_adapter = _build  # type: ignore[assignment]
    events: list[tuple[str, dict]] = []

    async def _emitter(name, data):
        events.append((name, data))

    ctx.turns.set_emitter(_emitter)

    tctx = ctx.turns.submit("这一轮会被取消", topic)
    for _ in range(500):
        if adapter.started:
            break
        await asyncio.sleep(0.01)
    assert adapter.started, "测试前提不成立：模型调用没有开始"

    assert ctx.turns.cancel_active() is True
    result = await ctx.turns.wait(tctx.turn_id, timeout=15)
    assert result is not None and result.get("reason") == "cancelled"

    # 1) 在途请求被真正取消
    assert adapter.aborted is True

    # 2) turn 进 cancelled 终态，且只发一个 TURN_START / TURN_END
    ends = [d for (name, d) in events if name == "TURN_END"]
    starts = [d for (name, d) in events if name == "TURN_START"]
    assert len(starts) == 1 and len(ends) == 1
    assert ends[0]["status"] == "cancelled"
    trace = ctx.trace_store.get(tctx.turn_id)
    assert trace is not None and trace["status"] == "cancelled"

    # 3) 不保存假 final answer
    assert ends[0].get("final_content") in (None, "")
    assert _assistant_rows(ctx, tctx.turn_id) == 0


async def test_cancelled_queued_turn_is_not_recoverable_after_restart(tmp_path):
    """用户取消 ≠ 「被进程掐断」：重启后不该提示「这条没执行，要不要重发」。"""
    ctx1 = _ctx(tmp_path)
    topic = ctx1.topics.nodes.create_topic("排队取消").id
    adapter = _HangingAdapter()

    async def _build():
        return adapter

    ctx1.build_adapter = _build  # type: ignore[assignment]
    first = ctx1.turns.submit("第一条（慢）", topic)
    for _ in range(500):
        if adapter.started:
            break
        await asyncio.sleep(0.01)
    assert adapter.started
    second = ctx1.turns.submit("第二条（排队）", topic)
    assert ctx1.turns.queued_count() == 1

    assert ctx1.turns.cancel(second.turn_id) is True
    assert ctx1.turns.queued_count() == 0
    row = ctx1.conn.execute(
        "SELECT status FROM turn_journal WHERE turn_id = ?", (second.turn_id,)
    ).fetchone()
    assert row["status"] == "cancelled"
    assert _assistant_rows(ctx1, second.turn_id) == 0

    await ctx1.turns.shutdown()
    ctx2 = _ctx(tmp_path)  # 重启
    lost = {r["message"] for r in ctx2.turn_journal.unfinished()}
    assert "第一条（慢）" in lost  # 被进程掐断 → 提示用户「没有执行完」
    assert "第二条（排队）" not in lost  # 用户自己取消 → 不再提示
    assert first.turn_id  # 只是保持引用，断言对象是 second


@pytest.fixture()
def client(tmp_path):
    conn = connect(tmp_path / "cancel_api.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path), conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def test_cancel_while_running_via_http_api(client):
    """HTTP 取消路径（现有测试只覆盖了「空闲时取消返回 false」）。"""
    ctx = client.app.state.ctx
    adapter = _HangingAdapter()

    async def _build():
        return adapter

    ctx.build_adapter = _build  # type: ignore[assignment]
    accepted = client.post("/api/turns", json={"message": "会被取消的一轮"}).json()
    assert accepted["accepted"] is True

    deadline = time.time() + 15
    while time.time() < deadline and not adapter.started:
        time.sleep(0.02)
    assert adapter.started, "测试前提不成立：模型调用没有开始"

    cancelled = client.post("/api/turns/cancel").json()
    assert cancelled["cancelled"] is True
    assert cancelled["turn_id"] == accepted["turn_id"]

    deadline = time.time() + 15
    while time.time() < deadline:
        if client.get("/api/turns/queue").json()["running"] is None:
            break
        time.sleep(0.02)
    assert client.get("/api/turns/queue").json()["running"] is None, "取消后队列必须回到空闲"
    assert adapter.aborted is True
    assert _assistant_rows(ctx, accepted["turn_id"]) == 0
