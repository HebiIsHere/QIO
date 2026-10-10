# -*- coding: utf-8 -*-
"""M04：实体卡的「用户明确整张修订」与「模型部分候选」必须区分开。

验收覆盖（逐条）：
* 已有两个属性，新候选只提一个 → 另一个保留（未提及 ≠ 删除）；
* 人工纠正不被迟到的自动结果覆盖 —— **管理 API 与纠正工具两入口**都验；
* 别名 / 摘要 / 类型 / 属性用同一套冲突规则；
* 迟到结果在**提交时**再次核对修订版本（不只在发起请求前）；
* 用户主动删除属性 / 删除整张卡不被后续普通提炼反转；
* 历史行（没有来源元数据）保守保留已有内容。
"""
from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from agent.entities.cards import (
    SOURCE_AUTO,
    SOURCE_USER,
    EntityAttribute,
    EntityCardCandidate,
    EntityCardService,
)
from agent.tools.entity_tools import CorrectEntityTool


def _ensure_card_meta_columns(connection: sqlite3.Connection) -> None:
    """A 的迁移会补这两列；本组测试自带幂等补列，保证验收可独立跑。"""
    cols = {row["name"] for row in connection.execute("PRAGMA table_info(entity_cards)")}
    if "revision" not in cols:
        connection.execute(
            "ALTER TABLE entity_cards ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
        )
    if "field_meta" not in cols:
        connection.execute(
            "ALTER TABLE entity_cards ADD COLUMN field_meta TEXT NOT NULL DEFAULT '{}'"
        )


def _candidate(name: str = "我家的鹅", **kwargs) -> EntityCardCandidate:
    return EntityCardCandidate(name=name, **kwargs)


def _attrs(card) -> dict[str, str]:
    return {str(a.get("key")): str(a.get("value")) for a in card.attributes}


def _seed(db_conn: sqlite3.Connection, *, summary: str = "原摘要", kind: str = "动物"):
    _ensure_card_meta_columns(db_conn)
    return EntityCardService(db_conn).upsert(
        _candidate(
            aliases=["鹅"],
            summary=summary,
            kind=kind,
            attributes=[
                EntityAttribute(key="健康状况", value="红肿"),
                EntityAttribute(key="年龄", value="2 岁"),
            ],
        )
    )


def test_auto_candidate_merges_attributes_and_keeps_unmentioned(db_conn):
    """关键验收：只提一个属性，另一个必须保留。"""
    card = _seed(db_conn)
    svc = EntityCardService(db_conn)

    updated = svc.upsert(
        _candidate(attributes=[EntityAttribute(key="健康状况", value="已康复")]),
        source=SOURCE_AUTO,
    )

    values = _attrs(updated)
    assert values["健康状况"] == "已康复", "候选提到的属性要更新"
    assert values["年龄"] == "2 岁", "未提及的属性不得被删除（旧行为的缺陷）"
    assert len(updated.attributes) == 2
    assert updated.revision == card.revision + 1


def test_auto_candidate_unions_aliases_and_updates_auto_summary(db_conn):
    """别名取并集、摘要/类型在来源是 auto 时按新值更新（同一套规则）。"""
    _seed(db_conn)
    svc = EntityCardService(db_conn)

    updated = svc.upsert(
        _candidate(
            aliases=["大鹅"],
            summary="新摘要",
            kind="家禽",
            attributes=[EntityAttribute(key="健康状况", value="已康复")],
        ),
        source=SOURCE_AUTO,
    )

    assert set(updated.aliases) == {"鹅", "大鹅"}, "自动提炼只增不减"
    assert updated.summary == "新摘要"
    assert updated.kind == "家禽"


def test_correction_tool_value_survives_a_late_auto_result(db_conn):
    """入口一：纠正工具（correct_entity）改过的值，迟到的自动结果不得覆盖。"""
    import asyncio

    card = _seed(db_conn)
    stale_revision = card.revision
    tool = CorrectEntityTool(db_conn)

    result = asyncio.run(
        tool.run(entity="我家的鹅", attribute_key="健康状况", attribute_value="已康复")
    )
    assert result.ok

    svc = EntityCardService(db_conn)
    late = svc.upsert(
        _candidate(attributes=[EntityAttribute(key="健康状况", value="红肿")]),
        source=SOURCE_AUTO,
        expected_revision=stale_revision,
    )

    assert _attrs(late)["健康状况"] == "已康复", "人工纠正优先于迟到的自动结果"
    pending = svc.pending_candidates(card.id)
    assert any(p["field"] == "attributes.健康状况" for p in pending), pending
    assert any(p["value"] == "红肿" for p in pending), "被挡下的自动值要留成可管理候选"


