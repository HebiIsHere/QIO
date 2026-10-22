"""审批通过后的真实执行器（契约 §5.3）：真实写文件 / 重核对 / 回收站兜底 / 进程重放。

这些用例验证的是「用户批准之后到底有没有真的执行、失败有没有如实落库」——
不是审批状态机本身（那由 test_approval_lifecycle/test_approval_restart 覆盖）。
"""

from __future__ import annotations

import json

import pytest

from _r9_auth_support import make_security, wait_for

from agent.security.operations import ExecutionContext, PendingOperation
from agent.services.computer import ComputerSandbox
from agent.tools import security_gate
from agent.tools.approved_operations import register_approved_operations
from agent.tools.base import ToolResult


def _security(tmp_path, db_conn, *, mode="restricted"):
    security = make_security(tmp_path, db_conn, mode=mode)
    computer = ComputerSandbox(
        resolve_root=lambda: str(security.workspace),
        permission_mode=lambda: security.store.get(
            "computer.permission_mode", "restricted"
        ),
    )
    kinds = register_approved_operations(
        security.operations,
        computer=computer,
        security=security.engine,
        tool_lookup=lambda name: None,
    )
    return security, computer, kinds


def _ctx(
    security,
    *,
    kind,
    payload,
    fingerprint=None,
    confirmations=1,
    mode="restricted",
):
    operation = PendingOperation(
        kind=kind,
        label="测试操作",
        payload=dict(payload),
        fingerprint=dict(fingerprint or {}),
        confirmations_required=confirmations,
        mode_at_request=mode,
        source="tool",
    )
    return ExecutionContext(
        approval_id="appr_test",
        operation=operation,
        mode_at_request=mode,
        mode_at_approval=mode,
        engine=security.engine if security is not None else None,
        mode_getter=(security.approvals.current_mode_value if security is not None else None),
    )


def _write_fingerprint(path, content: str) -> dict:
    identity = security_gate.file_identity(path)
    return {
        "identity": identity,
        # 与 fs_tools 提交审批时一致：记录目标当前内容摘要，执行前核对是否被改。
        "base_content_sha256": (
            security_gate.content_sha256(path) if identity.get("exists") else None
        ),
        "content_sha256": security_gate.text_sha256(content),
    }


# --------------------------------------------------------------------------
# 注册表
# --------------------------------------------------------------------------


def test_registers_all_expected_operation_kinds(tmp_path, db_conn):
    _security_obj, _computer, kinds = _security(tmp_path, db_conn)
    assert set(kinds) == {
        "fs.write",
        "fs.patch",
        "fs.delete",
        "proc.run_program",
        "proc.shell",
        "proc.kill",
        "subagent.create",
        "dep.install",
        "net.egress",
        "internal.data_write",
    }
    # tool.invoke 由 app.py 自己的执行器负责（registry approved 重跑管线），不重复注册。
    assert "tool.invoke" not in kinds


# --------------------------------------------------------------------------
# fs.write
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fs_write_really_writes_atomically(tmp_path, db_conn):
    security, _computer, _kinds = _security(tmp_path, db_conn)
    target = security.workspace / "out.txt"
    content = "批准后的真实内容"
    fingerprint = _write_fingerprint(target, content)
    ctx = _ctx(
        security,
        kind="fs.write",
        payload={"path": str(target), "content": content},
        fingerprint=fingerprint,
    )

    result = await security.operations.execute("fs.write", ctx.operation.payload, ctx)

    assert result.ok is True, result.error
    assert "已写入" in result.summary
    assert target.read_text(encoding="utf-8") == content
    leftovers = [path.name for path in target.parent.iterdir() if path.name.startswith(".qio-tmp-")]
    assert leftovers == [], "原子写不得留下临时文件"


