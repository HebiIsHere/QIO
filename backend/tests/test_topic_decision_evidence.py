# -*- coding: utf-8 -*-
"""统一话题判断证据（契约 §2.3 / §4）——A 的定向单元测试。

只测纯数据与纯渲染：fake prediction + fake topic service / fragment，
不连数据库、不调用模型、不发布事件。相似度只是证据，本文件也断言
「高相似度不等于延续、也不自动触发切换」以及「没有候选时仍可正常回答」。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.services.params import TOPIC_DECISION
from agent.services.predict import TopicPrediction
from agent.services.topic_decision import (
    TopicCandidate,
    build_candidates,
    build_topic_decision_state,
    normalize_refused_attempts,
    render_topic_decision_note,
)


# ---------------------------------------------------------------------------
# fakes：只实现被读取的接口，不碰数据库
# ---------------------------------------------------------------------------


class FakeTopics:
    """TopicService 的替身：fingerprint 返回真实话题名与摘要。"""

    def __init__(self, topics: dict[str, tuple[str, str]]) -> None:
        self.topics = dict(topics)
        self.nodes = SimpleNamespace(get_topic=self._get_topic)

    def _get_topic(self, topic_id: str):
        title, _summary = self.topics.get(topic_id, (topic_id, ""))
        return SimpleNamespace(id=topic_id, name=title)

    def fingerprint(self, topic_id: str, recent_n: int = 5):
        title, summary = self.topics.get(topic_id, (topic_id, ""))
        return SimpleNamespace(
            topic_id=topic_id,
            title=title,
            summary_preview=summary,
            keywords=[],
            fragment_count=0,
            last_activity=None,
        )


class FakeFragments:
    """FragmentManager 的替身：只读 get / open_fragment。"""

    def __init__(self, fragments: dict | None = None) -> None:
        self.fragments = dict(fragments or {})
        self.open_by_topic: dict[str, object] = {}

    def get(self, fragment_id: str):
        return self.fragments.get(fragment_id)

    def open_fragment(self, topic_id: str):
        return self.open_by_topic.get(topic_id)


def _fragment(fragment_id: str, topic_id: str, summary: str | None):
    return SimpleNamespace(id=fragment_id, topic_id=topic_id, summary=summary)


def _app(topics=None, fragments=None, conn=None):
    app = SimpleNamespace(
        topics=topics if topics is not None else FakeTopics({}),
        fragments=fragments if fragments is not None else FakeFragments(),
    )
    if conn is not None:
        app.conn = conn
    return app


class _FakeTitleConn:
    """最小替身：只回答 memory_index 的 title 查询（当前片段标题兜底用）。"""

    def __init__(self, title: str) -> None:
        self.title = title

    def execute(self, sql, params):  # noqa: ANN001, ARG002
        title = self.title
        return SimpleNamespace(fetchone=lambda: ({"title": title} if title else None))


def _ctx(message: str = "把顺丁橡胶那次实验的结论再整理一下", **overrides):
    data = {
        "message": message,
        "bound_fragment_id": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _prediction(**overrides) -> TopicPrediction:
    data = {
        "main_topic_id": None,
        "aux_topic_ids": [],
        "is_new_topic_candidate": False,
        "suggested_switch": False,
        "scores": {},
        "backend_used": "onnx",
    }
    data.update(overrides)
    return TopicPrediction(**data)


# ---------------------------------------------------------------------------
# 候选：排序 / 去重 / 上限
# ---------------------------------------------------------------------------


def test_candidates_sorted_by_score_and_deduplicated():
    topics = FakeTopics(
        {
            "t_hot": ("橡胶实验", "关于顺丁橡胶降解的讨论"),
            "t_mid": ("前端", "QIO 前端交互"),
            "t_old": ("旧话题", "很久以前的讨论"),
            "t_aux": ("辅助话题", "只作为辅助证据"),
        }
    )
    prediction = _prediction(
        main_topic_id="t_hot",
        aux_topic_ids=["t_mid", "t_hot", "t_aux"],  # t_hot 重复出现
        scores={"t_hot": 0.91, "t_mid": 0.44, "t_old": 0.20},
    )

    candidates = build_candidates(prediction, topics)

    assert [c.topic_id for c in candidates] == ["t_hot", "t_mid", "t_old", "t_aux"]
    assert candidates[0].name == "橡胶实验"
    assert candidates[0].summary == "关于顺丁橡胶降解的讨论"
    # 上限内的重复话题只出现一次
    assert [c.topic_id for c in candidates].count("t_hot") == 1


def test_candidates_respect_limit_and_policy_cap():
    topics = FakeTopics({f"t{i}": (f"话题{i}", f"摘要{i}") for i in range(8)})
    scores = {f"t{i}": 0.9 - i * 0.1 for i in range(8)}
    prediction = _prediction(scores=scores)

    two = build_candidates(prediction, topics, limit=2)
    assert [c.topic_id for c in two] == ["t0", "t1"]

    assert len(build_candidates(prediction, topics, limit=99)) == TOPIC_DECISION.candidate_limit
    assert build_candidates(prediction, topics, limit=0) == []


def test_candidates_without_topic_service_still_have_ids():
    prediction = _prediction(scores={"t1": 0.7})
    candidates = build_candidates(prediction, None)
    assert [c.topic_id for c in candidates] == ["t1"]
    assert candidates[0].name == "t1"
    assert candidates[0].summary == ""
    # 没有预测结果（例如替身预测器）时是空证据，不当成「应新建」
    assert build_candidates(None, None) == []


# ---------------------------------------------------------------------------
# 讨论状态：只从真实消息 / 片段 / 操作结果取证据
# ---------------------------------------------------------------------------


def test_state_takes_scope_from_real_fragment_and_never_echoes_message():
    topics = FakeTopics({"t1": ("橡胶实验", "话题指纹摘要")})
    fragments = FakeFragments(
        {"frag_open": _fragment("frag_open", "t1", "这段在讨论顺丁橡胶降解的工艺参数。")}
    )
    app = _app(topics=topics, fragments=fragments)
    message = "把昨天那组降解数据再对比一下"
    ctx = _ctx(message, bound_fragment_id="frag_open")

    state = build_topic_decision_state(app, ctx, "t1", _prediction())

    assert state.topic_id == "t1"
    assert state.topic_name == "橡胶实验"
    assert state.scope_summary == "这段在讨论顺丁橡胶降解的工艺参数。"
    # P7 不变量：user 原文只出现在 user 消息里，system 证据不得逐字复制
    assert state.current_goal == ""
    assert message not in render_topic_decision_note(state)
    # 没有程序来源的「确认事实 / 未解决事项」保持为空，不拿摘要或猜测填充
    assert state.confirmed_facts == []
    assert state.open_items == []


def test_current_goal_prefers_program_field_and_never_echoes_message():
    message = "把昨天那组降解数据再对比一下"
    topics = FakeTopics({"t1": ("橡胶实验", "")})

    # 1) 程序字段优先，且可以进入证据
    state = build_topic_decision_state(
        _app(topics=topics),
        _ctx(message, topic_current_goal="整理顺丁橡胶降解实验的数据"),
        "t1",
        _prediction(),
    )
    assert state.current_goal == "整理顺丁橡胶降解实验的数据"
    note = render_topic_decision_note(state)
    assert "整理顺丁橡胶降解实验的数据" in note
    assert message not in note

    # 2) 程序字段等于/包含本轮原文时丢弃（不得借「目标」把原文再抄一遍）
    for echoed in (message, "整理一下：" + message):
        echoed_state = build_topic_decision_state(
            _app(topics=topics),
            _ctx(message, topic_current_goal=echoed),
            "t1",
            _prediction(),
        )
        assert echoed_state.current_goal == ""
        assert message not in render_topic_decision_note(echoed_state)

    # 3) 片段标题作为兜底；标题回显本轮原文时同样丢弃
    fragments = FakeFragments({"frag_open": _fragment("frag_open", "t1", None)})
    titled = build_topic_decision_state(
        _app(topics=topics, fragments=fragments, conn=_FakeTitleConn("顺丁橡胶降解实验参数整理")),
        _ctx(message, bound_fragment_id="frag_open"),
        "t1",
        _prediction(),
    )
    assert titled.current_goal == "顺丁橡胶降解实验参数整理"
    titled_note = render_topic_decision_note(titled)
    assert "顺丁橡胶降解实验参数整理" in titled_note
    assert message not in titled_note

    echoed_title = build_topic_decision_state(
        _app(topics=topics, fragments=fragments, conn=_FakeTitleConn(message)),
        _ctx(message, bound_fragment_id="frag_open"),
        "t1",
        _prediction(),
    )
    assert echoed_title.current_goal == ""

    # 4) 没有任何真实来源就留空
    empty = build_topic_decision_state(
        _app(topics=topics, fragments=FakeFragments()),
        _ctx(message),
        "t1",
        _prediction(),
    )
    assert empty.current_goal == ""


def test_state_scope_falls_back_to_topic_fingerprint_and_clips():
    topics = FakeTopics({"t1": ("橡胶实验", "长" * 500)})
    app = _app(topics=topics, fragments=FakeFragments())
    ctx = _ctx("继续", bound_fragment_id=None)

    state = build_topic_decision_state(app, ctx, "t1", _prediction())

    assert state.scope_summary.endswith("…")
    assert len(state.scope_summary) == TOPIC_DECISION.scope_summary_chars + 1


def test_state_only_uses_program_supplied_confirmed_facts_and_open_items():
    app = _app(topics=FakeTopics({"t1": ("橡胶实验", "")}))
    ctx = _ctx(
        "继续之前那个方案",
        topic_confirmed_facts=["用户确认采用 150 度、2 小时"],
        topic_open_items=["还缺一次重复实验"],
    )

    state = build_topic_decision_state(app, ctx, "t1", _prediction())

    assert state.confirmed_facts == ["用户确认采用 150 度、2 小时"]
    assert state.open_items == ["还缺一次重复实验"]


# ---------------------------------------------------------------------------
# 拒绝范围：进入状态与渲染
# ---------------------------------------------------------------------------


def test_refusal_scope_enters_state_and_is_rendered_without_internal_codes():
    refuse_scope = {
        "action": "switch",
        "from_topic_id": "t1",
        "target_topic_id": "t2",
        "target_topic_name": "橡胶实验",
        "reason": "stay_current",
        "turn_id": "turn-1",
    }
    app = _app(topics=FakeTopics({"t1": ("前端", ""), "t2": ("橡胶实验", "")}), fragments=FakeFragments())
    state = build_topic_decision_state(app, _ctx(), "t1", _prediction(), refuse_scope=refuse_scope)

    assert state.refused_attempts == [
        {"action": "switch", "target": "橡胶实验", "reason": "", "outcome": "user_refused"}
    ]
    note = render_topic_decision_note(state)
    assert "申请切换到已有话题「橡胶实验」，用户选择在当前话题回答" in note
    assert "stay_current" not in note
    assert "同一段讨论不要重复申请" in note


def test_refusal_scope_accepts_entries_list_and_create_records():
    refuse_scope = {
        "entries": [
            {"action": "topic_create", "new_name": "新方向", "reason": "用户取消了"},
        ]
    }
    attempts = normalize_refused_attempts(refuse_scope, topic_id="t1")
    assert attempts == [
        {"action": "create", "target": "新方向", "reason": "用户取消了", "outcome": "user_refused"}
    ]

    note = render_topic_decision_note(
        build_topic_decision_state(
            _app(), _ctx(), "t1", _prediction(), refuse_scope=refuse_scope
        )
    )
    assert "申请新建话题「新方向」，用户选择在当前话题回答" in note
    assert "当时的原因：用户取消了" in note


def test_refusal_scope_ignores_other_topics_and_caps_entries():
    other = {"action": "switch", "from_topic_id": "t_other", "target_topic_name": "别处"}
    current = [
        {"action": "switch", "from_topic_id": "t1", "target_topic_name": f"目标{i}"}
        for i in range(8)
    ]
    attempts = normalize_refused_attempts(other, topic_id="t1")
    assert attempts == []
    attempts = normalize_refused_attempts(current, topic_id="t1")
    assert len(attempts) == TOPIC_DECISION.max_refused_attempts
    # 也接受裸列表
    assert len(normalize_refused_attempts(current, topic_id="t1")) == TOPIC_DECISION.max_refused_attempts


def test_refusal_scope_accepts_guard_refused_attempt_shape():
    """B 的 RefusedAttempt.as_dict()：topic_id/topic_name 是**目标**，不能按来源过滤。"""
    refused = {
        "topic_id": "t2",
        "topic_name": "橡胶实验",
        "action": "switch",
        "reason": "rejected",
        "turn_id": "turn-1",
        "user_message_id": "msg_1",
        "fragment_id": "frag_1",
        "created_at": "2026-11-10T00:00:00+00:00",
    }

    attempts = normalize_refused_attempts(refused, topic_id="t1")

    assert attempts == [
        {"action": "switch", "target": "橡胶实验", "reason": "", "outcome": "user_refused"}
    ]
    note = render_topic_decision_note(
        build_topic_decision_state(_app(), _ctx(), "t1", _prediction(), refuse_scope=refused)
    )
    assert "申请切换到已有话题「橡胶实验」，用户选择在当前话题回答" in note


def test_refusal_timeout_is_not_rendered_as_user_choice():
    """超时/失效是「没有执行」，不能写成用户选择留在当前话题（诚实口径）。"""
    refused = {
        "topic_id": "t2",
        "topic_name": "橡胶实验",
        "action": "switch",
        "reason": "timeout",
        "turn_id": "turn-1",
    }

    attempts = normalize_refused_attempts(refused, topic_id="t1")
    assert attempts == [
        {"action": "switch", "target": "橡胶实验", "reason": "等待超时", "outcome": "not_executed"}
    ]

    note = render_topic_decision_note(
        build_topic_decision_state(_app(), _ctx(), "t1", _prediction(), refuse_scope=refused)
    )
    assert "申请切换到已有话题「橡胶实验」没有执行（等待超时）" in note
    assert "申请切换到已有话题「橡胶实验」，用户选择在当前话题回答" not in note


# ---------------------------------------------------------------------------
# 渲染：当前话题 + 候选摘要 + 四结果规则
# ---------------------------------------------------------------------------


def _rich_state():
    topics = FakeTopics(
        {
            "t1": ("橡胶实验", "当前在讨论降解工艺参数"),
            "t2": ("前端交互", "QIO 前端与统一弹窗"),
        }
    )
    prediction = _prediction(
        main_topic_id="t2",
        aux_topic_ids=["t1"],
        is_new_topic_candidate=False,
        suggested_switch=True,
        scores={"t2": 0.91, "t1": 0.55},
    )
    app = _app(topics=topics, fragments=FakeFragments())
    return build_topic_decision_state(app, _ctx(), "t1", prediction)


def test_note_contains_current_topic_and_candidate_summaries():
    note = render_topic_decision_note(_rich_state())

    assert "当前话题：「橡胶实验」（t1）" in note
    assert "「前端交互」（t2）：QIO 前端与统一弹窗" in note
    # 候选是当前话题时标注，避免建议切到原话题
    assert "「橡胶实验」（t1）：当前在讨论降解工艺参数（当前话题，不需要切换）" in note


def test_four_outcomes_present_and_consistent():
    note = render_topic_decision_note(_rich_state())

    assert "延续" in note
    assert "建议切换已有话题" in note and "switch_topic" in note
    assert "建议创建新话题" in note and "create_topic" in note
    assert "暂时无法判断" in note and "不强制询问" in note
    # 何时可申请：只有可独立处理且超出当前范围的新目标
    assert "只有可独立处理" in note and "明显超出当前话题与用户已认可范围" in note
    # 不把每个新目标 / 新词拆成话题
    assert "不把每个新目标、每个新词都拆成话题" in note
    # 相似度只是证据，不能只凭分数
    assert "相似度只是证据" in note
    assert "相似度高也可能需要换题" in note
    assert "相似度低也可能只是延续" in note
    assert "不得只看分数" in note
    # 需要申请时只说过程，不说正式回答、不声称已切换/创建
    assert "只输出一句简短过程说明" in note
    assert "不要输出正式回答" in note
    assert "不要声称已经切换或创建" in note
    # 已选择留在当前话题的同一段讨论不重复申请，但独立新事项可重新判断
    assert "同一段讨论不要重复申请" in note
    assert "出现独立的新事项时可以重新判断" in note


def test_high_similarity_is_evidence_not_a_decision():
    state = _rich_state()
    assert state.candidates[0].topic_id == "t2"
    assert state.candidates[0].score > 0.9
    # 状态里没有「decided / should_switch」这类可执行结论字段
    assert not hasattr(state, "decision")
    assert not hasattr(state, "should_switch")

    note = render_topic_decision_note(state)
    # 分数本身不作为文本/结论渲染给模型
    assert "0.91" not in note
    # 高相似度候选只是证据，仍由模型按四结果判断
    assert "「前端交互」（t2）" in note
    assert "「前端交互」（t2）：QIO 前端与统一弹窗（当前话题，不需要切换）" not in note


def test_no_candidates_still_supports_normal_answer():
    app = _app(topics=FakeTopics({"t1": ("当前事项", "")}), fragments=FakeFragments())
    prediction = _prediction(
        main_topic_id=None, aux_topic_ids=[], is_new_topic_candidate=True
    )
    state = build_topic_decision_state(app, _ctx(), "t1", prediction)

    assert state.candidates == []
    note = render_topic_decision_note(state)
    assert "当前没有可切换的已有话题候选" in note
    assert "这不代表应该新建话题" in note
    # 没有候选也必须保留「直接回答」与「暂时无法判断时正常回答」的证据路径
    assert "延续，直接回答" in note
    assert "暂时无法判断，留在当前话题正常回答" in note


# ---------------------------------------------------------------------------
# 渲染结果是纯文本：没有可执行的前端载荷
# ---------------------------------------------------------------------------


def test_render_returns_plain_text_without_executable_payload():
    note = render_topic_decision_note(_rich_state())

    assert isinstance(note, str)
    # 契约 §3：note 本体不使用【】标题（动态上下文标题只能来自 CONTEXT_LABELS 块）
    assert note.startswith("话题判断（本轮证据与规则）：")
    assert "【" not in note and "】" not in note
    assert "TOPIC_SWITCH_SUGGESTED" not in note
    assert "pending_switch" not in note
    assert '"action"' not in note
    assert "{" not in note and "}" not in note
    with pytest.raises(json.JSONDecodeError):
        json.loads(note)


def test_render_accepts_scoped_candidates_without_program_hint():
    state = build_topic_decision_state(
        _app(topics=FakeTopics({"t1": ("当前事项", "")})),
        _ctx("继续"),
        "t1",
        _prediction(scores={"t9": 0.5}),
    )
    # P1-01：判断状态里不再有「程序已识别明确导航」字段；语义只来自本轮模型。
    assert not hasattr(state, "explicit_hint")

    note = render_topic_decision_note(
        state,
        [TopicCandidate("t_override", "覆盖候选", 0.4, "来自调用方的候选摘要")],
    )
    assert "覆盖候选" in note
    assert "来自调用方的候选摘要" in note

    assert render_topic_decision_note(None) == ""
