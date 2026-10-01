"""Topic prediction evaluator (deterministic, offline).

Input: a JSONL dataset of cases, each with topics (id/title/keywords), the
current topic, a message, and the expected mode. Thresholds come from
`agent.services.params.TopicPolicy`.

Output: switch / new-topic / in-topic metrics. No network, no paid models;
the embedding path uses a deterministic fake backend so it is reproducible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.services.params import TOPIC, TopicPolicy


@dataclass
class _Fingerprint:
    topic_id: str
    title: str
    keywords: list[str]
    summary_preview: str = ""


class _StubTopics:
    def __init__(self, fps: list[_Fingerprint]) -> None:
        self._fps = fps

    def list_with_fingerprints(self) -> list[_Fingerprint]:
        return self._fps


class FakeEmbedding:
    """Deterministic bag-of-token embedding (no onnx, no network).

    Same input → same vector, so the onnx path can be evaluated reproducibly.
    """

    def __init__(self, dims: int = 64) -> None:
        self.dims = dims
        self._vectors: dict[str, Any] = {}

    def available(self) -> bool:
        return True

    def _vec(self, text: str):
        import hashlib

        import numpy as np

        from agent.selector.tokenize import tokenize

        v = np.zeros(self.dims, dtype=np.float32)
        for tok in tokenize(text):
            # 稳定哈希（不用内置 hash()：它受 PYTHONHASHSEED 影响、跨进程不确定）
            bucket = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16) % self.dims
            v[bucket] += 1.0
        n = float(np.linalg.norm(v)) or 1e-9
        return v / n

    def embed_texts(self, texts: list[str]):
        import numpy as np

        if not texts:
            return None
        return np.stack([self._vec(t) for t in texts])

    def topic_vector(self, topic_id: str):
        return self._vectors.get(topic_id)

    def update_topic_vector(self, topic_id: str, text: str) -> None:
        self._vectors[topic_id] = self._vec(text)


#: recorded 后端使用**真实模型记录下来的余弦快照**（输入是 embedding，不是标签）。
#: 见 backend/evals/record_topic_scores.py；快照自带 model identity 以便溯源。
RECORDED_SCORES_PATH = (
    Path(__file__).resolve().parents[3] / "evals" / "topic_prediction" / "scores_onnx.json"
)


def load_cases(path: str | Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            cases.append(json.loads(line))
    return cases


def load_recorded_scores(path: str | Path | None = None) -> dict[str, Any]:
    path = Path(path) if path is not None else RECORDED_SCORES_PATH
    if not path.exists():
        raise RuntimeError(
            f"用例声明 backend=recorded，但快照不存在：{path}。"
            "请先跑 backend/evals/record_topic_scores.py（需要本机有 ONNX 模型）。"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def recorded_scores_for(case_id: str, path: str | Path | None = None) -> dict[str, float]:
    """取某条用例的记录余弦。

    **绝不静默回落**：缺快照、缺该用例、结果为空 —— 一律抛错。
    退回 fake / BM25 / 零向量都会让评测看起来有数、实际测的是别的东西。
    """
    snapshot = load_recorded_scores(path)
    entry = (snapshot.get("cases") or {}).get(case_id)
    if entry is None:
        raise RuntimeError(
            f"用例 {case_id} 声明 backend=recorded，但快照 {snapshot.get('model', '?')} 里没有它；"
            "请重跑 record_topic_scores.py，不要退回假 embedding。"
        )
    scores = entry.get("scores")
    if not isinstance(scores, dict) or not scores:
        raise RuntimeError(f"用例 {case_id} 的记录余弦为空：{entry!r}")
    return {str(k): float(v) for k, v in scores.items()}


def predict_mode(case: dict[str, Any], policy: TopicPolicy = TOPIC) -> str:
    from agent.services.affinity import classify
    from agent.services.predict import TopicPredictor

    fps = [
        _Fingerprint(t["id"], t.get("title", ""), t.get("keywords", []))
        for t in case.get("topics", [])
    ]
    backend = case.get("backend")
    embedding = FakeEmbedding() if backend == "fake_embedding" else None
    predictor = TopicPredictor(
        None,
        embedding,
        _StubTopics(fps),
        new_topic_threshold=policy.new_topic_threshold,
        rules_new_topic_threshold=policy.rules_new_topic_threshold,
        aux_topic_threshold=policy.aux_topic_threshold,
        rules_aux_topic_threshold=policy.rules_aux_topic_threshold,
        switch_delta=policy.switch_delta,
        aux_top_count=policy.aux_top_count,
    )
    if backend == "recorded":
        # 记录的真实 ONNX 余弦：只替换 embedding 这一层，
        # 后面的 _rank + classify 仍是生产代码（与 predict() 内部第二步一致）。
        prediction = predictor._rank(
            recorded_scores_for(case["id"]), case.get("current_topic"), backend="onnx"
        )
    else:
        prediction = predictor.predict(case["message"], current_topic_id=case.get("current_topic"))
    decision = classify(case["message"], prediction, case.get("current_topic"), [])
    return decision.mode.value


def evaluate(cases: list[dict[str, Any]], policy: TopicPolicy = TOPIC) -> dict[str, Any]:
    counts = {"in_topic": 0, "switch": 0, "new_topic": 0}
    correct = {"in_topic": 0, "switch": 0, "new_topic": 0}
    pred_new_total = 0
    false_new = 0
    false_switch = 0
    actual_not_new = 0
    actual_not_switch = 0
    rows: list[dict[str, Any]] = []
    for case in cases:
        expected = case["expected"]
        predicted = predict_mode(case, policy)
        counts[expected] = counts.get(expected, 0) + 1
        if predicted == expected:
            correct[expected] = correct.get(expected, 0) + 1
        if predicted == "new_topic":
            pred_new_total += 1
            if expected != "new_topic":
                false_new += 1
        if expected != "new_topic":
            actual_not_new += 1
        if predicted == "switch" and expected != "switch":
            false_switch += 1
        if expected != "switch":
            actual_not_switch += 1
        rows.append({"id": case.get("id"), "expected": expected, "predicted": predicted})

    def rate(num: int, den: int) -> float:
        return round(num / den, 4) if den else 0.0

    return {
        "n": len(cases),
        "in_topic_accuracy": rate(correct.get("in_topic", 0), counts.get("in_topic", 0)),
        "switch_accuracy": rate(correct.get("switch", 0), counts.get("switch", 0)),
        "new_topic_precision": rate(correct.get("new_topic", 0), pred_new_total),
        "new_topic_recall": rate(correct.get("new_topic", 0), counts.get("new_topic", 0)),
        "false_new_rate": rate(false_new, actual_not_new),
        "false_switch_rate": rate(false_switch, actual_not_switch),
        "rows": rows,
    }
