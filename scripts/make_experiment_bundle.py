# -*- coding: utf-8 -*-
"""把这次实验打成可分发的 zip。

用法（仓库根目录）：

    python scripts/make_experiment_bundle.py

产物：<仓库上级>/dist/qio-six-jobs-experiment-2026-09-24.zip
包含：完整记录 md、全部实验与测试代码、原始结果、测试数据、脱敏后的真实记忆库、清单与许可说明。
不包含：维基快照原始 parquet（124MB，清单里记了 URL 与哈希，可按需重新下载）。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT.parent / "dist"
NAME = "qio-experiment-round2-2026-09-25"
CACHE = Path(tempfile.gettempdir()) / "qio-stress-cache"

CODE = [
    "backend/src/agent/eval/experiment_log.py",
    "backend/src/agent/eval/real_corpus.py",
    "backend/src/agent/eval/real_run.py",
    "backend/src/agent/eval/stress_corpus.py",
    "backend/src/agent/eval/stress_metrics.py",
    "backend/src/agent/eval/stress_arms.py",
    "backend/src/agent/eval/stress_jev_arm.py",
    "backend/src/agent/eval/stress_rerank.py",
    "backend/src/agent/eval/stress_run.py",
    "backend/src/agent/eval/jev_client.py",
    "backend/src/agent/eval/embedding_backend.py",
    "backend/src/agent/eval/retrieval_eval.py",
    "backend/src/agent/eval/topic_eval.py",
    "backend/src/agent/eval/run.py",
    "backend/tests/test_real_corpus.py",
    "backend/tests/test_experiment_log.py",
    "backend/tests/test_stress_corpus.py",
    "backend/tests/test_stress_metrics.py",
    "backend/tests/test_stress_arms.py",
    "backend/tests/test_stress_jev_arm.py",
    "backend/tests/test_stress_rerank.py",
    "backend/tests/test_stress_run.py",
    "backend/tests/test_jev_client.py",
    "backend/tests/test_eval_embedding_backend.py",
    "scripts/make_experiment_report.py",
]

DOCS = [
    ("docs/superpowers/notes/2026-09-25-correction-and-per-case-log.md", "实验修正与逐条留痕.md"),
    ("docs/superpowers/notes/2026-09-24-six-jobs-experiment-full.md", "第一轮记录（结论已撤回）.md"),
]

RESULTS = [
    "backend/evals/stress_real_arms_200_fixed.json",
    "backend/evals/stress_real_200_fixed.json",
    "backend/evals/stress_real_arms_200.json",
    "backend/evals/stress_real_200.json",
    "backend/evals/stress_rules_vs_local.json",
    "backend/evals/baseline_onnx.json",
]

DATA_FILES = [
    ("questions.jsonl", "600 条真实提问（每条记忆 3 类），按正文哈希缓存"),
    ("real_cases.json", "180 条话题用例 + 120 条去重用例"),
    ("extra_cases.json", "60 条实体用例 + 14 条工具用例"),
    ("wiki_docs.jsonl", "200 篇维基中文文档正文（测试语料来源）"),
]

NOTICE = """# 数据来源与许可

## 语料

`data/wiki_docs.jsonl` 与由它派生的 `data/questions.jsonl`、`data/real_cases.json`、
`data/extra_cases.json` 中的内容，取自维基百科中文版。

- 数据集：`wikimedia/wikipedia`，快照 `20231101.zh`，分片 `train-00002-of-00006.parquet`
- 来源：https://huggingface.co/datasets/wikimedia/wikipedia
- 许可：Creative Commons Attribution-ShareAlike 4.0（CC BY-SA 4.0）
- 归属：维基百科贡献者（Wikipedia contributors）

按 CC BY-SA 4.0 的要求：再分发这些文本（及其派生内容）时必须保留本归属说明，
并以相同许可分发。原始快照 parquet（124MB）未包含在本包内，可按上表地址重新下载。

## 代码

`code/` 下的代码属于 qio 仓库，随本包按原仓库许可分发。

## 模型与调用

本实验调用过两个外部服务：

- OpenRouter 上的 `typesafe/jev-1.13`（判定模型），本包**不含**任何 API Key；
- 用户在 qio 里配置的 DeepSeek 端点（用于生成提问与用例），本包**不含**任何密钥。

`data/store/app.db` 是从实验记忆库复制的副本，已删除 `credentials` 表内容（连元数据也不留），
因此包里没有任何凭据信息。
"""

README = """# 第二轮实验包：正文修正 + 逐条留痕

**先读这一句**：第一轮的产品结论（"本地模型必须用""话题判定做不到""去重没有价值"）
均已撤回。修正正文进入链路后，BM25 在召回上反而更强（0.877 对 0.813）。

主文档：`实验修正与逐条留痕.md`；第一轮记录保留为 `第一轮记录（结论已撤回）.md`。

## 与第一轮包的差别

- `data/store-fixed/app.db` 是修正后的记忆库（200/200 片段正文非空）；
  `data/store/app.db` 是旧的标题版，仅作对照。
- `runs/<run_id>/cases.jsonl` 是本轮逐条留痕（1800 条），`meta.json` 记着代码版本与配置。
- 所有汇总数字都应从 `cases.jsonl` 用 `experiment_log.aggregate()` 重算，不要手写。
- Jev 与重排的数字**尚未**走留痕重跑，仍属探索结果。

