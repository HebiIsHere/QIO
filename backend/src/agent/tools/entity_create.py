"""create_entity 工具（Phase 1 C）：QIO 自主创建身份明确的具体对象。

冻结契约（docs/qio-refactor-contract.md §6.4 / §7）：

* 必填 name / identity / creation_reason / evidence_ids（证据必须在本轮程序证据目录）；
* 可选 kind / aliases / summary / attributes / relations（关系支持 target 与
  target_entity_id；只有名称、目标不确定的关系不写同名实体，留成待处理候选）；
* 先查重：同名但身份不同 → 不合并、不覆盖，返回结构化冲突；
  能证明同一对象 → 返回已有真实 id（幂等）；已有受保护信息不被覆盖；
* 创建后返回真实 entity id 与 revision；新增字段写进 field_meta.card，
  经 API 的 to_dict 可读。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from agent.entities.cards import (
    DECISION_CREATE,
    MAX_CREATION_REASON,
    MAX_EVIDENCE_IDS,
    MAX_IDENTITY,
    EntityAttribute,
    EntityCardCandidate,
    EntityCardService,
    EntityRelation,
    card_identity,
)
from agent.entities.extract import (
    MAX_ALIASES,
    MAX_ATTRIBUTES,
    MAX_ATTRIBUTE_KEY,
    MAX_ATTRIBUTE_VALUE,
    MAX_CARD_KIND,
    MAX_CARD_NAME,
    MAX_CARD_SUMMARY,
    MAX_RELATION_TARGET,
    MAX_RELATION_TYPE,
    MAX_RELATIONS,
)
from agent.knowledge.evidence import coerce_catalog
from agent.prompts import ARGUMENT_DESCRIPTIONS, TOOL_DESCRIPTIONS
from agent.tools.base import TOOL_RESULT_PROTOCOL_VERSION, Tool, ToolResult
from agent.tools.contracts import json_schema_for, validate_model


class CreateEntityAttributeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=MAX_ATTRIBUTE_KEY)
    value: str = Field(max_length=MAX_ATTRIBUTE_VALUE)
    evidence_ids: list[str] = Field(default_factory=list, max_length=MAX_EVIDENCE_IDS)


class CreateEntityRelationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str | None = Field(default=None, max_length=MAX_RELATION_TARGET)
    target_entity_id: str | None = None
    type: str = Field(min_length=1, max_length=MAX_RELATION_TYPE)
    evidence_ids: list[str] = Field(default_factory=list, max_length=MAX_EVIDENCE_IDS)


class CreateEntityArgs(BaseModel):
    """create_entity 的完整参数（展示 schema 与本地校验同源）。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_CARD_NAME)
    identity: str = Field(
        min_length=1,
        max_length=MAX_IDENTITY,
        description="这个对象是谁：区别于同名对象的具体身份",
    )
    creation_reason: str = Field(
        min_length=1,
        max_length=MAX_CREATION_REASON,
        description="为什么值得持续记录（持续关系 / 实际使用需要 / 信息价值）",
    )
    evidence_ids: list[str] = Field(
        min_length=1,
        max_length=MAX_EVIDENCE_IDS,
        description=ARGUMENT_DESCRIPTIONS["evidence_ids"],
    )
    kind: str | None = Field(default=None, max_length=MAX_CARD_KIND)
    aliases: list[str] = Field(default_factory=list, max_length=MAX_ALIASES)
    summary: str | None = Field(default=None, max_length=MAX_CARD_SUMMARY)
    attributes: list[CreateEntityAttributeArgs] = Field(
        default_factory=list, max_length=MAX_ATTRIBUTES
    )
    relations: list[CreateEntityRelationArgs] = Field(
        default_factory=list, max_length=MAX_RELATIONS
    )


def _failure(
    code: str,
    message: str,
    *,
    recoverable: bool = True,
    data: dict[str, Any] | None = None,
    status: str = "failed",
) -> ToolResult:
    return ToolResult(
        ok=False,
        content=message,
        error=message,
        recoverable=recoverable,
        protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
        status=status,
        code=code,
        data=dict(data or {}),
    )