@pytest.mark.asyncio
async def test_fs_write_does_not_overwrite_when_identity_changed(tmp_path, db_conn):
    security, _computer, _kinds = _security(tmp_path, db_conn)
    target = security.workspace / "business.txt"
    target.write_text("original", encoding="utf-8")
    fingerprint = _write_fingerprint(target, "new-content")
    # 提交审批之后、执行之前，文件被别的程序改了
    target.write_text("changed-by-someone-else", encoding="utf-8")
    ctx = _ctx(
        security,
        kind="fs.write",
        payload={"path": str(target), "content": "new-content"},
        fingerprint=fingerprint,
    )

    result = await security.operations.execute("fs.write", ctx.operation.payload, ctx)

    assert result.ok is False
    assert result.detail.get("invalidated") is True
    assert "未执行" in result.summary
    assert target.read_text(encoding="utf-8") == "changed-by-someone-else", "不得覆盖已变化的文件"


@pytest.mark.asyncio
async def test_fs_write_content_digest_mismatch_is_invalidated(tmp_path, db_conn):
    security, _computer, _kinds = _security(tmp_path, db_conn)
    target = security.workspace / "digest.txt"
    fingerprint = _write_fingerprint(target, "expected-content")
    ctx = _ctx(
        security,
        kind="fs.write",
        payload={"path": str(target), "content": "different-content"},
        fingerprint=fingerprint,
    )

    result = await security.operations.execute("fs.write", ctx.operation.payload, ctx)

    assert result.ok is False
    assert result.detail.get("invalidated") is True
    assert "content_sha256_mismatch" in (result.error or "")
    assert not target.exists(), "内容与审批单不一致时不得写入"


@pytest.mark.asyncio
async def test_fs_write_internal_data_requires_three_confirmations(tmp_path, db_conn):
    security, _computer, _kinds = _security(tmp_path, db_conn)
    internal = tmp_path / "qio-data"
    internal.mkdir()
    target = internal / "qio.db"
    fingerprint = _write_fingerprint(target, "x")
    fingerprint["internal_data_root"] = str(internal)
    ctx = _ctx(
        security,
        kind="fs.write",
        payload={"path": str(target), "content": "x"},
        fingerprint=fingerprint,
        confirmations=1,
    )

    result = await security.operations.execute("fs.write", ctx.operation.payload, ctx)

    assert result.ok is False
    assert result.detail.get("invalidated") is True
    assert "三次确认" in result.summary
    assert not target.exists()


# --------------------------------------------------------------------------
# fs.patch
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fs_patch_applies_after_rechecking_content(tmp_path, db_conn):
    security, _computer, _kinds = _security(tmp_path, db_conn)
    target = security.workspace / "note.txt"
    target.write_text("hello world", encoding="utf-8")
    new_text = "hello there"
    fingerprint = _write_fingerprint(target, new_text)
    ctx = _ctx(
        security,
        kind="fs.patch",
        payload={
            "path": str(target),
            "old": "world",
            "new": "there",
            "expected_identity": fingerprint["identity"],
        },
        fingerprint=fingerprint,
    )

    result = await security.operations.execute("fs.patch", ctx.operation.payload, ctx)

    assert result.ok is True, result.error
    assert target.read_text(encoding="utf-8") == new_text


@pytest.mark.asyncio
async def test_fs_patch_refuses_when_old_content_disappeared(tmp_path, db_conn):
    security, _computer, _kinds = _security(tmp_path, db_conn)
    target = security.workspace / "note2.txt"
    target.write_text("hello world", encoding="utf-8")
    fingerprint = _write_fingerprint(target, "hello there")
    target.write_text("hello changed", encoding="utf-8")
    ctx = _ctx(
        security,
        kind="fs.patch",
        payload={
            "path": str(target),
            "old": "world",
            "new": "there",
            "expected_identity": fingerprint["identity"],
        },
        fingerprint=fingerprint,
    )

    result = await security.operations.execute("fs.patch", ctx.operation.payload, ctx)

    assert result.ok is False
    assert result.detail.get("invalidated") is True
    assert target.read_text(encoding="utf-8") == "hello changed", "不得改动已变化的文件"


