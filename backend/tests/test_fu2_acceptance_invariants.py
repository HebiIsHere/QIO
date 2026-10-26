# -*- coding: utf-8 -*-
"""F 独立验收 · 保真项（候选 8cb18828）：审批不变量与结果闭环契约。

契约（COMMON §4.2 / §4.3 / §7 保护清单）：
- 三次确认：只能 1→2→3 连续推进；跳级/重复被拒；
- 期限：恢复/确认不续期；过期不得执行；
- CAS 与单次执行：并发 claim 只有一个成功；finish 只收口一次，迟到结果不覆盖终态；
- 执行前重核：批准后操作内容被改动 → 不执行（invalidated）；
- 非 legacy 的 v2 审批仍走 decide 并真实执行；
- 旧 /respond 的用户决定（approved/rejected）与真实执行结果分开可读：
  仅“已批准”不得显示为“已执行成功”；执行结束写 approval_history + APPROVAL_RESULT
  （phase=executed/failed、ok、summary、detail 带 ok/kind/label/error），字段与 B 契约一致。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent.api.bus import EventBus
from agent.security.operations import PendingOperation, operation_digest
from agent.storage.approvals import ApprovalConflict, ApprovalError, ApprovalStore
from agent.tools.approval import ApprovalService

from _r9_auth_support import (
    counting_executor,
    make_security,
    record_events,
    wait_for,
)


def _operation(
    *, confirmations: int = 1, path: str = "C:\\work\\fu2_inv.txt", with_digest: bool = False
) -> PendingOperation:
    operation = PendingOperation(
        kind="test.op",
        label="可恢复操作",
        payload={"value": 1},
        effect={"kind": "write", "targets": [{"kind": "path", "value": path}]},
        fingerprint={"path": path, "sha256": "abc"},
        instruction_id="instr_turn_fu2",
        instruction_text="把文件改成 X 并保存",
        purpose="F 验收不变量",
        confirmations_required=confirmations,
        source="tool",
    )
    if with_digest:
        # 只有直接走 store.create 的执行前重核用例需要手动算摘要；
        # 正常 submit() 会在补全 mode/source 之后自己算，提前算会得到过期摘要。
        operation.fingerprint = dict(operation.fingerprint or {})
        operation.fingerprint["operation_digest"] = operation_digest(operation)
    return operation


# --- 1) 三次确认：连续、不可跳级；完成前不能批准 --------------------------------


def test_three_confirmations_are_sequential_and_block_early_approval(db_conn):
    store = ApprovalStore(db_conn)
    row = store.create(_operation(confirmations=3))
    approval_id = row["approval_id"]
    expires_before = row["expires_at"]

    with pytest.raises(ApprovalConflict):
        store.confirm(approval_id, 2)  # 跳级
    with pytest.raises(ApprovalError) as early:
        store.decide(approval_id, "approve")
    assert "确认" in str(early.value), "确认未完成时的提前批准没有说明原因：" + str(early.value)

    first = store.confirm(approval_id, 1)
    assert first["state"] == "waiting" and first["confirmed_stage"] == 1
    second = store.confirm(approval_id, 2)
    assert second["state"] == "waiting" and second["confirmed_stage"] == 2
    third = store.confirm(approval_id, 3)
    assert third["state"] == "approved" and third["confirmed_stage"] == 3
    # 恢复/确认不续期
    assert third["expires_at"] == expires_before, "确认过程改了期限（不得续期）"
    with pytest.raises(ApprovalConflict):
        store.confirm(approval_id, 3)


# --- 2) 期限：过期不执行；CAS + 单次执行 ----------------------------------------


def test_deadline_expires_without_execution_and_cas_single_claim(db_conn):
    store = ApprovalStore(db_conn)
    expired_row = store.create(_operation(path="C:\\work\\fu2_expired.txt"), timeout_seconds=0.01)
    asyncio.run(asyncio.sleep(0.02))
    report = store.recover()
    assert expired_row["approval_id"] in report["expired"]
    expired = store.get(expired_row["approval_id"]) or {}
    assert expired["state"] == "expired"
    assert expired["expires_at"] == expired_row["expires_at"], "过期处理改了期限"
    assert store.claim(expired_row["approval_id"]) is None, "过期审批仍可被领取执行"

    row = store.create(_operation())
    approval_id = row["approval_id"]
    approved = store.decide(approval_id, "approve")
    assert approved["state"] == "approved"
    first = store.claim(approval_id)
    second = store.claim(approval_id)
    assert first is not None and first["state"] == "executing", "批准后 CAS 领取失败"
    assert second is None, "并发/重复 claim 有两个成功（会重复执行）"
    from agent.security.operations import ExecResult

    done = store.finish(approval_id, ExecResult(ok=True, summary="已执行", detail={"value": 1}))
    assert done["state"] == "completed"
    again = store.finish(approval_id, ExecResult(ok=False, summary="迟到的失败", detail={}))
    assert again["state"] == "completed", "迟到的结果覆盖了终态"
    history = {item["approval_id"]: item for item in store.history(limit=20)}
    assert history[approval_id]["outcome"] == "executed", "单次执行的历史被迟到结果改成失败"


# --- 3) 执行前重核：批准后操作内容被改 → 不执行 ---------------------------------


def test_pre_execution_recheck_invalidates_changed_operation(db_conn):
    store = ApprovalStore(db_conn)
    row = store.create(_operation(path="C:\\work\\fu2_recheck.txt", with_digest=True))
    approval_id = row["approval_id"]
    store.decide(approval_id, "approve")

    raw = store.get_raw(approval_id)
    operation = json.loads(str(raw["operation"]))
    operation["payload"] = {"value": 999}
    db_conn.execute(
        "UPDATE pending_approvals SET operation = ? WHERE approval_id = ?",
        (json.dumps(operation), approval_id),
    )
    db_conn.commit()

    claimed = store.claim(approval_id)
    assert claimed is None, "批准后操作内容变了仍然被领取执行（缺少执行前重核）"
    fresh = store.get(approval_id) or {}
    assert fresh["state"] == "invalidated", "执行前重核失败的状态不是 invalidated：" + str(fresh["state"])


# --- 4) 非 legacy 的 v2 审批仍走 decide 并真实执行 -------------------------------


def test_v2_decide_still_executes(db_conn):
    service = ApprovalService(EventBus(), conn=db_conn)
    counter: list = []
    service.operations.register("test.op", counting_executor(counter))
    row = service.store.create(_operation(path="C:\\work\\fu2_v2.txt"))

    async def scenario() -> None:
        decided = await service.decide(row["approval_id"], "approve")
        assert decided["state"] == "approved"
        assert await wait_for(lambda: bool(counter), timeout=5.0), "v2 decide 批准后没有执行"
        assert await wait_for(
            lambda: (service.store.get(row["approval_id"]) or {}).get("state") == "completed",
            timeout=5.0,
        ), "v2 审批批准后没有真实执行完成"

    asyncio.run(scenario())


# --- 5) legacy approved ≠ executed；执行结果闭环字段契约 --------------------------


def _start_legacy(service: ApprovalService, kind: str, payload: dict):
    async def run() -> tuple[str, asyncio.Task]:
        before = {str(item.get("approval_id")) for item in service.pending()}
        task = asyncio.create_task(
            service.request(kind, payload, turn_id="turn_legacy_fu2", session_id="sess_legacy_fu2")
        )
        for _ in range(200):
            fresh = [
                str(item.get("approval_id"))
                for item in service.pending()
                if str(item.get("approval_id")) not in before
            ]
            if fresh:
                return fresh[0], task
            await asyncio.sleep(0.01)
        raise AssertionError("旧 request() 没有出现在 pending 里")

    return run()


def _history_item(service: ApprovalService, approval_id: str) -> dict:
    return {
        str(item["approval_id"]): item for item in service.history(limit=50)
    }.get(approval_id, {})


def test_legacy_approved_is_not_executed_and_result_closure_contract(db_conn):
    bus = EventBus()
    service = ApprovalService(bus, conn=db_conn)
    events = record_events(bus)
    pending_payload = {"description": "旧路径：写发布材料", "kind": "tool_execution"}

    async def scenario() -> None:
        approval_id, task = await _start_legacy(service, "tool_execution", pending_payload)
        pending_item = service.pending()[0]
        # B 契约字段：legacy + overrides_supported（旧行不补 v2 state）
        assert pending_item.get("legacy") is True, "旧审批 pending 项没有 legacy=true"
        assert isinstance(pending_item.get("overrides_supported"), bool), "缺少 overrides_supported 布尔字段"
        assert pending_item.get("overrides_supported") is False

        assert await service.respond(
            approval_id,
            "approved",
            turn_id="turn_legacy_fu2",
            session_id="sess_legacy_fu2",
        )
        result = await task
        assert result.decision == "approved"

        # 仅“已批准”：历史必须写 approved/等待执行，不得写 executed/completed
        before = _history_item(service, approval_id)
        assert before.get("outcome") == "approved", "用户批准后历史直接写成了终态执行结果"
        assert "等待执行" in str(before.get("summary") or "")
        assert (before.get("detail") or {}).get("ok") is None
        approve_events = [
            event for event in events
            if event.type.value == "APPROVAL_RESULT"
            and (event.data or {}).get("approval_id") == approval_id
        ]
        assert approve_events, "旧 /respond 没有发布 APPROVAL_RESULT"
        assert all(
            str((event.data or {}).get("phase") or "") not in ("executed", "completed", "failed")
            for event in approve_events
        ), "仅批准就发布了执行完成/失败事件"

        # 真实执行结束：写 history + APPROVAL_RESULT（B 契约字段）
        executed = await service.record_legacy_execution(
            approval_id, ok=True, summary="已执行：写发布材料"
        )
        assert executed is not None
        assert executed["approval_id"] == approval_id
        assert executed["decision"] == "approved"
        assert executed["phase"] == "executed"
        assert executed["legacy"] is True
        assert executed["ok"] is True
        assert executed["summary"] == "已执行：写发布材料"
        detail = executed["detail"]
        assert detail["ok"] is True
        assert "kind" in detail and "label" in detail
        assert detail.get("error") is None
        assert detail.get("decision") == "approved"
        assert executed.get("turn_id") == "turn_legacy_fu2"

        after = _history_item(service, approval_id)
        assert after.get("outcome") == "executed", "执行结束后历史没有升级为 executed"
        assert (after.get("detail") or {}).get("ok") is True
        result_events = [
            event for event in events
            if event.type.value == "APPROVAL_RESULT"
            and (event.data or {}).get("approval_id") == approval_id
            and (event.data or {}).get("phase") == "executed"
        ]
        assert result_events, "执行结束没有发布 APPROVAL_RESULT(phase=executed)"

        # 失败路径：ok=False → phase failed + history failed + error 可读
        failed_id, failed_task = await _start_legacy(
            service, "tool_execution", {"description": "旧路径：删除发布材料"}
        )
        assert await service.respond(
            failed_id, "approved", turn_id="turn_legacy_fu2", session_id="sess_legacy_fu2"
        )
        await failed_task
        failed_event = await service.record_legacy_execution(
            failed_id, ok=False, error="磁盘只读"
        )
        assert failed_event is not None
        assert failed_event["phase"] == "failed"
        assert failed_event["ok"] is False
        assert "磁盘只读" in str(failed_event["summary"])
        assert "磁盘只读" in str(failed_event["detail"].get("error"))
        failed_history = _history_item(service, failed_id)
        assert failed_history.get("outcome") == "failed"
        assert (failed_history.get("detail") or {}).get("ok") is False

    asyncio.run(scenario())


# --- 6) 输出字段：legacy 标记与 overrides_supported ------------------------------


def test_approval_output_fields_mark_legacy_and_override_support(db_conn):
    store = ApprovalStore(db_conn)
    v2 = store.create(_operation())
    v2_row = store.get(v2["approval_id"]) or {}
    assert v2_row.get("legacy") is False, "v2 审批被标成 legacy"
    assert v2_row.get("overrides_supported") is False, "v2 生命周期记录不应可编辑 overrides"

    service = ApprovalService(EventBus(), conn=db_conn)

    async def run() -> None:
        approval_id, task = await _start_legacy(
            service, "tool_create", {"tool_type": "subagent", "name": "fu2_agent"}
        )
        pending = next(
            item for item in service.pending() if str(item.get("approval_id")) == approval_id
        )
        assert pending.get("legacy") is True
        assert pending.get("overrides_supported") is True, (
            "tool_create 子智能体旧路径应声明支持 overrides（预算可编辑）"
        )
        assert await service.respond(
            approval_id, "approved", overrides={"subagent_budget": {"max_iterations": 5}}
        )
        await task

    asyncio.run(run())