---

# 附录：第一轮说明（保留备查）

完整记录见 `实验完整记录.md`（正文九节 + 四个附录）。

## 包里有什么

| 目录 | 内容 |
| --- | --- |
| `实验完整记录.md` | 实验说明、结论、全部数字、代码与结果附录 |
| `code/` | 全部实验代码与测试（22 个文件） |
| `results/` | 原始结果 JSON（三臂数字、条件触发扫描、内置模型基线） |
| `data/` | 测试数据：真实提问、话题/去重/实体/工具用例、语料正文 |
| `data/store/app.db` | 脱敏后的真实记忆库（200 条记忆 / 200 话题 / 60 实体卡） |
| `manifest.json` | 每个文件的 sha256、字节数与说明 |
| `NOTICE.md` | 语料归属与许可（维基百科 CC BY-SA 4.0） |

## 怎么用

1. 读 `实验完整记录.md` 的第 2 节看结论，第 5 节看全部数字，第 8 节看偏离与边界；
2. 想复核数字：拿 `data/` 与 `results/` 对照，或按第 9 节重跑；
3. 想直接看真实记忆库：用任意 SQLite 工具打开 `data/store/app.db`，
   主要表是 `memory_index`（记忆）、`nodes`（话题/实体）、`fragments`（片段与摘要）。

## 一句话结论

本地 embedding 模型在**召回**与**实体匹配**上明显强于规则+BM25，必须用；
在**话题判定**上它与规则同样做不到（均为 0.333），而 Jev 是 0.794；
在**话题去重**上本地模型会把 65% 的新话题误判为重复，Jev 只有 13%；
**候选重排**上本地向量重排收益为零，Jev 条件触发能把第 1 名命中率从 0.748 提到 0.828。
"""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sanitize_store(source: Path, target: Path) -> dict:
    """复制记忆库并清空凭据表，返回脱敏摘要。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    conn = sqlite3.connect(str(target))
    removed = 0
    for table in ("credentials", "audit_log"):
        try:
            before = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            conn.execute(f"DELETE FROM {table}")
            removed += before
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.execute("VACUUM")
    counts = {}
    for table in ("memory_index", "nodes", "entity_cards", "fragments", "messages"):
        try:
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            counts[table] = None
    conn.close()
    return {"removed_credential_rows": removed, "tables": counts}


def main() -> int:
    staging = Path(tempfile.mkdtemp(prefix="qio-bundle-"))
    pack = staging / NAME
    manifest: list[dict] = []
    for rel in CODE:
        src = ROOT / rel
        if not src.exists():
            continue
        dst = pack / "code" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    for rel in DOCS:
        src = ROOT / rel[0]
        if src.exists():
            shutil.copy2(src, pack / rel[1])
    for rel in RESULTS:
        src = ROOT / rel
        if src.exists():
            dst = pack / "results" / Path(rel).name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    data_notes = dict(DATA_FILES)
    for name, note in DATA_FILES:
        src = CACHE / name
        if src.exists():
            dst = pack / "data" / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    store_info = {}
    store_src = CACHE / "qio-real-200b" / "app.db"
    if store_src.exists():
        store_info = sanitize_store(store_src, pack / "data" / "store" / "app.db")
    fixed_store = CACHE / "qio-real-200c" / "app.db"
    if fixed_store.exists():
        store_info["fixed"] = sanitize_store(
            fixed_store, pack / "data" / "store-fixed" / "app.db"
        )
    run_dirs = sorted((ROOT / "backend" / "evals" / "runs" / "real-arms-200").glob("*"))
    if run_dirs:
        latest = run_dirs[-1]
        for name in ("meta.json", "cases.jsonl"):
            src = latest / name
            if src.exists():
                dst = pack / "runs" / latest.name / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
    (pack / "NOTICE.md").write_text(NOTICE, encoding="utf-8")
    (pack / "README.md").write_text(README, encoding="utf-8")
    for path in sorted(pack.rglob("*")):
        if path.is_file():
            rel = path.relative_to(pack).as_posix()
            manifest.append(
                {
                    "path": rel,
                    "bytes": path.stat().st_size,
                    "sha256": sha256_of(path),
                    "note": data_notes.get(path.name, ""),
                }
            )
    meta = {
        "name": NAME,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": len(manifest),
        "bytes": sum(m["bytes"] for m in manifest),
        "store": store_info,
        "excluded": [
            {
                "what": "维基快照原始 parquet（124MB）",
                "why": "体积大且可从公开地址重新下载",
                "url": "https://huggingface.co/datasets/wikimedia/wikipedia/resolve/main/20231101.zh/train-00002-of-00006.parquet",
            }
        ],
    }
    (pack / "manifest.json").write_text(
        json.dumps({"experiment": meta, "files": manifest}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    DIST.mkdir(parents=True, exist_ok=True)
    zip_path = DIST / f"{NAME}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(pack.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(staging).as_posix())
    print(f"zip: {zip_path}")
    print(f"大小: {zip_path.stat().st_size / 1024 / 1024:.1f} MB")
    print(f"文件数: {len(manifest)}")
    print(f"记忆库: {store_info.get('tables', {})}")
    shutil.rmtree(staging, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
