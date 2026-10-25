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


async def test_read_only_still_denies_registered_internal_data_reads(db_conn, tmp_path):
    # R9-H：名称不再决定权限；真实注册的内部运行数据仍拒绝读取。
    protected = tmp_path / "app.db"
    protected.write_text("db", encoding="utf-8")
    security = make_security(tmp_path, db_conn, mode="read_only", protected=[protected])
    decision = await verdict(security.engine, path_effect(EffectKind.READ, protected))
    assert decision.verdict == Verdict.DENY
    assert "内部运行数据" in decision.reason
    # 普通 .env 同名材料只按普通读取规则，不因名字自动敏感。
    ordinary = await verdict(security.engine, path_effect(EffectKind.READ, tmp_path / ".env"))
    assert ordinary.verdict == Verdict.ALLOW


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
    assert write.needs_review is True
    delete = await verdict(security.engine, path_effect(EffectKind.DELETE, tmp_path / "a.txt"))
    assert delete.verdict == Verdict.APPROVE
    assert delete.needs_review is True


async def test_explicit_instruction_without_review_needs_approval(db_conn, tmp_path):
    """R9-F：关键词提取不再产生许可；没有模型审查时退回普通审批并标明原因。"""
    security = make_security(tmp_path, db_conn, mode="restricted")
    target = tmp_path / "a.docx"
    async with with_instruction(security, "把 " + str(target) + " 第 3 段改成 X 并保存"):
        decision = await verdict(security.engine, path_effect(EffectKind.WRITE, target))
        assert decision.verdict == Verdict.APPROVE
        assert decision.needs_review is True
        assert "暂时无法确认授权范围" in decision.reason
        assert not decision.scope_id


async def test_write_sentence_does_not_derive_delete_permission(db_conn, tmp_path):
    """「修改项目」不得扩张出删除授权；而且两种操作都不能靠关键词放行。"""
    security = make_security(tmp_path, db_conn, mode="restricted")
    target = tmp_path / "a.docx"
    async with with_instruction(security, "修改 " + str(target)):
        write = await verdict(security.engine, path_effect(EffectKind.WRITE, target))
        delete = await verdict(security.engine, path_effect(EffectKind.DELETE, target))
        assert write.verdict == Verdict.APPROVE
        assert delete.verdict == Verdict.APPROVE
        assert write.needs_review is True and delete.needs_review is True


async def test_delete_sentence_without_review_needs_approval(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="restricted")
    target = tmp_path / "a.tmp"
    async with with_instruction(security, "删除 " + str(target)):
        decision = await verdict(security.engine, path_effect(EffectKind.DELETE, target))
        assert decision.verdict == Verdict.APPROVE
        assert decision.needs_review is True


async def test_project_repair_sentence_needs_authorization_review(db_conn, tmp_path):
    """问题 D：目录/项目执行授权不再由关键词直接放行，交模型审查理解必要性。"""
    security = make_security(tmp_path, db_conn, mode="restricted")
    project = tmp_path / "proj"
    project.mkdir()
    async with with_instruction(security, "修复 " + str(project) + " 里的这个项目"):
        execute = await verdict(security.engine, path_effect(EffectKind.EXECUTE, project))
        assert execute.verdict == Verdict.APPROVE
        assert execute.needs_review is True
    # 指令结束后同样需要重新审查/审批（不继承）
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


# -- 用户明确禁止：只分析/不要修改在三种模式都生效 --------------------------


def _set_requirement(security, text: str, *, limits=()):
    """登记一条真实要求并返回绑定 + 结构化限制（R9-G：要求解析模型输出）。"""
    requirement = security.requirements.record_user_message(
        text=text, message_id="msg_test", turn_id="turn_test"
    )
    binding = security.requirements.binding_for(requirement.requirement_id)
    return binding, tuple(limits)


