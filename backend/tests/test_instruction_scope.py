"""指令授权范围：明确指令授权、后续指令不继承、grant 随指令关闭（契约 §2.2、§12）。"""

from __future__ import annotations

from agent.security.context import current_instruction, instruction_context
from agent.security.effects import Effect, EffectKind, Target, Verdict
from agent.security.instructions import InstructionRegistry
from agent.security.intent import extract_grants

from _r9_auth_support import make_security, with_instruction


def grants(text: str):
    return extract_grants(text, instruction_id="instr_test", created_at="2026-10-22T00:00:00+00:00")


def _ops(grant_list) -> set[str]:
    out: set[str] = set()
    for grant in grant_list:
        out |= set(grant.operations)
    return out


def test_explicit_file_write_grant(tmp_path):
    target = tmp_path / "a.docx"
    grant_list = grants("把 " + str(target) + " 第 3 段改成 X 并保存")
    assert len(grant_list) == 1
    grant = grant_list[0]
    assert "write" in grant.operations
    assert grant.objects[0].kind == "path"
    assert grant.objects[0].value == str(target)
    assert grant.source == "user_explicit"


def test_fix_project_grants_write_prefix_and_execute(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    grant_list = grants("修复 " + str(project) + " 里的这个项目")
    assert len(grant_list) == 1
    grant = grant_list[0]
    assert {"write", "create", "execute"} <= set(grant.operations)
    assert grant.objects[0].kind == "path_prefix"
    assert grant.objects[0].value == str(project)


def test_explicit_delete_grants_delete_only(tmp_path):
    target = tmp_path / "a.tmp"
    grant_list = grants("删除 " + str(target))
    assert len(grant_list) == 1
    assert set(grant_list[0].operations) == {"delete"}
    assert grant_list[0].objects[0].kind == "path"


def test_no_grant_without_explicit_action(tmp_path):
    target = tmp_path / "a.docx"
    assert grants(str(target) + " 是什么？") == []


def test_no_grant_without_explicit_object():
    assert grants("帮我修复一下这个") == []
    assert grants("把那段改一下并保存") == []


def test_write_verb_does_not_grant_delete(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    grant_list = grants("修改 " + str(project) + " 里的文件")
    assert "delete" not in _ops(grant_list)


def test_relative_paths_do_not_create_grants():
    """相对路径不凭猜测的工作目录扩权（保守）。"""
    assert grants("修改 ./a.txt") == []
    assert grants("修改 a.txt") == []


def test_instruction_end_closes_every_grant():
    registry = InstructionRegistry()
    instruction = registry.begin(turn_id="turn_1", text="修改 D:\\work\\a.txt")
    assert instruction.active_grants()
    registry.end(instruction)
    assert all(grant.revoked for grant in instruction.grants)
    assert instruction.active_grants() == []
    assert registry.current() is None


def test_grant_after_instruction_end_is_refused():
    registry = InstructionRegistry()
    instruction = registry.begin(turn_id="turn_1", text="修改 D:\\work\\a.txt")
    registry.end(instruction)
    assert registry.add_grant(
        instruction,
        operations={"write"},
        objects=[],
        purpose="迟到的批准",
    ) is None
    # 别的指令对象也不能被追加 grant（批准只能落在当前指令上）
    other = registry.begin(turn_id="turn_2", text="你好")
    assert registry.add_grant(instruction, operations={"write"}, objects=[], purpose="x") is None
    assert registry.add_grant(other, operations={"write"}, objects=[], purpose="y") is not None


def test_new_instruction_does_not_inherit_previous_grants(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="restricted")
    target = tmp_path / "a.docx"
    effect = Effect(
        kind=EffectKind.WRITE,
        targets=(Target("path", str(target)),),
        purpose="测试",
    )
    async def check(text):
        async with with_instruction(security, text, turn_id="turn_x"):
            return await security.engine.authorize(effect)

    import asyncio

    first = asyncio.run(check("把 " + str(target) + " 改成 X 并保存"))
    assert first.verdict == Verdict.ALLOW
    # 后续指令没有提到这个文件：不得继承上一条指令的授权
    second = asyncio.run(check("你好，介绍一下你自己"))
    assert second.verdict == Verdict.APPROVE


def test_engine_has_no_model_facing_grant_api(db_conn, tmp_path):
    """模型不能自我授权：引擎/网关没有「添加 grant」的公开入口。"""
    security = make_security(tmp_path, db_conn, mode="restricted")
    assert not hasattr(security.engine, "add_grant")
    assert not hasattr(security.engine, "grant")
    # 授权提取只接受用户原话文本，不接受模型/工具构造的授权对象
    assert grants("") == []
