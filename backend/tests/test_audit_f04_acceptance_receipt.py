"""审计 F04（Lead 侧）：受理事实必须如实、且四种状态可区分。

背景：`POST /api/turns` 的 request_id 去重分支曾经**无条件** accepted=True。
一次在准备期就被放弃、从未放行执行的发送，同身份重试时会得到「已受理」——
用户以为模型看过这条消息。这里的断言全部是**正向契约**（修复后的正确结果），
不是「断言缺陷发生」。
"""
from __future__ import annotations

import pytest

from agent.core.turn import TurnManager


def _tm() -> TurnManager:
    tm = TurnManager()
    tm.set_runner(lambda ctx: None)
    return tm


async def test_never_activated_abandon_is_not_accepted():
    """准备期放弃（从未放行）=> accepted=False + cancelled=True + 明确原因。"""
    tm = _tm()
    ctx = tm.reserve("准备期就被放弃的原文", None, prepare_id="prep-1", request_id="req-a")
    tm.abandon(ctx, reason="cancelled_during_prepare")
    facts = tm.acceptance_facts(ctx, deduped=True)
    assert facts["state"] == "not_accepted"
    assert facts["accepted"] is False, "从未放行执行的轮次不得被报成已受理"
    assert facts["cancelled"] is True
    assert facts["reason"] == "cancelled_during_prepare", "必须给出为什么没被受理"
    assert facts["deduped"] is True
    assert facts["attachment_ids"] == []
    await tm.shutdown()


async def test_activated_then_cancelled_is_distinguished_from_never_activated():
    """曾受理后又取消 != 从未放行：两者都必须可区分（不能只看 status=cancelled）。"""
    import asyncio

    started = asyncio.Event()
    release = asyncio.Event()

    async def _runner(ctx):
        started.set()
        await release.wait()

    tm = TurnManager()
    tm.set_runner(_runner)
    ctx = tm.submit("正常提交然后被取消", request_id="req-b")
    await asyncio.wait_for(started.wait(), 5)
    assert tm.cancel_active() is True
    facts = tm.acceptance_facts(ctx)
    assert facts["state"] == "cancelled_after_accept"
    assert facts["accepted"] is True, "它曾经被受理过，这一点不能被抹掉"
    assert facts["cancelled"] is True
    assert facts["reason"] == "cancelled_after_accept"
    release.set()
    await tm.shutdown()


async def test_preparing_is_neither_accepted_nor_rejected():
    """预留中（还没放行）=> preparing：既不能当成功，也不能当失败。"""
    tm = _tm()
    ctx = tm.reserve("还在准备", None, prepare_id="prep-2", request_id="req-c")
    facts = tm.acceptance_facts(ctx, deduped=True)
    assert facts["state"] == "preparing"
    assert facts["accepted"] is False and facts["cancelled"] is False
    assert facts["pending"] is True
    await tm.shutdown()


async def test_accepted_records_really_bound_attachment_ids():
    """去重回执必须拿得到**原请求真实绑定**的附件 id。"""
    tm = _tm()
    ctx = tm.submit("带附件的发送", request_id="req-d")
    ctx.bound_attachment_ids = ["att-1", "att-2"]
    facts = tm.acceptance_facts(ctx, deduped=True)
    assert facts["state"] == "accepted"
    assert facts["accepted"] is True and facts["cancelled"] is False
    assert facts["attachment_ids"] == ["att-1", "att-2"]
    await tm.shutdown()


def test_dedup_returns_original_request_facts_not_new_body(db_conn, settings):
    """真实 HTTP：同身份重试返回**原请求**的真实记录与受理事实。"""
    from fastapi.testclient import TestClient

    from agent.api.server import create_app
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app, raise_server_exceptions=False) as c:
        ctx = app.state.ctx
        rid = "audit-f04-dedup-normal"
        r1 = c.post(
            "/api/turns",
            json={"message": "原请求的原文", "topic_id": "T1", "client_request_id": rid},
        )
        assert r1.status_code == 200, r1.text
        first = r1.json()
        assert first["accepted"] is True and first.get("cancelled") is False
        assert first["receipt"]["state"] == "accepted"

        # 同身份重试：不产生第二条轮次；话题/原文取原请求，不随后来的输入重算
        r2 = c.post(
            "/api/turns",
            json={"message": "后来在别的输入框里打的字", "topic_id": "T2", "client_request_id": rid},
        )
        assert r2.status_code == 200, r2.text
        again = r2.json()
        assert again["turn_id"] == first["turn_id"]
        assert again["deduplicated"] is True
        assert again["accepted"] is True
        assert again["message"] == "原请求的原文", "重试不得用当前输入框的内容替换原请求语义"
        assert again["topic_id"] == "T1", "重试不得用当前话题替换原请求话题"

        # 查证端点给出同一份受理事实（前后端口径一致）
        r3 = c.get(f"/api/turns/by-request/{rid}")
        assert r3.status_code == 200, r3.text
        assert r3.json()["receipt"] == again["receipt"]


def test_dedup_after_preparation_abandon_is_honest(db_conn, settings):
    """真实 HTTP：准备期放弃后同身份重试 => accepted=false + 原因（不是「已受理」）。"""
    from fastapi.testclient import TestClient

    from agent.api.server import create_app
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app, raise_server_exceptions=False) as c:
        ctx = app.state.ctx
        rid = "audit-f04-prep-abandon"
        # 真实路径是「预留 → 准备期间被取消 → abandon」；这里直接构造那一步的结果
        # （同一 request_id 已绑定一轮、且**从未放行**）。
        turn = ctx.turns.reserve("准备期被放弃的原文", "T9", prepare_id="prep-f04", request_id=rid)
        ctx.turns.abandon(turn, reason="cancelled_during_prepare")

        r = c.post(
            "/api/turns",
            json={"message": "准备期被放弃的原文", "topic_id": "T9", "client_request_id": rid},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["turn_id"] == turn.turn_id, "同身份重试不得新建第二条轮次"
        assert data["deduplicated"] is True
        assert data["accepted"] is False, "从未放行执行却在去重回执里声称已受理（F04）"
        assert data["cancelled"] is True
        assert data["receipt"]["state"] == "not_accepted"
        assert data["receipt"]["reason"] == "cancelled_during_prepare"

        # 查证端点必须给出同一份事实（前端重开后据此显示「未受理」而不是「未知/已发送」）
        r2 = c.get(f"/api/turns/by-request/{rid}")
        assert r2.status_code == 200, r2.text
        checked = r2.json()
        assert checked["accepted"] is False
        assert checked["cancelled"] is True
        assert checked["receipt"]["state"] == "not_accepted"
        assert checked["message"] == "准备期被放弃的原文"
