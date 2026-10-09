# -*- coding: utf-8 -*-
"""EntityCardService：实体卡的持久化、关系边、注入文本。

实体卡 = 经验性实体的属性集合（是什么） + 关系（和谁什么关系）。
关系走图边（edges 支持任意关系类型）；属性/别名/摘要存在 entity_cards 表。

M04：区分「用户明确整张修订」与「模型部分候选」
------------------------------------------------

以前 `upsert` 是**整卡替换**：模型这一轮只提到「健康状况」，别名/摘要/其他属性
就一起被本次候选的内容覆盖 —— 未提及 = 删除。真实后果：

* 用户明确改过的属性，被一次迟到的自动提炼改回去；
* 用户删掉的属性/整张卡，被后续普通提炼复活；
* 用户修订与自动提炼并发时，谁后写谁赢（没有来源与版本的概念）。

现在的规则（别名 / 摘要 / 类型 / 属性用**同一套**冲突规则）：

======================  =========================================
`upsert(source="auto")`  模型**部分候选**：按字段合并，未提及≠删除
`upsert(source="user")`  用户/调用方明确给出的值：整卡替换
`revise(...)`            用户明确**整张修订**：给出的字段整段替换，未给字段保持
`set_attribute` 等       用户明确动作 → 该字段标为 user，自动提炼永不覆盖
`revoke`                 用户删除整张卡 → 自动提炼不复活
======================  =========================================

来源与版本存在 `entity_cards.field_meta`（JSON）+ `revision`（单调递增）：

* `fields.<字段>`：`{"source": "user"|"auto"|"system", "revision": n}`；
* `attributes.<键>`：同上（**不塞进 attributes 值里**，否则结构化输出会多出字段）；
* `tombstones.<键>`：用户删除过的属性键 —— 自动提炼不得复活；
* `pending`：被保护而没落的自动候选（可管理：用户可见、可接受、可丢弃）。

迟到结果的核对在**提交时**做（不只是发起请求前）：调用方把发起时的 `revision`
作为 `expected_revision` 传进来，提交时若卡已经变了，自动候选只能**补空**，
不得改动任何已有值；被挡下的值进入 `pending` 候选，人工纠正优先。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from agent.prompts import ENTITY_CARD_INJECT

logger = logging.getLogger(__name__)

SOURCE_USER = "user"
SOURCE_AUTO = "auto"
SOURCE_SYSTEM = "system"

# 降级告警只打一次（每次装配上下文都会新建 Service，不能刷屏）。
_LEGACY_SCHEMA_WARNED = False

# 可管理候选的上限：只留最近这么多条待处理候选，避免 field_meta 无限膨胀。
PENDING_LIMIT = 20

# 待处理候选的原因码（可读、可被管理界面直接展示）。
PENDING_USER_VALUE = "user_value"  # 与用户明确设定的值冲突
PENDING_USER_DELETED = "user_deleted"  # 用户删除过，自动提炼不得复活
PENDING_STALE = "stale_revision"  # 迟到结果：卡在请求期间已被改动
PENDING_CARD_ARCHIVED = "card_archived"  # 用户删了整张卡，自动提炼不复活


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _empty_meta() -> dict:
    return {
        "fields": {},
        "attributes": {},
        "tombstones": {},
        "pending": [],
        "card": {},
    }


def _normalize_meta(raw: object) -> dict:
    """读进来的 field_meta 一律规范成完整形状（旧行/坏 JSON 保守当空）。"""
    meta = _empty_meta()
    if not isinstance(raw, dict):
        return meta
    for key in ("fields", "attributes", "tombstones"):
        section = raw.get(key)
        if isinstance(section, dict):
            meta[key] = {
                str(k): dict(v) if isinstance(v, dict) else {"source": str(v)}
                for k, v in section.items()
            }
    pending = raw.get("pending")
    if isinstance(pending, list):
        meta["pending"] = [dict(item) for item in pending if isinstance(item, dict)]
    card = raw.get("card")
    if isinstance(card, dict):
        meta["card"] = dict(card)
    return meta


def _pending_id(field_name: str, value: object) -> str:
    raw = f"{field_name}\u0000{value}".encode("utf-8")
    return "pc_" + hashlib.sha1(raw).hexdigest()[:10]


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
    # M04：来源与修订版本。旧行读出来是 0 / 空 meta（保守：不改写已有内容）。
    revision: int = 0
    field_meta: dict = dataclass_field(default_factory=dict)


class EntityCardService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        global _LEGACY_SCHEMA_WARNED
        self.conn = conn
        cols = _entity_card_columns(conn)
        # 迁移未落地时降级：字段合并且「未提及≠删除」仍然生效，但没有来源/版本可存，
        # 因此用户纠正的保护在这条路径上无法持久化（只打一次告警，不静默）。
        self._has_meta = {"revision", "field_meta"} <= cols
        if not self._has_meta and not _LEGACY_SCHEMA_WARNED:
            _LEGACY_SCHEMA_WARNED = True
            logger.warning(
                "entity_cards 缺少 revision/field_meta 列：实体修订的来源与版本无法持久化"
                "（等迁移补列；字段合并不受影响）"
            )

    # -- reads -----------------------------------------------------------

    def _row_to_card(self, row: sqlite3.Row) -> EntityCard:
        keys = set(row.keys())
        raw_meta = row["field_meta"] if "field_meta" in keys else None
        try:
            meta = json.loads(raw_meta or "{}")
        except (TypeError, ValueError):
            meta = {}
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
            revision=int(row["revision"]) if "revision" in keys else 0,
            field_meta=_normalize_meta(meta),
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
            "SELECT * FROM entity_cards WHERE state = 'active' AND (name = ? OR "
            "EXISTS (SELECT 1 FROM json_each(aliases) WHERE json_each.value = ?)) "
            "ORDER BY updated_at DESC LIMIT 1",
            (name, name),
        ).fetchone()
        return self._row_to_card(row) if row else None

    def _find_any_by_name(self, name: str) -> EntityCard | None:
        """按 name / alias 找卡，**包括已归档的**（自动提炼不复活用户删掉的卡）。"""
        name = (name or "").strip()
        if not name:
            return None
        row = self.conn.execute(
            "SELECT * FROM entity_cards WHERE (name = ? OR "
            "EXISTS (SELECT 1 FROM json_each(aliases) WHERE json_each.value = ?)) "
            "ORDER BY updated_at DESC LIMIT 1",
            (name, name),
        ).fetchone()
        return self._row_to_card(row) if row else None

    def match_cards(self, message: str) -> list[EntityCard]:
        """消息命中实体的 name 或任一 alias 时返回该卡（交流锚点命中）。"""
        message = (message or "").strip()
        if not message:
            return []
        hits = []
        for card in self.list_active():
            if message.find(card.name) >= 0 or any(
                a and message.find(a) >= 0 for a in card.aliases
            ):
                hits.append(card)
        return hits

    def list_active(self) -> list[EntityCard]:
        rows = self.conn.execute(
            "SELECT * FROM entity_cards WHERE state = 'active' ORDER BY updated_at DESC"
        ).fetchall()
        return [self._row_to_card(r) for r in rows]

    def revision_snapshot(self) -> dict[str, int]:
        """name / alias → 当前 revision。

        派生任务在**发起模型调用之前**取一次；提交时按名字取回作为
        `expected_revision`，从而在提交那一刻再次核对「这张卡在请求期间有没有被改过」。
        """
        snapshot: dict[str, int] = {}
        for card in self.list_active():
            snapshot[card.name] = card.revision
            for alias in card.aliases:
                if alias:
                    snapshot.setdefault(str(alias), card.revision)
        return snapshot

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

    # -- 元数据小工具 -----------------------------------------------------

    @staticmethod
    def _field_source(meta: dict, field_name: str) -> str | None:
        entry = meta.get("fields", {}).get(field_name)
        return str(entry.get("source")) if isinstance(entry, dict) and entry.get("source") else None

    @staticmethod
    def _attribute_source(meta: dict, key: str) -> str | None:
        entry = meta.get("attributes", {}).get(str(key))
        return str(entry.get("source")) if isinstance(entry, dict) and entry.get("source") else None

    @staticmethod
    def _mark_field(meta: dict, field_name: str, source: str, revision: int) -> None:
        meta.setdefault("fields", {})[field_name] = {"source": source, "revision": revision}

    @staticmethod
    def _mark_attribute(meta: dict, key: str, source: str, revision: int) -> None:
        meta.setdefault("attributes", {})[str(key)] = {"source": source, "revision": revision}

    @staticmethod
    def _add_pending(
        meta: dict, field_name: str, value: object, *, reason: str, base_revision: int
    ) -> None:
        """把「被保护而没落」的自动候选记下来：人工纠正优先，但候选不丢。"""
        candidate = {
            "id": _pending_id(field_name, value),
            "field": field_name,
            "value": value,
            "reason": reason,
            "base_revision": int(base_revision),
            "at": _now(),
        }
        pending = [
            item
            for item in meta.get("pending", [])
            if not (item.get("field") == field_name and item.get("value") == value)
        ]
        pending.append(candidate)
        meta["pending"] = pending[-PENDING_LIMIT:]

    def _clear_pending(self, meta: dict, field_name: str) -> None:
        """用户在某个字段上做了明确决定：该字段的待处理候选视为已解决。"""
        meta["pending"] = [
            item
            for item in meta.get("pending", [])
            if not _pending_matches_field(item.get("field"), field_name)
        ]

    def pending_candidates(self, card_id: str) -> list[dict]:
        """可管理的自动候选（被用户明确值/用户删除/迟到结果挡下来的那些）。"""
        card = self.get(card_id)
        return list(card.field_meta.get("pending", [])) if card else []

    def resolve_pending(self, card_id: str, candidate_id: str, *, accept: bool) -> EntityCard | None:
        """处理一条待处理候选：accept=True 采纳（按用户决定写入并标 user）。"""
        card = self.get(card_id)
        if card is None:
            return None
        meta = _normalize_meta(card.field_meta)
        target = next(
            (item for item in meta.get("pending", []) if item.get("id") == candidate_id), None
        )
        if target is None:
            return card
        field_name = str(target.get("field") or "")
        meta["pending"] = [item for item in meta.get("pending", []) if item.get("id") != candidate_id]
        if not accept:
            return self._commit_card(card, meta=meta, values=None)

        revision = card.revision
        if field_name.startswith("attributes."):
            key = field_name.split(".", 1)[1]
            attrs = [dict(a) for a in card.attributes]
            hit = next((a for a in attrs if str(a.get("key")) == key), None)
            if hit is None:
                attrs.append(
                    {"key": key, "value": str(target.get("value") or ""), "confidence": 0.8}
                )
            else:
                hit["value"] = str(target.get("value") or "")
            values = {"attributes": json.dumps(attrs, ensure_ascii=False)}
            meta.setdefault("tombstones", {}).pop(key, None)
            self._mark_attribute(meta, key, SOURCE_USER, revision + 1)
        elif field_name in ("summary", "kind"):
            values = {field_name: str(target.get("value") or "")}
            self._mark_field(meta, field_name, SOURCE_USER, revision + 1)
        elif field_name == "aliases":
            aliases = list(card.aliases)
            value = target.get("value")
            for item in value if isinstance(value, list) else [value]:
                if item and str(item) not in aliases:
                    aliases.append(str(item))
            values = {"aliases": json.dumps(aliases, ensure_ascii=False)}
            self._mark_field(meta, "aliases", SOURCE_USER, revision + 1)
        else:
            return self._commit_card(card, meta=meta, values=None)
        return self._commit_card(card, meta=meta, values=values, mark_user=True)

    # -- 写入原语 ---------------------------------------------------------

    def _commit_card(
        self,
        card: EntityCard,
        *,
        meta: dict | None,
        values: dict | None,
        expected_revision: int | None = None,
        mark_user: bool = False,
    ) -> EntityCard | None:
        """带版本 CAS 的写入：并发（用户纠正 vs 迟到自动结果）时只有一个生效。

        返回写回后的卡；CAS 失败时由调用方（合并循环）重读重算。
        """
        payload = dict(values or {})
        payload["updated_at"] = _now()
        sets = [f"{key} = ?" for key in payload]
        params: list[object] = list(payload.values())
        if self._has_meta:
            revision = card.revision + 1
            if meta is not None:
                meta = _normalize_meta(meta)
                if mark_user:
                    meta.setdefault("card", {})["source"] = SOURCE_USER
            sets.extend(["revision = ?", "field_meta = ?"])
            params.extend([revision, json.dumps(meta or {}, ensure_ascii=False)])
        sql = f"UPDATE entity_cards SET {', '.join(sets)} WHERE id = ?"
        params.append(card.id)
        if self._has_meta and expected_revision is not None:
            sql += " AND revision = ?"
            params.append(int(expected_revision))
        cursor = self.conn.execute(sql, params)
        if int(cursor.rowcount or 0) != 1:
            return None
        return self.get(card.id)

    # -- 写入口 -----------------------------------------------------------

    def upsert(
        self,
        cand: EntityCardCandidate,
        *,
        source: str = SOURCE_AUTO,
        expected_revision: int | None = None,
    ) -> EntityCard | None:
        """写入一张实体卡候选。

        `source=SOURCE_AUTO`（默认）是**模型部分候选**：按字段合并，未提及≠删除，
        用户明确设定过的字段不被覆盖（冲突进 pending 候选）。
        `source=SOURCE_USER` 是调用方明确给出的值（例如用户填写的自述）：整卡替换，
        并把给出的字段标成 user。

        返回写回后的卡；自动提炼遇到「用户已经删掉这张卡」时返回那张已归档的卡
        （不复活、不新建）。
        """
        if source == SOURCE_AUTO:
            return self.apply_auto_candidate(cand, expected_revision=expected_revision)
        return self._replace_from_candidate(cand, source=source)

    def _replace_from_candidate(
        self, cand: EntityCardCandidate, *, source: str
    ) -> EntityCard:
        """整卡替换（用户/调用方明确给出的值），与迁移前行为一致。"""
        now = _now()
        attrs = [a.model_dump() for a in cand.attributes]
        existing = self.find_by_name(cand.name)
        if existing is not None:
            node_id = existing.node_id or self._ensure_node(cand.name)
            meta = _normalize_meta(existing.field_meta)
            revision = existing.revision + 1
            self._mark_field(meta, "name", source, revision)
            self._mark_field(meta, "aliases", source, revision)
            self._mark_field(meta, "summary", source, revision)
            self._mark_field(meta, "kind", source, revision)
            for attr in attrs:
                self._mark_attribute(meta, str(attr.get("key", "")), source, revision)
            meta.setdefault("card", {})["source"] = source
            for key in list(meta.get("tombstones", {})):
                if any(str(a.get("key", "")) == key for a in attrs):
                    meta["tombstones"].pop(key, None)
            for name in ("aliases", "summary", "kind", "attributes"):
                self._clear_pending(meta, name)
            self.conn.execute(
                "UPDATE entity_cards SET node_id = ?, name = ?, aliases = ?, kind = ?, "
                "summary = ?, attributes = ?, updated_at = ?"
                + (", revision = ?, field_meta = ?" if self._has_meta else "")
                + " WHERE id = ?",
                (
                    node_id,
                    cand.name,
                    json.dumps(cand.aliases, ensure_ascii=False),
                    cand.kind,
                    cand.summary,
                    json.dumps(attrs, ensure_ascii=False),
                    now,
                    *(
                        [revision, json.dumps(meta, ensure_ascii=False)]
                        if self._has_meta
                        else []
                    ),
                    existing.id,
                ),
            )
            card = self.get(existing.id)
            assert card is not None
        else:
            node_id = self._ensure_node(cand.name)
            card_id = _new_id("ec")
            meta = _empty_meta()
            meta["card"] = {"source": source, "revision": 1}
            for field_name in ("name", "aliases", "summary", "kind"):
                self._mark_field(meta, field_name, source, 1)
            for attr in attrs:
                self._mark_attribute(meta, str(attr.get("key", "")), source, 1)
            self.conn.execute(
                "INSERT INTO entity_cards (id, node_id, name, aliases, kind, summary, "
                "attributes, state, created_at, updated_at"
                + (", revision, field_meta" if self._has_meta else "")
                + ") VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?"
                + (", ?, ?" if self._has_meta else "")
                + ")",
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
                    *([1, json.dumps(meta, ensure_ascii=False)] if self._has_meta else []),
                ),
            )
            card = self.get(card_id)
            assert card is not None
        if cand.relations:
            self._sync_relations(card.node_id or node_id, cand.relations)
        return card

    def apply_auto_candidate(
        self, cand: EntityCardCandidate, *, expected_revision: int | None = None
    ) -> EntityCard | None:
        """模型部分候选的提交（M04 的主路径）。

        规则：
        * 卡不存在 → 新建（来源 auto）；
        * 卡已被用户归档 → **不复活**，候选记进那张卡的 pending；
        * 存在 → 按字段合并：属性按 key 合并、别名取并集、摘要/类型只填空；
          用户明确设定过的字段、用户删除过的属性键、以及「请求期间卡已被改动」
          （`expected_revision` 不匹配 = 迟到结果）都只记 pending 候选，不覆盖。
        """
        for _attempt in range(3):
            existing = self._find_any_by_name(cand.name)
            if existing is not None and existing.state != "active":
                meta = _normalize_meta(existing.field_meta)
                self._add_pending(
                    meta,
                    "card",
                    cand.name,
                    reason=PENDING_CARD_ARCHIVED,
                    base_revision=existing.revision,
                )
                if self._has_meta:
                    self._commit_card(existing, meta=meta, values=None)
                return existing
            if existing is None:
                return self._replace_from_candidate(cand, source=SOURCE_AUTO)

            stale = (
                expected_revision is not None
                and existing.revision != int(expected_revision)
            )
            meta = _normalize_meta(existing.field_meta)
            base = existing.revision
            revision = base  # 本次提交写回后的版本一律是 base + 1（下面统一用 revision + 1）
            changed = False
            values: dict[str, object] = {}

            # 名字：自动提炼不重命名用户的卡（改名是用户动作 rename()），
            # 新名字进别名，这样按新名字仍然找得到。
            aliases = list(existing.aliases)
            if cand.name and cand.name != existing.name:
                aliases.append(cand.name)
            aliases_field_source = self._field_source(meta, "aliases")
            alias_locked = aliases_field_source == SOURCE_USER or stale
            incoming_aliases = [a for a in aliases if a]
            for alias in cand.aliases:
                text = str(alias or "").strip()
                if text and text not in aliases:
                    incoming_aliases.append(text)
            added_aliases = [a for a in dict.fromkeys(incoming_aliases) if a not in aliases]
            if added_aliases:
                if alias_locked:
                    for alias in added_aliases:
                        self._add_pending(
                            meta,
                            "aliases",
                            alias,
                            reason=PENDING_USER_VALUE if aliases_field_source == SOURCE_USER else PENDING_STALE,
                            base_revision=base,
                        )
                else:
                    aliases.extend(added_aliases)
                    values["aliases"] = json.dumps(aliases, ensure_ascii=False)
                    changed = True
                    self._mark_field(meta, "aliases", SOURCE_AUTO, revision + 1)

            # 摘要：只在原值为空时填；用户设定过 / 迟到结果 → pending。
            if cand.summary:
                summary_source = self._field_source(meta, "summary")
                if not (existing.summary or "").strip():
                    values["summary"] = cand.summary
                    changed = True
                    self._mark_field(meta, "summary", SOURCE_AUTO, revision + 1)
                elif str(existing.summary) != cand.summary:
                    if summary_source == SOURCE_USER or stale:
                        self._add_pending(
                            meta,
                            "summary",
                            cand.summary,
                            reason=PENDING_USER_VALUE if summary_source == SOURCE_USER else PENDING_STALE,
                            base_revision=base,
                        )
                    else:
                        values["summary"] = cand.summary
                        changed = True
                        self._mark_field(meta, "summary", SOURCE_AUTO, revision + 1)

            # 类型：同上（只填空 / 非用户来源才更新）。
            if cand.kind:
                kind_source = self._field_source(meta, "kind")
                if not (existing.kind or "").strip():
                    values["kind"] = cand.kind
                    changed = True
                    self._mark_field(meta, "kind", SOURCE_AUTO, revision + 1)
                elif str(existing.kind) != str(cand.kind):
                    if kind_source == SOURCE_USER or stale:
                        self._add_pending(
                            meta,
                            "kind",
                            cand.kind,
                            reason=PENDING_USER_VALUE if kind_source == SOURCE_USER else PENDING_STALE,
                            base_revision=base,
                        )
                    else:
                        values["kind"] = cand.kind
                        changed = True
                        self._mark_field(meta, "kind", SOURCE_AUTO, revision + 1)

            # 属性：按 key 合并（未提及的保留）。
            attrs = [dict(a) for a in existing.attributes]
            by_key = {str(a.get("key")): a for a in attrs}
            attrs_changed = False
            for item in cand.attributes:
                key = str(item.key or "").strip()
                if not key:
                    continue
                if key in meta.get("tombstones", {}):
                    self._add_pending(
                        meta,
                        f"attributes.{key}",
                        item.value,
                        reason=PENDING_USER_DELETED,
                        base_revision=base,
                    )
                    continue
                hit = by_key.get(key)
                if hit is None:
                    attrs.append(
                        {"key": key, "value": item.value, "confidence": item.confidence}
                    )
                    by_key[key] = attrs[-1]
                    attrs_changed = True
                    self._mark_attribute(meta, key, SOURCE_AUTO, revision + 1)
                    continue
                if str(hit.get("value")) == str(item.value):
                    continue
                attr_source = self._attribute_source(meta, key)
                if attr_source == SOURCE_USER or stale:
                    self._add_pending(
                        meta,
                        f"attributes.{key}",
                        item.value,
                        reason=PENDING_USER_VALUE if attr_source == SOURCE_USER else PENDING_STALE,
                        base_revision=base,
                    )
                    continue
                hit["value"] = item.value
                hit["confidence"] = item.confidence
                attrs_changed = True
                self._mark_attribute(meta, key, SOURCE_AUTO, revision + 1)
            if attrs_changed:
                values["attributes"] = json.dumps(attrs, ensure_ascii=False)
                changed = True

            if changed or meta.get("pending") != _normalize_meta(existing.field_meta).get("pending"):
                written = self._commit_card(
                    existing, meta=meta, values=values, expected_revision=base
                )
                if written is None:
                    continue  # 有人并发改了这张卡：重读重算
                card = written
            else:
                card = existing
            if cand.relations:
                self._sync_relations(card.node_id or self._ensure_node(cand.name), cand.relations)
            return card
        # 连续 3 次都撞上并发写入：这次提交放弃（绝不退回整卡覆盖），
        # 派生任务会带着新内容版本/退避重试，卡片内容保持原样。
        from agent.trace.redact import redact_text

        logger.warning("实体卡自动候选连续版本冲突，本次放弃提交：%s", redact_text(cand.name))
        return self._find_any_by_name(cand.name)

    def revise(
        self,
        card_id: str,
        *,
        attributes: list[dict] | None = None,
        aliases: list[str] | None = None,
        summary: str | None = None,
        kind: str | None = None,
        source: str = SOURCE_USER,
    ) -> EntityCard | None:
        """整体修订（用户明确整张修订）：给出的字段整段替换；None 表示保持原值。

        与自动提炼的区别：这里给出什么就是什么（属性列表整段替换），被去掉的属性
        记成墓碑 —— 后续普通提炼不得把它复活；该字段上待处理的自动候选视为已解决。
        """
        card = self.get(card_id)
        if card is None:
            return None
        meta = _normalize_meta(card.field_meta)
        revision = card.revision + 1
        values: dict[str, object] = {}

        if attributes is not None:
            attrs = [
                {
                    "key": str(a.get("key", "")),
                    "value": str(a.get("value", "")),
                    "confidence": float(a.get("confidence", 0.8)),
                }
                if isinstance(a, dict)
                else {"key": "", "value": ""}
                for a in attributes
            ]
            values["attributes"] = json.dumps(attrs, ensure_ascii=False)
            kept = {str(a.get("key")) for a in attrs}
            for old in card.attributes:
                key = str(old.get("key") or "")
                if key and key not in kept:
                    meta.setdefault("tombstones", {})[key] = {
                        "revision": revision,
                        "at": _now(),
                    }
            for key in list(meta.get("tombstones", {})):
                if key in kept:
                    meta["tombstones"].pop(key, None)
            for attr in attrs:
                self._mark_attribute(meta, str(attr.get("key", "")), source, revision)
            self._clear_pending(meta, "attributes")

        new_aliases = card.aliases if aliases is None else [str(a) for a in aliases]
        if aliases is not None:
            values["aliases"] = json.dumps(new_aliases, ensure_ascii=False)
            self._mark_field(meta, "aliases", source, revision)
            self._clear_pending(meta, "aliases")

        new_summary = card.summary if summary is None else str(summary)
        if summary is not None:
            values["summary"] = new_summary
            self._mark_field(meta, "summary", source, revision)
            self._clear_pending(meta, "summary")

        new_kind = card.kind if kind is None else (str(kind) if kind else None)
        if kind is not None:
            values["kind"] = new_kind
            self._mark_field(meta, "kind", source, revision)
            self._clear_pending(meta, "kind")

        if not values:
            return card
        result = self._commit_card(card, meta=meta, values=values, mark_user=True)
        return result

    def rename(
        self, card_id: str, name: str, *, aliases: list[str] | None = None
    ) -> EntityCard | None:
        """改名（旧名字由调用方放进 aliases）：自我信息改名时必须走这里，
        否则同一个人会留下两张卡。"""
        card = self.get(card_id)
        if card is None:
            return None
        new_aliases = card.aliases if aliases is None else [str(a) for a in aliases]
        meta = _normalize_meta(card.field_meta)
        revision = card.revision + 1
        self._mark_field(meta, "name", SOURCE_USER, revision)
        if aliases is not None:
            self._mark_field(meta, "aliases", SOURCE_USER, revision)
        self.conn.execute(
            "UPDATE entity_cards SET name = ?, aliases = ?, updated_at = ?"
            + (", revision = ?, field_meta = ?" if self._has_meta else "")
            + " WHERE id = ?",
            (
                str(name),
                json.dumps(new_aliases, ensure_ascii=False),
                _now(),
                *([revision, json.dumps(meta, ensure_ascii=False)] if self._has_meta else []),
                card_id,
            ),
        )
        return self.get(card_id)

    def set_attribute(
        self, card_id: str, key: str, value: str, *, source: str = SOURCE_USER
    ) -> EntityCard | None:
        """设置属性（默认是用户明确动作）：该字段标 user，自动提炼不再覆盖它。"""
        card = self.get(card_id)
        if card is None:
            return None
        attrs = [dict(a) for a in card.attributes]
        hit = next((a for a in attrs if a.get("key") == key), None)
        if hit is not None:
            hit["value"] = value
        else:
            attrs.append({"key": key, "value": value, "confidence": 0.8})
        meta = _normalize_meta(card.field_meta)
        meta.setdefault("tombstones", {}).pop(str(key), None)
        self._mark_attribute(meta, str(key), source, card.revision + 1)
        self._clear_pending(meta, f"attributes.{key}")
        return self._commit_card(
            card,
            meta=meta,
            values={"attributes": json.dumps(attrs, ensure_ascii=False)},
            mark_user=True,
        )

    def remove_attribute(
        self, card_id: str, key: str, *, source: str = SOURCE_USER
    ) -> EntityCard | None:
        """删除属性（默认用户明确动作）：记墓碑，后续普通提炼不得复活。"""
        card = self.get(card_id)
        if card is None:
            return None
        attrs = [dict(a) for a in card.attributes if a.get("key") != key]
        meta = _normalize_meta(card.field_meta)
        if source == SOURCE_USER:
            meta.setdefault("tombstones", {})[str(key)] = {
                "revision": card.revision + 1,
                "at": _now(),
            }
        meta.get("attributes", {}).pop(str(key), None)
        self._clear_pending(meta, f"attributes.{key}")
        return self._commit_card(
            card,
            meta=meta,
            values={"attributes": json.dumps(attrs, ensure_ascii=False)},
            mark_user=True,
        )

    def add_relation(self, card_id: str, target: str, rel_type: str) -> EntityCard | None:
        card = self.get(card_id)
        if card is None or not card.node_id:
            return None
        self._sync_relations(card.node_id, [EntityRelation(target=target, type=rel_type)])
        return self.get(card_id)

    def remove_relation(self, card_id: str, target: str, rel_type: str) -> EntityCard | None:
        card = self.get(card_id)
        if card is None or not card.node_id:
            return None
        from agent.graph.nodes import NodeService

        other = NodeService(self.conn).get_entity_by_name(target)
        if other is not None:
            self.conn.execute(
                "DELETE FROM edges WHERE type = ? AND "
                "((src = ? AND dst = ?) OR (src = ? AND dst = ?))",
                (rel_type, card.node_id, other.id, other.id, card.node_id),
            )
        return self.get(card_id)

    def revoke(self, card_id: str, *, source: str = SOURCE_USER) -> bool:
        """归档整张卡（用户删除）。自动提炼不会复活它（候选进 pending）。"""
        card = self.get(card_id)
        if card is None:
            return False
        if self._has_meta:
            meta = _normalize_meta(card.field_meta)
            meta["card"] = {"source": source, "revision": card.revision + 1}
            cursor = self.conn.execute(
                "UPDATE entity_cards SET state = 'archived', updated_at = ?, revision = ?, "
                "field_meta = ? WHERE id = ? AND state = 'active'",
                (_now(), card.revision + 1, json.dumps(meta, ensure_ascii=False), card_id),
            )
        else:
            cursor = self.conn.execute(
                "UPDATE entity_cards SET state = 'archived', updated_at = ? "
                "WHERE id = ? AND state = 'active'",
                (_now(), card_id),
            )
        return int(cursor.rowcount or 0) > 0

    # -- formatting ------------------------------------------------------

    def topics_of(self, card: EntityCard) -> list[str]:
        """这张卡挂在哪些话题上（mention 边 topic→entity）。"""
        if not card.node_id:
            return []
        rows = self.conn.execute(
            "SELECT src FROM edges WHERE dst = ? AND type = 'mention'", (card.node_id,)
        ).fetchall()
        return [str(r["src"]) for r in rows]

    def format_card(self, card: EntityCard, *, note: str | None = None) -> str:
        """卡片文本。`note` 用来标注来源与适用范围（见阶段 3 的路径隔离）。"""
        attrs = "；".join(f"{a.get('key')}={a.get('value')}" for a in card.attributes)
        rels = self._format_relations(card)
        text = ENTITY_CARD_INJECT.format(
            name=card.name,
            summary=card.summary or "",
            attrs=attrs or "—",
            rels=rels or "—",
        )
        return f"{text}\n{note}" if note else text

    def _relation_rows(self, card: EntityCard) -> list[sqlite3.Row]:
        """实体卡的关系边行（排除 mention），_format_relations 与 to_dict 共用。"""
        if not card.node_id:
            return []
        return self.conn.execute(
            "SELECT e.type, n.name FROM edges e JOIN nodes n ON "
            "n.id = CASE WHEN e.dst = ? THEN e.src ELSE e.dst END "
            "WHERE (e.src = ? OR e.dst = ?) AND e.type NOT IN ('mention')",
            (card.node_id, card.node_id, card.node_id),
        ).fetchall()

    def _format_relations(self, card: EntityCard) -> str:
        rows = self._relation_rows(card)
        return "；".join(f"{r['type']}→{r['name']}" for r in rows)

    def to_dict(self, card: EntityCard) -> dict:
        """结构化输出：管理页/API 用（含属性、关系、node_id）。"""
        relations = [{"type": r["type"], "target": r["name"]} for r in self._relation_rows(card)]
        return {
            "id": card.id,
            "node_id": card.node_id,
            "name": card.name,
            "aliases": card.aliases,
            "kind": card.kind,
            "summary": card.summary,
            "attributes": card.attributes,
            "relations": relations,
            "state": card.state,
            "created_at": card.created_at,
            "updated_at": card.updated_at,
            # M04：修订版本与「可管理候选」（管理界面据此展示/采纳/丢弃）。
            "revision": card.revision,
            "field_sources": dict(card.field_meta.get("fields", {})),
            "attribute_sources": dict(card.field_meta.get("attributes", {})),
            "tombstones": dict(card.field_meta.get("tombstones", {})),
            "pending_candidates": list(card.field_meta.get("pending", [])),
        }


def _pending_matches_field(pending_field: object, field_name: str) -> bool:
    """候选的字段名是否属于这次用户明确的字段（attributes.* 用前缀匹配）。"""
    pending = str(pending_field or "")
    if pending == field_name:
        return True
    return pending.startswith("attributes.") and field_name == "attributes"


def _entity_card_columns(conn: sqlite3.Connection) -> set[str]:
    try:
        return {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(entity_cards)").fetchall()
        }
    except sqlite3.Error:
        return set()
