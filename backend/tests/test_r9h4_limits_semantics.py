# -*- coding: utf-8 -*-
"""R9-H4 A：限制语义（read/program）、父已知限制继承、精确部分解除与提示词契约。

真实 RequirementLedger / apply_resolution / requirement_limits 合并 / 真实解析器
（fake model_call）；不联网、不用真实 Key；不预构造最终状态。
"""

from __future__ import annotations

import json

import pytest

from agent.prompts import (
    LIMIT_EXTRACTION_CHAIN_SYSTEM,
    LIMIT_EXTRACTION_SYSTEM,
    REQUIREMENT_RESOLUTION_SYSTEM,
)
from agent.security import requirement_limits
from agent.security.requirements import RequirementLedger
from agent.security.resolution import (
    ModelRequirementResolver,
    RequirementRelation,
    RequirementResolutionUnavailable,
    ResolutionOutcome,
    parse_resolution_outcome,
    validate_resolution,
)


@pytest.fixture()
def ledger(db_conn):
    return RequirementLedger(db_conn)


def obj(kind: str, value: str) -> dict:
    return {"kind": kind, "value": value}


def limit(operations, objects=(), reason: str = "测试限制") -> dict:
    return {
        "operations": list(operations),
        "objects": [dict(item) for item in objects],
        "reason": reason,
    }


def persist(ledger, *, state: str, limits=(), text: str = "修复项目，但不要修改 B。"):
    return ledger.record_user_message(
        text=text,
        message_id="msg_h4",
        turn_id="turn_h4",
        structured_state=state,
        structured_limits=tuple(dict(item) for item in limits),
        structured_source="test_setup",
    )


def amend(
    ledger,
    parent,
    *,
    message: str,
    limits=(),
    removed=(),
    message_id: str = "msg_amend",
):
    outcome = ResolutionOutcome(
        relation=RequirementRelation.AMEND,
        target_requirement_id=parent.requirement_id,
        target_version=int(parent.version),
        corrections=(message,),
        limits=tuple(dict(item) for item in limits),
        removed_limits=tuple(dict(item) for item in removed),
        reason="按用户语义补充/修正",
        model="fake-r9h4",
        prompt_version="r9h4-test-resolution",
    )
    return ledger.apply_resolution(
        outcome,
        message_id=message_id,
        turn_id="turn_amend",
        message=message,
    )


def new_requirement(ledger, *, limits=(), message: str = "整理项目。"):
    outcome = ResolutionOutcome(
        relation=RequirementRelation.NEW,
        target_requirement_id=None,
        target_version=None,
        corrections=(),
        limits=tuple(dict(item) for item in limits),
        reason="新要求",
        model="fake-r9h4",
        prompt_version="r9h4-test-resolution",
    )
    return ledger.apply_resolution(
        outcome, message_id="msg_new", turn_id="turn_new", message=message
    )


def limits_of(ledger, requirement_id):
    requirement = ledger.by_id(requirement_id)
    assert requirement is not None
    return requirement


# ---------------------------------------------------------------------------
# 1) read / program：解析、保存、重启全链路一致
# ---------------------------------------------------------------------------


def test_read_and_program_are_canonical_limits():
    assert "read" in requirement_limits.LIMIT_OPERATIONS
    assert "program" in requirement_limits.LIMIT_OBJECT_KINDS
    read_limit = limit(("read",), (obj("path", "D:/secret.txt"),))
    program_limit = limit(
        ("execute",), (obj("program", "python"),), reason="不要运行 python"
    )
    normalized = requirement_limits.normalize_limits([read_limit, program_limit])
    assert {item["operations"][0] for item in normalized} == {"read", "execute"}
    serialized = requirement_limits.serialize_limits(normalized)
    assert requirement_limits.deserialize_limits(serialized) == normalized


