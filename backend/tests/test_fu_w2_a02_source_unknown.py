# -*- coding: utf-8 -*-
"""A02：历史来源未知（旧 schema / meta 被清）必须被保护。

冻结规则：某个字段/属性在卡上**已有非空值**、而 `field_meta` 里**没有**它的来源记录时，
来源视为 `unknown`，**按用户值保护** —— 自动候选不得覆盖，冲突进 pending 候选；
不得把 legacy 值伪造成 `source="user"`；`aliases` / `summary` / `kind` /
`attributes.<键>` 一致适用；未提及项保留；空缺仍可补；墓碑与归档卡的闸门不放松。
"""
from __future__ import annotations

import json
import sqlite3

from agent.entities.cards import (
    PENDING_CARD_ARCHIVED,
    PENDING_UNKNOWN_SOURCE,
    PENDING_USER_DELETED,
    SOURCE_AUTO,
    SOURCE_USER,
    SOURCE_UNKNOWN,
    EntityAttribute,
    EntityCardCandidate,
    EntityCardService,
)

_TS = "2026-01-01T00:00:00+00:00"


def _legacy_card(
    conn: sqlite3.Connection,
    *,
    card_id: str = "ec_legacy_1",
    name: str = "老陈",
    aliases: list[str] | None = None,
    kind: str | None = "同事",
    summary: str | None = "旧库里的手写摘要",
    attributes: list[dict] | None = None,
    state: str = "active",
    meta: dict | None = None,
) -> str:
    """插入一条真正的「旧 schema 形状」行：没有任何来源记录（revision=0 / meta='{}'）。"""
    conn.execute(
        "INSERT INTO entity_cards (id, node_id, name, aliases, kind, summary, attributes, "
        "state, created_at, updated_at, revision, field_meta) "
        "VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
        (
            card_id,
            name,
            json.dumps(aliases if aliases is not None else ["陈工"]),
            kind,
            summary,
            json.dumps(attributes if attributes is not None else [], ensure_ascii=False),
            state,
            _TS,
            _TS,
            json.dumps(meta if meta is not None else {}),
        ),
    )
    return card_id


def _candidate(**kwargs) -> EntityCardCandidate:
    return EntityCardCandidate(name="老陈", **kwargs)


def _attrs(card) -> dict[str, str]:
    return {str(a.get("key")): str(a.get("value")) for a in card.attributes}


def _pending(svc: EntityCardService, card_id: str) -> dict[str, dict]:
    return {str(p.get("field")): p for p in svc.pending_candidates(card_id)}


def test_source_unknown_constant_is_distinct():
    """来源常量必须与 user/auto/system 区分开（旧值不是「用户值」，但受同等保护）。"""
    assert SOURCE_UNKNOWN == "unknown"
    assert SOURCE_UNKNOWN not in (SOURCE_USER, SOURCE_AUTO, "system")


def test_legacy_summary_kind_aliases_are_all_protected(db_conn):
    """摘要 / 类型 / 别名：有非空值又没有来源记录 → 自动候选只进 pending，原值保留。"""
    card_id = _legacy_card(db_conn)
    svc = EntityCardService(db_conn)

    updated = svc.upsert(
        _candidate(aliases=["老陈头"], summary="模型新摘要", kind="朋友"),
        source=SOURCE_AUTO,
    )

    assert updated is not None
    assert updated.summary == "旧库里的手写摘要"
    assert updated.kind == "同事"
    assert updated.aliases == ["陈工"]
    pending = _pending(svc, card_id)
    assert set(pending) == {"aliases", "summary", "kind"}, pending
    assert all(p["reason"] == PENDING_UNKNOWN_SOURCE for p in pending.values()), pending
    assert pending["aliases"]["value"] == "老陈头"
    assert pending["summary"]["value"] == "模型新摘要"
    assert pending["kind"]["value"] == "朋友"


