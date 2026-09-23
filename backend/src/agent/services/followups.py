"""首次引导里的「追问」：读用户自己的描述，问一到两个有实际价值的问题。

规则（来自产品决策）：
- 只有在用户确实写了一段自由描述时才问；
- 由模型按描述现问，不是固定问题；
- 最多两个；模型不可用时**不出现**，不阻塞、不留痕。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from agent.adapters.base import BaseAdapter, ChatMessage

logger = logging.getLogger(__name__)

MAX_QUESTIONS = 2
_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

SUGGEST_PROMPT = (
    "你在帮一个人完成个人助手的首次设置。读他写下的这段自由描述，"
    "提出最多两个**有实际价值**的追问：只问能长期影响助手理解他的信息（背景、长期关注、"
    "以及他希望助手怎么跟他配合），不要问可以猜到的、也不要重复描述里已经说清楚的。"
    "每个问题一句话，用他的语言（中文）。"
    '只返回一个 JSON 对象，不要任何解释或代码块：{"questions": ["<问题>"]}\n'
    "如果没有值得追问的，返回空数组。"
)


def validate_questions(text: str) -> tuple[list[str], str | None]:
    """解析模型输出；严格校验，失败就当作「没有问题」。"""
    match = _JSON_BLOCK.search((text or "").strip())
    candidate = match.group(1) if match else (text or "").strip()
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return [], f"invalid JSON: {exc}"
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        return [], "questions missing"
    questions = [
        str(q).strip()
        for q in data["questions"][:MAX_QUESTIONS]
        if str(q).strip()
    ]
    return questions, None


async def suggest_follow_ups(
    adapter: BaseAdapter, description: str, *, temperature: float = 0.3
) -> tuple[list[str], str | None]:
    prompt = f"{SUGGEST_PROMPT}\n\n他的描述：\n{description.strip()[:2000]}"
    try:
        completion: Any = await adapter.complete(
            [ChatMessage(role="user", content=prompt)], tools=[], temperature=temperature
        )
    except Exception as exc:  # noqa: BLE001 - 追问失败不该影响引导
        logger.warning("follow-up suggestion failed: %s", exc)
        return [], f"model call failed: {exc}"
    return validate_questions(completion.message.content or "")
