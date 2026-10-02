# -*- coding: utf-8 -*-
"""话题判定的标定回归测试（真实 ONNX 余弦快照，离线、确定性）。

语料 backend/evals/topic_threshold/cases.jsonl 覆盖延续 / 措辞变化 / 跨话题引用 /
真新话题 / 对抗性新话题 / 模糊转移 / 无信号 / 中文短句 / 英文 / 中英混合 / **多轮上下文**；
分数快照 backend/evals/topic_threshold/scores_onnx.json 由
"uv run --frozen python evals/topic_threshold_curve.py --record" 用真实
bge-small-zh-v1.5(fp32) 记录，带 model identity 可溯源。

判定走**生产代码**（TopicPredictor._rank + affinity.classify），并且用的是
params.TOPIC 的生产默认值 —— 所以这里断言的就是产品行为，任何把阈值调回
「单条 0.7」之类的改动都会立刻失败（旧行为在这份语料上 acc=0.279）。

**多轮上下文（2026-10-02 追加）**：`multi_turn_*` 类别带 `previous_exchanges`，评测把
历史 user 轮折进当前话题的指纹文本再冷启动话题向量（见 topic_threshold_curve.py 的
说明）。快照里同时记录了 `scores_without_context`，所以测试能断言「上下文真的改变了
模型的分数」，而不是摆一个没人用的字段。

记录的实测值（2026-10-02，真实 ONNX，118 条）：
  acc 0.8983 | genuinely_new 召回 0.846 | in_topic 召回 0.938 | false_new 0.065
  多轮类别：multi_turn_continue 10/10、multi_turn_new_topic 3/3、multi_turn_switch 1/1
  多轮用例里 14/14 的分数随上下文变化，其中 1 条（ctx_09）的**判定**依赖上下文
  （不折历史会被判成新话题）—— 其余多轮用例的判定本来就由现任信号兜住，
  这条记录的是事实，不夸大上下文的作用。
"""

from __future__ import annotations

import json
from pathlib import Path

from agent.services.affinity import classify
from agent.services.params import TOPIC
from agent.services.predict import TopicPredictor

EVALS = Path(__file__).resolve().parents[1] / "evals" / "topic_threshold"
MODES = ("in_topic", "switch", "new_topic")

# 记录的实测值（2026-10-02，真实 ONNX）：
#   acc 0.885 | genuinely_new 召回 0.826 | in_topic 召回 0.930 | false_new 0.074
# 门槛留出余量，但远高于旧实现的 0.279 / 0.056。
MIN_ACCURACY = 0.85
MIN_GENUINELY_NEW_RECALL = 0.78
MIN_IN_TOPIC_RECALL = 0.90
MAX_FALSE_NEW_RATE = 0.10

# 多轮类别单独设下限：整体 acc 会被大类别稀释，掩盖多轮退化。
# 记录的实测值是 10/10、3/3、1/1；下限留在下面（有意的余量，不是精确快照）。
MIN_MULTI_TURN_ACCURACY = {
    "multi_turn_continue": 0.80,
    "multi_turn_new_topic": 0.66,
    "multi_turn_switch": 0.50,
}
REQUIRED_MULTI_TURN_CATEGORIES = tuple(MIN_MULTI_TURN_ACCURACY)


