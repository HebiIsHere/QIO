"""R9-H5 六项真实流程回归（Lead 集成验收；模型 fake，真实工具/台账/引擎/审批）。"""

from __future__ import annotations

import asyncio
import os
import sys
import types
from pathlib import Path

import pytest

from agent.api.server import create_app
from agent.config import Settings
from agent.security import requirement_limits
from agent.security.context import requirement_context
from agent.security.effects import DECISION_UNRESOLVED_LIMITS, AuthDecision, Effect, EffectKind, Target, Verdict
from agent.security.operations import ExecutionContext, PendingOperation
from agent.security.requirements import RequirementBinding
from agent.services.computer import ComputerSandbox
from agent.tools import fs_tools
from agent.tools.fs_tools import FsPatchTool, FsWriteTool
from agent.tools.security_types import AuthDecision as ToolAuthDecision
from agent.tools.cmd_tools import RunProgramTool

from _r9_auth_support import make_security


def _sandbox(root: Path, mode: str = "restricted") -> ComputerSandbox:
    return ComputerSandbox(resolve_root=lambda: str(root), permission_mode=lambda: mode)


class BlockingGateway:
    """模型/人工等待替身：在 write/execute 授权时卡住，供测试修改真实文件。"""

    def __init__(self, *, mode: str = "restricted", block_actions=("write", "execute")):
        self._mode = mode
        self.block_actions = set(block_actions)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.effects: list = []

    def mode(self):
        return self._mode

    def instruction(self):
        return None

    def internal_data_paths(self):
        return []

    async def authorize(self, effect, *, ctx=None):
        self.effects.append(effect)
        action = str((getattr(effect, "detail", {}) or {}).get("action") or "")
        kind = str(getattr(getattr(effect, "kind", None), "value", effect.kind))
        if action in self.block_actions or kind in self.block_actions:
            self.started.set()
            await self.release.wait()
        return AuthDecision(verdict=Verdict.ALLOW, reason="fake:allow")

    async def submit(self, operation, *, ctx=None):
        return None


async def test_fs_write_file_changed_during_model_wait_is_not_overwritten(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("before", encoding="utf-8")
    gateway = BlockingGateway()
    tool = FsWriteTool(computer=_sandbox(tmp_path), security=gateway)
    task = asyncio.create_task(tool.run(path=str(target), content="tool-after"))
    await asyncio.wait_for(gateway.started.wait(), 5)
    target.write_text("user-during-wait", encoding="utf-8")
    gateway.release.set()
    result = await task
    assert result.ok is False
    assert "发生变化" in str(result.error or "") or "未执行" in str(result.error or "")
    assert target.read_text(encoding="utf-8") == "user-during-wait"


async def test_fs_write_new_file_created_during_wait_is_not_overwritten(tmp_path):
    target = tmp_path / "new.txt"
    gateway = BlockingGateway()
    tool = FsWriteTool(computer=_sandbox(tmp_path), security=gateway)
    task = asyncio.create_task(tool.run(path=str(target), content="tool"))
    await asyncio.wait_for(gateway.started.wait(), 5)
    target.write_text("user-created", encoding="utf-8")
    gateway.release.set()
    result = await task
    assert result.ok is False
    assert target.read_text(encoding="utf-8") == "user-created"


async def test_fs_write_target_deleted_during_wait_is_not_recreated(tmp_path):
    target = tmp_path / "gone.txt"
    target.write_text("before", encoding="utf-8")
    gateway = BlockingGateway()
    tool = FsWriteTool(computer=_sandbox(tmp_path), security=gateway)
    task = asyncio.create_task(tool.run(path=str(target), content="tool"))
    await asyncio.wait_for(gateway.started.wait(), 5)
    target.unlink()
    gateway.release.set()
    result = await task
    assert result.ok is False
    assert not target.exists()


async def test_fs_write_target_replaced_during_wait_is_not_overwritten(tmp_path):
    target = tmp_path / "replaced.txt"
    target.write_text("before", encoding="utf-8")
    gateway = BlockingGateway()
    tool = FsWriteTool(computer=_sandbox(tmp_path), security=gateway)
    task = asyncio.create_task(tool.run(path=str(target), content="tool"))
    await asyncio.wait_for(gateway.started.wait(), 5)
    target.unlink()
    target.write_text("user-replacement", encoding="utf-8")
    gateway.release.set()
    result = await task
    assert result.ok is False
    assert target.read_text(encoding="utf-8") == "user-replacement"


async def test_fs_patch_file_changed_during_model_wait_is_not_overwritten(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("old-value", encoding="utf-8")
    gateway = BlockingGateway(block_actions=("patch",))
    tool = FsPatchTool(computer=_sandbox(tmp_path), security=gateway)
    task = asyncio.create_task(tool.run(path=str(target), old="old-value", new="tool-new"))
    await asyncio.wait_for(gateway.started.wait(), 5)
    target.write_text("user-new-value", encoding="utf-8")
    gateway.release.set()
    result = await task
    assert result.ok is False
    assert target.read_text(encoding="utf-8") == "user-new-value"


@pytest.mark.parametrize("mode", ["read_only", "restricted", "unrestricted"])
async def test_fs_patch_read_limit_blocks_before_body_read(tmp_path, monkeypatch, db_conn, mode):
    target = tmp_path / "private.txt"
    target.write_text("old", encoding="utf-8")
    security = make_security(tmp_path, db_conn, mode=mode)
    requirement = security.requirements.record_user_message(
        text="不要读取这个文件", message_id="m", turn_id="t"
    )
    security.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["read"], "objects": [{"kind": "path", "value": str(target)}], "reason": "禁止读取"}],
        source="test",
        state=requirement_limits.STATE_STRUCTURED,
    )
    binding = security.requirements.binding_for(requirement.requirement_id)
    reads: list[str] = []
    original = fs_tools._read_file_text

    def _spy(path):
        reads.append(str(path))
        return original(path)

    monkeypatch.setattr(fs_tools, "_read_file_text", _spy)
    tool = FsPatchTool(computer=_sandbox(tmp_path), security=security.engine)
    with requirement_context(binding):
        result = await tool.run(path=str(target), old="old", new="new")
    assert result.ok is False
    assert not reads, "读取限制必须在读取正文之前生效"
    assert target.read_text(encoding="utf-8") == "old"