# --------------------------------------------------------------------------
# fs.delete：必须进回收站；模块不可用时如实报告并保留文件（绝不 unlink）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fs_delete_without_recycle_bin_keeps_the_file(
    tmp_path, db_conn, monkeypatch
):
    security, _computer, _kinds = _security(tmp_path, db_conn)
    target = security.workspace / "old.txt"
    target.write_text("keep me", encoding="utf-8")
    fingerprint = {"identity": security_gate.file_identity(target)}
    ctx = _ctx(
        security, kind="fs.delete", payload={"path": str(target)}, fingerprint=fingerprint
    )
    # W3 回收站模块尚未合入：明确走「不可用兜底」分支
    monkeypatch.setattr(
        "agent.tools.approved_operations._recycle_bin_mover", lambda: None
    )

    result = await security.operations.execute("fs.delete", ctx.operation.payload, ctx)

    assert result.ok is False
    assert "回收站" in result.summary and "未删除" in result.summary
    assert "recycle_bin_unavailable" in (result.error or "")
    assert target.exists(), "回收站不可用时必须保留文件（绝不 unlink）"


@pytest.mark.asyncio
async def test_fs_delete_uses_recycle_bin_when_available(tmp_path, db_conn, monkeypatch):
    security, _computer, _kinds = _security(tmp_path, db_conn)
    target = security.workspace / "old2.txt"
    target.write_text("recycle me", encoding="utf-8")
    fingerprint = {"identity": security_gate.file_identity(target)}
    ctx = _ctx(
        security, kind="fs.delete", payload={"path": str(target)}, fingerprint=fingerprint
    )
    calls: list[str] = []

    def _fake_mover(path):
        calls.append(str(path))
        return {"ok": True, "path": path}

    monkeypatch.setattr(
        "agent.tools.approved_operations._recycle_bin_mover", lambda: _fake_mover
    )

    result = await security.operations.execute("fs.delete", ctx.operation.payload, ctx)

    assert result.ok is True, result.error
    assert calls == [str(target)]
    assert result.detail.get("recycled") is True


# --------------------------------------------------------------------------
# 进程操作：复用 cmd_tools 的真实执行路径
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proc_run_program_executes_after_approval(tmp_path, db_conn):
    security, _computer, _kinds = _security(tmp_path, db_conn)
    ctx = _ctx(
        security,
        kind="proc.run_program",
        payload={"program": "git", "args": ["--version"], "cwd": str(security.workspace)},
    )

    result = await security.operations.execute("proc.run_program", ctx.operation.payload, ctx)

    assert result.ok is True, result.error
    assert "git" in (result.detail.get("content") or "").lower()


@pytest.mark.asyncio
async def test_proc_shell_executes_after_approval(tmp_path, db_conn):
    security, _computer, _kinds = _security(tmp_path, db_conn)
    ctx = _ctx(
        security,
        kind="proc.shell",
        payload={"cmd": "echo approved-shell", "cwd": str(security.workspace)},
    )

    result = await security.operations.execute("proc.shell", ctx.operation.payload, ctx)

    assert result.ok is True, result.error
    assert "approved-shell" in (result.detail.get("content") or "")


@pytest.mark.asyncio
async def test_proc_kill_invalid_pid_reports_failure(tmp_path, db_conn):
    security, _computer, _kinds = _security(tmp_path, db_conn)
    ctx = _ctx(security, kind="proc.kill", payload={"pid": "abc"})

    result = await security.operations.execute("proc.kill", ctx.operation.payload, ctx)

    assert result.ok is False
    assert "pid" in (result.summary or "").lower()


# --------------------------------------------------------------------------
# 子智能体 / 未实现执行器
# --------------------------------------------------------------------------


class _FakeSubagentTool:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def execute_after_approval(self, payload: dict) -> ToolResult:
        self.calls.append(dict(payload))
        return ToolResult(ok=True, content="子任务完成")


