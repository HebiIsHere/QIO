"""互动模式（Interaction Mode）后端模块。

契约：docs/interactive-mode-contract.md
- models      共享数据含义（Lead）
- board       板面操作语义：分组 / 顺序 / 关系（A）
- board_store 板面与草稿的持久化（B）
- submission  可见范围 / 有效改动 / 提交前后状态（B）
- intents     QIO 意图与审批生命周期（C）
"""

from __future__ import annotations

from agent.interactive.models import (  # noqa: F401  (re-export for callers)
    CARD_KINDS,
    CHECKABLE_KINDS,
    DEFAULT_BOARD_ID,
    EXPRESSION_KINDS,
    INTENT_STATUSES,
    MATERIAL_KINDS,
    OPEN_INTENT_STATUSES,
    REPLY_KIND,
    card_by_id,
    default_group_name,
    dumps,
    empty_state,
    group_of,
    live_card_ids,
    loads,
    new_card,
    new_group,
    new_link,
    new_id,
    now_iso,
    selectable_cards,
    selectable_ids,
)
