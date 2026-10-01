# -*- coding: utf-8 -*-
"""检索排序的臂对比（生产代码 + 真实 ONNX embedding，离线可复现）。

臂（arm）：
  A 两层排序      当前生产：Selector 先加规则分并截断，Retriever 再加权排序
  B 单层统一排序  候选只带原始相关度，业务排序只发生一次
  C 纯相关性     单层 + 所有奖励权重为 0（相关度即分数）
  D 词面 baseline BM25 召回 + 纯相关性
  E 混合召回      BM25 + 向量 RRF 融合 + 纯相关性

指标：Recall@1 / Recall@5 / MRR / wrong-memory 注入率 / stale-memory 注入率
      + 分类别（cross_topic / person_entity / fact_update / paraphrase ...）Recall@1。

用法（backend 目录下）：
    uv run --frozen python evals/retrieval_ranking/run_arms.py --arm A
    uv run --frozen python evals/retrieval_ranking/run_arms.py --all --json evals/retrieval_ranking/arms_before.json

不联网；没有 ONNX 模型时 embedding 臂直接报错退出（不静默换成 BM25）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND / "src"))

HERE = Path(__file__).parent
CORPUS = HERE / "corpus.json"

MODEL_DIRS = (
    Path(os.environ["QIO_MODEL_DIR"]) if os.environ.get("QIO_MODEL_DIR") else None,
    Path(os.environ.get("LOCALAPPDATA", "")) / "Packages" / "OpenAI.Codex_2p2nqsd0c76g0"
    / "LocalCache" / "Roaming" / "qio" / "models" / "bge-small-zh-v1.5",
    Path(os.environ.get("APPDATA", "")) / "qio" / "models" / "bge-small-zh-v1.5",
    Path.home() / ".qio" / "models" / "bge-small-zh-v1.5",
)


class StubTopics:
    def list_with_fingerprints(self):
        return []


def _memo_onnx_class():
    """带查询向量记忆化的 ONNX 后端：纯粹为了评测跑得快。

    embed_texts 是确定性函数，记忆化不改变任何数值；扫描几十组权重时
    重复算同一批查询的 embedding 是纯浪费。
    """
    from agent.selector.onnx import OnnxEmbeddingBackend

    class MemoOnnx(OnnxEmbeddingBackend):
        def __init__(self, *args, **kwargs):
            self._memo: dict = {}
            super().__init__(*args, **kwargs)

        def embed_texts(self, texts):
            key = tuple(texts)
            if key not in self._memo:
                self._memo[key] = super().embed_texts(list(texts))
            return self._memo[key]

    return MemoOnnx


_MemoOnnx = None  # 延迟构造：import 时不能依赖 onnxruntime


def _memo_onnx(conn, model_dir):
    global _MemoOnnx
    if _MemoOnnx is None:
        _MemoOnnx = _memo_onnx_class()
    return _MemoOnnx(conn, model_dir)


def find_model_dir():
    for candidate in MODEL_DIRS:
        if candidate and (candidate / "model.onnx").exists() and (candidate / "tokenizer.json").exists():
            return candidate
    return None


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


#: 臂 = (召回后端, 排序权重(相对 relevance 归一), 说明)
#: 权重顺序 (relevance, recency, affinity, rule)；relevance 固定 1.0，
#: 其余按「相对相关度的倍数」给出，便于和旧配置的 0.4/0.25/0.35 对照
#: （旧比例 = rec 0.625 / aff 0.875）。
ARMS: dict[str, tuple[str, tuple[float, float, float, float], str]] = {
    "C_relevance": ("vector", (1.0, 0.0, 0.0, 0.0), "单层 + 纯相关性（生产默认）"),
    "B_single": ("vector", (1.0, 0.0, 0.0, 0.0), "单层统一排序（默认参数，等同 C）"),
    "D_bm25": ("bm25", (1.0, 0.0, 0.0, 0.0), "词面 baseline"),
    "E_hybrid": ("hybrid", (1.0, 0.0, 0.0, 0.0), "BM25 + 向量 RRF 融合"),
    "W_recency": ("vector", (1.0, 0.625, 0.0, 0.0), "+时效（旧比例）"),
    "W_recency_small": ("vector", (1.0, 0.15, 0.0, 0.0), "+时效（小权重）"),
    "W_affinity": ("vector", (1.0, 0.0, 0.875, 0.0), "+话题亲和（旧比例）"),
    "W_rule": ("vector", (1.0, 0.0, 0.0, 0.25), "+规则分项（anchor/entity/keyword）"),
    "W_legacy_ratio": ("vector", (1.0, 0.625, 0.875, 0.0), "旧的 rec+aff 比例放进单层"),
    "W_all": ("vector", (1.0, 0.3, 0.3, 0.2), "所有维度都给一个小权重"),
}

#: 生产默认（不传权重，直接吃 params.RETRIEVAL）——用来证明「跑的确实是生产配置」
def production_weights() -> tuple[float, float, float, float]:
    from agent.services.params import RETRIEVAL

    return (
        RETRIEVAL.relevance_weight,
        RETRIEVAL.recency_weight,
        RETRIEVAL.affinity_weight,
        RETRIEVAL.rule_weight,
    )


ARMS["P_default_vector"] = ("vector", None, "生产默认配置 + 向量召回")
ARMS["P_default_bm25"] = ("bm25", None, "生产默认配置 + BM25（CI 可跑，无需模型）")


def build(corpus: dict, arm: str):
    """按臂装配生产组件。所有臂都只用产品自己的类，评测里没有第二条排序实现。"""
    from agent.selector.base import IndexedDoc
    from agent.selector.bm25 import BM25Backend
    from agent.selector.hybrid import HybridBackend
    from agent.selector.selector import Selector
    from agent.services.decay import DecayPolicy
    from agent.services.retrieval import RetrievalConfig, Retriever
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations

    tmpdir = Path(tempfile.mkdtemp(prefix="qio-ranking-eval-"))
    conn = connect(tmpdir / "eval.db")
    apply_migrations(conn)

    docs = [
        IndexedDoc(
            doc_id=d["id"],
            text=d["text"],
            topic_id=d.get("topic"),
            keywords=d.get("keywords", []),
            created_at=_iso(d.get("created_days_ago", 0.0)),
        )
        for d in corpus["docs"]
    ]

    if arm not in ARMS:
        raise SystemExit(f"未知臂 {arm}；可选：{', '.join(ARMS)}")
    recall_kind, weights, _note = ARMS[arm]

    bm25 = BM25Backend()
    if recall_kind in ("vector", "hybrid"):
        model_dir = find_model_dir()
        if model_dir is None:
            raise SystemExit("找不到 ONNX 模型；embedding 臂不做静默降级")
        vector = _memo_onnx(conn, model_dir)
        if not vector.available():
            raise SystemExit("ONNX 后端不可用")
        recall = vector if recall_kind == "vector" else HybridBackend([vector, bm25])
    elif recall_kind == "bm25":
        recall = bm25
    else:
        raise SystemExit(f"未知召回后端 {recall_kind}")

    selector = Selector(recall=recall, fallback_recall=bm25)
    selector.load(docs, titles={d["id"]: d.get("title", "") for d in corpus["docs"]})
    if weights is None:
        config = RetrievalConfig()  # 完全走生产默认（params.RETRIEVAL）
    else:
        config = RetrievalConfig(
            relevance_weight=weights[0],
            recency_weight=weights[1],
            affinity_weight=weights[2],
            rule_weight=weights[3],
        )
    retriever = Retriever(selector, StubTopics(), config=config, conn=conn, decay=DecayPolicy())
    ages = {d["id"]: d.get("created_days_ago", 0.0) for d in corpus["docs"]}
    kinds = {d["id"]: d.get("kind", "ephemeral") for d in corpus["docs"]}
    retriever._created_at = lambda doc_id: _iso(ages.get(doc_id, 0.0))  # type: ignore[assignment]
    retriever.kind_of = lambda doc_id: kinds.get(doc_id, "ephemeral")  # type: ignore[assignment]
    retriever._entity_card_hits = lambda query, top_k: []  # type: ignore[assignment]
    return retriever


def rank(arm: str, corpus: dict, top_k: int = 5):
    retriever = build(corpus, arm)
    out = {}
    for query in corpus["queries"]:
        hits = retriever.search(query["query"], anchor_topic_id=query.get("topic"), top_k=top_k)
        out[query["id"]] = [h.doc_id for h in hits]
    return out


def measure(corpus: dict, ranked: dict, top_k: int = 5) -> dict:
    by_cat: dict = {}
    totals = {"n": 0, "r1": 0, "r5": 0, "rr": 0.0, "wrong": 0, "stale": 0}
    for query in corpus["queries"]:
        expected = set(query["expected"])
        stale = set(query.get("stale") or [])
        got = ranked[query["id"]]
        hit1 = bool(set(got[:1]) & expected)
        hit5 = bool(set(got[:top_k]) & expected)
        rr = 0.0
        for i, doc_id in enumerate(got, start=1):
            if doc_id in expected:
                rr = 1.0 / i
                break
        first_expected = next((i for i, d in enumerate(got) if d in expected), None)
        stale_above = any(
            d in stale and (first_expected is None or i < first_expected) for i, d in enumerate(got)
        )
        totals["n"] += 1
        totals["r1"] += hit1
        totals["r5"] += hit5
        totals["rr"] += rr
        totals["wrong"] += 0 if hit1 else 1
        totals["stale"] += 1 if stale_above else 0
        cat = by_cat.setdefault(query["category"], {"n": 0, "r1": 0, "r5": 0, "rr": 0.0})
        cat["n"] += 1
        cat["r1"] += hit1
        cat["r5"] += hit5
        cat["rr"] += rr

    n = totals["n"] or 1

    def rate(num, den):
        return round(num / den, 4) if den else 0.0

    return {
        "n": totals["n"],
        "recall@1": rate(totals["r1"], n),
        "recall@5": rate(totals["r5"], n),
        "mrr": round(totals["rr"] / n, 4),
        "wrong_memory_injection_rate": rate(totals["wrong"], n),
        "stale_memory_injection_rate": rate(totals["stale"], n),
        "by_category": {
            k: {"n": v["n"], "recall@1": rate(v["r1"], v["n"]), "recall@5": rate(v["r5"], v["n"]),
                "mrr": round(v["rr"] / (v["n"] or 1), 4)}
            for k, v in sorted(by_cat.items())
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", default="C_relevance", help="臂名（见 ARMS）")
    ap.add_argument("--all", action="store_true", help="跑所有臂")
    ap.add_argument("--json", default="", help="把结果写到指定路径")
    ap.add_argument("--top-k", type=int, default=5)
    args = ap.parse_args()

    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    arms = list(ARMS) if args.all else [args.arm]
    print(f"# 检索排序臂对比  corpus=docs {len(corpus['docs'])} / queries {len(corpus['queries'])}")
    print(f"{'arm':<18} {'R@1':>6} {'R@5':>6} {'MRR':>6} {'wrong':>7} {'stale':>7}  note")
    results = {}
    for arm in arms:
        ranked = rank(arm, corpus, top_k=args.top_k)
        metrics = measure(corpus, ranked, top_k=args.top_k)
        results[arm] = metrics
        print(f"{arm:<18} {metrics['recall@1']:>6.3f} {metrics['recall@5']:>6.3f} {metrics['mrr']:>6.3f} "
              f"{metrics['wrong_memory_injection_rate']:>7.3f} {metrics['stale_memory_injection_rate']:>7.3f}"
              f"  {ARMS[arm][2]}")
    for arm, metrics in results.items():
        print(f"\n## 臂 {arm} 分类别")
        for cat, st in metrics["by_category"].items():
            print(f"   {cat:<16} n={st['n']:<3} R@1={st['recall@1']:.3f} R@5={st['recall@5']:.3f} MRR={st['mrr']:.3f}")
    if args.json:
        Path(args.json).write_text(
            json.dumps({"arms": results, "top_k": args.top_k}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n结果 -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