def test_legacy_attribute_with_value_is_protected(db_conn):
    """属性：有非空值 + 没有来源记录 → 受保护；未提及的其它属性照旧保留。"""
    card_id = _legacy_card(
        db_conn,
        attributes=[{"key": "工位", "value": "A12"}, {"key": "爱好", "value": "钓鱼"}],
    )
    svc = EntityCardService(db_conn)

    updated = svc.upsert(
        _candidate(attributes=[EntityAttribute(key="工位", value="B07")]),
        source=SOURCE_AUTO,
    )

    values = _attrs(updated)
    assert values == {"工位": "A12", "爱好": "钓鱼"}
    pending = _pending(svc, card_id)
    assert pending["attributes.工位"]["reason"] == PENDING_UNKNOWN_SOURCE
    assert pending["attributes.工位"]["value"] == "B07"


def test_legacy_values_are_not_fabricated_as_user_source(db_conn):
    """不得把 legacy 值写成 source="user" 来伪造来源；也不得改写成未知以外的来源。"""
    card_id = _legacy_card(
        db_conn, attributes=[{"key": "工位", "value": "A12"}], kind="同事", summary="旧摘要"
    )
    svc = EntityCardService(db_conn)

    svc.upsert(_candidate(kind="朋友"), source=SOURCE_AUTO)

    payload = svc.to_dict(svc.get(card_id))
    assert "kind" not in payload["field_sources"], payload["field_sources"]
    assert payload["attribute_sources"] == {}
    assert set(payload["unknown_source_fields"]) == {
        "summary",
        "kind",
        "aliases",
        "attributes.工位",
    }
    # 读时按 unknown 保护，也没有被「顺手写回」成 user
    assert "user" not in json.dumps(payload["field_sources"])


def test_legacy_gaps_can_still_be_filled(db_conn):
    """空缺（字段为空 / 属性不存在）仍然可以补 —— 保护的是已有内容，不是整张卡。"""
    card_id = _legacy_card(
        db_conn, aliases=[], kind=None, summary="", attributes=[]
    )
    svc = EntityCardService(db_conn)

    updated = svc.upsert(
        _candidate(
            aliases=["陈工"],
            summary="模型补的摘要",
            kind="同事",
            attributes=[EntityAttribute(key="工位", value="A12")],
        ),
        source=SOURCE_AUTO,
    )

    assert updated.summary == "模型补的摘要"
    assert updated.kind == "同事"
    assert updated.aliases == ["陈工"]
    assert _attrs(updated) == {"工位": "A12"}
    assert _pending(svc, card_id) == {}
    payload = svc.to_dict(updated)
    assert payload["unknown_source_fields"] == [], "补上的值记了 auto 来源，不再是未知"
    assert payload["field_sources"]["summary"]["source"] == SOURCE_AUTO


def test_legacy_attribute_with_empty_value_is_a_gap(db_conn):
    """属性存在但值为空 = 空缺（可补），不是「已有非空值必须保护」。"""
    card_id = _legacy_card(db_conn, attributes=[{"key": "工位", "value": ""}])
    svc = EntityCardService(db_conn)

    updated = svc.upsert(
        _candidate(attributes=[EntityAttribute(key="工位", value="A12")]),
        source=SOURCE_AUTO,
    )

    assert _attrs(updated) == {"工位": "A12"}
    assert _pending(svc, card_id) == {}
    assert svc.to_dict(updated)["attribute_sources"]["工位"]["source"] == SOURCE_AUTO


