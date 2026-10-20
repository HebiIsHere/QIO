# -*- coding: utf-8 -*-
"""实体卡封块提炼（Phase 1 C 升级：决策 + 身份 + 证据）。

冻结契约（docs/qio-refactor-contract.md §6.4）：

* 固定文案只来自 agent.prompts：PROMPTS["ENTITY_EXTRACTION_SYSTEM"] +
  structured_task_system（结构化总则 + 任务规则 + 程序生成的 schema + limits）；
* 输出结构：
  {"entities": [{"entity_id": null, "name": "...", "identity": "...", "aliases": [],
    "kind": null, "summary": null, "decision": "observe", "reason": "...",
    "evidence_ids": [], "attributes": [{"key": "...", "value": "...",
    "evidence_ids": []}], "relations": [{"target": "...", "target_entity_id": null,
    "type": "...", "evidence_ids": []}]}]}
* decision 枚举：create / update / observe / identity_uncertain；
  create 必须空 entity_id，update 必须有真实已有 ID；
  observe / identity_uncertain 不自动创建；
* 证据按输入证据目录校验（每条候选的 evidence_ids 必须真实存在）；
* 新增字段必须贯穿解析 -> 修正 -> 存储 -> 重建 -> API -> 前端，不被旧可修正层丢弃。

失败仍按既有隔离语义：实体提炼失败不影响封块主流程，但原因与修正留痕要可读。
"""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from agent.adapters.base import BaseAdapter, ChatMessage
from agent.entities.cards import (
    DECISION_CREATE,
    DECISION_IDENTITY_UNCERTAIN,
    DECISION_OBSERVE,
    DECISION_UPDATE,
    ENTITY_DECISIONS,
    MAX_CREATION_REASON,
    MAX_EVIDENCE_IDS,
    MAX_EVIDENCE_ID_LENGTH,
    MAX_IDENTITY,
    EntityAttribute,
    EntityCardCandidate,
    EntityRelation,
)
from agent.knowledge.evidence import EvidenceCatalog, coerce_catalog
from agent.memory.model_output import (
    NOTE_DROPPED_INVALID_ITEM,
    ObjectListField,
    Outcome,
    RepairNote,
    TextField,
    TextListField,
    parse_json_object,
    repair_output,
)
from agent.prompts import structured_task_system
from agent.tools.contracts import json_schema_for

# 实体卡字段的硬上限。同一组数字既写进给模型的要求，也用在本地契约兜底。
MAX_ENTITY_CARDS = 20
MAX_CARD_NAME = 80
MAX_CARD_KIND = 40
MAX_CARD_SUMMARY = 500
MAX_ALIASES = 10
MAX_ATTRIBUTES = 20
MAX_ATTRIBUTE_KEY = 40
MAX_ATTRIBUTE_VALUE = 200
MAX_RELATIONS = 20
MAX_RELATION_TARGET = 80
MAX_RELATION_TYPE = 40


class EntityExtractionAttribute(BaseModel):
    key: str = Field(min_length=1, max_length=MAX_ATTRIBUTE_KEY)
    value: str = Field(max_length=MAX_ATTRIBUTE_VALUE)
    evidence_ids: list[str] = Field(default_factory=list, max_length=MAX_EVIDENCE_IDS)


class EntityExtractionRelation(BaseModel):
    target: str = Field(default="", max_length=MAX_RELATION_TARGET)
    type: str = Field(min_length=1, max_length=MAX_RELATION_TYPE)
    target_entity_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list, max_length=MAX_EVIDENCE_IDS)