def test_resolution_accepts_read_and_program_end_to_end(ledger):
    parent = persist(ledger, state=requirement_limits.STATE_STRUCTURED, limits=())
    raw = json.dumps(
        {
            "relation": "amend",
            "target_requirement_id": parent.requirement_id,
            "target_version": int(parent.version),
            "corrections": ["不要读取口令文件，也不要运行 python"],
            "limits": [
                {
                    "operations": ["read"],
                    "objects": [{"kind": "path", "value": "D:/secret.txt"}],
                    "reason": "不要读取",
                },
                {
                    "operations": ["execute"],
                    "objects": [{"kind": "program", "value": "python"}],
                    "reason": "不要运行 python",
                },
            ],
            "removed_limits": [],
            "reason": "用户明确限制",
        },
        ensure_ascii=False,
    )
    parsed = parse_resolution_outcome(raw)
    validated = validate_resolution(
        parsed, candidates=[{"requirement_id": parent.requirement_id, "version": 1}]
    )
    kept = (parsed.limits[0]["operations"], parsed.limits[1]["objects"][0]["kind"])
    assert validated.limits == parsed.limits

    applied = ledger.apply_resolution(
        validated, message_id="m_read", turn_id="t_read", message="新限制"
    )
    child = applied.requirement
    assert child is not None
    assert child.structured_limits_state == requirement_limits.STATE_STRUCTURED
    assert child.structured_limits is not None
    stored = {item["operations"][0]: item for item in child.structured_limits}
    assert stored["read"]["objects"][0] == {"kind": "path", "value": "D:/secret.txt"}
    # R9-H6 §4：裸 program=python 明确归一为 program_name（不再当路径静默忽略）。
    assert stored["execute"]["objects"][0]["kind"] == "program_name"

    # 重启：新 ledger 实例从同一 DB 读回一致结构
    reloaded = RequirementLedger(ledger.conn).by_id(child.requirement_id)
    assert reloaded is not None
    assert reloaded.structured_limits == child.structured_limits
    assert reloaded.structured_limits_state == requirement_limits.STATE_STRUCTURED
    # 解析器的结构解析也接受同样条目（extract 走 requirement_limits 校验）
    assert kept == (["read"], "program")


async def test_extract_chain_accepts_read_and_program(ledger):
    root = persist(
        ledger,
        state=requirement_limits.STATE_LEGACY,
        limits=(),
        text="不要读取 D:/secret.txt，也不要运行 python。",
    )
    context = ledger.recovery_context(root.requirement_id)
    assert context.complete is True
    calls = {"count": 0}

    def model_call(messages):
        calls["count"] += 1
        return json.dumps(
            {
                "limits": [
                    {
                        "operations": ["read"],
                        "objects": [{"kind": "path", "value": "D:/secret.txt"}],
                        "reason": "不读",
                    },
                    {
                        "operations": ["execute"],
                        "objects": [{"kind": "program", "value": "python"}],
                        "reason": "不运行",
                    },
                ]
            },
            ensure_ascii=False,
        )

    resolver = ModelRequirementResolver(
        adapter_factory=None, model_call=model_call, timeout_seconds=2.0
    )
    limits = await resolver.extract_limits_from_context(
        context, requirement_id=root.requirement_id, version=int(root.version)
    )
    assert calls["count"] == 1
    assert limits[0]["operations"] == ["read"]
    assert limits[1]["objects"][0]["kind"] == "program_name"


# ---------------------------------------------------------------------------
# 2) 父 unresolved 但已有 known limits：amend 必须继承
# ---------------------------------------------------------------------------


def test_unresolved_parent_known_limits_are_inherited_on_amend(ledger):
    b_write = limit(("write",), (obj("path", "D:/proj/B.py"),), "不要修改 B")
    parent = persist(
        ledger, state=requirement_limits.STATE_UNRESOLVED, limits=(b_write,)
    )
    assert parent.structured_limits == (b_write,)

    applied = amend(ledger, parent, message="只把标题改成 X。")
    child = applied.requirement
    assert child is not None
    # 父未恢复完整：子保持 unresolved，但必须继承 known
    assert child.structured_limits_state == requirement_limits.STATE_UNRESOLVED
    assert child.structured_limits == (b_write,)
    assert "partial_parent_known" in str(child.structured_limits_source or "")

    # 再补充一次：仍继承原 known
    applied2 = amend(ledger, child, message="再加一句结论。")
    child2 = applied2.requirement
    assert child2 is not None
    assert child2.structured_limits_state == requirement_limits.STATE_UNRESOLVED
    assert child2.structured_limits == (b_write,)

    # 重启后保持
    reloaded = RequirementLedger(ledger.conn).by_id(child2.requirement_id)
    assert reloaded is not None
    assert reloaded.structured_limits == (b_write,)
    assert reloaded.structured_limits_state == requirement_limits.STATE_UNRESOLVED


