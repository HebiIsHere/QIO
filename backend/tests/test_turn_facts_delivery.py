"""结束事实的落库与下发（R4 S6）：TURN_END → 台账 → 历史 / 分页 / RESYNC。

D 的实机 S6 确认：刷新之后失败轮的「重试」入口消失 —— 前端已有消费路径
（applyTurnFactsSnapshot，后端优先于留痕），缺的是**后端权威来源**。

本文件覆盖（我自己名下）：
1. TURN_END 的 reason_code / reason / stopped_by / actions 真的落进台账；
2. /api/session/context 与 /api/session/messages 各带 turn_facts（刷新 / 翻页都能恢复）；
3. /api/runtime/state（RESYNC）带当前相关轮次的 turn_facts；
4. 台账里**没有事实**的轮次不出现（不伪造成 none / 假原因）；
5. 一次请求只做**一次**批量查询（不 N+1）。

时序全部用「事件 / 状态等待」控制（有限超时只用于判定失败）。
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from agent.adapters.base import ChatMessage, Completion
from agent.adapters.errors import ProviderInternalError
from agent.api.server import create_app
from agent.credentials.store import MemoryKeyring

DEADLINE = 20.0


class _BoomAdapter:
    """厂商故障（归一化后的 ProviderError 家族）→ reason_code=provider_error。"""

    mode = "text"
    model = "fake-boom"
    supports_stream = False

    async def complete(self, messages, tools, **kwargs):
        raise ProviderInternalError("厂商返回 500：上游错误（fake provider）")


class _SlowAdapter:
    """一直不回答：用于「还在跑」与「用户取消」两种时序。"""

    mode = "text"
    model = "fake-slow"
    supports_stream = False

    async def complete(self, messages, tools, **kwargs):
        await asyncio.sleep(30)
        return Completion(message=ChatMessage(role="assistant", content="迟到的回答"))


@pytest.fixture()
def app_client(db_conn, settings, tmp_path):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    # 测试污染防护：Settings(data_dir=...) 会被 QIO_DATA_DIR 覆盖，钉住附件落点
    app.state.ctx.attachments.data_dir = tmp_path / "data"
    with TestClient(app) as client:
        yield client, app


def _turn_end(app, turn_id: str, *, timeout: float = DEADLINE) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for event in app.state.ctx.bus._history:
            if event.type.value == "TURN_END" and str(event.data.get("turn_id")) == turn_id:
                return event.data
        time.sleep(0.02)
    return None


def _submit(client, message: str) -> str:
    resp = client.post("/api/turns", json={"message": message, "attachment_ids": []})
    assert resp.status_code in (200, 201, 202), (resp.status_code, resp.text)
    return str(resp.json()["turn_id"])


def _wait_running(client, *, timeout: float = DEADLINE) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.get("/api/turns/queue").json().get("running"):
            return True
        time.sleep(0.02)
    return False


def _fail_one_turn(client, app, message: str = "会失败的一轮") -> str:
    app.state.ctx.build_adapter = AsyncMock(return_value=_BoomAdapter())
    turn_id = _submit(client, message)
    end = _turn_end(app, turn_id)
    assert end is not None, "没有等到 TURN_END"
    assert end["reason_code"] == "provider_error", end
    return turn_id


# ---- 1. 落库 -----------------------------------------------------------------------


def test_turn_end_facts_are_persisted_to_the_journal(app_client):
    """TURN_END 的结束事实必须落进台账（刷新后才有权威来源，而不是只有一次事件）。"""
    client, app = app_client
    turn_id = _fail_one_turn(client, app)

    rows = app.state.ctx.turn_journal.facts([turn_id])
    assert turn_id in rows, f"台账里没有这一轮的结束事实：{rows}"
    row = rows[turn_id]
    assert row["reason_code"] == "provider_error", row
    assert row["stopped_by"] == "system", row
    assert isinstance(row["reason"], str) and row["reason"].strip(), row
    assert "retry" in row["actions"], ("失败轮必须留下「重试」这个可用操作", row)
    assert row["status"] == "failed", row


# ---- 2. 历史 / 分页下发 --------------------------------------------------------------


def test_history_context_carries_turn_facts_for_the_failed_turn(app_client):
    """S6 的核心路径：刷新后 /api/session/context 必须带回失败轮的事实与可用操作。"""
    client, app = app_client
    turn_id = _fail_one_turn(client, app)

    ctx = client.get("/api/session/context").json()
    facts = {row["turn_id"]: row for row in ctx.get("turn_facts") or []}
    assert turn_id in facts, f"历史没有带回这一轮的事实：{ctx.get('turn_facts')}"
    row = facts[turn_id]
    assert row["status"] == "failed", row
    assert row["reason_code"] == "provider_error", row
    assert row["reason"] and row["reason"].strip(), row
    assert row["stopped_by"] == "system", row
    assert "retry" in row["actions"], ("前端要据此恢复「重试」入口", row)


def test_paged_history_carries_turn_facts(app_client):
    """向上翻页（/api/session/messages）同样要带回 turn_facts。"""
    client, app = app_client
    turn_id = _fail_one_turn(client, app, message="翻页也要看到的一轮")

    page = client.get("/api/session/messages", params={"limit": 20}).json()
    facts = {row["turn_id"]: row for row in page.get("turn_facts") or []}
    assert turn_id in facts, f"分页没有带回这一轮的事实：{page.get('turn_facts')}"
    assert facts[turn_id]["reason_code"] == "provider_error", facts[turn_id]
    assert "retry" in facts[turn_id]["actions"], facts[turn_id]


# ---- 3. RESYNC ----------------------------------------------------------------------


def test_runtime_state_carries_turn_facts_for_current_turns(app_client):
    """RESYNC（/api/runtime/state）要带**当前相关轮次**的事实：这里用「刚被用户停掉的一轮」。"""
    client, app = app_client
    app.state.ctx.build_adapter = AsyncMock(return_value=_SlowAdapter())
    turn_id = _submit(client, "跑到一半被停")
    assert _wait_running(client), "这一轮没有跑起来"

    stopped = client.post("/api/turns/cancel")
    assert stopped.status_code == 200, stopped.text
    end = _turn_end(app, turn_id)
    assert end is not None, "取消之后没有 TURN_END"
    assert end["reason_code"] == "user_stopped", end

    state = client.get("/api/runtime/state").json()
    facts = {row["turn_id"]: row for row in state.get("turn_facts") or []}
    assert turn_id in facts, f"RESYNC 没有带回当前相关轮次的事实：{state.get('turn_facts')}"
    assert facts[turn_id]["reason_code"] == "user_stopped", facts[turn_id]
    assert facts[turn_id]["stopped_by"] == "user", facts[turn_id]


# ---- 4. 没有事实就不出现（不伪造） ----------------------------------------------------


def test_turns_without_facts_are_not_faked(app_client):
    """还在跑（台账里没有结束事实）的轮次不得出现在 turn_facts 里，更不许编个 none。"""
    client, app = app_client
    app.state.ctx.build_adapter = AsyncMock(return_value=_SlowAdapter())
    turn_id = _submit(client, "还在跑，没有结束事实")
    assert _wait_running(client), "这一轮没有跑起来"

    context = client.get("/api/session/context").json()
    assert all(
        row["turn_id"] != turn_id for row in context.get("turn_facts") or []
    ), ("没有结束事实的轮次不该出现在历史 turn_facts 里", context.get("turn_facts"))
    state = client.get("/api/runtime/state").json()
    assert all(
        row["turn_id"] != turn_id for row in state.get("turn_facts") or []
    ), ("没有结束事实的轮次不该出现在 RESYNC 的 turn_facts 里", state.get("turn_facts"))

    client.post("/api/turns/cancel")  # 收尾，避免影响后续用例
    assert _turn_end(app, turn_id) is not None


# ---- 5. 批量查询（不 N+1） -----------------------------------------------------------


def test_turn_facts_query_is_batched_once_per_request(app_client):
    """一页历史只查一次台账（批量），不是每个轮次一次。"""
    client, app = app_client
    first = _fail_one_turn(client, app, message="第一条失败")
    second = _fail_one_turn(client, app, message="第二条失败")

    calls: list[list[str]] = []
    real_facts = app.state.ctx.turn_journal.facts

    def counting(turn_ids):
        calls.append([str(item) for item in turn_ids])
        return real_facts(turn_ids)

    app.state.ctx.turn_journal.facts = counting
    try:
        context = client.get("/api/session/context").json()
    finally:
        app.state.ctx.turn_journal.facts = real_facts

    assert len(calls) == 1, f"一页历史只该查一次台账，实际 {len(calls)} 次：{calls}"
    assert {first, second} <= set(calls[0]), calls
    got = {row["turn_id"] for row in context.get("turn_facts") or []}
    assert {first, second} <= got, context.get("turn_facts")