def test_management_api_value_survives_a_late_auto_result(db_conn, settings):
    """入口二：管理 API（POST /api/entities/{id}/revise）改过的值同样优先。"""
    from agent.api.server import create_app

    card = _seed(db_conn)
    stale_revision = card.revision
    app = create_app(settings, db_conn)
    with TestClient(app) as client:
        resp = client.post(
            f"/api/entities/{card.id}/revise",
            json={"summary": "用户手改的摘要", "kind": "用户定的类型"},
        )
        assert resp.status_code == 200
        assert resp.json()["entity"]["summary"] == "用户手改的摘要"

    svc = EntityCardService(db_conn)
    late = svc.upsert(
        _candidate(summary="模型迟到的摘要", kind="模型猜的类型"),
        source=SOURCE_AUTO,
        expected_revision=stale_revision,
    )

    assert late.summary == "用户手改的摘要"
    assert late.kind == "用户定的类型"
    pending_fields = {p["field"] for p in svc.pending_candidates(card.id)}
    assert {"summary", "kind"} <= pending_fields, pending_fields


def test_alias_summary_kind_use_the_same_conflict_rule(db_conn):
    """四个字段同一套规则：用户明确设过 → 自动只记候选，不替换。"""
    card = _seed(db_conn)
    svc = EntityCardService(db_conn)
    svc.revise(
        card.id,
        aliases=["用户起的别名"],
        summary="用户写的摘要",
        kind="用户定的类型",
    )

    late = svc.upsert(
        _candidate(
            aliases=["模型别名"],
            summary="模型摘要",
            kind="模型类型",
            attributes=[EntityAttribute(key="新属性", value="值")],
        ),
        source=SOURCE_AUTO,
        expected_revision=card.revision,
    )

    assert late.aliases == ["用户起的别名"]
    assert late.summary == "用户写的摘要"
    assert late.kind == "用户定的类型"
    # 「补空」仍然允许：全新的属性键可以落库
    assert _attrs(late)["新属性"] == "值"
    pending_fields = {p["field"] for p in svc.pending_candidates(card.id)}
    assert {"aliases", "summary", "kind"} <= pending_fields, pending_fields


def test_late_auto_result_rechecks_revision_at_submit_time(db_conn):
    """迟到结果在提交时再核对版本：卡在请求期间被改过 → 只能补空，不能改动已有值。"""
    card = _seed(db_conn)
    svc = EntityCardService(db_conn)
    snapshot = svc.revision_snapshot()["我家的鹅"]  # 「发起模型调用前」的版本

    # 期间另一条自动结果先提交：卡片版本推进
    svc.upsert(
        _candidate(attributes=[EntityAttribute(key="健康状况", value="好转")]),
        source=SOURCE_AUTO,
    )

    late = svc.apply_auto_candidate(
        _candidate(
            attributes=[
                EntityAttribute(key="健康状况", value="痊愈"),
                EntityAttribute(key="爱好", value="游泳"),
            ]
        ),
        expected_revision=snapshot,
    )

    values = _attrs(late)
    assert values["健康状况"] == "好转", "提交时版本不符 → 不得覆盖已有值"
    assert values["爱好"] == "游泳", "全新的属性仍然可以补上"
    pending = svc.pending_candidates(card.id)
    assert any(p["field"] == "attributes.健康状况" for p in pending), pending
    assert any(p["reason"] == "stale_revision" for p in pending), pending


def test_user_deleted_attribute_is_not_resurrected(db_conn):
    """用户删掉属性（纠正工具入口）→ 后续普通提炼不得复活。"""
    import asyncio

    card = _seed(db_conn)
    tool = CorrectEntityTool(db_conn)
    result = asyncio.run(
        tool.run(entity="我家的鹅", attribute_key="年龄", delete_attribute=True)
    )
    assert result.ok
    svc = EntityCardService(db_conn)
    assert "年龄" not in _attrs(svc.get(card.id))

    late = svc.upsert(
        _candidate(attributes=[EntityAttribute(key="年龄", value="2 岁")]),
        source=SOURCE_AUTO,
    )

    assert "年龄" not in _attrs(late), "用户删除不被普通提炼反转"
    pending = svc.pending_candidates(card.id)
    assert any(p["reason"] == "user_deleted" for p in pending), pending


