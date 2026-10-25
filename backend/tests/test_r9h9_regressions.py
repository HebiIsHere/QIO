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


def _runnable_copy(directory: Path, name: str) -> Path:
    """复制一个在任何部署布局下都**能真正运行**的外部程序。

    为什么不用 Python 解释器副本：解释器被复制到别的目录后能否启动取决于部署布局
    （Windows 缺同名 DLL、Linux 某些版本找不到 prefix），于是断言会变成“假绿/假红”：
    ok=False 可能只是“拷贝跑不起来”，而不是“授权被拒绝”。这里复制系统自带 shell，
    两个副本在任何环境都一定可执行。
    """
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    if os.name == "nt":
        source = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"
    else:
        source = Path("/bin/sh")
    shutil.copy2(source, target)
    if os.name != "nt":
        target.chmod(0o755)
    return target


def _write_marker_args(marker: Path) -> list[str]:
    """参数：让被执行的程序把 marker 写出来（证明“真的运行了”而不只是 ok=True）。"""
    if os.name == "nt":
        return ["/c", "echo ran> " + str(marker)]
    return ["-c", "echo ran > " + str(marker)]

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
    """R9-H9 §2 + R9-H13 §3.B3：程序名称限制固化到保存时的**对象身份**。

    R9-H13 §3.B3（object_state._name_branch，代码注释与 H13 报告的 B1-B3「对象判定过严
    纠正」一致）明确规定：同一个入口名当前解析到与绑定**不同的真实对象**（identity 可读
    且不同、入口路径也不同）时，对象事实证明那是另一个对象，按当前模式继续；只有
    「解析不到 / 绑定结构不完整」才 INVALID → 拒绝。

    所以这条用例守的是身份语义，不是「名字相同就拒绝」：
      1) 保存的 binding.identity/path 就是 A 的真实事实（不是名称字符串）；
      2) 按绝对路径运行 A 永远被拒（禁止不跟着对象走），重启后仍然如此；
      3) PATH 换成 dir2 后按名字运行落到的是 B（另一个真实对象）→ 按 §3.B3 继续，
         并且必须证明真的执行了 B、绑定没有被悄悄改写成 B；
      4) 重启（重读持久化 JSON）后同一绑定不变，A 仍被拒。
    """
    root = ctx.computer.root()
    dir1 = tmp_path / "path1"
    dir2 = tmp_path / "path2"
    name = "runner.exe" if os.name == "nt" else "runner"
    a = _runnable_copy(dir1, name)
    b = _runnable_copy(dir2, name)
    marker = dir2 / "ran-by-b.txt"
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
    saved = binding_obj.get("program_binding") or {}
    from agent.services.file_identity import identity_of

    identity_a = identity_of(a)
    identity_b = identity_of(b)
    assert identity_a is not None and identity_b is not None
    assert identity_a.key != identity_b.key, (identity_a.key, identity_b.key)
    assert saved.get("identity") == identity_a.key, (saved, identity_a.key)
    assert Path(str(saved.get("path"))).name == name
    assert "path1" in str(saved.get("path")), saved
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    tool = RunProgramTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    # 改变查找顺序：同名的**另一个真实对象** B 现在排在前面（§3.B3 允许按当前规则继续）
    monkeypatch.setenv("PATH", str(dir2) + os.pathsep + old_path)
    with requirement_context(binding):
        named = await tool.run(program="runner", args=_write_marker_args(marker), cwd=str(root), timeout=30)
        absolute_a = await tool.run(program=str(a), args=_write_marker_args(marker), cwd=str(root), timeout=30)
    # 名字现在落到 B：真的执行了（marker 出现），且不是把 A 的禁止转移过来；
    # 绑定对象 A 一点也没被放宽。
    assert named.ok is True, named
    assert marker.exists(), named
    assert absolute_a.ok is False, absolute_a
    after = ctx.requirements.by_id(requirement.requirement_id)
    assert (after.structured_limits[0]["objects"][0].get("program_binding") or {}).get("identity") == identity_a.key
    # 重启后从持久化 JSON 恢复同一绑定：A 仍被拒，名字仍落到 B
    from agent.security.requirements import RequirementLedger

    reloaded = RequirementLedger(ctx.requirements.conn).by_id(requirement.requirement_id)
    binding2 = ctx.requirements.binding_for(requirement.requirement_id)
    reloaded_obj = reloaded.structured_limits[0]["objects"][0]["program_binding"]
    reloaded_marker = dir2 / "ran-by-b-after-restart.txt"
    with requirement_context(binding2):
        again = await tool.run(program=str(a), args=_write_marker_args(marker), cwd=str(root), timeout=30)
        named2 = await tool.run(program="runner", args=_write_marker_args(reloaded_marker), cwd=str(root), timeout=30)
    assert reloaded is not None and again.ok is False, again
    assert reloaded_obj.get("identity") == identity_a.key, reloaded_obj
    assert named2.ok is True and reloaded_marker.exists(), named2

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