@pytest.mark.asyncio
async def test_subagent_create_rebuilds_with_snapshotted_scope(tmp_path, db_conn):
    security = make_security(tmp_path, db_conn, mode="restricted")
    fake = _FakeSubagentTool()
    register_approved_operations(
        security.operations,
        computer=None,
        security=security.engine,
        tool_lookup=lambda name: fake if name == "research_x" else None,
    )
    payload = {
        "tool": "research_x",
        "definition": "research_x",
        "arguments": {"query": "研究"},
        "agent_scope": {"depth": 1, "parent_instruction_id": "instr_1", "grants": []},
    }
    ctx = _ctx(security, kind="subagent.create", payload=payload)

    result = await security.operations.execute("subagent.create", payload, ctx)

    assert result.ok is True, result.error
    assert fake.calls and fake.calls[0]["agent_scope"]["depth"] == 1

    missing = _ctx(security, kind="subagent.create", payload={"tool": "ghost"})
    failed = await security.operations.execute("subagent.create", {"tool": "ghost"}, missing)
    assert failed.ok is False
    assert "不存在" in failed.summary


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["dep.install", "net.egress", "internal.data_write"])
async def test_unimplemented_operations_are_explicitly_unavailable(
    tmp_path, db_conn, kind
):
    security, _computer, _kinds = _security(tmp_path, db_conn)
    ctx = _ctx(security, kind=kind, payload={"value": 1})

    result = await security.operations.execute(kind, ctx.operation.payload, ctx)

    assert result.ok is False
    assert result.detail.get("executor_unavailable") is True
    assert "executor_unavailable" in (result.error or "")
    assert result.summary, "不可用也要说清原因，不能静默丢单"


# --------------------------------------------------------------------------
# 端到端：批准 → 真实执行 → 状态落库；失败也落库；重复领取不执行
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_executes_once_and_persists_completed(tmp_path, db_conn):
    security, _computer, _kinds = _security(tmp_path, db_conn)
    target = security.workspace / "approved.txt"
    content = "approved content"
    operation = PendingOperation(
        kind="fs.write",
        label="写入 approved.txt",
        payload={"path": str(target), "content": content},
        effect={
            "kind": "write",
            "targets": [{"kind": "path", "value": str(target)}],
            "purpose": "测试批准后执行",
        },
        fingerprint=_write_fingerprint(target, content),
        mode_at_request="restricted",
        source="tool",
    )
    ticket = await security.approvals.submit(operation)
    await security.approvals.decide(ticket.approval_id, "approve")

    assert await wait_for(
        lambda: (security.approvals.store.get(ticket.approval_id) or {}).get("state")
        in ("completed", "failed")
    )
    row = security.approvals.store.get(ticket.approval_id) or {}
    assert row["state"] == "completed", row
    assert target.read_text(encoding="utf-8") == content
    assert security.approvals.store.claim(ticket.approval_id) is None, "重复领取不执行"
    history = security.approvals.history(limit=10)
    assert any(item.get("approval_id") == ticket.approval_id for item in history)


@pytest.mark.asyncio
async def test_failed_execution_is_persisted_as_failed(tmp_path, db_conn):
    security, _computer, _kinds = _security(tmp_path, db_conn)
    target = security.workspace / "changed.txt"
    target.write_text("original", encoding="utf-8")
    fingerprint = _write_fingerprint(target, "new")
    # 提交审批后对象变化 → 执行器必须拒绝并落库成 failed（带 invalidated 事实）
    target.write_text("changed", encoding="utf-8")
    operation = PendingOperation(
        kind="fs.write",
        label="写入 changed.txt",
        payload={"path": str(target), "content": "new"},
        effect={"kind": "write", "targets": [{"kind": "path", "value": str(target)}]},
        fingerprint=fingerprint,
        mode_at_request="restricted",
        source="tool",
    )
    ticket = await security.approvals.submit(operation)
    await security.approvals.decide(ticket.approval_id, "approve")

    assert await wait_for(
        lambda: (security.approvals.store.get(ticket.approval_id) or {}).get("state")
        in ("completed", "failed")
    )
    row = security.approvals.store.get(ticket.approval_id) or {}
    assert row["state"] == "failed"
    assert "invalidated" in (row.get("error") or "")
    stored = row.get("result") or {}
    if isinstance(stored, str):
        stored = json.loads(stored or "{}")
    assert dict(stored).get("invalidated") is True
    assert target.read_text(encoding="utf-8") == "changed", "失败时不得覆盖目标"
