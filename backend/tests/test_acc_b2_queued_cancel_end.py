"""B2 独立验收：排队轮取消必须留下「恰好一条」TURN_END 结束事实（F12 · 冻结契约 C1/C2/§4）。

反例（基线应为红）：排队（accepted 未开始）轮被取消后只被 pop 掉 + 落一条
journal 终态，**没有任何 TURN_END**；worker 取到 tombstone 直接跳过。于是前端拿不到
结束事实，刷新后也恢复不了重发入口。

冻结契约（Lead 2026-10-09 裁定）：
* 排队取消路径的 actions == ["retry"]（前端重发该轮用户消息）；**不是** resend ——
  /api/turns/{id}/resend 只接受 journal 记成 interrupted 的行，排队取消后
  journal=cancelled，resend 必然 409，是死按钮；
* active 取消路径保持既有 user_stopped → ["resend"] 不变；
* 立刻（不等 active turn A 结束）发出且只发一条 TURN_END：status=cancelled、
  reason_code=user_stopped、stopped_by=user、final_content 为空（不伪造内容）；
* 台账 terminal(cancelled, reason="user") + record_facts 都落地（刷新后可恢复）；
* 重复 / 迟到取消幂等：不重复发 END、不改写已终态、不复活；
* 绝不触碰 active turn A（A 的 TURN_START / TURN_END / 事件归属不变），
  TURN_QUEUE 快照把 B 从队列摘掉。

运行：cd backend; uv run --frozen pytest tests/test_acc_b2_queued_cancel_end.py -q
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from agent.core.turn import TurnManager
from agent.storage.turn_journal import TurnJournal


# ---- 装置 -------------------------------------------------------------------


def _wire(journal: TurnJournal, runner) -> tuple[TurnManager, list[tuple[str, dict]]]:  # noqa: ANN001
    events: list[tuple[str, dict]] = []

    async def emitter(name: str, data: dict) -> None:
        events.append((name, data))
        if name == "TURN_END":
            # 与服务层 app.py::_record_turn_facts 同一口径。
            journal.record_facts(
                str(data.get("turn_id")),
                reason_code=data.get("reason_code"),
                reason=data.get("reason"),
                stopped_by=data.get("stopped_by"),
                actions=list(data.get("actions") or []),
            )

    manager = TurnManager(runner=runner, emitter=emitter)
    manager.set_journal(journal)
    return manager, events


def _of(events: list[tuple[str, dict]], kind: str, turn_id: str | None = None) -> list[dict]:
    out = []
    for name, data in events:
        if name != kind:
            continue
        if turn_id is not None and str(data.get("turn_id")) != turn_id:
            continue
        out.append(data)
    return out


async def _until(predicate: Callable[[], Any], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return bool(predicate())


async def _drain(manager: TurnManager) -> None:
    """让已经调度的补发任务跑完（取消路径不经过 worker）。"""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


# ---- 1. 排队取消：立刻一条 END + 落库 + A 不受影响 ------------------------------


async def test_queued_cancel_emits_one_end_and_persists_facts(db_conn):
    journal = TurnJournal(db_conn)
    a_started = asyncio.Event()
    release_a = asyncio.Event()
    order: list[str] = []

    async def runner(ctx) -> None:  # noqa: ANN001
        order.append(ctx.turn_id)
        if len(order) == 1:
            a_started.set()
            await release_a.wait()

    manager, events = _wire(journal, runner)
    a = manager.submit("A 请回答")
    await a_started.wait()
    b = manager.submit("B 请回答")
    replace_status_b = b.status
    assert manager.queued_count() == 1, "B 必须先在队列里"
    assert replace_status_b in ("queued", "accepted"), replace_status_b

    snapshot = manager.snapshot()
    assert snapshot["running"]["turn_id"] == a.turn_id, snapshot
    assert [q["turn_id"] for q in snapshot["queued"]] == [b.turn_id], snapshot

    assert manager.cancel(b.turn_id) is True
    # 立刻（A 还挂着）：B 的 TURN_END 必须已经（即将）发出
    assert await _until(lambda: _of(events, "TURN_END", b.turn_id)), (
        "排队轮被取消后没有 TURN_END（前端拿不到结束事实，刷新后也恢复不了）",
        [n for n, _ in events],
    )
    end_b = _of(events, "TURN_END", b.turn_id)[0]

    assert end_b["status"] == "cancelled", end_b
    assert end_b["reason_code"] == "user_stopped", end_b
    assert end_b["stopped_by"] == "user", end_b
    assert end_b["actions"] == ["retry"], end_b
    assert not (end_b.get("final_content") or ""), ("取消的排队轮不得伪造内容", end_b)
    assert isinstance(end_b.get("duration_ms"), int) and end_b["duration_ms"] >= 0, end_b
    assert isinstance(end_b.get("queue_ms"), int) and end_b["queue_ms"] >= 0, end_b

    # TURN_QUEUE 快照要把 B 从队列摘掉；A 仍然是唯一 active
    assert manager.snapshot()["queued"] == [], manager.snapshot()
    assert manager.active is a, "取消 B 不得抢走 active"
    assert _of(events, "TURN_END", a.turn_id) == [], "取消 B 不得结束 A"
    assert _of(events, "TURN_START", b.turn_id) == [], "排队轮被取消不得发 TURN_START"

    release_a.set()
    await manager.wait(a.turn_id, timeout=5)
    await _until(lambda: _of(events, "TURN_END", a.turn_id))
    await manager.shutdown()
    await _drain(manager)

    end_a = _of(events, "TURN_END", a.turn_id)
    assert len(end_a) == 1, end_a
    assert end_a[0]["status"] == "completed", ("A 必须继续跑完", end_a[0])
    assert len(_of(events, "TURN_END", b.turn_id)) == 1, "B 必须且只能有一条 TURN_END"
    assert len(_of(events, "TURN_START", a.turn_id)) == 1, _of(events, "TURN_START", a.turn_id)

    facts = journal.facts([b.turn_id]).get(b.turn_id)
    assert facts is not None, "排队轮取消的结束事实没有落进 turn_journal"
    assert facts["status"] == "cancelled", facts
    assert facts["reason_code"] == "user_stopped", facts
    assert facts["stopped_by"] == "user", facts
    assert facts["actions"] == ["retry"], facts
    print(
        "[诊断] B2 排队取消：B=%s status=%s reason=%s actions=%s duration_ms=%s queue_ms=%s；A=%s"
        % (
            b.turn_id,
            end_b["status"],
            end_b["reason_code"],
            end_b["actions"],
            end_b["duration_ms"],
            end_b["queue_ms"],
            end_a[0]["status"],
        )
    )


# ---- 2. 重复 / 迟到取消幂等 ----------------------------------------------------


async def test_repeated_and_late_cancel_is_idempotent(db_conn):
    journal = TurnJournal(db_conn)
    a_started = asyncio.Event()
    release_a = asyncio.Event()
    order: list[str] = []

    async def runner(ctx) -> None:  # noqa: ANN001
        order.append(ctx.turn_id)
        if len(order) == 1:
            a_started.set()
            await release_a.wait()

    manager, events = _wire(journal, runner)
    a = manager.submit("A")
    await a_started.wait()
    b = manager.submit("B")

    assert manager.cancel(b.turn_id) is True
    assert await _until(lambda: _of(events, "TURN_END", b.turn_id))
    # 重复取消：不再发 END、不改写已终态
    assert manager.cancel(b.turn_id) is False, "重复取消必须幂等回 False"
    release_a.set()
    await manager.wait(a.turn_id, timeout=5)
    # 迟到取消（A 都结束了）：仍然幂等，不复活、不重复发
    assert manager.cancel(b.turn_id) is False, "迟到取消必须幂等回 False"
    await manager.shutdown()
    await _drain(manager)

    ends = _of(events, "TURN_END", b.turn_id)
    assert len(ends) == 1, ends
    assert ends[0]["status"] == "cancelled", ends[0]
    assert ends[0]["reason_code"] == "user_stopped", ends[0]
    facts = journal.facts([b.turn_id])[b.turn_id]
    assert facts["status"] == "cancelled" and facts["reason_code"] == "user_stopped", facts
    # 关停也不能产生第二条，也不得把终态改写成 interrupted
    assert len(_of(events, "TURN_END", b.turn_id)) == 1
    assert journal.recoverable(b.turn_id) is None, "被用户取消的轮不是「重发」候选"


# ---- 3. 取消与启动竞争：已经开始执行的轮走既有停止流程 ----------------------------


async def test_cancel_of_already_started_turn_uses_stop_flow(db_conn):
    journal = TurnJournal(db_conn)
    started: list[str] = []
    first_done = asyncio.Event()
    release_second = asyncio.Event()

    async def runner(ctx) -> None:  # noqa: ANN001
        started.append(ctx.turn_id)
        if len(started) == 1:
            first_done.set()
            return
        await release_second.wait()

    manager, events = _wire(journal, runner)
    a = manager.submit("A")
    await first_done.wait()
    b = manager.submit("B")
    assert await _until(lambda: manager.active is not None and manager.active.turn_id == b.turn_id)

    assert manager.cancel(b.turn_id) is True, "已经开始的轮走 cancel_active"
    release_second.set()
    await manager.wait(b.turn_id, timeout=5)
    await manager.shutdown()
    await _drain(manager)

    ends = _of(events, "TURN_END", b.turn_id)
    assert len(ends) == 1, ends
    assert ends[0]["status"] == "cancelled", ends[0]
    assert ends[0]["reason_code"] == "user_stopped", ends[0]
    # 冻结契约 K3（2026-10-10 R7）：活动取消落台账是 cancelled（不是 interrupted），
    # resend 只认 interrupted → 给 resend 是必然 409 的死按钮；真正可用的恢复是 retry。
    # （上一轮「active 取消仍给 resend」的裁定已被本轮 K3 取代，理由见计划文档 §二 K3。）
    assert ends[0]["actions"] == ["retry"], ends[0]
    assert len(_of(events, "TURN_END", a.turn_id)) == 1


# ---- 4. 准备中（reserved）取消的既有语义不变 -------------------------------------


async def test_reserved_cancel_keeps_existing_semantics(db_conn):
    journal = TurnJournal(db_conn)
    events: list[tuple[str, dict]] = []

    async def emitter(name: str, data: dict) -> None:
        events.append((name, data))

    async def runner(ctx) -> None:  # noqa: ANN001
        raise AssertionError("准备中的预留绝不能被放行执行")

    manager = TurnManager(runner=runner, emitter=emitter)
    manager.set_journal(journal)
    reserved = manager.reserve("附件准备中", prepare_id="prep_b2")

    state, turn_id = manager.cancel_prepare("prep_b2")
    assert (state, turn_id) == ("cancelled", reserved.turn_id)
    # 幂等：同一个标识再取消仍是「已取消」
    assert manager.cancel_prepare("prep_b2") == ("cancelled", reserved.turn_id)
    await manager.shutdown()
    await _drain(manager)

    row = db_conn.execute(
        "SELECT status, reason FROM turn_journal WHERE turn_id = ?", (reserved.turn_id,)
    ).fetchone()
    assert row["status"] == "cancelled", dict(row)
    assert row["reason"] == "cancelled_during_prepare", dict(row)
    assert [n for n, _ in events if n == "TURN_START"] == [], events
