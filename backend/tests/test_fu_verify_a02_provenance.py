"""V 组独立验证 · A02：历史来源未知必须被保护。

原缺陷（基线 `da0436b`，见 `_contracts` §3.1）

    提交自动候选时，保护判断只看 `field_meta.fields.<字段>.source == "user"`：

        aliases_field_source = self._field_source(meta, "aliases")
        alias_locked = aliases_field_source == SOURCE_USER or stale
        ...
        summary_source = self._field_source(meta, "summary")
        ...
        attr_source = self._attribute_source(meta, key)

    **旧 schema 升级上来的卡**（或 meta 被清过的卡）人工值非空、而 meta 里没有
    该字段的来源记录 —— `_field_source()` 返回 `None`，于是：

      * `summary`：`None != "user"` → 直接 `values["summary"] = cand.summary`，
        **人工摘要被自动候选覆盖**，而且一个候选都不登记；
      * `kind`、每个 `attributes.<key>`：同理被覆盖；
      * `aliases`：`alias_locked=False` → 直接并进别名列表，也不登记候选。

    用户看不见这件事（既没有改动提示，也没有可管理的候选），历史人工值就这么没了。

验证手段

    * 直接构造「旧 schema 形状」的卡（`field_meta = '{}'`、`revision = 0`，
      summary / kind / aliases / attributes 都有人工值）；
    * 另加一条**真实升级路径**：用真实迁移定义把库建到 7（那时还没有
      revision / field_meta 列）→ 写入人工值 → `apply_migrations()` 升级 → 再提交自动候选；
    * 断言只看可观察量：卡片字段的最终值、新增了的空缺属性、
      以及「冲突是否被登记成可管理的候选」（冻结的候选读接口 + 既有 pending 读法取并集）。

    原缺陷在时红（值被覆盖、候选为空）；修复后仍绿。
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.entities.cards import (
    EntityAttribute,
    EntityCardCandidate,
    EntityCardService,
)
from agent.storage.db import transaction
from agent.storage.migrate import apply_migrations
from agent.storage.schema import MIGRATIONS

NOW = "2026-10-10T00:00:00+00:00"
CARD_ID = "card_legacy"

LEGACY_SUMMARY = "人工写的摘要：这只鹅会咬人"
LEGACY_KIND = "动物"
LEGACY_ALIAS = "大白"
LEGACY_AGE = "2 岁"

AUTO_SUMMARY = "自动摘要：鹅会看门"
AUTO_KIND = "家禽"
AUTO_ALIAS = "新别名"
AUTO_AGE = "3 岁"
AUTO_NEW_ATTR_KEY = "健康状况"
AUTO_NEW_ATTR_VALUE = "良好"


def _seed_legacy_card(conn: sqlite3.Connection) -> None:
    """旧 schema 形状：内容非空，来源记录缺失（field_meta 为空对象）。"""
    conn.execute(
        "INSERT INTO entity_cards (id, node_id, name, aliases, kind, summary, attributes, "
        "state, created_at, updated_at, revision, field_meta) "
        "VALUES (?, NULL, '我家的鹅', ?, ?, ?, ?, 'active', ?, ?, 0, '{}')",
        (
            CARD_ID,
            json.dumps([LEGACY_ALIAS], ensure_ascii=False),
            LEGACY_KIND,
            LEGACY_SUMMARY,
            json.dumps(
                [{"key": "年龄", "value": LEGACY_AGE, "confidence": 0.9}], ensure_ascii=False
            ),
            NOW,
            NOW,
        ),
    )


def _build_through(conn: sqlite3.Connection, target: int) -> None:
    """用真实迁移定义把库建到 target（含）。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " id INTEGER PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    for version, statements in MIGRATIONS:
        if version > target:
            continue
        with transaction(conn):
            for stmt in statements:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, NOW),
            )


def _auto_candidate() -> EntityCardCandidate:
    return EntityCardCandidate(
        name="我家的鹅",
        aliases=[AUTO_ALIAS],
        kind=AUTO_KIND,
        summary=AUTO_SUMMARY,
        attributes=[
            EntityAttribute(key="年龄", value=AUTO_AGE),
            EntityAttribute(key=AUTO_NEW_ATTR_KEY, value=AUTO_NEW_ATTR_VALUE),
        ],
    )


def _attrs(card) -> dict[str, str]:  # noqa: ANN001
    return {str(a.get("key")): str(a.get("value")) for a in card.attributes}