def test_user_deleted_card_is_not_resurrected(db_conn, settings):
    """用户删除整张卡（管理 API）→ 自动提炼不复活、也不新建同名卡。"""
    from agent.api.server import create_app

    card = _seed(db_conn)
    app = create_app(settings, db_conn)
    with TestClient(app) as client:
        assert client.post(f"/api/entities/{card.id}/revoke").status_code == 200

    svc = EntityCardService(db_conn)
    late = svc.upsert(
        _candidate(attributes=[EntityAttribute(key="健康状况", value="新值")]),
        source=SOURCE_AUTO,
    )

    assert late is not None and late.id == card.id
    assert svc.find_by_name("我家的鹅") is None, "删除后不得重新出现在活跃卡里"
    assert svc.list_active() == []
    assert db_conn.execute("SELECT COUNT(*) c FROM entity_cards").fetchone()["c"] == 1
    pending = svc.pending_candidates(card.id)
    assert any(p["field"] == "card" and p["reason"] == "card_archived" for p in pending), pending


def test_legacy_card_without_meta_keeps_its_content(db_conn):
    """历史行（没有来源元数据）也要保守保留已有内容。

    A02 补充：旧行「有非空值、但 field_meta 里没有它的来源记录」= 来源未知，
    按**用户值**保护 —— 自动候选不得覆盖，冲突进 pending 候选；未提及项照旧保留。
    （旧断言曾认为该属性可以被自动值更新，与保护规则不一致，故按新规则更新。）
    """
    card = _seed(db_conn)
    db_conn.execute(
        "UPDATE entity_cards SET revision = 0, field_meta = '{}' WHERE id = ?", (card.id,)
    )
    svc = EntityCardService(db_conn)

    updated = svc.upsert(
        _candidate(attributes=[EntityAttribute(key="健康状况", value="已康复")]),
        source=SOURCE_AUTO,
    )

    values = _attrs(updated)
    assert values["健康状况"] == "红肿", "来源未知的旧值按用户值保护：自动候选不得覆盖"
    assert values["年龄"] == "2 岁", "旧行内容不能被整卡替换掉"
    pending = svc.pending_candidates(card.id)
    assert any(p["field"] == "attributes.健康状况" for p in pending), pending

    payload = svc.to_dict(updated)
    assert payload["attribute_sources"].get("健康状况") is None, (
        "旧值不得被伪造成 source=user（也不该被改写成别的来源）"
    )
    assert "attributes.健康状况" in payload["unknown_source_fields"]


def test_user_upsert_still_replaces_whole_card(db_conn):
    """用户/调用方明确给出的值（source=user）保持整卡替换语义，行为不倒退。"""
    card = _seed(db_conn)
    svc = EntityCardService(db_conn)

    replaced = svc.upsert(
        _candidate(attributes=[EntityAttribute(key="健康状况", value="已康复")]),
        source=SOURCE_USER,
    )

    assert replaced.id == card.id
    assert list(_attrs(replaced)) == ["健康状况"], "明确整卡给出时按整段替换"
    assert (
        svc.to_dict(replaced)["attribute_sources"]["健康状况"]["source"] == "user"
    )


def test_pending_candidate_is_manageable(db_conn):
    """被挡下的自动候选可管理：能读出来，也能采纳（采纳后按用户决定写入）。"""
    card = _seed(db_conn)
    svc = EntityCardService(db_conn)
    svc.set_attribute(card.id, "健康状况", "已康复")

    svc.upsert(
        _candidate(attributes=[EntityAttribute(key="健康状况", value="红肿")]),
        source=SOURCE_AUTO,
        expected_revision=card.revision,
    )
    pending = svc.pending_candidates(card.id)
    target = next(p for p in pending if p["field"] == "attributes.健康状况")

    resolved = svc.resolve_pending(card.id, target["id"], accept=True)

    assert resolved is not None
    assert _attrs(resolved)["健康状况"] == "红肿"
    assert svc.pending_candidates(card.id) == []
    assert (
        svc.to_dict(resolved)["attribute_sources"]["健康状况"]["source"] == "user"
    ), "采纳候选是人工决定 → 该字段标 user，后续自动提炼不再覆盖"


def test_to_dict_exposes_revision_and_candidates(db_conn):
    """管理侧可见性：修订版本与待处理候选都在结构化输出里。"""
    card = _seed(db_conn)
    payload = EntityCardService(db_conn).to_dict(card)

    assert payload["revision"] == card.revision
    assert payload["pending_candidates"] == []
    assert payload["field_sources"]["summary"]["source"] == "auto"
