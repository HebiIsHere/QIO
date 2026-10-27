# -*- coding: utf-8 -*-
"""B · 实体卡修订：关系增删与字段修订的**原子提交**、`expected_revision` 冲突 409。

界面「草稿化编辑」依赖的后端契约只有三条，这里逐条钉住：

1. `expected_revision` 与当前不符 → 409，且**一个字段都没写**（不静默覆盖）；
2. 字段修订与关系增删是**同一次提交**（不是「先存字段、再存关系」两次写入）；
3. 关系写入失败时字段修订随事务回滚 —— 不留半截状态。

冲突形状与 A04 的候选冲突保持一致（`conflict: true` + `current_revision`），
界面据此展示「刷新后重试」，不需要解析字符串。
"""

from __future__ import annotations

import sqlite3

import httpx
import pytest

from agent.entities.cards import (
    EntityAttribute,
    EntityCardCandidate,
    EntityCardConflict,
    EntityCardService,
)


def _seed(conn: sqlite3.Connection) -> tuple[str, str]:
    """两张卡 + 一条已有关系：修订用的真实起点。"""
    svc = EntityCardService(conn)
    goose = svc.upsert(
        EntityCardCandidate(
            name="我家的鹅",
            aliases=["鹅"],
            summary="旧摘要",
            attributes=[EntityAttribute(key="健康状况", value="红肿治疗中")],
        )
    )
    assert goose is not None
    owner = svc.upsert(EntityCardCandidate(name="王翠华"))
    assert owner is not None
    svc.add_relation(goose.id, "王翠华", "主人")
    return goose.id, owner.id


@pytest.fixture()
def app(tmp_path, db_conn, settings):
    from agent.api.server import create_app

    return create_app(settings, db_conn)


# ---------------------------------------------------------------------------
# 服务层：CAS 与「一次调用里改字段 + 增删关系」
# ---------------------------------------------------------------------------


def test_revise_rejects_stale_expected_revision(db_conn: sqlite3.Connection):
    entity_id, _ = _seed(db_conn)
    svc = EntityCardService(db_conn)
    start = svc.get(entity_id)
    assert start is not None
    stale = start.revision + 99

    with pytest.raises(EntityCardConflict) as excinfo:
        svc.revise(entity_id, summary="新摘要", expected_revision=stale)

    assert excinfo.value.current_revision == start.revision
    assert excinfo.value.expected_revision == stale
    assert excinfo.value.to_dict()["conflict"] is True
    card = svc.get(entity_id)
    assert card is not None
    assert card.summary == "旧摘要", "冲突时不得写入任何字段"
    assert card.revision == start.revision, "冲突不得推进版本"


def test_revise_applies_fields_and_relations_together(db_conn: sqlite3.Connection):
    entity_id, _ = _seed(db_conn)
    svc = EntityCardService(db_conn)
    start = svc.get(entity_id)
    assert start is not None

    updated = svc.revise(
        entity_id,
        summary="新摘要",
        attributes=[{"key": "健康状况", "value": "已康复", "confidence": 0.9}],
        relations_add=[{"type": "照顾", "target": "王翠华"}],
        relations_remove=[{"type": "主人", "target": "王翠华"}],
        expected_revision=start.revision,
    )

    assert updated is not None
    assert updated.summary == "新摘要"
    assert updated.attributes[0]["value"] == "已康复"
    assert updated.revision == start.revision + 1, "一次提交只推进一个版本"
    payload = svc.to_dict(updated)
    relations = {(r["type"], r["target"]) for r in payload["relations"]}
    assert ("照顾", "王翠华") in relations
    assert ("主人", "王翠华") not in relations


def test_revise_relation_only_still_bumps_revision(db_conn: sqlite3.Connection):
    entity_id, _ = _seed(db_conn)
    svc = EntityCardService(db_conn)
    start = svc.get(entity_id)
    assert start is not None

    updated = svc.revise(
        entity_id,
        relations_add=[{"type": "照顾", "target": "王翠华"}],
        expected_revision=start.revision,
    )
    assert updated is not None
    assert updated.revision == start.revision + 1
    assert updated.summary == "旧摘要", "只改关系不应改动摘要"


# ---------------------------------------------------------------------------
# HTTP 路由：409 形状 + 事务回滚
# ---------------------------------------------------------------------------


async def test_route_returns_409_and_writes_nothing(app, db_conn: sqlite3.Connection):
    entity_id, _ = _seed(db_conn)
    start = EntityCardService(db_conn).get(entity_id)
    assert start is not None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://qio.test"
    ) as client:
        resp = await client.post(
            f"/api/entities/{entity_id}/revise",
            json={
                "summary": "新摘要",
                "attributes": [{"key": "健康状况", "value": "已康复", "confidence": 0.9}],
                "relations": {"add": [{"type": "照顾", "target": "王翠华"}]},
                "expected_revision": start.revision + 99,
            },
        )

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["conflict"] is True
    assert body["current_revision"] == start.revision
    assert body["entity_id"] == entity_id

    card = EntityCardService(db_conn).get(entity_id)
    assert card is not None
    assert card.summary == "旧摘要"
    assert card.attributes[0]["value"] == "红肿治疗中"
    assert card.revision == start.revision
    relations = {
        (r["type"], r["target"])
        for r in EntityCardService(db_conn).to_dict(card)["relations"]
    }
    assert ("照顾", "王翠华") not in relations, "409 时关系也不能写进去"


async def test_route_commits_fields_and_relations_in_one_request(
    app, db_conn: sqlite3.Connection
):
    entity_id, _ = _seed(db_conn)
    start = EntityCardService(db_conn).get(entity_id)
    assert start is not None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://qio.test"
    ) as client:
        resp = await client.post(
            f"/api/entities/{entity_id}/revise",
            json={
                "summary": "新摘要",
                "aliases": ["鹅"],
                "relations": {
                    "add": [{"type": "照顾", "target": "王翠华"}],
                    "remove": [{"type": "主人", "target": "王翠华"}],
                },
                "expected_revision": start.revision,
            },
        )

    assert resp.status_code == 200, resp.text
    entity = resp.json()["entity"]
    assert entity["summary"] == "新摘要"
    assert entity["revision"] == start.revision + 1
    relations = {(r["type"], r["target"]) for r in entity["relations"]}
    assert ("照顾", "王翠华") in relations
    assert ("主人", "王翠华") not in relations


async def test_route_rolls_back_fields_when_relation_write_fails(
    app, db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
):
    entity_id, _ = _seed(db_conn)
    start = EntityCardService(db_conn).get(entity_id)
    assert start is not None

    def _boom(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise RuntimeError("relation write failed")

    monkeypatch.setattr(EntityCardService, "_delete_relation", _boom)

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://qio.test") as client:
        resp = await client.post(
            f"/api/entities/{entity_id}/revise",
            json={
                "summary": "新摘要",
                "relations": {"remove": [{"type": "主人", "target": "王翠华"}]},
                "expected_revision": start.revision,
            },
        )

    assert resp.status_code == 500, resp.text
    card = EntityCardService(db_conn).get(entity_id)
    assert card is not None
    assert card.summary == "旧摘要", "关系写入失败时摘要必须随事务回滚"
    assert card.revision == start.revision, "失败的提交不得推进版本"
