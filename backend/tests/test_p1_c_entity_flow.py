"""Phase 1 C：create_entity / 后台实体决策 / 新字段贯穿 / correct_entity CAS。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from agent.adapters.base import ChatMessage, Completion
from agent.api.server import create_app
from agent.config import Settings
from agent.entities.cards import (
    DECISION_CREATE,
    DECISION_OBSERVE,
    DECISION_UPDATE,
    EntityCardCandidate,
    EntityCardService,
)
from agent.entities.extract import (
    extract_entity_cards_outcome,
    prepare_candidates_for_commit,
)
from agent.knowledge.evidence import EvidenceCatalog, EvidenceEntry
from agent.tools.entity_create import CreateEntityTool
from agent.tools.entity_tools import CorrectEntityTool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _catalog() -> EvidenceCatalog:
    return EvidenceCatalog(
        [
            EvidenceEntry(id="msg_user", kind="message", label="用户消息", role="user"),
            EvidenceEntry(id="tr_1", kind="tool_result", label="工具"),
        ]
    )


def _create_tool(conn: sqlite3.Connection) -> CreateEntityTool:
    catalog = _catalog()
    return CreateEntityTool(conn, evidence_provider=lambda: catalog)


def _create_args(**overrides):
    payload = {
        "name": "我家的鹅",
        "identity": "用户家里养的那只白鹅，最近嘴巴红肿",
        "creation_reason": "与用户的持续关系，后续健康记录会反复用到",
        "evidence_ids": ["msg_user"],
        "kind": "动物",
        "summary": "用户养的鹅",
        "attributes": [{"key": "健康状况", "value": "红肿"}],
        "relations": [{"target": "用户", "type": "属于"}],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# create_entity
# ---------------------------------------------------------------------------


def test_create_entity_returns_real_id_and_new_fields_are_readable(db_conn: sqlite3.Connection):
    tool = _create_tool(db_conn)
    result = asyncio.run(tool.run(**_create_args()))
    assert result.ok, result.error
    assert result.status == "completed" and result.code == "created"
    entity_id = result.data["entity_id"]
    assert result.data["revision"] >= 1
    card = EntityCardService(db_conn).get(entity_id)
    assert card is not None and card.name == "我家的鹅"
    payload = EntityCardService(db_conn).to_dict(card)
    assert payload["identity"].startswith("用户家里养的那只白鹅")
    assert payload["creation_reason"]
    assert payload["evidence_ids"] == ["msg_user"]
    # 只有名称、身份不确定的关系不写同名实体，留成候选（旧直接 upsert 语义不受影响）。
    assert payload["pending_relations"]


def test_create_entity_is_idempotent_for_same_identity(db_conn: sqlite3.Connection):
    tool = _create_tool(db_conn)
    first = asyncio.run(tool.run(**_create_args()))
    second = asyncio.run(tool.run(**_create_args()))
    assert second.ok and second.code == "matched_existing"
    assert second.data["entity_id"] == first.data["entity_id"]
    assert db_conn.execute("SELECT COUNT(*) c FROM entity_cards").fetchone()["c"] == 1


def test_same_name_different_identity_is_not_merged(db_conn: sqlite3.Connection):
    tool = _create_tool(db_conn)
    first = asyncio.run(tool.run(**_create_args()))
    other = asyncio.run(
        tool.run(
            **_create_args(
                identity="另一只同名但完全不同来源的鹅",
                creation_reason="同名不同对象",
                attributes=[],
                relations=[],
            )
        )
    )
    assert not other.ok and other.status == "conflict"
    assert other.code == "identity_conflict"
    assert other.data["entity_id"] == first.data["entity_id"]
    card = EntityCardService(db_conn).get(first.data["entity_id"])
    assert EntityCardService(db_conn).to_dict(card)["identity"].startswith("用户家里养的那只白鹅")
    assert db_conn.execute("SELECT COUNT(*) c FROM entity_cards").fetchone()["c"] == 1


def test_create_entity_evidence_must_exist(db_conn: sqlite3.Connection):
    tool = _create_tool(db_conn)
    result = asyncio.run(tool.run(**_create_args(evidence_ids=["ghost"])))
    assert not result.ok and result.code == "evidence_not_found"
    assert db_conn.execute("SELECT COUNT(*) c FROM entity_cards").fetchone()["c"] == 0


def test_relation_by_real_target_id_creates_edge(db_conn: sqlite3.Connection):
    svc = EntityCardService(db_conn)
    other = svc.upsert(EntityCardCandidate(name="用户"))
    tool = _create_tool(db_conn)
    result = asyncio.run(
        tool.run(
            **_create_args(
                relations=[
                    {"target_entity_id": other.node_id, "target": "用户", "type": "属于"}
                ]
            )
        )
    )
    assert result.ok
    card = svc.get(result.data["entity_id"])
    edge = db_conn.execute(
        "SELECT type FROM edges WHERE src = ? AND dst = ?",
        (card.node_id, other.node_id),
    ).fetchone()
    assert edge is not None and edge["type"] == "属于"


# ---------------------------------------------------------------------------
# 后台实体输出：决策 / 身份 / 证据贯穿解析 -> 修正 -> 存储 -> 重建 -> API
# ---------------------------------------------------------------------------


class _FakeAdapter:
    mode = "native"
    model = "fake-p1-c"

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def complete(self, messages, tools, **kwargs) -> Completion:
        assert messages[0].role == "system"
        assert '"entities"' in messages[0].content
        data = json.loads(messages[-1].content)
        assert "evidence_catalog" in data
        return Completion(
            message=ChatMessage(
                role="assistant", content=json.dumps(self.payload, ensure_ascii=False)
            )
        )


def _extract(payload: dict, catalog: EvidenceCatalog) -> list:
    outcome = asyncio.run(
        extract_entity_cards_outcome(
            _FakeAdapter(payload),
            [{"role": "user", "content": "我家的鹅嘴巴红肿"}],
            evidence_catalog=catalog,
        )
    )
    assert outcome.error is None, outcome.error
    return outcome.value or []


def test_new_entity_fields_survive_extraction_and_storage(
    db_conn: sqlite3.Connection, tmp_path
):
    catalog = _catalog()
    cards = _extract(
        {
            "entities": [
                {
                    "entity_id": None,
                    "name": "我家的鹅",
                    "identity": "用户家里那只白鹅",
                    "aliases": ["鹅"],
                    "kind": "动物",
                    "summary": "最近嘴巴红肿",
                    "decision": "create",
                    "reason": "持续健康记录需要",
                    "evidence_ids": ["msg_user"],
                    "attributes": [
                        {"key": "健康状况", "value": "红肿", "evidence_ids": ["msg_user"]}
                    ],
                    "relations": [],
                }
            ]
        },
        catalog,
    )
    assert cards[0].decision == "create"
    assert cards[0].identity == "用户家里那只白鹅"
    assert cards[0].evidence_ids == ["msg_user"]
    ready, notes = prepare_candidates_for_commit(cards, evidence_catalog=catalog, conn=db_conn)
    assert ready and not notes
    card = EntityCardService(db_conn).upsert(ready[0], source="auto")
    payload = EntityCardService(db_conn).to_dict(card)
    assert payload["identity"] == "用户家里那只白鹅"
    assert payload["creation_reason"] == "持续健康记录需要"
    assert payload["evidence_ids"] == ["msg_user"]
    assert payload["attributes"][0]["evidence_ids"] == ["msg_user"]
    # API 重建：/api/entities 读回同样的新字段。
    app = create_app(Settings(data_dir=tmp_path), db_conn)
    with TestClient(app) as client:
        rows = client.get("/api/entities").json()["entities"]
    row = next(r for r in rows if r["id"] == card.id)
    assert row["identity"] == "用户家里那只白鹅"
    assert row["evidence_ids"] == ["msg_user"]


def test_decision_update_uses_real_id_and_protects_user_values(db_conn: sqlite3.Connection):
    svc = EntityCardService(db_conn)
    card = svc.upsert(EntityCardCandidate(name="我家的鹅"))
    svc.set_attribute(card.id, "健康状况", "用户说的已康复")  # 用户明确值
    card = svc.get(card.id)  # 用户动作推进了版本：提交时按最新版本核对
    catalog = _catalog()
    cards = _extract(
        {
            "entities": [
                {
                    "entity_id": card.id,
                    "name": "我家的鹅",
                    "identity": "用户家里那只白鹅",
                    "decision": "update",
                    "summary": "模型补充的摘要",
                    "evidence_ids": ["msg_user"],
                    "attributes": [
                        {"key": "健康状况", "value": "红肿", "evidence_ids": ["msg_user"]}
                    ],
                }
            ]
        },
        catalog,
    )
    ready, _ = prepare_candidates_for_commit(cards, evidence_catalog=catalog, conn=db_conn)
    assert len(ready) == 1 and ready[0].decision == DECISION_UPDATE
    updated = svc.upsert(ready[0], source="auto", expected_revision=card.revision)
    assert updated.id == card.id and updated.revision > card.revision
    payload = svc.to_dict(updated)
    assert payload["summary"] == "模型补充的摘要"
    assert payload["identity"] == "用户家里那只白鹅"
    # 用户明确设过的属性不被覆盖，冲突进待处理候选。
    values = {a["key"]: a["value"] for a in updated.attributes}
    assert values["健康状况"] == "用户说的已康复"
    assert any(p["field"] == "attributes.健康状况" for p in svc.pending_candidates(card.id))


def test_decision_observe_keeps_candidate_without_creating_entity(db_conn: sqlite3.Connection):
    svc = EntityCardService(db_conn)
    existing = svc.upsert(EntityCardCandidate(name="我家的鹅"))
    catalog = _catalog()
    cards = _extract(
        {
            "entities": [
                {
                    "entity_id": None,
                    "name": "我家的鹅",
                    "identity": "可能是用户提到的那只鹅",
                    "decision": "observe",
                    "evidence_ids": ["msg_user"],
                    "attributes": [],
                    "relations": [],
                }
            ]
        },
        catalog,
    )
    ready, _ = prepare_candidates_for_commit(cards, evidence_catalog=catalog, conn=db_conn)
    assert ready == []
    pending = svc.pending_candidates(existing.id)
    assert any(p["reason"] == "observed_candidate" for p in pending), pending
    assert db_conn.execute("SELECT COUNT(*) c FROM entity_cards").fetchone()["c"] == 1


def test_invalid_decisions_and_evidence_are_dropped(db_conn: sqlite3.Connection):
    catalog = _catalog()
    cards = _extract(
        {
            "entities": [
                {
                    "entity_id": "ent_should_be_empty",
                    "name": "带ID的create",
                    "identity": "身份",
                    "decision": "create",
                    "evidence_ids": ["msg_user"],
                    "attributes": [],
                    "relations": [],
                },
                {
                    "entity_id": None,
                    "name": "证据丢失的候选",
                    "identity": "身份",
                    "decision": "create",
                    "evidence_ids": ["no_such_id"],
                    "attributes": [],
                    "relations": [],
                },
                {
                    "entity_id": None,
                    "name": "正常候选",
                    "identity": "身份明确",
                    "decision": "create",
                    "evidence_ids": ["msg_user"],
                    "attributes": [],
                    "relations": [],
                },
            ]
        },
        catalog,
    )
    # 坏证据与「create 带 entity_id」都在解析层被丢弃（可修正层逐条校验），
    # 好候选照常通过；不是整批失败。
    assert [c.name for c in cards] == ["正常候选"]
    ready, notes = prepare_candidates_for_commit(cards, evidence_catalog=catalog, conn=db_conn)
    assert [c.name for c in ready] == ["正常候选"]
    assert not notes


# ---------------------------------------------------------------------------
# correct_entity：稳定 ID + revision CAS + 身份歧义不猜
# ---------------------------------------------------------------------------


def test_correct_entity_uses_entity_id_and_revision_cas(db_conn: sqlite3.Connection):
    svc = EntityCardService(db_conn)
    card = svc.upsert(EntityCardCandidate(name="我家的鹅"))
    tool = CorrectEntityTool(db_conn)
    ok = asyncio.run(
        tool.run(
            entity_id=card.id,
            expected_revision=card.revision,
            attribute_key="健康状况",
            attribute_value="已康复",
        )
    )
    assert ok.ok and ok.status == "completed"
    assert ok.data["entity_id"] == card.id
    assert ok.data["revision"] == card.revision + 1
    stale = asyncio.run(
        tool.run(
            entity_id=card.id,
            expected_revision=card.revision,
            attribute_key="健康状况",
            attribute_value="又肿了",
        )
    )
    assert not stale.ok and stale.status == "conflict"
    assert stale.code == "revision_conflict"
    assert stale.data["current_revision"] == card.revision + 1


def test_correct_entity_does_not_guess_when_identity_is_ambiguous(
    db_conn: sqlite3.Connection,
):
    now = _now()
    for card_id in ("ec_dup1", "ec_dup2"):
        db_conn.execute(
            "INSERT INTO entity_cards (id, node_id, name, aliases, kind, summary, "
            "attributes, state, created_at, updated_at) VALUES (?, NULL, '同名对象', "
            "'[]', NULL, '', '[]', 'active', ?, ?)",
            (card_id, now, now),
        )
    db_conn.commit()
    tool = CorrectEntityTool(db_conn)
    result = asyncio.run(tool.run(entity="同名对象", attribute_key="状态", attribute_value="新值"))
    assert not result.ok
    assert result.status == "awaiting_input"
    assert result.code == "ambiguous_identity"
    assert {c["entity_id"] for c in result.data["candidates"]} == {"ec_dup1", "ec_dup2"}