def test_user_deleted_tombstone_still_wins_over_unknown_protection(db_conn):
    """墓碑不得被绕过：即使来源记录缺失，用户删除过的属性也不能被自动提炼复活。"""
    card_id = _legacy_card(db_conn, attributes=[{"key": "工位", "value": "A12"}])
    svc = EntityCardService(db_conn)
    assert svc.remove_attribute(card_id, "工位") is not None
    # 模拟旧库形状：墓碑还在，来源记录缺失
    db_conn.execute(
        "UPDATE entity_cards SET field_meta = ? WHERE id = ?",
        (json.dumps({"tombstones": {"工位": {"revision": 1, "at": _TS}}}, ensure_ascii=False), card_id),
    )

    updated = svc.upsert(
        _candidate(attributes=[EntityAttribute(key="工位", value="A12")]),
        source=SOURCE_AUTO,
    )

    assert "工位" not in _attrs(updated), "用户删除不被普通提炼反转"
    assert _pending(svc, card_id)["attributes.工位"]["reason"] == PENDING_USER_DELETED


def test_archived_legacy_card_is_not_resurrected(db_conn):
    """已归档（撤销）的旧卡不得被自动候选复活，候选记进该卡的 pending。"""
    card_id = _legacy_card(db_conn, state="archived")
    svc = EntityCardService(db_conn)

    result = svc.upsert(
        _candidate(attributes=[EntityAttribute(key="工位", value="A12")]),
        source=SOURCE_AUTO,
    )

    assert result is not None and result.id == card_id
    assert result.state == "archived"
    assert _attrs(result) == {}
    assert db_conn.execute("SELECT COUNT(*) c FROM entity_cards").fetchone()["c"] == 1
    assert _pending(svc, card_id)["card"]["reason"] == PENDING_CARD_ARCHIVED


def test_legacy_protection_holds_when_expected_revision_is_provided(db_conn):
    """提交时携带 expected_revision（旧行读到的是 0）也要走保护：来源未知不靠版本判定。"""
    card_id = _legacy_card(db_conn)
    svc = EntityCardService(db_conn)

    updated = svc.upsert(
        _candidate(summary="模型摘要", kind="朋友"),
        source=SOURCE_AUTO,
        expected_revision=0,
    )

    assert updated.summary == "旧库里的手写摘要"
    assert updated.kind == "同事"
    pending = _pending(svc, card_id)
    assert pending["summary"]["reason"] == PENDING_UNKNOWN_SOURCE
    assert pending["kind"]["reason"] == PENDING_UNKNOWN_SOURCE


def test_legacy_protection_holds_with_mismatching_expected_revision(db_conn):
    """expected_revision 早已对不上（卡在请求期间被改动）→ 一样不覆盖旧值。"""
    card_id = _legacy_card(db_conn)
    svc = EntityCardService(db_conn)
    svc.set_attribute(card_id, "工位", "A12")  # 用户动作：revision 推进到 1

    updated = svc.upsert(
        _candidate(summary="迟到的模型摘要"),
        source=SOURCE_AUTO,
        expected_revision=0,
    )

    assert updated.summary == "旧库里的手写摘要"
    pending = _pending(svc, card_id)
    assert pending["summary"]["reason"] == PENDING_UNKNOWN_SOURCE


def test_auto_source_card_is_still_updated_normally(db_conn):
    """不倒退：来源明确是 auto 的卡，自动提炼照旧更新（保护只针对未知/用户来源）。"""
    card_id = _legacy_card(
        db_conn,
        summary="",
        kind=None,
        aliases=[],
        attributes=[{"key": "工位", "value": "A12"}],
        meta={
            "fields": {"summary": {"source": SOURCE_AUTO, "revision": 1}},
            "attributes": {"工位": {"source": SOURCE_AUTO, "revision": 1}},
            "tombstones": {},
            "pending": [],
        },
    )
    svc = EntityCardService(db_conn)

    updated = svc.upsert(
        _candidate(
            summary="模型摘要",
            kind="同事",
            attributes=[EntityAttribute(key="工位", value="B07")],
        ),
        source=SOURCE_AUTO,
    )

    assert updated.summary == "模型摘要"
    assert updated.kind == "同事"
    assert _attrs(updated)["工位"] == "B07"
    assert _pending(svc, card_id) == {}