class EntityExtractionItem(BaseModel):
    """一条实体提炼结果（决策 + 身份 + 证据 + 属性 / 关系）。"""

    entity_id: str | None = None
    name: str = Field(min_length=1, max_length=MAX_CARD_NAME)
    identity: str | None = Field(default=None, max_length=MAX_IDENTITY)
    aliases: list[str] = Field(default_factory=list, max_length=MAX_ALIASES)
    kind: str | None = Field(default=None, max_length=MAX_CARD_KIND)
    summary: str | None = Field(default=None, max_length=MAX_CARD_SUMMARY)
    decision: Literal[
        "create", "update", "observe", "identity_uncertain"
    ] = DECISION_CREATE
    reason: str | None = Field(default=None, max_length=MAX_CREATION_REASON)
    evidence_ids: list[str] = Field(default_factory=list, max_length=MAX_EVIDENCE_IDS)
    attributes: list[EntityExtractionAttribute] = Field(
        default_factory=list, max_length=MAX_ATTRIBUTES
    )
    relations: list[EntityExtractionRelation] = Field(
        default_factory=list, max_length=MAX_RELATIONS
    )


class EntityExtraction(BaseModel):
    entities: list[EntityExtractionItem] = Field(
        default_factory=list, max_length=MAX_ENTITY_CARDS
    )


def entity_extraction_schema() -> dict[str, Any]:
    """下发 / 本地校验共用的唯一 JSON Schema。"""
    return json_schema_for(EntityExtraction)


def entity_extraction_limits() -> dict[str, Any]:
    return {
        "max_entities": MAX_ENTITY_CARDS,
        "max_name_chars": MAX_CARD_NAME,
        "max_identity_chars": MAX_IDENTITY,
        "max_summary_chars": MAX_CARD_SUMMARY,
        "max_aliases": MAX_ALIASES,
        "max_attributes": MAX_ATTRIBUTES,
        "max_relations": MAX_RELATIONS,
        "max_evidence_ids": MAX_EVIDENCE_IDS,
        "decision_values": list(ENTITY_DECISIONS),
    }


def entity_task_system(*, repair: bool = False) -> str:
    return structured_task_system(
        "ENTITY_EXTRACTION_SYSTEM",
        schema=entity_extraction_schema(),
        limits=entity_extraction_limits(),
        repair=repair,
    )


ENTITY_CARD_LIMITS_NOTE = (
    "上限（超出的部分会在本地被丢弃，请自己先收敛）："
    f"最多 {MAX_ENTITY_CARDS} 张卡；name <= {MAX_CARD_NAME} 字；"
    f"identity <= {MAX_IDENTITY} 字；summary <= {MAX_CARD_SUMMARY} 字；"
    f"aliases <= {MAX_ALIASES} 个；attributes <= {MAX_ATTRIBUTES} 条；"
    f"relations <= {MAX_RELATIONS} 条；不要重复条目，不要空字符串。"
)


def _item_invalid_reason(
    item: dict[str, Any], catalog: EvidenceCatalog | None
) -> str | None:
    """单条领域校验：坏掉只丢这一条（可修正层记 note），不整批失败。"""
    name = str(item.get("name") or "").strip()
    if not name:
        return "name 不能为空"
    decision = str(item.get("decision") or DECISION_CREATE)
    if decision not in ENTITY_DECISIONS:
        return f"decision 不在契约内（应为 {'|'.join(ENTITY_DECISIONS)}）"
    entity_id = str(item.get("entity_id") or "").strip()
    if decision == DECISION_CREATE and entity_id:
        return "create 不允许带 entity_id（create 对应新对象）"
    if decision == DECISION_UPDATE and not entity_id:
        return "update 必须带真实已有 entity_id"
    if catalog is None:
        return None
    evidence_ids = item.get("evidence_ids")
    if not isinstance(evidence_ids, list):
        evidence_ids = []
    _, missing = catalog.validate_evidence_ids(evidence_ids)
    if missing:
        return "evidence_ids 不在输入证据目录里：" + ", ".join(missing[:3])
    nested_groups = (
        ("attributes", item.get("attributes")),
        ("relations", item.get("relations")),
    )
    for label, rows in nested_groups:
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            _, nested_missing = catalog.validate_evidence_ids(
                row.get("evidence_ids") or []
            )
            if nested_missing:
                return (
                    f"{label}[{index}].evidence_ids 不在输入证据目录里："
                    + ", ".join(nested_missing[:3])
                )
    return None


