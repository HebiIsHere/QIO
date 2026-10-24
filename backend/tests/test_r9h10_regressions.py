"""R9-H10 程序限制绑定生命周期与跨操作一致性回归。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from agent.api.server import create_app
from agent.config import Settings
from agent.security import requirement_limits
from agent.security.context import requirement_context
from agent.security.requirements import RequirementLedger
from agent.security.effects import Effect, EffectKind, Target, Verdict
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools import fs_tools, security_gate
from agent.tools.cmd_tools import RunProgramTool
from agent.tools.fs_tools import FsDeleteTool, FsReadTool, FsWriteTool

from _r9_auth_support import make_security


@pytest.fixture()
def ctx(db_conn, tmp_path):
    return create_app(Settings(data_dir=tmp_path / "data"), db_conn).state.ctx


def _runner(directory: Path, name: str = "runner") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt" and not name.lower().endswith(".exe"):
        name = name + ".exe"
    target = directory / name
    shutil.copy2(sys.executable, target)
    if os.name != "nt":
        target.chmod(0o755)
    return target


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
def test_deserialize_is_pure_under_different_paths(monkeypatch):
    raw = json.dumps(
        [
            {
                "operations": ["execute"],
                "objects": [{"kind": "program_name", "value": "runner"}],
                "reason": "禁止 runner",
            }
        ],
        ensure_ascii=False,
    )
    calls = {"count": 0}
    real_resolve = requirement_limits._resolve_program_binding

    def spy(name):
        calls["count"] += 1
        return real_resolve(name)

    monkeypatch.setattr(requirement_limits, "_resolve_program_binding", spy)
    old_path = os.environ.get("PATH", "")
    try:
        for directory in ("dir-a", "dir-b"):
            os.environ["PATH"] = str(Path("/tmp") / directory) + os.pathsep + old_path
            parsed = requirement_limits.deserialize_limits(raw)
            assert parsed is not None
            assert "program_binding" not in parsed[0]["objects"][0]
            serialized = requirement_limits.serialize_limits(parsed)
            assert "program_binding" not in serialized
    finally:
        os.environ["PATH"] = old_path
    assert calls["count"] == 0, "反序列化/规范化不能查找程序或采集身份"


def test_binding_survives_close_reopen_without_environment_rebind(tmp_path, monkeypatch):
    db_path = tmp_path / "bind-lifecycle.db"
    conn = connect(db_path)
    apply_migrations(conn)
    ledger = RequirementLedger(conn)
    old_path = os.environ.get("PATH", "")
    dir_a = tmp_path / "bind-a"
    dir_b = tmp_path / "bind-b"
    runner_a = _runner(dir_a)
    runner_b = _runner(dir_b)
    monkeypatch.setenv("PATH", str(dir_a) + os.pathsep + old_path)
    requirement = ledger.record_user_message(text="禁止 runner", message_id="m-bind", turn_id="t-bind")
    ledger.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["execute"], "objects": [{"kind": "program_name", "value": "runner"}], "reason": "禁止"}],
        source="test-bind",
        state=requirement_limits.STATE_STRUCTURED,
    )
    stored = ledger.by_id(requirement.requirement_id)
    bound = stored.structured_limits[0]["objects"][0]["program_binding"]
    assert bound["path"] == str(runner_a.resolve())
    assert bound["identity"]
    original_json = conn.execute(
        "SELECT structured_limits FROM user_requirements WHERE id = ?",
        (requirement.requirement_id,),
    ).fetchone()[0]
    conn.close()

    monkeypatch.setenv("PATH", str(dir_b) + os.pathsep + old_path)
    conn2 = connect(db_path)
    try:
        reloaded = RequirementLedger(conn2).by_id(requirement.requirement_id)
        assert reloaded is not None
        raw_after = conn2.execute(
            "SELECT structured_limits FROM user_requirements WHERE id = ?",
            (requirement.requirement_id,),
        ).fetchone()[0]
        assert raw_after == original_json
        obj = reloaded.structured_limits[0]["objects"][0]
        assert obj["program_binding"]["path"] == str(runner_a.resolve())
        assert obj["program_binding"]["identity"] == bound["identity"]
        assert obj["program_binding"]["requirement_version"] == bound["requirement_version"]
    finally:
        conn2.close()


def test_historical_unbound_is_not_automatically_bound(tmp_path):
    raw = json.dumps(
        [{"operations": ["execute"], "objects": [{"kind": "program_name", "value": "runner"}], "reason": "旧记录"}],
        ensure_ascii=False,
    )
    parsed = requirement_limits.deserialize_limits(raw)
    assert parsed is not None
    assert "program_binding" not in parsed[0]["objects"][0]
    # 纯数据层不会因为当前环境可解析就补建绑定
    assert parsed == requirement_limits.normalize_limits(parsed)


# ---------------------------------------------------------------- 问题二/三
async def test_program_name_write_limit_blocks_real_file_write(ctx, tmp_path, monkeypatch):
    runner = _runner(tmp_path / "name-write")
    script = Path(ctx.computer.root()) / "name-write.py"
    script.write_text("print('x')\n", encoding="utf-8")
    old_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", str(runner.parent) + os.pathsep + old_path)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    requirement = ctx.requirements.record_user_message(
        text="禁止修改 runner", message_id="m-nw", turn_id="t-nw"
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["write"], "objects": [{"kind": "program_name", "value": "runner"}], "reason": "禁止修改"}],
        source="test-name-write",
        state=requirement_limits.STATE_STRUCTURED,
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    print("DEBUG_NAME_WRITE", ctx.requirements.by_id(requirement.requirement_id).structured_limits)
    tool = FsWriteTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(path=str(runner), content="overwritten")
    assert result.ok is False, result
    assert runner.read_bytes() != b"overwritten"


async def test_program_name_read_limit_blocks_hardlink_read(ctx, tmp_path, monkeypatch):
    runner = _runner(tmp_path / "name-read")
    hard = tmp_path / "name-read-hardlink"
    try:
        os.link(runner, hard)
    except (OSError, NotImplementedError):
        pytest.skip("平台不支持硬链接")
    old_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", str(runner.parent) + os.pathsep + old_path)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    requirement = ctx.requirements.record_user_message(
        text="禁止读取 runner", message_id="m-nr", turn_id="t-nr"
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["read"], "objects": [{"kind": "program_name", "value": "runner"}], "reason": "禁止读"}],
        source="test-name-read",
        state=requirement_limits.STATE_STRUCTURED,
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    reads: list[str] = []
    real_bytes = Path.read_bytes
    real_text = Path.read_text

    def spy_bytes(self, *args, **kwargs):
        reads.append(str(self))
        return real_bytes(self, *args, **kwargs)

    def spy_text(self, *args, **kwargs):
        reads.append(str(self))
        return real_text(self, *args, **kwargs)

    import pytest as _pytest
    monkeypatch.setattr(Path, "read_bytes", spy_bytes)
    monkeypatch.setattr(Path, "read_text", spy_text)
    tool = FsReadTool(computer=ctx.computer, security=ctx.authorization)
    with requirement_context(binding):
        original = await tool.run(path=str(runner))
        linked = await tool.run(path=str(hard))
    assert original.ok is False and linked.ok is False
    assert reads == []
    # 同内容复制件不是同一对象，允许读取（独立于名称绑定路径）
    copy = tmp_path / "name-read-copy"
    copy.write_bytes(runner.read_bytes())
    copy_effect = Effect(
        kind=EffectKind.READ,
        targets=(Target("path", str(copy)),),
        purpose="读取同内容复制件",
    )
    with requirement_context(binding):
        copied = await ctx.authorization.authorize(copy_effect)
    assert copied.verdict == Verdict.ALLOW, copied.reason
    try:
        hard.unlink()
    except OSError:
        pass


async def test_program_name_delete_limit_blocks_target(ctx, tmp_path, monkeypatch):
    runner = _runner(tmp_path / "name-delete")
    old_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", str(runner.parent) + os.pathsep + old_path)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    requirement = ctx.requirements.record_user_message(
        text="禁止删除 runner", message_id="m-nd", turn_id="t-nd"
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["delete"], "objects": [{"kind": "program_name", "value": "runner"}], "reason": "禁止删"}],
        source="test-name-delete",
        state=requirement_limits.STATE_STRUCTURED,
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    tool = FsDeleteTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(paths=[str(runner)])
    assert result.ok is False, result
    assert runner.exists()


# ---------------------------------------------------------------- 问题四
async def test_binding_replacement_blocks_absolute_and_keeps_hardlink_protected(ctx, tmp_path, monkeypatch):
    runner = _runner(tmp_path / "replace-a")
    hard = tmp_path / "replace-a-hardlink"
    try:
        os.link(runner, hard)
    except (OSError, NotImplementedError):
        pytest.skip("平台不支持硬链接")
    replacement = _runner(tmp_path / "replace-b", "replacement")
    old_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", str(runner.parent) + os.pathsep + old_path)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    requirement = ctx.requirements.record_user_message(
        text="禁止 runner", message_id="m-rp", turn_id="t-rp"
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["execute"], "objects": [{"kind": "program_name", "value": "runner"}], "reason": "禁止"}],
        source="test-replacement",
        state=requirement_limits.STATE_STRUCTURED,
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    # 原路径替换为另一个对象
    os.replace(replacement, runner)
    script = Path(ctx.computer.root()) / "replacement-run.py"
    script.write_text("from pathlib import Path\nPath(r'%s').write_text('ran')\n" % (Path(ctx.computer.root()) / "replacement-marker.txt"), encoding="utf-8")
    tool = RunProgramTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    with requirement_context(binding):
        replaced = await tool.run(program=str(runner), args=[str(script)], cwd=str(ctx.computer.root()), timeout=30)
        old_hardlink = await tool.run(program=str(hard), args=[str(script)], cwd=str(ctx.computer.root()), timeout=30)
    assert replaced.ok is False and old_hardlink.ok is False
    assert "绑定" in str(replaced.error or ""), replaced.error
    assert "程序退出码" not in str(old_hardlink.error or ""), old_hardlink.error
    assert not (Path(ctx.computer.root()) / "replacement-marker.txt").exists()
    assert not ctx.approvals.pending()
    try:
        hard.unlink()
    except OSError:
        pass
