# -*- coding: utf-8 -*-
"""A04：被保护而没落的自动候选必须能被用户管理（清单 / 采纳 / 丢弃 + 端点）。

覆盖契约 §3.2/§3.3：
* 清单：跨卡片 + 单卡片、`include_archived`、`total/shown/truncated` 如实、字段形状可用；
* 采纳：写值 + `source="user"` + 新 revision + 移除该候选；`expected_revision` 不符 → 409
  且**一个字节都不改**；归档卡 `adoptable=False`+`blocked_reason="card_archived"`；
  重复点击幂等（`already_resolved`）；不支持的候选类型 `adoptable=False` + 原因；
* 丢弃：只解决该候选，不改当前值 / 不影响其他候选，重复点击幂等；
* 端点形状按 §3.3（409 是 `{"ok":false,"conflict":true,"current_revision":N,"reason":"..."}`）。
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.api.entity_pending_routes import build_router
from agent.entities.cards import (
    PENDING_CARD_ARCHIVED,
    PENDING_USER_VALUE,
    SOURCE_USER,
    EntityAttribute,
    EntityCardCandidate,
    EntityCardService,
)
from agent.entities.pending import (
    BLOCKED_UNSUPPORTED,
    CandidateConflict,
    CandidateNotFound,
    adopt,
    dismiss,
    list_candidates,
)

_TS = "2026-01-01T00:00:00+00:00"


def _candidate(name: str = "我家的鹅", **kwargs) -> EntityCardCandidate:
    return EntityCardCandidate(name=name, **kwargs)


def _seed_user_card(
    conn: sqlite3.Connection,
    *,
    name: str = "我家的鹅",
    aliases: list[str] | None = None,
    kind: str | None = "动物",
    summary: str = "用户写的摘要",
    attributes: dict[str, str] | None = None,
) -> str:
    """一张「用户明确设定过」的卡（每个字段 source=user）；返回真实卡片 id。"""
    card = EntityCardService(conn).upsert(
        _candidate(
            name=name,
            aliases=aliases if aliases is not None else ["鹅"],
            kind=kind,
            summary=summary,
            attributes=[
                EntityAttribute(key=k, value=v)
                for k, v in (attributes if attributes is not None else {"健康状况": "已康复"}).items()
            ],
        ),
        source=SOURCE_USER,
    )
    assert card is not None
    return card.id


def _seed_conflict(
    conn: sqlite3.Connection,
    *,
    name: str = "我家的鹅",
    summary: str = "用户写的摘要",
    kind: str = "动物",
    attributes: dict[str, str] | None = None,
    cand_summary: str | None = None,
    cand_kind: str | None = None,
    cand_attributes: dict[str, str] | None = None,
) -> tuple[str, dict[str, str]]:
    """走真实路径造出「被保护而没落」的候选（自动候选撞上用户值）。"""
    card_id = _seed_user_card(
        conn, name=name, summary=summary, kind=kind, attributes=attributes
    )
    svc = EntityCardService(conn)
    card = svc.get(card_id)
    assert card is not None
    svc.upsert(
        _candidate(
            name=name,
            summary=cand_summary or "",
            kind=cand_kind,
            attributes=[
                EntityAttribute(key=k, value=v)
                for k, v in (
                    {"健康状况": "红肿"} if cand_attributes is None else cand_attributes
                ).items()
            ],
        ),
        source="auto",
        expected_revision=card.revision,
    )
    current = svc.get(card_id)
    assert current is not None
    pending = {str(p["field"]): str(p["id"]) for p in svc.pending_candidates(card_id)}
    return card_id, pending


def _snapshot(conn: sqlite3.Connection, card_id: str) -> tuple[int, str]:
    row = conn.execute(
        "SELECT revision, field_meta FROM entity_cards WHERE id = ?", (card_id,)
    ).fetchone()
    return int(row["revision"]), str(row["field_meta"])


def _client(conn: sqlite3.Connection) -> TestClient:
    """把 A04 路由按契约工厂挂到一个真实 FastAPI 应用上（不动 api/server.py）。"""
    app = FastAPI()
    app.include_router(build_router(SimpleNamespace(conn=conn)))
    return TestClient(app)


# -- 清单 ---------------------------------------------------------------------


def test_list_candidates_reports_total_and_truncation_honestly(db_conn):
    """total 是匹配总数（不受 limit 影响），truncated 如实；未显示的候选仍然存在。"""
    card_ids = []
    for index in range(3):
        card_id, pending = _seed_conflict(
            db_conn,
            name=f"实体{index}",
            cand_attributes={"健康状况": f"模型值{index}"},
        )
        assert pending
        card_ids.append(card_id)

    full = list_candidates(db_conn)
    assert full.total == 3
    assert full.shown == 3
    assert full.truncated is False

    page = list_candidates(db_conn, limit=1)
    assert page.total == 3, "total 不受 limit 影响"
    assert page.shown == 1
    assert page.truncated is True
    assert len(page.candidates) == 1

    zero = list_candidates(db_conn, limit=0)
    assert (zero.total, zero.shown, zero.truncated) == (3, 0, True)

    # 继续取不会因为「上一次截断」而消失
    assert {c.candidate_id for c in list_candidates(db_conn, limit=200).candidates} == {
        c.candidate_id for c in full.candidates
    }


def test_list_candidates_single_card_and_include_archived(db_conn):
    """单卡片过滤 + include_archived 语义（归档卡的候选不混进活跃视图）。"""
    active_id, pending_active = _seed_conflict(db_conn)
    svc = EntityCardService(db_conn)
    archived_id = _seed_user_card(db_conn, name="老仓库")
    assert svc.revoke(archived_id)
    svc.upsert(
        _candidate(name="老仓库", attributes=[EntityAttribute(key="位置", value="东区")]),
        source="auto",
    )
    assert svc.pending_candidates(archived_id), "归档卡的自动候选要记进该卡的 pending"

    one = list_candidates(db_conn, entity_id=active_id)
    assert one.total == 1
    assert one.candidates[0].entity_id == active_id
    assert one.candidates[0].candidate_id == pending_active["attributes.健康状况"]

    with_archived = list_candidates(db_conn, include_archived=True)
    assert {c.entity_id for c in with_archived.candidates} == {active_id, archived_id}

    active_only = list_candidates(db_conn, include_archived=False)
    assert {c.entity_id for c in active_only.candidates} == {active_id}
    assert active_only.total == 1


def test_candidate_view_is_display_ready(db_conn):
    """CandidateView 的每个字段都能直接给界面用（中文标签 + 一句话原因）。"""
    card_id, pending = _seed_conflict(
        db_conn, cand_summary="模型摘要", cand_kind="家禽", cand_attributes={"健康状况": "红肿"}
    )
    listing = list_candidates(db_conn, entity_id=card_id)
    by_field = {c.field: c for c in listing.candidates}

    assert set(by_field) == {"summary", "kind", "attributes.健康状况"}
    attr = by_field["attributes.健康状况"]
    assert attr.candidate_id == pending["attributes.健康状况"]
    assert attr.entity_name == "我家的鹅"
    assert attr.card_state == "active"
    assert attr.field_label == "属性「健康状况」"
    assert attr.kind == "attribute"
    assert attr.current_value == "已康复"
    assert attr.candidate_value == "红肿"
    assert attr.reason == PENDING_USER_VALUE
    assert attr.reason_label and "revision" not in attr.reason_label
    assert attr.created_at
    assert attr.card_revision == svc_revision(db_conn, card_id)
    assert attr.adoptable is True
    assert attr.blocked_reason == ""

    assert by_field["summary"].field_label == "摘要"
    assert by_field["summary"].kind == "summary"
    assert by_field["summary"].current_value == "用户写的摘要"
    assert by_field["summary"].candidate_value == "模型摘要"
    assert by_field["kind"].field_label == "类型"

    payload = listing.to_dict()
    assert set(payload) == {"candidates", "total", "shown", "truncated"}
    assert payload["total"] == 3 and payload["shown"] == 3 and payload["truncated"] is False


def svc_revision(conn: sqlite3.Connection, card_id: str) -> int:
    return int(EntityCardService(conn).get(card_id).revision)


def test_archived_card_candidate_is_not_adoptable(db_conn):
    """归档卡：候选可见但不可采纳，原因就是「先恢复实体」这件事。"""
    svc = EntityCardService(db_conn)
    archived_id = _seed_user_card(db_conn, name="老仓库")
    assert svc.revoke(archived_id)
    svc.upsert(
        _candidate(name="老仓库", attributes=[EntityAttribute(key="位置", value="东区")]),
        source="auto",
    )

    view = list_candidates(db_conn, entity_id=archived_id).candidates[0]
    assert view.card_state == "archived"
    assert view.adoptable is False
    assert view.blocked_reason == PENDING_CARD_ARCHIVED

    outcome = adopt(db_conn, archived_id, view.candidate_id)
    assert outcome.ok is False
    assert outcome.blocked_reason == PENDING_CARD_ARCHIVED
    assert outcome.adopted is None
    assert svc.get(archived_id).state == "archived"


def test_unsupported_candidate_type_is_not_adoptable(db_conn):
    """不支持的候选类型必须说清「为什么点不了」，而不是给一个没效果的按钮。"""
    card_id = _seed_user_card(db_conn)
    svc = EntityCardService(db_conn)
    card = svc.get(card_id)
    meta = json.loads(_snapshot(db_conn, card_id)[1])
    meta["pending"] = [
        {
            "id": "pc_weird",
            "field": "relations.某个关系",
            "value": "邻居",
            "reason": PENDING_USER_VALUE,
            "base_revision": card.revision,
            "at": _TS,
        }
    ]
    db_conn.execute(
        "UPDATE entity_cards SET field_meta = ? WHERE id = ?",
        (json.dumps(meta, ensure_ascii=False), card_id),
    )

    view = list_candidates(db_conn, entity_id=card_id).candidates[0]
    assert view.adoptable is False
    assert view.blocked_reason == BLOCKED_UNSUPPORTED
    assert view.reason_label

    before = _snapshot(db_conn, card_id)
    outcome = adopt(db_conn, card_id, "pc_weird")
    assert outcome.ok is False and outcome.blocked_reason == BLOCKED_UNSUPPORTED
    assert _snapshot(db_conn, card_id) == before, "说不支持就不能偷偷改任何东西"
    assert svc.pending_candidates(card_id), "候选不能被吞掉"


# -- 采纳 ---------------------------------------------------------------------


def test_adopt_writes_user_value_new_revision_and_removes_candidate(db_conn):
    """采纳 = 本次用户的明确决定：写值 + source=user + 新 revision + 移除该候选。"""
    card_id, pending = _seed_conflict(db_conn, cand_attributes={"健康状况": "红肿"})
    svc = EntityCardService(db_conn)
    before = svc.get(card_id)
    assert before is not None

    outcome = adopt(db_conn, card_id, pending["attributes.健康状况"], expected_revision=before.revision)

    assert outcome.ok is True
    assert outcome.already_resolved is False
    assert outcome.adopted == {"field": "attributes.健康状况", "value": "红肿"}
    after = svc.get(card_id)
    assert {str(a["key"]): str(a["value"]) for a in after.attributes}["健康状况"] == "红肿"
    assert after.revision == before.revision + 1
    payload = svc.to_dict(after)
    assert payload["attribute_sources"]["健康状况"]["source"] == SOURCE_USER
    assert svc.pending_candidates(card_id) == []
    assert outcome.entity["attributes"] == after.attributes


def test_adopt_summary_and_alias_candidates(db_conn):
    """摘要采纳直接替换；别名采纳只增不减（并集）。"""
    card_id, pending = _seed_conflict(
        db_conn, cand_summary="模型摘要", cand_kind="家禽", cand_attributes={"健康状况": "红肿"}
    )
    svc = EntityCardService(db_conn)

    adopted_summary = adopt(db_conn, card_id, pending["summary"], expected_revision=svc.get(card_id).revision)
    assert adopted_summary.ok and svc.get(card_id).summary == "模型摘要"
    assert svc.to_dict(svc.get(card_id))["field_sources"]["summary"]["source"] == SOURCE_USER

    alias_card, alias_pending = _seed_conflict(
        db_conn,
        name="邻居家的猫",
        attributes={"健康状况": "已康复"},
        cand_attributes={},
    )
    assert alias_pending == {}, "没有别名候选时不该凭空造"
    svc.upsert(
        _candidate(
            name="邻居家的猫",
            aliases=["大鹅"],
            attributes=[EntityAttribute(key="健康状况", value="红肿")],
        ),
        source="auto",
        expected_revision=svc.get(alias_card).revision,
    )
    alias_candidates = {c.field: c for c in list_candidates(db_conn, entity_id=alias_card).candidates}
    assert "aliases" in alias_candidates
    outcome = adopt(
        db_conn, alias_card, alias_candidates["aliases"].candidate_id, expected_revision=svc.get(alias_card).revision
    )
    assert outcome.ok
    assert "大鹅" in svc.get(alias_card).aliases, "采纳别名 = 明确加入"
    assert "鹅" in svc.get(alias_card).aliases, "原有别名不得被替换掉"


def test_adopt_user_deleted_attribute_candidate_clears_tombstone(db_conn):
    """采纳「你删除过」的属性候选 = 用户明确要它回来：值写入、墓碑清除；未采纳时仍受保护。"""
    card_id = _seed_user_card(db_conn, attributes={"工位": "A12"})
    svc = EntityCardService(db_conn)
    assert svc.remove_attribute(card_id, "工位") is not None
    svc.upsert(
        _candidate(attributes=[EntityAttribute(key="工位", value="B07")]),
        source="auto",
    )
    pending = {str(p["field"]): p for p in svc.pending_candidates(card_id)}
    assert pending["attributes.工位"]["reason"] == "user_deleted"
    assert "工位" not in {str(a["key"]) for a in svc.get(card_id).attributes}

    outcome = adopt(db_conn, card_id, str(pending["attributes.工位"]["id"]))

    assert outcome.ok is True
    assert {str(a["key"]): str(a["value"]) for a in svc.get(card_id).attributes}["工位"] == "B07"
    meta = json.loads(_snapshot(db_conn, card_id)[1])
    assert "工位" not in meta.get("tombstones", {})


def test_adopt_with_wrong_expected_revision_changes_nothing(db_conn):
    """expected_revision 不符 → CandidateConflict，且 revision / field_meta / 值都不动。"""
    card_id, pending = _seed_conflict(db_conn, cand_attributes={"健康状况": "红肿"})
    svc = EntityCardService(db_conn)
    before = _snapshot(db_conn, card_id)
    before_card = svc.get(card_id)

    with pytest.raises(CandidateConflict) as excinfo:
        adopt(db_conn, card_id, pending["attributes.健康状况"], expected_revision=before_card.revision + 5)

    assert excinfo.value.reason == "stale_revision"
    assert excinfo.value.current_revision == before_card.revision
    assert excinfo.value.to_dict()["conflict"] is True
    assert _snapshot(db_conn, card_id) == before
    assert {str(a["key"]): str(a["value"]) for a in svc.get(card_id).attributes}["健康状况"] == "已康复"
    assert svc.pending_candidates(card_id), "冲突时候选必须还在"


def test_adopt_is_idempotent_on_repeat(db_conn):
    """重复点击：候选已解决 → already_resolved，不报 500、不重复改值、不再推进版本。"""
    card_id, pending = _seed_conflict(db_conn, cand_attributes={"健康状况": "红肿"})
    svc = EntityCardService(db_conn)
    candidate_id = pending["attributes.健康状况"]

    first = adopt(db_conn, card_id, candidate_id, expected_revision=svc.get(card_id).revision)
    assert first.ok and first.already_resolved is False
    after_first = _snapshot(db_conn, card_id)

    second = adopt(db_conn, card_id, candidate_id, expected_revision=svc.get(card_id).revision)
    assert second.ok is True
    assert second.already_resolved is True
    assert second.adopted is None
    assert _snapshot(db_conn, card_id) == after_first, "重复点击不得再改一次"
    assert svc.get(card_id).revision == after_first[0]


def test_adopt_unknown_candidate_is_not_found(db_conn):
    """从来没存在过的候选 id：404 语义（CandidateNotFound），而不是假装成功。"""
    card_id = _seed_user_card(db_conn)
    with pytest.raises(CandidateNotFound):
        adopt(db_conn, card_id, "pc_从不存在的候选")
    with pytest.raises(CandidateNotFound):
        adopt(db_conn, "ec_不存在的卡", "pc_x")


# -- 丢弃 ---------------------------------------------------------------------


def test_dismiss_only_resolves_that_candidate(db_conn):
    """丢弃只解决这一条：当前值不变、其他候选仍在。"""
    card_id, pending = _seed_conflict(
        db_conn, cand_summary="模型摘要", cand_attributes={"健康状况": "红肿"}
    )
    svc = EntityCardService(db_conn)
    target = pending["summary"]

    outcome = dismiss(db_conn, card_id, target, expected_revision=svc.get(card_id).revision)

    assert outcome.ok is True and outcome.dismissed is True
    card = svc.get(card_id)
    assert card.summary == "用户写的摘要", "丢弃不改当前值"
    remaining = {str(p["field"]) for p in svc.pending_candidates(card_id)}
    assert remaining == {"attributes.健康状况"}, "其他候选不受影响"
    assert str(json.loads(_snapshot(db_conn, card_id)[1])["resolved"][-1]["id"]) == target


def test_dismiss_is_idempotent_and_checks_revision(db_conn):
    """丢弃的重复点击幂等；expected_revision 不符 → 409 且什么都不改。"""
    card_id, pending = _seed_conflict(db_conn, cand_attributes={"健康状况": "红肿"})
    svc = EntityCardService(db_conn)
    target = pending["attributes.健康状况"]

    with pytest.raises(CandidateConflict):
        dismiss(db_conn, card_id, target, expected_revision=999)
    assert _snapshot(db_conn, card_id)[0] == svc.get(card_id).revision
    assert len(svc.pending_candidates(card_id)) == 1

    first = dismiss(db_conn, card_id, target, expected_revision=svc.get(card_id).revision)
    assert first.dismissed is True
    after = _snapshot(db_conn, card_id)
    again = dismiss(db_conn, card_id, target, expected_revision=svc.get(card_id).revision)
    assert again.ok is True and again.already_resolved is True and again.dismissed is False
    assert _snapshot(db_conn, card_id) == after


# -- 端点（契约 §3.3 形状） ----------------------------------------------------


def test_endpoints_list_and_single_card(db_conn):
    card_id, pending = _seed_conflict(db_conn, cand_attributes={"健康状况": "红肿"})
    with _client(db_conn) as client:
        listing = client.get("/api/entities/candidates")
        assert listing.status_code == 200
        body = listing.json()
        assert set(body) == {"candidates", "total", "shown", "truncated"}
        assert body["total"] == 1 and body["shown"] == 1 and body["truncated"] is False
        assert body["candidates"][0]["candidate_id"] == pending["attributes.健康状况"]
        assert body["candidates"][0]["blocked_reason"] == ""
        assert body["candidates"][0]["adoptable"] is True

        single = client.get(f"/api/entities/{card_id}/candidates")
        assert single.status_code == 200
        payload = single.json()
        assert payload["entity"]["id"] == card_id
        assert payload["entity"]["revision"] >= 1
        assert len(payload["candidates"]) == 1

        archived_only = client.get("/api/entities/candidates?include_archived=false")
        assert archived_only.json()["total"] == 1

        missing = client.get("/api/entities/ec_不存在/candidates")
        assert missing.status_code == 404

        alias_path = client.get("/api/entity-candidates")
        assert alias_path.status_code == 200
        assert alias_path.json()["total"] == 1


def test_endpoint_adopt_ok_and_conflict(db_conn):
    card_id, pending = _seed_conflict(db_conn, cand_attributes={"健康状况": "红肿"})
    svc = EntityCardService(db_conn)
    candidate_id = pending["attributes.健康状况"]
    revision = svc.get(card_id).revision

    with _client(db_conn) as client:
        conflict = client.post(
            f"/api/entities/{card_id}/candidates/{candidate_id}/adopt",
            json={"expected_revision": revision + 1},
        )
        assert conflict.status_code == 409
        conflict_body = conflict.json()
        assert conflict_body["ok"] is False
        assert conflict_body["conflict"] is True
        assert conflict_body["current_revision"] == revision
        assert conflict_body["reason"] == "stale_revision"

        ok = client.post(
            f"/api/entities/{card_id}/candidates/{candidate_id}/adopt",
            json={"expected_revision": revision},
        )
        assert ok.status_code == 200
        body = ok.json()
        assert body["ok"] is True
        assert body["adopted"] == {"field": "attributes.健康状况", "value": "红肿"}
        assert body["entity"]["attribute_sources"]["健康状况"]["source"] == "user"

        repeat = client.post(
            f"/api/entities/{card_id}/candidates/{candidate_id}/adopt",
            json={"expected_revision": body["entity"]["revision"]},
        )
        assert repeat.status_code == 200
        assert repeat.json()["already_resolved"] is True
        assert repeat.json()["adopted"] is None


def test_endpoint_adopt_blocked_for_archived_card(db_conn):
    svc = EntityCardService(db_conn)
    archived_id = _seed_user_card(db_conn, name="老仓库")
    assert svc.revoke(archived_id)
    svc.upsert(_candidate(name="老仓库", attributes=[EntityAttribute(key="位置", value="东区")]), source="auto")
    candidate_id = str(svc.pending_candidates(archived_id)[0]["id"])

    with _client(db_conn) as client:
        response = client.post(
            f"/api/entities/{archived_id}/candidates/{candidate_id}/adopt", json={}
        )
        assert response.status_code == 409
        body = response.json()
        assert body["ok"] is False and body["conflict"] is True
        assert body["reason"] == PENDING_CARD_ARCHIVED
        assert body["current_revision"] == svc.get(archived_id).revision

        # 界面据此渲染：不可采纳 + 原因说明
        view = client.get(f"/api/entities/{archived_id}/candidates").json()["candidates"][0]
        assert view["adoptable"] is False
        assert view["blocked_reason"] == PENDING_CARD_ARCHIVED

        not_found = client.post(
            f"/api/entities/{archived_id}/candidates/pc_不存在/dismiss", json={}
        )
        assert not_found.status_code == 404


def test_endpoint_dismiss_is_idempotent(db_conn):
    card_id, pending = _seed_conflict(db_conn, cand_attributes={"健康状况": "红肿"})
    svc = EntityCardService(db_conn)
    candidate_id = pending["attributes.健康状况"]

    with _client(db_conn) as client:
        first = client.post(
            f"/api/entities/{card_id}/candidates/{candidate_id}/dismiss",
            json={"expected_revision": svc.get(card_id).revision},
        )
        assert first.status_code == 200
        assert first.json()["dismissed"] is True
        assert first.json()["already_resolved"] is False

        stale = client.post(
            f"/api/entities/{card_id}/candidates/{candidate_id}/dismiss",
            json={"expected_revision": 0},
        )
        assert stale.status_code == 409
        assert stale.json()["reason"] == "stale_revision"

        again = client.post(
            f"/api/entities/{card_id}/candidates/{candidate_id}/dismiss",
            json={"expected_revision": svc.get(card_id).revision},
        )
        assert again.status_code == 200
        assert again.json()["already_resolved"] is True
        assert again.json()["dismissed"] is False
