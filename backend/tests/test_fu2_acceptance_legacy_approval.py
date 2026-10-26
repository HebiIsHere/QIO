# -*- coding: utf-8 -*-
"""F 独立验收（基线 373fda40）· 反例 2/5：旧审批（legacy request() 行）的恢复身份与可消费边界。

契约（COMMON §4.2）：
- 旧 request() 落库行（state 为空、operation 为空）在 API/存储输出中必须 legacy=true，
  不得由 row_to_dict/state_of 伪造 state=waiting；
- 恢复后保留原 turn_id/session_id/created_at/expires_at 等绑定事实；旧审批 status 用独立事实
  （如 interrupted_legacy），不伪装成 v2 waiting；
- /decide 对 legacy 明确拒绝（可读 code+message，提示属于旧路径）；/respond 继续服务 legacy；
  恢复后的 legacy 不能被当成可 /decide 的 waiting 项。

基线预期（红）：state_of 对空 state 回退 LEGACY_STATUS_TO_STATE 或 "waiting"，
row_to_dict 没有 legacy 字段；store.decide 只看 state_of，legacy 行会被当成 waiting 批准。
"""

from __future__ import annotations

import asyncio

import pytest

from agent.api.bus import EventBus
from agent.storage.approvals import ApprovalError, ApprovalStore
from agent.tools.approval import ApprovalService

LEGACY_ID = "appr_fu2_legacy"
TURN_ID = "turn_fu2_legacy"
SESSION_ID = "sess_fu2_legacy"
DIGEST = "digest_fu2_legacy"
CREATED = "2026-10-26T00:00:00+00:00"
EXPIRES = "2099-01-01T00:00:00+00:00"


def _insert_legacy_row(conn, *, approval_id: str = LEGACY_ID) -> None:
    """复现旧 request() 落库行的形状：有 payload/绑定事实，但没有 state/operation。"""
    conn.execute(
        "INSERT INTO pending_approvals ("
        " approval_id, kind, payload, turn_id, session_id, created_at, expires_at, status"
        ") VALUES (?,?,?,?,?,?,?, 'pending')",
        (
            approval_id,
            "tool_execution",
            '{"description": "旧路径：修改发布材料", "source": "tool"}',
            TURN_ID,
            SESSION_ID,
            CREATED,
            EXPIRES,
        ),
    )
    conn.commit()


def _legacy_kind_of(exc: BaseException) -> str:
    return str(getattr(exc, "code", "") or "")


# --- 1) 读取形状：必须 legacy=true，且不得伪造 state=waiting ---------------------


def test_legacy_row_is_marked_and_state_not_forged(db_conn):
    _insert_legacy_row(db_conn)
    store = ApprovalStore(db_conn)
    row = store.get(LEGACY_ID)
    assert row is not None
    assert row.get("legacy") is True, (
        "旧 request() 行没有 legacy=true：前端无法区分旧路径与新 v2 审批。实际：" + str(row)
    )
    assert row.get("state") != "waiting", (
        "旧行（state 为空）被伪造为 waiting，会被当成可 /decide 的等待项。实际 state="
        + str(row.get("state"))
    )
    # 身份事实保留（不是猜测值）
    assert row.get("turn_id") == TURN_ID
    assert row.get("session_id") == SESSION_ID
    assert row.get("created_at") == CREATED
    assert row.get("expires_at") == EXPIRES


# --- 2) /decide 对 legacy 明确拒绝（不得消费） -----------------------------------


def test_decide_rejects_legacy_row_with_readable_reason(db_conn):
    _insert_legacy_row(db_conn)
    service = ApprovalService(EventBus(), conn=db_conn)

    async def run() -> None:
        with pytest.raises(ApprovalError) as caught:
            await service.decide(LEGACY_ID, "approve")
        exc = caught.value
        code = _legacy_kind_of(exc)
        message = str(exc)
        assert "legacy" in code.lower() or "旧" in message, (
            "legacy 审批的 /decide 拒绝缺少可读原因（旧路径）：code=" + code + " message=" + message
        )
        row = service.store.get(LEGACY_ID) or {}
        assert row.get("state") not in ("approved", "executing", "completed"), (
            "legacy 审批被 /decide 消费了（应当拒绝）：state=" + str(row.get("state"))
        )

    asyncio.run(run())


# --- 3) /respond 继续服务旧路径（单次消费 + 绑定校验不变） ------------------------


def test_respond_still_serves_live_legacy_request(db_conn):
    service = ApprovalService(EventBus(), conn=db_conn)

    async def run() -> None:
        task = asyncio.create_task(
            service.request(
                "tool_execution",
                {"description": "旧等待：写 2 个文件"},
                turn_id=TURN_ID,
                session_id=SESSION_ID,
            )
        )
        for _ in range(100):
            if service.pending():
                break
            await asyncio.sleep(0.01)
        pending = service.pending()
        assert pending, "旧 request() 没有出现在 pending 里"
        approval_id = str(pending[0]["approval_id"])
        handled = await service.respond(
            approval_id,
            "approved",
            turn_id=TURN_ID,
            session_id=SESSION_ID,
        )
        assert handled is True, "旧 /respond 不再能应答 legacy 审批"
        result = await task
        assert result.decision == "approved"
        # 单次使用：第二次应答必须失败
        assert await service.respond(approval_id, "approved") is False

    asyncio.run(run())


# --- 4) 启动恢复：旧行的 status 是独立事实（interrupted_legacy），不伪装 waiting ----


def test_recovery_marks_legacy_with_its_own_status_and_preserves_identity(db_conn):
    _insert_legacy_row(db_conn)
    restarted = ApprovalService(EventBus(), conn=db_conn)

    async def run() -> dict:
        return await restarted.recover()

    report = asyncio.run(run())
    assert LEGACY_ID in (report.get("legacy_interrupted") or []), (
        "启动恢复没有把旧路径行记入 legacy_interrupted：report=" + str(report)
    )
    row = restarted.store.get(LEGACY_ID) or {}
    assert row.get("legacy") is True, "恢复后旧行仍没有 legacy=true"
    assert row.get("state") != "waiting", "恢复后旧行仍被伪装成 v2 waiting"
    assert str(row.get("status") or "") == "interrupted_legacy", (
        "旧行的 status 应当是独立事实 interrupted_legacy（不伪装 v2 状态），实际："
        + str(row.get("status"))
    )
    # 原绑定事实保留
    assert row.get("turn_id") == TURN_ID
    assert row.get("session_id") == SESSION_ID
    assert row.get("created_at") == CREATED
    assert row.get("expires_at") == EXPIRES

    async def decide_after_recover() -> None:
        with pytest.raises(ApprovalError) as caught:
            await restarted.decide(LEGACY_ID, "approve")
        code = _legacy_kind_of(caught.value)
        assert "legacy" in code.lower(), (
            "恢复后的 legacy 审批被当成可 /decide 的项（缺少 legacy 拒绝码）：code=" + code
        )

    asyncio.run(decide_after_recover())