def _load():
    cases = [
        json.loads(line)
        for line in (EVALS / "cases.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    snapshot = json.loads((EVALS / "scores_onnx.json").read_text(encoding="utf-8"))
    return cases, snapshot


def _predict(case, scores, policy=TOPIC):
    predictor = TopicPredictor(None, None, None)
    predictor.new_topic_threshold = policy.new_topic_threshold
    prediction = predictor._rank(dict(scores), case.get("current_topic"), backend="onnx")
    return classify(case["message"], prediction, case.get("current_topic"), [], policy=policy)


def test_snapshot_is_complete_and_traceable():
    cases, snapshot = _load()
    assert "bge-small-zh-v1.5" in snapshot["model"]
    ids = {c["id"] for c in cases}
    assert ids == set(snapshot["cases"]), "快照与用例必须一一对应"
    assert len(cases) >= 100
    cats = {c["category"] for c in cases}
    for required in (
        "continue_current",
        "paraphrase",
        "cross_topic_reference",
        "genuinely_new",
        "ambiguous_shift",
        "short_cn",
        "en",
        "mixed",
        "multi_turn_continue",
        "multi_turn_new_topic",
        "multi_turn_switch",
    ):
        assert required in cats, f"语料缺少类别 {required}"
    for case in cases:
        assert case.get("note", "").strip(), f"{case['id']} 缺少 note（每条用例都要说明它为什么存在）"
        assert case.get("category"), case["id"]


def test_decision_quality_on_real_embedding_corpus():
    cases, snapshot = _load()
    scores = {cid: entry["scores"] for cid, entry in snapshot["cases"].items()}
    confusion = {a: {b: 0 for b in MODES} for a in MODES}
    for case in cases:
        decision = _predict(case, scores[case["id"]])
        confusion[case["expected"]][decision.mode.value] += 1

    n = len(cases)
    accuracy = sum(confusion[a][a] for a in MODES) / n
    genuinely_new_recall = confusion["new_topic"]["new_topic"] / sum(confusion["new_topic"].values())
    in_topic_recall = confusion["in_topic"]["in_topic"] / sum(confusion["in_topic"].values())
    predicted_new = sum(confusion[a]["new_topic"] for a in MODES)
    false_new = predicted_new - confusion["new_topic"]["new_topic"]
    false_new_rate = false_new / (n - sum(confusion["new_topic"].values()))

    assert accuracy >= MIN_ACCURACY, f"acc={accuracy:.3f} 低于标定值 {MIN_ACCURACY}（confusion={confusion}）"
    assert genuinely_new_recall >= MIN_GENUINELY_NEW_RECALL, (
        f"真新话题召回 {genuinely_new_recall:.3f} 掉到 {MIN_GENUINELY_NEW_RECALL} 以下"
    )
    assert in_topic_recall >= MIN_IN_TOPIC_RECALL, f"延续召回 {in_topic_recall:.3f} 太低"
    assert false_new_rate <= MAX_FALSE_NEW_RATE, f"假新话题率 {false_new_rate:.3f} 超标"


def test_multi_turn_cases_are_declared_with_context():
    """多轮用例必须真的带上下文，而且判定不能靠「短输入」这条捷径。

    末尾消息短于 min_new_topic_chars 时，生产逻辑会无条件判延续 —— 那样的用例
    测不到上下文，只是把长度规则又跑了一遍。
    """
    cases, _ = _load()
    multi = [c for c in cases if str(c["category"]).startswith("multi_turn_")]
    assert len(multi) >= 10, f"多轮语料缩水到 {len(multi)} 条"
    for case in multi:
        turns = case.get("previous_exchanges")
        assert isinstance(turns, list) and len(turns) >= 2, f"{case['id']} 缺少多轮上下文"
        for turn in turns:
            assert turn.get("role") in {"user", "assistant"}, case["id"]
            assert str(turn.get("content") or "").strip(), case["id"]
        assert any(str(t.get("role")) == "user" for t in turns), case["id"]
        assert len(case["message"].strip()) >= TOPIC.min_new_topic_chars, (
            f"{case['id']} 的末尾消息太短，判定会被长度规则接管，测不到上下文"
        )


def test_multi_turn_context_really_changes_the_scores():
    """上下文不是摆设：折了历史与不折历史，同一个末尾消息的分数必须不同。

    另外断言至少有一条用例的**判定**依赖上下文（否则这份语料只是多写了几个字段）。
    """
    cases, snapshot = _load()
    with_context = 0
    decision_changed = 0
    for case in cases:
        entry = snapshot["cases"][case["id"]]
        bare = entry.get("scores_without_context")
        if bare is None:
            continue
        with_context += 1
        assert bare != entry["scores"], f"{case['id']}：折了历史却没有任何分数变化"
        assert entry.get("context_turns", 0) >= 2, case["id"]
        decided = _predict(case, entry["scores"]).mode.value
        without = _predict(case, bare).mode.value
        decision_changed += decided != without
    assert with_context >= 10, f"只有 {with_context} 条用例记录了上下文对照"
    assert decision_changed >= 1, "没有任何用例的判定依赖上下文：多轮语料没有真正测到东西"


def test_multi_turn_categories_hold_their_measured_quality():
    """多轮类别逐类设下限：整体 acc 会被大类别稀释，掩盖多轮退化。"""
    cases, snapshot = _load()
    scores = {cid: entry["scores"] for cid, entry in snapshot["cases"].items()}
    per_category: dict[str, list[int]] = {}
    for case in cases:
        slot = per_category.setdefault(case["category"], [0, 0])
        slot[1] += 1
        if _predict(case, scores[case["id"]]).mode.value == case["expected"]:
            slot[0] += 1
    for category, floor in MIN_MULTI_TURN_ACCURACY.items():
        assert category in per_category, f"语料缺少类别 {category}"
        ok, total = per_category[category]
        assert ok / total >= floor, f"{category}: {ok}/{total} 低于下限 {floor}"


def test_generic_new_topic_without_lexical_signal_is_not_swallowed():
    """反向证明：长度护栏不是「没匹配上就默认延续」。

    这条消息与任何话题都没有词面重合（分数全为 0），而且足够长 —— 必须判新话题。
    """
    case = {
        "id": "no_signal_long_new",
        "message": "帮我看看这个周末的天气怎么样再去哪儿玩",
        "current_topic": "t_sql",
        "expected": "new_topic",
    }
    decision = _predict(case, {"t_sql": 0.0})
    assert decision.mode.value == "new_topic"


def test_short_acknowledgement_continues_the_current_topic():
    cases, snapshot = _load()
    for case_id in ("short_01", "short_04", "short_09"):
        case = next(c for c in cases if c["id"] == case_id)
        decision = _predict(case, snapshot["cases"][case_id]["scores"])
        assert decision.mode.value == "in_topic", case_id
