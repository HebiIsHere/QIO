"""三模式判定引擎（契约 §2.3、§12）。"""

from __future__ import annotations

import pytest

from agent.security.context import agent_scope_context
from agent.security.effects import Effect, EffectKind, Target, Verdict

from _r9_auth_support import make_security, with_instruction


def path_effect(kind, path, **detail):
    return Effect(
        kind=kind,
        targets=(Target("path", str(path)),),
        purpose="测试用途",
        detail=dict(detail),
    )


async def verdict(engine, effect):
    return await engine.authorize(effect)


# -- read_only ---------------------------------------------------------------


async def test_read_only_allows_read_but_denies_mutation(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="read_only")
    engine = security.engine
    assert (await verdict(engine, path_effect(EffectKind.READ, tmp_path / "a.txt"))).verdict == Verdict.ALLOW
    for kind in (
        EffectKind.WRITE,
        EffectKind.CREATE,
        EffectKind.DELETE,
        EffectKind.EXECUTE,
        EffectKind.INSTALL_DEPS,
        EffectKind.SYSTEM_CHANGE,
        EffectKind.TOOL_REGISTER,
    ):
        decision = await verdict(engine, path_effect(kind, tmp_path / "a.txt"))
        assert decision.verdict == Verdict.DENY, kind
        assert "只读模式" in decision.reason


async def test_read_only_still_denies_credential_reads(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="read_only")
    decision = await verdict(security.engine, path_effect(EffectKind.READ, tmp_path / ".env"))
    assert decision.verdict == Verdict.DENY
    assert "凭据" in decision.reason


# -- restricted --------------------------------------------------------------


async def test_restricted_read_allows(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="restricted")
    assert (await verdict(security.engine, path_effect(EffectKind.READ, tmp_path / "a.txt"))).verdict == Verdict.ALLOW


async def test_restricted_qio_generated_work_file_allows(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="restricted")
    effect = path_effect(EffectKind.WRITE, security.workspace / "报告.md", origin="qio_generated")
    decision = await verdict(security.engine, effect)
    assert decision.verdict == Verdict.ALLOW
    assert "QIO 生成" in decision.reason


async def test_restricted_user_file_write_and_delete_need_approval(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="restricted")
    write = await verdict(security.engine, path_effect(EffectKind.WRITE, tmp_path / "a.txt"))
    assert write.verdict == Verdict.APPROVE
    delete = await verdict(security.engine, path_effect(EffectKind.DELETE, tmp_path / "a.txt"))
    assert delete.verdict == Verdict.APPROVE


async def test_explicit_instruction_grant_allows_write_without_approval(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="restricted")
    target = tmp_path / "a.docx"
    async with with_instruction(security, "把 " + str(target) + " 第 3 段改成 X 并保存"):
        decision = await verdict(security.engine, path_effect(EffectKind.WRITE, target))
        assert decision.verdict == Verdict.ALLOW
        assert "你的指令" in decision.reason
        assert decision.scope_id


async def test_delete_grant_is_not_derived_from_write_grant(db_conn, tmp_path):
    """「修改项目」不得扩张出删除授权（契约 §2.3）。"""
    security = make_security(tmp_path, db_conn, mode="restricted")
    target = tmp_path / "a.docx"
    async with with_instruction(security, "修改 " + str(target)):
        write = await verdict(security.engine, path_effect(EffectKind.WRITE, target))
        delete = await verdict(security.engine, path_effect(EffectKind.DELETE, target))
        assert write.verdict == Verdict.ALLOW
        assert delete.verdict == Verdict.APPROVE


async def test_explicit_delete_grant_allows_delete(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="restricted")
    target = tmp_path / "a.tmp"
    async with with_instruction(security, "删除 " + str(target)):
        decision = await verdict(security.engine, path_effect(EffectKind.DELETE, target))
        assert decision.verdict == Verdict.ALLOW
        assert "回收站" in decision.reason