def _entity_fields(catalog: EvidenceCatalog | None) -> tuple[Any, ...]:
    def _invalid(item: dict[str, Any]) -> str | None:
        return _item_invalid_reason(item, catalog)

    return (
        ObjectListField(
            "entities",
            MAX_ENTITY_CARDS,
            fields=(
                TextField("name", MAX_CARD_NAME),
                TextField("entity_id", 80, required=False, allow_null_literal=True),
                TextField("identity", MAX_IDENTITY, required=False),
                TextField("kind", MAX_CARD_KIND, required=False),
                TextField("summary", MAX_CARD_SUMMARY, required=False),
                TextField("decision", 32, required=False),
                TextField("reason", MAX_CREATION_REASON, required=False),
                TextListField(
                    "aliases",
                    MAX_ALIASES,
                    max_item_length=MAX_CARD_NAME,
                    strict=False,
                ),
                TextListField(
                    "evidence_ids",
                    MAX_EVIDENCE_IDS,
                    max_item_length=MAX_EVIDENCE_ID_LENGTH,
                    required=False,
                    strict=False,
                ),
                ObjectListField(
                    "attributes",
                    MAX_ATTRIBUTES,
                    fields=(
                        TextField("key", MAX_ATTRIBUTE_KEY),
                        TextField("value", MAX_ATTRIBUTE_VALUE),
                        TextListField(
                            "evidence_ids",
                            MAX_EVIDENCE_IDS,
                            max_item_length=MAX_EVIDENCE_ID_LENGTH,
                            required=False,
                            strict=False,
                        ),
                    ),
                    dedupe=True,
                    strict=False,
                ),
                ObjectListField(
                    "relations",
                    MAX_RELATIONS,
                    fields=(
                        TextField("target", MAX_RELATION_TARGET, required=False),
                        TextField("type", MAX_RELATION_TYPE),
                        TextField(
                            "target_entity_id",
                            80,
                            required=False,
                            allow_null_literal=True,
                        ),
                        TextListField(
                            "evidence_ids",
                            MAX_EVIDENCE_IDS,
                            max_item_length=MAX_EVIDENCE_ID_LENGTH,
                            required=False,
                            strict=False,
                        ),
                    ),
                    dedupe=True,
                    strict=False,
                ),
            ),
            dedupe=True,
            item_validator=_invalid,
        ),
    )


#: 兼容别名：旧调用方可能引用该常量（新路径统一用 _entity_fields(catalog)）。
ENTITY_CARD_FIELDS = _entity_fields(None)


def validate_entity_extraction(
    text: str, *, evidence_catalog: Any | None = None
) -> Outcome[EntityExtraction]:
    """解析 + 修正 + 校验实体提炼输出。"""
    payload, error = parse_json_object(text)
    if error is not None:
        return Outcome(None, f"实体卡输出{error}")
    catalog = coerce_catalog(evidence_catalog) if evidence_catalog is not None else None
    repaired = repair_output(
        payload, _entity_fields(catalog), contract="实体卡输出"
    )
    if not repaired.ok:
        return Outcome(None, repaired.error, repaired.notes)
    try:
        data = repaired.data or {}
        return Outcome(EntityExtraction(**data), None, repaired.notes)
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError 也走可读原因
        return Outcome(None, f"schema violation: {exc}", repaired.notes)


