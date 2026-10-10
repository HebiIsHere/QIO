"""fb-D 独立验收（R7 · 冻结契约 K3）：终态动作与「重试 / 重发」准入一致。

K3 冻结（docs/plans/2026-10-10-final-boundaries-r1-r7.md §二）：
* 后端动作表：user_stopped（活动取消、排队取消）→ ("retry",)；interrupted → ("resend",)；
* resend 语义保持：只认 journal 里的 interrupted 行、一次性 claim，准入条件不动；
* 读历史 turn facts 时，status == cancelled 且 actions 含 resend 的旧记录，在**读路径**
  归一成 retry（不改写 journal 执行事实）；旧 cancelled 保持 cancelled，不伪装 interrupted。

基线（9e53736）实际：活动取消的 actions 是 ["resend"]，而该行 journal=cancelled，
POST /api/turns/{id}/resend 必然 409 —— 前端「重新发送」是一个点不通的死按钮。
本文件用真实 FastAPI + TurnManager + 真闸门（FakeStreamAdapter 的 hold）钉住：

1. 活动 cancel → TURN_END.actions == ["retry"]；
2. 「点重试」= 前端走既有 POST /api/turns（带 retry_of_turn_id）创建**新 turn**：
   原轮仍 cancelled、新轮被受理并在闸门放行后真的调用模型；
3. cancelled 的 resend 仍 409；interrupted 的 resend 成功且一次性；
4. 历史的 cancelled+resend 旧记录：API 读路径归一成 retry，journal 原样。

模型调用一律用假 provider；不联网、不读任何真实密钥。
运行：cd backend; uv run --frozen pytest -q -p no:warnings tests/test_fb_d_r7_actions_retry_resend.py
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from agent.adapters.fake import FakeStreamAdapter, StreamScript
from agent.api.server import create_app
from agent.credentials.store import MemoryKeyring

DECL = "[[QIO:ANSWER]]"
HOLD_ANSWER = "重试后的回答。"
OLD_ANSWER = "这一轮会被停掉。"


def _client(app):
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://qio.test", timeout=120.0
    )


def _events(app, kind: str, turn_id: str | None = None) -> list[dict]:
    out = []
    for event in list(app.state.ctx.bus._history):
        if event.type.value != kind:
            continue
        if turn_id is not None and str(event.data.get("turn_id")) != turn_id:
            continue
        out.append(event.data)
    return out


def _turn_end(app, turn_id: str) -> dict | None:
    ends = _events(app, "TURN_END", turn_id)
    return ends[0] if ends else None


async def _wait(predicate, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return bool(predicate())


def _has_streamed(app, turn_id: str) -> bool:
    return any(
        str(data.get("content") or "") for data in _events(app, "ASSISTANT", turn_id)
    )


@pytest.fixture()
def app(db_conn, settings):
    application = create_app(settings, db_conn)
    application.state.ctx.credentials._kr = MemoryKeyring()
    return application


async def test_active_cancel_reports_retry_and_retry_creates_new_turn(app):
    """① 活动取消给 retry；② retry 走既有发送接口创建新 turn（闸门控制模型调用）。"""
    hold_a = asyncio.Event()
    hold_b = asyncio.Event()
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                text_chunks=[DECL + "\n", OLD_ANSWER],
                # 先真的把正文发出来（否则「A 已流式」的装置断言不成立），再卡住流。
                hold_after=2,
                hold=hold_a,
                gap_ms=30,
            ),
            StreamScript(
                text_chunks=[DECL + "\n", HOLD_ANSWER, "（完）"],
                hold_after=2,
                hold=hold_b,
                gap_ms=30,
            ),
        ]
    )
    app.state.ctx.build_adapter = AsyncMock(return_value=adapter)

    async with _client(app) as client:
        a = await client.post("/api/turns", json={"message": "这一轮会被用户停掉"})
        assert a.status_code == 200, a.text[:200]
        turn_a = str(a.json()["turn_id"])
        assert await _wait(lambda: _has_streamed(app, turn_a)), "装置失效：A 没有流式增量"

        cancel = await client.post("/api/turns/%s/cancel" % turn_a)
        assert cancel.status_code == 200 and cancel.json().get("cancelled") is True, cancel.text[:200]
        hold_a.set()
        assert await _wait(lambda: _turn_end(app, turn_a) is not None), "A 没有 TURN_END"
        end_a = _turn_end(app, turn_a)

        # 闸门打开之前：B 还没被提交，模型调用数只有 A 这一次
        calls_before_retry = adapter._index
        assert calls_before_retry == 1, ("闸门期只应发生 A 的一次模型调用", calls_before_retry)

        # ② 「点重试」= 既有发送接口 + retry_of_turn_id（不依赖原文件、不自动启动）
        b = await client.post(
            "/api/turns",
            json={"message": "这一轮会被用户停掉", "retry_of_turn_id": turn_a},
        )
        assert b.status_code == 200, b.text[:200]
        turn_b = str(b.json()["turn_id"])
        assert turn_b != turn_a, "retry 必须创建新 turn，而不是复活原轮"

        # 闸门（hold_b）把 B 的模型调用钉住：正文已到，但这一轮还没结束
        assert await _wait(lambda: _has_streamed(app, turn_b)), "B 没有真的调用模型（闸门未生效）"
        assert _turn_end(app, turn_b) is None, "闸门未放行时 B 不得结束（说明闸门确实控住了模型调用）"

        hold_b.set()
        assert await _wait(lambda: _turn_end(app, turn_b) is not None), "B 没有 TURN_END"
        end_b = _turn_end(app, turn_b)

        # cancelled 的 resend 仍 409（准入不动）
        resend = await client.post("/api/turns/%s/resend" % turn_a)
        assert resend.status_code == 409, ("cancelled 不得被 resend 准入", resend.status_code)

    # ① 活动取消的动作 = retry（K3.1）
    assert end_a is not None and end_a["status"] == "cancelled", end_a
    assert end_a["reason_code"] == "user_stopped", end_a
    assert end_a["stopped_by"] == "user", end_a
    assert end_a["actions"] == ["retry"], (
        "活动取消必须给可用的 retry（resend 对该行必然 409）",
        end_a["actions"],
    )

    # ② 新 turn 真的跑完（模型调用数 +1），原轮保持 cancelled
    assert end_b is not None and end_b["status"] == "completed", end_b
    assert HOLD_ANSWER in str(end_b.get("final_content") or ""), end_b.get("final_content")
    assert adapter._index == 2, ("重试必须真的调用一次模型", adapter._index)

    ctx = app.state.ctx
    row_a = ctx.conn.execute(
        "SELECT status, reason_code, actions FROM turn_journal WHERE turn_id = ?", (turn_a,)
    ).fetchone()
    assert row_a["status"] == "cancelled", dict(row_a)
    assert row_a["reason_code"] == "user_stopped", dict(row_a)
    assert json.loads(row_a["actions"]) == ["retry"], dict(row_a)
    assert ctx.turn_journal.recoverable(turn_a) is None, "cancelled 轮不得成为 resend 候选"
    print(
        "[诊断] R7 活动取消：actions=%s；retry 新轮=%s（受理并跑完）；resend=%d"
        % (end_a["actions"], end_b["status"] == "completed", resend.status_code)
    )


async def test_interrupted_still_resends_once(app):
    """③ interrupted 保留 resend：成功一次、第二次 409（准入与一次性 claim 不动）。"""
    ctx = app.state.ctx
    topic = ctx.topics.nodes.create_topic("R7 重发话题").id
    ctx.turn_journal.accepted(turn_id="turn_fb_d_lost", message="上一进程没执行", topic_id=topic)
    ctx.turn_journal.running("turn_fb_d_lost")
    ctx.turn_journal.interrupt_stale()

    adapter = FakeStreamAdapter([StreamScript(text_chunks=[DECL + "\n", "重发成功。"])])
    ctx.build_adapter = AsyncMock(return_value=adapter)

    async with _client(app) as client:
        first = await client.post("/api/turns/turn_fb_d_lost/resend")
        assert first.status_code == 200, first.text[:200]
        new_turn = str(first.json()["turn_id"])
        assert new_turn != "turn_fb_d_lost"
        assert await _wait(lambda: _turn_end(app, new_turn) is not None), "重发的轮没有跑完"
        again = await client.post("/api/turns/turn_fb_d_lost/resend")
        assert again.status_code == 409, ("resend 必须一次性", again.status_code)

    facts = ctx.turn_journal.facts(["turn_fb_d_lost"]).get("turn_fb_d_lost")
    assert facts is not None and facts["status"] == "interrupted", facts
    assert facts["actions"] == ["resend"], ("interrupted 保留 resend", facts)
    print("[诊断] R7 interrupted：首=200；再=%d；facts.actions=%s" % (again.status_code, facts["actions"]))


async def test_history_normalizes_legacy_cancelled_resend_without_rewriting(app):
    """④ 历史的 cancelled+resend 旧记录：API 读路径归一成 retry，journal 原样。"""
    hold = asyncio.Event()
    adapter = FakeStreamAdapter(
        [StreamScript(text_chunks=[DECL + "\n", OLD_ANSWER], hold_after=2, hold=hold, gap_ms=30)]
    )
    app.state.ctx.build_adapter = AsyncMock(return_value=adapter)

    async with _client(app) as client:
        a = await client.post("/api/turns", json={"message": "会被停掉"})
        turn_a = str(a.json()["turn_id"])
        assert await _wait(lambda: _has_streamed(app, turn_a)), "装置失效"
        assert (await client.post("/api/turns/%s/cancel" % turn_a)).json().get("cancelled") is True
        hold.set()
        assert await _wait(lambda: _turn_end(app, turn_a) is not None)

        ctx = app.state.ctx
        # 实时路径给的是新语义（retry）
        live = (await client.get("/api/runtime/state")).json()
        live_facts = {row["turn_id"]: row for row in live["turn_facts"]}.get(turn_a)
        assert live_facts is not None and live_facts["actions"] == ["retry"], live_facts

        # 模拟「旧记录」：把执行事实改回 resend（journal 本身不变，读路径必须归一）
        ctx.conn.execute(
            "UPDATE turn_journal SET actions = ? WHERE turn_id = ?",
            (json.dumps(["resend"]), turn_a),
        )
        legacy = (await client.get("/api/runtime/state")).json()
        legacy_facts = {row["turn_id"]: row for row in legacy["turn_facts"]}.get(turn_a)
        assert legacy_facts is not None, "旧 cancelled 轮必须仍出现在 turn_facts 里"
        assert legacy_facts["status"] == "cancelled", ("旧 cancelled 不得伪装 interrupted", legacy_facts)
        assert legacy_facts["actions"] == ["retry"], (
            "读路径必须把历史 cancelled+resend 归一成 retry",
            legacy_facts["actions"],
        )

    # journal 原样：读路径归一不得批量改写执行事实
    row = ctx.conn.execute(
        "SELECT status, actions FROM turn_journal WHERE turn_id = ?", (turn_a,)
    ).fetchone()
    assert row["status"] == "cancelled", dict(row)
    assert json.loads(row["actions"]) == ["resend"], ("journal 不得被读路径改写", dict(row))
    print(
        "[诊断] R7 历史归一：live=%s legacy_read=%s journal=%s"
        % (live_facts["actions"], legacy_facts["actions"], json.loads(row["actions"]))
    )