class CreateEntityTool(Tool):
    name = "create_entity"
    description = TOOL_DESCRIPTIONS["create_entity"]
    parameters = json_schema_for(CreateEntityArgs)

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        evidence_provider: Callable[[], Any] | None = None,
    ) -> None:
        self.conn = conn
        self.evidence_provider = evidence_provider

    def _catalog(self):
        if self.evidence_provider is None:
            return coerce_catalog(None)
        return coerce_catalog(self.evidence_provider())

    def _resolve_relations(
        self, relations: list[CreateEntityRelationArgs]
    ) -> tuple[list[EntityRelation], str | None]:
        """校验 / 解析关系目标：有真实 ID 按 ID；只有名字找不到就不猜（留候选）。"""
        svc = EntityCardService(self.conn)
        resolved: list[EntityRelation] = []
        for rel in relations:
            target_id = str(rel.target_entity_id or "").strip()
            target = str(rel.target or "").strip()
            if target_id:
                row = self.conn.execute(
                    "SELECT id FROM nodes WHERE id = ?", (target_id,)
                ).fetchone()
                if row is None:
                    return [], f"target_entity_id 不是真实实体节点：{target_id}"
                resolved.append(
                    EntityRelation(
                        target=target or target_id,
                        type=rel.type,
                        target_entity_id=target_id,
                        evidence_ids=list(rel.evidence_ids),
                    )
                )
                continue
            if not target:
                return [], "关系至少需要 target 或 target_entity_id"
            card = svc.find_by_name(target)
            if card is not None and card.node_id:
                resolved.append(
                    EntityRelation(
                        target=target,
                        type=rel.type,
                        target_entity_id=card.node_id,
                        evidence_ids=list(rel.evidence_ids),
                    )
                )
            else:
                # 只有名称、目标身份不确定：保留候选关系，不写同名实体。
                resolved.append(
                    EntityRelation(
                        target=target,
                        type=rel.type,
                        evidence_ids=list(rel.evidence_ids),
                    )
                )
        return resolved, None

    async def run(self, **kwargs: Any) -> ToolResult:
        args, errors = validate_model(CreateEntityArgs, kwargs)
        if args is None:
            return _failure(
                "invalid_arguments",
                "参数不合法，请修正后重试：" + "；".join(errors[:5]),
                data={"field_errors": errors},
            )
        catalog = self._catalog()
        evidence_ids, missing = catalog.validate_evidence_ids(args.evidence_ids)
        if missing:
            return _failure(
                "evidence_not_found",
                "evidence_ids 必须来自本轮实际证据目录，未找到："
                + ", ".join(missing[:5]),
                data={"missing_evidence_ids": missing, "available_ids": catalog.ids()[:20]},
            )
        relations, rel_error = self._resolve_relations(args.relations)
        if rel_error is not None:
            return _failure("invalid_relations", rel_error)
        candidate = EntityCardCandidate(
            name=args.name.strip(),
            identity=args.identity.strip(),
            creation_reason=args.creation_reason.strip(),
            evidence_ids=list(evidence_ids),
            kind=(args.kind.strip() if args.kind else None),
            aliases=[a.strip() for a in args.aliases if a.strip()],
            summary=args.summary or "",
            attributes=[
                EntityAttribute(
                    key=a.key,
                    value=a.value,
                    evidence_ids=list(a.evidence_ids),
                )
                for a in args.attributes
            ],
            relations=relations,
            decision=DECISION_CREATE,
        )
        svc = EntityCardService(self.conn)
        existing = svc.find_by_name(candidate.name)
        if existing is not None:
            stored = card_identity(existing)
            incoming = candidate.identity or ""
            if stored and incoming and stored != incoming:
                return _failure(
                    "identity_conflict",
                    "已有同名实体但身份不同，未合并、未覆盖：「"
                    + candidate.name
                    + "」。请确认是否为同一对象。",
                    recoverable=True,
                    status="conflict",
                    data={
                        "entity_id": existing.id,
                        "revision": existing.revision,
                        "stored_identity": stored,
                        "incoming_identity": incoming,
                    },
                )
        card = svc.apply_auto_candidate(candidate)
        if card is None:
            return _failure(
                "create_failed",
                "实体写入失败（可能遇到并发修改），请稍后重试。",
                recoverable=True,
            )
        created = existing is None
        return ToolResult(
            ok=True,
            content=(
                ("已创建实体「" if created else "已关联到已有实体「")
                + card.name
                + "」（ID: "
                + card.id
                + "）"
            ),
            protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
            status="completed",
            code="created" if created else "matched_existing",
            data={
                "entity_id": card.id,
                "name": card.name,
                "revision": card.revision,
                "created": created,
                "identity": card_identity(card),
                "evidence_ids": list(evidence_ids),
                "pending_relations": svc.pending_relations(card.id),
            },
        )
