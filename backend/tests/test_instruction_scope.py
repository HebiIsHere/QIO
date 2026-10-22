"""指令授权范围（R9-F 契约 §2、§4.7）：关键词不再产生许可，grant 只来自用户批准。

R9 旧实现用"句子里出现修改/删除动作词"直接生成 grant；审查发现这会把
"不要修改 X"也当成授权。R9-F 起：

* `extract_grants` 永久停用（恒返回空），自然语言许可由受限模式模型授权审查
  （security/review.py）理解后经后端验证形成**有界授权记录**；
* 指令对象仍维护 grant 生命周期（申请/关闭/不继承），但 grant 只能由审批服务
  在用户批准后通过 `add_grant` 追加（source=user_approved）。
"""

from __future__ import annotations

import asyncio

from agent.security.context import instruction_context
from agent.security.effects import Effect, EffectKind, Target, Verdict
from agent.security.instructions import InstructionRegistry, Scope
from agent.security.intent import extract_grants

from _r9_auth_support import make_security, with_instruction


def grants(text: str):
    return extract_grants(
        text, instruction_id="instr_test", created_at="2026-10-22T00:00:00+00:00"
    )


def _ops(grant_list) -> set[str]:
    out: set[str] = set()
    for grant in grant_list:
        out |= set(grant.operations)
    return out


# -- 关键词授权已停用：肯定句也不能再直接产生许可 ---------------------------


def test_explicit_file_write_sentence_does_not_produce_grant(tmp_path):
    target = tmp_path / "a.docx"
    assert grants("把 " + str(target) + " 第 3 段改成 X 并保存") == []


def test_fix_project_sentence_does_not_produce_execute_grant(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    grant_list = grants("修复 " + str(project) + " 里的这个项目")
    assert grant_list == []
    assert _ops(grant_list) == set()


def test_delete_sentence_does_not_produce_grant(tmp_path):
    target = tmp_path / "a.tmp"
    assert grants("删除 " + str(target)) == []


def test_negated_sentence_does_not_authorize(tmp_path):
    target = tmp_path / "a.docx"
    # 审查发现的伪造授权案例：否定句在旧实现里会生成写权限。
    assert grants("不要修改 " + str(target)) == []
    assert grants("不要删除 " + str(target)) == []


def test_instruction_begin_does_not_extract_grants(tmp_path):
    registry = InstructionRegistry()
    instruction = registry.begin(turn_id="turn_1", text="修改 D:\\work\\a.txt")
    assert instruction.active_grants() == []
    registry.end(instruction)


# -- grant 生命周期仍成立：只允许审批服务在用户批准后追加 -------------------


def test_instruction_end_closes_every_grant():
    registry = InstructionRegistry()
    instruction = registry.begin(turn_id="turn_1", text="修改 D:\\work\\a.txt")
    grant = registry.add_grant(
        instruction,
        operations={"write"},
        objects=[Scope("path", "D:\\work\\a.txt")],
        purpose="用户批准了这次写入",
    )
    assert grant is not None and instruction.active_grants()
    registry.end(instruction)
    assert all(item.revoked for item in instruction.grants)
    assert instruction.active_grants() == []
    assert registry.current() is None


def test_grant_after_instruction_end_is_refused():
    registry = InstructionRegistry()
    instruction = registry.begin(turn_id="turn_1", text="修改 D:\\work\\a.txt")
    registry.end(instruction)
    assert (
        registry.add_grant(instruction, operations={"write"}, objects=[], purpose="迟到的批准")
        is None
    )
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

    async def check(text, *, approve=False):
        async with with_instruction(security, text, turn_id="turn_x") as instruction:
            if approve:
                assert (
                    security.instructions.add_grant(
                        instruction,
                        operations={"write"},
                        objects=[Scope("path", str(target))],
                        purpose="用户批准了这次写入",
                    )
                    is not None
                )
                with instruction_context(instruction):
                    return await security.engine.authorize(effect)
            return await security.engine.authorize(effect)

    approved = asyncio.run(check("把 " + str(target) + " 改成 X 并保存", approve=True))
    assert approved.verdict == Verdict.ALLOW
    # 后续指令没有提到这个文件，也没有用户批准：不得继承上一条指令的授权
    second = asyncio.run(check("你好，介绍一下你自己"))
    assert second.verdict == Verdict.APPROVE


def test_engine_has_no_model_facing_grant_api(db_conn, tmp_path):
    """模型不能自我授权：引擎/网关没有「添加 grant」的公开入口。"""
    security = make_security(tmp_path, db_conn, mode="restricted")
    assert not hasattr(security.engine, "add_grant")
    assert not hasattr(security.engine, "grant")
    assert grants("") == []