def _registered_candidates(conn: sqlite3.Connection, tmp_path, card_id: str) -> list[dict]:
    """「冲突进候选」的可观察证据：既有 pending 读法 + 冻结的候选读接口，取并集。

    两种读法任一能读出这条冲突都算「候选被登记了」；读接口在基线是 404，
    所以基线这里只会拿到既有 pending，而在基线上它本来就是空的。
    """
    found: list[dict] = list(EntityCardService(conn).pending_candidates(card_id))
    app = create_app(Settings(data_dir=tmp_path / "candidates-app"), conn)
    with TestClient(app) as client:
        resp = client.get(f"/api/entities/{card_id}/candidates")
        if resp.status_code == 200:
            body = resp.json()
            for item in body.get("candidates", []) or []:
                found.append(dict(item))
    return found


@pytest.fixture()
def legacy_card(db_conn, tmp_path):
    _seed_legacy_card(db_conn)
    return db_conn, tmp_path


# --------------------------------------------------------------------------
# 主场景：旧 schema 人工值 vs 自动候选
# --------------------------------------------------------------------------


def test_a02_legacy_manual_values_are_not_overwritten(legacy_card):
    """旧 schema 的人工值必须原样保留（基线：全部被自动候选覆盖）。"""
    conn, tmp_path = legacy_card
    service = EntityCardService(conn)

    updated = service.apply_auto_candidate(_auto_candidate())

    assert updated is not None
    assert updated.summary == LEGACY_SUMMARY, "历史人工摘要不得被自动候选覆盖"
    assert (updated.kind or "") == LEGACY_KIND, "历史人工类型不得被自动候选覆盖"
    attrs = _attrs(updated)
    assert attrs.get("年龄") == LEGACY_AGE, "历史人工属性值不得被自动候选覆盖"
    assert updated.aliases == [LEGACY_ALIAS], "历史人工别名不得被自动候选改写"


def test_a02_conflicts_are_registered_as_candidates(legacy_card):
    """被保护而没落地的候选必须登记成用户可管理的候选（基线：一个都没有）。"""
    conn, tmp_path = legacy_card
    service = EntityCardService(conn)

    service.apply_auto_candidate(_auto_candidate())

    candidates = _registered_candidates(conn, tmp_path, CARD_ID)
    assert candidates, "冲突必须进候选，不能静默丢弃"

    fields = {str(item.get("field")) for item in candidates}
    assert "summary" in fields, f"摘要冲突必须可管理：{fields}"
    assert "kind" in fields, f"类型冲突必须可管理：{fields}"
    assert "aliases" in fields, f"别名冲突必须可管理：{fields}"
    assert "attributes.年龄" in fields, f"属性冲突必须可管理：{fields}"

    values = {str(item.get("value")) for item in candidates}
    assert AUTO_SUMMARY in values
    assert AUTO_KIND in values
    assert AUTO_AGE in values
    assert AUTO_ALIAS in values


def test_a02_gaps_still_get_filled(legacy_card):
    """空缺（当前缺失）仍可合理补充：未提及的旧值保留，新属性正常补上。"""
    conn, tmp_path = legacy_card
    service = EntityCardService(conn)

    updated = service.apply_auto_candidate(_auto_candidate())

    attrs = _attrs(updated)
    assert attrs.get(AUTO_NEW_ATTR_KEY) == AUTO_NEW_ATTR_VALUE, "空缺属性应当被补上"
    assert attrs.get("年龄") == LEGACY_AGE, "已有属性必须保留（未提及≠删除）"


def test_a02_legacy_source_is_not_faked_as_user(legacy_card):
    """不得把 legacy 值伪装成 `source: "user"`；来源只能是 auto / unknown 一类。"""
    conn, tmp_path = legacy_card
    service = EntityCardService(conn)

    updated = service.apply_auto_candidate(_auto_candidate())
    payload = service.to_dict(updated)

    for source in payload.get("field_sources", {}).values():
        assert (source or {}).get("source") != "user", "legacy 值不得被写成用户来源"
    for source in payload.get("attribute_sources", {}).values():
        assert (source or {}).get("source") != "user", "legacy 属性不得被写成用户来源"


# --------------------------------------------------------------------------
# 真实升级路径：旧 schema（无 revision/field_meta） → 迁移 → 人工值 → 冲突
# --------------------------------------------------------------------------


