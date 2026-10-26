"""FU3-B 回归用例：审批读取失败必须显式失败（不能伪装空集合）+ 到期事件相位。

基线 dae32828 的两个问题：

P1（读取失败伪装成空集合）：
- ApprovalService.pending() 吞掉 store.list_pending() 的异常只记 warning，返回内存部分；
- ApprovalService.history() 吞异常返回 []。
  → /api/approvals/pending、/api/approvals/history 返回 200+空/部分，前端把失败的补读
    当成成功并清掉待办。要求：存储读取失败必须显式失败（服务层专用异常 → API 503，
    code=approvals_unreadable），不得返回空集合或部分集合；内存里的 legacy 不能掩盖
    持久层失败；失败后可以重试恢复。

P2（到期事件相位错误）：
- _execute_approved 到期路径调用 mark_expired（history 已是 expired），却发布
  phase="invalidated"，前端因此显示「执行前核对不通过」而不是「已过期」。
  要求：发布 phase="expired"（summary 保留原文）。

运行：cd backend && uv run --frozen pytest tests/test_fu3_approval_read.py -q
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.bus import EventBus
from agent.api.server import create_app
from agent.config import Settings
from agent.security.operations import PendingOperation
from agent.tools.approval import ApprovalService

from _r9_auth_support import (
    bind_requirement,
    counting_executor,
    make_security,
    record_events,
)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(db_conn, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _make_operation(security=None, *, label="读取失败测试操作", path="C:\\work\\fu3.txt"):
    target = str(path)
    operation = PendingOperation(
        kind="test.op",
        label=label,
        payload={"value": 1},
        effect={"kind": "write", "targets": [{"kind": "path", "value": target}]},
        fingerprint={"path": target, "sha256": "fu3"},
        instruction_id="instr_fu3",
        instruction_text="把文件改成 X 并保存",
        purpose="FU3-B 测试",
        confirmations_required=1,
        source="tool",
    )
    if security is not None:
        bind_requirement(security, operation)
    return operation


def _boom_sqlite(message="database disk image is malformed"):
    def _raise(*args, **kwargs):  # noqa: ANN002, ANN003
        raise sqlite3.Error(message)

    return _raise


def _result_events(events, approval_id):
    return [
        event
        for event in events
        if event.type.value == "APPROVAL_RESULT"
        and event.data.get("approval_id") == approval_id
    ]


def _history_for(service, approval_id):
    return [
        item
        for item in service.history(limit=50)
        if item["approval_id"] == approval_id
    ]


# ---------------------------------------------------------------------------
# P1：存储读取失败不得伪装成空/部分集合（服务层）
# ---------------------------------------------------------------------------


def test_pending_storage_failure_raises_instead_of_empty(db_conn, monkeypatch):
    from agent.storage.approvals import ApprovalsUnreadable

    service = ApprovalService(EventBus(), conn=db_conn)
    monkeypatch.setattr(service.store, "list_pending", _boom_sqlite())
    with pytest.raises(ApprovalsUnreadable) as excinfo:
        service.pending()
    assert excinfo.value.code == "approvals_unreadable"
    assert "不代表没有审批" in str(excinfo.value)
    assert "可以重试" in str(excinfo.value)


def test_history_storage_failure_raises_instead_of_empty(db_conn, monkeypatch):
    from agent.storage.approvals import ApprovalsUnreadable

    service = ApprovalService(EventBus(), conn=db_conn)
    monkeypatch.setattr(service.store, "history", _boom_sqlite("database is locked"))
    with pytest.raises(ApprovalsUnreadable) as excinfo:
        service.history(limit=20)
    assert excinfo.value.code == "approvals_unreadable"
    assert "不代表没有" in str(excinfo.value)


async def test_pending_failure_not_masked_by_in_memory_legacy(db_conn, monkeypatch):
    """内存里确实有一条 legacy 等待项时，持久层读取失败仍必须显式失败。"""
    from agent.storage.approvals import ApprovalsUnreadable

    service = ApprovalService(EventBus(), conn=db_conn)
    task = asyncio.create_task(service.request("computer", {"description": "写入文件"}))
    try:
        for _ in range(200):
            if service._requests:
                break
            await asyncio.sleep(0.01)
        assert service._requests, "内存里应有 legacy 等待项"

        original = service.store.list_pending
        monkeypatch.setattr(service.store, "list_pending", _boom_sqlite())
        with pytest.raises(ApprovalsUnreadable):
            service.pending()

        # 恢复读取后可以重试（失败不改变任何状态）
        monkeypatch.setattr(service.store, "list_pending", original)
        items = service.pending()
        assert items and items[0]["legacy"] is True
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# P1：API 层必须 503 + 可读 code，而不是 200+空
# ---------------------------------------------------------------------------


def test_api_pending_read_failure_returns_503_and_is_retryable(client, monkeypatch):
    approvals = client.app.state.ctx.approvals
    store = approvals.store
    original = store.list_pending
    monkeypatch.setattr(store, "list_pending", _boom_sqlite())

    resp = client.get("/api/approvals/pending")
    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "approvals_unreadable"
    assert "不代表没有审批" in detail["message"]
    assert "可以重试" in detail["message"]

    # 失败后可重试：恢复读取 → 200 且是真实列表（不是空集合的伪装）
    monkeypatch.setattr(store, "list_pending", original)
    retry = client.get("/api/approvals/pending")
    assert retry.status_code == 200
    assert isinstance(retry.json()["approvals"], list)


def test_api_history_read_failure_returns_503_and_is_retryable(client, monkeypatch):
    approvals = client.app.state.ctx.approvals
    store = approvals.store
    original = store.history
    monkeypatch.setattr(store, "history", _boom_sqlite("database is locked"))

    resp = client.get("/api/approvals/history", params={"limit": 20})
    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "approvals_unreadable"
    assert "不代表没有" in detail["message"]

    monkeypatch.setattr(store, "history", original)
    retry = client.get("/api/approvals/history", params={"limit": 20})
    assert retry.status_code == 200
    assert isinstance(retry.json()["history"], list)


# ---------------------------------------------------------------------------
# P2：到期路径必须发布 phase=expired（history 已是 expired）
# ---------------------------------------------------------------------------


async def test_expired_approved_publishes_expired_phase(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="restricted")
    counter: list = []
    security.operations.register("test.op", counting_executor(counter))
    events = record_events(security.bus)

    store = security.approvals.store
    row = store.create(_make_operation(security=security))
    approval_id = row["approval_id"]
    store.decide(approval_id, "approve")
    # 批准后、执行前已经超过原始有效期（期限不续期）。
    db_conn.execute(
        "UPDATE pending_approvals SET expires_at = ? WHERE approval_id = ?",
        ("2000-01-01T00:00:00+00:00", approval_id),
    )

    await security.approvals._execute_approved(approval_id)

    updated = store.get(approval_id) or {}
    assert updated["state"] == "expired"
    assert counter == [], "过期后不得执行"

    result_events = _result_events(events, approval_id)
    phases = [event.data.get("phase") for event in result_events]
    assert "expired" in phases, "到期必须发布 APPROVAL_RESULT phase=expired，实际：" + repr(phases)
    assert "invalidated" not in phases, "到期不是「执行前核对不通过」，不得发 invalidated"
    expired_events = [
        event for event in result_events if event.data.get("phase") == "expired"
    ]
    assert "超过原始有效期" in str(expired_events[-1].data.get("summary") or "")

    history = _history_for(security.approvals, approval_id)
    assert history and history[0]["outcome"] == "expired"
