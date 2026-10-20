# -*- coding: utf-8 -*-
"""实体卡纠错工具（Phase 1 C：稳定 ID + 修订版本 CAS + 结构化结果）。

契约 §6.3 / §7：

* 保留原有参数（entity / attribute_key / attribute_value / delete_attribute /
  relation_target / relation_type / delete_relation / delete_entity）；
* 新增 entity_id 与 expected_revision：有稳定 ID 时按 ID + revision CAS；
* 身份不明确时**不得按同名猜测**：返回 conflict / awaiting_input 并说明缺什么；
* 用户明确纠正当场生效（同一轮返回真实结果），不需要再次确认同一明确纠正。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent.entities.cards import EntityCardService
from agent.prompts import TOOL_DESCRIPTIONS
from agent.tools.base import TOOL_RESULT_PROTOCOL_VERSION, Tool, ToolResult
from agent.tools.contracts import json_schema_for, validate_model


class CorrectEntityArgs(BaseModel):
    """correct_entity 参数（展示 schema 与本地校验同源）。"""

    model_config = ConfigDict(extra="forbid")

    entity: str | None = None
    entity_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=0)
    attribute_key: str | None = None
    attribute_value: str | None = None
    delete_attribute: bool = False
    relation_target: str | None = None
    relation_type: str | None = None
    delete_relation: bool = False
    delete_entity: bool = False


def _completed(message: str, *, code: str, data: dict[str, Any]) -> ToolResult:
    return ToolResult(
        ok=True,
        content=message,
        protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
        status="completed",
        code=code,
        data=data,
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


class CorrectEntityTool(Tool):
    name = "correct_entity"
    description = TOOL_DESCRIPTIONS["correct_entity"]
    parameters = json_schema_for(CorrectEntityArgs)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def _candidates_by_name(self, name: str) -> list:
        """同名 / 别名命中的活跃卡（用来发现「同名多对象」的身份歧义）。"""
        rows = self.conn.execute(
            "SELECT * FROM entity_cards WHERE state = 'active' AND (name = ? OR "
            "EXISTS (SELECT 1 FROM json_each(aliases) WHERE json_each.value = ?)) "
            "ORDER BY updated_at DESC",
            (name, name),
        ).fetchall()
        svc = EntityCardService(self.conn)
        return [svc.row_to_card(row) for row in rows]

    async def run(self, **kwargs: Any) -> ToolResult:
        args, errors = validate_model(CorrectEntityArgs, kwargs)
        if args is None:
            return _failure(
                "invalid_arguments",
                "参数不合法：" + "；".join(errors[:5]),
                data={"field_errors": errors},
            )
        svc = EntityCardService(self.conn)
        name = str(args.entity or "").strip()
        if args.entity_id:
            card = svc.get(str(args.entity_id))
            if card is None:
                return _failure(
                    "not_found",
                    f"未找到实体：{args.entity_id}",
                    data={"entity_id": args.entity_id},
                )
        else:
            if not name:
                return _failure(
                    "invalid_arguments",
                    "entity 或 entity_id 至少需要一个",
                )
            matches = self._candidates_by_name(name)
            if not matches:
                return _failure("not_found", f"未找到实体「{name}」")
            if len(matches) > 1:
                return _failure(
                    "ambiguous_identity",
                    f"「{name}」匹配到 {len(matches)} 个同名/别名实体，身份不明确，"
                    "不会按同名猜测；请提供 entity_id 确认要修改哪一个。",
                    recoverable=True,
                    status="awaiting_input",
                    data={"candidates": [{"entity_id": c.id, "name": c.name} for c in matches[:8]]},
                )
            card = matches[0]
        if args.expected_revision is not None and int(args.expected_revision) != int(card.revision):
            return _failure(
                "revision_conflict",
                f"实体「{card.name}」已变化：期望 revision {args.expected_revision}，"
                f"当前 {card.revision}；未做修改。",
                recoverable=True,
                status="conflict",
                data={"entity_id": card.id, "current_revision": card.revision},
            )

        key = str(args.attribute_key or "").strip()
        value = args.attribute_value
        rel_target = str(args.relation_target or "").strip()
        rel_type = str(args.relation_type or "").strip()

        if args.delete_entity:
            svc.revoke(card.id)
            return _completed(
                f"已删除实体「{card.name}」",
                code="entity_revoked",
                data={"entity_id": card.id, "revision": card.revision, "deleted": True},
            )
        if args.delete_attribute and key:
            updated = svc.remove_attribute(card.id, key)
            return _completed(
                f"已删除实体「{card.name}」的属性「{key}」",
                code="attribute_removed",
                data={
                    "entity_id": card.id,
                    "revision": updated.revision if updated else card.revision,
                    "attribute_key": key,
                },
            )
        if key and value is not None:
            updated = svc.set_attribute(card.id, key, str(value))
            return _completed(
                f"已更新实体「{card.name}」属性「{key}」={value}",
                code="attribute_updated",
                data={
                    "entity_id": card.id,
                    "revision": updated.revision if updated else card.revision,
                    "attribute_key": key,
                    "attribute_value": str(value),
                },
            )
        if args.delete_relation and rel_target and rel_type:
            updated = svc.remove_relation(card.id, rel_target, rel_type)
            return _completed(
                f"已删除实体「{card.name}」与「{rel_target}」的「{rel_type}」关系",
                code="relation_removed",
                data={
                    "entity_id": card.id,
                    "revision": updated.revision if updated else card.revision,
                },
            )
        if rel_target and rel_type:
            updated = svc.add_relation(card.id, rel_target, rel_type)
            return _completed(
                f"已新增实体「{card.name}」与「{rel_target}」的「{rel_type}」关系",
                code="relation_added",
                data={
                    "entity_id": card.id,
                    "revision": updated.revision if updated else card.revision,
                },
            )
        return _failure("invalid_arguments", "未指定要执行的操作")
