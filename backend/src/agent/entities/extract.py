# -*- coding: utf-8 -*-
"""实体卡封块提炼：封块时让主模型从对话提炼实体卡候选（离线，失败静默降级）。"""
from __future__ import annotations

import json
import re

from agent.adapters.base import BaseAdapter, ChatMessage
from agent.entities.cards import EntityCardCandidate
from agent.prompts import ENTITY_EXTRACT_PROMPT

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_MAX_MESSAGES_CHARS = 12000


async def extract_entity_cards(
    adapter: BaseAdapter,
    messages: list[dict],
    *,
    temperature: float = 0.2,
) -> list[EntityCardCandidate]:
    """调用主模型提炼实体卡候选；任何失败都返回 []（不影响封块主流程）。"""
    lines = [f"[{m['role']}] {m['content']}" for m in messages if m.get("content")]
    text = "\n".join(lines)[:_MAX_MESSAGES_CHARS]
    if not text.strip():
        return []
    prompt = ENTITY_EXTRACT_PROMPT.replace("{messages}", text)  # 用 replace 避免 JSON 花括号与 format 冲突
    try:
        completion = await adapter.complete(
            [ChatMessage(role="user", content=prompt)], []
        )
    except Exception:
        return []
    content = completion.message.content or ""
    match = _JSON_BLOCK.search(content)
    candidate = match.group(1) if match else content.strip()
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return []
    items = data.get("entities", []) if isinstance(data, dict) else []
    out: list[EntityCardCandidate] = []
    for item in items:
        try:
            out.append(EntityCardCandidate(**item))
        except Exception:
            continue
    return out