def test_a02_real_upgrade_path_protects_manual_values(tmp_path):
    """真实升级路径：先在建到 7 的旧库里写人工值，再跑迁移，然后提交自动候选。"""
    from agent.storage.db import connect

    conn = connect(tmp_path / "upgrade.db")
    try:
        _build_through(conn, 7)
        columns = {str(r["name"]) for r in conn.execute("PRAGMA table_info(entity_cards)")}
        assert "field_meta" not in columns, "场景构造：升级前不该有 field_meta 列"
        assert "revision" not in columns, "场景构造：升级前不该有 revision 列"

        conn.execute(
            "INSERT INTO entity_cards (id, node_id, name, aliases, kind, summary, attributes, "
            "state, created_at, updated_at) "
            "VALUES (?, NULL, '我家的鹅', ?, ?, ?, ?, 'active', ?, ?)",
            (
                CARD_ID,
                json.dumps([LEGACY_ALIAS], ensure_ascii=False),
                LEGACY_KIND,
                LEGACY_SUMMARY,
                json.dumps(
                    [{"key": "年龄", "value": LEGACY_AGE, "confidence": 0.9}],
                    ensure_ascii=False,
                ),
                NOW,
                NOW,
            ),
        )

        apply_migrations(conn)
        columns = {str(r["name"]) for r in conn.execute("PRAGMA table_info(entity_cards)")}
        assert {"revision", "field_meta"} <= columns, "场景构造：迁移必须补上这两列"

        service = EntityCardService(conn)
        before = service.get(CARD_ID)
        assert before is not None
        assert before.summary == LEGACY_SUMMARY
        assert before.field_meta.get("fields", {}).get("summary") is None, (
            "场景构造：升级上来的行没有来源记录"
        )

        service.apply_auto_candidate(_auto_candidate())
        after = service.get(CARD_ID)
        assert after is not None

        assert after.summary == LEGACY_SUMMARY, "真实升级路径上的人工摘要必须被保护"
        assert (after.kind or "") == LEGACY_KIND
        attrs = _attrs(after)
        assert attrs.get("年龄") == LEGACY_AGE
        assert attrs.get(AUTO_NEW_ATTR_KEY) == AUTO_NEW_ATTR_VALUE

        candidates = _registered_candidates(conn, tmp_path, CARD_ID)
        fields = {str(item.get("field")) for item in candidates}
        assert "summary" in fields, f"真实升级路径上的冲突也必须进候选：{fields}"
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 不回归（对照）：用户明确值 / 墓碑 / 归档保护不得倒退
# --------------------------------------------------------------------------


def test_a02_explicit_user_value_keeps_blocking_and_registers_candidate(db_conn, tmp_path):
    """用户明确改过的字段：基线就在保护（对照，必须继续成立）。"""
    _seed_legacy_card(db_conn)
    service = EntityCardService(db_conn)

    # 用户明确改摘要 → source=user
    service.revise(CARD_ID, summary=LEGACY_SUMMARY)
    service.apply_auto_candidate(_auto_candidate())

    card = service.get(CARD_ID)
    assert card is not None
    assert card.summary == LEGACY_SUMMARY, "用户明确值不得被覆盖"

    candidates = _registered_candidates(db_conn, tmp_path, CARD_ID)
    assert any(str(item.get("field")) == "summary" for item in candidates)


def test_a02_missing_source_but_empty_value_is_still_a_gap(db_conn):
    """元数据缺失但值为空：这不是「人工值」，空缺应当照常补齐（不倒退）。"""
    db_conn.execute(
        "INSERT INTO entity_cards (id, node_id, name, aliases, kind, summary, attributes, "
        "state, created_at, updated_at, revision, field_meta) "
        "VALUES ('card_empty', NULL, '我家的鹅', '[]', NULL, '', '[]', 'active', ?, ?, 0, '{}')",
        (NOW, NOW),
    )
    service = EntityCardService(db_conn)

    service.apply_auto_candidate(_auto_candidate())

    card = service.get("card_empty")
    assert card is not None
    assert card.summary == AUTO_SUMMARY, "空摘要属于空缺，应该被补上"
    assert (card.kind or "") == AUTO_KIND, "空类型属于空缺，应该被补上"


def test_a02_archived_card_is_not_revived(db_conn, tmp_path):
    """已归档的卡不得被自动候选复活（对照）。"""
    _seed_legacy_card(db_conn)
    service = EntityCardService(db_conn)
    assert service.revoke(CARD_ID) is True

    service.apply_auto_candidate(_auto_candidate())

    card = service.get(CARD_ID)
    assert card is not None
    assert card.state == "archived", "归档状态不得被自动候选改回 active"
    assert service.find_by_name("我家的鹅") is None

    candidates = _registered_candidates(db_conn, tmp_path, CARD_ID)
    assert any(str(item.get("field")) == "card" for item in candidates), (
        "归档卡上的自动候选应当登记为可处理（而不是静默丢弃）"
    )
