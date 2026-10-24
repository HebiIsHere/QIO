"""R9-H8 根因修复真实流程回归（Lead 集成验收；模型 fake/mock）。"""

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
from agent.security.effects import Effect, EffectKind, Target, Verdict
from agent.services.computer import ComputerSandbox
from agent.tools import fs_tools, security_gate
from agent.tools.cmd_tools import RunProgramTool
from agent.tools.fs_tools import FsDeleteTool, FsReadTool, FsWriteTool

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
@pytest.mark.parametrize("mode", ["read_only", "restricted", "unrestricted"])
async def test_empty_objects_read_limit_blocks_real_read(tmp_path, db_conn, mode):
    target = tmp_path / "global-read.txt"
    target.write_text("secret", encoding="utf-8")
    security = make_security(tmp_path, db_conn, mode=mode)
    requirement = security.requirements.record_user_message(
        text="不要读取任何文件", message_id="m-gr", turn_id="t-gr"
    )
    security.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["read"], "objects": [], "reason": "整体禁止读取"}],
        source="test-global-read",
        state=requirement_limits.STATE_STRUCTURED,
    )
    binding = security.requirements.binding_for(requirement.requirement_id)
    tool = FsReadTool(computer=_sandbox(tmp_path, mode), security=security.engine)
    with requirement_context(binding):
        result = await tool.run(path=str(target))
    assert result.ok is False, result
    assert "secret" not in str(result.content or "")


async def test_empty_objects_write_and_delete_limits_block_real_tools(tmp_path, db_conn):
    security = make_security(tmp_path, db_conn, mode="restricted")
    target = tmp_path / "global-write.txt"
    target.write_text("original", encoding="utf-8")
    requirement = security.requirements.record_user_message(
        text="不要覆盖任何文件", message_id="m-gw", turn_id="t-gw"
    )
    security.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["write"], "objects": [], "reason": "整体禁止写入"}],
        source="test-global-write",
        state=requirement_limits.STATE_STRUCTURED,
    )
    binding = security.requirements.binding_for(requirement.requirement_id)
    tool = FsWriteTool(computer=_sandbox(tmp_path, "restricted"), security=security.engine)
    with requirement_context(binding):
        result = await tool.run(path=str(target), content="not-written")
    assert result.ok is False, result
    assert target.read_text(encoding="utf-8") == "original"

    requirement2 = security.requirements.record_user_message(
        text="不要删除任何文件", message_id="m-gd", turn_id="t-gd"
    )
    security.requirements.set_structured_limits(
        requirement2.requirement_id,
        [{"operations": ["delete"], "objects": [], "reason": "整体禁止删除"}],
        source="test-global-delete",
        state=requirement_limits.STATE_STRUCTURED,
    )
    binding2 = security.requirements.binding_for(requirement2.requirement_id)
    delete_tool = FsDeleteTool(computer=_sandbox(tmp_path, "restricted"), security=security.engine)
    with requirement_context(binding2):
        deleted = await delete_tool.run(paths=[str(target)])
    assert deleted.ok is False, deleted
    assert target.exists()


async def test_global_execute_limit_blocks_real_program(ctx):
    root = ctx.computer.root()
    marker = root / "global-exec-marker.txt"
    script = root / "global-exec.py"
    script.write_text(
        "from pathlib import Path\nPath(r'%s').write_text('ran')\n" % marker,
        encoding="utf-8",
    )
    requirement = ctx.requirements.record_user_message(
        text="不要运行任何程序", message_id="m-ge", turn_id="t-ge"
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["execute"], "objects": [], "reason": "整体禁止执行"}],
        source="test-global-exec",
        state=requirement_limits.STATE_STRUCTURED,
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    tool = RunProgramTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(program=sys.executable, args=[str(script)], cwd=str(root), timeout=30)
    assert result.ok is False, result
    assert not marker.exists()


# ---------------------------------------------------------------- 问题二
async def test_program_name_object_binding_blocks_alias_entry_and_approval(ctx):
    root = ctx.computer.root()
    marker = root / "program-bind-marker.txt"
    script = root / "program-bind.py"
    script.write_text(
        "from pathlib import Path\nPath(r'%s').write_text('ran')\n" % marker,
        encoding="utf-8",
    )
    requirement = ctx.requirements.record_user_message(
        text="禁止运行 python", message_id="m-pb", turn_id="t-pb"
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["execute"], "objects": [{"kind": "program", "value": "python"}], "reason": "禁止 python"}],
        source="test-program-bind",
        state=requirement_limits.STATE_STRUCTURED,
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    tool = RunProgramTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    python_abs = shutil.which("python")
    assert python_abs, "测试需要 PATH 中的 python"
    with requirement_context(binding):
        named = await tool.run(program="python", args=[str(script)], cwd=str(root), timeout=30)
        absolute = await tool.run(program=python_abs, args=[str(script)], cwd=str(root), timeout=30)
    direct = Effect(
        kind=EffectKind.EXECUTE,
        targets=(Target("program", str(python_abs)),),
        purpose="运行",
        detail={"requested_program": str(python_abs), "execution": {"program": str(python_abs)}},
    )
    with requirement_context(binding):
        direct_decision = await ctx.authorization.authorize(direct)
    assert direct_decision.verdict == Verdict.DENY, direct_decision.reason
    assert named.ok is False and absolute.ok is False
    assert not marker.exists()
    assert not ctx.approvals.pending(), "明确程序禁止不能被普通批准覆盖"

    # 无关程序不被名称相似阻塞。
    other = root / "other-program"
    other.write_text("not-an-exe", encoding="utf-8")
    effect = Effect(kind=EffectKind.EXECUTE, targets=(Target("program", str(other)),), purpose="运行无关程序")
    with requirement_context(binding):
        allowed = await ctx.authorization.authorize(effect)
    assert allowed.verdict == Verdict.ALLOW, allowed.reason


