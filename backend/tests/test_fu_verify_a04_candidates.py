"""V 组独立验证 · A04：候选必须能被用户管理（端到端）。

原缺陷（基线 `da0436b`，见 `_contracts` §3.2 / §3.3）

    被保护下来的自动候选只以 `field_meta.pending` 的形式躺在 JSON 列里：
    * 没有 `entities/pending.py`（`CandidateView` / `list_candidates` / `adopt` /
      `dismiss` 都不存在）；
    * 没有 `api/entity_pending_routes.py`，`/api/entities/{id}/candidates` 与
      `.../candidates/{cid}/adopt|dismiss` 全部 404。

    于是界面上**没有任何入口**让用户处理这些候选：既看不见「有一份自动提炼想改
    这个字段」，也不能采纳或丢弃。这是「长期信息的用户控制」的直接缺失。

验证手段（全部走冻结的 HTTP 接口，只看可观察行为）

    * `GET /api/entities/candidates?include_archived=true` 与
      `GET /api/entities/{id}/candidates` 的 200 + 字段；
    * `POST .../adopt` 的 200/409、卡片最终值、`field_meta` 里的来源标记；
    * `POST .../dismiss` 的 200、当前值不变、其它候选不受影响；
    * 归档卡：`adoptable=False` + `blocked_reason="card_archived"`，采纳不得复活归档；
    * 过期候选（`expected_revision` 不符）：409 + 值不变；
    * 重复点击：幂等、不 500、不重复改值（revision 不再涨）。

    基线在这些断言上全红（404），修复后仍绿。

判据说明（补充修复轮修正，详见 `scripts/fu-verify/README.md`）

    **丢弃会推进卡级 `revision`（设计裁定，保留实现）**：`revision` 是**卡级 CAS
    令牌**，不是纯内容版本。丢弃要落 `field_meta.pending/resolved`，递增它才能让
    并发的「丢弃 / 采纳 / 自动提交」互相以 409 暴露，而不是静默覆盖较新的决定
    （提示词明确要求「不能静默覆盖较新的决定；提供刷新与冲突反馈」）。因此原
    「丢弃后 `revision` 相等」的断言写错了判据，被**更强**的行为断言取代：
    内容值一字不变 + 其它候选原样留在 pending + 用丢弃前的旧令牌采纳另一条候选
    必须 409 且一行不改。丢弃**不改内容值**这一条没有放宽。
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.entities.cards import (
    SOURCE_USER,
    EntityAttribute,
    EntityCardCandidate,
    EntityCardService,
)
from agent.credentials.store import MemoryKeyring

CARD_NAME = "我家的鹅"
USER_SUMMARY = "用户写下的摘要"
AUTO_SUMMARY = "自动提炼的摘要"
USER_KIND = "动物"
AUTO_KIND = "家禽"
USER_AGE = "2 岁"
AUTO_AGE = "3 岁"


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _seed_card(conn: sqlite3.Connection) -> str:
    """用户明确给出的卡（source=user），再提交一份冲突的自动候选。"""
    service = EntityCardService(conn)
    service.upsert(
        EntityCardCandidate(
            name=CARD_NAME,
            aliases=["大白"],
            kind=USER_KIND,
            summary=USER_SUMMARY,
            attributes=[EntityAttribute(key="年龄", value=USER_AGE)],
        ),
        source=SOURCE_USER,
    )
    card = service.find_by_name(CARD_NAME)
    assert card is not None, "场景构造：用户卡必须已建立"
    service.apply_auto_candidate(
        EntityCardCandidate(
            name=CARD_NAME,
            aliases=["新别名"],
            kind=AUTO_KIND,
            summary=AUTO_SUMMARY,
            attributes=[EntityAttribute(key="年龄", value=AUTO_AGE)],
        )
    )
    return card.id


def _listing(client: TestClient, entity_id: str) -> dict:
    resp = client.get(f"/api/entities/{entity_id}/candidates")
    assert resp.status_code == 200, (
        f"候选读接口必须可用（基线是 404）：{resp.status_code} {resp.text[:200]}"
    )
    body = resp.json()
    assert "candidates" in body, f"响应必须带 candidates：{list(body)}"
    return body


def _by_field(body: dict, field: str) -> dict:
    for item in body["candidates"]:
        if str(item.get("field")) == field:
            return item
    pytest.fail(f"没有 field={field!r} 的候选：{[i.get('field') for i in body['candidates']]}")


def _content_snapshot(service: EntityCardService, card_id: str) -> dict:
    """实体**内容值**快照：只取用户看得见的内容，不含 `revision` / `field_meta`。

    丢弃（以及被 409 挡下的动作）必须做到「内容一字不改」—— 这里就是那个
    「一字」的判据面：summary / kind / aliases / attributes 四项全等。
    """
    payload = service.to_dict(service.get(card_id))
    return {key: payload[key] for key in ("summary", "kind", "aliases", "attributes")}


def _other_pending_snapshot(body: dict, *, drop: str) -> list:
    """除 `drop` 之外的 pending 候选（顺序无关，逐条比较字段/值/原因）。"""
    return sorted(
        (
            str(item.get("field")),
            json.dumps(item.get("candidate_value"), ensure_ascii=False, sort_keys=True),
            str(item.get("reason") or ""),
        )
        for item in body["candidates"]
        if str(item.get("candidate_id")) != str(drop)
    )


# --------------------------------------------------------------------------
# 读取：跨卡片清单 + 单卡片清单
# --------------------------------------------------------------------------


def test_a04_cross_card_listing_lists_candidates(client: TestClient, db_conn):
    _seed_card(db_conn)

    resp = client.get("/api/entities/candidates?include_archived=true")

    assert resp.status_code == 200, f"跨卡片候选清单必须可用：{resp.status_code}"
    body = resp.json()
    assert "candidates" in body and "total" in body
    assert body["total"] >= 1
    fields = {str(item.get("field")) for item in body["candidates"]}
    assert "summary" in fields


def test_a04_listing_exposes_the_fields_the_ui_needs(client: TestClient, db_conn):
    card_id = _seed_card(db_conn)

    body = _listing(client, card_id)
    entry = _by_field(body, "summary")

    assert entry.get("candidate_id"), "候选必须有自己的 id（界面按它操作）"
    assert str(entry.get("entity_id")) == card_id
    assert str(entry.get("entity_name")) == CARD_NAME
    assert entry.get("card_state") == "active"
    assert str(entry.get("current_value")) == USER_SUMMARY
    assert str(entry.get("candidate_value")) == AUTO_SUMMARY
    assert entry.get("reason"), "必须给出机器原因"
    assert entry.get("reason_label"), "必须给出给用户看的一句话原因"
    assert "revision" not in str(entry.get("reason_label")), "给用户的原因不得暴露内部字段名"
    assert isinstance(entry.get("card_revision"), int)
    assert entry.get("adoptable") is True


# --------------------------------------------------------------------------
# 采纳
# --------------------------------------------------------------------------


def test_a04_adopt_writes_value_and_marks_user_source(client: TestClient, db_conn):
    card_id = _seed_card(db_conn)
    service = EntityCardService(db_conn)
    revision = service.get(card_id).revision
    entry = _by_field(_listing(client, card_id), "summary")

    resp = client.post(
        f"/api/entities/{card_id}/candidates/{entry['candidate_id']}/adopt",
        json={"expected_revision": revision},
    )

    assert resp.status_code == 200, f"采纳必须成功：{resp.status_code} {resp.text[:200]}"
    assert resp.json().get("ok") is True

    card = service.get(card_id)
    assert card.summary == AUTO_SUMMARY, "采纳后字段值必须是候选值"
    payload = service.to_dict(card)
    assert payload["field_sources"]["summary"]["source"] == SOURCE_USER, (
        "采纳是用户的明确决定，来源必须记成 user"
    )
    assert card.revision > revision, "采纳要推进 revision"
    assert _by_field_candidates(client, card_id, "summary") == [], "被采纳的候选必须消失"


def test_a04_adopt_with_stale_revision_conflicts_and_changes_nothing(client: TestClient, db_conn):
    card_id = _seed_card(db_conn)
    service = EntityCardService(db_conn)
    entry = _by_field(_listing(client, card_id), "summary")
    stale_revision = int(entry["card_revision"])

    # 用户又改了一次 → 候选登记时依据的版本已经过期
    service.revise(card_id, summary="用户后来自己改成了这句")
    before = service.get(card_id)

    resp = client.post(
        f"/api/entities/{card_id}/candidates/{entry['candidate_id']}/adopt",
        json={"expected_revision": stale_revision},
    )

    assert resp.status_code == 409, f"过期候选必须 409：{resp.status_code} {resp.text[:200]}"
    body = resp.json()
    assert body.get("conflict") is True
    assert isinstance(body.get("current_revision"), int)

    after = service.get(card_id)
    assert after.summary == "用户后来自己改成了这句", "409 不得改任何东西"
    assert after.revision == before.revision


def test_a04_duplicate_adopt_is_idempotent(client: TestClient, db_conn):
    card_id = _seed_card(db_conn)
    service = EntityCardService(db_conn)
    entry = _by_field(_listing(client, card_id), "summary")
    url = f"/api/entities/{card_id}/candidates/{entry['candidate_id']}/adopt"
    revision = service.get(card_id).revision

    first = client.post(url, json={"expected_revision": revision})
    assert first.status_code == 200
    revision_after_first = service.get(card_id).revision

    second = client.post(url, json={"expected_revision": revision})
    assert second.status_code != 500, "重复点击不得 500"
    assert second.status_code in (200, 409)

    card = service.get(card_id)
    assert card.summary == AUTO_SUMMARY, "值只能被采纳一次"
    assert card.revision == revision_after_first, "重复点击不得再改一次版本"


def test_a04_archived_card_is_not_adoptable(client: TestClient, db_conn):
    card_id = _seed_card(db_conn)
    service = EntityCardService(db_conn)
    assert service.revoke(card_id) is True
    archived = service.get(card_id)
    assert archived.state == "archived"

    body = _listing(client, card_id)
    entry = _by_field(body, "summary")
    assert entry.get("adoptable") is False, "归档卡上的候选不能是一个点了没效果的按钮"
    assert str(entry.get("blocked_reason")) == "card_archived"
    assert str(entry.get("card_state")) == "archived"

    resp = client.post(
        f"/api/entities/{card_id}/candidates/{entry['candidate_id']}/adopt",
        json={"expected_revision": archived.revision},
    )
    assert resp.status_code != 500
    assert "card_archived" in resp.text, "被挡下时必须说明原因（card_archived）"

    after = service.get(card_id)
    assert after.state == "archived", "采纳不得顺手复活已归档卡片"
    assert after.summary != AUTO_SUMMARY, "归档卡上的候选值不得被写进去"


# --------------------------------------------------------------------------
# 丢弃
# --------------------------------------------------------------------------


def test_a04_dismiss_resolves_only_that_candidate(client: TestClient, db_conn):
    """丢弃一条候选：内容一字不改、其它候选不受影响，并且**推进卡级 revision**。

    `revision` 是**卡级 CAS 令牌**，不是纯内容版本。丢弃要落
    `field_meta.pending/resolved`（一次真实的用户决定），递增它才能让并发的
    「丢弃 / 采纳 / 自动提交」互相以 409 暴露，而不是静默覆盖较新的决定。
    所以这里断言的是「内容值不变 + 令牌推进 + 旧令牌被拒」（判据比原来的
    「revision 相等」更强），而不是删掉版本断言。
    """
    card_id = _seed_card(db_conn)
    service = EntityCardService(db_conn)
    body = _listing(client, card_id)
    summary_entry = _by_field(body, "summary")
    kind_entry = _by_field(body, "kind")  # 场景前提：还存在别的候选
    revision_before = service.get(card_id).revision
    content_before = _content_snapshot(service, card_id)
    others_before = _other_pending_snapshot(body, drop=summary_entry["candidate_id"])

    resp = client.post(
        f"/api/entities/{card_id}/candidates/{summary_entry['candidate_id']}/dismiss",
        json={"expected_revision": revision_before},
    )

    assert resp.status_code == 200, f"丢弃必须成功：{resp.status_code} {resp.text[:200]}"
    assert resp.json().get("dismissed") is True or resp.json().get("ok") is True

    # 1) 内容值一字不变：丢弃不是一次内容修改
    assert _content_snapshot(service, card_id) == content_before, (
        f"丢弃不得改任何内容值：{content_before} → {_content_snapshot(service, card_id)}"
    )

    # 2) 卡级 CAS 令牌推进 —— 这正是并发保护：别的页面手里的旧令牌立刻失效
    revision_after = service.get(card_id).revision
    assert revision_after > revision_before, (
        "丢弃要落 field_meta.pending/resolved，必须推进卡级 revision（CAS 令牌）"
    )

    # 3) 其它候选仍在 pending：数量与内容都不变
    after_body = _listing(client, card_id)
    remaining = {str(item.get("field")) for item in after_body["candidates"]}
    assert "summary" not in remaining, "被丢弃的候选必须消失"
    assert "kind" in remaining, "其它候选不得被连带解决"
    after_others = _other_pending_snapshot(after_body, drop=summary_entry["candidate_id"])
    assert after_others == others_before, "其它候选必须原样留在 pending（数量与内容都不变）"

    # 4) 用**丢弃之前**的令牌采纳另一条候选 → 409，且一行不改、不改值
    stale = client.post(
        f"/api/entities/{card_id}/candidates/{kind_entry['candidate_id']}/adopt",
        json={"expected_revision": revision_before},
    )
    assert stale.status_code == 409, (
        "旧卡级令牌必须被拒绝（否则就是静默覆盖较新的决定）："
        f"{stale.status_code} {stale.text[:200]}"
    )
    assert stale.json().get("conflict") is True
    assert int(stale.json()["current_revision"]) == revision_after

    assert _content_snapshot(service, card_id) == content_before, "409 不得改任何值"
    assert service.get(card_id).revision == revision_after, "409 不得推进版本"
    assert _other_pending_snapshot(
        _listing(client, card_id), drop=summary_entry["candidate_id"]
    ) == others_before, "409 不得动其它候选"


def test_a04_duplicate_dismiss_is_idempotent(client: TestClient, db_conn):
    card_id = _seed_card(db_conn)
    service = EntityCardService(db_conn)
    entry = _by_field(_listing(client, card_id), "summary")
    url = f"/api/entities/{card_id}/candidates/{entry['candidate_id']}/dismiss"
    revision = service.get(card_id).revision

    assert client.post(url, json={"expected_revision": revision}).status_code == 200
    revision_after_first = service.get(card_id).revision
    assert revision_after_first > revision, "第一次丢弃要推进卡级 revision（CAS 令牌）"
    content_after_first = _content_snapshot(service, card_id)

    second = client.post(url, json={"expected_revision": revision})

    assert second.status_code != 500
    assert second.status_code in (200, 409)
    assert service.get(card_id).revision == revision_after_first, "重复丢弃不得再推进版本"
    assert _content_snapshot(service, card_id) == content_after_first, "重复丢弃不得改值"


def _by_field_candidates(client: TestClient, entity_id: str, field: str) -> list[dict]:
    return [
        item
        for item in _listing(client, entity_id)["candidates"]
        if str(item.get("field")) == field
    ]


def test_a04_listing_json_has_no_secret_material(client: TestClient, db_conn):
    """候选清单属于用户可见输出：不得带出密钥原文（AGENTS.md 硬约束）。"""
    card_id = _seed_card(db_conn)
    raw = json.dumps(_listing(client, card_id), ensure_ascii=False)
    assert "sk-" not in raw
    assert "api_key" not in raw.lower()