async def test_fs_patch_read_allowed_write_forbidden_does_not_modify(tmp_path, db_conn):
    target = tmp_path / "a.txt"
    target.write_text("old", encoding="utf-8")
    security = make_security(tmp_path, db_conn, mode="restricted")
    requirement = security.requirements.record_user_message(
        text="可以读但不能写", message_id="m2", turn_id="t2"
    )
    security.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["write"], "objects": [{"kind": "path", "value": str(target)}], "reason": "禁止写"}],
        source="test",
        state=requirement_limits.STATE_STRUCTURED,
    )
    binding = security.requirements.binding_for(requirement.requirement_id)
    tool = FsPatchTool(computer=_sandbox(tmp_path), security=security.engine)
    with requirement_context(binding):
        result = await tool.run(path=str(target), old="old", new="new")
    assert result.ok is False
    assert target.read_text(encoding="utf-8") == "old"


async def test_fs_patch_read_write_allowed_completes(tmp_path, db_conn):
    target = tmp_path / "a.txt"
    target.write_text("old", encoding="utf-8")
    security = make_security(tmp_path, db_conn, mode="unrestricted")
    requirement = security.requirements.record_user_message(
        text="可以补丁这个文件", message_id="m3", turn_id="t3"
    )
    binding = security.requirements.binding_for(requirement.requirement_id)
    tool = FsPatchTool(computer=_sandbox(tmp_path), security=security.engine)
    with requirement_context(binding):
        result = await tool.run(path=str(target), old="old", new="new")
    assert result.ok is True, result.error
    assert target.read_text(encoding="utf-8") == "new"


async def test_script_unchanged_executes_in_direct_path(tmp_path):
    script = tmp_path / "run_ok.py"
    marker = tmp_path / "ok.txt"
    script.write_text(
        "from pathlib import Path\nPath(r'%s').write_text('ok')\n" % marker,
        encoding="utf-8",
    )
    gateway = BlockingGateway(block_actions=())
    tool = RunProgramTool(computer=_sandbox(tmp_path), security=gateway)
    result = await tool.run(
        program=sys.executable, args=[str(script)], cwd=str(tmp_path), timeout=30
    )
    assert result.ok is True, result
    assert marker.read_text(encoding="utf-8") == "ok"


async def test_script_changed_during_model_wait_is_not_executed(tmp_path):
    script = tmp_path / "run_me.py"
    marker = tmp_path / "executed.txt"
    script.write_text(
        "from pathlib import Path\n"
        "Path(r'%s').write_text('original')\n" % marker,
        encoding="utf-8",
    )
    gateway = BlockingGateway(block_actions=("execute",))
    tool = RunProgramTool(computer=_sandbox(tmp_path), security=gateway)
    task = asyncio.create_task(
        tool.run(program=sys.executable, args=[str(script)], cwd=str(tmp_path), timeout=30)
    )
    await asyncio.wait_for(gateway.started.wait(), 5)
    script.write_text(
        "from pathlib import Path\n"
        "Path(r'%s').write_text('replaced')\n" % marker,
        encoding="utf-8",
    )
    gateway.release.set()
    result = await task
    assert result.ok is False, result
    assert not marker.exists(), "等待期间替换的脚本不能被直接执行"