def test_unresolved_parent_known_plus_new_limit(ledger):
    b_write = limit(("write",), (obj("path", "D:/proj/B.py"),), "不要修改 B")
    parent = persist(
        ledger, state=requirement_limits.STATE_UNRESOLVED, limits=(b_write,)
    )
    read_a = limit(("read",), (obj("path", "D:/proj/A.txt"),), "不要读取 A")
    applied = amend(ledger, parent, message="另外不要读取 A。", limits=(read_a,))
    child = applied.requirement
    assert child is not None
    assert child.structured_limits_state == requirement_limits.STATE_UNRESOLVED
    values = {
        (item["operations"][0], item["objects"][0]["value"])
        for item in (child.structured_limits or ())
    }
    assert values == {("write", "D:/proj/B.py"), ("read", "D:/proj/A.txt")}


def test_unresolved_parent_known_supports_precise_removal(ledger):
    b_path = "D:/proj/B.py"
    write_b = limit(("write",), (obj("path", b_path),), "不要写 B")
    delete_b = limit(("delete",), (obj("path", b_path),), "不要删 B")
    parent = persist(
        ledger,
        state=requirement_limits.STATE_UNRESOLVED,
        limits=(write_b, delete_b),
    )
    applied = amend(
        ledger,
        parent,
        message="B 可以写了，仍然不要删除。",
        removed=(limit(("write",), (obj("path", b_path),)),),
    )
    child = applied.requirement
    assert child is not None
    # 父未恢复完整，子仍 unresolved；但 known 必须继承并精确移除 write
    assert child.structured_limits_state == requirement_limits.STATE_UNRESOLVED
    assert child.structured_limits == (delete_b,)
    assert "partial_parent_known" in str(child.structured_limits_source or "")


def test_direct_add_correction_inherits_known_limits(ledger):
    b_write = limit(("write",), (obj("path", "D:/proj/B.py"),), "不要修改 B")
    parent = persist(
        ledger, state=requirement_limits.STATE_UNRESOLVED, limits=(b_write,)
    )
    child = ledger.add_correction(parent.requirement_id, "再补充一句。")
    assert child.structured_limits_state == requirement_limits.STATE_UNRESOLVED
    assert child.structured_limits == (b_write,)


# ---------------------------------------------------------------------------
# 3) 精确部分解除：只移除指定对象/操作，不误删其它
# ---------------------------------------------------------------------------


def test_remove_partial_operation_and_object(ledger):
    b_path = "D:/proj/B.py"
    a_path = "D:/proj/A.py"
    parent = persist(
        ledger,
        state=requirement_limits.STATE_STRUCTURED,
        limits=(
            limit(("write", "delete"), (obj("path", b_path),), "B 的写/删限制"),
            limit(("write",), (obj("path", a_path),), "A 的写限制"),
        ),
    )
    # 只解除 B 的 write：不得删除 A 的 write，也不得删除 B 的 delete
    applied = amend(
        ledger,
        parent,
        message="B 的写入限制取消，其他不变。",
        removed=(limit(("write",), (obj("path", b_path),), "解除 B write"),),
    )
    child = applied.requirement
    assert child is not None
    assert child.structured_limits_state == requirement_limits.STATE_STRUCTURED
    # 展开成 (operation, object) 组合核对：A write、B delete 保留；B write 移除
    pairs = {
        (operation, object_item["value"])
        for item in (child.structured_limits or ())
        for operation in item["operations"]
        for object_item in item["objects"]
    }
    assert pairs == {("write", a_path), ("delete", b_path)}


