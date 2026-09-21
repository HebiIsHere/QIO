"""分段边界策略（阶段 4）：**纯判断**，不写库、不调模型、不改导航。

它只回答一个问题：这条新的用户输入，是不是「同一阶段继续」，还是「进入了新的
交付工作」，或者「说不准」。真正执行（封存、建片段、改绑定）由 FragmentManager
与 TurnOrchestrator 负责 —— 判断与执行分开，才可能被离线评测反复跑。

三条纪律（来自规格）：

1. **长度是兜底，不是完成信号**：容量到点就分块，但不得据此宣称「任务已完成」；
2. **不因关键词误切**：否定句、引用、假设里的「开始写代码」都不是指令；
3. **说不准就不切**：没有明确信号时返回 uncertain，由上层保守处理。

首版只把**确定性规则**当真（容量、明确进入新工作、短确认、同阶段追问），
语义相似度一类需要校准的信号默认只记录建议，不实际切分。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

POLICY_VERSION = "boundary-v1"

ACTION_CONTINUE = "continue"
ACTION_SPLIT = "split"
ACTION_UNCERTAIN = "uncertain"

REASON_CAPACITY = "capacity"
REASON_PHASE_CHANGE = "phase_change"
REASON_SHORT_ACK = "short_ack"
REASON_REFINEMENT = "same_stage_refinement"
REASON_NEGATED = "negated_instruction"
REASON_QUOTED = "quoted_instruction"
REASON_HYPOTHETICAL = "hypothetical_instruction"
REASON_NO_SIGNAL = "no_signal"

# 「明确进入新的交付工作」的词组。命中只是候选：还要过否定/引用/假设三关。
_PHASE_PHRASES = (
    "开始编写",
    "开始写",
    "开始实现",
    "开始落地",
    "开始动手",
    "进入实施",
    "进入开发",
    "进入实现",
    "动手实现",
    "按照方案实现",
)

_NEGATION_BEFORE = ("不要", "不用", "别", "先不", "暂不", "不", "别急着", "还没")
_HYPOTHETICAL_NEAR = ("如果", "假如", "要是", "假设", "一旦", "等", "什么时候")
_QUOTE_CHARS = ("「", "」", "“", "”", "\"", "'", "『", "』")

_ACK_PATTERNS = ("好", "好的", "行", "可以", "就这样", "继续", "开始吧", "嗯", "ok", "OK", "是的")
_REFINEMENT_MARKERS = (
    "不过",
    "但是",
    "其实",
    "再补充",
    "补充一下",
    "改成",
    "换成",
    "换句话说",
    "再想想",
    "我还是觉得",
    "为什么",
    "怎么",
)

MAX_ACK_CHARS = 12


@dataclass(frozen=True)
class BoundaryDecision:
    action: str
    reason: str
    evidence: str = ""
    confidence: float = 0.5
    policy_version: str = POLICY_VERSION
    # 由上层决定真正落在哪一轮边界上（首版固定为「本轮结束之后」）
    boundary: str = "after_current_turn"

    @property
    def is_split(self) -> bool:
        return self.action == ACTION_SPLIT


def _span_is_quoted(text: str, start: int, end: int) -> bool:
    """词组是否落在引号里（在讨论措辞，而不是在下指令）。"""
    prefix = text[:start]
    for char in _QUOTE_CHARS:
        if prefix.count(char) % 2 == 1:
            return True
    return False


def _span_is_negated(text: str, start: int) -> bool:
    window = text[max(0, start - 4) : start]
    return any(neg in window for neg in _NEGATION_BEFORE)


def _span_is_hypothetical(text: str, start: int, end: int) -> bool:
    window = text[max(0, start - 8) : min(len(text), end + 8)]
    return any(marker in window for marker in _HYPOTHETICAL_NEAR)


def _phase_phrase(text: str) -> tuple[str, str] | None:
    """返回 (词组, 判定) —— 判定是 quoted / negated / hypothetical / imperative 之一。"""
    for phrase in _PHASE_PHRASES:
        start = text.find(phrase)
        if start < 0:
            continue
        end = start + len(phrase)
        if _span_is_quoted(text, start, end):
            return phrase, "quoted"
        if _span_is_negated(text, start):
            return phrase, "negated"
        if _span_is_hypothetical(text, start, end):
            return phrase, "hypothetical"
        return phrase, "imperative"
    return None


class FragmentBoundaryPolicy:
    """确定性边界判断。构造时给定容量目标，`decide` 是纯函数。"""

    def __init__(self, *, max_turns: int, max_tokens: int = 0) -> None:
        self.max_turns = max(1, int(max_turns))
        self.max_tokens = max(0, int(max_tokens))

    def decide(
        self,
        *,
        user_input: str,
        fragment_turns: int,
        fragment_tokens: int,
        recent: Sequence[dict] | None = None,
        idle_minutes: float | None = None,
    ) -> BoundaryDecision:
        """判断当前这条输入是否开启新阶段。

        `recent` 只用于「短确认是否接得上上一轮的下一步」这类判断，
        首版不依赖模型评分，也不使用语义相似度。
        """
        text = (user_input or "").strip()
        if not text:
            return BoundaryDecision(ACTION_CONTINUE, REASON_NO_SIGNAL, confidence=0.4)

        # 1) 容量兜底：与阶段是否结束无关，到点就分块（但保留完整轮次边界）
        hit_turns = fragment_turns >= self.max_turns
        hit_tokens = self.max_tokens > 0 and fragment_tokens >= self.max_tokens
        if hit_turns or hit_tokens:
            why = "轮数到点" if hit_turns else "内容长度到点"
            return BoundaryDecision(
                ACTION_SPLIT,
                REASON_CAPACITY,
                evidence=f"{why}（轮数 {fragment_turns}/{self.max_turns}，"
                f"长度 {fragment_tokens}/{self.max_tokens or '不限'}）",
                confidence=1.0,
            )

        # 2) 明确进入新的交付工作：先排除「否定 / 引用 / 假设」三种误命中
        hit = _phase_phrase(text)
        if hit is not None:
            phrase, verdict = hit
            if verdict == "negated":
                return BoundaryDecision(
                    ACTION_CONTINUE, REASON_NEGATED, evidence=f"「{phrase}」被否定", confidence=0.9
                )
            if verdict == "quoted":
                return BoundaryDecision(
                    ACTION_CONTINUE, REASON_QUOTED, evidence=f"「{phrase}」在引号里", confidence=0.9
                )
            if verdict == "hypothetical":
                return BoundaryDecision(
                    ACTION_CONTINUE,
                    REASON_HYPOTHETICAL,
                    evidence=f"「{phrase}」出现在假设句里",
                    confidence=0.85,
                )
            return BoundaryDecision(
                ACTION_SPLIT,
                REASON_PHASE_CHANGE,
                evidence=f"出现明确的开工指令「{phrase}」",
                confidence=0.7,
            )

        # 3) 短确认：接在上一轮下一步之后，属同一阶段；它自己不该生成新片段
        if len(text) <= MAX_ACK_CHARS and any(text.startswith(p) for p in _ACK_PATTERNS):
            return BoundaryDecision(
                ACTION_CONTINUE,
                REASON_SHORT_ACK,
                evidence="短确认，跟随上一轮的下一步",
                confidence=0.6,
            )

        # 4) 同一方案的追问 / 反驳 / 修正 / 补充
        if any(marker in text for marker in _REFINEMENT_MARKERS):
            return BoundaryDecision(
                ACTION_CONTINUE, REASON_REFINEMENT, evidence="同一阶段的追问或修正", confidence=0.6
            )

        # 5) 其余情况不猜：时间间隔、单次插话、语义相似度下降都不单独证明阶段结束
        del idle_minutes, recent  # 首版明确不使用这两个信号做切分
        return BoundaryDecision(ACTION_UNCERTAIN, REASON_NO_SIGNAL, confidence=0.5)
