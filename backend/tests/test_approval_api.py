"""审批 API 与事件：新接口、旧接口兼容、前端状态恢复（契约 §5.4、§12）。"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.security.operations import PendingOperation


@pytest.fixture()
def client(db_conn, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _submit(client, *, kind="test.op", label="测试操作", confirmations=1, path="C:\\work\\a.txt"):
    async def _do():
        ctx = client.app.state.ctx
        operation = PendingOperation(
            kind=kind,
            label=label,
            payload={"value": 1},
            effect={"kind": "write", "targets": [{"kind": "path", "value": path}]},
            fingerprint={"path": path, "sha256": "abc"},
            instruction_id="instr_turn_api",
            instruction_text="把文件改成 X 并保存",
            purpose="测试用途",
            confirmations_required=confirmations,
            source="tool",
        )
        # R9-G：审批单必须绑定具体用户要求；测试在真实 app 的台账里登记一条要求。
        if getattr(ctx, "requirements", None) is not None:
            requirement = ctx.requirements.record_user_message(
                text="把文件改成 X 并保存", message_id="msg_api", turn_id="turn_api"
            )
            operation.requirement_id = requirement.requirement_id
            operation.requirement_version = int(getattr(requirement, "version", 1) or 1)
        return await ctx.approvals.submit(operation)

    return client.portal.call(_do)


def _row(client, approval_id):
    return client.app.state.ctx.approvals.store.get(approval_id)


def _wait_state(client, approval_id, state, *, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = _row(client, approval_id)
        if row is not None and row["state"] == state:
            return row
        client.portal.call(lambda: asyncio.sleep(0.02))
    return _row(client, approval_id)


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


def _register_executor(client, *, kind="test.op", counter=None):
    async def handler(payload, ctx):
        from agent.security.operations import ExecResult

        (counter if counter is not None else []).append(payload)
        return ExecResult(ok=True, summary="已执行", detail={"payload": payload})

    # 应用构造时会把尚无独立执行器的 kind 显式注册为「不可用」（不允许批准后静默丢单）。
    # 这里用 replace=True 覆盖成测试执行器：测试要验证的是「批准后真的执行」，
    # 不是绕过应用的生产注册结果。
    client.app.state.ctx.operations.register(kind, handler, replace=True)


def test_pending_api_returns_operation_summary_and_state(client):
    ticket = _submit(client)
    resp = client.get("/api/approvals/pending")
    assert resp.status_code == 200
    body = resp.json()
    items = [item for item in body["approvals"] if item["approval_id"] == ticket.approval_id]
    assert len(items) == 1
    item = items[0]
    assert item["state"] == "waiting"
    assert item["kind"] == "test.op"
    assert item["label"] == "测试操作"
    assert item["purpose"] == "测试用途"
    assert item["expires_at"]
    assert item["confirmed_stage"] == 0
    assert item["requires_confirmations"] == 1
    assert item["source"] == "tool"
    assert item["instruction_id"] == "instr_turn_api"
    assert item["operation"]["kind"] == "test.op"
    assert isinstance(item["payload"], dict)


def test_decide_api_executes_and_records_history_and_events(client):
    counter: list = []
    _register_executor(client, counter=counter)
    events = _record_events(client)
    ticket = _submit(client)
    resp = client.post(
        "/api/approvals/{}/decide".format(ticket.approval_id),
        json={"decision": "approve"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    row = _wait_state(client, ticket.approval_id, "completed")
    assert row["state"] == "completed"
    assert counter == [{"value": 1}]

    history = client.get("/api/approvals/history", params={"limit": 20}).json()["history"]
    entry = next(item for item in history if item["approval_id"] == ticket.approval_id)
    assert entry["outcome"] == "executed"
    assert entry["summary"] == "已执行"

    phases = [
        event.data.get("phase")
        for event in events
        if event.type.value in ("APPROVAL_REQUIRED", "APPROVAL_RESULT")
    ]
    for expected in ("waiting", "approved", "executing", "executed"):
        assert expected in phases, phases
    # 事件里带审批身份，前端据此原地更新卡片
    result_events = [e for e in events if e.type.value == "APPROVAL_RESULT"]
    assert any(
        e.data["approval_id"] == ticket.approval_id and e.data.get("approval")
        for e in result_events
    )


def test_decide_api_rejects_stale_revision_and_unknown_state(client):
    ticket = _submit(client)
    row = _row(client, ticket.approval_id)
    stale = int(row["revisions"]) + 5
    resp = client.post(
        "/api/approvals/{}/decide".format(ticket.approval_id),
        json={"decision": "approve", "expected_revision": stale},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "approval_conflict"
    missing = client.post("/api/approvals/appr_missing/decide", json={"decision": "approve"})
    assert missing.status_code == 404
    bad = client.post(
        "/api/approvals/{}/decide".format(ticket.approval_id),
        json={"decision": "maybe"},
    )
    assert bad.status_code == 400


def test_reject_and_cancel_api_are_terminal(client):
    rejected = _submit(client, label="要拒绝")
    assert (
        client.post(
            "/api/approvals/{}/decide".format(rejected.approval_id),
            json={"decision": "reject"},
        ).status_code
        == 200
    )
    assert _row(client, rejected.approval_id)["state"] == "rejected"
    cancelled = _submit(client, label="要取消")
    assert (
        client.post(
            "/api/approvals/{}/decide".format(cancelled.approval_id),
            json={"decision": "cancel"},
        ).status_code
        == 200
    )
    assert _row(client, cancelled.approval_id)["state"] == "cancelled"
    # 终态不再出现在待审批列表里
    pending_ids = {item["approval_id"] for item in client.get("/api/approvals/pending").json()["approvals"]}
    assert rejected.approval_id not in pending_ids
    assert cancelled.approval_id not in pending_ids


def test_confirm_api_enforces_sequential_cas_and_three_confirmations(client):
    protected = client.app.state.ctx.settings.data_dir / "app.db"
    _register_executor(client, kind="internal.data_write")
    ticket = _submit(
        client,
        kind="internal.data_write",
        label="直接维护内部数据",
        confirmations=3,
        path=str(protected),
    )
    early_approve = client.post(
        "/api/approvals/{}/decide".format(ticket.approval_id),
        json={"decision": "approve"},
    )
    assert early_approve.status_code == 409
    out_of_order = client.post(
        "/api/approvals/{}/confirm".format(ticket.approval_id), json={"stage": 3}
    )
    assert out_of_order.status_code == 409
    first = client.post(
        "/api/approvals/{}/confirm".format(ticket.approval_id), json={"stage": 1}
    )
    assert first.status_code == 200
    assert first.json()["approval"]["confirmed_stage"] == 1
    assert (
        client.post(
            "/api/approvals/{}/confirm".format(ticket.approval_id), json={"stage": 1}
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/approvals/{}/confirm".format(ticket.approval_id), json={"stage": 2}
        ).status_code
        == 200
    )
    final = client.post(
        "/api/approvals/{}/confirm".format(ticket.approval_id), json={"stage": 3}
    )
    assert final.status_code == 200
    assert final.json()["approval"]["state"] in ("approved", "executing", "completed")
    assert _wait_state(client, ticket.approval_id, "completed")["state"] == "completed"


def test_old_respond_endpoint_still_drives_new_approvals(client):
    _register_executor(client)
    ticket = _submit(client)
    resp = client.post(
        "/api/approvals/{}/respond".format(ticket.approval_id),
        json={"decision": "approved"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert _wait_state(client, ticket.approval_id, "completed")["state"] == "completed"


def test_runtime_state_restores_pending_approvals(client):
    ticket = _submit(client, label="断线恢复")
    state = client.get("/api/runtime/state").json()
    approvals = [item for item in state["approvals"] if item["approval_id"] == ticket.approval_id]
    assert approvals, "断线/RESYNC 后必须能查回等待中的审批"
    assert approvals[0]["state"] == "waiting"
    assert "interrupted_approvals" in state
