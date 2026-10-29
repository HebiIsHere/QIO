# -*- coding: utf-8 -*-
"""F 独立验收（基线 373fda40）· 反例 3/5：overrides 只被支持的旧路径消费，其余必须显式拒绝。

契约（COMMON §4.2 预算）：
- 支持 overrides 的旧路径（tool_create 子智能体创建）必须让执行器采用用户批准值（既有测试已覆盖）；
- **不支持 overrides 的路径收到 overrides 必须显式 400 拒绝（code overrides_unsupported），
  不得静默丢弃。**

基线预期（红）：ApprovalService.respond() 对任何 legacy 审批都接受 overrides 并返回 True，
v2 审批走 /respond→decide 时也把 overrides 静默丢掉。
"""

from __future__ import annotations

import asyncio

import pytest

from agent.api.bus import EventBus
from agent.security.operations import PendingOperation
from agent.storage.approvals import ApprovalError
from agent.tools.approval import ApprovalService


def _operation() -> PendingOperation:
    return PendingOperation(
        kind="test.op",
        label="可恢复操作",
        payload={"value": 1},
        effect={"kind": "write", "targets": [{"kind": "path", "value": "C:\\work\\fu2.txt"}]},
        fingerprint={"path": "C:\\work\\fu2.txt", "sha256": "abc"},
        instruction_text="把文件改成 X 并保存",
        purpose="F 验收",
        confirmations_required=1,
        source="tool",
    )


async def _live_request(service: ApprovalService, kind: str, payload: dict) -> str:
    task = asyncio.create_task(service.request(kind, payload))
    for _ in range(200):
        pending = service.pending()
        if pending:
            return str(pending[0]["approval_id"])
        await asyncio.sleep(0.01)
    task.cancel()
    raise AssertionError("旧 request() 没有出现在 pending 里")


def _code_of(exc: BaseException) -> str:
    return str(getattr(exc, "code", "") or "")


# --- 1) 不支持的旧路径带 overrides：必须显式拒绝（不能静默接受） --------------------


def test_legacy_unsupported_path_rejects_overrides(db_conn):
    service = ApprovalService(EventBus(), conn=db_conn)

    async def run() -> None:
        approval_id = await _live_request(
            service, "tool_execution", {"description": "执行一个不需要预算的操作"}
        )
        with pytest.raises(ApprovalError) as caught:
            await service.respond(
                approval_id,
                "approved",
                overrides={"subagent_budget": {"max_iterations": 9}},
            )
        exc = caught.value
        assert _code_of(exc) == "overrides_unsupported", (
            "不支持 overrides 的旧路径没有显式拒绝（code 应为 overrides_unsupported），实际 code="
            + _code_of(exc)
            + " message=" + str(exc)
        )
        # 拒绝后这条审批不能被消费掉（用户还要能按原样批准/拒绝）
        assert service.pending(), "拒绝 overrides 后审批不应消失"

    asyncio.run(run())


# --- 2) v2 路径（/respond 映射到 decide）同样不支持 overrides ----------------------


def test_v2_respond_with_overrides_is_rejected(db_conn):
    service = ApprovalService(EventBus(), conn=db_conn)
    assert service.store is not None
    row = service.store.create(_operation())

    async def run() -> None:
        with pytest.raises(ApprovalError) as caught:
            await service.respond(
                row["approval_id"],
                "approved",
                overrides={"subagent_budget": {"max_iterations": 9}},
            )
        assert _code_of(caught.value) == "overrides_unsupported", (
            "v2 审批经 /respond 收到 overrides 被静默丢弃（应显式 overrides_unsupported），实际 code="
            + _code_of(caught.value)
        )
        fresh = service.store.get(row["approval_id"]) or {}
        assert fresh.get("state") == "waiting", (
            "带 overrides 的 v2 决定被静默接受并推进了状态：state=" + str(fresh.get("state"))
        )

    asyncio.run(run())


# --- 3) 支持的旧路径（tool_create + subagent_budget）仍然接受 ----------------------


def test_supported_tool_create_still_accepts_overrides(db_conn):
    service = ApprovalService(EventBus(), conn=db_conn)

    async def run() -> None:
        approval_id = await _live_request(
            service, "tool_create", {"tool_type": "subagent", "name": "fu2_agent"}
        )
        handled = await service.respond(
            approval_id,
            "approved",
            overrides={"subagent_budget": {"max_iterations": 5}},
        )
        assert handled is True, "支持 overrides 的 tool_create 旧路径被误拒"
        assert service._settle  # noqa: B018 - 只是让断言读到服务对象（保持公开路径）

    asyncio.run(run())
