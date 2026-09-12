"""Eval runner: run topic + retrieval evals and print metrics JSON.

Usage:  python -m agent.eval.run [--baseline]
Deterministic and offline (no network, no paid models).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agent.eval.retrieval_eval import evaluate as eval_retrieval
from agent.eval.retrieval_eval import load_cases as load_retrieval
from agent.eval.topic_eval import evaluate as eval_topic
from agent.eval.topic_eval import load_cases as load_topic
from agent.eval.anchor_eval import evaluate as eval_anchor
from agent.eval.anchor_eval import load_cases as load_anchor
from agent.eval.anchor_eval import public_metrics as anchor_public

# run.py = backend/src/agent/eval/run.py → parents[3] = backend
EVALS_DIR = Path(__file__).resolve().parents[3] / "evals"


def run_all() -> dict:
    topic = eval_topic(load_topic(EVALS_DIR / "topic_prediction" / "cases.jsonl"))
    retrieval = eval_retrieval(load_retrieval(EVALS_DIR / "retrieval" / "cases.jsonl"))
    anchor = eval_anchor(
        load_anchor(EVALS_DIR / "anchor_continuation" / "cases.jsonl")
    )
    topic.pop("rows", None)
    retrieval.pop("rows", None)
    return {
        "topic_prediction": topic,
        "retrieval": retrieval,
        "anchor_continuation": anchor_public(anchor),
    }


def main() -> None:
    metrics = run_all()
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    if "--baseline" in sys.argv:
        out = EVALS_DIR / "baseline.json"
        out.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote baseline → {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