async def test_program_name_does_not_merge_different_versions(tmp_path, db_conn, monkeypatch):
    security = make_security(tmp_path, db_conn, mode="unrestricted")
    # R9-H12 §2/§3.2：不同版本不合并必须由对象事实证明（真实存在的两个不同对象），
    # 不能靠“入口名不同/当前查找失败”推断无关。
    version_dir = tmp_path / "version-bin"
    version_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".exe" if os.name == "nt" else ""
    program_311 = version_dir / ("python3.11" + suffix)
    program_312 = version_dir / ("python3.12" + suffix)
    program_311.write_text("version-3.11", encoding="utf-8")
    program_312.write_text("version-3.12", encoding="utf-8")
    monkeypatch.setenv(
        "PATH", str(version_dir) + os.pathsep + os.environ.get("PATH", "")
    )
    requirement = security.requirements.record_user_message(
        text="只禁止 python3.11", message_id="m-pv", turn_id="t-pv"
    )
    security.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["execute"], "objects": [{"kind": "program_name", "value": "python3.11"}], "reason": "版本"}],
        source="test-program-version",
        state=requirement_limits.STATE_STRUCTURED,
    )
    stored = security.requirements.by_id(requirement.requirement_id)
    bound = stored.structured_limits[0]["objects"][0].get("program_binding")
    assert bound and bound.get("identity"), "保存时必须形成真实对象绑定"
    assert Path(str(bound["path"])).name == program_311.name
    binding = security.requirements.binding_for(requirement.requirement_id)

    def effect(entry, target):
        return Effect(
            kind=EffectKind.EXECUTE,
            targets=(Target("program", str(target)),),
            purpose="运行",
            detail={"requested_program": entry, "execution": {"program": entry}},
        )

    assert program_312.exists() and program_311.exists()
    # 另一版本入口解析到另一个真实对象（identity 不同）→ 可靠无关，继续；
    # 同一版本入口命中保存绑定 → 拒绝。两个目标路径都不存在，也各自提供
    # candidate_not_exists 的可靠无关事实。
    with requirement_context(binding):
        other = await security.engine.authorize(effect("python3.12", tmp_path / "python3.12"))
        same = await security.engine.authorize(effect("python3.11", tmp_path / "python3.11"))
    assert other.verdict == Verdict.ALLOW, other.reason
    assert same.verdict == Verdict.DENY


# ---------------------------------------------------------------- 问题三
async def _internal_file(ctx):
    data_dir = Path(getattr(ctx.settings, "data_dir"))
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "r9h8-internal.log"
    target.write_text("internal-v1", encoding="utf-8")
    assert security_gate.match_internal_data(ctx.authorization, target) is not None
    return target


async def test_internal_maintenance_change_during_confirmations_preserves_edit(ctx):
    target = await _internal_file(ctx)
    ctx.settings_store.set("computer.permission_mode", "restricted")
    requirement = ctx.requirements.record_user_message(
        text="维护内部日志", message_id="m-im", turn_id="t-im"
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    tool = FsWriteTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(path=str(target), content="approved-v2")
    assert result.ok is True and result.code == "pending_approval", result
    approval_id = result.data["approval_id"]
    target.write_text("user-edit-during-confirmation", encoding="utf-8")
    await ctx.approvals.confirm(approval_id, 1)
    await ctx.approvals.confirm(approval_id, 2)
    await ctx.approvals.confirm(approval_id, 3)
    row = ctx.approvals.store.get(approval_id) or {}
    if str(row.get("state")) == "waiting":
        await ctx.approvals.decide(approval_id, "approve")
    row = await _wait_state(ctx, approval_id)
    assert row is not None and str(row.get("state")) in ("failed", "invalidated"), row
    assert target.read_text(encoding="utf-8") == "user-edit-during-confirmation"


# ---------------------------------------------------------------- 问题四
async def test_batch_delete_one_read_denied_other_still_pending(ctx, monkeypatch):
    root = ctx.computer.root()
    a = root / "batch-h8-a.txt"
    b = root / "batch-h8-b.txt"
    a.write_text("A", encoding="utf-8")
    b.write_text("B", encoding="utf-8")
    requirement = ctx.requirements.record_user_message(
        text="删除 A 和 B，但不要读取 B", message_id="m-bd", turn_id="t-bd"
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["read"], "objects": [{"kind": "path", "value": str(b)}], "reason": "禁止读 B"}],
        source="test-batch-read",
        state=requirement_limits.STATE_STRUCTURED,
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    ctx.settings_store.set("computer.permission_mode", "restricted")
    tool = FsDeleteTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(paths=[str(a), str(b)])
    assert result.ok is True and result.code == "pending_approval", result
    statuses = {item["path"]: item["status"] for item in result.data.get("items") or []}
    assert statuses[str(a)] == "waiting"
    assert statuses[str(b)] == "denied"
    pending = ctx.approvals.pending()
    assert len(pending) == 1
    op_detail = pending[0].get("operation_detail") or {}
    approval_payload = op_detail.get("payload") or pending[0].get("payload") or {}
    payload_paths = [entry.get("path") for entry in (approval_payload.get("items") or [])]
    assert str(a) in payload_paths and str(b) not in payload_paths


