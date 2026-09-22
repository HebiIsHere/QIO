"""Execution Narrative：模型自主决定的过程说明。

职责边界（spec 2026-09-22）：

* 本模块只回答「模型说了什么」以及「怎么把它安全地交给前端」；
* 工具事实、参数、风险、审批权限不经过本模块，也不受本模块影响；
* `silent`（不说明）是默认选项：解析失败、文本为空、kind 非法一律返回 None。

模型给的原始信封只允许三个键：``kind`` / ``text`` / ``explanation``。
其它键（例如模型试图提交 ``risk``、``capabilities``、``description``）直接丢弃，
所以文案不可能覆盖系统生成的真实操作信息。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.trace.redact import redact_text

# 工具调用参数里的保留字段名：模型写在这里，adapter 解析时剥离，
# 工具与风险判断永远看不到它。
NARRATIVE_KEY = "_qio"

NARRATIVE_KINDS = ("announce", "progress", "warning", "result")

MAX_TEXT_CHARS = 120
MAX_EXPLANATION_CHARS = 200


@dataclass(frozen=True)
class Narrative:
    """一条模型叙事：过程说明文本，以及可选的审批 explanation。"""

    kind: str
    text: str
    explanation: str = ""
    silent: bool = False  # 只带 explanation、没有 text 时为 True


def _clean(value: object, limit: int) -> str:
    """统一清洗：非字符串丢弃、密钥形态内容脱敏、长度截断。"""
    if not isinstance(value, str):
        return ""
    return redact_text(value).strip()[:limit]


def parse_narrative(raw: object) -> Narrative | None:
    """白名单解析模型给的 `_qio`；任何不合法输入都返回 None（= 保持安静）。"""
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    text = _clean(raw.get("text"), MAX_TEXT_CHARS)
    explanation = _clean(raw.get("explanation"), MAX_EXPLANATION_CHARS)
    if kind not in NARRATIVE_KINDS:
        # 只带 explanation（例如"这次调用需要你确认，因为…"）时按 progress 记录，
        # 但没有文本 → 不产生过程说明行。
        if not text and explanation:
            kind = "progress"
        else:
            return None
    if not text and not explanation:
        return None
    return Narrative(kind=str(kind), text=text, explanation=explanation, silent=not text)


def split_narrative_arguments(arguments: dict[str, Any]) -> tuple[dict[str, Any], dict | None]:
    """把保留字段从工具参数里剥出来。

    返回 ``(干净参数, 原始信封)``。工具执行、风险判断、审批摘要只看干净参数。
    """
    source = dict(arguments or {})
    raw = source.pop(NARRATIVE_KEY, None)
    return source, raw if isinstance(raw, dict) else None


def narrative_event_payload(
    narrative_id: str,
    turn_id: str | None,
    narrative: Narrative,
    *,
    tool: str | None = None,
    call_id: str | None = None,
    call_ids: list[str] | None = None,
    created_at: str | None = None,
) -> dict:
    """SSE NARRATIVE 载荷：工具/调用标识全部由系统提供，模型无法伪造。"""
    return {
        "narrative_id": narrative_id,
        "turn_id": turn_id,
        "kind": narrative.kind,
        "text": narrative.text,
        "tool": tool,
        "call_id": call_id,
        "call_ids": list(call_ids or []),
        "created_at": created_at,
    }