async def test_unrestricted_keys_off_text_but_structured_limit_denies(db_conn, tmp_path):
    """关键词不再决定权限：纯文本"只分析/不要修改"不产生 DENY；结构化限制才生效。"""
    from agent.security.context import requirement_context, requirement_limits_context

    security = make_security(tmp_path, db_conn, mode="unrestricted")
    binding, _ = _set_requirement(security, "只分析这个项目，不要修改任何文件")
    with requirement_context(binding):
        write = await verdict(
            security.engine, path_effect(EffectKind.WRITE, tmp_path / "a.py")
        )
        read = await verdict(
            security.engine, path_effect(EffectKind.READ, tmp_path / "a.py")
        )
    assert write.verdict == Verdict.ALLOW, "纯文本限制不再由关键词判定（语义由模型理解）"
    assert read.verdict == Verdict.ALLOW

    structured = (
        {
            "operations": ["write"],
            "objects": [{"kind": "path_prefix", "value": str(tmp_path)}],
            "reason": "用户明确限制不要修改这个目录",
        },
    )
    with requirement_context(binding), requirement_limits_context(structured):
        write2 = await verdict(
            security.engine, path_effect(EffectKind.WRITE, tmp_path / "a.py")
        )
    assert write2.verdict == Verdict.DENY
    assert "限制" in write2.reason


async def test_structured_limit_denies_delete_in_restricted(db_conn, tmp_path):
    from agent.security.context import requirement_context, requirement_limits_context

    security = make_security(tmp_path, db_conn, mode="restricted")
    binding, _ = _set_requirement(security, "只分析，不要删除这个文件")
    delete_effect = path_effect(EffectKind.DELETE, tmp_path / "a.txt")
    structured = (
        {
            "operations": ["delete"],
            "objects": [{"kind": "path", "value": str(tmp_path / "a.txt")}],
            "reason": "用户明确不要删除这个文件",
        },
    )
    with requirement_context(binding), requirement_limits_context(structured):
        delete = await verdict(security.engine, delete_effect)
    assert delete.verdict == Verdict.DENY
    assert "删除" in delete.reason or "限制" in delete.reason


async def test_structured_limit_only_denies_matching_effect_kind(db_conn, tmp_path):
    from agent.security.context import requirement_context, requirement_limits_context

    security = make_security(tmp_path, db_conn, mode="unrestricted")
    binding, _ = _set_requirement(security, "不要删除这些文件，但可以修改")
    structured = (
        {
            "operations": ["delete"],
            "objects": [{"kind": "path_prefix", "value": str(tmp_path)}],
            "reason": "用户明确不要删除这些文件",
        },
    )
    with requirement_context(binding), requirement_limits_context(structured):
        write = await verdict(
            security.engine, path_effect(EffectKind.WRITE, tmp_path / "a.txt")
        )
        delete = await verdict(
            security.engine, path_effect(EffectKind.DELETE, tmp_path / "a.txt")
        )
    assert write.verdict == Verdict.ALLOW
    assert delete.verdict == Verdict.DENY


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
    # 真实授权现在只能来自用户批准（审批服务 add_grant）；用它验证子智能体子集规则。
    from agent.security.instructions import Scope

    assert (
        security.instructions.add_grant(
            instruction,
            operations={"write", "create"},
            objects=[Scope("path_prefix", str(allowed))],
            purpose="用户批准修复这个项目",
        )
        is not None
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
    # R9-H4 §9：子智能体范围不足必须明确拒绝；普通审查/审批不能覆盖范围不足。
    assert outside_decision.verdict == Verdict.DENY
    assert "范围" in outside_decision.reason or "主智能体" in outside_decision.reason
    security.instructions.end(instruction)


# -- 多效果合成 --------------------------------------------------------------


async def test_authorize_many_deny_wins(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="read_only")
    allowed = path_effect(EffectKind.READ, tmp_path / "a.txt")
    denied = path_effect(EffectKind.WRITE, tmp_path / "a.txt")
    decision = await security.engine.authorize_many([allowed, denied])
    assert decision.verdict == Verdict.DENY
    assert len(decision.effects) == 2
