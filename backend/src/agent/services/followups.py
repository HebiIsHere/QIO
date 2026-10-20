# -*- coding: utf-8 -*-
"""首次引导里的「追问」（角色 C，阶段四契约 §14.5）。

规则（产品决策，保持原有行为）：
- 只有在用户确实写了一段自由描述时才问；
- 由模型按描述现问，不是固定问题；
- **最多两个**；模型不可用 / 输出无效 / 没有必要追问时都返回空，不阻塞、不留痕。

阶段四收口：
* 固定规则改为引用 prompts.ONBOARDING_FOLLOWUP_SYSTEM（唯一来源在 agent/prompts.py）；
* 使用公共结构化任务入口：结构化总则 + 任务规则 + 程序生成的 schema + limits，
  user 是一条独立 JSON 数据（json.dumps），不拼接主对话、不注入聊天回答标记；
* 输出固定 {"questions": []}；本地用 pydantic 校验（与 schema 同一类型）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent import prompts
from agent.adapters.base import ChatMessage

logger = logging.getLogger(__name__)

MAX_QUESTIONS = 2
MAX_QUESTION_CHARS = 200
MAX_DESCRIPTION_CHARS = 4000

ONBOARDING_LIMITS: dict[str, int] = {
    "max_questions": MAX_QUESTIONS,
    "max_question_chars": MAX_QUESTION_CHARS,
    "max_description_chars": MAX_DESCRIPTION_CHARS,
}


class OnboardingQuestions(BaseModel):
    """首次引导追问的结构化输出（允许空列表）。"""

    model_config = ConfigDict(extra="forbid")

    questions: list[str] = Field(default_factory=list, max_length=MAX_QUESTIONS)


def onboarding_questions_schema() -> dict[str, Any]:
    """展示给模型的输出 JSON Schema（与本地校验同一类型）。"""
    from agent.tools.contracts import json_schema_for

    return json_schema_for(OnboardingQuestions)


def _clean_questions(values: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(values, list):
        return out
    for item in values:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text[:MAX_QUESTION_CHARS])
    return out[:MAX_QUESTIONS]


def validate_questions(text: str) -> tuple[list[str], str | None]:
    """解析模型输出；严格校验，失败就当作「没有问题」（最多保留前两条）。"""
    import re

    raw = (text or "").strip()
    match = re.search(r"\x60{3}(?:json)?\s*(.*?)\x60{3}", raw, re.DOTALL)
    candidate = match.group(1) if match else raw
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return [], f"invalid JSON: {exc}"
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        return [], "questions missing"
    # 超出的问题按既有行为截断到两条（不把整次追问判失败）。
    return _clean_questions(data["questions"]), None


async def suggest_follow_ups(
    adapter: Any,
    description: str,
    *,
    language: str = "",
    temperature: float = 0.3,
) -> tuple[list[str], str | None]:
    """一次追问生成；失败返回空（引导不阻塞）。"""
    text = str(description or "").strip()
    if not text or adapter is None:
        return [], None
    data = {
        "description": text[:MAX_DESCRIPTION_CHARS],
        "interface_language": str(language or "").strip() or "zh",
        "limits": dict(ONBOARDING_LIMITS),
    }
    system_text = prompts.structured_task_system(
        "ONBOARDING_FOLLOWUP_SYSTEM",
        schema=onboarding_questions_schema(),
        limits=ONBOARDING_LIMITS,
    )
    messages = [
        ChatMessage(role="system", content=system_text),
        ChatMessage(role="user", content=json.dumps(data, ensure_ascii=False, sort_keys=True)),
    ]
    try:
        completion: Any = await adapter.complete(messages, tools=[], temperature=temperature)
    except Exception as exc:  # noqa: BLE001 - 追问失败不该影响引导
        from agent.trace.redact import redact_text

        reason = redact_text(f"{type(exc).__name__}: {exc}")
        logger.warning("follow-up suggestion failed: %s", reason)
        return [], f"model call failed: {reason}"
    raw = getattr(getattr(completion, "message", None), "content", None) or ""
    questions, error = validate_questions(raw)
    return questions, error
