# -*- coding: utf-8 -*-
"""F02 定向回归：用户明确清空的实体字段必须被保护（空值 ≠ 没有来源）。

缺陷现场（核查）
----------------

`entities/cards.py` 的摘要 / 类型分支只看**现值是不是空**：

    if not (existing.summary or "").strip():
        values["summary"] = cand.summary     # ← 直接写自动值，绕过 SOURCE_USER

于是用户 `revise(summary='', kind='')` 明确清空之后，下一次自动提炼又把摘要和类型填回来
（用户清空 = 一个明确的决定，不能因为「字段是空的」就当成「从未填写」）。

修好之后：空格子也要先过 `_conflict_reason`（它与 aliases / attributes 共用同一套来源规则）：

* `source=user`（用户设定过 / 明确清空）→ 自动值只能进待处理候选，原值保持为空；
* 迟到结果（revision 不符）→ 同上；
* **从来没有填过**的空字段（来源缺失）→ 仍然可以补全；
* 旧记录（迁移上没有来源）里**已有非空值** → 仍然按用户值保护（A02 不回归）。
"""

from __future__ import annotations

import json
import sqlite3

from agent.entities.cards import (
    PENDING_USER_VALUE,
    SOURCE_AUTO,
    SOURCE_USER,
    SOURCE_UNKNOWN,
    EntityCardCandidate,
    EntityCardService,
)

_TS = "2026-01-01T00:00:00+00:00"


def _legacy_card(
    conn: sqlite3.Connection,
    *,
    card_id: str = "ec_legacy_f02",
    name: str = "老陈",
    kind: str | None = None,
    summary: str | None = None,
    meta: dict | None = None,
) -> str:
    """真实的「旧 schema 形状」：revision=0、field_meta='{}'（没有任何来源记录）。"""
    conn.execute(
        "INSERT INTO entity_cards (id, node_id, name, aliases, kind, summary, attributes, "
        "state, created_at, updated_at, revision, field_meta) "
        "VALUES (?, NULL, ?, '[]', ?, ?, '[]', 'active', ?, ?, 0, ?)",
        (card_id, name, kind, summary, _TS, _TS, json.dumps(meta if meta is not None else {})),
    )
    return card_id


def _cand(**kwargs) -> EntityCardCandidate:
    return EntityCardCandidate(name="老陈", **kwargs)


def _pending(svc: EntityCardService, card_id: str) -> dict[str, dict]:
    return {str(p.get("field")): p for p in svc.pending_candidates(card_id)}


def _source(svc: EntityCardService, card_id: str, field: str) -> str | None:
    meta = svc.get(card_id).field_meta
    return (meta.get("fields") or {}).get(field, {}).get("source")


# ---------------------------------------------------------------------------
# 用户清空：摘要 / 类型
# ---------------------------------------------------------------------------


def test_user_cleared_summary_and_kind_are_not_refilled(db_conn):
    svc = EntityCardService(db_conn)
    card = svc.upsert(_cand(summary="初始摘要", kind="同事"), source=SOURCE_AUTO)
    assert card is not None

    cleared = svc.revise(card.id, summary="", kind="")
    assert cleared is not None
    assert (cleared.summary or "") == ""
    assert (cleared.kind or "") == ""

    updated = svc.upsert(_cand(summary="模型新摘要", kind="朋友"), source=SOURCE_AUTO)
    assert updated is not None
    assert (updated.summary or "") == "", "用户清空过的摘要不得被自动候选复填"
    assert (updated.kind or "") == "", "用户清空过的类型不得被自动候选复填"

    pending = _pending(svc, card.id)
    assert pending["summary"]["value"] == "模型新摘要"
    assert pending["kind"]["value"] == "朋友"
    assert pending["summary"]["reason"] == PENDING_USER_VALUE
    assert pending["kind"]["reason"] == PENDING_USER_VALUE
    # 来源必须还是 user（清空是用户的明确决定）
    assert _source(svc, card.id, "summary") == SOURCE_USER
    assert _source(svc, card.id, "kind") == SOURCE_USER


def test_repeated_candidates_do_not_accumulate_or_refill(db_conn):
    svc = EntityCardService(db_conn)
    card = svc.upsert(_cand(summary="初始摘要"), source=SOURCE_AUTO)
    svc.revise(card.id, summary="")

    for _ in range(3):
        again = svc.upsert(_cand(summary="同一个模型摘要"), source=SOURCE_AUTO)
        assert (again.summary or "") == ""

    pendings = [p for p in svc.pending_candidates(card.id) if p.get("field") == "summary"]
    assert len(pendings) == 1, "同一条（字段 + 值）候选只该有一条"
    assert pendings[0]["value"] == "同一个模型摘要"


