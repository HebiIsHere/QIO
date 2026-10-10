"""F 独立验证（阶段一 · F05）：排队不改变活动 turn 归属。

契约：docs/plans/2026-10-09-process-attachment-audit-consolidation.md §C1 ——
「排队中的 turn（已受理未启动）不改变任何事件归属；活动 turn 同一时刻只有一个」、
「同一 turn 恰好一次 TURN_START、一次 TURN_END」、「回答与结束事实一律按 turn_id 归属」。

链路（真实跨层）：假 provider（FakeStreamAdapter）→ adapter → AgentLoop → TurnManager → 事件总线。
A 流式执行（provider 被 hold 卡住）期间提交 B / C，B、C 排队。断言：
1) 排队期间活动归属仍是 A：只有 A 的 ASSISTANT / TURN_START；
2) A 的回答与结束事实归 A（TURN_END.final_content 是 A 自己的回答、status=completed）；
3) B 的 TURN_START 严格晚于 A 的 TURN_END（B 在 A 完成后才启动），C 同理。

基线判定见 docs/acc/acc-f.md。
运行：cd backend; uv run --frozen --extra dev pytest tests/test_acc_f_05_turn_ownership.py -q
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
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


async def _wait(predicate, timeout: float = 60.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return bool(predicate())


def _turn_end(app, turn_id: str) -> dict | None:
    for event in _of(app, "TURN_END", turn_id):
        return event.data
    return None


async def _wait_turn_end(app, turn_id: str, timeout: float = 60.0) -> dict | None:
    await _wait(lambda: _turn_end(app, turn_id) is not None, timeout)
    return _turn_end(app, turn_id)


@pytest.fixture()
def app(db_conn, settings):
    application = create_app(settings, db_conn)
    application.state.ctx.credentials._kr = MemoryKeyring()
    return application


async def test_queued_turns_do_not_steal_active_turn_ownership(app):
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
            StreamScript(text_chunks=[ANSWER_MARKER + "\n", "C 的回答。"]),
        ]
    )
    app.state.ctx.build_adapter = AsyncMock(return_value=adapter)

    async with _client(app) as client:
        a = await client.post("/api/turns", json={"message": "A 请回答"})
        assert a.status_code == 200, a.text[:200]
        turn_a = str(a.json()["turn_id"])

        # A 已开始且已有可观测的流式 ASSISTANT 增量
        streamed = await _wait(
            lambda: any(
                str(e.data.get("turn_id")) == turn_a and str(e.data.get("content") or "")
                for e in _of(app, "ASSISTANT")
            ),
            timeout=20,
        )
        assert streamed, "装置失效：A 没有产出流式增量（provider hold 未生效？）"

        # A 仍在跑：提交 B、C（应当排队，不改变 A 的活动归属）
        b = await client.post("/api/turns", json={"message": "B 请回答"})
        c = await client.post("/api/turns", json={"message": "C 请回答"})
        assert b.status_code == 200 and c.status_code == 200, (b.text[:200], c.text[:200])
        turn_b = str(b.json()["turn_id"])
        turn_c = str(c.json()["turn_id"])
        await asyncio.sleep(0.2)

        snapshot = (await client.get("/api/turns/queue")).json()
        assert snapshot.get("running") and snapshot["running"]["turn_id"] == turn_a, snapshot
        assert [q["turn_id"] for q in snapshot.get("queued") or []] == [turn_b, turn_c], snapshot

        # 排队期间：活动归属仍是 A —— 不允许 B / C 的 ASSISTANT 事件或 TURN_START
        assistant_turns = {str(e.data.get("turn_id")) for e in _of(app, "ASSISTANT")}
        assert assistant_turns == {turn_a}, (
            "B/C 排队改变了 A 的活动归属（出现了别的 turn 的 ASSISTANT 事件）",
            assistant_turns,
        )
        start_turns = [str(e.data.get("turn_id")) for e in _of(app, "TURN_START")]
        assert start_turns == [turn_a], (
            "B/C 排队期间不得发出 TURN_START（只有真正开始执行的 turn 才有）",
            start_turns,
        )
        assert _turn_end(app, turn_a) is None, "A 还没结束，不应有 TURN_END"

        hold.set()
        end_a = await _wait_turn_end(app, turn_a)
        end_b = await _wait_turn_end(app, turn_b)
        end_c = await _wait_turn_end(app, turn_c)

    assert end_a is not None and end_b is not None and end_c is not None, "有 TURN_END 没有到达"
    assert end_a["status"] == "completed", ("A 必须正常完成", end_a.get("status"))
    assert end_a["final_content"] == "A 第一段。A 第二段。", (
        "A 的回答必须归 A，内容不得被 B/C 覆盖",
        end_a.get("final_content"),
    )
    assert end_b["final_content"] == "B 的回答。", end_b.get("final_content")
    assert end_c["final_content"] == "C 的回答。", end_c.get("final_content")

    # 事件归属：A 的全部 ASSISTANT 事件都带 turn_id=A，且只出现 A 的文字
    a_contents = [str(e.data.get("content") or "") for e in _of(app, "ASSISTANT", turn_a)]
    assert a_contents, "A 必须有流式 ASSISTANT 事件"
    assert all("B 的回答" not in text and "C 的回答" not in text for text in a_contents)

    # 每个 turn 恰好一次 TURN_START / TURN_END
    for turn_id in (turn_a, turn_b, turn_c):
        assert len(_of(app, "TURN_START", turn_id)) == 1, turn_id
        assert len(_of(app, "TURN_END", turn_id)) == 1, turn_id

    # B 在 A 完成后才启动：B 的 TURN_START 在事件序列里必须晚于 A 的 TURN_END
    ordered = _history(app)
    index_a_end = next(
        i for i, e in enumerate(ordered)
        if e.type.value == "TURN_END" and str(e.data.get("turn_id")) == turn_a
    )
    index_b_start = next(
        i for i, e in enumerate(ordered)
        if e.type.value == "TURN_START" and str(e.data.get("turn_id")) == turn_b
    )
    index_c_start = next(
        i for i, e in enumerate(ordered)
        if e.type.value == "TURN_START" and str(e.data.get("turn_id")) == turn_c
    )
    assert index_b_start > index_a_end, (
        "B 在 A 结束前就启动了（排队顺序破坏）",
        {"a_end": index_a_end, "b_start": index_b_start},
    )
    assert index_c_start > index_b_start, ("C 必须在 B 之后启动（FIFO）", index_c_start)

    print(
        "[诊断] F05 排队归属：A=%s B=%s C=%s；ASSISTANT 全部归 A=%s；B_start>A_end=%s；C在B后=%s"
        % (
            end_a["status"],
            end_b["status"],
            end_c["status"],
            set(str(e.data.get("turn_id")) for e in _of(app, "ASSISTANT")) == {turn_a},
            index_b_start > index_a_end,
            index_c_start > index_b_start,
        )
    )