def test_remove_partial_object_from_multi_object_entry(ledger):
    a_path = "D:/proj/A.py"
    b_path = "D:/proj/B.py"
    parent = persist(
        ledger,
        state=requirement_limits.STATE_STRUCTURED,
        limits=(limit(("write",), (obj("path", a_path), obj("path", b_path))),),
    )
    applied = amend(
        ledger,
        parent,
        message="B 可以写了，A 仍不要写。",
        removed=(limit(("write",), (obj("path", b_path),)),),
    )
    child = applied.requirement
    assert child is not None
    values = {
        (item["operations"][0], item["objects"][0]["value"])
        for item in (child.structured_limits or ())
    }
    assert values == {("write", a_path)}


def test_remove_part_of_all_operations_entry(ledger):
    b_path = "D:/proj/B.py"
    parent = persist(
        ledger,
        state=requirement_limits.STATE_STRUCTURED,
        limits=(limit((), (obj("path", b_path),), "B 上所有操作都禁止"),),
    )
    applied = amend(
        ledger,
        parent,
        message="B 可以写，但其他操作仍不允许。",
        removed=(limit(("write",), (obj("path", b_path),)),),
    )
    child = applied.requirement
    assert child is not None
    # 无法表达"除 write 外的所有操作"：保守保留原限制并标 unresolved，不猜测
    assert child.structured_limits_state == requirement_limits.STATE_UNRESOLVED
    assert child.structured_limits == (
        limit((), (obj("path", b_path),), "B 上所有操作都禁止"),
    )
    assert "removed_ambiguous" in str(child.structured_limits_source or "")


def test_ambiguous_removal_preserves_known_limits_and_marks_unresolved(ledger):
    b_write = limit(("write",), (obj("path", "D:/proj/B.py"),), "不要修改 B")
    parent = persist(
        ledger, state=requirement_limits.STATE_STRUCTURED, limits=(b_write,)
    )
    # removed 引用了父限制里不存在的对象：无法精确匹配，不得静默忽略
    applied = amend(
        ledger,
        parent,
        message="C 的限制取消。",
        removed=(limit(("write",), (obj("path", "D:/proj/C.py"),)),),
    )
    child = applied.requirement
    assert child is not None
    assert child.structured_limits_state == requirement_limits.STATE_UNRESOLVED
    assert child.structured_limits == (b_write,)
    assert "removed_ambiguous" in str(child.structured_limits_source or "")


def test_removed_limits_can_target_read_and_program(ledger):
    parent = persist(
        ledger,
        state=requirement_limits.STATE_STRUCTURED,
        limits=(
            limit(("read",), (obj("path", "D:/secret.txt"),), "不要读取"),
            limit(("execute",), (obj("program", "python"),), "不要运行 python"),
        ),
    )
    applied = amend(
        ledger,
        parent,
        message="python 可以运行了，读取限制保留。",
        removed=(limit(("execute",), (obj("program", "python"),)),),
    )
    child = applied.requirement
    assert child is not None
    assert child.structured_limits_state == requirement_limits.STATE_STRUCTURED
    assert child.structured_limits == (
        limit(("read",), (obj("path", "D:/secret.txt"),), "不要读取"),
    )


# ---------------------------------------------------------------------------
# 4) 恢复失败/空结果不得覆盖 known；new/withdraw/重启保持
# ---------------------------------------------------------------------------


def test_invalid_resolution_limits_preserve_parent_known(ledger):
    b_write = limit(("write",), (obj("path", "D:/proj/B.py"),), "不要修改 B")
    parent = persist(
        ledger, state=requirement_limits.STATE_UNRESOLVED, limits=(b_write,)
    )
    outcome = ResolutionOutcome(
        relation=RequirementRelation.AMEND,
        target_requirement_id=parent.requirement_id,
        target_version=int(parent.version),
        corrections=("假装输出非法限制",),
        limits=({"operations": ["keyword_allow"], "objects": []},),
        reason="非法限制测试",
        model="fake-r9h4",
        prompt_version="r9h4-test",
    )
    applied = ledger.apply_resolution(
        outcome, message_id="m_bad", turn_id="t_bad", message="非法限制"
    )
    child = applied.requirement
    assert child is not None
    assert child.structured_limits_state == requirement_limits.STATE_UNRESOLVED
    assert child.structured_limits == (b_write,)
    assert "invalid_limits" in str(child.structured_limits_source or "")