def _to_candidate(item: EntityExtractionItem) -> EntityCardCandidate:
    return EntityCardCandidate(
        name=item.name,
        aliases=list(item.aliases),
        kind=item.kind,
        summary=item.summary or "",
        attributes=[
            EntityAttribute(
                key=a.key,
                value=a.value,
                evidence_ids=list(a.evidence_ids),
            )
            for a in item.attributes
        ],
        relations=[
            EntityRelation(
                target=r.target,
                type=r.type,
                target_entity_id=r.target_entity_id,
                evidence_ids=list(r.evidence_ids),
            )
            for r in item.relations
        ],
        entity_id=item.entity_id,
        identity=item.identity,
        creation_reason=item.reason,
        decision=item.decision,
        evidence_ids=list(item.evidence_ids),
    )


async def extract_entity_cards_outcome(
    adapter: BaseAdapter,
    messages: list[dict],
    *,
    evidence_catalog: Any | None = None,
    temperature: float = 0.2,
) -> Outcome[list[EntityCardCandidate]]:
    """提炼实体卡候选：返回（候选 / 可读失败原因 / 修正留痕）。"""
    catalog = coerce_catalog(evidence_catalog)
    data = {
        "messages": [dict(m) for m in messages],
        "evidence_catalog": catalog.prompt_entries(),
    }
    if not any(str(m.get("content") or "").strip() for m in messages):
        return Outcome([], None)  # 没有可提炼的原文，不是失败
    system = entity_task_system()
    prompt_messages = [
        ChatMessage(role="system", content=system),
        ChatMessage(
            role="user",
            content=json.dumps(data, ensure_ascii=False, sort_keys=True),
        ),
    ]
    # 用量归因由 adapter 在每次实际请求上统一完成（含内部重试的每次响应）。
    from agent.credentials.policy import BudgetExhausted

    try:
        completion = await adapter.complete(prompt_messages, [])
    except BudgetExhausted as exc:
        return Outcome(None, str(exc))
    except Exception as exc:  # noqa: BLE001 - 模型失败按可读原因降级
        return Outcome(None, f"model call failed: {exc}")
    outcome = validate_entity_extraction(
        completion.message.content or "", evidence_catalog=catalog
    )
    if outcome.value is None:
        return outcome
    cards = [_to_candidate(item) for item in outcome.value.entities]
    return Outcome(cards, None, outcome.notes)


async def extract_entity_cards(
    adapter: BaseAdapter,
    messages: list[dict],
    *,
    evidence_catalog: Any | None = None,
    temperature: float = 0.2,
) -> list[EntityCardCandidate]:
    """兼容入口：只要候选列表，任何失败都返回 []（不影响封块主流程）。"""
    outcome = await extract_entity_cards_outcome(
        adapter, messages, evidence_catalog=evidence_catalog, temperature=temperature
    )
    return outcome.value or []


def _merge_candidates(
    candidates: list[EntityCardCandidate],
) -> tuple[list[EntityCardCandidate], int, int]:
    """提交前收敛：丢掉空名，合并同名候选（决策取更强的 create/update）。"""
    cleaned: list[EntityCardCandidate] = []
    by_name: dict[str, EntityCardCandidate] = {}
    merged = 0
    dropped = 0
    rank = {DECISION_CREATE: 3, DECISION_UPDATE: 2, DECISION_IDENTITY_UNCERTAIN: 1, DECISION_OBSERVE: 0}
    for cand in candidates:
        name = str(cand.name or "").strip()
        if not name:
            dropped += 1
            continue
        if name not in by_name:
            by_name[name] = cand
            cleaned.append(cand)
            continue
        merged += 1
        first = by_name[name]
        stronger = cand if rank.get(str(cand.decision or ""), 0) > rank.get(str(first.decision or ""), 0) else first
        aliases = list(dict.fromkeys([*first.aliases, *cand.aliases]))
        attrs = {a.key: a for a in first.attributes}
        for attr in cand.attributes:
            attrs.setdefault(attr.key, attr)
        relations = {(r.target, r.type, r.target_entity_id): r for r in first.relations}
        for rel in cand.relations:
            relations.setdefault((rel.target, rel.type, rel.target_entity_id), rel)
        evidence = list(dict.fromkeys([*first.evidence_ids, *cand.evidence_ids]))
        merged_cand = EntityCardCandidate(
            name=name,
            aliases=aliases,
            kind=first.kind or cand.kind,
            summary=first.summary or cand.summary,
            attributes=list(attrs.values()),
            relations=list(relations.values()),
            entity_id=stronger.entity_id or first.entity_id or cand.entity_id,
            identity=first.identity or cand.identity,
            creation_reason=first.creation_reason or cand.creation_reason,
            decision=stronger.decision,
            evidence_ids=evidence,
        )
        by_name[name] = merged_cand
        cleaned = [by_name.get(str(c.name or "").strip(), c) for c in cleaned]
    return cleaned, merged, dropped


