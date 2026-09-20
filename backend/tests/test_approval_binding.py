"""审批必须绑定 turn/session、有寿命、只能消费一次。"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from agent.api.bus import EventBus
from agent.api.server import create_app
from agent.tools.approval import ApprovalService, request_digest


def _parse(chunks: list[str]) -> list[dict]:
    events = []
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


async def _publish_and_collect(bus: EventBus) -> list[str]:
    collected: list[str] = []

    async def consume():
        async for chunk in bus.stream():
            collected.append(chunk)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    return collected


async def test_request_is_bound_and_round_trips_once():
    bus = EventBus()
    service = ApprovalService(bus, timeout_seconds=5.0)
    service.set_context(turn_id="turn_1", session_id="sess_1")
    collected: list[str] = []

    async def consume():
        async for chunk in bus.stream():
            collected.append(chunk)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)

    pending = asyncio.create_task(service.request("computer", {"action": "read", "path": "x"}))
    await asyncio.sleep(0.05)
    events = _parse(collected)
    required = [e for e in events if e["type"] == "APPROVAL_REQUIRED"]
    assert len(required) == 1
    approval = required[0]["data"]["approval"]
    assert approval["turn_id"] == "turn_1"
    assert approval["session_id"] == "sess_1"
    assert approval["expires_at"]
    assert approval["request_digest"] == request_digest("computer", {"action": "read", "path": "x"})

    assert await service.respond(approval["approval_id"], "approved") is True
    result = await asyncio.wait_for(pending, timeout=2)
    assert result.decision == "approved"

    # 单次使用：同一 id 再应答必须失败
    assert await service.respond(approval["approval_id"], "approved") is False
    task.cancel()


async def test_unknown_approval_id_is_rejected():
    service = ApprovalService(EventBus(), timeout_seconds=5.0)
    assert await service.respond("appr_does_not_exist", "approved") is False


async def test_expired_approval_cannot_be_answered():
    service = ApprovalService(EventBus(), timeout_seconds=5.0)
    service.set_context(turn_id="turn_x")
    pending = asyncio.create_task(service.request("computer", {"action": "read"}))
    await asyncio.sleep(0.05)
    approval_id = next(iter(service._requests))
    # 直接把寿命推到过去：模拟「用户隔太久才点」的过期路径
    service._requests[approval_id].expires_at = "2000-01-01T00:00:00+00:00"
    assert await service.respond(approval_id, "approved") is False
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending


async def test_wrong_turn_or_session_or_digest_is_rejected():
    service = ApprovalService(EventBus(), timeout_seconds=5.0)
    service.set_context(turn_id="turn_a", session_id="sess_a")
    pending = asyncio.create_task(service.request("computer", {"action": "read"}))
    await asyncio.sleep(0.05)
    approval_id = next(iter(service._requests))

    with pytest.raises(ValueError):
        await service.respond(approval_id, "approved", turn_id="turn_b")
    with pytest.raises(ValueError):
        await service.respond(approval_id, "approved", session_id="sess_b")
    with pytest.raises(ValueError):
        await service.respond(approval_id, "approved", digest="deadbeef")
    with pytest.raises(ValueError):
        await service.respond(approval_id, "maybe")

    # 被拒绝的尝试不能把审批消费掉
    assert await service.respond(approval_id, "approved", turn_id="turn_a") is True
    await asyncio.wait_for(pending, timeout=2)


async def test_timeout_returns_timeout_and_is_single_use():
    service = ApprovalService(EventBus(), timeout_seconds=0.05)
    result = await service.request("computer", {"action": "read"})
    assert result.decision == "timeout"


# ---------------------------------------------------------------------------
# 真实 HTTP 路径：绑定校验必须真的执行（而不只是服务层有这段代码）
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(db_conn, settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def test_api_forwards_binding_fields_to_service(client, monkeypatch):
    """HTTP 层必须把 turn_id / session_id / request_digest 真的传给服务。"""
    approvals = client.app.state.ctx.approvals
    seen: dict = {}

    async def fake_respond(approval_id, decision, scope=None, overrides=None, **kwargs):
        seen["approval_id"] = approval_id
        seen["decision"] = decision
        seen["kwargs"] = kwargs
        return True

    monkeypatch.setattr(approvals, "respond", fake_respond)
    resp = client.post(
        "/api/approvals/appr_x/respond",
        json={
            "decision": "approved",
            "turn_id": "turn_a",
            "session_id": "sess_a",
            "request_digest": "digest_a",
        },
    )
    assert resp.status_code == 200
    assert seen["approval_id"] == "appr_x"
    assert seen["kwargs"] == {
        "turn_id": "turn_a",
        "session_id": "sess_a",
        "digest": "digest_a",
    }


def test_api_rejects_mismatched_approval_binding(client):
    """一次批准只能批准它原本对应的那一次具体请求。"""
    approvals = client.app.state.ctx.approvals

    async def arm():
        approvals.set_context(turn_id="turn_a", session_id="sess_a")
        task = asyncio.create_task(approvals.request("computer", {"action": "read", "path": "x"}))
        await asyncio.sleep(0.05)
        return next(iter(approvals._requests)), task

    approval_id, task = client.portal.call(arm)
    digest = request_digest("computer", {"action": "read", "path": "x"})

    # 别的 turn 拿这次批准 → 拒绝
    resp = client.post(
        f"/api/approvals/{approval_id}/respond",
        json={"decision": "approved", "turn_id": "turn_b"},
    )
    assert resp.status_code == 400

    # 请求摘要不一致（批准 A 却想执行 B）→ 拒绝
    resp = client.post(
        f"/api/approvals/{approval_id}/respond",
        json={"decision": "approved", "request_digest": "deadbeef"},
    )
    assert resp.status_code == 400

    # 绑定正确 → 正常批准，用户操作方式没有任何变化
    resp = client.post(
        f"/api/approvals/{approval_id}/respond",
        json={"decision": "approved", "turn_id": "turn_a", "request_digest": digest},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    assert client.portal.call(lambda: task)
