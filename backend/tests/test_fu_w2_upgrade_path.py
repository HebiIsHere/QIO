# -*- coding: utf-8 -*-
"""A02/A04 真实升级路径：旧 schema → 迁移 29 → 卡上的人工值 → 自动候选冲突。

这是「旧库升级上来」的验收主线，用**真实迁移定义**构造，不靠把版本号调高：

1. 只用 `MIGRATIONS` 里 < 29 的真实迁移建库（此时 `entity_cards` 没有
   `revision` / `field_meta` 两列）；
2. 按旧形状写入一张卡（用户当年手填的摘要 / 类型 / 别名 / 属性，没有任何来源记录）；
3. 跑真实 `apply_migrations`（迁移 29 只加列、不回填）——数据必须完好；
4. 自动候选撞上来：原值保留 + 冲突候选进 pending（来源按 unknown 保护）、
   空缺可补、未提及项保留、重复候选不重复；
5. 候选可被用户处理（采纳 / 丢弃），采纳写 `source="user"` + 新 revision；
6. 人工纠正与删除保护不回退。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.api.entity_pending_routes import build_router
from agent.entities.cards import (
    PENDING_UNKNOWN_SOURCE,
    SOURCE_AUTO,
    SOURCE_USER,
    EntityAttribute,
    EntityCardCandidate,
    EntityCardService,
)
from agent.entities.pending import adopt, list_candidates
from agent.storage.db import connect, transaction
from agent.storage.migrate import apply_migrations, current_version
from agent.storage.schema import MIGRATIONS

# 旧库里有值但没有来源记录的字段（迁移 29 不回填，所以读出来就是 unknown）。
LEGACY_VALUES = {
    "name": "老陈",
    "aliases": ["陈工"],
    "kind": "同事",
    "summary": "旧库里的手写摘要",
    "attributes": [{"key": "工位", "value": "A12"}, {"key": "备注", "value": ""}],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _apply_migrations_until(conn: sqlite3.Connection, ceiling: int) -> int:
    """按真实迁移定义把库建到 `ceiling`（包含），复刻 `apply_migrations` 的事务语义。"""
    version = current_version(conn)
    for target, statements in MIGRATIONS:
        if target <= version or target > ceiling:
            continue
        with transaction(conn):
            for stmt in statements:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (target, _now()),
            )
        version = target
    return version


def _insert_legacy_card(conn: sqlite3.Connection, card_id: str = "ec_legacy_upgrade") -> str:
    """旧形状写入（没有 revision / field_meta 列，默认值就是「没有来源记录」）。"""
    now = _now()
    conn.execute(
        "INSERT INTO entity_cards (id, node_id, name, aliases, kind, summary, attributes, "
        "state, created_at, updated_at) VALUES (?, NULL, ?, ?, ?, ?, ?, 'active', ?, ?)",
        (
            card_id,
            LEGACY_VALUES["name"],
            json.dumps(LEGACY_VALUES["aliases"], ensure_ascii=False),
            LEGACY_VALUES["kind"],
            LEGACY_VALUES["summary"],
            json.dumps(LEGACY_VALUES["attributes"], ensure_ascii=False),
            now,
            now,
        ),
    )
    return card_id


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {str(row["name"]) for row in conn.execute("PRAGMA table_info(entity_cards)")}


def _candidate(**kwargs) -> EntityCardCandidate:
    return EntityCardCandidate(name=LEGACY_VALUES["name"], **kwargs)


def _attrs(card) -> dict[str, str]:
    return {str(a.get("key")): str(a.get("value")) for a in card.attributes}


def _legacy_db(tmp_path: Path) -> sqlite3.Connection:
    """旧 schema（< 29）的库 + 一张人工填过的卡（真实迁移定义构造）。"""
    conn = connect(tmp_path / "legacy_upgrade.db")
    below_29 = max(target for target, _ in MIGRATIONS if target < 29)
    assert _apply_migrations_until(conn, 28) == below_29, "旧库只建到 29 之前的真实迁移"
    assert current_version(conn) < 29, "迁移 29 还没应用，这才是升级前的存量库"
    assert "field_meta" not in _columns(conn) and "revision" not in _columns(conn)
    _insert_legacy_card(conn)
    return conn


def test_migration_29_is_the_real_source_of_the_columns(tmp_path):
    """迁移 29 必须是真实加列的那条（不是把版本号调高），且只加列不回填。"""
    statements_29 = next(stmts for target, stmts in MIGRATIONS if target == 29)
    assert any("entity_cards" in stmt and "revision" in stmt for stmt in statements_29)
    assert any("entity_cards" in stmt and "field_meta" in stmt for stmt in statements_29)
    assert not any("UPDATE entity_cards" in stmt.upper() for stmt in statements_29), (
        "迁移 29 只加列，不能顺手回填来源"
    )

    conn = _legacy_db(tmp_path)
    try:
        assert apply_migrations(conn) == max(target for target, _ in MIGRATIONS)
        assert current_version(conn) == max(target for target, _ in MIGRATIONS)
        assert {"revision", "field_meta"} <= _columns(conn)

        row = conn.execute(
            "SELECT * FROM entity_cards WHERE id = 'ec_legacy_upgrade'"
        ).fetchone()
        # 升级不得动用户数据
        assert row["name"] == LEGACY_VALUES["name"]
        assert json.loads(row["aliases"]) == LEGACY_VALUES["aliases"]
        assert row["kind"] == LEGACY_VALUES["kind"]
        assert row["summary"] == LEGACY_VALUES["summary"]
        assert json.loads(row["attributes"]) == LEGACY_VALUES["attributes"]
        # 只加列不回填：旧行读出来是 0 / 空 meta
        assert int(row["revision"]) == 0
        assert json.loads(row["field_meta"]) == {}
    finally:
        conn.close()


def test_upgrade_then_auto_candidate_protects_manual_values(tmp_path):
    """升级后第一次自动提炼：人工值原样保留 + 冲突候选可处理 + 空缺可补 + 未提及项保留。"""
    conn = _legacy_db(tmp_path)
    try:
        apply_migrations(conn)
        svc = EntityCardService(conn)
        card = svc.get("ec_legacy_upgrade")
        assert card is not None
        assert svc.to_dict(card)["unknown_source_fields"] == [
            "summary",
            "kind",
            "aliases",
            "attributes.工位",
        ], "有值却没有来源记录的字段必须按 unknown 保护（备注为空值，是空缺不是保护对象）"

        updated = svc.upsert(
            _candidate(
                aliases=["老陈头"],
                summary="模型以为的摘要",
                kind="朋友",
                attributes=[
                    EntityAttribute(key="工位", value="B07"),
                    EntityAttribute(key="备注", value="无"),
                    EntityAttribute(key="爱好", value="钓鱼"),
                ],
            ),
            source=SOURCE_AUTO,
            expected_revision=svc.revision_snapshot()["老陈"],
        )

        # 1) 人工值原样保留
        assert updated.summary == LEGACY_VALUES["summary"]
        assert updated.kind == LEGACY_VALUES["kind"]
        assert updated.aliases == LEGACY_VALUES["aliases"]
        # 2) 未提及项保留 + 空缺可补 + 新信息可加
        assert _attrs(updated) == {"工位": "A12", "备注": "无", "爱好": "钓鱼"}

        # 3) 冲突候选可处理（来源 unknown 保护）
        pending = {str(p["field"]): p for p in svc.pending_candidates(card.id)}
        assert set(pending) == {"aliases", "summary", "kind", "attributes.工位"}, pending
        assert all(p["reason"] == PENDING_UNKNOWN_SOURCE for p in pending.values())
        assert pending["attributes.工位"]["value"] == "B07"

        # 4) 重复候选幂等：同一批候选再提交一次，不重复、又不覆盖
        again = svc.upsert(
            _candidate(
                aliases=["老陈头"],
                summary="模型以为的摘要",
                kind="朋友",
                attributes=[EntityAttribute(key="工位", value="B07")],
            ),
            source=SOURCE_AUTO,
        )
        assert again.summary == LEGACY_VALUES["summary"]
        assert _attrs(again)["工位"] == "A12"
        assert len(svc.pending_candidates(card.id)) == 4, "相同候选不得堆成多条"

        # 5) 读侧清单：四个候选都可采纳，原因给用户看得懂
        listing = list_candidates(conn, entity_id=card.id)
        assert listing.total == 4
        for view in listing.candidates:
            assert view.adoptable is True
            assert view.blocked_reason == ""
            assert view.reason_label
            assert view.card_revision == svc.get(card.id).revision

        # 6) 采纳一个冲突候选 = 本次用户的明确决定：写值 + source=user + 新 revision
        revision_before = svc.get(card.id).revision
        outcome = adopt(
            conn, card.id, str(pending["summary"]["id"]), expected_revision=revision_before
        )
        assert outcome.ok is True
        adopted = svc.get(card.id)
        assert adopted.summary == "模型以为的摘要"
        assert adopted.revision == revision_before + 1
        payload = svc.to_dict(adopted)
        assert payload["field_sources"]["summary"]["source"] == SOURCE_USER
        assert "summary" not in payload["unknown_source_fields"]
        assert {str(p["field"]) for p in svc.pending_candidates(card.id)} == {
            "aliases",
            "kind",
            "attributes.工位",
        }

        # 7) 采纳后重复点击幂等
        repeat = adopt(conn, card.id, str(pending["summary"]["id"]))
        assert repeat.ok and repeat.already_resolved is True
        assert svc.get(card.id).revision == revision_before + 1
    finally:
        conn.close()


def test_upgrade_path_over_real_endpoints(tmp_path):
    """同一条升级路径走真实端点（W6 接的就是这些形状）。"""
    conn = _legacy_db(tmp_path)
    try:
        apply_migrations(conn)
        app = FastAPI()
        app.include_router(build_router(SimpleNamespace(conn=conn)))
        svc = EntityCardService(conn)
        card = svc.get("ec_legacy_upgrade")
        assert card is not None

        svc.upsert(
            _candidate(summary="模型摘要", kind="朋友", attributes=[EntityAttribute(key="工位", value="B07")]),
            source=SOURCE_AUTO,
        )

        with TestClient(app) as client:
            listing = client.get("/api/entities/candidates").json()
            assert listing["total"] == 3
            by_field = {item["field"]: item for item in listing["candidates"]}
            assert by_field["attributes.工位"]["current_value"] == "A12"
            assert by_field["attributes.工位"]["candidate_value"] == "B07"
            assert by_field["kind"]["reason"] == PENDING_UNKNOWN_SOURCE
            assert by_field["kind"]["reason_label"]

            kind_id = by_field["kind"]["candidate_id"]
            summary_id = by_field["summary"]["candidate_id"]
            revision = svc.get(card.id).revision

            stale = client.post(
                f"/api/entities/{card.id}/candidates/{summary_id}/adopt",
                json={"expected_revision": revision + 1},
            )
            assert stale.status_code == 409
            assert stale.json()["reason"] == "stale_revision"
            assert svc.get(card.id).summary == LEGACY_VALUES["summary"]

            dismissed = client.post(
                f"/api/entities/{card.id}/candidates/{kind_id}/dismiss",
                json={"expected_revision": revision},
            )
            assert dismissed.status_code == 200 and dismissed.json()["dismissed"] is True
            assert svc.get(card.id).kind == LEGACY_VALUES["kind"], "丢弃不改当前值"
            assert str(json.loads(
                conn.execute("SELECT field_meta FROM entity_cards WHERE id = ?", (card.id,)).fetchone()["field_meta"]
            )["resolved"][-1]["id"]) == kind_id

            adopted = client.post(
                f"/api/entities/{card.id}/candidates/{summary_id}/adopt",
                json={"expected_revision": svc.get(card.id).revision},
            )
            assert adopted.status_code == 200
            body = adopted.json()
            assert body["adopted"] == {"field": "summary", "value": "模型摘要"}
            assert body["entity"]["summary"] == "模型摘要"
            assert body["entity"]["field_sources"]["summary"]["source"] == SOURCE_USER
            # 丢弃过的候选不会再回来，未处理的候选还在
            remaining = client.get(f"/api/entities/{card.id}/candidates").json()
            assert {item["field"] for item in remaining["candidates"]} == {"attributes.工位"}
    finally:
        conn.close()


def test_upgrade_path_keeps_user_correction_and_deletion_protection(tmp_path):
    """升级后的人工纠正 / 删除保护不回退：用户改过的值不被覆盖，删掉的属性不复活。"""
    conn = _legacy_db(tmp_path)
    try:
        apply_migrations(conn)
        svc = EntityCardService(conn)
        card = svc.get("ec_legacy_upgrade")
        assert card is not None

        # 用户升级后明确纠正：工位 → C01（该字段变成 user 来源）
        corrected = svc.set_attribute(card.id, "工位", "C01")
        assert corrected is not None
        assert svc.to_dict(corrected)["attribute_sources"]["工位"]["source"] == SOURCE_USER
        # 用户明确删除：备注（记墓碑）
        deleted = svc.remove_attribute(card.id, "备注")
        assert deleted is not None

        late = svc.upsert(
            _candidate(
                attributes=[
                    EntityAttribute(key="工位", value="B07"),
                    EntityAttribute(key="备注", value="无"),
                ]
            ),
            source=SOURCE_AUTO,
        )

        values = _attrs(late)
        assert values["工位"] == "C01", "人工纠正优先于自动候选"
        assert "备注" not in values, "用户删除不被普通提炼反转"
        pending = {str(p["field"]): p for p in svc.pending_candidates(card.id)}
        assert pending["attributes.工位"]["reason"] == "user_value"
        assert pending["attributes.备注"]["reason"] == "user_deleted"
        # 已被用户明确的字段不再是「来源未知」
        assert "attributes.工位" not in svc.to_dict(late)["unknown_source_fields"]
    finally:
        conn.close()
