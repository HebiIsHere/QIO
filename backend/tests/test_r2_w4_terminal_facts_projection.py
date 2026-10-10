"""R6（W4）· 终态动作投影：成功重发之后（以及系统通知轮）不得再暴露 resend。

冻结契约（_r2-contracts-20261010.md §5 R6）：

    recoverable_now = (status == "interrupted" and notify == 0 and recovered_at IS NULL)

* "resend" in actions 且 **不是** recoverable_now：
  - status == "cancelled" → 既有的 resend→retry 归一；
  - 其它（已领取的 interrupted / notify 轮）→ **去掉 resend**（不给必然 409 的死按钮）。
* status == "interrupted" 且 actions 为空且 recoverable_now → 补 ["resend"]（既有行为）。
* 保留：并发抢占语义、重启恢复语义、cancelled 轮的 retry 归一。

缺陷（基线 6ca65f9）：facts() 的读时投影只把「cancelled + resend」归一成 retry；
一条 interrupted 行如果**持久化的 actions 里本来就写着 ["resend"]**，在 recovered_at 已被写上
（已经重发过）或 notify=1 时，投影**原样透出 resend** —— 再点必然 409。
api/server.py 的 _turn_facts_for（约 2140–2165）还有同一套归一的镜像，必须一致修。

本文件分两层钉死：
1. 数据库级：facts() 的读时投影（含「读不改写执行事实」）；
2. 真实 API 级：真实 FastAPI + 真实库 + 假 provider —— interrupt 入口可见 resend →
   首次 POST /resend 成功（200）→ 重复 POST 409 → 刷新 / 重启后再读**看不到** resend。

时序全部用事件 / 状态等待（有限超时只用于判定失败），不碰运气、不联网、不用真实 Key。
运行：cd backend; uv run --frozen --extra dev pytest tests/test_r2_w4_terminal_facts_projection.py -q
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
from agent.storage.db import connect
from agent.storage.turn_journal import TurnJournal

DECL = "[[QIO:ANSWER]]"
RESEND_REASON = "进程被掐断，这一轮没有跑完"


# ---------------------------------------------------------------- 装置


@pytest.fixture()
def journal(db_conn) -> TurnJournal:
    return TurnJournal(db_conn)


@pytest.fixture()
def app(db_conn, settings, tmp_path):
    application = create_app(settings, db_conn)
    application.state.ctx.credentials._kr = MemoryKeyring()
    # 测试污染防护：附件落点钉在 tmp_path（与 test_turn_facts_delivery 一致）
    application.state.ctx.attachments.data_dir = tmp_path / "data"
    return application


def _client(application):
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://qio.test",
        timeout=120.0,
    )


def _turn_end(application, turn_id: str) -> dict | None:
    for event in list(application.state.ctx.bus._history):
        if event.type.value != "TURN_END":
            continue
        if str(event.data.get("turn_id")) == turn_id:
            return event.data
    return None


async def _wait(predicate, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return bool(predicate())


def _seed_interrupted(
    journal: TurnJournal,
    turn_id: str,
    *,
    actions: tuple[str, ...] = ("resend",),
    notify: bool = False,
) -> None:
    """造一条「进程掐断、且**持久化的 actions 已含 resend**」的真实形状的行。

    persisted actions 含 resend 的现实来源：进程优雅退出时 TURN_END 落账
    （status=cancelled + reason=shutdown → 台账归一成 interrupted，actions=["resend"]）。
    """
    journal.accepted(turn_id=turn_id, message="被掐断的消息", topic_id="t1", notify=notify)
    journal.running(turn_id)
    journal.interrupt_stale()
    journal.record_facts(
        turn_id,
        reason_code="interrupted",
        reason=RESEND_REASON,
        stopped_by="system",
        actions=list(actions),
    )


def _raw_actions(db_conn, turn_id: str):
    row = db_conn.execute(
        "SELECT actions FROM turn_journal WHERE turn_id = ?", (turn_id,)
    ).fetchone()
    return json.loads(row["actions"]) if row and row["actions"] else []


# ------------------------------------------------- 1. 数据库级：读时投影


def test_r2_w4_claimed_interrupted_row_no_longer_projects_resend(db_conn, journal):
    """重发（一次性 claim）之后：interrupted 行不得再投影出 resend。"""
    _seed_interrupted(journal, "turn_r2_w4_recovered")

    before = journal.facts(["turn_r2_w4_recovered"])["turn_r2_w4_recovered"]
    assert before["status"] == "interrupted", before
    assert journal.recoverable("turn_r2_w4_recovered") is not None
    assert before["actions"] == ["resend"], (
        "claim 之前这行真的可恢复（准入一致），投影必须给出可用的 resend",
        before,
    )

    assert journal.claim("turn_r2_w4_recovered") is True, "装置失效：抢占没成功"

    after = journal.facts(["turn_r2_w4_recovered"])["turn_r2_w4_recovered"]
    assert after["status"] == "interrupted", after
    assert journal.recoverable("turn_r2_w4_recovered") is None
    assert "resend" not in after["actions"], (
        "已经领取过的 interrupted 行不得再给 resend —— 那是必然 409 的死按钮",
        after,
    )
    assert after["actions"] == [], after

    # 读时投影**不改写**执行事实：journal 行里的 actions 原样保留
    assert _raw_actions(db_conn, "turn_r2_w4_recovered") == ["resend"], (
        "读路径归一不得批量改写 journal 执行事实",
        _raw_actions(db_conn, "turn_r2_w4_recovered"),
    )


def test_r2_w4_notify_row_never_projects_resend(journal):
    """系统通知轮（notify=1）：即使持久化 actions 写着 resend，投影也不得透出。"""
    _seed_interrupted(journal, "turn_r2_w4_notify", notify=True)

    facts = journal.facts(["turn_r2_w4_notify"])["turn_r2_w4_notify"]
    assert facts["status"] == "interrupted", facts
    assert journal.recoverable("turn_r2_w4_notify") is None
    assert "resend" not in facts["actions"], (
        "通知轮不可被 resend 准入（409），投影不得摆出这个死按钮",
        facts,
    )
    assert facts["actions"] == [], facts


def test_r2_w4_cancelled_resend_still_normalized_to_retry(db_conn, journal):
    """回归：cancelled + resend 仍然归一成 retry（既有语义不许被本轮改坏）。"""
    journal.accepted(turn_id="turn_r2_w4_cancelled", message="用户停掉的", topic_id="t1")
    journal.running("turn_r2_w4_cancelled")
    journal.terminal("turn_r2_w4_cancelled", "cancelled", reason="user_stopped")
    journal.record_facts(
        "turn_r2_w4_cancelled",
        reason_code="user_stopped",
        reason="你停止了这一轮",
        stopped_by="user",
        actions=["resend"],
    )

    facts = journal.facts(["turn_r2_w4_cancelled"])["turn_r2_w4_cancelled"]
    assert facts["status"] == "cancelled", facts
    assert facts["actions"] == ["retry"], (
        "cancelled 轮的 resend 必须归一成真正可用的 retry",
        facts,
    )
    assert _raw_actions(db_conn, "turn_r2_w4_cancelled") == ["resend"], "journal 原样"


def test_r2_w4_unclaimed_interrupted_row_still_projects_resend(journal):
    """回归：还没被领取的 interrupted 行（actions 为空）仍然补 resend 入口。"""
    _seed_interrupted(journal, "turn_r2_w4_open", actions=())

    facts = journal.facts(["turn_r2_w4_open"])["turn_r2_w4_open"]
    assert facts["status"] == "interrupted", facts
    assert journal.recoverable("turn_r2_w4_open") is not None
    assert facts["actions"] == ["resend"], (
        "真正可恢复的 interrupted 行必须保留 resend 入口（否则刷新后入口消失）",
        facts,
    )


def test_r2_w4_projection_keeps_other_actions_for_claimed_row(journal):
    """已经被领取的 interrupted 行：只去掉 resend，其它动作原样保留。"""
    _seed_interrupted(journal, "turn_r2_w4_mixed", actions=("resend", "continue"))
    assert journal.claim("turn_r2_w4_mixed") is True

    facts = journal.facts(["turn_r2_w4_mixed"])["turn_r2_w4_mixed"]
    assert facts["actions"] == ["continue"], (
        "读时投影只摘掉点不通的 resend，不得顺手删掉别的动作",
        facts,
    )


def test_r2_w4_failed_row_loses_the_dead_resend_button(journal):
    """终态（failed）行上的 resend 同样是点不通的死按钮：去掉它，别的动作保留。"""
    journal.accepted(turn_id="turn_r2_w4_failed", message="失败的一轮", topic_id="t1")
    journal.running("turn_r2_w4_failed")
    journal.terminal("turn_r2_w4_failed", "failed", reason="provider_error")
    journal.record_facts(
        "turn_r2_w4_failed",
        reason_code="provider_error",
        reason="模型服务没有响应",
        stopped_by="system",
        actions=["retry", "resend"],
    )

    facts = journal.facts(["turn_r2_w4_failed"])["turn_r2_w4_failed"]
    assert facts["status"] == "failed", facts
    assert facts["actions"] == ["retry"], (
        "resend 的准入只认 interrupted（notify=0、未领取），失败轮上的 resend 必然 409",
        facts,
    )


def test_r2_w4_non_terminal_row_actions_untouched(journal):
    """保护既有语义：非终态（running）行的事实原样读回，投影不介入。"""
    journal.accepted(turn_id="turn_r2_w4_running", message="还在跑", topic_id="t1")
    journal.running("turn_r2_w4_running")
    journal.record_facts(
        "turn_r2_w4_running",
        reason_code=None,
        reason=None,
        stopped_by=None,
        actions=["retry", "resend"],
    )

    facts = journal.facts(["turn_r2_w4_running"])["turn_r2_w4_running"]
    assert facts["status"] == "running", facts
    assert facts["actions"] == ["retry", "resend"], (
        "投影只针对终态动作表（含 interrupted）；还没到终态的 running 行不介入",
        facts,
    )


# ------------------------------------------- 2. 真实 API：重发 → 刷新 / 重启


async def test_r2_w4_resend_then_refresh_and_restart_hide_resend(app, settings, tmp_path):
    """真实 API：入口可见 → 首次重发 200 → 重复 409 → 刷新 / 重启后读不到 resend。"""
    ctx = app.state.ctx
    topic = ctx.current_topic()
    original = "turn_r2_w4_api_recovered"

    ctx.turn_journal.accepted(turn_id=original, message="进程掐断的消息", topic_id=topic)
    ctx.turn_journal.running(original)
    ctx.turn_journal.interrupt_stale()
    ctx.turn_journal.record_facts(
        original,
        reason_code="interrupted",
        reason=RESEND_REASON,
        stopped_by="system",
        actions=["resend"],
    )
    # 这一轮的用户消息真的在历史里（进程掐断前已经写进历史）——刷新后历史页仍会带它的事实
    ctx.memory.append_message(
        topic_id=topic,
        role="user",
        content="进程掐断的消息",
        content_type="text",
        model="fake-stream",
        turn_id=original,
    )

    adapter = FakeStreamAdapter([StreamScript(text_chunks=[DECL + "\n", "重发成功。"])])
    ctx.build_adapter = AsyncMock(return_value=adapter)

    async with _client(app) as client:
        # ① 重发之前：这一轮在入口里，动作表给出真正可用的 resend
        state = (await client.get("/api/runtime/state")).json()
        before = {row["turn_id"]: row for row in state.get("turn_facts") or []}.get(original)
        assert before is not None, ("重发之前快照必须带这一轮的事实", state.get("turn_facts"))
        assert before["status"] == "interrupted", before
        assert before["actions"] == ["resend"], before

        # ② 首次重发成功（真实端点、真实 TurnManager、假 provider）
        first = await client.post("/api/turns/%s/resend" % original)
        assert first.status_code == 200, first.text[:300]
        new_turn = str(first.json()["turn_id"])
        assert new_turn != original
        assert await _wait(lambda: _turn_end(app, new_turn) is not None), "重发的轮没有跑完"

        # ③ 一次性：重复 POST 仍然 409（准入不动）
        again = await client.post("/api/turns/%s/resend" % original)
        assert again.status_code == 409, ("resend 必须一次性", again.status_code)

        # ④ 刷新（同一进程）：历史页仍带这一轮的事实，但**不得**再有 resend
        page = (await client.get("/api/session/context")).json()
        after = {row["turn_id"]: row for row in page.get("turn_facts") or []}.get(original)
        assert after is not None, (
            "刷新后历史页必须仍带回这一轮的事实（否则这次用例根本没覆盖住路径）",
            page.get("turn_facts"),
        )
        assert after["status"] == "interrupted", after
        assert "resend" not in after["actions"], (
            "重发成功后刷新仍看到 resend —— 那就是一个点了必然 409 的死按钮",
            after,
        )

        # ⑤ 快照入口也不再列为「未处理」（既有语义）
        state_after = (await client.get("/api/runtime/state")).json()
        assert original not in {
            row["turn_id"] for row in state_after.get("interrupted_turns") or []
        }, state_after.get("interrupted_turns")

    # ⑥ 重启（同一份库、新连接、新 AppContext）后再读：仍然看不到 resend
    conn2 = connect(tmp_path / "test.db")
    try:
        app2 = create_app(settings, conn2)
        app2.state.ctx.credentials._kr = MemoryKeyring()
        async with _client(app2) as client2:
            page2 = (await client2.get("/api/session/context")).json()
            restarted = {row["turn_id"]: row for row in page2.get("turn_facts") or []}.get(
                original
            )
            assert restarted is not None, (
                "重启后历史页仍要带回这一轮的事实",
                page2.get("turn_facts"),
            )
            assert "resend" not in restarted["actions"], (
                "重启后仍然看到 resend（后端权威事实没有收敛）",
                restarted,
            )
            assert TurnJournal(conn2).facts([original])[original]["actions"] == [], (
                "重启后台账读时投影仍给 resend"
            )
    finally:
        conn2.close()

    # 执行事实没有被读路径改写；准入仍是一次性
    row = ctx.conn.execute(
        "SELECT actions, recovered_at FROM turn_journal WHERE turn_id = ?", (original,)
    ).fetchone()
    assert json.loads(row["actions"]) == ["resend"], ("读路径不得改写 journal", dict(row))
    assert row["recovered_at"] is not None, dict(row)


async def test_r2_w4_notify_turn_facts_over_api_never_expose_resend(app):
    """真实 API：系统通知轮即使持久化了 resend，也不得在历史事实里透出。"""
    ctx = app.state.ctx
    topic = ctx.current_topic()
    notify_turn = "turn_r2_w4_api_notify"

    ctx.turn_journal.accepted(
        turn_id=notify_turn, message="系统通知轮", topic_id=topic, notify=True
    )
    ctx.turn_journal.running(notify_turn)
    ctx.turn_journal.interrupt_stale()
    ctx.turn_journal.record_facts(
        notify_turn,
        reason_code="interrupted",
        reason=RESEND_REASON,
        stopped_by="system",
        actions=["resend"],
    )
    ctx.memory.append_message(
        topic_id=topic,
        role="assistant",
        content="系统通知轮",
        content_type="text",
        model="fake-stream",
        turn_id=notify_turn,
    )

    async with _client(app) as client:
        page = (await client.get("/api/session/context")).json()
        row = {item["turn_id"]: item for item in page.get("turn_facts") or []}.get(notify_turn)
        assert row is not None, ("通知轮的事实仍要可读", page.get("turn_facts"))
        assert row["status"] == "interrupted", row
        assert "resend" not in row["actions"], (
            "通知轮不可被 resend 准入；投影透出 resend 就是死按钮",
            row,
        )

        refused = await client.post("/api/turns/%s/resend" % notify_turn)
        assert refused.status_code == 409, ("通知轮准入保持不变", refused.status_code)
