"""Eval runner: run topic + retrieval evals and print metrics JSON.

用法：

    python -m agent.eval.run                      # 默认路径（关键词/规则），不需要模型
    python -m agent.eval.run --baseline           # 同上，并把数字写进 baseline.json
    python -m agent.eval.run --embedding onnx     # 改走内置 ONNX 嵌入模型
    python -m agent.eval.run --embedding onnx --out evals/baseline_onnx.json

默认路径确定且离线（不联网、不调用付费模型）。`--embedding onnx` 会用本机
内置模型算一遍，所以结果依赖机器与模型档位；`--baseline` 只允许在默认路径
下使用 —— baseline.json 是测试守着的那份数字，不能被模型跑覆盖。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent.eval.embedding_backend import build_backend, resolve_model_dir
from agent.eval.retrieval_eval import evaluate as eval_retrieval
from agent.eval.retrieval_eval import load_cases as load_retrieval
from agent.eval.topic_eval import evaluate as eval_topic
from agent.eval.topic_eval import load_cases as load_topic
from agent.eval.anchor_eval import evaluate as eval_anchor
from agent.eval.anchor_eval import load_cases as load_anchor
from agent.eval.anchor_eval import public_metrics as anchor_public

# run.py = backend/src/agent/eval/run.py → parents[3] = backend
EVALS_DIR = Path(__file__).resolve().parents[3] / "evals"


def run_all(embedding_factory=None) -> dict:
    """跑三套评测。`embedding_factory` 为空 = 现在的确定性路径（默认）。"""
    topic = eval_topic(
        load_topic(EVALS_DIR / "topic_prediction" / "cases.jsonl"),
        embedding_factory=embedding_factory,
    )
    retrieval = eval_retrieval(
        load_retrieval(EVALS_DIR / "retrieval" / "cases.jsonl"),
        recall_factory=embedding_factory,
    )
    # 锚点延续评测仍未接模型：它守着一个「不实现距离偏置」的决策，
    # 改它的输入会让那份决策失效，属于另一件事。
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


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="QIO 离线评测（话题 / 检索 / 锚点延续）")
    parser.add_argument(
        "--embedding",
        choices=("none", "onnx"),
        default="none",
        help="none=默认确定性路径；onnx=用本机内置嵌入模型跑",
    )
    parser.add_argument("--model-dir", default="", help="内置模型目录（默认按生产同源路径找）")
    parser.add_argument("--baseline", action="store_true", help="把默认路径的数字写进 baseline.json")
    parser.add_argument("--out", default="", help="把本次结果写到指定文件")
    return parser.parse_args(sys.argv[1:] if argv is None else argv)


def _onnx_factory(model_dir, notes: list[str]):
    """每个用例新建一个后端；第一条说明记下来（模型缺失时说明找过哪里）。"""

    def build():
        backend, note = build_backend(model_dir)
        if not notes:
            notes.append(note)
        return backend

    return build


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    if args.baseline and args.embedding != "none":
        raise SystemExit(
            "--baseline 只写默认路径的数字（baseline.json 被测试守着）；"
            "接了模型的运行请用 --out <路径>"
        )

    notes: list[str] = []
    factory = (
        _onnx_factory(args.model_dir or None, notes) if args.embedding == "onnx" else None
    )

    metrics = run_all(embedding_factory=factory)
    payload = dict(metrics)
    if args.embedding != "none":
        payload["_run"] = {
            "embedding": args.embedding,
            "model_dir": str(resolve_model_dir(args.model_dir or None)),
            "notes": notes,
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if args.baseline:
        out = EVALS_DIR / "baseline.json"
        out.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote baseline → {out}", file=sys.stderr)
    if args.out:
        out = Path(args.out)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote → {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