def test_empty_resolution_does_not_clear_parent_known(ledger):
    b_write = limit(("write",), (obj("path", "D:/proj/B.py"),), "不要修改 B")
    parent = persist(
        ledger, state=requirement_limits.STATE_STRUCTURED, limits=(b_write,)
    )
    applied = amend(ledger, parent, message="只补充一句，不改限制。")
    child = applied.requirement
    assert child is not None
    assert child.structured_limits_state == requirement_limits.STATE_STRUCTURED
    assert child.structured_limits == (b_write,)


def test_new_and_withdraw_preserve_structured_limits(ledger):
    read_limit = limit(("read",), (obj("path", "D:/secret.txt"),), "不要读取")
    applied = new_requirement(ledger, limits=(read_limit,), message="不要读取 secret。")
    new_req = applied.requirement
    assert new_req is not None
    assert new_req.structured_limits == (read_limit,)
    assert new_req.structured_limits_state == requirement_limits.STATE_STRUCTURED

    ledger.withdraw(new_req.requirement_id)
    withdrawn = RequirementLedger(ledger.conn).by_id(new_req.requirement_id)
    assert withdrawn is not None
    assert withdrawn.status == "withdrawn"
    assert withdrawn.structured_limits == (read_limit,)
    assert withdrawn.structured_limits_state == requirement_limits.STATE_STRUCTURED


def test_set_structured_limits_failure_does_not_clear_known(ledger):
    b_write = limit(("write",), (obj("path", "D:/proj/B.py"),), "不要修改 B")
    requirement = persist(
        ledger, state=requirement_limits.STATE_UNRESOLVED, limits=(b_write,)
    )
    # 恢复失败路径以 None 表示"保留已有 known"
    saved = ledger.set_structured_limits(
        requirement.requirement_id,
        None,
        state=requirement_limits.STATE_UNRESOLVED,
        source="recovery_failed:timeout",
    )
    assert saved.structured_limits == (b_write,)
    assert saved.structured_limits_state == requirement_limits.STATE_UNRESOLVED


def test_remove_all_operations_on_object_from_all_ops_entry(ledger):
    b_path = "D:/proj/B.py"
    parent = persist(
        ledger,
        state=requirement_limits.STATE_STRUCTURED,
        limits=(limit((), (obj("path", b_path),), "B 上所有操作都禁止"),),
    )
    # 解除"B 上所有操作"：可以精确表达为整条移除
    applied = amend(
        ledger,
        parent,
        message="B 的限制全部取消。",
        removed=(limit((), (obj("path", b_path),)),),
    )
    child = applied.requirement
    assert child is not None
    assert child.structured_limits_state == requirement_limits.STATE_STRUCTURED
    assert child.structured_limits == ()


def test_remove_all_operations_on_object_keeps_other_objects(ledger):
    a_path = "D:/proj/A.py"
    b_path = "D:/proj/B.py"
    parent = persist(
        ledger,
        state=requirement_limits.STATE_STRUCTURED,
        limits=(limit(("write", "delete"), (obj("path", a_path), obj("path", b_path))),),
    )
    applied = amend(
        ledger,
        parent,
        message="B 上的限制全部取消，A 不变。",
        removed=(limit((), (obj("path", b_path),)),),
    )
    child = applied.requirement
    assert child is not None
    assert child.structured_limits_state == requirement_limits.STATE_STRUCTURED
    pairs = {
        (operation, object_item["value"])
        for item in (child.structured_limits or ())
        for operation in item["operations"]
        for object_item in item["objects"]
    }
    assert pairs == {
        ("write", a_path),
        ("delete", a_path),
    }


# ---------------------------------------------------------------------------
# 5) 提示词/输出契约：规范操作与 kind 一致，不重新引入宽白名单
# ---------------------------------------------------------------------------


