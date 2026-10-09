# -*- coding: utf-8 -*-
"""实体卡封块提炼：封块时让主模型从对话提炼实体卡候选（离线，失败降级）。

派生数据的「可修正 / 不可修正」判定统一走 `agent.memory.model_output` 的可修正层：
模型给多了、给长了、重复、空白、单条坏条目都在本地收敛后继续；只有
**无法解析 / 类型完全错误 / 必需结构缺失 / 无法安全恢复**才判失败。

失败不再静默：`extract_entity_cards_outcome` 返回可读失败原因与修正留痕，
由调用方写进 trace 与诊断；`extract_entity_cards` 是兼容入口，失败仍返回 []，
保持「实体卡失败不影响封块主流程」的既有隔离。
"""
from __future__ import annotations

from agent.adapters.base import BaseAdapter, ChatMessage
from agent.entities.cards import EntityCardCandidate
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
from agent.prompts import ENTITY_EXTRACT_PROMPT

_MAX_MESSAGES_CHARS = 12000

# 实体卡字段的硬上限。同一组数字既写进给模型的要求，也用在本地契约兜底 ——
# 模型先自己收敛，截断/丢弃只是兜底而不是常态。
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

# attributes / relations 是条目内部的子字段：它们坏掉只丢那一条子项，
# 不把承载它们的实体卡一起丢掉（strict=False）。
ENTITY_CARD_FIELDS = (
    ObjectListField(
        "entities",
        MAX_ENTITY_CARDS,
        fields=(
            TextField("name", MAX_CARD_NAME),
            TextField("kind", MAX_CARD_KIND, required=False),
            TextField("summary", MAX_CARD_SUMMARY, required=False),
            TextListField(
                "aliases",
                MAX_ALIASES,
                max_item_length=MAX_CARD_NAME,
                strict=False,
            ),
            ObjectListField(
                "attributes",
                MAX_ATTRIBUTES,
                fields=(
                    TextField("key", MAX_ATTRIBUTE_KEY),
                    TextField("value", MAX_ATTRIBUTE_VALUE),
                ),
                dedupe=True,
                strict=False,
            ),
            ObjectListField(
                "relations",
                MAX_RELATIONS,
                fields=(
                    TextField("target", MAX_RELATION_TARGET),
                    TextField("type", MAX_RELATION_TYPE),
                ),
                dedupe=True,
                strict=False,
            ),
        ),
        dedupe=True,
    ),
)

ENTITY_CARD_LIMITS_NOTE = (
    "上限（超出的部分会在本地被丢弃，请自己先收敛）："
    f"最多 {MAX_ENTITY_CARDS} 张卡；name <= {MAX_CARD_NAME} 字；"
    f"summary <= {MAX_CARD_SUMMARY} 字；aliases <= {MAX_ALIASES} 个；"
    f"attributes <= {MAX_ATTRIBUTES} 条；relations <= {MAX_RELATIONS} 条；"
    "不要重复条目，不要空字符串。"
)


async def extract_entity_cards_outcome(
    adapter: BaseAdapter,
    messages: list[dict],
    *,
    temperature: float = 0.2,
) -> Outcome[list[EntityCardCandidate]]:
    """提炼实体卡候选：返回（候选 / 可读失败原因 / 修正留痕）。"""
    lines = [f"[{m['role']}] {m['content']}" for m in messages if m.get("content")]
    text = "\n".join(lines)[:_MAX_MESSAGES_CHARS]
    if not text.strip():
        return Outcome([], None)  # 没有可提炼的原文，不是失败
    prompt = (
        ENTITY_EXTRACT_PROMPT.replace("{messages}", text)  # 用 replace 避免 JSON 花括号与 format 冲突
        + "\n"
        + ENTITY_CARD_LIMITS_NOTE
    )
    # 用量归因由 adapter 在每次实际请求上统一完成（含内部重试的每次响应）；
    # 这里只把「用量上限已耗尽」如实转成简短原因，不重试、不换配置、不造数。
    from agent.credentials.policy import BudgetExhausted

    try:
        completion = await adapter.complete(
            [ChatMessage(role="user", content=prompt)], []
        )
    except BudgetExhausted as exc:
        return Outcome(None, str(exc))
    except Exception as exc:
        return Outcome(None, f"model call failed: {exc}")
    payload, error = parse_json_object(completion.message.content or "")
    if error is not None:
        return Outcome(None, f"实体卡输出{error}")
    repaired = repair_output(payload, ENTITY_CARD_FIELDS, contract="实体卡输出")
    if not repaired.ok:
        return Outcome(None, repaired.error, repaired.notes)

    cards: list[EntityCardCandidate] = []
    rejected = 0
    for item in repaired.data.get("entities", []):
        try:
            cards.append(EntityCardCandidate(**item))
        except Exception:  # noqa: BLE001 - 单条契约不符只丢这一条，但计数留痕
            rejected += 1
    notes = list(repaired.notes)
    if rejected:
        notes.append(
            RepairNote(
                "entities",
                NOTE_DROPPED_INVALID_ITEM,
                f"丢掉 {rejected} 项未通过契约",
            )
        )
    return Outcome(cards, None, tuple(notes))


async def extract_entity_cards(
    adapter: BaseAdapter,
    messages: list[dict],
    *,
    temperature: float = 0.2,
) -> list[EntityCardCandidate]:
    """兼容入口：只要候选列表，任何失败都返回 []（不影响封块主流程）。

    需要失败原因 / 修正留痕（trace、派生诊断）的调用方用
    :func:`extract_entity_cards_outcome`。
    """
    outcome = await extract_entity_cards_outcome(
        adapter, messages, temperature=temperature
    )
    return outcome.value or []
