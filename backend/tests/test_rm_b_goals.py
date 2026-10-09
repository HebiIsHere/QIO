"""B 组 M03：目标多值（upsert/稳定身份）、重复提交幂等、改一个不撤销另一个。

直接调 `OnboardingService.submit`（真实落库路径），不联网、不用真实模型。
"""

from __future__ import annotations

import sqlite3

from agent.knowledge.lifecycle import KnowledgeService
from agent.services.onboarding import OnboardingService, goal_field_key
from agent.tools.knowledge_correction import CorrectKnowledgeTool


def _payload(**overrides) -> dict:
    base = {
        "name": "祠莎",
        "background": "学生",
        "current_focus": "开发 QIO",
        "interests": ["agent 记忆问题"],
        "familiarity": "刚入门",
        "limits": {"dont_do": "不要自动动用外部工具", "how_to_talk": "先讲逻辑再给代码"},
        "preferences": [{"kind": "verbosity", "value": "简洁", "scope": {"type": "global"}}],
        "goals": [],
        "inferred": [],
    }
    base.update(overrides)
    return base


def _service(conn: sqlite3.Connection) -> OnboardingService:
    return OnboardingService(conn, "0.1.14")


def _active_goals(conn: sqlite3.Connection):
    ks = KnowledgeService(conn)
    return [i for i in ks.list_items(category="goal") if i.state.value == "active"]


def _all_goals(conn: sqlite3.Connection):
    return KnowledgeService(conn).list_items(category="goal")


def _goal_entries(result: dict) -> list[dict]:
    return [w for w in result["written"] if w.get("field", "").startswith("goal:")]


# -- M03：多目标 --------------------------------------------------------------


def test_two_goals_are_both_active(db_conn):
    result = _service(db_conn).submit(_payload(goals=["把 QIO 的记忆问题做完", "每周读一篇论文"]))

    goals = _active_goals(db_conn)
    contents = {g.content for g in goals}
    assert contents == {"目标：把 QIO 的记忆问题做完", "目标：每周读一篇论文"}
    assert len(goals) == 2

    entries = _goal_entries(result)
    assert len(entries) == 2
    assert all(e["created"] is True and e["state"] == "active" for e in entries)
    # 清单里的 id 与实际生效结果一致
    assert {e["id"] for e in entries} == {g.id for g in goals}


def test_repeat_submit_does_not_add_goals_or_topics(db_conn):
    service = _service(db_conn)
    first = service.submit(_payload(goals=["目标甲", "目标乙"]))
    first_ids = {e["id"] for e in _goal_entries(first)}
    topics_before = db_conn.execute("SELECT COUNT(*) AS n FROM nodes WHERE type='topic'").fetchone()["n"]

    second = service.submit(_payload(goals=["目标甲", "目标乙"]))

    assert len(_active_goals(db_conn)) == 2
    assert len(_all_goals(db_conn)) == 2
    assert db_conn.execute("SELECT COUNT(*) AS n FROM nodes WHERE type='topic'").fetchone()["n"] == topics_before
    entries = _goal_entries(second)
    assert {e["id"] for e in entries} == first_ids
    assert all(e["created"] is False and e["reused"] is True for e in entries)


def test_one_goal_change_does_not_revoke_another(db_conn):
    service = _service(db_conn)
    service.submit(_payload(goals=["目标甲", "目标乙"]))

    # 追加一个新目标：旧的两个都不受影响
    service.submit(_payload(goals=["目标甲", "目标乙", "目标丙"]))
    contents = {g.content for g in _active_goals(db_conn)}
    assert contents == {"目标：目标甲", "目标：目标乙", "目标：目标丙"}
    assert len(_all_goals(db_conn)) == 3, "多值字段不得因为新目标而取代旧目标"

    # 纠正其中一个目标（对话内纠正工具）：只动这一条链
    target = next(g for g in _active_goals(db_conn) if g.content == "目标：目标甲")
    from agent.knowledge.lifecycle import revise_atomic

    new_version = revise_atomic(db_conn, target.id, target.version, {"content": "目标：目标甲（已调整）"})
    active = {g.content for g in _active_goals(db_conn)}
    assert active == {"目标：目标甲（已调整）", "目标：目标乙", "目标：目标丙"}
    assert KnowledgeService(db_conn).get(target.id).state.value == "revoked"
    assert new_version.state.value == "active"


