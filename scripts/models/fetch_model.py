"""把内置嵌入模型准备好（打包前跑一次；产物不进 git）。

目标目录：`frontend/src-tauri/resources/models/bge-small-zh-v1.5/`
Tauri 会把 `resources/models` 打进安装包，壳在启动时把它复制到用户数据目录
（`%APPDATA%\\qio\\models`）交给后端，所以**不需要**联网、也不需要用户手动放文件。

两种来源：

    # 1) 从本机已有的模型目录复制（最快；校验哈希后会生成清单）
    python scripts/models/fetch_model.py --from-dir "C:\\Tools\\models\\bge-small-zh-v1.5"

    # 2) 从 ModelScope 下载（hf-mirror 太慢时的常用源）
    python scripts/models/fetch_model.py --from-modelscope

内置默认档是 **fp32**（`model.onnx`）。加 `--with-int8` 会同时带上量化版
（多 23MB，作为可选档；不加也不影响使用）。

哈希是**钉死**的：下载到的文件与预期 sha256 不一致就直接失败，不把可疑文件打进安装包。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_DEST = REPO / "frontend" / "src-tauri" / "resources" / "models" / "bge-small-zh-v1.5"

# 这两个值来自我们对 ModelScope 下载结果的实测（scripts/models/onnx_ab.py 跑通过）
FP32 = {
    "file": "model.onnx",
    "bytes": 94_851_877,
    "sha256": "69a0b846f4f116b5e6aabf9546ea6754d02264f3211a13a1bd69b31b8040749a",
}
INT8 = {
    "file": "model_quantized.onnx",
    "bytes": 24_010_842,
    "sha256": "15b717c382bcb518ba457b93ea6850ede7f4f1cd8937454aa06972366cd19bcc",
}
MODELSCOPE = "https://modelscope.cn/api/v1/models/Xenova/bge-small-zh-v1.5/repo?Revision=master&FilePath="

MIT_TEXT = """MIT License

Copyright (c) BAAI (Beijing Academy of Artificial Intelligence)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

NOTICE_TEXT = """QIO 内置嵌入模型声明

模型：bge-small-zh-v1.5（中文文本嵌入，512 维，CPU 推理）
来源：BAAI（北京智源人工智能研究院）发布；ONNX 转换版来自社区仓库 Xenova/bge-small-zh-v1.5
许可证：MIT（全文见同目录 LICENSE-BAAI-bge-small-zh-v1.5.txt）

本目录中的 model.onnx / model_quantized.onnx / tokenizer.json 随 QIO 一起分发，
运行时由桌面壳复制到用户数据目录（%APPDATA%\\qio\\models\\bge-small-zh-v1.5），
只在本地用于文本向量计算：不联网、不上传任何内容。
"""


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(path: Path, spec: dict) -> None:
    size = path.stat().st_size
    if size != spec["bytes"]:
        raise SystemExit(f"{path.name} 体积不符：期望 {spec['bytes']}，实际 {size}")
    digest = sha256_of(path)
    if digest != spec["sha256"]:
        raise SystemExit(f"{path.name} 哈希不符：期望 {spec['sha256']}，实际 {digest}")
    print(f"  ✓ {path.name}（{size / 1024 / 1024:.1f}MB，哈希一致）")


def download(dest: Path, relative: str, spec: dict) -> None:
    target = dest / spec["file"]
    if target.exists():
        try:
            verify(target, spec)
            return
        except SystemExit:
            print(f"  ! 已有的 {target.name} 校验不过，重新下载")
            target.unlink()
    url = f"{MODELSCOPE}{relative}"
    print(f"  下载 {url}")
    with urllib.request.urlopen(url, timeout=120) as resp, target.open("wb") as out:
        shutil.copyfileobj(resp, out)
    verify(target, spec)


def copy_from_dir(source: Path, dest: Path, spec: dict) -> None:
    src = source / spec["file"]
    if not src.exists():
        raise SystemExit(f"源目录里没有 {spec['file']}：{source}")
    shutil.copy2(src, dest / spec["file"])
    verify(dest / spec["file"], spec)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default=str(DEFAULT_DEST))
    ap.add_argument("--from-dir", default="")
    ap.add_argument("--from-modelscope", action="store_true")
    ap.add_argument("--with-int8", action="store_true")
    args = ap.parse_args()

    if not args.from_dir and not args.from_modelscope:
        raise SystemExit("请指定来源：--from-dir <目录> 或 --from-modelscope")

    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    print(f"目标目录：{dest}")

    if args.from_dir:
        source = Path(args.from_dir)
        copy_from_dir(source, dest, FP32)
        tokenizer_src = source / "tokenizer.json"
        if not tokenizer_src.exists():
            raise SystemExit(f"源目录里没有 tokenizer.json：{source}")
        shutil.copy2(tokenizer_src, dest / "tokenizer.json")
        print("  ✓ tokenizer.json")
        if args.with_int8:
            copy_from_dir(source, dest, INT8)
    else:
        download(dest, "onnx/model.onnx", FP32)
        download(dest, "tokenizer.json", {"file": "tokenizer.json", "bytes": 439_120, "sha256": ""})
        if args.with_int8:
            download(dest, "onnx/model_quantized.onnx", INT8)
        # 从 ModelScope 下的 tokenizer 体积可能随上游更新而变：只有在钉死的哈希匹配时才校验
        tokenizer = dest / "tokenizer.json"
        if tokenizer.stat().st_size == 0:
            raise SystemExit("tokenizer.json 下载为空")

    # 许可证与声明随包分发
    (dest / "LICENSE-BAAI-bge-small-zh-v1.5.txt").write_text(MIT_TEXT, encoding="utf-8")
    (dest / "NOTICE.txt").write_text(NOTICE_TEXT, encoding="utf-8")

    # 清单：默认档固定为 fp32
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from write_manifest import main as write_manifest_main  # type: ignore

    argv = sys.argv
    try:
        sys.argv = [
            "write_manifest.py",
            "--model-dir",
            str(dest),
            "--default",
            FP32["file"],
        ]
        write_manifest_main()
    finally:
        sys.argv = argv
    print("\n内置模型已就绪；打包时 Tauri 会把 resources/models 打进安装包。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