def prepare_candidates_for_commit(
    candidates: list[EntityCardCandidate],
    *,
    evidence_catalog: Any | None = None,
    conn: Any | None = None,
) -> tuple[list[EntityCardCandidate], tuple[RepairNote, ...]]:
    """提交前校验（M04/M05 兼容 + Phase 1 C 决策语义）。

    * 空名 / 同名合并 / 证据不在目录里的候选 → 丢弃并留 note；
    * create 带 entity_id / update 缺 entity_id 或目标不存在 → 丢弃并留 note；
    * observe / identity_uncertain 不自动创建：有真实已存在卡且给了 conn 时记成
      待处理候选（现有 pending 机制），否则丢弃并留 note；
    * 返回真正可以写库的 create / update 候选。

    run_entities_task 的现有调用（只传 candidates）保持可用；需要把
    observe / identity_uncertain 记成待处理候选的调用方传 conn。
    """
    merged, merged_count, dropped_count = _merge_candidates(candidates)
    catalog = coerce_catalog(evidence_catalog) if evidence_catalog is not None else None
    notes: list[RepairNote] = []
    if dropped_count:
        notes.append(
            RepairNote("entities", NOTE_DROPPED_INVALID_ITEM, f"丢掉 {dropped_count} 条空名候选")
        )
    if merged_count:
        notes.append(
            RepairNote("entities", NOTE_DROPPED_INVALID_ITEM, f"合并 {merged_count} 条同名候选")
        )
    ready: list[EntityCardCandidate] = []
    dropped_notes: list[str] = []
    for cand in merged:
        decision = str(cand.decision or DECISION_CREATE)
        if catalog is not None:
            _, missing = catalog.validate_evidence_ids(cand.evidence_ids)
            if missing:
                dropped_notes.append(f"{cand.name}: evidence_ids 不在目录")
                continue
        if decision == DECISION_OBSERVE or decision == DECISION_IDENTITY_UNCERTAIN:
            recorded = False
            if conn is not None:
                from agent.entities.cards import EntityCardService

                card = EntityCardService(conn).record_observed_candidate(cand, decision)
                recorded = card is not None
            if not recorded:
                dropped_notes.append(f"{cand.name}: {decision} 候选无已存在实体可挂")
            continue
        if decision == DECISION_CREATE and str(cand.entity_id or "").strip():
            dropped_notes.append(f"{cand.name}: create 带了 entity_id")
            continue
        if decision == DECISION_UPDATE:
            entity_id = str(cand.entity_id or "").strip()
            if not entity_id:
                dropped_notes.append(f"{cand.name}: update 缺少 entity_id")
                continue
            if conn is not None:
                from agent.entities.cards import EntityCardService

                if EntityCardService(conn).get(entity_id) is None:
                    dropped_notes.append(f"{cand.name}: update 目标不存在")
                    continue
        ready.append(cand)
    if dropped_notes:
        notes.append(
            RepairNote(
                "entities",
                NOTE_DROPPED_INVALID_ITEM,
                "；".join(dropped_notes[:5]),
            )
        )
    return ready, tuple(notes)