async def test_correction_tool_touches_only_the_named_goal(db_conn):
    service = _service(db_conn)
    result = service.submit(_payload(goals=["目标甲", "目标乙"]))
    target = next(e for e in _goal_entries(result) if e["content"] == "目标：目标甲")

    tool = CorrectKnowledgeTool(
        db_conn,
        snapshot_provider=lambda: [{"item_id": target["id"], "content": target["content"]}],
    )
    outcome = await tool.run(content=target["content"], new_content="目标：目标甲改")
    assert outcome.ok

    contents = {g.content for g in _active_goals(db_conn)}
    assert contents == {"目标：目标甲改", "目标：目标乙"}


def test_goal_field_key_is_stable_and_content_scoped(db_conn):
    goal = "把 QIO 的记忆问题做完"
    assert goal_field_key(goal) == goal_field_key(f"  {goal}  ")
    assert goal_field_key(goal) != goal_field_key("另一个目标")

    result = _service(db_conn).submit(_payload(goals=[goal]))
    entry = _goal_entries(result)[0]
    item = KnowledgeService(db_conn).get(entry["id"])
    assert item is not None
    assert item.provenance.get("field_key") == goal_field_key(goal)


# -- M03：单值字段仍是替换语义 + 结果清单准确 --------------------------------


def test_single_value_field_replacement_is_reported_accurately(db_conn):
    service = _service(db_conn)
    first = service.submit(_payload())
    old_name_id = next(w["id"] for w in first["written"] if w["content"] == "称呼：祠莎")

    second = service.submit(_payload(name="柯莎"))
    name_entry = next(w for w in second["written"] if w.get("field") == "称呼：")

    assert name_entry["created"] is True
    assert name_entry["superseded"] == old_name_id
    assert KnowledgeService(db_conn).get(old_name_id).state.value == "revoked"
    active_names = [
        i
        for i in KnowledgeService(db_conn).list_items(category="user_profile")
        if i.state.value == "active" and i.content.startswith("称呼：")
    ]
    assert len(active_names) == 1 and active_names[0].content == "称呼：柯莎"


def test_repeat_submit_marks_every_entry_as_reused(db_conn):
    service = _service(db_conn)
    service.submit(_payload(goals=["目标甲"]))
    second = service.submit(_payload(goals=["目标甲"]))
    assert second["written"], "结果清单不能是空的"
    assert all(w["created"] is False for w in second["written"])
    assert all(w["reused"] is True for w in second["written"])
    # 清单里报的 id 状态与实际库一致
    ks = KnowledgeService(db_conn)
    for entry in second["written"]:
        stored = ks.get(entry["id"])
        assert stored is not None
        assert stored.state.value == entry["state"]


def test_ended_focus_is_not_reactivated_by_resubmit(db_conn):
    service = _service(db_conn)
    service.submit(_payload(current_focus_ended=True))
    focus = next(
        i
        for i in KnowledgeService(db_conn).list_items(category="user_profile")
        if i.state.value == "active" and i.content.startswith("最近在做：")
    )
    assert focus.provenance.get("ended_at")

    service.submit(_payload(current_focus_ended=False))

    rows = db_conn.execute(
        "SELECT id, provenance FROM knowledge WHERE content = ? AND state = 'active'",
        (focus.content,),
    ).fetchall()
    assert len(rows) == 1, "重复提交不得复制目标/字段"
    assert "ended_at" in (rows[0]["provenance"] or ""), "用户主动结束的状态不得被无意重新启用"


def test_goals_keep_tenant_scope_on_user_node(db_conn):
    from agent.graph.nodes import NodeService

    user_node_id = NodeService(db_conn).get_or_create_user_root().id
    result = _service(db_conn).submit(_payload(goals=["范围检查目标"]))
    entry = _goal_entries(result)[0]
    item = KnowledgeService(db_conn).get(entry["id"])
    assert item is not None
    assert item.node_ids == [user_node_id]
    assert item.scope_global is True
