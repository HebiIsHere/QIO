# -*- coding: utf-8 -*-
"""实体卡纠错工具：用户否定/修正实体的属性或关系时调用。"""
from __future__ import annotations

import sqlite3
from typing import Any

from agent.entities.cards import EntityCardService
from agent.tools.base import Tool, ToolResult


class CorrectEntityTool(Tool):
    name = "correct_entity"
    description = (
        "纠正用户实体的属性或关系。当用户否定/修正之前关于某个实体（如「我家的鹅」「我师哥」）"
        "的事实、属性或关系时调用；可更新/删除属性、新增/删除关系，或删除整个实体。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "entity": {"type": "string", "description": "实体名（如「我家的鹅」）"},
            "attribute_key": {"type": "string", "description": "要更新/删除的属性名（可选）"},
            "attribute_value": {"type": "string", "description": "属性新值（更新属性时必填）"},
            "delete_attribute": {"type": "boolean", "description": "是否删除该属性，默认 false"},
            "relation_target": {"type": "string", "description": "关联实体名（可选）"},
            "relation_type": {"type": "string", "description": "关系类型（如「属于」「饲养」）"},
            "delete_relation": {"type": "boolean", "description": "是否删除该关系，默认 false"},
            "delete_entity": {"type": "boolean", "description": "是否删除整个实体，默认 false"},
        },
        "required": ["entity"],
    }

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    async def run(self, **kwargs: Any) -> ToolResult:
        svc = EntityCardService(self.conn)
        name = str(kwargs.get("entity") or "").strip()
        if not name:
            return ToolResult(ok=False, error="entity 必填")
        card = svc.find_by_name(name)
        if card is None:
            return ToolResult(ok=False, error=f"未找到实体「{name}」")

        key = str(kwargs.get("attribute_key") or "").strip()
        value = kwargs.get("attribute_value")
        rel_target = str(kwargs.get("relation_target") or "").strip()
        rel_type = str(kwargs.get("relation_type") or "").strip()

        if kwargs.get("delete_entity"):
            svc.revoke(card.id)
            return ToolResult(ok=True, content=f"已删除实体「{card.name}」")
        if kwargs.get("delete_attribute") and key:
            svc.remove_attribute(card.id, key)
            return ToolResult(ok=True, content=f"已删除实体「{card.name}」的属性「{key}」")
        if key and value is not None:
            svc.set_attribute(card.id, key, str(value))
            return ToolResult(ok=True, content=f"已更新实体「{card.name}」属性「{key}」={value}")
        if kwargs.get("delete_relation") and rel_target and rel_type:
            svc.remove_relation(card.id, rel_target, rel_type)
            return ToolResult(ok=True, content=f"已删除实体「{card.name}」与「{rel_target}」的「{rel_type}」关系")
        if rel_target and rel_type:
            svc.add_relation(card.id, rel_target, rel_type)
            return ToolResult(ok=True, content=f"已新增实体「{card.name}」与「{rel_target}」的「{rel_type}」关系")
        return ToolResult(ok=False, error="未指定要执行的操作")
