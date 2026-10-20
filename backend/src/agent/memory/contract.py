"""摘要契约（FragmentSummary）：类型、提示词、本地校验。

生成、schema、修正与存取共用 :mod:`agent.memory.limits` 的同一组上限。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field, ValidationError

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


SUMMARY_PROMPT = (
    "You are summarizing a closed conversation fragment. "
    "Return only a single JSON object, no prose or Markdown fences:\n"
    '{"title": "<short title, <=%(title)d chars>", "summary": "<condensed record of facts, decisions, preferences, and commitments, <=%(summary)d chars>", "entities": ["<mentioned people/objects, exact names, at most %(entities)d, no duplicates>"], "keywords": ["<searchable keywords, at most %(keywords)d, no duplicates>"]}\n'
    "Keep the summary faithful to the transcript; do not add or infer facts. entities and keywords may be empty arrays when none. "
    "Keep the most important %(entities)d entries at most for entities and for keywords; anything longer is dropped before it is stored."
) % {
    "title": MAX_TITLE,
    "summary": MAX_SUMMARY,
    "entities": MAX_ENTITIES,
    "keywords": MAX_KEYWORDS,
}

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

    需要把「本地修正过什么」写进 trace / 诊断的调用方用 :func:`validate_summary`。
    """
    outcome = validate_summary(text)
    return outcome.value, outcome.error
