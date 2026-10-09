"""F 独立验证（阶段一 · F12）：排队轮被取消后必须有可持久化的结束事实。

契约：docs/plans/2026-10-09-process-attachment-audit-consolidation.md §C1/§C2 ——
「同一 turn 恰好一次 TURN_START、一次 TURN_END（终态 completed / failed / cancelled /
unavailable）」「结束事实（原因 / 动作 / 耗时）被记录持久化」「一个 turn 的取消不得影响
另一个正在运行的 turn」。

场景：A 流式执行中提交 B（排队）→ 按 turn_id 取消 B → B 的结束事实必须：
1) 以 TURN_END 事件发出（reason_code=user_stopped、stopped_by=user、actions 含 retry）；

Lead 裁定（2026-10-09，列为冻结）：排队取消的可恢复动作是 **retry** 而不是 resend。
理由：/api/turns/{id}/resend 只接受 journal 里记成 interrupted 的行（server.py 的
recoverable/claim），排队取消后 journal 落的是 cancelled，resend 必然 409 —— 那是个死
按钮；retry 走前端「重发该轮用户消息」（session.retryTurn → 普通发送接口），真实可用。
active 取消路径保持既有 ("resend")，既有 test_turn_timing_facts.py 的精确相等断言不动。
2) 持久化进 turn_journal（刷新 / 换设备后仍可恢复「重发」入口）；
3) A 完全不受影响（继续完成、回答归 A、ASSISTANT 事件全部归 A）。

基线：TurnManager 取消排队项时只置终态 + 兑现等待者，**不发 TURN_END**；worker 取到
tombstone 直接跳过。于是 B 既没有结束事件，也没有落库的结束事实。

运行：cd backend; uv run --frozen --extra dev pytest tests/test_acc_f_12_queued_cancel_facts.py -q
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from agent.adapters.fake import FakeStreamAdapter, StreamScript
from agent.api.server import create_app
from agent.credentials.store import MemoryKeyring

ANSWER_MARKER = "[[QIO:ANSWER]]"


def _client(app):
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://qio.test", timeout=120.0
    )


def _history(app) -> list:
    return list(app.state.ctx.bus._history)


def _of(app, kind: str, turn_id: str | None = None) -> list:
    out = []
    for event in _history(app):
        if event.type.value != kind:
            continue
        if turn_id is not None and str(event.data.get("turn_id")) != turn_id:
            continue
        out.append(event)
    return out


async def _wait(predicate, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return bool(predicate())


def _turn_end(app, turn_id: str) -> dict | None:
    for event in _of(app, "TURN_END", turn_id):
        return event.data
    return None


@pytest.fixture()
def app(db_conn, settings):
    application = create_app(settings, db_conn)
    application.state.ctx.credentials._kr = MemoryKeyring()
    return application


async def test_cancelled_queued_turn_has_persisted_end_facts_and_does_not_touch_active(app):
    hold = asyncio.Event()
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                text_chunks=[ANSWER_MARKER + "\n", "A 第一段。", "A 第二段。"],
                hold_after=2,
                hold=hold,
                gap_ms=60,
            ),
            StreamScript(text_chunks=[ANSWER_MARKER + "\n", "B 的回答。"]),
        ]
    )
    app.state.ctx.build_adapter = AsyncMock(return_value=adapter)

    async with _client(app) as client:
        a = await client.post("/api/turns", json={"message": "A 请回答"})
        turn_a = str(a.json()["turn_id"])
        streamed = await _wait(
            lambda: any(
                str(e.data.get("turn_id")) == turn_a and str(e.data.get("content") or "")
                for e in _of(app, "ASSISTANT")
            ),
            timeout=20,
        )
        assert streamed, "装置失效：A 没有产出流式增量"

        b = await client.post("/api/turns", json={"message": "B 请回答"})
        turn_b = str(b.json()["turn_id"])
        await asyncio.sleep(0.15)
        snapshot = (await client.get("/api/turns/queue")).json()
        assert snapshot["running"]["turn_id"] == turn_a, snapshot
        assert [q["turn_id"] for q in snapshot["queued"]] == [turn_b], snapshot

        cancel = await client.post("/api/turns/%s/cancel" % turn_b)
        assert cancel.status_code == 200, cancel.text[:200]
        assert cancel.json().get("cancelled") is True, cancel.json()

        # A 不得被取消
        assert _of(app, "TURN_START", turn_a), "A 的 TURN_START 不应消失"
        assert _turn_end(app, turn_a) is None, "取消 B 不得结束 A"

        hold.set()
        await _wait(lambda: _turn_end(app, turn_a) is not None, timeout=60)
        await asyncio.sleep(0.5)

        queue_after = (await client.get("/api/turns/queue")).json()

    end_a = _turn_end(app, turn_a)
    end_b = _turn_end(app, turn_b)
    assert end_a is not None, "A 必须有 TURN_END"
    assert end_a["status"] == "completed", ("A 必须继续跑完", end_a.get("status"))
    assert end_a["final_content"] == "A 第一段。A 第二段。", end_a.get("final_content")
    assert [q["turn_id"] for q in queue_after.get("queued") or []] == [], queue_after
    assert not queue_after.get("running"), queue_after

    # 归属：A 的 ASSISTANT 事件全部归 A
    assistant_turns = {str(e.data.get("turn_id")) for e in _of(app, "ASSISTANT")}
    assert assistant_turns == {turn_a}, ("取消 B 影响了 A 的活动归属", assistant_turns)

    # 1) B 必须有 TURN_END 结束事实
    assert end_b is not None, (
        "排队轮被取消后没有 TURN_END（前端拿不到结束事实，刷新后也恢复不了）",
        {"turn_b": turn_b, "events": [e.type.value for e in _history(app)]},
    )
    assert end_b["status"] == "cancelled", end_b
    assert end_b["reason_code"] == "user_stopped", (
        "用户取消排队轮的结束原因必须是 user_stopped",
        end_b.get("reason_code"),
    )
    assert end_b["stopped_by"] == "user", end_b.get("stopped_by")
    assert "retry" in [str(x) for x in end_b.get("actions") or []], (
        "被取消的排队轮应当给「重发/重试」入口（retry：resend 对该行必然 409，是死按钮）",
        end_b.get("actions"),
    )
    assert isinstance(end_b.get("duration_ms"), int) and end_b["duration_ms"] >= 0, (
        "结束事实必须带耗时口径",
        end_b.get("duration_ms"),
    )
    assert isinstance(end_b.get("queue_ms"), int) and end_b["queue_ms"] >= 0, end_b.get("queue_ms")

    # 2) 结束事实必须持久化（刷新 / 换设备后仍在）
    facts = app.state.ctx.turn_journal.facts([turn_b]).get(turn_b)
    assert facts is not None, "排队轮取消的结束事实没有落进 turn_journal"
    assert facts["reason_code"] == "user_stopped", facts
    assert "retry" in facts["actions"], facts
    assert facts["status"] == "cancelled", facts

    print(
        "[诊断] F12 排队取消：B_end=%s reason=%s actions=%s 落库=%s；A=%s 归属全部归A=%s"
        % (
            end_b.get("status"),
            end_b.get("reason_code"),
            end_b.get("actions"),
            facts.get("reason_code"),
            end_a.get("status"),
            assistant_turns == {turn_a},
        )
    )