# ---------------------------------------------------------------- 问题五
async def test_internal_directory_delete_no_preread_and_confirm(ctx, monkeypatch):
    data_dir = Path(getattr(ctx.settings, "data_dir"))
    logs = data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    directory = logs / "r9h8-internal-dir"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "log.txt").write_text("internal-dir-content", encoding="utf-8")
    assert security_gate.match_internal_data(ctx.authorization, directory) is not None
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
    ctx.settings_store.set("computer.permission_mode", "restricted")
    requirement = ctx.requirements.record_user_message(
        text="删除内部日志目录", message_id="m-id", turn_id="t-id"
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    tool = FsDeleteTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(paths=[str(directory)])
    assert result.ok is True and result.code == "pending_approval", result
    assert int(result.data.get("confirmations_required") or 1) == 3
    assert reads == [], "内部目录三次确认前不得读取正文/摘要"
    approval_id = result.data["approval_id"]
    trash = Path(ctx.computer.root()) / "fake-trash-h8"
    trash.mkdir(exist_ok=True)

    async def fake_move(path):
        source = Path(path)
        os.replace(source, trash / source.name)
        return True, "fake_recycle", ""

    import agent.tools.approved_operations as approved_ops
    monkeypatch.setattr(approved_ops, "_move_to_recycle_bin", fake_move)
    await ctx.approvals.confirm(approval_id, 1)
    await ctx.approvals.confirm(approval_id, 2)
    await ctx.approvals.confirm(approval_id, 3)
    row = ctx.approvals.store.get(approval_id) or {}
    if str(row.get("state")) == "waiting":
        await ctx.approvals.decide(approval_id, "approve")
    row = await _wait_state(ctx, approval_id)
    assert row is not None and str(row.get("state")) == "completed", row
    assert not directory.exists()


# ---------------------------------------------------------------- 问题六
async def test_unchanged_script_approval_executes(ctx):
    root = ctx.computer.root()
    script = root / "unchanged-script.py"
    marker = root / "unchanged-marker.txt"
    script.write_text(
        "from pathlib import Path\nPath(r'%s').write_text('ok')\n" % marker,
        encoding="utf-8",
    )
    ctx.settings_store.set("computer.permission_mode", "restricted")
    requirement = ctx.requirements.record_user_message(
        text="运行这个脚本", message_id="m-unchanged", turn_id="t-unchanged"
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    tool = RunProgramTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(program=sys.executable, args=[str(script)], cwd=str(root), timeout=30)
    assert result.ok is True and result.code == "pending_approval", result
    approval_id = result.data["approval_id"]
    await ctx.approvals.decide(approval_id, "approve")
    row = await _wait_state(ctx, approval_id)
    assert row is not None and str(row.get("state")) == "completed", row
    assert marker.read_text(encoding="utf-8") == "ok"


async def test_changed_script_approval_does_not_execute(ctx):
    root = ctx.computer.root()
    script = root / "changed-script.py"
    marker = root / "changed-marker.txt"
    script.write_text(
        "from pathlib import Path\nPath(r'%s').write_text('original')\n" % marker,
        encoding="utf-8",
    )
    ctx.settings_store.set("computer.permission_mode", "restricted")
    tool = RunProgramTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    result = await tool.run(program=sys.executable, args=[str(script)], cwd=str(root), timeout=30)
    assert result.ok is True and result.code == "pending_approval", result
    approval_id = result.data["approval_id"]
    script.write_text(
        "from pathlib import Path\nPath(r'%s').write_text('replaced')\n" % marker,
        encoding="utf-8",
    )
    await ctx.approvals.decide(approval_id, "approve")
    row = await _wait_state(ctx, approval_id)
    assert row is not None and str(row.get("state")) in ("failed", "invalidated"), row
    assert not marker.exists()
