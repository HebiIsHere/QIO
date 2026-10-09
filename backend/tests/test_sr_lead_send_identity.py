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
