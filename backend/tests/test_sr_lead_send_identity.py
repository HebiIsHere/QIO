"""契约 5 Lead 侧测试：client_request_id 幂等受理与查证端点。"""
from __future__ import annotations

import pytest

from agent.core.turn import TurnManager


async def test_lookup_request_roundtrip():
    tm = TurnManager()

    async def _runner(ctx):
        return None

    tm.set_runner(_runner)
    ctx = tm.submit("你好", request_id="req-1")
    tm.submit("你好", request_id="req-2")
    found = tm.lookup_request("req-1")
    assert found is not None and found.turn_id == ctx.turn_id
    assert tm.lookup_request("missing") is None
    assert tm.lookup_request("") is None
    await tm.shutdown()


async def test_lookup_request_bounded():
    tm = TurnManager()
    tm.set_runner(lambda ctx: None)
    for i in range(400):
        tm.submit(f"m{i}", request_id=f"req-{i}")
    # 超出上限的最老条目被丢弃（上限 300）
    assert tm.lookup_request("req-0") is None
    assert tm.lookup_request("req-399") is not None
    await tm.shutdown()


async def test_start_turn_idempotent_by_request_id():
    # 幂等数据面：同一 request_id 只映射到同一轮；API 层的幂等短路（不提交第二条
    # turn）与 404 语义由集成验收（真实 HTTP）覆盖，这里钉住核心数据结构。
    tm = TurnManager()
    ran: list[str] = []

    async def _runner(ctx):
        ran.append(ctx.turn_id)

    tm.set_runner(_runner)
    ctx1 = tm.submit("同一个意图", request_id="req-x")
    ctx2 = tm.lookup_request("req-x")
    assert ctx2 is not None and ctx2.turn_id == ctx1.turn_id
    assert ctx1.request_id == "req-x"
    await tm.shutdown()
    # 结束后本进程仍能查到（查证 200）；重启后为空则由端点 404 语义覆盖
    assert tm.lookup_request("req-x") is not None


def test_api_turns_idempotent_and_by_request_endpoint(db_conn, settings):
    """真实 HTTP：POST /api/turns 幂等受理（client_request_id 复用同一个 turn）；
    by-request 查证端点命中 200 / 未知 404（unknown:true）。
    """
    from fastapi.testclient import TestClient

    from agent.api.server import create_app
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app, raise_server_exceptions=False) as c:
        body = {"message": "契约5 端到端 idempotent", "client_request_id": "sr-lead-e2e-1"}
        r1 = c.post("/api/turns", json=body)
        assert r1.status_code == 200, r1.text
        data1 = r1.json()
        assert data1.get("ok") and data1.get("accepted")
        turn_id = data1["turn_id"]

        # 同一 request_id 再提交：幂等命中同一 turn，不产生第二条
        r2 = c.post("/api/turns", json=body)
        assert r2.status_code == 200, r2.text
        data2 = r2.json()
        assert data2["turn_id"] == turn_id
        assert data2.get("deduplicated") is True

        # 查证端点：受理曾在 → 200；陌生 request_id → 404 unknown
        r3 = c.get("/api/turns/by-request/sr-lead-e2e-1")
        assert r3.status_code == 200
        assert r3.json()["turn_id"] == turn_id
        r4 = c.get("/api/turns/by-request/sr-lead-never-existed")
        assert r4.status_code == 404
        assert r4.json() == {"ok": False, "unknown": True}

        # 不带 request_id 的提交不受影响（原语义保留）
        r5 = c.post("/api/turns", json={"message": "无身份的普通发送"})
        assert r5.status_code == 200
        assert r5.json().get("ok")
