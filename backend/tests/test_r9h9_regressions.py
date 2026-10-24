"""R9-H9 共同规则真实流程回归。"""

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
from agent.tools import fs_tools, security_gate
from agent.tools.cmd_tools import RunProgramTool
from agent.tools.fs_tools import FsDeleteTool, FsWriteTool

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


def _python_copy(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    shutil.copy2(sys.executable, target)
    if os.name != "nt":
        target.chmod(0o755)
    return target


def _script(root: Path, marker: Path, text="ran") -> Path:
    script = root / ("r9h9-" + marker.name + ".py")
    script.write_text(
        "from pathlib import Path\nPath(r'%s').write_text('%s')\n" % (marker, text),
        encoding="utf-8",
    )
    return script


# ---------------------------------------------------------------- 问题一
async def test_internal_directory_incomplete_no_approval(ctx):
    data_dir = Path(getattr(ctx.settings, "data_dir"))
    directory = data_dir / "logs" / "r9h9-big"
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(502):
        (directory / ("f%03d.txt" % index)).write_text("x", encoding="utf-8")
    ctx.settings_store.set("computer.permission_mode", "restricted")
    tool = FsDeleteTool(computer=ctx.computer, security=ctx.authorization)
    result = await tool.run(paths=[str(directory)])
    assert result.ok is False, result
    assert not ctx.approvals.pending(), "影响不完整的目录不能创建可执行审批"
    assert directory.exists()
    items = {item["path"]: item["status"] for item in result.data.get("items") or []}
    assert items[str(directory)] == "invalidated"


async def test_internal_directory_execution_incomplete_fails(ctx, monkeypatch):
    data_dir = Path(getattr(ctx.settings, "data_dir"))
    directory = data_dir / "logs" / "r9h9-exec-incomplete"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "a.txt").write_text("a", encoding="utf-8")
    ctx.settings_store.set("computer.permission_mode", "restricted")
    tool = FsDeleteTool(computer=ctx.computer, security=ctx.authorization)
    result = await tool.run(paths=[str(directory)])
    assert result.ok is True and result.code == "pending_approval", result
    approval_id = result.data["approval_id"]
    # 审批后增加超过上限的子文件，执行时影响不完整
    for index in range(510):
        (directory / ("late%03d.txt" % index)).write_text("x", encoding="utf-8")
    monkeypatch.setattr(fs_tools, "DIRECTORY_IMPACT_MAX_ENTRIES", 10)
    await ctx.approvals.confirm(approval_id, 1)
    await ctx.approvals.confirm(approval_id, 2)
    await ctx.approvals.confirm(approval_id, 3)
    row = ctx.approvals.store.get(approval_id) or {}
    if str(row.get("state")) == "waiting":
        await ctx.approvals.decide(approval_id, "approve")
    row = await _wait_state(ctx, approval_id)
    assert row is not None and str(row.get("state")) in ("failed", "invalidated"), row
    assert directory.exists()


# ---------------------------------------------------------------- 问题二
async def test_hardlink_same_program_object_blocked(ctx, tmp_path, monkeypatch):
    root = ctx.computer.root()
    script = _script(root, root / "hardlink-marker.txt")
    real = Path(sys.executable).resolve()
    hard = real.with_name(("r9h9-hardlink-" + str(os.getpid()) + ".exe") if os.name == "nt" else ("r9h9-hardlink-" + str(os.getpid())))
    try:
        os.link(real, hard)
    except (OSError, NotImplementedError):
        pytest.skip("平台不支持硬链接")
    # 路径限制：原路径禁止，硬链接同一对象也必须拒绝
    requirement = ctx.requirements.record_user_message(
        text="禁止这个程序", message_id="m-hl", turn_id="t-hl"
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["execute"], "objects": [{"kind": "program", "value": str(real)}], "reason": "路径禁止"}],
        source="test-hardlink",
        state=requirement_limits.STATE_STRUCTURED,
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    tool = RunProgramTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    try:
        with requirement_context(binding):
            result = await tool.run(program=str(hard), args=[str(script)], cwd=str(root), timeout=30)
        assert result.ok is False, result
    finally:
        try:
            hard.unlink()
        except OSError:
            pass
    # 同内容复制件不是同一对象：复制到 dir2 的 runner 允许
    dir2 = tmp_path / "hl2"
    copy = _python_copy(dir2, "runner-copy.exe" if os.name == "nt" else "runner-copy")
    effect = Effect(kind=EffectKind.EXECUTE, targets=(Target("program", str(copy)),), purpose="运行复制件")
    with requirement_context(binding):
        allowed = await ctx.authorization.authorize(effect)
    assert allowed.verdict == Verdict.ALLOW, allowed.reason


