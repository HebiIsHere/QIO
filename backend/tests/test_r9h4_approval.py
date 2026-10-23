# -*- coding: utf-8 -*-
"""R9-H4 F：审批完成、重放与并发（契约 §10）。

范围（只测 F 独占文件真实实现，模型一律 fake；不触碰 Lead 共享文件）：
1) unresolved 下用户对具体操作的有效批准可以完成这次操作；已知限制仍优先 DENY；
   批准不清除未知限制/不解除已知禁止/不形成长期授权/不把 unresolved 改成无限制；
2) 结束进程审批重放完整传递原审批快照/操作依据（ExecutionContext.replay_basis）；
   提供 Lead 接线所需的接口测试与“不再一律报没有审批记录”的验收点；
3) 确认阶段/审批状态更新在实际写入时核对前一状态（BEGIN IMMEDIATE + CAS），
   两个连接可控交错时只有一方推进同一步；旧请求不能覆盖新阶段/回退次数/绕过撤回；
4) 状态与审计一致（同一事务；审计失败回滚）；
5) 真实 ApprovalService/Store/operations：确认次数、期限、模式、要求、内容、
   重复领取按既定规则；一次/两次确认不能执行，取消不执行。

不用随机 sleep 制造并发：两个真实 SQLite 连接 + threading.Barrier 让两个请求同时进入，
断言的是不变量（恰一方成功）与最终状态/审计，不依赖执行顺序。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from _r9_auth_support import record_events, wait_for

from agent.api.bus import EventBus
from agent.security.effects import EffectKind
from agent.security.engine import AuthorizationEngine
from agent.security.operations import (
    ExecResult,
    ExecutionContext,
    OperationRegistry,
    PendingOperation,
    replay_gateway_kwargs,
)
from agent.security.requirements import AuthorizationLedger, RequirementLedger
from agent.storage.approvals import (
    ApprovalConflict,
    ApprovalConfirmRequired,
    ApprovalStore,
)
from agent.storage.db import connect
from agent.storage.settings import SettingsStore
from agent.tools import security_gate
from agent.tools.approval import ApprovalService


# --------------------------------------------------------------------------
# 小装配（真实 SQLite 审批 / 台账 / 引擎 / 操作注册表）
# --------------------------------------------------------------------------


def _make_security(tmp_path, conn, *, mode="unrestricted"):
    store = SettingsStore(conn)
    store.set("computer.permission_mode", mode)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bus = EventBus()
    approvals = ApprovalService(bus, conn=conn)
    operations = OperationRegistry()
    requirements = RequirementLedger(conn)
    authorizations = AuthorizationLedger(conn)
    engine = AuthorizationEngine(
        conn=conn,
        approvals=approvals,
        settings=store,
        instance_id="inst_test",
        workspace_root=lambda: workspace,
        internal_data_paths=lambda: [],
        requirements=requirements,
        authorizations=authorizations,
    )
    approvals.set_engine(engine)
    approvals.set_operations(operations)
    return SimpleNamespace(
        conn=conn,
        store=store,
        bus=bus,
        engine=engine,
        approvals=approvals,
        operations=operations,
        requirements=requirements,
        authorizations=authorizations,
        workspace=workspace,
    )


def _write_operation(security, target, *, kind="test.write", confirmations=1, payload=None):
    effect = security_gate.write_effect(
        "test",
        target,
        purpose="测试写入",
        content_sha256="c" * 64,
        exists=False,
        detail={"action": "write"},
    )
    operation = PendingOperation(
        kind=kind,
        label="写入测试文件",
        payload=dict(payload or {"path": str(target), "content": "X"}),
        effect=security_gate.effect_to_dict(effect),
        fingerprint={"path": str(target), "content_sha256": "c" * 64},
        instruction_id="instr_turn_1",
        instruction_text="把测试文件写成 X",
        purpose="测试写入",
        mode_at_request="unrestricted",
        confirmations_required=int(confirmations),
        source="tool",
    )
    requirement = security.requirements.record_user_message(
        text="把测试文件写成 X", message_id="msg_test", turn_id="turn_test"
    )
    operation.requirement_id = requirement.requirement_id
    operation.requirement_version = int(requirement.version)
    return operation, effect, requirement


def _set_unresolved(security, requirement, limits=None):
    return security.requirements.set_structured_limits(
        requirement.requirement_id,
        None if limits is None else list(limits),
        state="unresolved",
        source="test_fixture",
    )


def _auth_record_count(conn) -> int:
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM authorization_records").fetchone()
        return int(row["n"] if row is not None else 0)
    except Exception:  # noqa: BLE001 - 迁移未提供该表时按 0
        return 0


def _bind_requirement(security, operation, *, text="按用户要求执行这次操作"):
    requirement = security.requirements.record_user_message(
        text=text, message_id="msg_bind", turn_id="turn_bind"
    )
    operation.requirement_id = requirement.requirement_id
    operation.requirement_version = int(requirement.version)
    return operation


def _disable_auto_execute(security) -> None:
    """决定后不自动后台执行：让测试能可控地在“批准之后、执行之前”插入状态变化。"""
    security.approvals._schedule_execute = lambda approval_id: False  # type: ignore[assignment]


async def _run_approved(security, approval_id: str) -> None:
    await security.approvals._execute_approved(approval_id)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# §10.1 unresolved 下具体批准完成
# --------------------------------------------------------------------------


async def test_unresolved_specific_approval_completes_exactly_once(tmp_path, db_conn):
    security = _make_security(tmp_path, db_conn, mode="unrestricted")
    target = tmp_path / "workspace" / "a.txt"
    operation, _effect, requirement = _write_operation(security, target)
    _set_unresolved(security, requirement)

    calls: list = []
    contexts: list = []

    async def executor(payload, ctx):
        calls.append(payload)
        contexts.append(ctx)
        return ExecResult(ok=True, summary="已写入", detail={"actual": "written"})

    security.operations.register("test.write", executor)
    before_records = _auth_record_count(db_conn)
    before_requirement = security.requirements.by_id(requirement.requirement_id)

    ticket = await security.approvals.submit(operation)
    await security.approvals.decide(ticket.approval_id, "approve")
    assert await wait_for(
        lambda: (security.approvals.store.get(ticket.approval_id) or {}).get("state")
        == "completed",
        timeout=5.0,
    ), "unresolved 下用户对这个具体操作的有效批准必须能完成（不能批准后立即失效）"
    assert len(calls) == 1, "具体批准只完成这一次"

    ctx = contexts[0]
    basis = ctx.authorization_basis
    assert basis["kind"] == "specific_approval_completed_unresolved"
    assert basis["approval_id"] == ticket.approval_id
    assert basis["known_limits_checked"] is True
    assert basis["unresolved_requirement_kept"] is True
    assert basis["does_not_clear_unknown_limits"] is True
    assert basis["does_not_lift_known_prohibitions"] is True
    assert basis["does_not_grant_long_term"] is True

    row = security.approvals.store.get(ticket.approval_id)
    assert row["state"] == "completed"
    result_basis = (row.get("result") or {}).get("authorization_basis") or {}
    assert result_basis.get("kind") == "specific_approval_completed_unresolved", (
        "执行结果/审计必须留下“这次完成所依据的具体批准”，而不是只写成功"
    )

    # 要求仍是 unresolved、已知限制原样保留、没有形成长期授权记录
    after_requirement = security.requirements.by_id(requirement.requirement_id)
    assert after_requirement.structured_limits_state == "unresolved"
    assert int(after_requirement.version) == int(before_requirement.version)
    assert _auth_record_count(db_conn) == before_records, "具体批准不得写入长期授权记录"

    audit = security.approvals.audit_trail(ticket.approval_id)
    actions = [item["action"] for item in audit]
    assert "executed" in actions
    executed = next(item for item in audit if item["action"] == "executed")
    detail = executed.get("detail") or {}
    assert (detail.get("authorization_basis") or {}).get("kind") == (
        "specific_approval_completed_unresolved"
    )


async def test_unresolved_known_prohibition_still_denies_specific_approval(tmp_path, db_conn):
    security = _make_security(tmp_path, db_conn, mode="unrestricted")
    protected = tmp_path / "workspace" / "B.txt"
    operation, _effect, requirement = _write_operation(security, protected)
    _set_unresolved(
        security,
        requirement,
        limits=[
            {
                "operations": ["write"],
                "objects": [{"kind": "path", "value": str(protected)}],
                "reason": "用户明确要求不要修改 B",
            }
        ],
    )
    calls: list = []

    async def executor(payload, ctx):
        calls.append(payload)
        return ExecResult(ok=True, summary="不应执行")

    security.operations.register("test.write", executor)
    ticket = await security.approvals.submit(operation)
    await security.approvals.decide(ticket.approval_id, "approve")
    assert await wait_for(
        lambda: (security.approvals.store.get(ticket.approval_id) or {}).get("state")
        in ("invalidated", "failed"),
        timeout=5.0,
    )
    row = security.approvals.store.get(ticket.approval_id)
    assert calls == [], "已知禁止优先于具体批准，绝不能执行"
    assert row["state"] == "invalidated"
    assert "用户明确要求不要修改 B" in str(row.get("error") or ""), row.get("error")
    assert security.requirements.by_id(requirement.requirement_id).structured_limits_state == (
        "unresolved"
    )


async def test_specific_approval_does_not_turn_unresolved_into_unrestricted(tmp_path, db_conn):
    security = _make_security(tmp_path, db_conn, mode="unrestricted")
    target = tmp_path / "workspace" / "a.txt"
    operation, _effect, requirement = _write_operation(security, target)
    _set_unresolved(security, requirement, limits=None)  # 未知限制
    security.operations.register(
        "test.write",
        lambda payload, ctx: _ok_result("已执行"),
    )
    ticket = await security.approvals.submit(operation)
    await security.approvals.decide(ticket.approval_id, "approve")
    assert await wait_for(
        lambda: (security.approvals.store.get(ticket.approval_id) or {}).get("state")
        == "completed",
        timeout=5.0,
    )
    requirement_after = security.requirements.by_id(requirement.requirement_id)
    assert requirement_after.structured_limits_state == "unresolved", (
        "具体批准不得把 unresolved 改成 structured（更不得当成明确无限制）"
    )
    assert tuple(requirement_after.structured_limits or ()) == (), (
        "未知限制不得被清成“明确无限制”（state 仍是 unresolved，没有新增假定限制）"
    )
    assert security.store.get("computer.permission_mode") == "unrestricted"
    assert _auth_record_count(db_conn) == 0


async def test_stale_requirement_version_invalidates_approved_operation(tmp_path, db_conn):
    security = _make_security(tmp_path, db_conn, mode="unrestricted")
    target = tmp_path / "workspace" / "a.txt"
    operation, _effect, requirement = _write_operation(security, target)
    _set_unresolved(security, requirement)
    calls: list = []

    async def executor(payload, ctx):
        calls.append(payload)
        return ExecResult(ok=True, summary="不应执行")

    security.operations.register("test.write", executor)
    _disable_auto_execute(security)
    ticket = await security.approvals.submit(operation)
    await security.approvals.decide(ticket.approval_id, "approve")
    # 批准之后，用户修正要求 → 旧版本要求被 superseded，绑定失效
    security.requirements.add_correction(
        requirement.requirement_id, "改成不要写这个文件", message_id="msg_correction"
    )
    await _run_approved(security, ticket.approval_id)
    row = security.approvals.store.get(ticket.approval_id)
    assert calls == [], "要求版本/状态已变化时不得执行旧批准"
    assert row["state"] == "invalidated"
    assert "失效" in str(row.get("error") or "") or "版本" in str(row.get("error") or "")
    assert security.requirements.by_id(requirement.requirement_id).status != "active"


# --------------------------------------------------------------------------
# §10.2 结束进程审批重放：完整快照/操作依据
# --------------------------------------------------------------------------


def test_execution_context_replay_basis_is_complete():
    operation = PendingOperation(
        kind="proc.kill",
        label="结束测试进程",
        payload={"pid": "4242"},
        effect={
            "kind": "system_change",
            "targets": [{"kind": "program", "value": "taskkill"}],
            "purpose": "结束测试进程",
            "detail": {"command": "taskkill /pid 4242", "pid": "4242"},
        },
        fingerprint={"pid": "4242", "created_by": "test", "managed": True},
        instruction_id="instr_turn_9",
        instruction_text="结束这个测试进程",
        purpose="结束测试进程",
        mode_at_request="restricted",
        confirmations_required=1,
        source="tool",
        turn_id="turn_9",
        session_id="sess_9",
        requirement_id="req_9",
        requirement_version=3,
    )
    ctx = ExecutionContext(
        approval_id="appr_replay_1",
        operation=operation,
        mode_at_request="restricted",
        mode_at_approval="restricted",
        attempt=1,
        expires_at="2030-01-01T00:00:00+00:00",
        confirmed_stage=0,
    )
    basis = ctx.replay_basis()
    assert basis["approval_id"] == "appr_replay_1"
    assert basis["kind"] == "proc.kill"
    assert basis["payload"] == {"pid": "4242"}
    assert basis["effect"]["detail"]["pid"] == "4242"
    assert basis["fingerprint"] == {
        "pid": "4242",
        "created_by": "test",
        "managed": True,
    }
    assert basis["instruction_id"] == "instr_turn_9"
    assert basis["instruction_text"] == "结束这个测试进程"
    assert basis["requirement_id"] == "req_9"
    assert basis["requirement_version"] == 3
    assert basis["mode_at_request"] == "restricted"
    assert basis["expires_at"] == "2030-01-01T00:00:00+00:00"
    # Lead 接线后 execute_proc_kill 必须把 operation/approval_id 传给重放网关；
    # 这里直接验证网关消费完整快照时的行为（不是无条件放行、也不是“没有审批记录”）。
    from agent.tools.approved_operations import _ApprovedReplayGateway

    gateway = _ApprovedReplayGateway(
        None, operation=operation, approval_id=ctx.approval_id
    )
    expected_effect = security_gate.effect_from_dict(operation.effect)
    import asyncio

    decision = asyncio.run(gateway.authorize(expected_effect))
    assert security_gate.verdict_of(decision) == "allow"
    assert "审批记录一致" in (decision.reason or "")
    mismatched = security_gate.execute_effect(
        "run_program", "git", ["commit", "-m", "x"], purpose="不同的操作"
    )
    denied = asyncio.run(gateway.authorize(mismatched))
    assert security_gate.verdict_of(denied) == "deny"
    assert "没有审批记录" not in (denied.reason or ""), (
        "有审批快照时不得再一律报“没有审批记录”"
    )
    # Lead 接线帮助：执行器一行接入即可拿到原审批快照/操作依据
    kwargs = replay_gateway_kwargs(ctx)
    assert kwargs["operation"] is operation
    assert kwargs["approval_id"] == "appr_replay_1"
    wired = _ApprovedReplayGateway(None, **kwargs)
    wired_decision = asyncio.run(wired.authorize(expected_effect))
    assert security_gate.verdict_of(wired_decision) == "allow"


async def test_proc_kill_replay_transports_basis_and_operates_only_test_process(
    tmp_path, db_conn
):
    security = _make_security(tmp_path, db_conn, mode="unrestricted")
    test_process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        operation = PendingOperation(
            kind="proc.kill",
            label="结束测试进程",
            payload={"pid": str(test_process.pid)},
            effect=security_gate.execute_effect(
                "proc_kill",
                "taskkill",
                ["/f", "/pid", str(test_process.pid)],
                purpose="结束测试进程",
                detail={"pid": str(test_process.pid), "managed": True},
            ),
            fingerprint={"pid": str(test_process.pid), "created_by": "test"},
            purpose="结束测试进程",
            mode_at_request="unrestricted",
            source="tool",
        )
        requirement = security.requirements.record_user_message(
            text="结束这个测试进程", message_id="msg_kill", turn_id="turn_kill"
        )
        operation.requirement_id = requirement.requirement_id
        operation.requirement_version = int(requirement.version)
        operation.effect = security_gate.effect_to_dict(
            security_gate.make_effect(
                EffectKind.SYSTEM_CHANGE,
                purpose="结束测试进程",
                detail={"pid": str(test_process.pid), "managed": True},
            )
        )
        seen: list = []

        async def kill_executor(payload, ctx):
            basis = ctx.replay_basis()
            seen.append(basis)
            from agent.tools.approved_operations import _ApprovedReplayGateway

            gateway = _ApprovedReplayGateway(
                security.engine, operation=ctx.operation, approval_id=ctx.approval_id
            )
            decision = await gateway.authorize(
                security_gate.effect_from_dict(ctx.operation.effect)
            )
            assert security_gate.verdict_of(decision) == "allow", decision.reason
            test_process.terminate()
            test_process.wait(timeout=5)
            return ExecResult(
                ok=True,
                summary="已结束测试进程 " + str(payload.get("pid")),
                detail={
                    "pid": str(payload.get("pid")),
                    "approval_id": basis["approval_id"],
                },
            )

        security.operations.register("proc.kill", kill_executor)
        ticket = await security.approvals.submit(operation)
        await security.approvals.decide(ticket.approval_id, "approve")
        assert await wait_for(
            lambda: (security.approvals.store.get(ticket.approval_id) or {}).get("state")
            == "completed",
            timeout=5.0,
        )
        assert seen and seen[0]["approval_id"] == ticket.approval_id
        assert seen[0]["kind"] == "proc.kill"
        assert seen[0]["payload"]["pid"] == str(test_process.pid)
        assert test_process.poll() is not None, "只结束测试创建的进程，并且真的执行了"
        row = security.approvals.store.get(ticket.approval_id)
        assert (row.get("result") or {}).get("pid") == str(test_process.pid)
    finally:
        if test_process.poll() is None:
            test_process.kill()


# --------------------------------------------------------------------------
# §10.3/§10.4 并发：两个连接、可控交错、CAS、审计一致
# --------------------------------------------------------------------------


def _plain_operation(*, confirmations=1, kind="test.op") -> PendingOperation:
    return PendingOperation(
        kind=kind,
        label="并发测试操作",
        payload={"value": 1},
        effect={"kind": "write", "targets": [{"kind": "path", "value": "X"}]},
        fingerprint={"path": "X"},
        confirmations_required=int(confirmations),
        mode_at_request="restricted",
        source="tool",
    )


def _race(call_a, call_b):
    """两个真实线程同时发起两个操作（Barrier 控制同时进入，不用随机 sleep）。"""
    barrier = threading.Barrier(2, timeout=10)
    results: list = [None, None]
    errors: list = [None, None]

    def _run(index, fn):
        barrier.wait()
        try:
            results[index] = fn()
        except Exception as exc:  # noqa: BLE001 - 记录后由断言判断
            errors[index] = exc

    threads = [
        threading.Thread(target=_run, args=(0, call_a), daemon=True),
        threading.Thread(target=_run, args=(1, call_b), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive(), "并发请求超时未结束"
    return results, errors


def _second_store(tmp_path, db_conn):
    conn2 = connect(tmp_path / "test.db")
    return ApprovalStore(conn2), conn2


async def test_two_connections_only_one_advances_same_confirmation_stage(tmp_path, db_conn):
    first = ApprovalStore(db_conn)
    second, conn2 = _second_store(tmp_path, db_conn)
    try:
        row = first.create(_plain_operation(confirmations=3))
        approval_id = row["approval_id"]
        results, errors = _race(
            lambda: first.confirm(approval_id, 1),
            lambda: second.confirm(approval_id, 1),
        )
        assert sum(1 for item in results if item is not None) == 1, (
            "同一确认阶段并发只能有一个请求成功"
        )
        conflicts = [exc for exc in errors if exc is not None]
        assert len(conflicts) == 1 and isinstance(conflicts[0], ApprovalConflict)
        current = first.get(approval_id)
        assert current["confirmed_stage"] == 1
        assert current["revisions"] == 1, "失败的一方不得把次数/版本推进两次"
        confirmed_actions = [
            item for item in first.audit_trail(approval_id) if item["action"] == "confirmed"
        ]
        assert len(confirmed_actions) == 1, "状态与审计一致：只记一次 confirmed"
    finally:
        conn2.close()


async def test_stale_request_cannot_advance_rewind_or_bypass_cancel(tmp_path, db_conn):
    first = ApprovalStore(db_conn)
    second, conn2 = _second_store(tmp_path, db_conn)
    try:
        row = first.create(_plain_operation(confirmations=3))
        approval_id = row["approval_id"]
        stale_revision = int(row["revisions"])
        first.confirm(approval_id, 1)
        with pytest.raises(ApprovalConflict):
            second.confirm(approval_id, 2, expected_revision=stale_revision)
        with pytest.raises(ApprovalConflict):
            second.confirm(approval_id, 1, expected_revision=stale_revision)
        current = first.get(approval_id)
        assert current["confirmed_stage"] == 1, "旧请求不得回退或跳跃确认阶段"
        first.confirm(approval_id, 2)
        third = first.confirm(approval_id, 3)
        assert third["state"] == "approved"
        # 撤回后，任何旧批准请求都不能把状态改回 approved
        first.decide(approval_id, "cancel")
        with pytest.raises(ApprovalConflict):
            second.decide(approval_id, "approve", expected_revision=stale_revision)
        cancelled = first.get(approval_id)
        assert cancelled["state"] == "cancelled"
        assert first.claim(approval_id) is None
    finally:
        conn2.close()


async def test_approve_and_cancel_race_only_one_state_transition(tmp_path, db_conn):
    first = ApprovalStore(db_conn)
    second, conn2 = _second_store(tmp_path, db_conn)
    try:
        row = first.create(_plain_operation())
        approval_id = row["approval_id"]
        results, errors = _race(
            lambda: first.decide(approval_id, "approve"),
            lambda: second.decide(approval_id, "cancel"),
        )
        assert sum(1 for item in results if item is not None) == 1
        assert len([exc for exc in errors if exc is not None]) == 1
        current = first.get(approval_id)
        assert current["state"] in ("approved", "cancelled")
        assert current["revisions"] == 1
        actions = [item["action"] for item in first.audit_trail(approval_id)]
        assert actions.count("approved") + actions.count("cancelled") == 1
    finally:
        conn2.close()


async def test_duplicate_claim_only_one_attempt(tmp_path, db_conn):
    first = ApprovalStore(db_conn)
    second, conn2 = _second_store(tmp_path, db_conn)
    try:
        row = first.create(_plain_operation())
        approval_id = row["approval_id"]
        first.decide(approval_id, "approve")
        results, errors = _race(
            lambda: first.claim(approval_id),
            lambda: second.claim(approval_id),
        )
        assert not [exc for exc in errors if exc is not None]
        claimed = [item for item in results if item is not None]
        assert len(claimed) == 1, "同一审批只能被领取一次"
        current = first.get(approval_id)
        assert current["state"] == "executing"
        assert current["attempt"] == 1
        claimed_actions = [
            item for item in first.audit_trail(approval_id) if item["action"] == "claimed"
        ]
        assert len(claimed_actions) == 1
    finally:
        conn2.close()


async def test_finish_only_from_executing_and_never_overwrite_terminal(tmp_path, db_conn):
    first = ApprovalStore(db_conn)
    second, conn2 = _second_store(tmp_path, db_conn)
    try:
        row = first.create(_plain_operation())
        approval_id = row["approval_id"]
        first.decide(approval_id, "approve")
        # 未领取就 finish：不得把 approved 直接写成完成
        before = second.finish(approval_id, ExecResult(ok=True, summary="不应生效"))
        assert before["state"] == "approved"
        assert first.claim(approval_id) is not None
        completed = first.finish(approval_id, ExecResult(ok=True, summary="第一次完成"))
        assert completed["state"] == "completed"
        first_result = completed["result"]
        stale = second.finish(approval_id, ExecResult(ok=False, summary="迟到的旧结果"))
        assert stale["state"] == "completed", "终态不得被迟到的旧结果覆盖"
        assert stale["result"] == first_result
        actions = [item["action"] for item in first.audit_trail(approval_id)]
        assert actions.count("executed") == 1
        assert actions.count("finish_rejected") >= 1
    finally:
        conn2.close()


async def test_audit_failure_rolls_back_confirmation(tmp_path, db_conn, monkeypatch):
    """状态与审计一致：审计写不进去时，确认推进必须整体回滚（不能只改状态）。"""
    from agent.storage.approvals import ApprovalError

    store = ApprovalStore(db_conn)
    row = store.create(_plain_operation(confirmations=3))
    approval_id = row["approval_id"]
    assert row["revisions"] == 0 and row["confirmed_stage"] == 0

    def broken_audit(*args, **kwargs):
        raise ApprovalError("模拟审计写入失败")

    monkeypatch.setattr(store, "_audit", broken_audit)
    with pytest.raises(ApprovalError):
        store.confirm(approval_id, 1)
    current = store.get(approval_id)
    assert current["confirmed_stage"] == 0, "审计失败时确认阶段不得推进"
    assert current["revisions"] == 0, "审计失败时 revision 不得推进"
    assert current["state"] == "waiting"


async def test_audit_sequence_is_consistent_after_controlled_races(tmp_path, db_conn):
    first = ApprovalStore(db_conn)
    second, conn2 = _second_store(tmp_path, db_conn)
    try:
        row = first.create(_plain_operation(confirmations=3))
        approval_id = row["approval_id"]
        _race(
            lambda: first.confirm(approval_id, 1),
            lambda: second.confirm(approval_id, 1),
        )
        first.confirm(approval_id, 2)
        first.confirm(approval_id, 3)  # 第三次确认完成即 approved（不再需要一次额外批准）
        first.claim(approval_id)
        first.finish(approval_id, ExecResult(ok=True, summary="完成"))
        audit = first.audit_trail(approval_id)
        assert audit, "审计不能丢"
        assert [item["seq"] for item in audit] == list(range(1, len(audit) + 1)), (
            "审计序号必须连续（同一事务写入，不能并发丢条目）"
        )
        actions = [item["action"] for item in audit]
        assert actions.count("confirmed") == 2  # 第 1 阶段并发只成功一次
        assert actions.count("confirm_completed") == 1
        assert actions.count("approved") == 0, "三次确认完成时只记 confirm_completed，不重复记 approved"
        assert actions.count("claimed") == 1
        assert actions.count("executed") == 1
    finally:
        conn2.close()


# --------------------------------------------------------------------------
# §10.5 期限 / 确认次数 / 取消 / 内容变化 / 模式变化
# --------------------------------------------------------------------------


async def test_expired_approval_cannot_be_approved_confirmed_or_claimed(tmp_path, db_conn):
    clock = {"now": datetime(2026, 10, 24, tzinfo=timezone.utc)}
    store = ApprovalStore(db_conn, clock=lambda: clock["now"])
    row = store.create(_plain_operation(confirmations=3), timeout_seconds=600)
    approval_id = row["approval_id"]
    clock["now"] = clock["now"] + timedelta(seconds=601)
    with pytest.raises(ApprovalConflict):
        store.confirm(approval_id, 1)
    assert store.get(approval_id)["state"] == "expired"
    with pytest.raises(ApprovalConflict):
        store.decide(approval_id, "approve")
    assert store.claim(approval_id) is None


async def test_three_confirmations_required_and_one_or_two_cannot_execute(tmp_path, db_conn):
    security = _make_security(tmp_path, db_conn, mode="restricted")
    counter: list = []
    security.operations.register("internal.data_write", _counting_executor(counter))
    operation = _plain_operation(confirmations=3, kind="internal.data_write")
    _bind_requirement(security, operation)
    ticket = await security.approvals.submit(operation)
    first = await security.approvals.confirm(ticket.approval_id, 1)
    assert first["state"] == "waiting" and first["confirmed_stage"] == 1
    assert counter == [], "只完成一次确认时绝不能执行"
    with pytest.raises(ApprovalConfirmRequired):
        await security.approvals.decide(ticket.approval_id, "approve")
    second = await security.approvals.confirm(ticket.approval_id, 2)
    assert second["state"] == "waiting" and second["confirmed_stage"] == 2
    assert counter == [], "只完成两次确认时绝不能执行"
    # 第三条取消：不执行
    await security.approvals.decide(ticket.approval_id, "cancel")
    assert counter == []
    assert (security.approvals.store.get(ticket.approval_id) or {})["state"] == "cancelled"

    # 三次完成 → 批准并执行一次
    operation2 = _plain_operation(confirmations=3, kind="internal.data_write")
    _bind_requirement(security, operation2)
    ticket2 = await security.approvals.submit(operation2)
    await security.approvals.confirm(ticket2.approval_id, 1)
    await security.approvals.confirm(ticket2.approval_id, 2)
    third = await security.approvals.confirm(ticket2.approval_id, 3)
    assert third["state"] == "approved"
    assert await wait_for(
        lambda: (security.approvals.store.get(ticket2.approval_id) or {}).get("state")
        == "completed",
        timeout=5.0,
    )
    assert len(counter) == 1


async def test_confirmation_count_is_bound_to_operation_effect(tmp_path, db_conn):
    security = _make_security(tmp_path, db_conn, mode="restricted")
    ticket_internal = await security.approvals.submit(
        _plain_operation(confirmations=3, kind="internal.data_write")
    )
    internal_row = security.approvals.store.get(ticket_internal.approval_id)
    assert internal_row["requires_confirmations"] == 3
    ticket_permanent = await security.approvals.submit(
        _plain_operation(confirmations=3, kind="fs.permanent_delete")
    )
    permanent_row = security.approvals.store.get(ticket_permanent.approval_id)
    assert permanent_row["requires_confirmations"] == 3, (
        "确认次数来自操作/effect（内部数据、永久删除），不是界面临时决定"
    )


async def test_operation_content_change_resets_confirmation_and_blocks_approval(
    tmp_path, db_conn
):
    security = _make_security(tmp_path, db_conn, mode="restricted")
    operation = _plain_operation(confirmations=3)
    ticket = await security.approvals.submit(operation)
    await security.approvals.confirm(ticket.approval_id, 1)
    # 提交后篡改审批单绑定的操作内容（模拟旧请求/坏写入）
    row = security.approvals.store.get_raw(ticket.approval_id)
    operation_json = json.loads(row["operation"])
    operation_json["payload"]["value"] = 999
    db_conn.execute(
        "UPDATE pending_approvals SET operation = ? WHERE approval_id = ?",
        (json.dumps(operation_json, ensure_ascii=False), ticket.approval_id),
    )
    with pytest.raises(ApprovalConflict):
        await security.approvals.decide(ticket.approval_id, "approve")
    current = security.approvals.store.get(ticket.approval_id)
    assert current["state"] == "waiting"
    assert current["confirmed_stage"] == 0, "操作内容变化必须重置确认次数"
    actions = [item["action"] for item in security.approvals.audit_trail(ticket.approval_id)]
    assert "binding_reset" in actions


async def test_mode_change_after_approval_invalidates_before_execution(tmp_path, db_conn):
    security = _make_security(tmp_path, db_conn, mode="restricted")
    target = tmp_path / "workspace" / "a.txt"
    operation, _effect, _requirement = _write_operation(security, target)
    operation.mode_at_request = "restricted"
    calls: list = []

    async def executor(payload, ctx):
        calls.append(payload)
        return ExecResult(ok=True, summary="不应执行")

    security.operations.register("test.write", executor)
    _disable_auto_execute(security)
    ticket = await security.approvals.submit(operation)
    await security.approvals.decide(ticket.approval_id, "approve")
    security.store.set("computer.permission_mode", "read_only")
    await _run_approved(security, ticket.approval_id)
    row = security.approvals.store.get(ticket.approval_id)
    assert calls == [], "批准后切换到只读模式：执行前核对必须拒绝"
    assert row["state"] == "invalidated"
    assert "只读" in str(row.get("error") or "")


async def test_tampered_operation_after_approval_is_not_replayed(tmp_path, db_conn):
    security = _make_security(tmp_path, db_conn, mode="restricted")
    target = tmp_path / "workspace" / "a.txt"
    operation, _effect, _requirement = _write_operation(security, target)
    operation.mode_at_request = "restricted"
    calls: list = []

    async def executor(payload, ctx):
        calls.append(payload)
        return ExecResult(ok=True, summary="不应执行")

    security.operations.register("test.write", executor)
    _disable_auto_execute(security)
    ticket = await security.approvals.submit(operation)
    await security.approvals.decide(ticket.approval_id, "approve")
    row = security.approvals.store.get_raw(ticket.approval_id)
    operation_json = json.loads(row["operation"])
    operation_json["payload"]["content"] = "被篡改的内容"
    db_conn.execute(
        "UPDATE pending_approvals SET operation = ? WHERE approval_id = ?",
        (json.dumps(operation_json, ensure_ascii=False), ticket.approval_id),
    )
    await _run_approved(security, ticket.approval_id)
    after = security.approvals.store.get(ticket.approval_id)
    assert calls == [], "审批单绑定内容被改动后不得重放"
    assert after["state"] == "invalidated"
    assert "改动" in str(after.get("error") or "")


# --------------------------------------------------------------------------
# 局部小工具
# --------------------------------------------------------------------------


async def _ok_result(summary: str):
    return ExecResult(ok=True, summary=summary)


def _counting_executor(counter: list):
    async def handler(payload, ctx):
        counter.append(payload)
        return ExecResult(ok=True, summary="已执行", detail={"payload": payload})

    return handler