async def test_project_repair_grant_allows_execute_in_project(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="restricted")
    project = tmp_path / "proj"
    project.mkdir()
    async with with_instruction(security, "修复 " + str(project) + " 里的这个项目"):
        execute = await verdict(security.engine, path_effect(EffectKind.EXECUTE, project))
        assert execute.verdict == Verdict.ALLOW
    # 指令结束后 grant 关闭：同样操作重新需要审批
    after = await verdict(security.engine, path_effect(EffectKind.EXECUTE, project))
    assert after.verdict == Verdict.APPROVE


async def test_restricted_command_rules(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="restricted")
    shell = await verdict(security.engine, path_effect(EffectKind.EXECUTE, tmp_path, shell=True))
    assert shell.verdict == Verdict.APPROVE
    assert "shell" in shell.reason
    tool_env = await verdict(
        security.engine, path_effect(EffectKind.INSTALL_DEPS, tmp_path, env_scope="tool_env")
    )
    assert tool_env.verdict == Verdict.ALLOW
    system_install = await verdict(security.engine, path_effect(EffectKind.INSTALL_DEPS, tmp_path))
    assert system_install.verdict == Verdict.APPROVE
    register = await verdict(security.engine, path_effect(EffectKind.TOOL_REGISTER, tmp_path))
    assert register.verdict == Verdict.ALLOW


async def test_restricted_network_egress_rules(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="restricted")
    cloud = Effect(
        kind=EffectKind.NETWORK_EGRESS,
        targets=(Target("url", "https://api.example.com/v1"),),
        detail={"scope": "configured_cloud_model"},
    )
    assert (await verdict(security.engine, cloud)).verdict == Verdict.ALLOW
    third_party = Effect(
        kind=EffectKind.NETWORK_EGRESS,
        targets=(Target("url", "https://unknown.example.org/upload"),),
        detail={"material_summary": "报告.pdf"},
    )
    decision = await verdict(security.engine, third_party)
    assert decision.verdict == Verdict.APPROVE
    assert "第三方" in decision.reason


# -- unrestricted ------------------------------------------------------------


async def test_unrestricted_allows_normal_operations(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="unrestricted")
    for kind in (EffectKind.WRITE, EffectKind.DELETE, EffectKind.EXECUTE):
        decision = await verdict(security.engine, path_effect(kind, tmp_path / "a.txt"))
        assert decision.verdict == Verdict.ALLOW, kind


# -- 内部运行数据：所有模式都要三次确认 -------------------------------------


@pytest.mark.parametrize("mode", ["read_only", "restricted", "unrestricted"])
async def test_internal_data_requires_confirmations_in_every_mode(db_conn, tmp_path, mode):
    protected = tmp_path / "app.db"
    protected.write_text("db", encoding="utf-8")
    security = make_security(tmp_path, db_conn, mode=mode, protected=[protected])
    decision = await verdict(security.engine, path_effect(EffectKind.WRITE, protected))
    assert decision.verdict == Verdict.APPROVE
    assert "三次确认" in decision.reason
    assert "内部运行数据" in decision.reason


# -- 子智能体能力子集 --------------------------------------------------------


async def test_subagent_cannot_expand_parent_scope(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="restricted")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    from agent.security.context import instruction_context

    instruction = security.instructions.begin(
        turn_id="turn_parent", text="修复 " + str(allowed) + " 里的这个项目"
    )
    inside = path_effect(EffectKind.WRITE, allowed / "a.py")
    outside = path_effect(EffectKind.WRITE, tmp_path / "outside.py")
    with instruction_context(instruction):
        scope = security.engine.child_scope(purpose="子任务")
        assert scope.depth >= 1
        with agent_scope_context(scope):
            inside_decision = await security.engine.authorize(inside)
            outside_decision = await security.engine.authorize(outside)
    assert inside_decision.verdict == Verdict.ALLOW
    assert outside_decision.verdict == Verdict.APPROVE
    assert "主智能体" in outside_decision.reason
    security.instructions.end(instruction)


# -- 多效果合成 --------------------------------------------------------------


async def test_authorize_many_deny_wins(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="read_only")
    allowed = path_effect(EffectKind.READ, tmp_path / "a.txt")
    denied = path_effect(EffectKind.WRITE, tmp_path / "a.txt")
    decision = await security.engine.authorize_many([allowed, denied])
    assert decision.verdict == Verdict.DENY
    assert len(decision.effects) == 2