# ---------------------------------------------------------------- 问题三
async def test_program_binding_survives_path_change_and_restart(ctx, tmp_path, monkeypatch):
    root = ctx.computer.root()
    script = _script(root, root / "path-marker.txt")
    dir1 = tmp_path / "path1"
    dir2 = tmp_path / "path2"
    a = _python_copy(dir1, "runner.exe" if os.name == "nt" else "runner")
    b = _python_copy(dir2, "runner.exe" if os.name == "nt" else "runner")
    old_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", str(dir1) + os.pathsep + old_path)
    requirement = ctx.requirements.record_user_message(
        text="禁止运行 runner", message_id="m-pathbind", turn_id="t-pathbind"
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["execute"], "objects": [{"kind": "program", "value": "runner"}], "reason": "禁止 runner"}],
        source="test-pathbind",
        state=requirement_limits.STATE_STRUCTURED,
    )
    row = ctx.requirements.by_id(requirement.requirement_id)
    binding_obj = row.structured_limits[0]["objects"][0]
    assert binding_obj.get("program_binding", {}).get("identity"), binding_obj
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    tool = RunProgramTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    # 改变查找顺序：名称入口不能自动从 A 转移到 B
    monkeypatch.setenv("PATH", str(dir2) + os.pathsep + old_path)
    with requirement_context(binding):
        named = await tool.run(program="runner", args=[str(script)], cwd=str(root), timeout=30)
        absolute_a = await tool.run(program=str(a), args=[str(script)], cwd=str(root), timeout=30)
    assert named.ok is False and absolute_a.ok is False
    # 重启后从持久化 JSON 恢复同一绑定
    from agent.security.requirements import RequirementLedger

    reloaded = RequirementLedger(ctx.requirements.conn).by_id(requirement.requirement_id)
    binding2 = ctx.requirements.binding_for(requirement.requirement_id)
    with requirement_context(binding2):
        again = await tool.run(program=str(a), args=[str(script)], cwd=str(root), timeout=30)
    assert reloaded is not None and again.ok is False


# ---------------------------------------------------------------- 问题四
async def test_program_write_limit_does_not_block_execute(ctx, tmp_path, monkeypatch):
    root = ctx.computer.root()
    script = _script(root, root / "write-limit-marker.txt")
    runner = Path(sys.executable).resolve()
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    requirement = ctx.requirements.record_user_message(
        text="禁止修改 python，但允许运行", message_id="m-wl", turn_id="t-wl"
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [
            {"operations": ["write"], "objects": [{"kind": "program_name", "value": "python"}], "reason": "禁止修改名称"},
            {"operations": ["write"], "objects": [{"kind": "program", "value": str(runner)}], "reason": "禁止修改路径"},
        ],
        source="test-write-limit",
        state=requirement_limits.STATE_STRUCTURED,
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    tool = RunProgramTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(program="python", args=[str(script)], cwd=str(root), timeout=30)
    assert result.ok is True, result
    assert (root / "write-limit-marker.txt").exists()
    # 修改该程序文件仍被 write 限制拒绝（程序路径对象与路径目标按身份匹配）。
    write_tool = FsWriteTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    with requirement_context(binding):
        denied = await write_tool.run(path=str(runner), content="x")
    assert denied.ok is False, denied


# ---------------------------------------------------------------- 问题五
@pytest.mark.parametrize("mode", ["read_only", "restricted", "unrestricted"])
async def test_program_limit_blocks_fixed_query_in_all_modes(ctx, mode):
    ctx.settings_store.set("computer.permission_mode", mode)
    requirement = ctx.requirements.record_user_message(
        text="禁止运行 python", message_id="m-fq", turn_id="t-fq"
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["execute"], "objects": [{"kind": "program", "value": "python"}], "reason": "禁止 python"}],
        source="test-fixed-query-limit",
        state=requirement_limits.STATE_STRUCTURED,
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    tool = RunProgramTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(program="python", args=["--version"], cwd=str(ctx.computer.root()), timeout=30)
    assert result.ok is False, result
