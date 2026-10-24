"""R9-H7 四类真实流程回归（Lead 集成验收；模型 fake、真实工具/台账/引擎/审批）。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from agent.api.server import create_app
from agent.config import Settings
from agent.security import requirement_limits
from agent.security.context import requirement_context
from agent.security.effects import Effect, EffectKind, Target, Verdict
from agent.tools import fs_tools, security_gate
from agent.tools.cmd_tools import RunProgramTool
from agent.tools.fs_tools import FsDeleteTool, FsPatchTool, FsWriteTool

from _r9_auth_support import make_security


@pytest.fixture()
def ctx(db_conn, tmp_path):
    return create_app(Settings(data_dir=tmp_path / "data"), db_conn).state.ctx


async def _wait_state(ctx, approval_id: str, states=("completed", "failed", "invalidated"), timeout=8):
    deadline = asyncio.get_running_loop().time() + timeout
    row = None
    while asyncio.get_running_loop().time() < deadline:
        row = ctx.approvals.store.get(approval_id)
        if row and str(row.get("state")) in states:
            return row
        await asyncio.sleep(0.03)
    return row


# ---------------------------------------------------------------- 问题一
async def test_program_name_entry_and_target_version_are_distinct(ctx):
    root = ctx.computer.root()
    marker = root / "entry-marker.txt"
    script = root / "entry.py"
    script.write_text(
        "from pathlib import Path\nPath(r'%s').write_text('ran')\n" % marker,
        encoding="utf-8",
    )
    requirement = ctx.requirements.record_user_message(
        text="禁止执行 python", message_id="m-entry", turn_id="t-entry"
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["execute"], "objects": [{"kind": "program", "value": "python"}], "reason": "禁止 python"}],
        source="test-entry",
        state=requirement_limits.STATE_STRUCTURED,
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    tool = RunProgramTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(program="python", args=[str(script)], cwd=str(root), timeout=30)
    assert result.ok is False, result
    assert not marker.exists(), "调用入口 python 必须命中程序名称限制"

    # 不同版本入口不能按去点/名称主体被误拦；同一入口才命中。
    def effect(entry: str):
        return Effect(
            kind=EffectKind.EXECUTE,
            targets=(Target("program", str(root / "some-python")),),
            purpose="运行",
            detail={"requested_program": entry, "execution": {"program": entry}},
        )

    requirement2 = ctx.requirements.record_user_message(
        text="只禁止 python3.11", message_id="m-ver", turn_id="t-ver"
    )
    ctx.requirements.set_structured_limits(
        requirement2.requirement_id,
        [{"operations": ["execute"], "objects": [{"kind": "program_name", "value": "python3.11"}], "reason": "版本"}],
        source="test-version",
        state=requirement_limits.STATE_STRUCTURED,
    )
    binding2 = ctx.requirements.binding_for(requirement2.requirement_id)
    with requirement_context(binding2):
        same = await ctx.authorization.authorize(effect("python3.11"))
        other = await ctx.authorization.authorize(effect("python3.12"))
    assert same.verdict == Verdict.DENY
    assert other.verdict == Verdict.ALLOW, other.reason


async def test_program_entry_alias_preserved_and_path_limit_scoped(ctx):
    root = ctx.computer.root()
    a = root / "A" / "tool"
    b = root / "B" / "tool"
    a.parent.mkdir(); b.parent.mkdir()
    a.write_text("A", encoding="utf-8"); b.write_text("B", encoding="utf-8")
    requirement = ctx.requirements.record_user_message(
        text="只禁止 A/tool", message_id="m-path", turn_id="t-path"
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["execute"], "objects": [{"kind": "program", "value": str(a)}], "reason": "路径"}],
        source="test-path",
        state=requirement_limits.STATE_STRUCTURED,
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")

    def effect(path):
        return Effect(kind=EffectKind.EXECUTE, targets=(Target("program", str(path)),), purpose="运行")

    with requirement_context(binding):
        denied = await ctx.authorization.authorize(effect(a))
        allowed = await ctx.authorization.authorize(effect(b))
    assert denied.verdict == Verdict.DENY
    assert allowed.verdict == Verdict.ALLOW, allowed.reason


# ---------------------------------------------------------------- 问题二
async def _internal_file(ctx):
    data_dir = Path(getattr(ctx.settings, "data_dir"))
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "r9h7-internal.log"
    target.write_text("internal-v1", encoding="utf-8")
    assert security_gate.match_internal_data(ctx.authorization, target) is not None
    return target


async def test_internal_maintenance_creates_three_confirmation_without_preread(ctx):
    target = await _internal_file(ctx)
    ctx.settings_store.set("computer.permission_mode", "restricted")
    security = ctx.authorization
    requirement = ctx.requirements.record_user_message(
        text="维护内部日志", message_id="m-maint", turn_id="t-maint"
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    reads: list[str] = []
    real_sha = security_gate.content_sha256
    real_bytes = Path.read_bytes
    real_text = Path.read_text

    def spy_sha(path):
        reads.append("sha:" + str(path))
        return real_sha(path)

    def spy_bytes(self, *args, **kwargs):
        reads.append("bytes:" + str(self))
        return real_bytes(self, *args, **kwargs)

    def spy_text(self, *args, **kwargs):
        reads.append("text:" + str(self))
        return real_text(self, *args, **kwargs)

    import agent.tools.fs_tools as fs_module
    import pytest as _pytest

    # monkeypatch fixture is added as argument by pytest; local aliases for readability.
    tool = FsWriteTool(computer=ctx.computer, approvals=ctx.approvals, security=security)
    # Patch module-level helper used by fs_tools.
    from unittest.mock import patch

    with patch.object(fs_module.security_gate, "content_sha256", spy_sha), patch.object(
        Path, "read_bytes", spy_bytes
    ), patch.object(Path, "read_text", spy_text):
        with requirement_context(binding):
            result = await tool.run(path=str(target), content="internal-v2")
    assert result.ok is True and result.code == "pending_approval", result
    approval_id = result.data["approval_id"]
    assert int(result.data.get("confirmations_required") or 1) == 3
    assert reads == [], "三次确认前不得读取内部文件正文/摘要"
    assert target.read_text(encoding="utf-8") == "internal-v1"

    await ctx.approvals.confirm(approval_id, 1)
    await ctx.approvals.confirm(approval_id, 2)
    await ctx.approvals.confirm(approval_id, 3)
    row = ctx.approvals.store.get(approval_id) or {}
    if str(row.get("state")) == "waiting":
        await ctx.approvals.decide(approval_id, "approve")
    row = await _wait_state(ctx, approval_id, ("completed", "failed", "invalidated"))
    assert row is not None and str(row.get("state")) == "completed", row
    assert target.read_text(encoding="utf-8") == "internal-v2"


async def test_internal_maintenance_explicit_read_limit_still_denied(ctx):
    target = await _internal_file(ctx)
    ctx.settings_store.set("computer.permission_mode", "restricted")
    requirement = ctx.requirements.record_user_message(
        text="禁止读取内部文件", message_id="m-maint-read", turn_id="t-maint-read"
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["read"], "objects": [{"kind": "path", "value": str(target)}], "reason": "禁止读"}],
        source="test-maint-read",
        state=requirement_limits.STATE_STRUCTURED,
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    tool = FsWriteTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(path=str(target), content="should-not-write")
    assert result.ok is False, result
    assert target.read_text(encoding="utf-8") == "internal-v1"


# ---------------------------------------------------------------- 问题三
async def test_delete_read_limit_blocks_before_content_digest(ctx, monkeypatch):
    target = ctx.computer.root() / "delete-secret.txt"
    target.write_text("secret", encoding="utf-8")
    requirement = ctx.requirements.record_user_message(
        text="禁止读取该文件", message_id="m-del-read", turn_id="t-del-read"
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["read"], "objects": [{"kind": "path", "value": str(target)}], "reason": "禁止读"}],
        source="test-del-read",
        state=requirement_limits.STATE_STRUCTURED,
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    reads: list[str] = []
    real_sha = security_gate.content_sha256
    real_bytes = Path.read_bytes

    def spy_sha(path):
        reads.append(str(path))
        return real_sha(path)

    def spy_bytes(self, *args, **kwargs):
        reads.append(str(self))
        return real_bytes(self, *args, **kwargs)

    monkeypatch.setattr(security_gate, "content_sha256", spy_sha)
    monkeypatch.setattr(Path, "read_bytes", spy_bytes)
    tool = FsDeleteTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(paths=[str(target)])
    assert result.ok is False, result
    assert reads == [], "删除检查前不得读取被禁止读取的内容"
    assert target.exists()


async def test_delete_directory_child_read_limit_blocks_before_child_reads(ctx, monkeypatch):
    directory = ctx.computer.root() / "delete-dir"
    directory.mkdir()
    child = directory / "child.txt"
    child.write_text("secret-child", encoding="utf-8")
    requirement = ctx.requirements.record_user_message(
        text="禁止读取子文件", message_id="m-dir-read", turn_id="t-dir-read"
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["read"], "objects": [{"kind": "path", "value": str(child)}], "reason": "禁止读"}],
        source="test-dir-read",
        state=requirement_limits.STATE_STRUCTURED,
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    reads: list[str] = []
    real_sha = security_gate.content_sha256

    def spy_sha(path):
        reads.append(str(path))
        return real_sha(path)

    monkeypatch.setattr(security_gate, "content_sha256", spy_sha)
    tool = FsDeleteTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(paths=[str(directory)])
    assert result.ok is False, result
    assert reads == [], "目录删除不能先读取禁读子文件"
    assert child.exists()


async def test_delete_approval_recovery_read_limit_blocks_before_snapshot_reads(ctx, monkeypatch):
    target = ctx.computer.root() / "delete-approval-read.txt"
    target.write_text("secret", encoding="utf-8")
    ctx.settings_store.set("computer.permission_mode", "restricted")
    requirement = ctx.requirements.record_user_message(
        text="删除文件，但之后禁止读", message_id="m-del-appr", turn_id="t-del-appr"
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    tool = FsDeleteTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(paths=[str(target)])
    assert result.ok is True and result.code == "pending_approval", result
    approval_id = result.data["approval_id"]
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["read"], "objects": [{"kind": "path", "value": str(target)}], "reason": "禁止读"}],
        source="test-del-approval-read",
        state=requirement_limits.STATE_STRUCTURED,
    )
    reads: list[str] = []
    real_sha = security_gate.content_sha256
    real_bytes = Path.read_bytes

    def spy_sha(path):
        reads.append(str(path))
        return real_sha(path)

    def spy_bytes(self, *args, **kwargs):
        reads.append(str(self))
        return real_bytes(self, *args, **kwargs)

    monkeypatch.setattr(security_gate, "content_sha256", spy_sha)
    monkeypatch.setattr(Path, "read_bytes", spy_bytes)
    await ctx.approvals.decide(approval_id, "approve")
    row = await _wait_state(ctx, approval_id)
    assert row is not None and str(row.get("state")) in ("failed", "invalidated"), row
    assert reads == [], "删除审批恢复也不得先读取被禁读内容"
    assert target.exists()


async def test_delete_allowed_read_forbidden_write_keeps_file(ctx):
    target = ctx.computer.root() / "delete-write-limit.txt"
    target.write_text("keep", encoding="utf-8")
    requirement = ctx.requirements.record_user_message(
        text="允许读但禁止删除", message_id="m-del-write", turn_id="t-del-write"
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["delete"], "objects": [{"kind": "path", "value": str(target)}], "reason": "禁止删"}],
        source="test-del-write",
        state=requirement_limits.STATE_STRUCTURED,
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    tool = FsDeleteTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(paths=[str(target)])
    assert result.ok is False, result
    assert target.exists()


# ---------------------------------------------------------------- 问题四
async def test_recheck_reads_permission_before_content(ctx, monkeypatch):
    target = ctx.computer.root() / "recheck-order.txt"
    target.write_text("old-value", encoding="utf-8")
    ctx.settings_store.set("computer.permission_mode", "restricted")
    requirement = ctx.requirements.record_user_message(
        text="允许补丁但之后禁止读", message_id="m-recheck", turn_id="t-recheck"
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    engine = ctx.authorization
    gate = asyncio.Event()
    started = asyncio.Event()
    real_apply = engine._apply_review

    async def wait_review(effect, context, mode, reason, scope_id):
        started.set()
        await gate.wait()
        return Verdict.ALLOW, "fake allow", None, True

    monkeypatch.setattr(engine, "_apply_review", wait_review)
    tool = FsPatchTool(computer=ctx.computer, approvals=ctx.approvals, security=engine)
    with requirement_context(binding):
        task = asyncio.create_task(tool.run(path=str(target), old="old-value", new="new-value"))
        await asyncio.wait_for(started.wait(), 5)
        # 等待期间新增读禁止
        ctx.requirements.set_structured_limits(
            requirement.requirement_id,
            [{"operations": ["read"], "objects": [{"kind": "path", "value": str(target)}], "reason": "禁止读"}],
            source="test-recheck-order",
            state=requirement_limits.STATE_STRUCTURED,
        )
        reads: list[str] = []
        real_bytes = Path.read_bytes
        real_text = Path.read_text
        real_helper = fs_tools._read_file_text

        def spy_bytes(self, *args, **kwargs):
            reads.append(str(self))
            return real_bytes(self, *args, **kwargs)

        def spy_text(self, *args, **kwargs):
            reads.append(str(self))
            return real_text(self, *args, **kwargs)

        def spy_helper(path):
            reads.append(str(path))
            return real_helper(path)

        monkeypatch.setattr(Path, "read_bytes", spy_bytes)
        monkeypatch.setattr(Path, "read_text", spy_text)
        monkeypatch.setattr(fs_tools, "_read_file_text", spy_helper)
        gate.set()
        result = await task
    assert result.ok is False, result
    assert reads == [], "执行前重核对必须先核读取权限，再读取内容"
    assert target.read_text(encoding="utf-8") == "old-value"
    # 恢复原方法，避免影响后续测试
    engine._apply_review = real_apply
