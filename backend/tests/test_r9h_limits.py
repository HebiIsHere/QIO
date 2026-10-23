"""R9-H C：结构化限制持久化 / 继承 / 恢复（契约 §6）。

全部使用**真实** sqlite 文件数据库 + 真实迁移（不是 dict 替身）：
* 新要求 state=structured、[] = 明确无限制；NULL/legacy = 历史未知（不是无限制）；
* 关闭连接后新建 ledger/新连接恢复；amend 继承/合并/明确解除；withdraw 后失效；
* 父 legacy/unresolved 时子保持未解决，不声称完整；
* 引擎按持久化限制拒绝（限制只约束不授权）；无关要求限制不串入；
* 迁移 59/补偿 60 的列/版本/REQUIRED_OBJECTS 真实核对。
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from agent.security import internal_data, requirement_limits
from agent.security.effects import Effect, EffectKind, Target
from agent.security.engine import AuthorizationEngine
from agent.security.requirements import (
    STATUS_SUPERSEDED,
    STATUS_WITHDRAWN,
    RequirementLedger,
    UserRequirement,
)
from agent.security.resolution import RequirementRelation
from agent.storage.db import connect
from agent.storage.migrate import REQUIRED_OBJECTS, apply_migrations
from agent.storage.schema import COMPENSATION_VERSION, MIGRATIONS, SCHEMA_VERSION


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "r9h_limits.db"


@pytest.fixture()
def conn(db_path: Path):
    connection = connect(db_path)
    apply_migrations(connection)
    yield connection
    connection.close()


def _limit(path: str, *, operations=("write",), reason="只允许这项") -> dict:
    return {
        "operations": list(operations),
        "objects": [{"kind": "path", "value": str(path)}],
        "reason": reason,
    }


def _resolution(relation: str, *, target=None, version=None, limits=(), removed=(), corrections=("补充",)):
    return types.SimpleNamespace(
        relation=relation,
        target_requirement_id=target,
        target_version=version,
        limits=tuple(dict(item) for item in limits),
        removed_limits=tuple(dict(item) for item in removed),
        corrections=tuple(corrections),
        reason="fake",
        model="fake-model",
        prompt_version="fake-v1",
    )


# ---------------------------------------------------------------------------
# 持久化 / 重启恢复
# ---------------------------------------------------------------------------


def test_new_requirement_is_structured_and_limit_roundtrip(conn, db_path):
    ledger = RequirementLedger(conn)
    requirement = ledger.record_user_message(text="整理报告", message_id="m1", turn_id="t1")
    assert requirement.structured_limits_state == requirement_limits.STATE_STRUCTURED
    assert requirement.structured_limits == ()
    assert requirement_limits.limits_from_requirement(requirement) == ()

    entry = _limit("/work/report.md")
    updated = ledger.set_structured_limits(
        requirement.requirement_id, [entry], source="test:structured"
    )
    assert updated.structured_limits_state == requirement_limits.STATE_STRUCTURED
    assert updated.structured_limits == (entry,)
    raw = conn.execute(
        "SELECT structured_limits FROM user_requirements WHERE id = ?",
        (requirement.requirement_id,),
    ).fetchone()[0]
    assert json.loads(raw) == [entry]

    # 同一 ledger / 新 ledger（同一连接）/ 关闭连接后重开（模拟重启）都能恢复
    assert RequirementLedger(conn).structured_limits_for(requirement.requirement_id) == (entry,)
    conn.close()
    reopened = connect(db_path)
    try:
        restarted = RequirementLedger(reopened)
        reloaded = restarted.by_id(requirement.requirement_id)
        assert reloaded is not None
        assert reloaded.structured_limits == (entry,)
        assert restarted.structured_limits_for(requirement.requirement_id) == (entry,)
    finally:
        reopened.close()


def test_legacy_null_is_unknown_not_unlimited(conn):
    ledger = RequirementLedger(conn)
    requirement = ledger.record_user_message(text="历史要求", message_id="m1", turn_id="t1")
    # 模拟 R9-H 之前的历史行：structured_limits=NULL、state=legacy
    conn.execute(
        "UPDATE user_requirements SET structured_limits = NULL,"
        " structured_limits_state = 'legacy', structured_limits_source = NULL"
        " WHERE id = ?",
        (requirement.requirement_id,),
    )
    conn.commit()
    legacy = ledger.by_id(requirement.requirement_id)
    assert legacy is not None
    assert legacy.structured_limits is None
    assert legacy.structured_limits_state == requirement_limits.STATE_LEGACY
    assert requirement_limits.limits_from_requirement(legacy) is None
    assert requirement_limits.deserialize_limits(None) is None
    assert [item.requirement_id for item in ledger.legacy_requirements()] == [
        requirement.requirement_id
    ]

    # 恢复入口：显式设置后才成为 structured（[] 表示确认无限制）
    recovered = ledger.set_structured_limits(
        requirement.requirement_id, [], source="recovery:model-v2"
    )
    assert recovered.structured_limits_state == requirement_limits.STATE_STRUCTURED
    assert requirement_limits.limits_from_requirement(recovered) == ()
    assert ledger.legacy_requirements() == []


def test_unresolved_keeps_known_entries_but_not_complete(conn):
    ledger = RequirementLedger(conn)
    requirement = ledger.record_user_message(text="无法可靠恢复", message_id="m1", turn_id="t1")
    entry = _limit("/work/report.md")
    ledger.set_structured_limits(
        requirement.requirement_id,
        [entry],
        source="recovery:model-timeout",
        state=requirement_limits.STATE_UNRESOLVED,
    )
    stored = ledger.by_id(requirement.requirement_id)
    assert stored is not None
    assert stored.structured_limits_state == requirement_limits.STATE_UNRESOLVED
    assert stored.structured_limits == (entry,)  # 已知新增保留
    assert requirement_limits.limits_from_requirement(stored) is None  # 不得声称完整
    assert [item.requirement_id for item in ledger.legacy_requirements()] == [
        requirement.requirement_id
    ]


# ---------------------------------------------------------------------------
# amend：继承 / 追加 / 明确解除 / 父未解决
# ---------------------------------------------------------------------------


def test_add_correction_inherits_and_text_only_does_not_clear(conn):
    ledger = RequirementLedger(conn)
    parent = ledger.record_user_message(text="整理报告", message_id="m1", turn_id="t1")
    entry = _limit("/work/report.md", operations=("write", "delete"))
    ledger.set_structured_limits(parent.requirement_id, [entry], source="test")
    child = ledger.add_correction(parent.requirement_id, "只补充一句：格式用 Markdown")
    assert child.structured_limits_state == requirement_limits.STATE_STRUCTURED
    assert child.structured_limits == (entry,), "只补充一句话不得清空父限制"
    assert ledger.by_id(parent.requirement_id).status == STATUS_SUPERSEDED
    assert ledger.binding_for(parent.requirement_id) is None


def test_amend_merge_add_and_explicit_remove(conn):
    ledger = RequirementLedger(conn)
    parent = ledger.record_user_message(text="整理报告", message_id="m1", turn_id="t1")
    entry_a = _limit("/work/report.md")
    ledger.set_structured_limits(parent.requirement_id, [entry_a], source="test")

    entry_b = _limit("/work/other.md", operations=("write", "delete"), reason="新增限制")
    amended = ledger.apply_resolution(
        _resolution(
            RequirementRelation.AMEND.value,
            target=parent.requirement_id,
            version=parent.version,
            limits=[entry_b],
        ),
        message_id="m2",
        turn_id="t2",
        message="另外不要改 /work/other.md 的删除？",
    )
    assert amended.relation == "amend"
    child = amended.requirement
    assert child is not None
    assert amended.limits_state == requirement_limits.STATE_STRUCTURED
    assert child.structured_limits == (entry_a, entry_b)
    assert requirement_limits.limits_from_requirement(child) == (entry_a, entry_b)

    # 明确解除：removed 条目从父集合移除（按 operations+objects 匹配）
    removed = ledger.apply_resolution(
        _resolution(
            RequirementRelation.AMEND.value,
            target=child.requirement_id,
            version=child.version,
            limits=[],
            removed=[entry_a],
        ),
        message_id="m3",
        turn_id="t3",
        message="可以改 report.md 了，但 other.md 仍不行",
    )
    final = removed.requirement
    assert final is not None
    assert final.structured_limits == (entry_b,)


def test_amend_from_legacy_parent_keeps_unresolved_state(conn):
    ledger = RequirementLedger(conn)
    parent = ledger.record_user_message(text="历史要求", message_id="m1", turn_id="t1")
    conn.execute(
        "UPDATE user_requirements SET structured_limits = NULL,"
        " structured_limits_state = 'legacy', structured_limits_source = NULL"
        " WHERE id = ?",
        (parent.requirement_id,),
    )
    conn.commit()
    known = _limit("/work/new.md")
    amended = ledger.apply_resolution(
        _resolution(
            RequirementRelation.AMEND.value,
            target=parent.requirement_id,
            version=parent.version,
            limits=[known],
        ),
        message_id="m2",
        turn_id="t2",
        message="补充：别动 new.md",
    )
    child = amended.requirement
    assert child is not None
    assert child.structured_limits_state == requirement_limits.STATE_LEGACY
    assert requirement_limits.limits_from_requirement(child) is None, "父未解决不得声称完整"
    assert child.structured_limits == (known,), "已知新增可保存，但状态仍未解决"


def test_withdraw_invalidates_binding_and_old_limits(conn):
    ledger = RequirementLedger(conn)
    requirement = ledger.record_user_message(text="整理报告", message_id="m1", turn_id="t1")
    ledger.set_structured_limits(requirement.requirement_id, [_limit("/work/a.md")], source="test")
    applied = ledger.apply_resolution(
        _resolution(
            RequirementRelation.WITHDRAW.value,
            target=requirement.requirement_id,
            version=requirement.version,
        ),
        message_id="m2",
        turn_id="t2",
        message="算了，撤回刚才的要求",
    )
    assert applied.requirement is not None
    assert applied.requirement.status == STATUS_WITHDRAWN
    assert ledger.binding_for(requirement.requirement_id) is None
    assert ledger.validate_binding(requirement.as_binding()) is False


# ---------------------------------------------------------------------------
# 引擎按持久化限制拒绝；无关要求不串入；限制不授权
# ---------------------------------------------------------------------------


def _engine(conn) -> AuthorizationEngine:
    return AuthorizationEngine(conn=conn)


def test_engine_uses_persisted_limits_after_reload(conn):
    ledger = RequirementLedger(conn)
    requirement = ledger.record_user_message(text="只改报告", message_id="m1", turn_id="t1")
    target = Path("/work/report.md")
    ledger.set_structured_limits(
        requirement.requirement_id,
        [_limit(str(target), operations=("write",))],
        source="test",
    )
    reloaded = ledger.by_id(requirement.requirement_id)
    engine = _engine(conn)
    conflict = engine._limit_conflict(
        Effect(kind=EffectKind.WRITE, targets=(Target("path", str(target)),)), reloaded
    )
    assert conflict, "持久化限制必须在重载后仍然拒绝被限制的对象"
    other = engine._limit_conflict(
        Effect(kind=EffectKind.WRITE, targets=(Target("path", "/work/other.md"),)), reloaded
    )
    assert other is None


def test_unrelated_requirements_limits_do_not_leak(conn):
    ledger = RequirementLedger(conn)
    first = ledger.record_user_message(text="要求一", message_id="m1", turn_id="t1")
    second = ledger.record_user_message(text="要求二", message_id="m2", turn_id="t2")
    ledger.set_structured_limits(
        first.requirement_id, [_limit("/work/one.md")], source="test"
    )
    ledger.set_structured_limits(
        second.requirement_id, [_limit("/work/two.md")], source="test"
    )
    reloaded_first = ledger.by_id(first.requirement_id)
    reloaded_second = ledger.by_id(second.requirement_id)
    engine = _engine(conn)
    one = Effect(kind=EffectKind.WRITE, targets=(Target("path", "/work/one.md"),))
    two = Effect(kind=EffectKind.WRITE, targets=(Target("path", "/work/two.md"),))
    assert engine._limit_conflict(one, reloaded_first)
    assert engine._limit_conflict(one, reloaded_second) is None
    assert engine._limit_conflict(two, reloaded_second)
    assert engine._limit_conflict(two, reloaded_first) is None


def test_legacy_and_unresolved_are_not_treated_as_unlimited(conn):
    ledger = RequirementLedger(conn)
    requirement = ledger.record_user_message(text="历史", message_id="m1", turn_id="t1")
    conn.execute(
        "UPDATE user_requirements SET structured_limits = NULL,"
        " structured_limits_state = 'unresolved', structured_limits_source = 'timeout'"
        " WHERE id = ?",
        (requirement.requirement_id,),
    )
    conn.commit()
    reloaded = ledger.by_id(requirement.requirement_id)
    engine = _engine(conn)
    assert engine._limits_unresolved(reloaded) is True
    # 限制只约束不授权：模块里没有 authorize/grant 入口
    for forbidden in ("authorize", "grant", "allow"):
        assert not hasattr(requirement_limits, forbidden)


# ---------------------------------------------------------------------------
# requirement_limits 纯函数
# ---------------------------------------------------------------------------


def test_normalize_serialize_deserialize_roundtrip():
    limits = [_limit("/work/a.md", operations=("write", "delete"))]
    normalized = requirement_limits.normalize_limits(limits)
    serialized = requirement_limits.serialize_limits(normalized)
    assert requirement_limits.deserialize_limits(serialized) == normalized
    assert requirement_limits.deserialize_limits("[]") == ()
    assert requirement_limits.deserialize_limits(None) is None
    assert requirement_limits.deserialize_limits("not-json") is None
    assert requirement_limits.deserialize_limits('{"operations": []}') is None
    with pytest.raises(ValueError):
        requirement_limits.normalize_limits(None)
    with pytest.raises(ValueError):
        requirement_limits.normalize_limits([{"operations": ["unknown-op"], "objects": []}])
    with pytest.raises(ValueError):
        requirement_limits.normalize_limits(
            [{"operations": [], "objects": [{"kind": "regex", "value": "x"}]}]
        )
    # 去重
    assert len(requirement_limits.normalize_limits(limits + limits)) == 1


def test_merge_limits_add_remove_and_dedupe():
    parent = requirement_limits.normalize_limits([_limit("/a.md")])
    added = requirement_limits.normalize_limits([_limit("/b.md"), _limit("/a.md")])
    removed = requirement_limits.normalize_limits([_limit("/a.md")])
    merged = requirement_limits.merge_limits(parent, added=added, removed=removed)
    assert [item["objects"][0]["value"] for item in merged] == ["/b.md"]
    assert requirement_limits.merge_limits(parent, added=[], removed=[]) == parent
    with pytest.raises(ValueError):
        requirement_limits.merge_limits(None)  # 父未知不能声称完整


# ---------------------------------------------------------------------------
# 迁移 59 / 补偿 60
# ---------------------------------------------------------------------------


def test_migration_59_and_compensation_60_real_db(conn):
    # R9-H2 把版本推到 62；本用例核对迁移 59/60 的对象仍在最终补偿链中。
    assert SCHEMA_VERSION == COMPENSATION_VERSION >= 60
    targets = {target for target, _ in MIGRATIONS}
    assert 59 in targets and 60 in targets
    for column in (
        "user_requirements.structured_limits",
        "user_requirements.structured_limits_state",
        "user_requirements.structured_limits_source",
    ):
        assert column in REQUIRED_OBJECTS["columns"], column
    columns = {row[1] for row in conn.execute("PRAGMA table_info(user_requirements)").fetchall()}
    assert {"structured_limits", "structured_limits_state", "structured_limits_source"} <= columns
    statements = [
        str(stmt) for target, stmts in MIGRATIONS if target == COMPENSATION_VERSION for stmt in stmts
    ]
    blob = "\n".join(" ".join(item.split()) for item in statements)
    for column in ("structured_limits", "structured_limits_state", "structured_limits_source"):
        assert ("ADD COLUMN " + column) in blob, column
    # 迁移后历史行默认 legacy/NULL（不是"明确无限制"）
    conn.execute(
        "INSERT INTO user_requirements"
        " (id, text, provenance, parent_id, status, limits, created_at, updated_at)"
        " VALUES ('req_hist', '历史要求', '{}', NULL, 'active', '[]', '2026-01-01', '2026-01-01')"
    )
    conn.commit()
    row = conn.execute(
        "SELECT structured_limits, structured_limits_state FROM user_requirements"
        " WHERE id = 'req_hist'"
    ).fetchone()
    assert row[0] is None
    assert row[1] == "legacy"
