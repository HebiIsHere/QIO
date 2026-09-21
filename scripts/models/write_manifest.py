"""给模型目录生成 `model_manifest.json`（打包与本地开发共用）。

为什么需要清单：QIO 要能回答「现在用的是哪个模型、哪一档精度」。
清单里写清楚默认档、每一档的文件名、体积与 sha256 —— 启动时按它加载，
并把「模型 + 精度 + 哈希」写进向量身份（换档位 = 换身份，旧向量不会被误用）。

用法：

    python scripts/models/write_manifest.py --model-dir "C:\\Tools\\models\\bge-small-zh-v1.5" \
        --default model.onnx --name bge-small-zh-v1.5 --dims 512 --max-len 512

目录里有哪些 `*.onnx` 就登记哪些；`--default` 指定的那一档必须存在。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

PRECISION_BY_NAME = (
    ("quantized", "int8"),
    ("int8", "int8"),
    ("fp16", "fp16"),
)


def precision_of(name: str) -> str:
    lowered = name.lower()
    for marker, precision in PRECISION_BY_NAME:
        if marker in lowered:
            return precision
    return "fp32"


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--default", default="model.onnx")
    ap.add_argument("--name", default="bge-small-zh-v1.5")
    ap.add_argument("--dims", type=int, default=512)
    ap.add_argument("--max-len", type=int, default=512)
    args = ap.parse_args()

    model_dir = Path(args.model_dir)
    if not model_dir.is_dir():
        raise SystemExit(f"模型目录不存在：{model_dir}")

    files = sorted(model_dir.glob("*.onnx"))
    if not files:
        raise SystemExit(f"目录里没有 .onnx 文件：{model_dir}")
    names = {f.name for f in files}
    if args.default not in names:
        raise SystemExit(f"默认档 {args.default} 不在目录里（现有：{sorted(names)}）")
    if not (model_dir / "tokenizer.json").exists():
        raise SystemExit(f"缺少 tokenizer.json：{model_dir}")

    entries = []
    for path in files:
        print(f"计算 sha256：{path.name}（{path.stat().st_size / 1024 / 1024:.1f}MB）…")
        entries.append(
            {
                "file": path.name,
                "precision": precision_of(path.name),
                "bytes": path.stat().st_size,
                "sha256": sha256_of(path),
            }
        )
    # 默认档排在最前，读起来更直观
    entries.sort(key=lambda e: 0 if e["file"] == args.default else 1)

    manifest = {
        "name": args.name,
        "dims": args.dims,
        "max_len": args.max_len,
        "default": args.default,
        "tokenizer": "tokenizer.json",
        "files": entries,
    }
    out = model_dir / "model_manifest.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {out}")
    for entry in entries:
        print(f"  {entry['precision']:>4}  {entry['file']:<24} {entry['bytes'] / 1024 / 1024:.1f}MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
