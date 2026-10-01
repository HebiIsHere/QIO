# -*- coding: utf-8 -*-
"""用真实 ONNX 模型记录「用例 id -> 每个话题的余弦」快照（离线可复现的评测输入）。

为什么要有它：agent/eval/topic_eval.py 里的 FakeEmbedding 是 bag-of-tokens 的
假几何体，它会把「继续聊 SQLite 迁移」对数据库话题打成 0.2946，而生产真正用的
bge-small-zh-v1.5 给 0.5259 —— 用假 embedding 评 embedding 分支，测的就不是产品
会走的路径。快照把**真实 embedding 的输出**固定下来：CI 上不需要 90MB 模型，
也不会联网，判定的仍然是生产代码（TopicPredictor._rank + affinity.classify）。

指纹文本用的是生产函数 agent.services.predict.TopicPredictor._fingerprint_text，
所以快照与线上打分口径一致。

用法（backend 目录下）：
    uv run --frozen python evals/record_topic_scores.py                 # 12 条旧集
    uv run --frozen python evals/record_topic_scores.py \
        --cases evals/topic_threshold/cases.jsonl \
        --out evals/topic_threshold/scores_onnx.json
    uv run --frozen python evals/record_topic_scores.py --check         # 只比对漂移，不写
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src"))

MODEL_DIRS = (
    Path(os.environ["QIO_MODEL_DIR"]) if os.environ.get("QIO_MODEL_DIR") else None,
    Path(os.environ.get("LOCALAPPDATA", "")) / "Packages" / "OpenAI.Codex_2p2nqsd0c76g0"
    / "LocalCache" / "Roaming" / "qio" / "models" / "bge-small-zh-v1.5",
    Path(os.environ.get("APPDATA", "")) / "qio" / "models" / "bge-small-zh-v1.5",
    Path.home() / ".qio" / "models" / "bge-small-zh-v1.5",
)


@dataclass
class Fingerprint:
    topic_id: str
    title: str
    keywords: list
    summary_preview: str = ""


def find_model_dir() -> Path | None:
    for candidate in MODEL_DIRS:
        if candidate and (candidate / "model.onnx").exists() and (candidate / "tokenizer.json").exists():
            return candidate
    return None


def real_embedding(model_dir: Path):
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


def fingerprint_text(fp: Fingerprint, predictor_cls) -> str:
    """与生产一致：标题 + 关键词 + 摘要预览（见 predict._fingerprint_text）。"""
    return predictor_cls._fingerprint_text(None, fp)


def load_cases(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def record(cases, embedding) -> dict:
    import numpy as np

    from agent.services.predict import TopicPredictor

    out = {}
    for case in cases:
        fps = [
            Fingerprint(t["id"], t.get("title", ""), t.get("keywords", []), t.get("summary_preview", ""))
            for t in case.get("topics", [])
        ]
        texts = [fingerprint_text(fp, TopicPredictor) for fp in fps] + [case["message"]]
        vectors = embedding.embed_texts(texts)
        query = vectors[-1]
        scores = {}
        for fp, vec in zip(fps, vectors[:-1]):
            denom = float(np.linalg.norm(query)) * float(np.linalg.norm(vec)) or 1e-9
            scores[fp.topic_id] = round(float(np.dot(query, vec) / denom), 6)
        out[case["id"]] = {"scores": scores, "current": case.get("current_topic")}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(BACKEND / "evals" / "topic_prediction" / "cases.jsonl"))
    ap.add_argument("--out", default=str(BACKEND / "evals" / "topic_prediction" / "scores_onnx.json"))
    ap.add_argument("--check", action="store_true", help="只报告与现有快照的差异，不写文件")
    args = ap.parse_args()

    model_dir = find_model_dir()
    if model_dir is None:
        print("找不到真实 ONNX 模型；设置 QIO_MODEL_DIR 后再跑（CI 不需要跑本脚本）", file=sys.stderr)
        return 2

    backend = real_embedding(model_dir)
    cases = load_cases(Path(args.cases))
    cases = [c for c in cases if c.get("backend") == "recorded"]
    if not cases:
        print("没有 backend=recorded 的用例，什么都不用记", file=sys.stderr)
        return 0
    recorded = record(cases, backend)

    out_path = Path(args.out)
    if args.check:
        current = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {"cases": {}}
        drift = 0
        for cid, entry in recorded.items():
            old = (current.get("cases") or {}).get(cid, {}).get("scores") or {}
            for tid, value in entry["scores"].items():
                if tid not in old or abs(old[tid] - value) > 1e-3:
                    print(f"[DRIFT] {cid}.{tid}: 快照 {old.get(tid)} -> 重算 {value}")
                    drift += 1
        print(f"模型身份：{backend.model_identity}；漂移项 {drift}")
        return 1 if drift else 0

    payload = {
        "model": backend.model_identity,
        "model_file": backend.model_file,
        "model_dir": str(model_dir),
        "provenance": (
            "由 evals/record_topic_scores.py 记录：真实 bge-small-zh-v1.5 ONNX 余弦。"
            "指纹文本 = TopicPredictor._fingerprint_text（title + keywords + summary_preview）"
        ),
        "cases": recorded,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"记录 {len(recorded)} 条 -> {out_path}（model={backend.model_identity}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
