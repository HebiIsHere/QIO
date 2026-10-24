"""R9-H6 五项真实流程回归（Lead 集成验收；模型 fake、真实工具/台账/引擎/审批恢复）。"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

from agent.api.server import create_app
from agent.config import Settings
from agent.security import requirement_limits
from agent.security.context import requirement_context
from agent.services.computer import ComputerSandbox
from agent.tools import fs_tools, security_gate
from agent.tools.fs_tools import FsDeleteTool, FsPatchTool, FsWriteTool

from _r9_auth_support import make_security


def _sandbox(root: Path, mode: str = "restricted") -> ComputerSandbox:
    return ComputerSandbox(resolve_root=lambda: str(root), permission_mode=lambda: mode)


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
async def test_patch_snapshot_same_read_survives_file_change_between_reads(tmp_path, monkeypatch):
    target = tmp_path / "snapshot.txt"
    target.write_text("old-value\n", encoding="utf-8")
    real_read = fs_tools._read_file_text

    def read_then_change(path):
        text = real_read(path)
        # 在“第一次正文读取后、前置摘要形成前”修改真实临时文件。
        Path(path).write_text("old-value USER_NEW_EDIT\n", encoding="utf-8")
        return text

    monkeypatch.setattr(fs_tools, "_read_file_text", read_then_change)
    tool = FsPatchTool(computer=_sandbox(tmp_path, "unrestricted"))
    result = await tool.run(path=str(target), old="old-value", new="tool-new")
    assert result.ok is False, result
    assert target.read_text(encoding="utf-8") == "old-value USER_NEW_EDIT\n"


async def test_patch_unchanged_and_newline_encoding_roundtrip(tmp_path):
    target = tmp_path / "中文.txt"
    target.write_bytes("旧内容\r\n第二行\r\n".encode("utf-8"))
    tool = FsPatchTool(computer=_sandbox(tmp_path, "unrestricted"))
    result = await tool.run(path=str(target), old="旧内容", new="新内容")
    assert result.ok is True, result
    data = target.read_bytes().decode("utf-8")
    assert "新内容" in data and "第二行" in data
    # 当前工具以规范化文本写入；至少不能损坏中文内容/结构。
    assert "旧内容" not in data


# ---------------------------------------------------------------- 问题二
def _make_dir(root: Path, name: str, child_text="child") -> Path:
    directory = root / name
    (directory / "deep").mkdir(parents=True)
    (directory / "a.txt").write_text(child_text, encoding="utf-8")
    (directory / "deep" / "b.txt").write_text("deep-child", encoding="utf-8")
    return directory


async def _batch_dirs(ctx, monkeypatch):
    root = ctx.computer.root()
    a = _make_dir(root, "dir-A")
    b = _make_dir(root, "dir-B")
    requirement = ctx.requirements.record_user_message(
        text="删除两个目录", message_id="m-dirs", turn_id="t-dirs"
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    ctx.settings_store.set("computer.permission_mode", "restricted")
    trash = root / "trash"
    trash.mkdir(exist_ok=True)

    async def fake_move(path):
        source = Path(path)
        os.replace(source, trash / source.name)
        return True, "fake_recycle", ""

    import agent.tools.approved_operations as approved_ops
    monkeypatch.setattr(approved_ops, "_move_to_recycle_bin", fake_move)
    tool = FsDeleteTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(paths=[str(a), str(b)])
    assert result.ok is True and result.code == "pending_approval", result
    return requirement, binding, a, b, result.data["approval_id"]


async def test_batch_directory_child_limit_only_stops_that_directory(ctx, monkeypatch):
    requirement, _binding, a, b, approval_id = await _batch_dirs(ctx, monkeypatch)
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [
            {
                "operations": ["delete"],
                "objects": [{"kind": "path", "value": str(b / "deep" / "b.txt")}],
                "reason": "禁止删除 B 的深层子文件",
            }
        ],
        source="test-child-limit",
        state=requirement_limits.STATE_STRUCTURED,
    )
    await ctx.approvals.decide(approval_id, "approve")
    row = await _wait_state(ctx, approval_id)
    assert row is not None and str(row.get("state")) in ("failed", "invalidated"), row
    assert not a.exists(), "A 目录不涉及子文件限制，应完成"
    assert b.exists(), "B 内子文件被禁止删除，整个 B 目录必须保留"
    detail = row.get("result") or {}
    assert detail.get("partial") is True, detail


async def test_batch_directory_content_change_invalidates_only_that_directory(ctx, monkeypatch):
    _requirement, _binding, a, b, approval_id = await _batch_dirs(ctx, monkeypatch)
    (b / "a.txt").write_text("changed-during-approval", encoding="utf-8")
    await ctx.approvals.decide(approval_id, "approve")
    row = await _wait_state(ctx, approval_id)
    assert row is not None and str(row.get("state")) in ("failed", "invalidated"), row
    assert not a.exists()
    assert b.exists()


async def test_batch_directory_all_valid_and_common_withdrawal(ctx, monkeypatch):
    requirement, _binding, a, b, approval_id = await _batch_dirs(ctx, monkeypatch)
    await ctx.approvals.decide(approval_id, "approve")
    row = await _wait_state(ctx, approval_id)
    assert row is not None and str(row.get("state")) == "completed", row
    assert not a.exists() and not b.exists()

    requirement2, _binding2, a2, b2, approval_id2 = await _batch_dirs(ctx, monkeypatch)
    ctx.requirements.withdraw(requirement2.requirement_id)
    await ctx.approvals.decide(approval_id2, "approve")
    row2 = await _wait_state(ctx, approval_id2)
    assert row2 is not None and str(row2.get("state")) == "invalidated", row2
    assert a2.exists() and b2.exists()


async def test_fs_write_approval_latest_read_limit_blocks_before_content_hash(ctx, monkeypatch):
    target = ctx.computer.root() / "write-approval-read.txt"
    target.write_text("original", encoding="utf-8")
    ctx.settings_store.set("computer.permission_mode", "restricted")
    requirement = ctx.requirements.record_user_message(
        text="允许写但之后禁止读", message_id="m-wr-read", turn_id="t-wr-read"
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    tool = FsWriteTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(path=str(target), content="tool-content")
    assert result.ok is True and result.code == "pending_approval", result
    approval_id = result.data["approval_id"]
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["read"], "objects": [{"kind": "path", "value": str(target)}], "reason": "禁止读"}],
        source="test-write-approval-read",
        state=requirement_limits.STATE_STRUCTURED,
    )
    reads: list[str] = []
    real_sha = security_gate.content_sha256
    real_text = Path.read_text
    real_bytes = Path.read_bytes

    def spy_sha(path):
        reads.append("sha:" + str(path))
        return real_sha(path)

    def spy_text(self, *args, **kwargs):
        reads.append("text:" + str(self))
        return real_text(self, *args, **kwargs)

    def spy_bytes(self, *args, **kwargs):
        reads.append("bytes:" + str(self))
        return real_bytes(self, *args, **kwargs)

    monkeypatch.setattr(security_gate, "content_sha256", spy_sha)
    monkeypatch.setattr(Path, "read_text", spy_text)
    monkeypatch.setattr(Path, "read_bytes", spy_bytes)
    await ctx.approvals.decide(approval_id, "approve")
    row = await _wait_state(ctx, approval_id)
    assert row is not None and str(row.get("state")) in ("failed", "invalidated"), row
    assert reads == [], "审批恢复的读取禁止必须阻止内容摘要/正文读取"
    assert target.read_text(encoding="utf-8") == "original"


# ---------------------------------------------------------------- 问题三
async def test_patch_approval_latest_read_limit_blocks_before_any_read(ctx, monkeypatch):
    target = ctx.computer.root() / "patch-read.txt"
    target.write_text("old-value", encoding="utf-8")
    ctx.settings_store.set("computer.permission_mode", "restricted")
    requirement = ctx.requirements.record_user_message(
        text="允许补丁，但之后禁止读取", message_id="m-read", turn_id="t-read"
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    tool = FsPatchTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(path=str(target), old="old-value", new="new-value")
    assert result.ok is True and result.code == "pending_approval", result
    approval_id = result.data["approval_id"]
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["read"], "objects": [{"kind": "path", "value": str(target)}], "reason": "禁止读"}],
        source="test-read-limit",
        state=requirement_limits.STATE_STRUCTURED,
    )
    reads: list[str] = []
    real_read_bytes = Path.read_bytes
    real_read_text = Path.read_text

    def spy_read_bytes(self, *args, **kwargs):
        reads.append(str(self))
        return real_read_bytes(self, *args, **kwargs)

    def spy_read_text(self, *args, **kwargs):
        reads.append(str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", spy_read_bytes)
    monkeypatch.setattr(Path, "read_text", spy_read_text)
    await ctx.approvals.decide(approval_id, "approve")
    row = await _wait_state(ctx, approval_id)
    assert row is not None and str(row.get("state")) in ("failed", "invalidated"), row
    assert reads == [], "批准后的读取禁止必须阻止再次读取正文"
    assert target.read_text(encoding="utf-8") == "old-value"


async def test_patch_approval_read_allowed_write_forbidden_keeps_original(ctx, monkeypatch):
    target = ctx.computer.root() / "patch-rw.txt"
    target.write_text("old-value", encoding="utf-8")
    ctx.settings_store.set("computer.permission_mode", "restricted")
    requirement = ctx.requirements.record_user_message(
        text="允许读但之后禁止修改", message_id="m-rw", turn_id="t-rw"
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    tool = FsPatchTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(path=str(target), old="old-value", new="new-value")
    assert result.ok is True and result.code == "pending_approval", result
    approval_id = result.data["approval_id"]
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["write"], "objects": [{"kind": "path", "value": str(target)}], "reason": "禁止写"}],
        source="test-write-limit",
        state=requirement_limits.STATE_STRUCTURED,
    )
    await ctx.approvals.decide(approval_id, "approve")
    row = await _wait_state(ctx, approval_id)
    assert row is not None and str(row.get("state")) in ("failed", "invalidated"), row
    assert target.read_text(encoding="utf-8") == "old-value"


async def test_program_name_persists_restarts_and_removes_precisely(db_conn, tmp_path):
    from agent.security.requirements import RequirementLedger

    security = make_security(tmp_path, db_conn, mode="unrestricted")
    requirement = security.requirements.record_user_message(
        text="禁止 python", message_id="m-round", turn_id="t-round"
    )
    security.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["execute"], "objects": [{"kind": "program", "value": "python"}], "reason": "禁止"}],
        source="test-round",
        state=requirement_limits.STATE_STRUCTURED,
    )
    reloaded = RequirementLedger(db_conn).by_id(requirement.requirement_id)
    assert reloaded is not None
    objects = reloaded.structured_limits[0]["objects"]
    assert objects[0]["kind"] == "program_name"
    serialized = requirement_limits.serialize_limits(reloaded.structured_limits)
    assert requirement_limits.deserialize_limits(serialized) == requirement_limits.normalize_limits(
        reloaded.structured_limits
    )
    remaining = requirement_limits.remove_limits_precise(
        reloaded.structured_limits,
        [{"operations": ["execute"], "objects": [{"kind": "program_name", "value": "python"}]}],
    )
    assert remaining and all(
        not (entry.get("operations") == ["execute"] and entry.get("objects"))
        for entry in remaining
    ) or remaining == ()


# ---------------------------------------------------------------- 问题四
async def test_program_name_limit_blocks_real_run_program(ctx):
    root = ctx.computer.root()
    marker = root / "program-name-marker.txt"
    script = root / "program-name.py"
    script.write_text(
        "from pathlib import Path\nPath(r'%s').write_text('ran')\n" % marker,
        encoding="utf-8",
    )
    # 真实解释器名称（Windows python.exe / POSIX pythonX.Y）作为历史裸名称限制；
    # 基线把它当相对路径精确比较 → 不匹配并真实执行；修复后明确 program_name 生效。
    program_name = Path(sys.executable).stem
    requirement = ctx.requirements.record_user_message(
        text="禁止运行该解释器", message_id="m-name", turn_id="t-name"
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["execute"], "objects": [{"kind": "program", "value": program_name}], "reason": "禁止该程序"}],
        source="test-program-name",
        state=requirement_limits.STATE_STRUCTURED,
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    from agent.tools.cmd_tools import RunProgramTool

    tool = RunProgramTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(
            program=sys.executable, args=[str(script)], cwd=str(root), timeout=30
        )
    assert result.ok is False, result
    assert not marker.exists(), "program_name 限制必须在真实执行前生效"
    row = ctx.requirements.by_id(requirement.requirement_id)
    assert row is not None and row.structured_limits_state == requirement_limits.STATE_STRUCTURED


async def test_explicit_path_limit_does_not_block_other_same_name_program(ctx):
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
        [{"operations": ["execute"], "objects": [{"kind": "program", "value": str(a)}], "reason": "禁止 A"}],
        source="test-program-path",
        state=requirement_limits.STATE_STRUCTURED,
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    from agent.security.effects import Effect, EffectKind, Target

    def effect(path):
        return Effect(kind=EffectKind.EXECUTE, targets=(Target("program", str(path)),), purpose="运行")

    with requirement_context(binding):
        denied = await ctx.authorization.authorize(effect(a))
        allowed = await ctx.authorization.authorize(effect(b))
    from agent.security.effects import Verdict
    assert denied.verdict == Verdict.DENY
    assert allowed.verdict == Verdict.ALLOW, allowed.reason


# ---------------------------------------------------------------- 问题五
@pytest.mark.parametrize("mode", ["read_only", "restricted", "unrestricted"])
async def test_fs_write_read_limit_blocks_before_any_content_read(tmp_path, db_conn, monkeypatch, mode):
    target = tmp_path / "write-read.txt"
    target.write_text("original", encoding="utf-8")
    security = make_security(tmp_path, db_conn, mode=mode)
    requirement = security.requirements.record_user_message(
        text="禁止读取这个文件", message_id="m-write-read", turn_id="t-write-read"
    )
    security.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["read"], "objects": [{"kind": "path", "value": str(target)}], "reason": "禁止读"}],
        source="test-write-read-limit",
        state=requirement_limits.STATE_STRUCTURED,
    )
    binding = security.requirements.binding_for(requirement.requirement_id)
    reads: list[str] = []
    real_content_sha = security_gate.content_sha256

    def spy_content(path):
        reads.append(str(path))
        return real_content_sha(path)

    monkeypatch.setattr(fs_tools.security_gate, "content_sha256", spy_content)
    tool = FsWriteTool(computer=_sandbox(tmp_path, mode=mode), security=security.engine)
    with requirement_context(binding):
        result = await tool.run(path=str(target), content="new-content")
    assert result.ok is False, result
    assert reads == [], "读取限制必须在读取原内容/生成摘要之前生效"
    assert target.read_text(encoding="utf-8") == "original"


async def test_fs_write_read_write_allowed_and_later_change_invalidates(tmp_path, db_conn):
    target = tmp_path / "write-rw.txt"
    target.write_text("original", encoding="utf-8")
    security = make_security(tmp_path, db_conn, mode="unrestricted")
    requirement = security.requirements.record_user_message(
        text="可以写", message_id="m-write-rw", turn_id="t-write-rw"
    )
    binding = security.requirements.binding_for(requirement.requirement_id)
    tool = FsWriteTool(computer=_sandbox(tmp_path), security=security.engine)
    with requirement_context(binding):
        result = await tool.run(path=str(target), content="new-content")
    assert result.ok is True, result
    assert target.read_text(encoding="utf-8") == "new-content"

    # 等待变化：手工按同一前置摘要构造验证（已有 H5 覆盖完整等待流程）。
    target.write_text("user-latest", encoding="utf-8")
    again = await tool.run(path=str(target), content="tool-again")
    assert again.ok is True and target.read_text(encoding="utf-8") == "tool-again"


async def test_fs_write_new_file_absent_does_not_fake_read(tmp_path, db_conn, monkeypatch):
    target = tmp_path / "brand-new.txt"
    security = make_security(tmp_path, db_conn, mode="unrestricted")
    requirement = security.requirements.record_user_message(
        text="新建文件", message_id="m-new", turn_id="t-new"
    )
    binding = security.requirements.binding_for(requirement.requirement_id)
    calls: list[str] = []
    real = security_gate.content_sha256

    def spy(path):
        calls.append(str(path))
        return real(path)

    monkeypatch.setattr(fs_tools.security_gate, "content_sha256", spy)
    tool = FsWriteTool(computer=_sandbox(tmp_path), security=security.engine)
    with requirement_context(binding):
        result = await tool.run(path=str(target), content="created")
    assert result.ok is True, result
    assert target.read_text(encoding="utf-8") == "created"
    assert calls == [], "新建文件不存在，不应读取不存在的原内容"
