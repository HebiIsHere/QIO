"""分段边界策略：确定性规则逐条（阶段 4）。

这些用例直接对应规格里的规则表与验收矩阵：容量、明确开工、短确认、追问修正、
以及三种**不得误切**的表达（否定 / 引用 / 假设）。
"""

from __future__ import annotations

from agent.memory.boundary import (
    ACTION_CONTINUE,
    ACTION_SPLIT,
    ACTION_UNCERTAIN,
    REASON_CAPACITY,
    REASON_HYPOTHETICAL,
    REASON_NEGATED,
    REASON_PHASE_CHANGE,
    REASON_QUOTED,
    REASON_SHORT_ACK,
    FragmentBoundaryPolicy,
)


def _policy(*, turns: int = 10, tokens: int = 8000) -> FragmentBoundaryPolicy:
    return FragmentBoundaryPolicy(max_turns=turns, max_tokens=tokens)


def test_capacity_hits_by_turns_or_tokens():
    p = _policy(turns=5, tokens=1000)

    by_turns = p.decide(user_input="继续吧", fragment_turns=5, fragment_tokens=10)
    by_tokens = p.decide(user_input="继续吧", fragment_turns=1, fragment_tokens=1000)
    below = p.decide(user_input="继续吧", fragment_turns=4, fragment_tokens=999)

    assert by_turns.action == ACTION_SPLIT and by_turns.reason == REASON_CAPACITY
    assert by_tokens.action == ACTION_SPLIT and by_tokens.reason == REASON_CAPACITY
    assert below.action != ACTION_SPLIT
    # 容量到点也不宣称「任务完成」：结论必须只说分段原因
    assert "完成" not in by_turns.evidence


def test_explicit_kickoff_starts_a_new_stage():
    p = _policy()
    for text in ("设计确定了，开始实现", "进入实施阶段吧", "那现在开始编写实施提示词"):
        decision = p.decide(user_input=text, fragment_turns=1, fragment_tokens=10)
        assert decision.action == ACTION_SPLIT, text
        assert decision.reason == REASON_PHASE_CHANGE


def test_negated_kickoff_is_not_an_instruction():
    p = _policy()
    decision = p.decide(user_input="不要开始写代码，继续分析", fragment_turns=1, fragment_tokens=10)
    assert decision.action == ACTION_CONTINUE
    assert decision.reason == REASON_NEGATED


def test_quoted_kickoff_is_discussion_not_an_instruction():
    p = _policy()
    decision = p.decide(
        user_input="你刚才那句「现在开始实现」的措辞不太对，我们换个说法",
        fragment_turns=1,
        fragment_tokens=10,
    )
    assert decision.action == ACTION_CONTINUE
    assert decision.reason == REASON_QUOTED


def test_hypothetical_kickoff_is_not_an_instruction():
    p = _policy()
    decision = p.decide(
        user_input="如果我们开始实现的话，第一步应该做什么？",
        fragment_turns=1,
        fragment_tokens=10,
    )
    assert decision.action == ACTION_CONTINUE
    assert decision.reason == REASON_HYPOTHETICAL


def test_short_acknowledgement_stays_in_the_same_stage():
    p = _policy()
    for text in ("好", "好的", "就这样", "继续", "开始吧", "OK"):
        decision = p.decide(user_input=text, fragment_turns=2, fragment_tokens=100)
        assert decision.action == ACTION_CONTINUE, text
        assert decision.reason == REASON_SHORT_ACK


def test_refinement_of_the_same_plan_does_not_split():
    p = _policy()
    decision = p.decide(
        user_input="不过我觉得第二步的顺序要换一下，再补充一个前提",
        fragment_turns=3,
        fragment_tokens=200,
    )
    assert decision.action == ACTION_CONTINUE


def test_single_interjection_and_idle_time_do_not_split():
    p = _policy()
    # 单次插话：内容与当前工作无关，但只有一条信号 → 保守，不切
    interjection = p.decide(
        user_input="今天上海下雨了", fragment_turns=3, fragment_tokens=200
    )
    # 长时间间隔同样不能单独证明阶段结束
    idle = p.decide(
        user_input="我们接着上次的说", fragment_turns=3, fragment_tokens=200, idle_minutes=600
    )

    assert interjection.action == ACTION_UNCERTAIN
    assert idle.action != ACTION_SPLIT


def test_empty_input_is_not_a_stage_signal():
    p = _policy()
    decision = p.decide(user_input="   ", fragment_turns=1, fragment_tokens=10)
    assert decision.action == ACTION_CONTINUE
