# -*- coding: utf-8 -*-
"""话题阈值曲线评测（真实 ONNX / 确定性 fake 两档）。

为什么需要它：TopicPolicy 的历史默认值（new_topic_threshold=0.7，一条绝对余弦
同时管「延续 / 切换 / 新建」）此前只有 12 条 fake-embedding 用例在观测。
这里用**扩充后的真实语料**（>100 条，覆盖延续 / 措辞变化 / 跨话题引用 / 真新
话题 / 对抗性新话题 / 模糊转移 / 无信号 / 中文短句 / 英文 / 中英混合）跑**生产
代码路径**：

    TopicPredictor.predict()  ->  agent.services.affinity.classify(policy=...)

policy 是可注入的 TopicPolicy（生产默认就是 params.TOPIC），所以这里扫的就是
产品真正会执行的那份逻辑，不存在「评测里另写一条」。

用法（backend 目录下）：
    uv run --frozen python evals/topic_threshold_curve.py                 # 真实模型 + 曲线
    uv run --frozen python evals/topic_threshold_curve.py --record        # 记录真实余弦快照
    uv run --frozen python evals/topic_threshold_curve.py --fake          # 确定性 fake（离线）
    uv run --frozen python evals/topic_threshold_curve.py --json out.json

模型发现顺序：QIO_MODEL_DIR -> 常见 QIO 数据目录下的 models/bge-small-zh-v1.5。
不联网、不需要 API Key；没有模型时用 --fake 仍可复现（结论只对 fake 成立）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src"))

CASES_PATH = Path(__file__).with_name("topic_threshold") / "cases.jsonl"
SNAPSHOT_PATH = Path(__file__).with_name("topic_threshold") / "scores_onnx.json"
CURVE_PATH = Path(__file__).with_name("topic_threshold") / "curve_onnx.json"

MODEL_DIRS = (
    Path(os.environ["QIO_MODEL_DIR"]) if os.environ.get("QIO_MODEL_DIR") else None,
    Path(os.environ.get("LOCALAPPDATA", "")) / "Packages" / "OpenAI.Codex_2p2nqsd0c76g0"
    / "LocalCache" / "Roaming" / "qio" / "models" / "bge-small-zh-v1.5",
    Path(os.environ.get("APPDATA", "")) / "qio" / "models" / "bge-small-zh-v1.5",
    Path.home() / ".qio" / "models" / "bge-small-zh-v1.5",
)

MODES = ("in_topic", "switch", "new_topic")


@dataclass
class Fingerprint:
    topic_id: str
    title: str
    keywords: list
    summary_preview: str = ""


class StubTopics:
    def __init__(self, fps):
        self._fps = fps

    def list_with_fingerprints(self):
        return self._fps


class CachedEmbedding:
    """真的 embedding 后端 + topic 向量表 + 文本级记忆化。

    与生产一致：topic_vector 缺失时由 TopicPredictor 调 update_topic_vector
    冷启动生成（用的就是 predict.py 里 _fingerprint_text 的文本）。
    记忆化只影响速度，不改变数值（embedding 是确定性函数）。
    """

    def __init__(self, inner):
        self.inner = inner
        self._topic_vectors: dict = {}
        self._text_cache: dict = {}

    def available(self) -> bool:
        return self.inner.available()

    def embed_texts(self, texts):
        key = tuple(texts)
        if key not in self._text_cache:
            self._text_cache[key] = self.inner.embed_texts(list(texts))
        return self._text_cache[key]

    def topic_vector(self, topic_id):
        return self._topic_vectors.get(topic_id)

    def update_topic_vector(self, topic_id, text):
        vectors = self.embed_texts([text])
        if vectors is not None:
            self._topic_vectors[topic_id] = vectors[0]


def find_model_dir():
    for candidate in MODEL_DIRS:
        if candidate and (candidate / "model.onnx").exists() and (candidate / "tokenizer.json").exists():
            return candidate
    return None


def real_embedding(model_dir):
    from agent.selector.onnx import OnnxEmbeddingBackend

    backend = OnnxEmbeddingBackend.__new__(OnnxEmbeddingBackend)
    backend.model_dir = Path(model_dir)
    backend.max_len = 512
    backend.threads = 8
    backend.model_name = "bge-small-zh-v1.5"
    backend.model_identity = ""
    backend.model_file = ""
    backend.dims = 512
    backend._session = None
    backend._tokenizer = None
    backend._vectors = {}
    backend._matrix = None
    backend._matrix_keys = []
    backend._matrix_dirty = True
    backend._load()
    if not backend.available():
        raise SystemExit(f"ONNX 模型不可用：{model_dir}")
    return backend


def fake_embedding():
    """确定性 bag-of-token embedding（与 agent/eval/topic_eval.py 的同名实现一致）。"""
    import hashlib

    import numpy as np

    from agent.selector.tokenize import tokenize

    class Fake:
        dims = 64

        def available(self):
            return True

        def _vec(self, text):
            v = np.zeros(self.dims, dtype=np.float32)
            for tok in tokenize(text):
                bucket = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16) % self.dims
                v[bucket] += 1.0
            n = float(np.linalg.norm(v)) or 1e-9
            return v / n

        def embed_texts(self, texts):
            if not texts:
                return None
            return np.stack([self._vec(t) for t in texts])

    return Fake()


def load_cases(path=CASES_PATH):
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def fingerprints_of(case):
    return [
        Fingerprint(t["id"], t.get("title", ""), t.get("keywords", []), t.get("summary_preview", ""))
        for t in case["topics"]
    ]


def production_decision(case, embedding, policy):
    """跑生产路径：TopicPredictor.predict() + affinity.classify(policy=...)。"""
    from agent.services.affinity import classify
    from agent.services.predict import TopicPredictor

    fps = fingerprints_of(case)
    predictor = TopicPredictor(
        None,
        embedding,
        StubTopics(fps),
        new_topic_threshold=policy.new_topic_threshold,
        switch_delta=policy.switch_delta,
        aux_top_count=policy.aux_top_count,
        aux_topic_threshold=policy.aux_topic_threshold,
        rules_new_topic_threshold=policy.rules_new_topic_threshold,
        rules_aux_topic_threshold=policy.rules_aux_topic_threshold,
    )
    prediction = predictor.predict(case["message"], current_topic_id=case.get("current_topic"))
    decision = classify(
        case["message"], prediction, case.get("current_topic"), [], policy=policy
    )
    return decision.mode.value, prediction, decision


def evaluate(cases, embedding, policy, collect_rows=False):
    confusion = {a: {b: 0 for b in MODES} for a in MODES}
    per_category: dict = {}
    rows = []
    for case in cases:
        expected = case["expected"]
        predicted, prediction, decision = production_decision(case, embedding, policy)
        confusion[expected][predicted] += 1
        cat = per_category.setdefault(case["category"], {"n": 0, "ok": 0, "wrong": []})
        cat["n"] += 1
        if predicted == expected:
            cat["ok"] += 1
        else:
            cat["wrong"].append({"id": case["id"], "expected": expected, "predicted": predicted})
        if collect_rows:
            scores = dict(prediction.scores or {})
            top = max(scores, key=scores.get) if scores else None
            rows.append({
                "id": case["id"],
                "category": case["category"],
                "expected": expected,
                "predicted": predicted,
                "current_score": round(scores.get(case.get("current_topic"), 0.0), 4),
                "top": top,
                "top_score": round(scores[top], 4) if top else 0.0,
            })
    n = len(cases) or 1
    ok = sum(confusion[a][a] for a in MODES)
    pred_new = sum(confusion[a]["new_topic"] for a in MODES)
    act_new = sum(confusion["new_topic"][b] for b in MODES)
    false_new = pred_new - confusion["new_topic"]["new_topic"]

    def rate(num, den):
        return round(num / den, 4) if den else 0.0

    out = {
        "policy": {
            "new_topic_threshold": policy.new_topic_threshold,
            "incumbent_threshold": policy.incumbent_threshold,
            "switch_threshold": policy.switch_threshold,
            "switch_delta": policy.switch_delta,
            "new_topic_strict": policy.new_topic_strict,
        },
        "n": len(cases),
        "accuracy": rate(ok, n),
        "confusion": confusion,
        "in_topic_recall": rate(confusion["in_topic"]["in_topic"], sum(confusion["in_topic"].values())),
        "switch_recall": rate(confusion["switch"]["switch"], sum(confusion["switch"].values())),
        "new_topic_precision": rate(confusion["new_topic"]["new_topic"], pred_new),
        "new_topic_recall": rate(confusion["new_topic"]["new_topic"], act_new),
        "false_new_rate": rate(false_new, n - act_new),
        "per_category": {k: {"n": v["n"], "ok": v["ok"], "accuracy": rate(v["ok"], v["n"])} for k, v in per_category.items()},
    }
    out["wrong"] = [
        {"id": wid["id"], "expected": wid["expected"], "predicted": wid["predicted"]}
        for cat in per_category.values() for wid in cat["wrong"]
    ]
    if collect_rows:
        out["rows"] = rows
    return out


def print_report(result, title):
    pol = result["policy"]
    print(f"## {title}")
    print(f"   policy: new_topic={pol['new_topic_threshold']} incumbent={pol['incumbent_threshold']} "
          f"switch={pol['switch_threshold']} delta={pol['switch_delta']}")
    print(f"   accuracy={result['accuracy']}  in_topic_recall={result['in_topic_recall']}  "
          f"switch_recall={result['switch_recall']}  new_topic_recall={result['new_topic_recall']}  "
          f"false_new={result['false_new_rate']}")
    print(f"   confusion(exp->pred): {json.dumps(result['confusion'], ensure_ascii=False)}")
    print("   per-category:", json.dumps(
        {k: f"{v['ok']}/{v['n']}" for k, v in sorted(result["per_category"].items())}, ensure_ascii=False))
    if result["wrong"]:
        print("   误判：", json.dumps(result["wrong"], ensure_ascii=False))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", action="store_true", help="用确定性 fake embedding（离线可复现）")
    ap.add_argument("--record", action="store_true", help="把真实余弦写入 scores_onnx.json")
    ap.add_argument("--json", default="", help="把曲线结果写到指定路径")
    ap.add_argument("--grid", default="", help="incumbent_threshold 网格，默认 0.25..0.45 步长 0.05")
    args = ap.parse_args()

    grid = [round(0.25 + 0.05 * i, 2) for i in range(5)]
    if args.grid:
        grid = [float(x) for x in args.grid.split(",") if x.strip()]

    from agent.services.params import TOPIC, TopicPolicy

    cases = load_cases()
    if args.fake:
        embedding = CachedEmbedding(fake_embedding())
        model_label = "fake-bag-of-tokens(64d)"
    else:
        model_dir = find_model_dir()
        if model_dir is None:
            print("找不到真实 ONNX 模型；用 --fake 或设置 QIO_MODEL_DIR", file=sys.stderr)
            return 2
        backend = real_embedding(model_dir)
        embedding = CachedEmbedding(backend)
        model_label = f"{backend.model_identity} @ {model_dir}"

    # 指纹一致性自检：同一个 topic_id 在所有用例里的指纹文本必须相同，
    # 否则 CachedEmbedding 的 topic 向量缓存会串味（评测结果不可信）。
    seen: dict = {}
    for case in cases:
        for t in case["topics"]:
            key = (t["id"], t.get("title", ""), tuple(t.get("keywords", [])), t.get("summary_preview", ""))
            seen.setdefault(t["id"], key)
            assert seen[t["id"]] == key, f"topic {t['id']} 的指纹在不同用例里不一致"

    print(f"# 话题判定曲线  model={model_label}  cases={len(cases)}")
    print(f"# 生产默认 policy: {TOPIC}")
    print()

    legacy = evaluate(cases, embedding, TOPIC, collect_rows=True)
    print_report(legacy, "生产默认值（历史：new_topic_threshold=0.7 / 单阈值）")
    print()

    print(f"# incumbent_threshold 曲线（switch_threshold={TOPIC.switch_threshold} 固定）")
    print(f"{'inc':>5} {'acc':>6} {'in_topic':>9} {'switch':>7} {'new_R':>7} {'false_new':>9}")
    curve = []
    for inc in grid:
        pol = replace(TOPIC, incumbent_threshold=inc)
        m = evaluate(cases, embedding, pol)
        curve.append(m)
        print(f"{inc:>5.2f} {m['accuracy']:>6.3f} {m['in_topic_recall']:>9.3f} {m['switch_recall']:>7.3f} "
              f"{m['new_topic_recall']:>7.3f} {m['false_new_rate']:>9.3f}")

    payload = {
        "model": model_label,
        "n": len(cases),
        "production_default": legacy,
        "incumbent_curve": curve,
    }
    if args.json:
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n曲线结果 -> {args.json}", file=sys.stderr)
    if args.record and not args.fake:
        snapshot_cases = {}
        for case in cases:
            _, prediction, _ = production_decision(case, embedding, TOPIC)
            snapshot_cases[case["id"]] = {
                "scores": {k: round(float(v), 6) for k, v in (prediction.scores or {}).items()},
                "current": case.get("current_topic"),
            }
        SNAPSHOT_PATH.write_text(json.dumps({
            "model": model_label,
            "provenance": "由 evals/topic_threshold_curve.py --record 生成：真实 bge-small-zh-v1.5 ONNX 余弦",
            "cases": snapshot_cases,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n记录余弦快照 -> {SNAPSHOT_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
