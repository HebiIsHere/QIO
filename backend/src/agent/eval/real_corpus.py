"""真实语料层：把公开中文语料取下来，并守住实验的预算与缓存。

这一层只做三件事：

1. 下载/切片真实语料（中文维基快照），筛出符合长度、去重后的条目；
2. 提供带成本累计与硬上限的模型调用守卫（一次跑批不能把钱花超）；
3. 所有昂贵产物（正文缓存、提问缓存）落成 jsonl，重跑命中缓存不再计费。

记忆写入与提问生成走产品自己的代码路径，见 `real_run.py`。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import httpx

# 这台机器上 Python 的默认证书链不完整（httpx 直接报 unable to get local issuer
# certificate），而 Windows 证书存储是好的。用 truststore 把系统信任根注入 ssl，
# 保持校验开启 —— 关掉校验会让 API Key 暴露给中间人。
try:
    import truststore

    truststore.inject_into_ssl()
except Exception:
    pass

HF_REPO = "wikimedia/wikipedia"
HF_REVISION = "main"
HF_SHARD = "20231101.zh/train-00002-of-00006.parquet"
HF_URL = f"https://huggingface.co/datasets/{HF_REPO}/resolve/{HF_REVISION}/{HF_SHARD}"
DEFAULT_CACHE = Path(os.environ.get("TEMP", ".")) / "qio-stress-cache"
DOCS_CACHE_NAME = "wiki_docs.jsonl"
MIN_DOC_CHARS = 400
MAX_DOC_CHARS = 2000

_CJK = re.compile(r"[\u4e00-\u9fff]")


@dataclass(frozen=True)
class WikiDoc:
    title: str
    text: str


class BudgetExceeded(RuntimeError):
    """模型调用花费触及上限。"""


class BudgetGuard:
    """按真实用量累计花费；到阈值就拒绝继续调用。"""

    def __init__(self, max_usd: float = 1.0, stop_at: float = 0.9) -> None:
        self.max_usd = max_usd
        self.stop_at = stop_at
        self.spent = 0.0
        self.calls = 0
        self.exhausted = False

    def add(self, cost_usd: float) -> None:
        self.spent += float(cost_usd or 0.0)
        self.calls += 1
        if self.spent >= self.stop_at:
            self.exhausted = True
            raise BudgetExceeded(
                f"已花费 ${self.spent:.4f}（上限 ${self.max_usd:.2f}，停机线 ${self.stop_at:.2f}）"
            )

    def summary(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "spent_usd": round(self.spent, 6),
            "max_usd": self.max_usd,
            "exhausted": self.exhausted,
        }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
    path.write_text(body + ("\n" if body else ""), encoding="utf-8")


def cjk_len(text: str) -> int:
    return len(_CJK.findall(text))


def select_docs(
    rows: Iterable[tuple[str, str]],
    n: int,
    min_len: int = MIN_DOC_CHARS,
    max_len: int = MAX_DOC_CHARS,
) -> list[WikiDoc]:
    """按长度筛、按标题去重，取前 n 条（顺序稳定 = 结果可复现）。"""
    seen: set[str] = set()
    out: list[WikiDoc] = []
    for title, text in rows:
        title = (title or "").strip()
        text = (text or "").strip()
        if not title or title in seen:
            continue
        length = cjk_len(text)
        if length < min_len or length > max_len:
            continue
        seen.add(title)
        out.append(WikiDoc(title=title, text=text))
        if len(out) >= n:
            break
    return out


def download_wiki_shard(cache_dir: Path = DEFAULT_CACHE) -> Path:
    """下载维基中文快照分片；已存在且校验通过就直接复用。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / Path(HF_SHARD).name
    if target.exists() and target.stat().st_size > 1_000_000:
        return target
    partial = target.with_suffix(".part")
    with httpx.stream("GET", HF_URL, follow_redirects=True, timeout=120.0) as response:
        response.raise_for_status()
        with partial.open("wb") as handle:
            for chunk in response.iter_bytes(chunk_size=1 << 20):
                handle.write(chunk)
    partial.replace(target)
    return target


def read_parquet_titles_texts(parquet_path: Path, limit: int = 20000) -> list[tuple[str, str]]:
    """从快照里读出 (标题, 正文) 列表。"""
    import pyarrow.parquet as pq

    table = pq.read_table(parquet_path, columns=["title", "text"])
    titles = table.column("title").to_pylist()
    texts = table.column("text").to_pylist()
    return list(zip(titles[:limit], texts[:limit]))


def load_wiki_docs(n: int, cache_dir: Path = DEFAULT_CACHE) -> list[WikiDoc]:
    """拿到 n 篇真实文档；命中缓存就不再联网、不再读 parquet。"""
    cache = cache_dir / DOCS_CACHE_NAME
    cached = read_jsonl(cache)
    if len(cached) >= n:
        return [WikiDoc(title=r["title"], text=r["text"]) for r in cached[:n]]
    shard = download_wiki_shard(cache_dir)
    rows = read_parquet_titles_texts(shard)
    docs = select_docs(rows, n=max(n, len(cached)))
    write_jsonl(cache, [{"title": d.title, "text": d.text} for d in docs])
    return docs[:n]


def shard_manifest(cache_dir: Path = DEFAULT_CACHE) -> dict[str, Any]:
    """仓库里只留这类清单：数据集、分片、行数与哈希。"""
    cache = cache_dir / DOCS_CACHE_NAME
    docs = read_jsonl(cache)
    digest = hashlib.sha256()
    for row in docs:
        digest.update(row["title"].encode("utf-8"))
        digest.update(b"\x00")
        digest.update(row["text"].encode("utf-8"))
        digest.update(b"\n")
    return {
        "dataset": HF_REPO,
        "shard": HF_SHARD,
        "docs": len(docs),
        "sha256": digest.hexdigest()[:16],
    }
