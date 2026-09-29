"""跑批器：把三条臂跑在同一份压力语料上，输出可对照的数字。

用法：

    python -m agent.eval.stress_run --arm both --n-memories 10000 --n-queries 1000
    python -m agent.eval.stress_run --arm local --out evals/stress_local.json
    python -m agent.eval.stress_run --arm jev --n-queries 50        # 需要 OPENROUTER_API_KEY

每个工作都给出：整体指标、按难度分层的指标（字面重合 / 部分重合 / 完全不相交）、
以及延迟分位。C 臂的用量与花费也写进结果里。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from agent.eval.stress_corpus import StressCorpus, generate
from agent.eval.stress_metrics import (
    binary_metrics,
    classification_metrics,
    latency_summary,
    ranking_metrics,
    stratify_recall,
)

EVALS_DIR = Path(__file__).resolve().parents[3] / "evals"


def run_arm(arm, corpus: StressCorpus, k: int = 5) -> dict[str, Any]:
    started = time.perf_counter()
    ranked, recall_lat = arm.recall(corpus.recall_cases, k=k)
    topic_rows, topic_lat = arm.topic(corpus.topic_cases)
    entity_rows, entity_lat = arm.entity(corpus.entity_cases)
    tool_rows, tool_lat = arm.tool(corpus.tool_cases)
    dedup_rows, dedup_lat = arm.dedup(corpus.dedup_cases)
    payload = {
        "recall": {
            **stratify_recall(ranked, corpus.recall_cases, k=k),
            "latency_ms": latency_summary(recall_lat),
        },
        "topic": {
            **classification_metrics(topic_rows),
            "latency_ms": latency_summary(topic_lat),
        },
        "entity": {
            **classification_metrics(entity_rows),
            "latency_ms": latency_summary(entity_lat),
        },
        "tool": {
            **ranking_metrics(tool_rows, k=k),
            "latency_ms": latency_summary(tool_lat),
        },
        "dedup": {
            **binary_metrics(dedup_rows),
            "latency_ms": latency_summary(dedup_lat),
        },
    }
    note = getattr(arm, "note", "")
    if note:
        payload["backend"] = note
    summary = getattr(arm, "usage", None)
    if callable(summary):
        payload["usage"] = summary()
    payload["wall_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return payload


def run_all(corpus: StressCorpus, arms: list, k: int = 5) -> dict[str, Any]:
    return {arm.name: run_arm(arm, corpus, k=k) for arm in arms}


def build_arms(which: str, corpus: StressCorpus):
    from agent.eval.stress_arms import LocalEmbeddingArm, RulesArm

    arms: list = []
    if which in ("rules", "both"):
        arms.append(RulesArm(corpus))
    if which in ("local", "both"):
        arms.append(LocalEmbeddingArm(corpus))
    if which == "jev":
        raise SystemExit(
            "jev 臂尚未接入：先实现 JevArm（见 docs/superpowers/plans/2026-09-23-six-jobs-benchmark.md）"
        )
    return arms


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="六项工作的三方对照跑批器")
    parser.add_argument("--arm", choices=("rules", "local", "both", "jev"), default="both")
    parser.add_argument("--n-memories", type=int, default=10_000)
    parser.add_argument("--n-topics", type=int, default=200)
    parser.add_argument("--n-queries", type=int, default=1_000)
    parser.add_argument("--seed", type=int, default=20260923)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--out", default="")
    return parser.parse_args(sys.argv[1:] if argv is None else argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    corpus = generate(
        seed=args.seed,
        n_memories=args.n_memories,
        n_topics=args.n_topics,
        n_queries=args.n_queries,
    )
    arms = build_arms(args.arm, corpus)
    payload = run_all(corpus, arms, k=args.k)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.out:
        out = Path(args.out)
        if not out.is_absolute():
            out = EVALS_DIR / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote → {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