def test_late_candidate_with_a_stale_revision_still_does_not_refill(db_conn):
    """迟到的自动结果（提交时 revision 已经不符）同样不得复填用户清空的字段。"""
    svc = EntityCardService(db_conn)
    card = svc.upsert(_cand(summary="初始摘要", kind="同事"), source=SOURCE_AUTO)
    stale_revision = card.revision

    cleared = svc.revise(card.id, summary="", kind="")
    assert cleared is not None

    late = svc.upsert(
        _cand(summary="迟到的摘要", kind="迟到的类型"),
        source=SOURCE_AUTO,
        expected_revision=stale_revision,
    )
    assert late is not None
    assert (late.summary or "") == ""
    assert (late.kind or "") == ""
    pending = _pending(svc, card.id)
    assert pending["summary"]["value"] == "迟到的摘要"
    assert pending["kind"]["value"] == "迟到的类型"


# ---------------------------------------------------------------------------
# 从来没有填过的空字段：仍然可以补全
# ---------------------------------------------------------------------------


def test_never_filled_empty_fields_are_still_filled(db_conn):
    svc = EntityCardService(db_conn)
    card = svc.upsert(EntityCardCandidate(name="新实体"), source=SOURCE_AUTO)
    assert card is not None and (card.summary or "") == ""

    updated = svc.upsert(_cand(summary="模型补的摘要", kind="朋友"), source=SOURCE_AUTO)
    assert updated is not None
    assert updated.summary == "模型补的摘要"
    assert updated.kind == "朋友"
    assert _source(svc, card.id, "summary") == SOURCE_AUTO


def test_migrated_legacy_row_with_empty_values_is_still_fillable(db_conn):
    """旧记录迁移上来：值本来就空、meta 里也没有来源 → 空缺仍可合理补充。"""
    card_id = _legacy_card(db_conn, summary=None, kind=None)
    svc = EntityCardService(db_conn)

    updated = svc.upsert(_cand(summary="补上的摘要", kind="同事"), source=SOURCE_AUTO)
    assert updated is not None
    assert updated.summary == "补上的摘要"
    assert updated.kind == "同事"


def test_migrated_legacy_row_with_values_stays_protected(db_conn):
    """旧记录里**已有非空值**又没有来源记录 → 仍然按用户值保护（A02 不回归）。"""
    card_id = _legacy_card(db_conn, summary="旧库手写摘要", kind="同事")
    svc = EntityCardService(db_conn)

    updated = svc.upsert(_cand(summary="模型新摘要", kind="朋友"), source=SOURCE_AUTO)
    assert updated is not None
    assert updated.summary == "旧库手写摘要"
    assert updated.kind == "同事"
    pending = _pending(svc, card_id)
    assert pending["summary"]["value"] == "模型新摘要"
    # A02 是**读时判定**：不往历史行里伪造来源，只在读出口如实列出「来源未知」
    assert "summary" in svc.to_dict(svc.get(card_id))["unknown_source_fields"]
    assert _source(svc, card_id, "summary") in (None, SOURCE_UNKNOWN), "不得伪造成 user"


# ---------------------------------------------------------------------------
# 其它字段的来源规则不因这次修复而改变
# ---------------------------------------------------------------------------


def test_alias_and_attribute_rules_are_unchanged(db_conn):
    """aliases / attributes 的既有规则原样保留（不是只给 summary/kind 打特判）。"""
    svc = EntityCardService(db_conn)
    card = svc.upsert(
        _cand(aliases=["阿陈"], attributes=[]), source=SOURCE_AUTO
    )
    # 用户明确给出空别名列表 = 清空；自动候选不得再加
    cleared = svc.revise(card.id, aliases=[])
    assert cleared is not None and cleared.aliases == []
    updated = svc.upsert(_cand(aliases=["老陈头"]), source=SOURCE_AUTO)
    assert updated is not None and updated.aliases == [], "用户清空的别名不得被复填"
    assert any(p.get("field") == "aliases" for p in svc.pending_candidates(card.id))

    # 用户删除过的属性（墓碑）不得被自动提炼复活
    svc.set_attribute(card.id, "城市", "杭州")
    svc.remove_attribute(card.id, "城市")
    again = svc.upsert(
        _cand(attributes=[{"key": "城市", "value": "上海", "confidence": 0.9}]),
        source=SOURCE_AUTO,
    )
    assert again is not None
    assert {a.get("key") for a in again.attributes} == set()
    assert any(
        p.get("field") == "attributes.城市" for p in svc.pending_candidates(card.id)
    )
