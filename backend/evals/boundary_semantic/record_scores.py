# -*- coding: utf-8 -*-
"""用真实 ONNX 模型记录边界评测需要的语义分数（输入快照，不是标签）。

每条用例记一个数：当前输入 embedding 与「上一段上下文」embedding 的余弦。
run_boundary_arms.py 用它离线复现「规则 + 语义」臂，CI 上不需要模型。

用法（backend 目录下）：uv run --frozen python evals/boundary_semantic/record_scores.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND / "src"))

HERE = Path(__file__).parent
CASES = HERE / "cases.jsonl"
OUT = HERE / "scores_onnx.json"

MODEL_DIRS = (
    Path(os.environ["QIO_MODEL_DIR"]) if os.environ.get("QIO_MODEL_DIR") else None,
    Path(os.environ.get("LOCALAPPDATA", "")) / "Packages" / "OpenAI.Codex_2p2nqsd0c76g0"
    / "LocalCache" / "Roaming" / "qio" / "models" / "bge-small-zh-v1.5",
    Path(os.environ.get("APPDATA", "")) / "qio" / "models" / "bge-small-zh-v1.5",
    Path.home() / ".qio" / "models" / "bge-small-zh-v1.5",
)


def find_model_dir():
    for candidate in MODEL_DIRS:
        if candidate and (candidate / "model.onnx").exists() and (candidate / "tokenizer.json").exists():
            return candidate
    return None


def main() -> int:
    import numpy as np

    from agent.selector.onnx import OnnxEmbeddingBackend

    model_dir = find_model_dir()
    if model_dir is None:
        print("找不到 ONNX 模型；CI 不需要跑本脚本", file=sys.stderr)
        return 2

    backend = OnnxEmbeddingBackend.__new__(OnnxEmbeddingBackend)
    backend.model_dir = model_dir
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

    cases = [json.loads(l) for l in CASES.read_text(encoding="utf-8").splitlines() if l.strip()]
    out = {}
    for case in cases:
        vectors = backend.embed_texts([case["text"], case["recent"]])
        a, b = vectors[0], vectors[1]
        sim = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))
        out[case["id"]] = {"sim": round(sim, 6), "text": case["text"], "recent": case["recent"]}
    OUT.write_text(json.dumps({
        "model": backend.model_identity,
        "provenance": "由 evals/boundary_semantic/record_scores.py 记录：当前输入 vs 上一段上下文的余弦",
        "cases": out,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"记录 {len(out)} 条 -> {OUT}（model={backend.model_identity}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
