# -*- coding: utf-8 -*-
"""EntityCardService：实体卡的持久化、关系边、注入文本。

实体卡 = 经验性实体的属性集合（是什么） + 关系（和谁什么关系）。
关系走图边（edges 支持任意关系类型）；属性/别名/摘要存在 entity_cards 表。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from agent.prompts import ENTITY_CARD_INJECT


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class EntityAttribute(BaseModel):
    key: str
    value: str
    confidence: float = 0.8


class EntityRelation(BaseModel):
    target: str
    type: str


class EntityCardCandidate(BaseModel):
    """主模型封块提炼输出的实体卡候选（本地校验契约）。"""
    name: str
    aliases: list[str] = Field(default_factory=list)
    kind: str | None = None
    summary: str = ""
    attributes: list[EntityAttribute] = Field(default_factory=list)
    relations: list[EntityRelation] = Field(default_factory=list)


@dataclass(frozen=True)
class EntityCard:
    id: str
    node_id: str | None
    name: str
    aliases: list[str]
    kind: str | None
    summary: str
    attributes: list[dict]
    state: str
    created_at: str
    updated_at: str


class EntityCardService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -- reads -----------------------------------------------------------

    def _row_to_card(self, row: sqlite3.Row) -> EntityCard:
        return EntityCard(
            id=row["id"],
            node_id=row["node_id"],
            name=row["name"],
            aliases=json.loads(row["aliases"] or "[]"),
            kind=row["kind"],
            summary=row["summary"] or "",
            attributes=json.loads(row["attributes"] or "[]"),
            state=row["state"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get(self, card_id: str) -> EntityCard | None:
        row = self.conn.execute(
            "SELECT * FROM entity_cards WHERE id = ?", (card_id,)
        ).fetchone()
        return self._row_to_card(row) if row else None

    def find_by_name(self, name: str) -> EntityCard | None:
        name = (name or "").strip()
        if not name:
            return None
        row = self.conn.execute(
            "SELECT * FROM entity_cards WHERE name = ? OR "
            "EXISTS (SELECT 1 FROM json_each(aliases) WHERE json_each.value = ?) "
            "ORDER BY updated_at DESC LIMIT 1",
            (name, name),
        ).fetchone()
        return self._row_to_card(row) if row else None

    def list_active(self) -> list[EntityCard]:
        rows = self.conn.execute(
            "SELECT * FROM entity_cards WHERE state = 'active' ORDER BY updated_at DESC"
        ).fetchall()
        return [self._row_to_card(r) for r in rows]

    # -- writes ----------------------------------------------------------

    def _ensure_node(self, name: str) -> str:
        from agent.graph.nodes import NodeService

        nodes = NodeService(self.conn)
        existing = nodes.get_entity_by_name(name)
        if existing is not None:
            return existing.id
        if name == "我":
            node = nodes.get_or_create_user_root()
            return node.id
        return nodes.create_entity(name).id

    def _sync_relations(self, node_id: str, relations: list[EntityRelation]) -> None:
        from agent.graph.edges import EdgeService

        edges = EdgeService(self.conn)
        for rel in relations:
            target = (rel.target or "").strip()
            if not target or (rel.type or "").strip() == "":
                continue
            other = self._ensure_node(target)
            edges.add(node_id, other, rel.type.strip())

    def upsert(self, cand: EntityCardCandidate) -> EntityCard:
        now = _now()
        existing = self.find_by_name(cand.name)
        attrs = [a.model_dump() for a in cand.attributes]
        if existing is not None:
            node_id = existing.node_id or self._ensure_node(cand.name)
            self.conn.execute(
                "UPDATE entity_cards SET node_id = ?, name = ?, aliases = ?, kind = ?, "
                "summary = ?, attributes = ?, updated_at = ? WHERE id = ?",
                (
                    node_id,
                    cand.name,
                    json.dumps(cand.aliases, ensure_ascii=False),
                    cand.kind,
                    cand.summary,
                    json.dumps(attrs, ensure_ascii=False),
                    now,
                    existing.id,
                ),
            )
            card = self.get(existing.id)
            assert card is not None
        else:
            node_id = self._ensure_node(cand.name)
            card_id = _new_id("ec")
            self.conn.execute(
                "INSERT INTO entity_cards (id, node_id, name, aliases, kind, summary, "
                "attributes, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                (
                    card_id,
                    node_id,
                    cand.name,
                    json.dumps(cand.aliases, ensure_ascii=False),
                    cand.kind,
                    cand.summary,
                    json.dumps(attrs, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            card = self.get(card_id)
            assert card is not None
        if cand.relations:
            self._sync_relations(card.node_id or node_id, cand.relations)
        return card

    def revoke(self, card_id: str) -> bool:
        row = self.conn.execute(
            "UPDATE entity_cards SET state = 'archived', updated_at = ? WHERE id = ? AND state = 'active'",
            (_now(), card_id),
        )
        return row.rowcount > 0

    # -- formatting ------------------------------------------------------

    def format_card(self, card: EntityCard) -> str:
        attrs = "；".join(f"{a.get('key')}={a.get('value')}" for a in card.attributes)
        rels = self._format_relations(card)
        return ENTITY_CARD_INJECT.format(
            name=card.name,
            summary=card.summary or "",
            attrs=attrs or "—",
            rels=rels or "—",
        )

    def _format_relations(self, card: EntityCard) -> str:
        if not card.node_id:
            return ""
        rows = self.conn.execute(
            "SELECT e.type, n.name FROM edges e JOIN nodes n ON "
            "n.id = CASE WHEN e.dst = ? THEN e.src ELSE e.dst END "
            "WHERE (e.src = ? OR e.dst = ?) AND e.type NOT IN ('mention')",
            (card.node_id, card.node_id, card.node_id),
        ).fetchall()
        return "；".join(f"{r['type']}→{r['name']}" for r in rows)
