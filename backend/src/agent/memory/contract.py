"""摘要契约（FragmentSummary）：类型、结构化任务参数、本地校验。

Phase 1 起：固定提示词正文**只在** agent.prompts（CLOSED_SUMMARY_SYSTEM /
MERGE_SUMMARY_SYSTEM），本模块只保留类型、由类型生成的 schema / limits，以及
本地可修正校验。生成、schema、修正与存取共用 agent.memory.limits 的同一组上限，
避免多处各写一份。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from agent import prompts
from agent.memory.limits import MAX_ENTITIES, MAX_KEYWORDS, MAX_SUMMARY, MAX_TITLE
from agent.memory.model_output import (
    Outcome,
    TextField,
    TextListField,
    parse_json_object,
    repair_output,
)

logger = logging.getLogger(__name__)


class FragmentSummary(BaseModel):
    """Contract for fragment summaries (schema v1)."""

    title: str = Field(min_length=1, max_length=MAX_TITLE)
    summary: str = Field(min_length=1, max_length=MAX_SUMMARY)
    entities: list[str] = Field(default_factory=list, max_length=MAX_ENTITIES)
    keywords: list[str] = Field(default_factory=list, max_length=MAX_KEYWORDS)


# 摘要契约：title/summary 必需且可截断；entities/keywords 是字符串数组。
_SUMMARY_FIELDS = (
    TextField("title", MAX_TITLE),
    TextField("summary", MAX_SUMMARY),
    TextListField("entities", MAX_ENTITIES),
    TextListField("keywords", MAX_KEYWORDS),
)


def validate_summary(text: str) -> Outcome[FragmentSummary]:
    """解析 + 修正 + 校验模型输出。返回带可读失败原因与修正留痕的 Outcome。"""
    payload, error = parse_json_object(text)
    if error is not None:
        return Outcome(None, f"摘要输出{error}")
    repaired = repair_output(payload, _SUMMARY_FIELDS, contract="摘要输出")
    if not repaired.ok:
        return Outcome(None, repaired.error, repaired.notes)
    try:
        return Outcome(FragmentSummary(**repaired.data), None, repaired.notes)
    except ValidationError as exc:
        # 修正层放过的形态问题在这里兜底：契约（pydantic）才是最终权威。
        return Outcome(None, f"schema violation: {exc.errors()[:3]}", repaired.notes)


def validate_summary_text(text: str) -> tuple[FragmentSummary | None, str | None]:
    """兼容入口：只关心 (值, 失败原因) 的调用方继续用它。

    需要把「本地修正过什么」写进 trace / 诊断的调用方用 validate_summary。
    """
    outcome = validate_summary(text)
    return outcome.value, outcome.error


# ---------------------------------------------------------------------------
# 结构化摘要任务（Phase 1）：模型输入参数的唯一来源
# ---------------------------------------------------------------------------
# 为什么在契约模块生成：schema 由 FragmentSummary 生成、limits 由 memory.limits
# 生成，二者与本地校验同源；提示词正文本身仍只来自 agent.prompts。


def summary_schema() -> dict[str, Any]:
    """封块摘要 / 合并摘要的输出 JSON Schema（由类型定义生成）。"""
    return FragmentSummary.model_json_schema()


def summary_limits() -> dict[str, Any]:
    """写进结构化任务提示词的 limits（与生成侧上限同一来源）。"""
    return {
        "title_max_chars": MAX_TITLE,
        "summary_max_chars": MAX_SUMMARY,
        "entities_max_items": MAX_ENTITIES,
        "keywords_max_items": MAX_KEYWORDS,
    }


def closed_summary_task_system() -> str:
    """封块摘要（单段）的 system：结构化总则 + CLOSED_SUMMARY_SYSTEM + schema + limits。"""
    return prompts.structured_task_system(
        "CLOSED_SUMMARY_SYSTEM",
        schema=summary_schema(),
        limits=summary_limits(),
    )


def merge_summary_task_system() -> str:
    """分段摘要合并的 system：结构化总则 + MERGE_SUMMARY_SYSTEM + schema + limits。"""
    return prompts.structured_task_system(
        "MERGE_SUMMARY_SYSTEM",
        schema=summary_schema(),
        limits=summary_limits(),
    )


def rolling_summary_task_system() -> str:
    """滚动摘要的 system：结构化总则 + ROLLING_SUMMARY_SYSTEM + schema + limits。"""
    return prompts.structured_task_system(
        "ROLLING_SUMMARY_SYSTEM",
        schema=summary_schema(),
        limits=summary_limits(),
    )
