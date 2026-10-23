"""R9-H4 真实工具主流程交叉验收：读取限制、目录内保护子对象的三次确认。

真实 AppContext / RequirementLedger / AuthorizationEngine / fs 工具；模型 fake。
"""

from __future__ import annotations

import os
import uuid

import pytest

from agent.api.server import create_app
from agent.config import Settings
from agent.security import requirement_limits
from agent.security.context import requirement_context
from agent.security.requirements import RequirementBinding, RequirementLedger
from agent.tools.fs_tools import FsDeleteTool, FsReadTool


@pytest.fixture()
def ctx(db_conn, tmp_path):
    return create_app(Settings(data_dir=tmp_path / "data"), db_conn).state.ctx


def _read_deny_limit(path):
    return {
        "operations": ["read"],
        "objects": [{"kind": "path", "value": str(path)}],
        "reason": "用户明确禁止读取这个文件",
    }


async def test_read_limit_blocks_real_fs_read_and_survives_restart(ctx):
    workspace = ctx.computer.root()
    target = workspace / "private.txt"
    target.write_text("secret", encoding="utf-8")
    requirement = ctx.requirements.record_user_message(
        text="不要读取 " + str(target), message_id="m-read", turn_id="t-read"
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [_read_deny_limit(target)],
        source="test",
        state=requirement_limits.STATE_STRUCTURED,
    )
    ctx.settings_store.set("computer.permission_mode", "restricted")
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    assert isinstance(binding, RequirementBinding)

    tool = FsReadTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(path=str(target))
    assert result.ok is False
    assert "禁止读取" in str(result.error or ""), result.error

    # 重启语义：新台账实例从 DB 读回同一限制，仍拒绝真实读取。
    restarted = RequirementLedger(ctx.conn)
    restarted_binding = restarted.binding_for(requirement.requirement_id)
    assert restarted_binding is not None
    with requirement_context(restarted_binding):
        again = await tool.run(path=str(target))
    assert again.ok is False
    assert "禁止读取" in str(again.error or ""), again.error


async def test_directory_with_protected_child_sets_three_confirmations(ctx):
    data_dir = ctx.settings.data_dir
    logs = data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    secret = logs / "secret.db"
    secret.write_text("internal", encoding="utf-8")
    # 可信应用路径同步：把本次创建的内部文件登记到身份台账。
    refreshed = ctx.refresh_internal_data_identities()
    assert refreshed.get("ok") is True, refreshed

    workspace = ctx.computer.root()
    bundle = workspace / ("bundle-" + uuid.uuid4().hex[:8])
    bundle.mkdir()
    linked = bundle / "secret.db"
    try:
        os.link(secret, linked)
    except OSError as exc:
        pytest.skip("平台不支持硬链接，无法验证目录内保护子对象：%s" % exc)

    ctx.settings_store.set("computer.permission_mode", "restricted")
    tool = FsDeleteTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    result = await tool.run(paths=[str(bundle)])
    assert result.ok is True and result.code == "pending_approval", result

    rows = ctx.approvals.pending()
    assert rows, "目录删除应进入审批"
    row = rows[-1]
    assert row.get("requires_confirmations") == 3 or row.get("confirmations_required") == 3, row
    detail = row.get("operation_detail") or {}
    assert (
        detail.get("internal_data_entries")
        or (detail.get("effect") or {}).get("kind") == "internal_data"
    ), "目录内保护子对象不能让审批退化为普通删除"
