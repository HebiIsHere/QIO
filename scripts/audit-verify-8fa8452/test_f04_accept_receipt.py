r"""E 路独立核验探针 · F04 准备期已取消的轮次被误报为已受理
基线 8fa845255c118a7104b1de8e37298ca745924482

规格来源：D:\qio-dev\_audit-findings-8fa8452.md 的 F04 段，以及 Lead 冻结的受理事实契约：
  { state: 'accepted'|'preparing'|'not_accepted'|'cancelled_after_accept',
    accepted, cancelled, pending, reason, deduped, turn_id, status, attachment_ids }
  - 准备期放弃且从未激活 => state=not_accepted, accepted=false, cancelled=true；
  - 曾放行执行后被取消  => state=cancelled_after_accept, accepted=true, cancelled=true；
  - 仍在准备            => state=preparing, accepted=false, pending=true。
两个端点（POST /api/turns 与 GET /api/turns/by-request/{id}）必须给出一致的受理事实。

**探针自身的教训（第一版踩过的坑，记录在此以免复现）**：第一版先用 POST 走完整路由让
turn 激活，再调 abandon() 模拟放弃 —— 但那条 turn 的 activated 已经是 True，于是它确实
属于「曾受理后取消」，探针的期望本身错了。真正要构造的是**从未放行执行**的轮次：
用 ctx.turns.reserve(...)（= 路由里「预留但未 activate」的同一状态）+ cancel_prepare(...)
（= 取消端点与断连取消走的同一条真实路径）。
对「曾受理后取消」，则应走**已经 activate 之后**再取消，断言 state=cancelled_after_accept。

反例设计（区分缺陷与修复的关键）：
  F04-a 从未放行的准备期取消 + 同身份重试 → 必须 not_accepted（基线无条件 accepted=true）。
  F04-b by-request 对同一条请求 → 必须给出同一份受理事实（基线只回 ok/turn_id/status）。
  F04-c 曾放行执行的轮次取消 + 同身份重试 → 必须 cancelled_after_accept（与 a 可区分）。
  F04-d 仍在准备（预留未放行，未取消）→ 必须 state=preparing, pending=true。
  F04-e 回归保护：正常受理过的请求重复提交仍 accepted + deduplicated，且不产生第二条轮次。

运行（只跑本节点，禁止全量）：
  cd <被测 worktree>\backend
  $env:PYTHONPATH = '<被测 worktree>\backend\src'
  & 'D:\qio-dev\qa-lead\backend\.venv\Scripts\python.exe' -m pytest <本文件> -q -p no:cacheprovider -s -o asyncio_mode=auto

注：探针在 backend/tests 之外 → pyproject 的 pythonpath / asyncio_mode 都不生效，必须显式给出。
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.pop("QIO_DATA_DIR", None)
os.environ.setdefault("QIO_DEV_INSECURE", "1")
os.environ.setdefault("QIO_DISABLE_DB_CHECK", "1")

import httpx  # noqa: E402
import pytest  # noqa: E402

from agent.api.server import create_app  # noqa: E402
from agent.config import Settings  # noqa: E402
from agent.storage.db import connect  # noqa: E402
from agent.storage.migrate import apply_migrations  # noqa: E402

PREPARE_REASON = "cancelled_during_prepare"


@pytest.fixture()
def app(tmp_path: Path):
    conn = connect(tmp_path / "test.db")
    apply_migrations(conn)
    application = create_app(Settings(data_dir=tmp_path / "data"), conn)
    try:
        from agent.credentials.store import MemoryKeyring

        application.state.ctx.credentials._kr = MemoryKeyring()
    except Exception:  # noqa: BLE001 - 取不到不影响本探针
        pass
    yield application
    conn.close()


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://qio.test", timeout=60.0
    )


def _facts(body: dict) -> dict:
    """统一的受理事实：优先取 receipt，退化到顶层字段。"""
    receipt = body.get("receipt")
    if isinstance(receipt, dict):
        return receipt
    return {k: body.get(k) for k in ("state", "accepted", "cancelled", "pending", "reason", "deduped")}


async def _wait_for(predicate, timeout: float = 20.0, step: float = 0.05):
    """等一个条件成立（有界），返回它最后一次取值。"""
    import asyncio
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(step)
    return predicate()


def _fake_adapter_builder(adapter):
    """把假 adapter 装进 ctx.build_adapter（与既有测试同一手法）。"""

    async def _build(*_args, **_kwargs):
        return adapter

    return _build


def _reserve(app, rid: str, prepare_id: str, message: str = "准备期取消的文字"):
    """构造「预留但从未 activate」的轮次 —— 与路由 reserve() 之后、activate() 之前同一状态。"""
    return app.state.ctx.turns.reserve(message, None, prepare_id=prepare_id, request_id=rid)


async def test_f04_a_never_activated_cancel_then_same_identity_retry(app) -> None:
    rid, prep = "e-f04-never-1", "e-prep-1"
    turn = _reserve(app, rid, prep)
    state, tid = app.state.ctx.turns.cancel_prepare(prep)
    print("\n[F04-a] cancel_prepare ->", state, tid, " turn.status =", turn.status,
          " activated =", turn.activated)
    assert state == "cancelled", "前置条件：准备期取消必须真的取消掉这一轮"

    async with _client(app) as client:
        resp = await client.post(
            "/api/turns",
            json={"message": "准备期取消的文字", "attachment_ids": [], "client_request_id": rid},
        )
        body = resp.json()
    print("[F04-a] 同身份重试 POST =", resp.status_code, body)
    facts = _facts(body)
    print("[F04-a] 受理事实 =", facts)

    assert body.get("accepted") is False, (
        "F04 缺陷复现：准备期就被取消、从未放行执行的轮次，同身份重试被报成 accepted=true。"
        "body=" + str(body)
    )
    assert body.get("cancelled") is True, "F04：必须 cancelled=true。body=" + str(body)
    assert facts.get("state") == "not_accepted", (
        "F04：从未放行就取消必须 state=not_accepted（与「曾受理后取消」可区分）。facts=" + str(facts)
    )
    assert facts.get("reason"), "F04：必须给出「为什么没被受理」（reason 不能为空）。facts=" + str(facts)


async def test_f04_b_by_request_reports_same_facts(app) -> None:
    rid, prep = "e-f04-never-2", "e-prep-2"
    app.state.ctx.turns.reserve("查证用", None, prepare_id=prep, request_id=rid)
    app.state.ctx.turns.cancel_prepare(prep)

    async with _client(app) as client:
        resp = await client.get("/api/turns/by-request/" + rid)
        body = resp.json()
    print("\n[F04-b] by-request =", resp.status_code, body)
    facts = _facts(body)
    assert body.get("accepted") is False, (
        "F04 缺陷复现：by-request 没有如实表达「从未受理」。body=" + str(body)
    )
    assert body.get("cancelled") is True, "F04：by-request 必须 cancelled=true。body=" + str(body)
    assert facts.get("state") == "not_accepted", (
        "F04：by-request 的受理事实必须与 POST 一致（state=not_accepted）。facts=" + str(facts)
    )


async def test_f04_c_cancelled_after_accept_is_distinguishable(app) -> None:
    """已经放行（activate）之后被用户停止 → cancelled_after_accept，绝不能与「从未受理」混为一谈。

    走**真实路径**：假 adapter 把流钉在第二段，POST 之后轮次真的在跑；用
    POST /api/turns/{turn_id}/cancel 停止它（= 用户按停止）；然后再用同一身份重试。
    """
    import asyncio

    from agent.adapters.fake import FakeStreamAdapter, StreamScript

    rid = "e-f04-after-3"
    hold = asyncio.Event()
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                text_chunks=["[[QIO:ANSWER]]\n", "第一段。", "第二段。"],
                hold_after=2,
                hold=hold,
                gap_ms=40,
            )
        ]
    )
    app.state.ctx.build_adapter = _fake_adapter_builder(adapter)

    async with _client(app) as client:
        first = await client.post(
            "/api/turns", json={"message": "已受理后取消", "attachment_ids": [], "client_request_id": rid}
        )
        assert first.status_code == 200, first.json()
        turn_id = first.json()["turn_id"]
        ctx = await _wait_for(lambda: app.state.ctx.turns.lookup_request(rid), timeout=20.0)
        assert ctx is not None, "前置条件：必须能按请求身份查到这一轮"
        started = await _wait_for(lambda: bool(getattr(ctx, "activated", False)), timeout=20.0)
        assert started, "前置条件：这一轮必须已经被放行执行（activated）"
        cancelled = await client.post(f"/api/turns/{turn_id}/cancel")
        print("[F04-c] cancel 回执 =", cancelled.status_code, cancelled.json())
        hold.set()
        await _wait_for(lambda: ctx.status in ("cancelled", "completed", "failed", "unavailable"), timeout=20.0)
        print("[F04-c] 取消后 ctx.status =", ctx.status, " activated =", ctx.activated)
        second = await client.post(
            "/api/turns", json={"message": "已受理后取消", "attachment_ids": [], "client_request_id": rid}
        )
        body = second.json()
    facts = _facts(body)
    print("\n[F04-c] 曾受理后取消的同身份重试 =", body)
    assert facts.get("state") == "cancelled_after_accept", (
        "F04：已经放行执行过的轮次被取消，必须报 cancelled_after_accept（accepted 仍为 true）。facts="
        + str(facts)
    )
    assert body.get("accepted") is True and body.get("cancelled") is True, (
        "F04：曾受理后取消 = accepted=true 且 cancelled=true。body=" + str(body)
    )


async def test_f04_d_still_preparing_is_pending_not_failure(app) -> None:
    """预留未放行、也未取消 → preparing/pending，既不能说成功也不能说失败。"""
    rid, prep = "e-f04-prep-4", "e-prep-4"
    app.state.ctx.turns.reserve("还在准备", None, prepare_id=prep, request_id=rid)
    async with _client(app) as client:
        resp = await client.post(
            "/api/turns", json={"message": "还在准备", "attachment_ids": [], "client_request_id": rid}
        )
        body = resp.json()
    facts = _facts(body)
    print("\n[F04-d] 仍在准备的同身份重试 =", body)
    assert facts.get("state") == "preparing", (
        "F04：仍在准备的轮次必须报 preparing，不能提前说成功或失败。facts=" + str(facts)
    )
    assert body.get("accepted") is False and facts.get("pending") is True, (
        "F04：preparing 必须 accepted=false + pending=true。body=" + str(body)
    )


async def test_f04_e_normal_dedup_regression(app) -> None:
    rid = "e-f04-normal-5"
    async with _client(app) as client:
        first = await client.post(
            "/api/turns", json={"message": "正常受理", "attachment_ids": [], "client_request_id": rid}
        )
        assert first.status_code == 200, first.json()
        second = await client.post(
            "/api/turns", json={"message": "正常受理", "attachment_ids": [], "client_request_id": rid}
        )
        body = second.json()
    print("\n[F04-e] 正常重复提交 =", body)
    assert body.get("turn_id") == first.json().get("turn_id"), "同身份重复提交不得创建第二条轮次"
    assert body.get("deduplicated") is True, "F04：必须标记 deduplicated"
    assert body.get("accepted") is True, "F04：正常受理过的请求重复提交仍 accepted=true"
