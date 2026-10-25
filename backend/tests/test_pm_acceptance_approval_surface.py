# -*- coding: utf-8 -*-
"""E 独立验收 · 审批生命周期（HTTP 层，真实 app + 临时数据库）。

验的是**用户可见语义**，不是实现细节：
- 批准 ≠ 完成：批准后、执行器真正返回之前，pending 里必须还能查到 approved / executing，
  历史里不能提前出现 executed；
- 执行结果落历史（GET /api/approvals/history），按 approval_id 归属，两个并发审批互不覆盖；
- 内部数据三次确认：第 1/2 次确认绝不触发执行，第 3 次才执行（由后端状态机决定）。

不依赖真实模型 / Key。数据目录是 tmp_path 下的隔离库（db_conn fixture）。
运行：cd backend && uv run --frozen pytest tests/test_pm_acceptance_approval_surface.py -q
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.security.operations import ExecResult, PendingOperation


@pytest.fixture()
def client(db_conn, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _submit(client, *, kind="test.op", label="验收操作", confirmations=1, path="C:\\work\\accept.txt"):
    async def _do():
        ctx = client.app.state.ctx
        operation = PendingOperation(
            kind=kind,
            label=label,
            payload={"value": 1},
            effect={"kind": "write", "targets": [{"kind": "path", "value": path}]},
            fingerprint={"path": path, "sha256": "accept"},
            instruction_id="instr_pm_accept",
            instruction_text="把验收文件写成 X",
            purpose="E 独立验收",
            confirmations_required=confirmations,
            source="tool",
        )
        if getattr(ctx, "requirements", None) is not None:
            requirement = ctx.requirements.record_user_message(
                text="把验收文件写成 X", message_id="msg_pm_accept", turn_id="turn_pm_accept"
            )
            operation.requirement_id = requirement.requirement_id
            operation.requirement_version = int(getattr(requirement, "version", 1) or 1)
        return await ctx.approvals.submit(operation)

    return client.portal.call(_do)


def _row(client, approval_id):
    return client.app.state.ctx.approvals.store.get(approval_id)


def _wait_state(client, approval_id, state, *, timeout=8.0):
    deadline = time.time() + timeout
    row = _row(client, approval_id)
    while time.time() < deadline:
        row = _row(client, approval_id)
        if row is not None and row["state"] == state:
            return row
        client.portal.call(lambda: asyncio.sleep(0.02))
    return row


def _pending_ids(client):
    return {item["approval_id"] for item in client.get("/api/approvals/pending").json()["approvals"]}


def _history(client, approval_id):
    rows = client.get("/api/approvals/history", params={"limit": 50}).json()["history"]
    return [row for row in rows if row.get("approval_id") == approval_id]


def _register_gated_executor(client, gate: threading.Event, counter: list, *, kind="test.op"):
    async def handler(payload, ctx):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, gate.wait, 8.0)
        counter.append(payload)
        return ExecResult(ok=True, summary="验收执行完成")

    client.app.state.ctx.operations.register(kind, handler, replace=True)


def _record_events(client):
    events: list = []
    ctx = client.app.state.ctx

    async def _install():
        original = ctx.bus.publish

        async def spy(event):
            events.append(event)
            await original(event)

        ctx.bus.publish = spy

    client.portal.call(_install)
    return events


def test_approved_is_not_completed_and_result_lands_in_history(client):
    """批准后延迟执行：approved / executing 期间不得显示成已完成，最终结果落历史。"""
    gate = threading.Event()
    counter: list = []
    _register_gated_executor(client, gate, counter)
    events = _record_events(client)

    ticket = _submit(client, label="延迟执行验收")
    approve = client.post(f"/api/approvals/{ticket.approval_id}/decide", json={"decision": "approve"})
    assert approve.status_code == 200, approve.text

    # 执行器还没返回：pending 里必须还能查到这一条，状态只能是 approved / executing
    raw = client.get("/api/approvals/pending").json()["approvals"]
    mine = [item for item in raw if item["approval_id"] == ticket.approval_id]
    assert mine, "批准后执行完成前，pending 不应把这一条丢掉（界面会以为已经结束）"
    assert mine[0]["state"] in ("approved", "executing"), mine[0]["state"]
    assert _history(client, ticket.approval_id) == [], "执行器还没返回就写了执行历史（提前报成功）"

    gate.set()
    row = _wait_state(client, ticket.approval_id, "completed")
    assert row["state"] == "completed"
    history = _history(client, ticket.approval_id)
    assert history, "执行完成后历史里查不到结果"
    assert history[0]["outcome"] == "executed"
    assert counter, "执行器没有被真实调用"

    phases = [
        str(event.data.get("phase") or "")
        for event in events
        if event.type.value in ("APPROVAL_REQUIRED", "APPROVAL_RESULT")
    ]
    for expected in ("approved", "executing", "executed"):
        assert expected in phases, (expected, phases)


def test_two_concurrent_approvals_keep_separate_results(client):
    """并发两个审批：结果按 approval_id 归属，互不覆盖。"""
    gate = threading.Event()
    gate.set()  # 立即放行：验证的是归属，不是时序
    counter: list = []
    _register_gated_executor(client, gate, counter)

    first = _submit(client, label="并发审批 A", path="C:\\work\\a.txt")
    second = _submit(client, label="并发审批 B", path="C:\\work\\b.txt")
    assert client.post(f"/api/approvals/{second.approval_id}/decide", json={"decision": "approve"}).status_code == 200
    assert client.post(f"/api/approvals/{first.approval_id}/decide", json={"decision": "approve"}).status_code == 200

    _wait_state(client, first.approval_id, "completed")
    _wait_state(client, second.approval_id, "completed")

    history_a = _history(client, first.approval_id)
    history_b = _history(client, second.approval_id)
    assert history_a and history_b, "并发审批有结果丢失"
    assert history_a[0]["outcome"] == "executed"
    assert history_b[0]["outcome"] == "executed"
    assert first.approval_id not in _pending_ids(client)
    assert second.approval_id not in _pending_ids(client)


def test_internal_data_three_confirmations_only_execute_on_third(client):
    """内部数据三次确认：0/1/2 次不执行，第 3 次才执行（进度由后端决定）。"""
    gate = threading.Event()
    gate.set()
    counter: list = []
    _register_gated_executor(client, gate, counter, kind="internal.data_write")

    protected = client.app.state.ctx.settings.data_dir / "app.db"
    ticket = _submit(
        client,
        kind="internal.data_write",
        label="直接维护内部数据",
        confirmations=3,
        path=str(protected),
    )
    assert client.post(f"/api/approvals/{ticket.approval_id}/decide", json={"decision": "approve"}).status_code == 409

    for stage in (1, 2):
        resp = client.post(f"/api/approvals/{ticket.approval_id}/confirm", json={"stage": stage})
        assert resp.status_code == 200, resp.text
        assert resp.json()["approval"]["confirmed_stage"] == stage
        row = _row(client, ticket.approval_id)
        assert row["state"] in ("waiting", "approved"), row["state"]
        assert counter == [], f"第 {stage} 次确认就执行了（应当等到第 3 次）"

    final = client.post(f"/api/approvals/{ticket.approval_id}/confirm", json={"stage": 3})
    assert final.status_code == 200, final.text
    row = _wait_state(client, ticket.approval_id, "completed")
    assert row["state"] == "completed"
    assert counter, "第 3 次确认后执行器没有被调用"
