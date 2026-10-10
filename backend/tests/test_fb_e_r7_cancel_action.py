"""fb-E / R7 反例：活动任务取消后动作表仍给 resend（而 resend 对该行必然 409）。

用户可见规则（冻结契约 K3）：
* `user_stopped`（活动取消、排队取消）→ `("retry",)`；`interrupted` → `("resend",)`；
* 「动作表只列**当前确实可用**的操作」：列进 actions 的每个动作都必须真的能点通；
* /api/turns/{id}/resend 只接受 journal 记成 `interrupted` 且未被领取的行 ——
  `user_stopped` 取消的行落库是 `cancelled`，resend 必然被拒。

基线：活动取消 → TURN_END.actions = ["resend"]；紧接着 POST resend 得到 409 ——
一个点不通的死按钮被摆给了用户。

运行：cd backend; uv run --frozen pytest -q tests/test_fb_e_r7_cancel_action.py
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


async def _start_held_turn(app, hold: asyncio.Event):
    """启动一个被 hold 卡住的活动 turn（真实：假 provider → adapter → loop → TurnManager）。"""
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                text_chunks=[ANSWER_MARKER + "\n", "第一段。", "第二段。"],
                hold_after=2,
                hold=hold,
                gap_ms=60,
            )
        ]
    )
    app.state.ctx.build_adapter = AsyncMock(return_value=adapter)
    return adapter


async def test_active_cancel_advertises_retry_not_dead_resend(app):
    hold = asyncio.Event()
    await _start_held_turn(app, hold)
    async with _client(app) as client:
        a = await client.post("/api/turns", json={"message": "A 请回答"})
        assert a.status_code == 200, a.text[:200]
        turn_a = str(a.json()["turn_id"])
        assert await _wait(
            lambda: any(
                str(e.data.get("turn_id")) == turn_a and str(e.data.get("content") or "")
                for e in _of(app, "ASSISTANT")
            ),
            timeout=20,
        ), "装置：A 没有产出流式增量"

        cancel = await client.post("/api/turns/%s/cancel" % turn_a)
        assert cancel.status_code == 200, cancel.text[:200]
        assert await _wait(lambda: _turn_end(app, turn_a) is not None, timeout=30), "取消后没有 TURN_END"
        end = _turn_end(app, turn_a)

    assert end["status"] == "cancelled", end
    assert end["reason_code"] == "user_stopped", end.get("reason_code")
    actions = [str(x) for x in end.get("actions") or []]
    # K3：活动取消（user_stopped）只应给 retry（前端「重发这条用户消息」）
    assert actions == ["retry"], (
        "活动取消的可用动作必须是 retry（resend 只认 interrupted 行）",
        {"actions": actions, "reason_code": end.get("reason_code"), "status": end.get("status")},
    )


async def test_every_advertised_action_must_be_usable(app):
    """动作表只列「当前确实可用」的操作：列进去的就必须点得通。"""
    hold = asyncio.Event()
    await _start_held_turn(app, hold)
    async with _client(app) as client:
        a = await client.post("/api/turns", json={"message": "B 请回答"})
        turn_a = str(a.json()["turn_id"])
        assert await _wait(
            lambda: any(
                str(e.data.get("turn_id")) == turn_a and str(e.data.get("content") or "")
                for e in _of(app, "ASSISTANT")
            ),
            timeout=20,
        ), "装置：没有流式增量"
        await client.post("/api/turns/%s/cancel" % turn_a)
        assert await _wait(lambda: _turn_end(app, turn_a) is not None, timeout=30)
        end = _turn_end(app, turn_a)
        actions = [str(x) for x in end.get("actions") or []]

        # resend 是后端唯一对应的端点：只有被列出来时才要求它可用
        resend_status = None
        if "resend" in actions:
            resp = await client.post("/api/turns/%s/resend" % turn_a)
            resend_status = resp.status_code

    assert "resend" not in actions or resend_status == 200, (
        "动作表列出的动作必须真的可用：cancelled 行上的 resend 是死按钮",
        {"actions": actions, "resend_status": resend_status, "reason_code": end.get("reason_code")},
    )