def test_prompts_use_canonical_limit_contract():
    combined = "\n".join(
        (
            REQUIREMENT_RESOLUTION_SYSTEM,
            LIMIT_EXTRACTION_SYSTEM,
            LIMIT_EXTRACTION_CHAIN_SYSTEM,
        )
    )
    # 规范操作与对象在提示词里可出现；宽白名单旧词不应作为允许项出现
    assert "read" in combined and "write" in combined
    assert "path_prefix" in combined and "program" in combined
    assert "material" not in combined
    assert "record" not in combined
    assert "shell" not in combined
    assert "internal_data" not in combined
    assert "tool_register" not in combined
    assert "subagent" not in combined


def test_resolution_rejects_unsupported_operation_and_object_kind():
    raw = json.dumps(
        {
            "relation": "new",
            "corrections": ["x"],
            "limits": [{"operations": ["shell"], "objects": []}],
            "reason": "x",
        }
    )
    parsed = parse_resolution_outcome(raw)
    with pytest.raises(RequirementResolutionUnavailable):
        validate_resolution(parsed, candidates=[])

    raw2 = json.dumps(
        {
            "relation": "new",
            "corrections": ["x"],
            "limits": [{"operations": ["write"], "objects": [{"kind": "material", "value": "x"}]}],
            "reason": "x",
        }
    )
    with pytest.raises(RequirementResolutionUnavailable):
        validate_resolution(parse_resolution_outcome(raw2), candidates=[])


def test_mixed_valid_and_invalid_entries_keep_reliable_limits(ledger):
    """个别条目非法时保留其余可靠限制，标 unresolved，不清空父 known（契约 §2）。"""
    b_write = limit(("write",), (obj("path", "D:/proj/B.py"),), "不要修改 B")
    read_a = limit(("read",), (obj("path", "D:/proj/A.txt"),), "不要读取 A")
    parent = persist(
        ledger, state=requirement_limits.STATE_STRUCTURED, limits=(b_write,)
    )
    outcome = ResolutionOutcome(
        relation=RequirementRelation.AMEND,
        target_requirement_id=parent.requirement_id,
        target_version=int(parent.version),
        corrections=("新增读取限制；另一条是非法输出",),
        limits=(read_a, {"operations": ["shell"], "objects": []}),
        reason="混合限制测试",
        model="fake-r9h4",
        prompt_version="r9h4-test",
    )
    applied = ledger.apply_resolution(
        outcome, message_id="m_mix", turn_id="t_mix", message="混合限制"
    )
    child = applied.requirement
    assert child is not None
    assert child.structured_limits_state == requirement_limits.STATE_UNRESOLVED
    values = {
        (item["operations"][0], item["objects"][0]["value"])
        for item in (child.structured_limits or ())
    }
    # 父 known B 保留；合法新增 read A 也保留；非法 shell 条目不落库
    assert values == {("write", "D:/proj/B.py"), ("read", "D:/proj/A.txt")}
    assert "invalid_limits" in str(child.structured_limits_source or "")


def test_new_with_mixed_entries_keeps_valid_and_marks_unresolved(ledger):
    read_a = limit(("read",), (obj("path", "D:/proj/A.txt"),), "不要读取 A")
    outcome = ResolutionOutcome(
        relation=RequirementRelation.NEW,
        target_requirement_id=None,
        target_version=None,
        corrections=(),
        limits=(read_a, {"operations": ["shell"], "objects": []}),
        reason="新要求混合限制",
        model="fake-r9h4",
        prompt_version="r9h4-test",
    )
    applied = ledger.apply_resolution(
        outcome, message_id="m_new_mix", turn_id="t_new_mix", message="混合限制新要求"
    )
    requirement = applied.requirement
    assert requirement is not None
    assert requirement.structured_limits_state == requirement_limits.STATE_UNRESOLVED
    assert requirement.structured_limits == (read_a,)
    reloaded = RequirementLedger(ledger.conn).by_id(requirement.requirement_id)
    assert reloaded is not None
    assert reloaded.structured_limits == (read_a,)
