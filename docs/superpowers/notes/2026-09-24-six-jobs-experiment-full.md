# 六项工作是否该交给模型：一次真实环境实验的完整记录

日期：2026-09-24　仓库：`qio`　执行者：Codex（与用户协作）

---

## 1. 这次要回答的两个问题

qio 里有六项判定工作，分别由「规则 + 关键词（BM25）」或「模型」承担：

1. 记忆召回——从历史记忆里找出与当前问题相关的片段；
2. 话题判定——这条消息属于哪个已有话题，还是新话题；
3. 实体匹配——这句话在说哪个已知对象；
4. 工具路由——这个请求该用哪个工具；
5. 话题去重——新话题名是否与已有话题重复；
6. 候选重排——在已召回的候选里重新排序（代码里只有钩子，没有实现）。

**问题一：本地 embedding 模型做这六项，是否强于规则 + BM25？**
**问题二：云端 Jev 做除召回以外的五项，是否明显强于本地 embedding 模型？**

---

## 2. 结论摘要

### 问题一

| 工作 | 规则+BM25（A） | 本地 embedding（B） | 判断 |
| --- | --- | --- | --- |
| 记忆召回（前 5 命中） | 0.607 | **0.823** | 本地模型明显更强 |
| 话题判定（自然口语） | 0.333 | 0.333 | **两者都做不到** |
| 实体匹配（代指提问） | 0.000 | **0.933** | 本地模型压倒性更强 |
| 工具路由 | **1.000** | 0.929 | 无差别（样本仅 14 条） |
| 话题去重 | 0.500 | 0.492（误拦 65%） | 无差别，且本地模型会误伤 |
| 候选重排 | — | 0.000 增益 | 本地模型做不了（见 §7） |

一句话：**召回、实体匹配上本地模型必须用；话题判定上两者都不够用；工具路由与去重上它没有价值，去重还带来明确的误伤风险。**

### 问题二

| 工作 | 本地 embedding（B） | Jev（C） | 判断 |
| --- | --- | --- | --- |
| 话题判定 | 0.333 | **0.794** | 明显更强 |
| 实体匹配 | 0.933 | **1.000** | 略强 |
| 工具路由 | 0.929 | **1.000** | 打平（样本小） |
| 话题去重 | 0.492（误拦 65%） | **0.917**（误拦 13%） | 明显更强 |
| 候选重排 | 0 增益 | recall@1 +8.0pp | 明显更强 |
| 记忆召回 | 0.823 | 不适用 | Jev 无向量输出 |

一句话：**在本地模型"做不到"或"会误伤"的三项（话题判定、去重、重排）上，Jev 的价值是明确的；在本地模型已经够用的两项（实体、工具）上不值得为它付联网与延迟的代价。**

---

## 3. 实验环境与数据

### 3.1 机器与运行时

- Windows，16 逻辑核，31.5GB 内存，剩余磁盘 30GB；
- Python 3.12（`backend/.venv`），pytest 全绿；
- **本机 Python 的证书链不完整**：httpx 直接请求 HTTPS 会报 `CERTIFICATE_VERIFY_FAILED`，
  解决方式是 `truststore.inject_into_ssl()`（用 Windows 证书存储，保持校验开启）。

### 3.2 三类模型

| 角色 | 模型 | 接入方式 | 用途 |
| --- | --- | --- | --- |
| A 臂 | 无模型 | 关键词 BM25 + 规则 | 对照基线 |
| B 臂 | `bge-small-zh-v1.5`（fp32, ONNX, CPU, 512 维） | 本地，离线 | 生产内置模型 |
| C 臂 | `typesafe/jev-1.13` | OpenRouter `POST /api/alpha/decisions` | 判定模型 |
| 生成器 | `deepseek-v4-flash` | qio 已配置的凭据（`api.deepseek.com/v1`） | 生成提问与用例 |

### 3.3 真实语料与真实记忆库

- 语料：`wikimedia/wikipedia` 快照 `20231101.zh/train-00002-of-00006.parquet`（121MB），
  取正文 400–2000 汉字、按标题去重的 **200 篇**；
- 记忆库：用 **qio 自己的代码路径**写入独立数据目录
  （`FragmentManager` → `close_fragment` → `IndexBuilder` → 话题由 `NodeService` 建），
  产出 **200 条记忆、200 个话题、200 个片段、400 条消息**；
  话题名就是文档标题（如「临武县」「曼聯2012年至2013年球季」）；
- **刻意跳过"模型写摘要"**：记忆内容直接用原文（见 §8 偏离说明）；
- 生成器产出的用例：**600 条召回提问**（每条记忆 3 类）、**180 条话题用例**
  （60 话题 × 继续/切换/全新）、**120 条去重用例**、**60 条实体用例**、**14 条工具用例**；
- 实体卡：用文档标题建 60 张（`entity_cards`），未跑模型提炼；
- 工具清单：取自产品真实注册表（`ctx.registry.specs()`，14 个，如 echo / now / memory_search / web_search / web_fetch）。

### 3.4 三个难度分层（召回）

召回提问由真实模型按"用户日后怎么回忆这件事"生成，并按**查询与正确记忆的内容词重叠率**
自动分层（不采信模型自我描述）：

| 分层 | 口径 | 用例数 |
| --- | --- | --- |
| literal | 重叠率 ≥ 0.4 | 111 |
| partial | 0 < 重叠率 < 0.4 | 253 |
| disjoint | 重叠率 = 0 | 236 |

---

## 4. 三条臂的实现口径

- **A 臂**：`Selector()` 默认关键词后端 + `Retriever` 的生产加权（相关性 0.4 / 时效 0.25 / 亲和 0.35）；话题判定用关键词重叠；实体匹配用名称/别名包含；去重用名称 Jaccard ≥ 0.8（生产的 `NAME_SIM_THRESHOLD`）。
- **B 臂**：`Selector(recall=OnnxEmbeddingBackend, fallback_recall=BM25)`；话题判定用 `TopicPredictor` 的向量路径；实体用 `entity_card_search`（阈值 0.25）；工具路由用 `ToolRouter(embedding=...)`；去重用向量模糊带 [0.5, 0.7)（生产的 `EMBED_LO/EMBED_HI`）。
- **C 臂**：全部折叠成"在闭合候选集里选一个"的 Jev 问题；召回项直接 `NotImplementedError`。
- 三条臂吃同一份输入、同一批用例；检索索引只建一次（生产也是常驻索引）。

---

## 5. 结果总表（真实数据）

### 5.1 记忆召回（600 条真实提问 / 200 条真实记忆）

| 指标 | A 规则+BM25 | B 本地模型 |
| --- | --- | --- |
| 第 1 名命中 | 0.593 | **0.750** |
| 前 5 名命中 | 0.607 | **0.823** |
| MRR | 0.599 | **0.776** |
| 错误记忆注入 | 0.393 | **0.177** |
| 陈旧知识注入 | 0.000 | 0.000 |
| 单条延迟 p50 | 0.17ms | 2.3ms |

按难度分层（前 5 命中）：

| 分层 | 用例数 | A | B |
| --- | --- | --- | --- |
| literal | 111 | 1.000 | 1.000 |
| partial | 253 | 1.000 | 1.000 |
| disjoint | 236 | **0.000** | **0.551** |

### 5.2 其余四项

| 工作 | 用例数 | A 规则 | B 本地 | C Jev |
| --- | --- | --- | --- | --- |
| 话题判定 | 180 | 0.333 | 0.333 | **0.794** |
| 实体匹配 | 60 | 0.000 | 0.933 | **1.000** |
| 工具路由 | 14 | **1.000** | 0.929 | **1.000** |
| 话题去重 | 120 | 0.500 | 0.492（误拦 0.650） | **0.917**（误拦 0.133） |

话题判定的混淆（A/B 完全相同）：继续→新话题 60/60、切换→新话题 60/60、全新→新话题 60/60
——两臂把所有"继续/切换"都判成了新话题。

### 5.3 候选重排（600 条查询，同一批 12 条候选）

| 方案 | recall@1 | recall@5 | MRR | 错误注入 | 每条延迟 | 花费 |
| --- | --- | --- | --- | --- | --- | --- |
| 原序（基线） | 0.748 | 0.815 | 0.777 | 0.185 | 0 | $0 |
| 本地向量重排 | 0.748 | 0.815 | 0.777 | 0.185 | 18.9ms | $0 |
| Jev 无条件重排 | 0.792 | 0.843 | 0.815 | 0.157 | 63.6ms | $0.0279 |
| **Jev 条件触发（阈值 0.15）** | **0.828** | **0.848** | **0.837** | **0.152** | ~43% 查询触发 | $0.0121 |
| Jev 条件触发（阈值 0.05） | 0.818 | 0.845 | 0.830 | 0.155 | ~28% 触发 | $0.0077 |
| Jev 条件触发（阈值 0.30） | 0.807 | 0.845 | 0.824 | 0.155 | ~82% 触发 | $0.0229 |

三个反直觉的结论：**本地向量重排的收益精确为零**（同算法同盲点）；
**条件触发比无条件重排更好**（0.828 对 0.792），因为顶部清晰的排序本来是对的，
Jev 有时会把对的改错；**过度触发会变差**（阈值 0.30 反而掉到 0.807）。

### 5.4 代价

| | 延迟 | 花费 | 联网 |
| --- | --- | --- | --- |
| A 规则+BM25 | 0.1–0.2ms | 0 | 否 |
| B 本地 embedding | 2–4ms | 0 | 否 |
| C Jev | 0.4–1.4s（视问题数量） | 每条约 $0.00002–0.00043 | **是** |

本次实验 Jev 累计花费约 **$0.18**（含小样与三次阈值扫描）；生成器（DeepSeek）用于
600 条提问 + 300 条用例，未计入 OpenRouter 账单。

---

## 6. 关键发现与建议

1. **本地 embedding 的价值集中在"用户换一种说法"**：字面重合时关键词与向量都是满分；
   完全改写时关键词 0.000、向量 0.551。这是它唯一但足够重要的增量。
2. **话题判定是本地模型的盲区**：自然口语提问下它与关键词同分（0.333），
   因为两者都只能算"消息与话题名的相似度"，而用户往往根本不说话题名。
3. **话题去重的固定区间会误伤**：生产用 [0.5, 0.7) 拦截重复话题，在 200 个真实话题上
   把 65% 的全新话题误判为重复。这是可以直接改的产品缺陷（提高下限或改判据）。
4. **实体匹配要区分"说名字"与"说代指"**：说名字时规则臂够用（本次用例 0 条含名字，
   所以规则臂 0.000 有构造成分）；说代指时只有向量与 Jev 能接住。
5. **重排必须放在加权之后**：现有钩子在时效/亲和加权之前，实装时要移动，
   否则上述重排收益会被后面的加权吃掉。
6. **接入建议**：召回与实体用本地模型（零成本、毫秒级）；话题判定、去重、重排用 Jev 或
   其它判定模型；重排用条件触发控成本。

---

## 7. 实现过程中被测试抓出来的三个真 bug

1. **语料第一版失效**：所有查询用话题词直接拼，字面重叠 100% 可答，测不出模型价值。
   → 引入查询侧同义词表（零 2-gram 重叠）+ 内容词重叠判定。
2. **语料第二版失效**：同一话题下有几十条句式相同的记忆，正确答案不唯一，
   BM25 命中的是同话题的别条。→ 改成"话题内讨论事项唯一"，规则臂才恢复到应有水平。
3. **Jev 重排的问题没指向候选**：12 个问题问的是同一句话，模型给出同一批概率，
   排序退化成按 id 排（recall@5 掉到 0.330）。→ 用反引号显式引用
   `` `candidates.c0` `` 后恢复正常。

另外 Jev 客户端的测试抓出"错误详情里回显的密钥被打码后仍保留 `sk-or` 片段"，改为整串替换。

---

## 8. 偏离生产默认的地方（结论必须带着这些看）

1. **记忆内容是原文而非模型摘要**：生产存的是模型写的摘要，本次跳过提炼（用户决定），
   换来速度与语言稳定；因此记忆形态比生产更"生"。
2. **实体卡由文档标题生成**，没有跑实体提炼；
3. **话题用例是模型只看标题写出来的**，天然模糊，对关键词路径不利、对能看到全部候选的 Jev 有利；
4. **实体用例 0 条含实体名**，所以规则臂 0.000 有构造成分；
5. **工具路由只有 14 条用例**，不足以定论；
6. **重排实验用的是回忆阶段的原始候选**，未叠加生产的时效/亲和加权；
7. 所有对话都是"用户贴一段资料"的形态，不是真实的多轮对话。

---

## 9. 怎么复现

```bash
cd backend
# 1) 下载并切片真实语料（121MB，约 70 秒）
uv run --frozen python -m agent.eval.real_run --step docs --n 200
# 2) 建真实记忆库 + 生成提问（提问走 qio 已配置的凭据）
uv run --frozen python -m agent.eval.real_run --step store --n 200 \
  --data-dir "%TEMP%\qio-stress-cache\qio-real-200b" --out stress_real_200.json
# 3) 生成话题与去重用例
uv run --frozen python -m agent.eval.real_run --step cases --n 60 \
  --data-dir "%TEMP%\qio-stress-cache\qio-real-200b"
# 4) 跑 A 臂与 B 臂
uv run --frozen python -m agent.eval.real_run --step arms \
  --data-dir "%TEMP%\qio-stress-cache\qio-real-200b" --out stress_real_arms_200.json
# 5) C 臂（需要 OPENROUTER_API_KEY）与重排实验见附录 D 的命令
```

固定的随机种子与内容哈希缓存保证：**重跑命中缓存不再产生模型费用**。

---

## 附录 A：实验代码

以下文件是本次实验新增或改造的全部代码。


### `backend/src/agent/eval/real_corpus.py`

真实语料层：下载/切片/缓存/预算守卫

```python
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
```

### `backend/src/agent/eval/real_run.py`

真实记忆库构建、提问与用例生成、装配与跑臂

```python
"""真实环境数据：用产品自己的路径建记忆库，再由真实模型生成提问。

与合成语料的区别只有一处，但很关键：记忆**不是**我拼的模板，而是
「真实中文文档 → 产品封块 → 真实模型按产品提示词写摘要」这条链路产出的，
存进 `memory_index` 的就是生产里会存的那种摘要。

刻意偏离默认的地方（报告里必须标注）：

- `fragment.max_turns` 调成 1，让每篇文档封一次块、产出一条记忆；
- 语料以「用户贴一段资料」的形式进入片段，不是用户自己的话。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.adapters.base import BaseAdapter, ChatMessage, Completion
from agent.eval.real_corpus import (
    DEFAULT_CACHE,
    BudgetGuard,
    WikiDoc,
    load_wiki_docs,
    read_jsonl,
    shard_manifest,
    write_jsonl,
)
from agent.eval.stress_corpus import (
    CATEGORIES,
    DedupCase,
    EntityCard,
    EntityCase,
    Memory,
    RecallCase,
    StressCorpus,
    ToolCase,
    ToolSpecLite,
    Topic,
    TopicCase,
)

# 免费档（qwen/qwen3.8-27b:free）实测会被 429 限流，不适合批量跑；
# 换成最便宜的付费档：约 $0.02/M 输入、$0.09/M 输出，一千条记忆约 $0.2。
DEFAULT_MODEL = "openai/gpt-oss-20b"
FALLBACK_MODEL = "mistralai/mistral-nemo"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
STORE_CACHE = "store.jsonl"
QUESTIONS_CACHE = "questions.jsonl"
CASES_CACHE = "real_cases.json"

_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def post_chat(payload: dict[str, Any], api_key: str, *, tries: int = 4, timeout: float = 120.0) -> dict:
    """带退避重试的 chat 调用：限流（429）与 5xx 都重试，其它错误直接抛。"""
    import httpx

    last: Exception | None = None
    for attempt in range(tries):
        try:
            response = httpx.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
                timeout=timeout,
            )
            if response.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError(
                    f"{response.status_code}", request=response.request, response=response
                )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last = exc
            time.sleep(min(8.0, 1.0 * (2**attempt)))
    raise RuntimeError(f"模型调用连续失败：{type(last).__name__}: {last}")


@dataclass
class StoreResult:
    data_dir: Path
    memories: list[Memory] = field(default_factory=list)
    topics: list[Topic] = field(default_factory=list)
    entities: list[EntityCard] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    adapter: Any = None
    adapter_model: str = ""
    copied_key_ids: list[str] = field(default_factory=list)


def seed_credentials(conn, source_db: Path | None = None) -> list[str]:
    """把用户真实库里的 active 凭据复制一份到实验库。

    实验的记忆库跑在独立数据目录（不污染日常使用的 app.db），但模型 Key 存在真实库里，
    所以这里只读地复制「标签 + 端点 + 默认模型 + 密钥」；密钥走 OS 凭据库，
    全程不打印、不落文件。返回新库里的 key_id，供跑完清理。
    """
    import sqlite3

    from agent.config import default_data_dir
    from agent.credentials.store import CredentialStore

    source_db = Path(source_db) if source_db else default_data_dir() / "app.db"
    current = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    if not source_db.exists() or source_db == current:
        return []
    src = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        rows = src.execute(
            "SELECT * FROM credentials WHERE status = 'active' AND enabled = 1"
        ).fetchall()
    except sqlite3.OperationalError:
        src.close()
        return []
    src_store = CredentialStore(src)
    dst_store = CredentialStore(conn)
    copied: list[str] = []
    for row in rows:
        secret = src_store.get_secret(row["id"])
        if not secret:
            continue
        created = dst_store.create(
            row["id"],
            secret,
            json.loads(row["tags"] or "[]"),
            endpoint=row["endpoint"],
            default_model=row["default_model"],
            note="stress copy",
        )
        copied.append(created if isinstance(created, str) else row["id"])
    src.close()
    return copied


class OpenRouterAdapter(BaseAdapter):
    """给产品提炼链路用的适配器：一进一出，同时记录真实花费。"""

    mode = "native"
    endpoint = "https://openrouter.ai/api/v1"
    tools_in_prompt = False

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, guard: BudgetGuard | None = None) -> None:
        self.api_key = api_key
        self.model = model
        self.guard = guard or BudgetGuard()
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[Any],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        payload = {
            "model": self.model,
            "messages": [
                {"role": m.role, "content": m.content or ""}
                for m in messages
                if m.role in ("system", "user", "assistant")
            ],
            "temperature": 0.2 if temperature is None else temperature,
        }
        import asyncio

        data = await asyncio.to_thread(post_chat, payload, self.api_key)
        usage = data.get("usage") or {}
        self.calls += 1
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        self.guard.add(float(usage.get("cost") or 0.0))
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        return Completion(message=ChatMessage(role="assistant", content=text))

    def to_chat(self, messages: list[ChatMessage]) -> Completion:
        return Completion(message=ChatMessage(role="assistant", content=""))


def chat_json(
    api_key: str,
    model: str,
    prompt: str,
    guard: BudgetGuard,
    *,
    timeout: float = 120.0,
) -> Any:
    """同步调用一次，返回解析后的 JSON（失败返回 None）。"""
    data = post_chat(
        {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3},
        api_key,
        timeout=timeout,
    )
    usage = data.get("usage") or {}
    guard.add(float(usage.get("cost") or 0.0))
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    match = _JSON_BLOCK.search(text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def ask_json(adapter, prompt: str, *, temperature: float = 0.3) -> Any:
    """用**产品自己的适配器**问一次并解析 JSON（凭据、端点、重试策略都走生产那条路）。"""
    async def _run() -> str:
        completion = await adapter.complete(
            [ChatMessage(role="user", content=prompt)], tools=[], temperature=temperature
        )
        return completion.message.content or ""

    text = asyncio.run(_run())
    match = _JSON_BLOCK.search(text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def build_store(
    docs: list[WikiDoc],
    *,
    data_dir: Path,
    adapter: Any | None = None,
    limit: int | None = None,
    raw_text: bool = True,
) -> StoreResult:
    """把文档喂进产品路径，产出真实记忆库。

    `raw_text=True` 跳过「模型写摘要」这一步：记忆内容直接用原文，索引仍由产品
    自己的 `IndexBuilder` 写、话题仍由产品的 `NodeService` 建。跑得快、不受模型
    语言漂移影响；代价是记忆形态与生产不一致（生产存的是模型摘要），报告里要标注。
    """
    from agent.config import default_data_dir

    # 真实凭据在用户日常使用的库里；必须在改写 QIO_DATA_DIR 之前把它记下来。
    source_db = default_data_dir() / "app.db"
    os.environ["QIO_DATA_DIR"] = str(data_dir)
    from agent.api.bus import EventBus
    from agent.config import Settings
    from agent.services.app import AppContext
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations

    settings = Settings(data_dir=data_dir)
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    apply_migrations(conn)
    ctx = AppContext(settings, conn, EventBus())
    ctx.settings_store.set("fragment.max_turns", "30")
    ctx.fragments.max_turns = 30
    # 凭据总要带过来：原文模式下摘要不调模型，但提问生成仍要用产品那把 Key。
    copied_key_ids = seed_credentials(conn, source_db)
    result = StoreResult(data_dir=data_dir)
    if adapter is None:
        adapter = asyncio.run(ctx.build_adapter())
    if adapter is None:
        raise SystemExit("没有可用的 main-loop 凭据：先在设置里配一把模型 Key")
    result.adapter = adapter
    result.adapter_model = getattr(adapter, "model", "")
    result.copied_key_ids = copied_key_ids
    chosen = docs[:limit] if limit else docs
    for doc in chosen:
        # 话题名直接用文档标题本身：这才是用户看到的那种话题名
        topic = ctx.topics.nodes.create_topic(doc.title[:32])
        topic_id = topic.id
        ctx.memory.append_message(
            topic_id=topic_id,
            role="user",
            content=doc.text,
        )
        ctx.memory.append_message(
            topic_id=topic_id,
            role="assistant",
            content="已记录这份资料的要点。",
        )
        fragment = ctx.fragments.open_fragment(topic_id)
        if fragment is None:
            result.failures.append({"title": doc.title, "error": "没有开放片段"})
            continue
        try:
            asyncio.run(ctx.memory_lifecycle.close_fragment(topic_id, None))
            if raw_text:
                ctx.index_builder.build(
                    fragment_id=fragment.id,
                    topic_id=topic_id,
                    title=doc.title,
                    summary_text=doc.text,
                    entities=[],
                    keywords=[],
                    message_texts=[doc.text],
                )
            else:
                asyncio.run(ctx.memory_lifecycle.drain_derived_tasks(adapter, limit=4))
        except Exception as exc:
            result.failures.append({"title": doc.title, "error": f"{type(exc).__name__}: {exc}"})
            continue
        rows = conn.execute(
            "SELECT mi.id AS index_id, mi.title, mi.topic_id, f.summary "
            "FROM memory_index mi LEFT JOIN fragments f ON f.id = mi.fragment_id "
            "WHERE mi.topic_id = ?",
            (topic_id,),
        ).fetchall()
        if not rows:
            result.failures.append({"title": doc.title, "error": "封块后没有产生记忆索引"})
            continue
        for row in rows:
            text = " ".join([row["summary"] or "", row["title"] or ""]).strip()
            result.memories.append(
                Memory(
                    id=row["index_id"],
                    text=text,
                    topic_id=row["topic_id"],
                    kind="knowledge",
                    age_days=0.0,
                )
            )
        result.topics.append(Topic(id=topic_id, title=doc.title[:32], keywords=()))
    emit_entities(conn, result)
    return result


def emit_entities(conn, result: StoreResult) -> None:
    rows = conn.execute(
        "SELECT id, name, aliases, summary FROM entity_cards WHERE state = 'active'"
    ).fetchall()
    for row in rows:
        result.entities.append(
            EntityCard(
                id=row["id"],
                name=row["name"],
                aliases=tuple(json.loads(row["aliases"] or "[]")),
                summary=row["summary"] or "",
            )
        )


QUESTION_PROMPT = """你是测试集生成器。下面是若干条"记忆摘要"，每条有 id。
请为每条生成 3 个中文提问，模拟用户日后回忆这件事时会怎么问：

- literal：几乎照抄摘要里的说法；
- partial：保留一部分原词，换掉另一部分；
- rewrite：完全换一种说法，不出现摘要里的原词（但语义仍指向这条记忆）。

**必须用简体中文（不要粤语、不要繁体）**，语气像普通话用户在日常聊天里提问。
只输出 JSON，不要解释。格式：
{{"items": [{{"id": "<记忆id>", "literal": "...", "partial": "...", "rewrite": "..."}}]}}

记忆列表：
{payload}"""


def generate_questions(
    store: StoreResult,
    *,
    adapter: Any | None = None,
    guard: BudgetGuard | None = None,
    batch: int = 8,
    cache_path: Path | None = None,
    cache_only: bool = False,
) -> dict[str, dict[str, str]]:
    """为每条真实记忆生成三类提问。

    缓存按**记忆正文的哈希**而不是 index id：重建记忆库后 id 会变，但内容没变，
    这时不该再花一次模型调用。
    """
    cache_path = cache_path or (DEFAULT_CACHE / QUESTIONS_CACHE)
    keyed = {row["key"]: row for row in read_jsonl(cache_path) if row.get("key")}
    by_key = {_doc_key(m.text): m for m in store.memories}
    out: dict[str, dict[str, str]] = {}
    for key, row in keyed.items():
        memory = by_key.get(key)
        if memory is not None:
            out[memory.id] = {
                k: row[k] for k in ("literal", "partial", "rewrite") if k in row
            }
    pending = [m for m in store.memories if m.id not in out]
    if not pending:
        return out
    adapter = adapter or store.adapter
    if adapter is None or cache_only:
        return out
    for start in range(0, len(pending), batch):
        chunk = pending[start : start + batch]
        payload = "\n".join(f"- id={m.id} 摘要：{m.text[:400]}" for m in chunk)
        data = ask_json(adapter, QUESTION_PROMPT.format(payload=payload))
        if not data:
            continue
        for item in data.get("items") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            if item["id"] not in {m.id for m in chunk}:
                continue
            out[item["id"]] = {
                "literal": str(item.get("literal") or ""),
                "partial": str(item.get("partial") or ""),
                "rewrite": str(item.get("rewrite") or ""),
            }
        text_of = {m.id: m.text for m in chunk}
        write_jsonl(
            cache_path,
            [
                {"key": _doc_key(text_of.get(k, "") or by_id_text(store, k)), "id": k, **v}
                for k, v in out.items()
            ],
        )
    return out


def _doc_key(text: str) -> str:
    import hashlib

    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def by_id_text(store: StoreResult, memory_id: str) -> str:
    for memory in store.memories:
        if memory.id == memory_id:
            return memory.text
    return ""


def load_store(data_dir: Path) -> StoreResult:
    """从已有的实验库里把记忆/话题/实体读回来（用于跑臂，不重建）。"""
    import sqlite3

    from agent.storage.migrate import apply_migrations

    db = Path(data_dir) / "app.db"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    result = StoreResult(data_dir=Path(data_dir))
    rows = conn.execute(
        "SELECT mi.id AS index_id, mi.title, mi.topic_id, f.summary "
        "FROM memory_index mi LEFT JOIN fragments f ON f.id = mi.fragment_id"
    ).fetchall()
    for row in rows:
        result.memories.append(
            Memory(
                id=row["index_id"],
                text=" ".join([row["summary"] or "", row["title"] or ""]).strip(),
                topic_id=row["topic_id"],
                kind="knowledge",
                age_days=0.0,
            )
        )
    # 话题关键词按产品口径来：取该话题下各条记忆索引里的关键词；索引里没有就退回标题分词 ——
    # 否则规则臂拿不到任何可比对的词，会被白白判死（这正是第一次跑出 A=0.0 的原因）。
    from agent.selector.tokenize import tokenize

    per_topic: dict[str, list[str]] = {}
    for row in conn.execute("SELECT topic_id, keywords FROM memory_index"):
        per_topic.setdefault(row["topic_id"], []).extend(json.loads(row["keywords"] or "[]"))
    for row in conn.execute("SELECT id, name FROM nodes WHERE type = 'topic'"):
        words = list(dict.fromkeys(per_topic.get(row["id"], [])))[:8]
        result.topics.append(
            Topic(
                id=row["id"],
                title=row["name"],
                keywords=tuple(words) or tuple(tokenize(row["name"] or "")[:6]),
            )
        )
    for row in conn.execute("SELECT id, name, aliases, summary FROM entity_cards"):
        result.entities.append(
            EntityCard(
                id=row["id"],
                name=row["name"],
                aliases=tuple(json.loads(row["aliases"] or "[]")),
                summary=row["summary"] or "",
            )
        )
    conn.close()
    return result


def assemble_corpus(
    store: StoreResult,
    questions: dict[str, dict[str, str]],
    *,
    n_queries: int | None = None,
    cases_path: Path | None = None,
) -> StressCorpus:
    """把真实记忆与生成的问题装配成既有 `StressCorpus`，后续三臂直接复用。"""
    from agent.eval.stress_corpus import _overlap_ratio, _tier_of

    generated = load_cases(cases_path) if cases_path else {}
    by_id = {m.id: m for m in store.memories}
    cases: list[RecallCase] = []
    kinds = ("literal", "partial", "rewrite")
    index = 0
    for memory in store.memories:
        item = questions.get(memory.id) or {}
        for kind in kinds:
            query = (item.get(kind) or "").strip()
            if not query:
                continue
            overlap = _overlap_ratio(query, memory.text)
            cases.append(
                RecallCase(
                    id=f"rq_{index:05d}",
                    category=CATEGORIES[index % len(CATEGORIES)],
                    query=query,
                    expected=(memory.id,),
                    stale=(),
                    keyword_answerable=overlap > 0,
                    overlap=round(overlap, 4),
                    tier=_tier_of(overlap),
                )
            )
            index += 1
    if n_queries:
        cases = cases[:n_queries]
    entity_cases = [
        EntityCase(id=f"en_{i:05d}", query=f"{card.name}负责什么？", expected_card_id=card.id)
        for i, card in enumerate(store.entities)
    ]
    topic_cases = generated.get("topic") or [
        TopicCase(
            id=f"tp_{i:05d}",
            category="in_topic",
            message=f"继续 {topic.title} 这个话题",
            current_topic_id=topic.id,
            expected_topic_id=topic.id,
            expected_mode="in_topic",
        )
        for i, topic in enumerate(store.topics)
    ]
    dedup_cases = generated.get("dedup") or []
    return StressCorpus(
        seed=0,
        topics=list(store.topics),
        memories=list(by_id.values()),
        entities=list(store.entities),
        tools=[],
        recall_cases=cases,
        topic_cases=topic_cases,
        entity_cases=entity_cases,
        tool_cases=[],
        dedup_cases=dedup_cases,
    )


def load_cases(path: Path | None) -> dict:
    """读回生成好的用例（话题 / 去重），没有就返回空。"""
    if path is None or not Path(path).exists():
        return {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, list] = {}
    if raw.get("topic"):
        out["topic"] = [
            TopicCase(
                id=r["id"],
                category=r["category"],
                message=r["message"],
                current_topic_id=r.get("current_topic_id"),
                expected_topic_id=r.get("expected_topic_id"),
                expected_mode=r["expected_mode"],
            )
            for r in raw["topic"]
        ]
    if raw.get("dedup"):
        out["dedup"] = [
            DedupCase(
                id=r["id"],
                candidate_name=r["candidate_name"],
                expected_duplicate=bool(r["expected_duplicate"]),
                duplicate_of=r.get("duplicate_of"),
            )
            for r in raw["dedup"]
        ]
    return out


TOPIC_CASE_PROMPT = """你是测试集生成器。下面是若干真实话题（id + 标题），都是某位用户和他的助手长期讨论过的东西。
请为每个话题写两条**普通话自然口语**的消息：

- continue：用户想继续聊这个话题时会怎么说（不要直接抄标题，要像日常说话）；
- new：与这些话题都无关、明显开启新方向的另一条消息（每条都要不一样）。

必须用简体中文。只输出 JSON，不要解释。格式：
{{"items": [{{"id": "<话题id>", "continue": "...", "new": "..."}}]}}

话题列表：
{payload}"""

DEDUP_CASE_PROMPT = """你是测试集生成器。下面是若干真实话题标题。
请为每个标题生成两个**候选新话题名**：

- duplicate：换一种说法表达**同一个**话题（应当被判为重复）；
- novel：一个看起来相似、但实际是完全不同方向的新话题（不该被判为重复）。

必须用简体中文，长度像真实话题名。只输出 JSON。格式：
{{"items": [{{"id": "<话题id>", "duplicate": "...", "novel": "..."}}]}}

话题列表：
{payload}"""


def generate_cases(
    store: StoreResult,
    *,
    adapter: Any,
    batch: int = 10,
    cache_path: Path | None = None,
    limit: int | None = None,
) -> dict:
    """用真实模型造话题与去重用例；结果落盘，重跑不再计费。"""
    path = Path(cache_path) if cache_path else (DEFAULT_CACHE / CASES_CACHE)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    topics = store.topics[:limit] if limit else store.topics
    payload_rows = generate_case_rows(topics, adapter, batch)
    chosen = [t for t in topics if t.id in payload_rows]
    topic_cases: list[dict] = []
    for i, topic in enumerate(chosen):
        row = payload_rows[topic.id]
        other = chosen[(i + 7) % len(chosen)] if len(chosen) > 1 else topic
        topic_cases.append(
            {
                "id": f"tp_{i:05d}",
                "category": "in_topic",
                "message": row["continue"],
                "current_topic_id": topic.id,
                "expected_topic_id": topic.id,
                "expected_mode": "in_topic",
            }
        )
        topic_cases.append(
            {
                "id": f"ts_{i:05d}",
                "category": "switch",
                "message": row["continue"],
                "current_topic_id": other.id,
                "expected_topic_id": topic.id,
                "expected_mode": "switch",
            }
        )
        topic_cases.append(
            {
                "id": f"tn_{i:05d}",
                "category": "new_topic",
                "message": row["new"],
                "current_topic_id": topic.id,
                "expected_topic_id": None,
                "expected_mode": "new_topic",
            }
        )
    dedup_cases: list[dict] = []
    for i, topic in enumerate(chosen):
        row = payload_rows[topic.id]
        if not (row.get("duplicate") and row.get("novel")):
            continue
        dedup_cases.append(
            {
                "id": f"dd_{i:05d}",
                "candidate_name": row["duplicate"],
                "expected_duplicate": True,
                "duplicate_of": topic.id,
            }
        )
        dedup_cases.append(
            {
                "id": f"dn_{i:05d}",
                "candidate_name": row["novel"],
                "expected_duplicate": False,
                "duplicate_of": None,
            }
        )
    payload = {"topic": topic_cases, "dedup": dedup_cases}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def generate_case_rows(topics: list[Topic], adapter: Any, batch: int) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    for start in range(0, len(topics), batch):
        chunk = topics[start : start + batch]
        listed = "\n".join(f"- id={t.id} 标题：{t.title}" for t in chunk)
        data = ask_json(adapter, TOPIC_CASE_PROMPT.format(payload=listed))
        if isinstance(data, dict):
            for item in data.get("items") or []:
                if isinstance(item, dict) and item.get("continue"):
                    rows[str(item.get("id"))] = {
                        "continue": str(item.get("continue")),
                        "new": str(item.get("new") or ""),
                        "duplicate": "",
                        "novel": "",
                    }
        data2 = ask_json(adapter, DEDUP_CASE_PROMPT.format(payload=listed))
        if isinstance(data2, dict):
            for item in data2.get("items") or []:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("id"))
                if key in rows and item.get("duplicate") and item.get("novel"):
                    rows[key]["duplicate"] = str(item["duplicate"])
                    rows[key]["novel"] = str(item["novel"])
    return {k: v for k, v in rows.items() if v["continue"] and v["new"]}


def _api_key() -> str:
    from agent.eval.jev_client import resolve_api_key

    key = resolve_api_key()
    if not key:
        raise SystemExit("缺少 OPENROUTER_API_KEY（User 作用域或环境变量）")
    return key


def build_standalone_adapter(data_dir: Path):
    """在实验库上单独取一个产品适配器（凭据在建库时已复制过来）。"""
    async def _build():
        from agent.api.bus import EventBus
        from agent.config import Settings
        from agent.services.app import AppContext
        from agent.storage.db import connect

        os.environ["QIO_DATA_DIR"] = str(data_dir)
        settings = Settings(data_dir=data_dir)
        conn = connect(settings.db_path)
        ctx = AppContext(settings, conn, EventBus())
        return await ctx.build_adapter()

    return _build()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实环境数据：建库 → 生成提问 → 装配语料")
    parser.add_argument(
        "--step",
        choices=("docs", "store", "questions", "cases", "corpus", "arms"),
        default="corpus",
    )
    parser.add_argument("--n", type=int, default=200)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--data-dir", default=str(DEFAULT_CACHE / "qio-real"))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--out", default="stress_real.json")
    parser.add_argument("--max-usd", type=float, default=1.0)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    cache_dir = Path(args.cache_dir)
    guard = BudgetGuard(max_usd=args.max_usd, stop_at=args.max_usd * 0.9)
    if args.step == "docs":
        docs = load_wiki_docs(args.n, cache_dir=cache_dir)
        print(json.dumps({"docs": len(docs), **shard_manifest(cache_dir)}, ensure_ascii=False))
        return 0
    if args.step == "arms":
        from agent.eval.stress_arms import LocalEmbeddingArm, RulesArm
        from agent.eval.stress_run import run_all as run_arms

        store = load_store(Path(args.data_dir))
        questions = generate_questions(
            store, cache_path=cache_dir / QUESTIONS_CACHE, cache_only=True
        )
        corpus = assemble_corpus(
            store, questions, cases_path=cache_dir / CASES_CACHE
        )
        guard = BudgetGuard(max_usd=args.max_usd, stop_at=args.max_usd * 0.9)
        arms = [RulesArm(corpus), LocalEmbeddingArm(corpus)]
        started = time.perf_counter()
        payload = run_arms(corpus, arms, k=5)
        payload["_corpus"] = {
            "memories": len(corpus.memories),
            "topics": len(corpus.topics),
            "entities": len(corpus.entities),
            "recall_cases": len(corpus.recall_cases),
            "questions_cached": len(questions),
            "wall_s": round(time.perf_counter() - started, 1),
            "budget": guard.summary(),
            "data_dir": str(args.data_dir),
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        print(text)
        out = Path(args.out)
        if not out.is_absolute():
            out = Path(__file__).resolve().parents[3] / "evals" / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote → {out}", file=sys.stderr)
        return 0
    if args.step == "cases":
        store = load_store(Path(args.data_dir))
        if store.adapter is None:
            store.adapter = asyncio.run(build_standalone_adapter(Path(args.data_dir)))
        payload_cases = generate_cases(
            store, adapter=store.adapter, cache_path=cache_dir / CASES_CACHE, limit=args.n
        )
        print(
            json.dumps(
                {
                    "topic_cases": len(payload_cases.get("topic", [])),
                    "dedup_cases": len(payload_cases.get("dedup", [])),
                    "adapter": getattr(store.adapter, "model", ""),
                },
                ensure_ascii=False,
            )
        )
        return 0
    docs = load_wiki_docs(args.n, cache_dir=cache_dir)
    started = time.perf_counter()
    store = build_store(docs, data_dir=Path(args.data_dir))
    questions = generate_questions(
        store,
        adapter=store.adapter,
        guard=guard,
        batch=args.batch,
        cache_path=cache_dir / QUESTIONS_CACHE,
    )
    corpus = assemble_corpus(store, questions, n_queries=None)
    payload = {
        "docs": len(docs),
        "memories": len(store.memories),
        "topics": len(store.topics),
        "entities": len(store.entities),
        "recall_cases": len(corpus.recall_cases),
        "questions": len(questions),
        "failures": store.failures[:10],
        "adapter": {"model": store.adapter_model},
        "budget": guard.summary(),
        "wall_s": round(time.perf_counter() - started, 1),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    out = Path(args.out)
    if not out.is_absolute():
        out = Path(__file__).resolve().parents[3] / "evals" / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote → {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

### `backend/src/agent/eval/stress_corpus.py`

合成压力语料生成器（对照用）

```python
"""压力语料生成器：按 qio 的任务形状造记忆、话题、实体、工具与查询。

设计要点：

- **确定性**：同一个 seed 必然产出同一份语料（测试守着这一点）。
- **答案由构造决定**：每条查询的正确答案是"它由哪条记忆派生"这件事本身，
  不靠人手写期望。事实更新类同时放入旧值与新值，只有新值算正确。
- **关键词可答性实算**：`keyword_answerable` 由查询与正确记忆的字面重叠算出，
  用来把"模型在关键词答不对的子集上有多少增量"单独统计出来。

内容是按模板和词表拼出来的中文，不是自然语料 —— 这是本实验的已知边界，
结论只能说明"在这套任务形状下模型能不能把活干完"。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from agent.selector.tokenize import tokenize

CATEGORIES = ("fact_update", "same_topic", "entity_ref", "cross_topic", "paraphrase", "recent_noise")

# -- 词表 -----------------------------------------------------------------

DOMAINS = [
    "数据库", "前端界面", "接口设计", "部署运维", "缓存策略", "权限模型", "日志系统",
    "消息队列", "支付流程", "订单系统", "用户画像", "搜索排序", "推荐算法", "数据同步",
    "备份恢复", "监控告警", "灰度发布", "代码审查", "测试策略", "性能优化", "内存管理",
    "并发控制", "文件存储", "图片压缩", "音频处理", "视频转码", "文本分词", "向量检索",
    "模型微调", "提示工程", "账号安全", "密钥管理", "桌面打包", "安装升级", "离线同步",
    "多语言支持", "无障碍访问", "主题配色", "图表渲染", "快捷键",
]

ASPECTS = ["迁移", "索引", "备份", "权限", "缓存", "队列"]

# 查询侧同义词表：与上面的记忆用词**不共享任何 2-gram**。硬类别（跨话题引用、
# 近义表述、无关近因）只用这张表提问，所以关键词路径不可能靠内容词命中 ——
# 这正是「模型的增量价值」要落在的那个子集。单字虚词（的/是/了）不算重叠。
DOMAINS_QUERY = [
    "存储层", "视图层", "通信协议", "上线流程", "加速方案", "授权体系", "记录体系",
    "异步通道", "结算步骤", "交易体系", "人群标签", "结果排名", "偏好预测", "一致性复制",
    "容灾流程", "观测提示", "分批上线", "同行评审", "验收方针", "提速改造", "运存调度",
    "并行协调", "磁盘归档", "画质瘦身", "声波加工", "影像格式替换", "字串切块", "相似度查找",
    "权重校准", "指令设计", "登录防护", "凭据保管", "安装包构建", "版本更新", "断网复制",
    "本地化适配", "读屏可用", "外观颜色", "可视化绘制", "组合手势",
]

ASPECTS_QUERY = ["切换", "目录", "容灾", "准入", "加速", "通道"]

KINDS = ["decision", "knowledge", "preference", "chatter"]

# 同族替换对：(旧值, 新值)。用来构造"事实更新"用例。
VALUE_PAIRS = [
    ("PostgreSQL", "SQLite"), ("REST", "GraphQL"), ("Redis", "本地文件"),
    ("单体部署", "容器部署"), ("定时轮询", "事件推送"), ("固定宽高", "响应式布局"),
    ("全量同步", "增量同步"), ("同步写入", "异步写入"), ("手工发布", "灰度发布"),
    ("单库查询", "读写分离"),
]

REASONS = ["维护成本更低", "性能更稳", "团队更熟悉", "依赖更少", "排查更容易", "扩展更省事"]

ENTITIES_NAME = ["小林", "阿岚", "老周", "小满", "阿良", "小荷", "阿澄", "小舟", "阿禾", "小野"]
ENTITIES_TITLE = ["项目", "服务", "模块", "系统", "平台"]

NOVEL_DOMAINS = ["天文观测", "园艺种植", "烹饪调味", "钓鱼装备", "露营路线", "陶艺拉坯"]

# 记忆侧的"讨论事项"= 基名 × 面名，同一个话题下 50 个组合互不重复，
# 这样每条查询的正确记忆才是唯一的（否则同一话题下会有几十条近乎相同的候选，
# 标注就失去意义）。查询侧另有一套完全不共享 2-gram 的说法。
FACT_BASE = [
    "最终选择", "定稿方案", "上线口径", "验收标准", "备份策略",
    "排期结论", "回滚条件", "容量下限", "故障口径", "交接清单",
]
FACT_FACET = ["方案", "细节", "边界", "顺序", "口径"]
SIDE_BASE = [
    "接入方式", "回滚流程", "监控口径", "命名规范", "灰度范围",
    "交接方式", "校验规则", "归档要求", "告警门槛", "升级节奏",
]
SIDE_FACET = ["草案", "说明", "约定", "记录", "口径"]
QFACT_BASE = [
    "最后结果", "定下来的做法", "发布标准", "通过条件", "兜底办法",
    "时间安排", "可回退要求", "容量底线", "出错口径", "移交目录",
]
QFACT_FACET = ["做法", "要点", "范围", "次序", "说法"]

CHATTER = [
    "顺手整理了一下桌面", "楼下换了家咖啡店", "出门忘了带伞", "把抽屉清了一遍",
    "傍晚去散了会儿步", "给绿植浇了水", "把旧硬盘翻出来看了看", "换了新的鼠标垫",
    "午休睡了半小时", "把窗台擦了一遍",
]


def content_overlap(query: str, text: str) -> bool:
    """查询与文本是否共享**内容**词。

    只看 2-gram 与长度 ≥2 的拉丁词：单字虚词（的、是、了）在语料里几乎无处不在，
    把它们算成「字面可答」会让这个标记失去意义；qio 的 BM25 里这些字也被 IDF 压得很低。
    """

    def content_tokens(s: str) -> set[str]:
        return {t for t in tokenize(s) if len(t) >= 2}

    return bool(content_tokens(query) & content_tokens(text))

TOOLS = [
    ("memory_search", "在历史记忆里检索与当前问题相关的内容"),
    ("knowledge_lookup", "查询知识库条目，用于事实性回答"),
    ("topic_create", "新建一个话题，用于开启新的讨论方向"),
    ("topic_switch", "把当前讨论切换到另一个已有话题"),
    ("fragment_close", "关闭当前片段并生成结论文本"),
    ("web_search", "联网搜索最新的公开信息"),
    ("file_read", "读取工作区里的文件内容"),
    ("file_write", "把内容写入工作区文件"),
    ("code_run", "在受限子进程里执行一段代码"),
    ("python_test", "运行项目测试并返回结果"),
    ("git_status", "查看工作区改动状态"),
    ("shell_exec", "执行一条本地命令行指令"),
    ("image_view", "查看一张本地图片"),
    ("entity_card_get", "读取实体卡的详细信息"),
    ("entity_card_update", "更新实体卡的摘要与属性"),
    ("calendar_lookup", "查询日程与时间安排"),
    ("reminder_set", "设置一个提醒"),
    ("note_save", "把一条想法保存为笔记"),
    ("table_render", "把数据渲染成表格"),
    ("chart_plot", "把序列数据画成图表"),
    ("json_validate", "校验一段 JSON 是否合法"),
    ("regex_test", "测试一条正则表达式的匹配结果"),
    ("text_diff", "比较两段文本的差异"),
    ("translate_text", "把文本翻译成另一种语言"),
    ("summarize_text", "把长文本压缩成摘要"),
    ("token_count", "估算文本的 token 数量"),
    ("password_strength", "评估口令强度"),
    ("url_fetch", "抓取一个网页的正文"),
    ("rss_read", "读取订阅源的最新条目"),
    ("mail_draft", "起草一封邮件"),
    ("doc_export", "把内容导出成文档"),
    ("slide_build", "生成一份演示文稿"),
    ("sheet_edit", "编辑电子表格里的单元格"),
    ("pdf_extract", "从 PDF 中提取文本"),
    ("audio_transcribe", "把音频转写成文字"),
    ("video_trim", "裁剪一段视频"),
    ("qr_generate", "生成一个二维码"),
    ("hash_compute", "计算一段内容的哈希"),
    ("uuid_generate", "生成一个唯一标识"),
    ("time_now", "读取当前时间"),
    ("timezone_convert", "在不同时区之间换算时间"),
    ("unit_convert", "换算长度、重量、温度等单位"),
    ("currency_convert", "按汇率换算金额"),
    ("dns_lookup", "查询域名的解析记录"),
    ("port_scan_local", "检查本机端口占用"),
    ("disk_usage", "查看磁盘占用情况"),
    ("process_list", "列出正在运行的进程"),
    ("env_read", "读取一个环境变量的值"),
    ("keyring_get", "从本机密钥库读取一个凭据"),
    ("log_tail", "查看最近的运行日志"),
    ("trace_open", "打开一次运行的追踪记录"),
    ("benchmark_run", "运行一次性能基准测试"),
    ("schema_migrate", "执行数据库迁移"),
    ("backup_create", "创建一份数据备份"),
    ("restore_verify", "校验一份备份是否可用"),
    ("cache_clear", "清空缓存"),
    ("index_rebuild", "重建检索索引"),
    ("embedding_encode", "把一段文本编码成向量"),
    ("rerank_pairs", "对候选列表重新排序"),
]


# -- 数据结构 -------------------------------------------------------------


@dataclass(frozen=True)
class Topic:
    id: str
    title: str
    keywords: tuple[str, ...]
    #: 提问用的同义说法（与 title 不共享 2-gram）；硬类别查询只用它
    query_title: str = ""


@dataclass(frozen=True)
class Memory:
    id: str
    text: str
    topic_id: str
    kind: str
    age_days: float
    entity_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class EntityCard:
    id: str
    name: str
    aliases: tuple[str, ...]
    summary: str


@dataclass(frozen=True)
class ToolSpecLite:
    name: str
    description: str


@dataclass(frozen=True)
class RecallCase:
    id: str
    category: str
    query: str
    expected: tuple[str, ...]
    stale: tuple[str, ...]
    keyword_answerable: bool
    #: 查询与正确记忆共享的内容词占查询的比例（0 = 完全不相交）
    overlap: float = 0.0
    #: 难度分层：literal（近似原文）/ partial（部分重合）/ disjoint（零重合）
    tier: str = "literal"


def _overlap_ratio(query: str, text: str) -> float:
    query_tokens = {t for t in tokenize(query) if len(t) >= 2}
    if not query_tokens:
        return 0.0
    text_tokens = {t for t in tokenize(text) if len(t) >= 2}
    return len(query_tokens & text_tokens) / len(query_tokens)


def _tier_of(overlap: float) -> str:
    if overlap >= 0.4:
        return "literal"
    return "partial" if overlap > 0 else "disjoint"


@dataclass(frozen=True)
class TopicCase:
    id: str
    category: str
    message: str
    current_topic_id: str | None
    expected_topic_id: str | None
    expected_mode: str


@dataclass(frozen=True)
class EntityCase:
    id: str
    query: str
    expected_card_id: str


@dataclass(frozen=True)
class ToolCase:
    id: str
    query: str
    expected_tool: str


@dataclass(frozen=True)
class DedupCase:
    id: str
    candidate_name: str
    expected_duplicate: bool
    duplicate_of: str | None


@dataclass(frozen=True)
class StressCorpus:
    seed: int
    topics: list[Topic] = field(default_factory=list)
    memories: list[Memory] = field(default_factory=list)
    entities: list[EntityCard] = field(default_factory=list)
    tools: list[ToolSpecLite] = field(default_factory=list)
    recall_cases: list[RecallCase] = field(default_factory=list)
    topic_cases: list[TopicCase] = field(default_factory=list)
    entity_cases: list[EntityCase] = field(default_factory=list)
    tool_cases: list[ToolCase] = field(default_factory=list)
    dedup_cases: list[DedupCase] = field(default_factory=list)


# -- 生成 -----------------------------------------------------------------


def _build_topics(n_topics: int) -> list[Topic]:
    topics: list[Topic] = []
    for i in range(n_topics):
        domain = DOMAINS[i % len(DOMAINS)]
        aspect = ASPECTS[(i // len(DOMAINS)) % len(ASPECTS)]
        title = f"{domain}{aspect}"
        keywords = tuple(dict.fromkeys(tokenize(title)))[:5]
        query_title = (
            f"{DOMAINS_QUERY[i % len(DOMAINS)]}"
            f"{ASPECTS_QUERY[(i // len(DOMAINS)) % len(ASPECTS)]}"
        )
        topics.append(
            Topic(id=f"t_{i:04d}", title=title, keywords=keywords, query_title=query_title)
        )
    return topics


def _build_entities(n_entities: int) -> list[EntityCard]:
    cards: list[EntityCard] = []
    for i in range(n_entities):
        name = ENTITIES_NAME[i % len(ENTITIES_NAME)]
        title = ENTITIES_TITLE[(i // len(ENTITIES_NAME)) % len(ENTITIES_TITLE)]
        full = f"{name}{i:03d}"
        cards.append(
            EntityCard(
                id=f"e_{i:04d}",
                name=full,
                aliases=(f"{full[:2]}{title}",),
                summary=f"{full} 负责 {DOMAINS[i % len(DOMAINS)]} 相关工作",
            )
        )
    return cards


@dataclass(frozen=True)
class _Block:
    """一"块"记忆：同一个话题下的一组相关片段 + 它的提问说法。"""

    topic: Topic
    entity: EntityCard
    new_fact: Memory
    old_fact: Memory
    side: Memory
    knowledge: Memory
    entity_mem: Memory
    fact_subject: str
    side_subject: str
    query_subject: str


def _generate_blocks(
    rng: random.Random, topics: list[Topic], entities: list[EntityCard], n_memories: int
) -> tuple[list[Memory], list[_Block]]:
    """按块生成记忆。

    每个话题的第 k 块用 (基名 k%10, 面名 (k//10)%5) 组合出**话题内唯一**的讨论事项，
    保证每条查询的正确记忆只有一条 —— 否则同一话题下几十条近乎相同的片段会让标注失去意义。
    """
    memories: list[Memory] = []
    blocks_out: list[_Block] = []
    per_topic: dict[str, int] = {}
    index = 0
    while len(memories) < n_memories:
        topic = topics[index % len(topics)]
        entity = entities[index % len(entities)]
        k = per_topic.get(topic.id, 0)
        per_topic[topic.id] = k + 1
        old_value, new_value = VALUE_PAIRS[k % len(VALUE_PAIRS)]
        reason = REASONS[k % len(REASONS)]
        fact_subject = _subject(FACT_BASE[k % len(FACT_BASE)], FACT_FACET[(k // len(FACT_BASE)) % len(FACT_FACET)])
        side_subject = _subject(SIDE_BASE[k % len(SIDE_BASE)], SIDE_FACET[(k // len(SIDE_BASE)) % len(SIDE_FACET)])
        query_subject = _subject(QFACT_BASE[k % len(QFACT_BASE)], QFACT_FACET[(k // len(QFACT_BASE)) % len(QFACT_FACET)])
        mi = len(memories)
        new_fact = Memory(
            id=f"m_{mi:06d}",
            text=f"{topic.title}的{fact_subject}是{new_value}，理由是{reason}。",
            topic_id=topic.id,
            kind="decision",
            age_days=round(rng.uniform(2, 20), 1),
        )
        old_fact = Memory(
            id=f"m_{mi + 1:06d}",
            text=f"{topic.title}的{fact_subject}是{old_value}，理由是{reason}。",
            topic_id=topic.id,
            kind="decision",
            age_days=round(rng.uniform(60, 140), 1),
        )
        side = Memory(
            id=f"m_{mi + 2:06d}",
            text=f"{topic.title}的{side_subject}是{new_value}，{reason}。",
            topic_id=topic.id,
            kind="decision",
            age_days=round(rng.uniform(10, 45), 1),
        )
        knowledge = Memory(
            id=f"m_{mi + 3:06d}",
            text=f"{topic.title}相关资料：{fact_subject}上 {new_value} 与 {old_value} 各有取舍。",
            topic_id=topic.id,
            kind="knowledge",
            age_days=round(rng.uniform(30, 200), 1),
        )
        entity_mem = Memory(
            id=f"m_{mi + 4:06d}",
            text=f"{entity.name}负责{topic.title}的{side_subject}，当前状态是{reason}。",
            topic_id=topic.id,
            kind="knowledge",
            age_days=round(rng.uniform(5, 60), 1),
            entity_ids=(entity.id,),
        )
        preference = Memory(
            id=f"m_{mi + 5:06d}",
            text=f"用户偏好{topic.title}的{side_subject}先小范围验证，{reason}。",
            topic_id=topic.id,
            kind="preference",
            age_days=round(rng.uniform(20, 120), 1),
        )
        chatter = Memory(
            id=f"m_{mi + 6:06d}",
            text=f"{CHATTER[k % len(CHATTER)]}，没什么要紧的。",
            topic_id=topic.id,
            kind="chatter",
            age_days=round(rng.uniform(0, 3), 1),
        )
        for memory in (new_fact, old_fact, side, knowledge, entity_mem, preference, chatter):
            if len(memories) < n_memories:
                memories.append(memory)
        blocks_out.append(
            _Block(
                topic=topic,
                entity=entity,
                new_fact=new_fact,
                old_fact=old_fact,
                side=side,
                knowledge=knowledge,
                entity_mem=entity_mem,
                fact_subject=fact_subject,
                side_subject=side_subject,
                query_subject=query_subject,
            )
        )
        index += 1
    return memories, blocks_out


def _subject(base: str, facet: str) -> str:
    return f"{base}{facet}"


def _generate_recall_cases(rng: random.Random, blocks: list[_Block], n: int) -> list[RecallCase]:
    """每条查询由某个块派生：正确答案就是这个块里的那条记忆。"""
    texts = {}
    for block in blocks:
        for memory in (block.new_fact, block.old_fact, block.side, block.knowledge, block.entity_mem):
            texts[memory.id] = memory.text
    cases: list[RecallCase] = []
    for i in range(n):
        block = blocks[i % len(blocks)]
        topic = block.topic
        category = CATEGORIES[i % len(CATEGORIES)]
        stale: tuple[str, ...] = ()
        expected: tuple[str, ...]
        if category == "fact_update":
            # 一半字面问法（关键词答得出），一半同义说法（答不出）
            hard = (i // len(CATEGORIES)) % 2 == 1
            query = (
                f"{topic.query_title}那块的{block.query_subject}定了吗？"
                if hard
                else f"{topic.title}的{block.fact_subject}是什么？"
            )
            expected, stale = (block.new_fact.id,), (block.old_fact.id,)
        elif category == "same_topic":
            query = f"{topic.title}的{block.side_subject}是怎么定的？"
            expected = (block.side.id,)
        elif category == "entity_ref":
            golden = block.entity.aliases[0] if i % 2 else block.entity.name
            query = f"{golden}现在负责哪一块？"
            expected = (block.entity_mem.id,)
        elif category == "cross_topic":
            # 一半保留话题原词（部分重合），一半换成同义说法（零重合）
            name = topic.title if (i // len(CATEGORIES)) % 2 == 0 else topic.query_title
            query = f"之前聊过的{name}后来是怎么定的？"
            expected = (block.new_fact.id,)
        elif category == "paraphrase":
            name = topic.title if (i // len(CATEGORIES)) % 2 == 0 else topic.query_title
            query = f"{name}那块最后选的是哪个？"
            expected = (block.new_fact.id,)
        else:  # recent_noise：正确答案是较早的知识条目，语料里有更新的闲聊干扰
            query = f"{topic.query_title}那两套思路谁更合适？"
            expected = (block.knowledge.id,)
        overlap = max(_overlap_ratio(query, texts[m]) for m in expected)
        cases.append(
            RecallCase(
                id=f"r_{i:05d}",
                category=category,
                query=query,
                expected=expected,
                stale=stale,
                keyword_answerable=overlap > 0,
                overlap=round(overlap, 4),
                tier=_tier_of(overlap),
            )
        )
    return cases


def _generate_other_cases(
    rng: random.Random,
    memories: list[Memory],
    topics: list[Topic],
    entities: list[EntityCard],
    n: int,
) -> tuple[list[TopicCase], list[EntityCase], list[ToolCase], list[DedupCase]]:
    topic_cases: list[TopicCase] = []
    for i in range(n):
        mode = ("in_topic", "switch", "new_topic")[i % 3]
        current = topics[i % len(topics)]
        if mode == "in_topic":
            message = f"继续 {current.title} 这块，接着往下说"
            expected_topic, expected_mode = current.id, "in_topic"
        elif mode == "switch":
            other = topics[(i + 7) % len(topics)]
            message = f"换个话题，{other.title} 现在到哪一步了"
            expected_topic, expected_mode = other.id, "switch"
        else:
            novel = NOVEL_DOMAINS[i % len(NOVEL_DOMAINS)]
            message = f"想聊聊{novel}，这跟前面没关系"
            expected_topic, expected_mode = None, "new_topic"
        topic_cases.append(
            TopicCase(
                id=f"tp_{i:05d}",
                category=mode,
                message=message,
                current_topic_id=current.id,
                expected_topic_id=expected_topic,
                expected_mode=expected_mode,
            )
        )

    entity_cases = [
        EntityCase(
            id=f"en_{i:05d}",
            query=f"{entities[i % len(entities)].name}负责什么？",
            expected_card_id=entities[i % len(entities)].id,
        )
        for i in range(n)
    ]
    tool_cases = [
        ToolCase(
            id=f"tl_{i:05d}",
            query=TOOLS[i % len(TOOLS)][1],
            expected_tool=TOOLS[i % len(TOOLS)][0],
        )
        for i in range(n)
    ]
    dedup_cases: list[DedupCase] = []
    for i in range(n):
        if i % 2 == 0:
            topic = topics[i % len(topics)]
            dedup_cases.append(
                DedupCase(
                    id=f"dd_{i:05d}",
                    candidate_name=topic.title,
                    expected_duplicate=True,
                    duplicate_of=topic.id,
                )
            )
        else:
            novel = NOVEL_DOMAINS[i % len(NOVEL_DOMAINS)]
            dedup_cases.append(
                DedupCase(
                    id=f"dd_{i:05d}",
                    candidate_name=f"{novel}{ASPECTS[i % len(ASPECTS)]}",
                    expected_duplicate=False,
                    duplicate_of=None,
                )
            )
    return topic_cases, entity_cases, tool_cases, dedup_cases


def generate(
    seed: int = 20260923,
    n_memories: int = 10_000,
    n_topics: int = 200,
    n_queries: int = 1_000,
    n_entities: int = 200,
    n_tools: int = 60,
) -> StressCorpus:
    rng = random.Random(seed)
    topics = _build_topics(n_topics)
    entities = _build_entities(n_entities)
    memories, blocks = _generate_blocks(rng, topics, entities, n_memories)
    recall_cases = _generate_recall_cases(rng, blocks, n_queries)
    topic_cases, entity_cases, tool_cases, dedup_cases = _generate_other_cases(
        rng, memories, topics, entities, n_queries
    )
    return StressCorpus(
        seed=seed,
        topics=topics,
        memories=memories,
        entities=entities,
        tools=[ToolSpecLite(name=n, description=d) for n, d in TOOLS[:n_tools]],
        recall_cases=recall_cases,
        topic_cases=topic_cases,
        entity_cases=entity_cases,
        tool_cases=tool_cases,
        dedup_cases=dedup_cases,
    )
```

### `backend/src/agent/eval/stress_metrics.py`

指标：召回/分类/二分类/排序/延迟

```python
"""压力实验的指标计算：纯函数，不碰模型也不碰数据库。

召回类指标与既有 `retrieval_eval` 保持同一口径（前 1 命中、前 k 命中、MRR、
错误记忆注入、陈旧知识注入），另外加了分层：把"关键词路径字面答不出"的子集
单独算一遍 —— 模型的增量价值主要落在那上面。
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from agent.eval.stress_corpus import RecallCase


def _rate(num: float, den: int) -> float:
    return round(num / den, 4) if den else 0.0


def recall_metrics(
    ranked: Sequence[Sequence[str]], cases: Sequence[RecallCase], k: int = 5
) -> dict[str, Any]:
    """`ranked[i]` 是第 i 条用例的召回结果（按相关性降序的 doc_id）。"""
    hit1 = hitk = wrong = stale_above = 0
    rr_sum = 0.0
    for ids, case in zip(ranked, cases):
        expected = set(case.expected)
        stale = set(case.stale)
        top1 = set(ids[:1])
        if top1 & expected:
            hit1 += 1
        if set(ids[:k]) & expected:
            hitk += 1
        else:
            wrong += 1
        for i, doc_id in enumerate(ids, start=1):
            if doc_id in expected:
                rr_sum += 1.0 / i
                break
        first_expected = next((i for i, d in enumerate(ids) if d in expected), None)
        if any(
            d in stale and (first_expected is None or i < first_expected)
            for i, d in enumerate(ids)
        ):
            stale_above += 1
    n = len(cases)
    return {
        "n": n,
        "recall@1": _rate(hit1, n),
        f"recall@{k}": _rate(hitk, n),
        "mrr": round(rr_sum / n, 4) if n else 0.0,
        "wrong_memory_injection_rate": _rate(wrong, n),
        "stale_knowledge_injection_rate": _rate(stale_above, n),
    }


def stratify_recall(
    ranked: Sequence[Sequence[str]], cases: Sequence[RecallCase], k: int = 5
) -> dict[str, Any]:
    """整体 + 两个子集（关键词能答 / 答不出）+ 按类别的细表。"""
    def subset(pred: Callable[[RecallCase], bool]) -> dict[str, Any]:
        idx = [i for i, c in enumerate(cases) if pred(c)]
        if not idx:
            return {"n": 0}
        return recall_metrics([ranked[i] for i in idx], [cases[i] for i in idx], k=k)

    out: dict[str, Any] = {
        "all": recall_metrics(ranked, cases, k=k),
        "keyword_answerable": subset(lambda c: c.keyword_answerable),
        "keyword_unanswerable": subset(lambda c: not c.keyword_answerable),
        "by_category": {},
        "by_tier": {},
    }
    for category in sorted({c.category for c in cases}):
        out["by_category"][category] = subset(lambda c, cat=category: c.category == cat)
    for tier in ("literal", "partial", "disjoint"):
        out["by_tier"][tier] = subset(lambda c, want=tier: c.tier == want)
    return out


def classification_metrics(
    rows: Sequence[dict[str, Any]], *, key: str = "predicted", expected_key: str = "expected"
) -> dict[str, Any]:
    """通用分类指标：正确率 + 混淆计数（话题判定、实体匹配、去重都用它）。"""
    n = len(rows)
    correct = sum(1 for r in rows if r.get(key) == r.get(expected_key))
    counts: dict[str, int] = {}
    for r in rows:
        pair = f"{r.get(expected_key)}→{r.get(key)}"
        counts[pair] = counts.get(pair, 0) + 1
    return {"n": n, "accuracy": _rate(correct, n), "confusions": counts}


def binary_metrics(
    rows: Sequence[dict[str, Any]], *, key: str = "predicted", expected_key: str = "expected"
) -> dict[str, Any]:
    """二分类指标：真阳/假阳/真阴/假阴，用于"是否重复话题"这类判定。"""
    tp = fp = tn = fn = 0
    for r in rows:
        want, got = bool(r.get(expected_key)), bool(r.get(key))
        if want and got:
            tp += 1
        elif want and not got:
            fn += 1
        elif not want and got:
            fp += 1
        else:
            tn += 1
    return {
        "n": len(rows),
        "accuracy": _rate(tp + tn, len(rows)),
        "recall": _rate(tp, tp + fn),
        "false_positive_rate": _rate(fp, fp + tn),
        "confusions": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
    }


def ranking_metrics(
    rows: Sequence[dict[str, Any]], *, k: int = 5
) -> dict[str, Any]:
    """"目标是否进入候选 + 平均位次"：工具路由用。"""
    n = len(rows)
    if not n:
        return {"n": 0}
    hit = sum(1 for r in rows if r.get("expected") in (r.get("ranked") or [])[:k])
    positions = [
        (r["ranked"].index(r["expected"]) + 1)
        for r in rows
        if r.get("expected") in (r.get("ranked") or [])
    ]
    return {
        "n": n,
        f"hit@{k}": _rate(hit, n),
        "mean_position": round(sum(positions) / len(positions), 2) if positions else None,
    }


def latency_summary(samples_ms: Sequence[float]) -> dict[str, Any]:
    if not samples_ms:
        return {"n": 0}
    ordered = sorted(samples_ms)

    def pick(p: float) -> float:
        idx = min(len(ordered) - 1, int(round(p * (len(ordered) - 1))))
        return round(ordered[idx], 2)

    return {
        "n": len(ordered),
        "mean_ms": round(sum(ordered) / len(ordered), 2),
        "p50_ms": pick(0.5),
        "p90_ms": pick(0.9),
        "max_ms": round(ordered[-1], 2),
    }
```

### `backend/src/agent/eval/stress_arms.py`

A 臂（规则+BM25）与 B 臂（本地 embedding）

```python
"""三条臂：把同一份压力语料喂进 qio 的真实生产入口。

A 臂 = 规则 + 关键词；B 臂 = 规则 + 本地 embedding（生产上模型可用时的完整链路）。
两臂共用同一批用例，所以每个数字都是同题对照。

几处刻意的做法：

- 检索只建一次索引、跑全部查询 —— 生产也是这样（记忆库是常驻索引），
  逐个用例重建索引会把"每条查询的真实代价"算歪。
- 本地模型后端**整个工作跑一个实例**：话题向量与实体卡向量在生产里是持久化复用的，
  每个用例重建既慢又不真实。
- 实体匹配的规则臂用"名称/别名包含"（生产 `EntityCardService.match_cards` 的规则），
  去重的规则臂用名称 Jaccard ≥ 0.8（生产 `CreateTopicTool.NAME_SIM_THRESHOLD`）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from agent.eval.embedding_backend import build_backend
from agent.eval.stress_corpus import Memory, StressCorpus
from agent.selector.base import IndexedDoc
from agent.selector.bm25 import BM25Backend
from agent.selector.selector import Selector
from agent.services import params
from agent.services.affinity import classify
from agent.services.decay import DecayPolicy
from agent.services.predict import TopicPredictor
from agent.services.retrieval import RetrievalConfig, Retriever
from agent.services.tool_router import ToolRouter

NAME_SIM_THRESHOLD = 0.8
EMBED_LO = 0.5
EMBED_HI = 0.7


@dataclass
class _Fingerprint:
    topic_id: str
    title: str
    keywords: list[str]
    summary_preview: str = ""


class _TopicsStub:
    """把语料里的全部话题喂给 TopicPredictor（生产里这来自 TopicService）。"""

    def __init__(self, topics) -> None:
        self._fps = [_Fingerprint(t.id, t.title, list(t.keywords)) for t in topics]
        self.nodes = _NodesStub([t.id for t in topics])

    def list_with_fingerprints(self):
        return self._fps

    def fingerprint(self, topic_id: str):
        for fp in self._fps:
            if fp.topic_id == topic_id:
                return fp
        return None


class _TopicNode:
    """话题节点替身：`meta` 里在生产中有 `ended_at` 等状态，这里一律视为未结束。"""

    def __init__(self) -> None:
        self.meta: dict[str, Any] = {}


class _NodesStub:
    def __init__(self, ids: list[str]) -> None:
        self._ids = set(ids)

    def get_topic(self, topic_id: str):
        return _TopicNode() if topic_id in self._ids else None


def _iso(age_days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()


def _index_docs(memories: list[Memory]) -> tuple[list[IndexedDoc], dict[str, str], dict[str, str]]:
    docs: list[IndexedDoc] = []
    ages: dict[str, str] = {}
    kinds: dict[str, str] = {}
    for m in memories:
        docs.append(
            IndexedDoc(
                doc_id=m.id,
                text=m.text,
                topic_id=m.topic_id,
                keywords=[],
                created_at=_iso(m.age_days),
            )
        )
        ages[m.id] = _iso(m.age_days)
        kinds[m.id] = m.kind
    return docs, ages, kinds


def _jaccard(a: str, b: str) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


class RulesArm:
    name = "rules"

    def __init__(self, corpus: StressCorpus) -> None:
        self.corpus = corpus
        self._docs, self._ages, self._kinds = _index_docs(corpus.memories)
        self._selector: Selector | None = None

    def _retriever(self) -> Retriever:
        if self._selector is None:
            self._selector = Selector()
            self._selector.load(self._docs)
        rp = params.RETRIEVAL
        retriever = Retriever(
            self._selector,
            _TopicsStub(self.corpus.topics),
            config=RetrievalConfig(
                relevance_weight=rp.relevance_weight,
                recency_weight=rp.recency_weight,
                affinity_weight=rp.affinity_weight,
                recency_half_life_days=rp.recency_half_life_days,
            ),
            conn=None,
            decay=DecayPolicy(),
        )
        retriever._created_at = lambda doc_id: self._ages.get(doc_id)
        retriever._preview = lambda doc_id, title: ""
        retriever.kind_of = lambda doc_id: self._kinds.get(doc_id, "ephemeral")
        return retriever

    def recall(self, cases, k: int = 5):
        retriever = self._retriever()
        ranked: list[list[str]] = []
        latencies: list[float] = []
        for case in cases:
            started = time.perf_counter()
            hits = retriever.search(case.query, top_k=k)
            latencies.append((time.perf_counter() - started) * 1000)
            ranked.append([h.doc_id for h in hits])
        return ranked, latencies

    def topic(self, cases):
        predictor = TopicPredictor(None, None, _TopicsStub(self.corpus.topics))
        return _run_topic(predictor, cases)

    def entity(self, cases):
        rows: list[dict[str, Any]] = []
        latencies: list[float] = []
        for case in cases:
            started = time.perf_counter()
            predicted = None
            for card in self.corpus.entities:
                if case.query.find(card.name) >= 0 or any(
                    a and case.query.find(a) >= 0 for a in card.aliases
                ):
                    predicted = card.id
                    break
            latencies.append((time.perf_counter() - started) * 1000)
            rows.append(
                {"id": case.id, "expected": case.expected_card_id, "predicted": predicted}
            )
        return rows, latencies

    def tool(self, cases):
        specs = [
            SimpleNamespace(name=t.name, description=t.description) for t in self.corpus.tools
        ]
        router = ToolRouter(embedding=None)
        return _run_tool(router, specs, cases)

    def dedup(self, cases):
        rows: list[dict[str, Any]] = []
        latencies: list[float] = []
        for case in cases:
            started = time.perf_counter()
            predicted = None
            for topic in self.corpus.topics:
                if _jaccard(case.candidate_name, topic.title) >= NAME_SIM_THRESHOLD:
                    predicted = topic.id
                    break
            latencies.append((time.perf_counter() - started) * 1000)
            rows.append(
                {
                    "id": case.id,
                    "expected": case.expected_duplicate,
                    "expected_duplicate_of": case.duplicate_of,
                    "predicted": predicted is not None,
                    "predicted_duplicate_of": predicted,
                }
            )
        return rows, latencies


class LocalEmbeddingArm(RulesArm):
    """在规则之上把本地模型接进判定（生产上模型可用时的链路）。"""

    name = "local"

    def __init__(self, corpus: StressCorpus, model_dir=None) -> None:
        super().__init__(corpus)
        backend, note = build_backend(model_dir)
        if backend is None:
            raise RuntimeError(f"本地嵌入模型不可用：{note}")
        self.backend = backend
        self.note = note
        self._topics_warm = False
        self._entities_warm = False

    def _retriever(self) -> Retriever:
        if self._selector is None:
            self._selector = Selector(
                recall=self.backend, fallback_recall=BM25Backend()
            )
            self._selector.load(self._docs)
        rp = params.RETRIEVAL
        retriever = Retriever(
            self._selector,
            _TopicsStub(self.corpus.topics),
            config=RetrievalConfig(
                relevance_weight=rp.relevance_weight,
                recency_weight=rp.recency_weight,
                affinity_weight=rp.affinity_weight,
                recency_half_life_days=rp.recency_half_life_days,
            ),
            conn=None,
            decay=DecayPolicy(),
        )
        retriever._created_at = lambda doc_id: self._ages.get(doc_id)
        retriever._preview = lambda doc_id, title: ""
        retriever.kind_of = lambda doc_id: self._kinds.get(doc_id, "ephemeral")
        return retriever

    def topic(self, cases):
        return _run_topic(self._predictor(), cases)

    def dedup(self, cases):
        predictor = self._predictor()
        rows: list[dict[str, Any]] = []
        latencies: list[float] = []
        for case in cases:
            started = time.perf_counter()
            predicted = None
            for topic in self.corpus.topics:
                if _jaccard(case.candidate_name, topic.title) >= NAME_SIM_THRESHOLD:
                    predicted = topic.id
                    break
            if predicted is None:
                prediction = predictor.predict(case.candidate_name, current_topic_id=None)
                scores = dict(getattr(prediction, "scores", None) or {})
                if scores:
                    top_id = max(scores, key=scores.get)
                    top = scores[top_id]
                    if EMBED_LO <= top < EMBED_HI:
                        predicted = top_id
            latencies.append((time.perf_counter() - started) * 1000)
            rows.append(
                {
                    "id": case.id,
                    "expected": case.expected_duplicate,
                    "expected_duplicate_of": case.duplicate_of,
                    "predicted": predicted is not None,
                    "predicted_duplicate_of": predicted,
                }
            )
        return rows, latencies

    def entity(self, cases):
        if not self._entities_warm:
            for card in self.corpus.entities:
                self.backend.save_entity_card_vector(
                    card.id, f"{card.name} {' '.join(card.aliases)} {card.summary}"
                )
            self._entities_warm = True
        rows: list[dict[str, Any]] = []
        latencies: list[float] = []
        for case in cases:
            started = time.perf_counter()
            hits = self.backend.entity_card_search(case.query, top_k=1)
            latencies.append((time.perf_counter() - started) * 1000)
            predicted = hits[0][0] if hits else None
            rows.append(
                {"id": case.id, "expected": case.expected_card_id, "predicted": predicted}
            )
        return rows, latencies

    def tool(self, cases):
        specs = [
            SimpleNamespace(name=t.name, description=t.description) for t in self.corpus.tools
        ]
        router = ToolRouter(embedding=self.backend)
        return _run_tool(router, specs, cases)

    def _predictor(self) -> TopicPredictor:
        predictor = TopicPredictor(None, self.backend, _TopicsStub(self.corpus.topics))
        if not self._topics_warm:
            for topic in self.corpus.topics:
                fingerprint = predictor.topics.fingerprint(topic.id)
                self.backend.update_topic_vector(
                    topic.id, predictor._fingerprint_text(fingerprint)
                )
            self._topics_warm = True
        return predictor


def _run_topic(predictor: TopicPredictor, cases):
    rows: list[dict[str, Any]] = []
    latencies: list[float] = []
    for case in cases:
        started = time.perf_counter()
        prediction = predictor.predict(case.message, current_topic_id=case.current_topic_id)
        decision = classify(case.message, prediction, case.current_topic_id, [])
        latencies.append((time.perf_counter() - started) * 1000)
        rows.append(
            {
                "id": case.id,
                "expected": case.expected_mode,
                "predicted": decision.mode.value,
                "expected_topic": case.expected_topic_id,
                "predicted_topic": prediction.main_topic_id,
                "backend": prediction.backend_used,
            }
        )
    return rows, latencies


def _run_tool(router: ToolRouter, specs, cases):
    order = [s.name for s in specs]
    rows: list[dict[str, Any]] = []
    latencies: list[float] = []
    for case in cases:
        started = time.perf_counter()
        picked = router.route(case.query, specs)
        latencies.append((time.perf_counter() - started) * 1000)
        picked_names = [s.name for s in picked]
        rest = [n for n in order if n not in picked_names]
        rows.append(
            {
                "id": case.id,
                "expected": case.expected_tool,
                "ranked": picked_names + rest,
                "predicted": picked_names[0] if picked_names else None,
            }
        )
    return rows, latencies
```

### `backend/src/agent/eval/stress_jev_arm.py`

C 臂（Jev）：话题判定/实体/工具/去重

```python
"""C 臂：用 Jev 做除召回以外的五项判定。

Jev 没有向量输出，所以召回这一项直接 `NotImplementedError` —— 这不是偷懒，
是它的形态决定的（调研结论）。

每项工作都把「候选集合」放进 state/criteria，让 Jev 在闭合集合里选，
这样答案一定落在我们自己定义的标签空间里，不需要解析自由文本。
"""

from __future__ import annotations

from typing import Any

from agent.eval.stress_corpus import StressCorpus


class JevArm:
    name = "jev"

    def __init__(self, client: Any, *, max_topics: int = 200) -> None:
        self.client = client
        self.max_topics = max_topics
        self._topics: list = []
        self._entities: list = []
        self._tools: list = []

    def recall(self, cases, k: int = 5):
        raise NotImplementedError("Jev 没有向量输出，无法参与召回")

    def topic(self, cases):
        criteria = {
            t.id: f"{t.title} {(' '.join(t.keywords) if t.keywords else '')}".strip()
            for t in self._topics
        }
        criteria["__new__"] = "与上面所有已有话题都不属于同一个方向的新主题"
        rows: list[dict[str, Any]] = []
        latencies: list[float] = []
        for case in cases:
            answer = self.client.ask(
                {"message": case.message, "current_topic": case.current_topic_id},
                {
                    "topic": {
                        "type": "choice",
                        "instructions": "这条消息属于哪个已有话题？如果它是与现有话题都不同的新方向，选 __new__。",
                        "criteria": criteria,
                    }
                },
            )
            picked = answer.choice("topic")
            if picked == "__new__" or picked is None:
                mode, predicted_topic = "new_topic", None
            elif picked == case.current_topic_id:
                mode, predicted_topic = "in_topic", picked
            else:
                mode, predicted_topic = "switch", picked
            latencies.append(answer.latency_ms)
            rows.append(
                {
                    "id": case.id,
                    "expected": case.expected_mode,
                    "predicted": mode,
                    "expected_topic": case.expected_topic_id,
                    "predicted_topic": predicted_topic,
                }
            )
        return rows, latencies

    def entity(self, cases):
        criteria = {c.id: f"{c.name}（{c.summary[:30]}）" for c in self._entities}
        criteria["__none__"] = "没有任何一张卡与这条消息指的是同一个对象"
        rows: list[dict[str, Any]] = []
        latencies: list[float] = []
        for case in cases:
            answer = self.client.ask(
                {"message": case.query},
                {
                    "card": {
                        "type": "choice",
                        "instructions": "这条消息在说哪个对象？没有匹配就选 __none__。",
                        "criteria": criteria,
                    }
                },
            )
            picked = answer.choice("card")
            latencies.append(answer.latency_ms)
            rows.append(
                {
                    "id": case.id,
                    "expected": case.expected_card_id,
                    "predicted": None if picked == "__none__" else picked,
                }
            )
        return rows, latencies

    def tool(self, cases):
        criteria = {t.name: t.description for t in self._tools}
        rows: list[dict[str, Any]] = []
        latencies: list[float] = []
        for case in cases:
            answer = self.client.ask(
                {"request": case.query},
                {
                    "tool": {
                        "type": "choice",
                        "instructions": "这个请求最该用哪个工具？",
                        "criteria": criteria,
                    }
                },
            )
            picked = answer.choice("tool")
            latencies.append(answer.latency_ms)
            rows.append(
                {
                    "id": case.id,
                    "expected": case.expected_tool,
                    "ranked": [picked] if picked else [],
                    "predicted": picked,
                }
            )
        return rows, latencies

    def dedup(self, cases):
        titles = [t.title for t in self._topics]
        rows: list[dict[str, Any]] = []
        latencies: list[float] = []
        for case in cases:
            answer = self.client.ask(
                {"new_topic_name": case.candidate_name, "existing_topics": titles},
                {
                    "duplicate": {
                        "type": "noul",
                        "instructions": "这个新话题名和已有话题里的某一个是不是在讲同一件事？",
                        "criteria": {
                            "true": "与某个已有话题指向同一个讨论对象",
                            "false": "是已有话题都没覆盖的新方向",
                        },
                    }
                },
            )
            value = answer.noul("duplicate")
            latencies.append(answer.latency_ms)
            rows.append(
                {
                    "id": case.id,
                    "expected": case.expected_duplicate,
                    "expected_duplicate_of": case.duplicate_of,
                    "predicted": value >= 0.5,
                    "predicted_duplicate_of": None,
                }
            )
        return rows, latencies

    def attach(self, corpus: StressCorpus) -> "JevArm":
        """把语料里的话题与实体挂上（跑批器调用）。"""
        self._topics = list(corpus.topics)[: self.max_topics]
        self._entities = list(corpus.entities)
        self._tools = list(corpus.tools)
        return self

    def usage(self) -> dict[str, Any]:
        return self.client.summary() if hasattr(self.client, "summary") else {}
```

### `backend/src/agent/eval/stress_rerank.py`

候选重排三件套 + 条件触发

```python
"""候选重排的三种做法，用于测量「重排到底能带来多少收益」。

- `IdentityRerank`：原序（对照组，验证实现没出错）；
- `LocalVectorRerank`：本地双编码器重排 —— 与召回同一套算法，预期收益接近零；
- `JevRerank`：一次请求对每条候选问一个"是否回答了这个问题"，按概率重排。

重排只能改顺序、不能改候选集，所以**候选集合本身**在三种做法下必须完全相同。
（注意：recall@5 不是不变量 —— 它看的是前 5 名，重排会改变谁落进前 5；
真正的不变量是"返回的 id 多重集与输入一致"。第一次实现时我把它当成不变量，
结果它反而帮我发现了"问题没有指向具体候选"这个 bug。）
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence


@dataclass(frozen=True)
class Candidate:
    doc_id: str
    score: float
    text: str = ""


class IdentityRerank:
    name = "identity"

    def rerank(self, query: str, candidates: Sequence[Candidate]) -> list[str]:
        return [c.doc_id for c in candidates]


class LocalVectorRerank:
    """用查询向量与候选文本的余弦重新排序（与召回同算法，作为对照）。"""

    name = "local_vector"

    def __init__(self, backend: Any) -> None:
        self.backend = backend
        self.calls = 0

    def rerank(self, query: str, candidates: Sequence[Candidate]) -> list[str]:
        import numpy as np

        if not candidates:
            return []
        vectors = self.backend.embed_texts([query] + [c.text for c in candidates])
        self.calls += 1
        if vectors is None:
            return [c.doc_id for c in candidates]
        query_vec = vectors[0]
        matrix = vectors[1:]
        scores = matrix @ query_vec
        order = np.argsort(-scores)
        return [candidates[i].doc_id for i in order]


class JevRerank:
    """一次请求带 N 个 noul 问题；按"是否回答了这个问题"的概率重排。"""

    name = "jev"
    # 每个问题必须**显式引用**对应候选（TypeSafe 用反引号指向 state 字段），
    # 否则 N 个问题问的是同一句话，模型给出同一批概率，排序退化成按 id 排 ——
    # 这个 bug 是靠"recall@5 必须不变"这条哨兵指标抓出来的。
    PROMPT = "`candidates.c{i}` 这条候选内容是否真的回答了 `question`？只按语义相关性判断，不要因为用词相近就判是。"

    def __init__(self, client: Any, *, max_candidates: int = 12) -> None:
        self.client = client
        self.max_candidates = max_candidates
        self.calls = 0
        self.failures = 0
        self.latencies_ms: list[float] = []

    def rerank(self, query: str, candidates: Sequence[Candidate]) -> list[str]:
        chosen = list(candidates)[: self.max_candidates]
        if not chosen:
            return []
        questions = {
            f"c{i}": {
                "type": "noul",
                "instructions": self.PROMPT.replace("{i}", str(i)),
            }
            for i in range(len(chosen))
        }
        state = {
            "question": query,
            "candidates": {f"c{i}": c.text[:600] for i, c in enumerate(chosen)},
        }
        try:
            answer = self.client.ask(state, questions)
        except Exception:
            self.failures += 1
            return [c.doc_id for c in chosen]
        self.calls += 1
        self.latencies_ms.append(answer.latency_ms)
        scored = [
            (answer.noul(f"c{i}", default=0.0), c.doc_id) for i, c in enumerate(chosen)
        ]
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        head = [doc_id for _, doc_id in scored]
        tail = [c.doc_id for c in candidates[len(chosen):]]
        return head + tail


class ConditionalRerank:
    """只在"排序不可信"时才调重排：顶部两条的相对分差小于阈值就触发。

    判据只用召回阶段已有的分数，不产生额外调用 —— 这是它能把成本压下来的原因。
    """

    name = "conditional"

    def __init__(self, reranker: Any, *, relative_gap: float = 0.15) -> None:
        self.reranker = reranker
        self.relative_gap = relative_gap
        self.triggered = 0
        self.skipped = 0

    def should_rerank(self, candidates: Sequence[Candidate]) -> bool:
        if len(candidates) < 2:
            return False
        first, second = candidates[0].score, candidates[1].score
        denom = abs(first) if abs(first) > 1e-9 else 1.0
        return (first - second) / denom < self.relative_gap

    def rerank(self, query: str, candidates: Sequence[Candidate]) -> list[str]:
        if not self.should_rerank(candidates):
            self.skipped += 1
            return [c.doc_id for c in candidates]
        self.triggered += 1
        return self.reranker.rerank(query, candidates)


def run_rerank(
    reranker: Any,
    cases: Sequence[Any],
    candidate_lists: Sequence[Sequence[Candidate]],
    *,
    k: int = 5,
) -> tuple[list[list[str]], list[float]]:
    """对每条用例应用一次重排，返回排名列表与耗时。"""
    ranked: list[list[str]] = []
    latencies: list[float] = []
    for case, candidates in zip(cases, candidate_lists):
        started = time.perf_counter()
        ranked.append(reranker.rerank(case.query, candidates))
        latencies.append((time.perf_counter() - started) * 1000)
    return ranked, latencies
```

### `backend/src/agent/eval/stress_run.py`

合成语料的跑批器与 CLI

```python
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
```

### `backend/src/agent/eval/jev_client.py`

Jev 客户端（OpenRouter Decisions）

```python
"""Jev 客户端：走 OpenRouter 的 Decisions 接口。

Jev 是 TypeSafe 的 System One 判定模型，不做召回也不生成文本：发一个 `state`
加一组类型化问题（choice / score / noul），拿回带概率的类型化答案。OpenRouter
上的模型 id 是 `typesafe/jev-1.13`（别名 `~typesafe/jev-latest`），计费记在
OpenRouter 账户上，只收输入 token。

安全约定：密钥只从参数或环境变量读（Windows 上再回落到注册表 User 作用域），
任何异常消息、日志、结果文件里都不得出现密钥原文。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

try:
    import truststore

    truststore.inject_into_ssl()
except Exception:
    pass

DEFAULT_MODEL = "typesafe/jev-1.13"
DEFAULT_URL = "https://openrouter.ai/api/alpha/decisions"
ENV_VAR = "OPENROUTER_API_KEY"


class JevUnavailable(RuntimeError):
    """Jev 调用不可用（缺 key、网络失败、HTTP 错误、限流）。"""


def mask(secret: str | None) -> str:
    """把密钥变成可安全打印的形式。"""
    if not secret:
        return "(empty)"
    if len(secret) <= 12:
        return "***"
    return f"{secret[:8]}…{secret[-4:]}"


def _registry_key() -> str:
    """Windows 注册表 User 作用域里的 OPENROUTER_API_KEY（非 Windows 返回空）。"""
    if os.name != "nt":
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, ENV_VAR)
            return str(value or "")
    except Exception:
        return ""


def resolve_api_key(explicit: str | None = None) -> str:
    """密钥来源顺序：显式参数 → 进程环境变量 → 注册表 User 作用域。"""
    return (explicit or os.environ.get(ENV_VAR) or _registry_key() or "").strip()


@dataclass
class JevAnswer:
    answers: dict[str, Any] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    model: str = ""

    def noul(self, question_id: str, default: float = 0.5) -> float:
        got = self.answers.get(question_id) or {}
        value = got.get("noul")
        return float(value) if isinstance(value, (int, float)) else default

    def choice(self, question_id: str) -> str | None:
        got = self.answers.get(question_id) or {}
        value = got.get("choice")
        return str(value) if value is not None else None

    def score(self, question_id: str) -> float | None:
        got = self.answers.get(question_id) or {}
        value = got.get("score")
        return float(value) if isinstance(value, (int, float)) else None


class JevClient:
    """薄客户端：一次请求可带多个问题，同时累计用量与成本。"""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = DEFAULT_MODEL,
        url: str = DEFAULT_URL,
        timeout: float = 30.0,
        http_client: httpx.Client | None = None,
        max_retries: int = 2,
    ) -> None:
        self.api_key = resolve_api_key(api_key)
        self.model = model
        self.url = url
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = http_client
        self.requests = 0
        self.failures = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost_usd = 0.0
        self.latencies_ms: list[float] = []

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def ask(self, state: Any, questions: dict[str, Any], *, model: str | None = None) -> JevAnswer:
        if not self.configured:
            raise JevUnavailable(
                f"缺少 {ENV_VAR}：请设置环境变量或显式传入 api_key（当前为空）"
            )
        payload = {"model": model or self.model, "state": state, "questions": questions}
        last_error = ""
        for attempt in range(self.max_retries + 1):
            started = time.perf_counter()
            try:
                response = self._http().post(
                    self.url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    json=payload,
                    timeout=self.timeout,
                )
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {mask(str(exc))}"
                if attempt >= self.max_retries:
                    self.failures += 1
                    raise JevUnavailable(f"Jev 请求失败（已重试 {attempt} 次）：{last_error}") from None
                time.sleep(0.5 * (attempt + 1))
                continue
            latency_ms = (time.perf_counter() - started) * 1000
            if response.status_code >= 400:
                detail = _safe_detail(response)
                if response.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    last_error = f"HTTP {response.status_code} {detail}"
                    time.sleep(1.0 * (attempt + 1))
                    continue
                self.failures += 1
                raise JevUnavailable(f"Jev 返回 HTTP {response.status_code}：{detail}")
            data = response.json()
            usage = data.get("usage") or {}
            answer = JevAnswer(
                answers=data.get("answers") or {},
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                cost_usd=float(usage.get("cost") or 0.0),
                latency_ms=round(latency_ms, 2),
                model=str(data.get("model") or payload["model"]),
            )
            self.requests += 1
            self.total_input_tokens += answer.input_tokens
            self.total_output_tokens += answer.output_tokens
            self.total_cost_usd += answer.cost_usd
            self.latencies_ms.append(answer.latency_ms)
            return answer
        raise JevUnavailable(f"Jev 请求失败：{last_error}")

    def summary(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "failures": self.failures,
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "cost_usd": round(self.total_cost_usd, 6),
            "key": mask(self.api_key),
        }


def _safe_detail(response: httpx.Response) -> str:
    """错误详情可能回显密钥原文：形如 sk-or… 的片段一律整串替换掉。

    这里比 `mask()` 更严格 —— `mask()` 是给"当前用的是哪把 key"这种主动展示用的，
    而错误正文来自外部，不能保证片段长度，索性不给任何残留。
    """
    try:
        text = response.text
    except Exception:
        text = ""
    out = []
    for piece in text[:300].split():
        stripped = piece.strip('",')
        out.append("[redacted-key]" if stripped.startswith("sk-or") else piece)
    return " ".join(out)
```

### `backend/src/agent/eval/embedding_backend.py`

评测用的本地嵌入后端装配

```python
"""评测用的真实嵌入后端装配（只服务 agent/eval，不进生产链路）。

评测要回答的问题是「内置模型在现有用例上得几分」，所以这里只做一件事：
按与生产同源的方式把 `OnnxEmbeddingBackend` 装起来。两个刻意的选择：

1. **内存库**：每次装配都用新的 `:memory:` 连接并跑迁移。评测既不碰用户的
   `app.db`，也不让上一次运行留下的向量影响这一次。
2. **每个用例一个新实例**：后端实例内部按话题 id / 文档 id 缓存向量，跨用例
   复用会把 A 用例算出的向量喂给 B 用例（不同用例里话题 id 会重复）。代价是
   每个用例重新加载一次 ONNX 会话 —— 用例只有几十条，这个代价可以接受。

模型找不到时返回 `None`，调用方退回原来的确定性路径，并在结果里说明原因。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from agent.config import Settings
from agent.storage.migrate import apply_migrations

MODEL_SUBDIR = "bge-small-zh-v1.5"


def resolve_model_dir(explicit: str | Path | None = None) -> Path:
    """模型目录：显式参数 > `QIO_MODELS_DIR` > 与生产同源的 `data_dir/models`。"""
    if explicit:
        return Path(explicit)
    base = os.environ.get("QIO_MODELS_DIR")
    root = Path(base) if base else Settings().data_dir / "models"
    return root / MODEL_SUBDIR


def _memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.isolation_level = None  # autocommit，与 storage/db.connect 一致
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)
    return conn


def build_backend(model_dir: str | Path | None = None) -> tuple[Any | None, str]:
    """返回 `(嵌入后端, 说明)`；模型不可用时后端为 `None`，说明里写清找过哪里。"""
    from agent.selector.onnx import OnnxEmbeddingBackend

    directory = resolve_model_dir(model_dir)
    backend = OnnxEmbeddingBackend(_memory_conn(), model_dir=directory)
    if not backend.available():
        return None, f"嵌入模型不可用：{directory}（缺 model.onnx 或 tokenizer.json）"
    return backend, backend.model_identity
```

### `backend/src/agent/eval/retrieval_eval.py`

检索评测（本次改造：可注入召回后端）

```python
"""Retrieval evaluator (deterministic, offline).

Uses the production recall (Selector/BM25) + production ranking weights
(RetrievalPolicy) + production decay (DecayPolicy). No DB and no network:
timestamps and kinds come from the case data via small overrides, so results
are reproducible on any machine.

Metrics: Recall@1, Recall@5, MRR, wrong-memory injection rate,
stale-knowledge injection rate.

默认走关键词后端（不需要模型、任何机器上结果一致）。传入 `recall_factory`
时改用它生产的那条召回路径（例如内置 ONNX 嵌入模型）：工厂**每个用例调一次**，
因为后端实例会把向量缓存在自己身上，跨用例复用会串味。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from agent.selector.base import IndexedDoc, RecallBackend
from agent.selector.bm25 import BM25Backend
from agent.selector.selector import Selector
from agent.services import params
from agent.services.decay import DecayPolicy
from agent.services.retrieval import RetrievalConfig, Retriever


class _StubTopics:
    def list_with_fingerprints(self):
        return []


def load_cases(path: str | Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def rank_case(
    case: dict[str, Any],
    top_k: int = 5,
    *,
    recall_factory: Callable[[], RecallBackend | None] | None = None,
) -> list[str]:
    docs = [
        IndexedDoc(
            doc_id=d["id"],
            text=d["text"],
            topic_id=d.get("topic"),
            keywords=d.get("keywords", []),
        )
        for d in case["docs"]
    ]
    # 不传工厂 = 现状（关键词后端）；传了就用调用方给的召回后端，
    # 挂 BM25 作为后备 —— 与 app.py 里 Selector 的装配方式一致。
    selector = (
        Selector()
        if recall_factory is None
        else Selector(recall=recall_factory(), fallback_recall=BM25Backend())
    )
    selector.load(docs)
    rp = params.RETRIEVAL
    retriever = Retriever(
        selector,
        _StubTopics(),
        config=RetrievalConfig(
            relevance_weight=rp.relevance_weight,
            recency_weight=rp.recency_weight,
            affinity_weight=rp.affinity_weight,
            recency_half_life_days=rp.recency_half_life_days,
        ),
        conn=None,
        decay=DecayPolicy(),
    )
    ages = {d["id"]: d.get("created_days_ago", 0.0) for d in case["docs"]}
    kinds = {d["id"]: d.get("kind", "ephemeral") for d in case["docs"]}
    retriever._created_at = lambda doc_id: _iso(ages.get(doc_id, 0.0))  # type: ignore[assignment]
    retriever._preview = lambda doc_id, title: case["docs"][0]["text"][:0]  # type: ignore[assignment]
    retriever.kind_of = lambda doc_id: kinds.get(doc_id, "ephemeral")  # type: ignore[assignment]
    hits = retriever.search(case["query"], top_k=top_k)
    return [h.doc_id for h in hits]


def evaluate(
    cases: list[dict[str, Any]],
    top_k: int = 5,
    *,
    recall_factory: Callable[[], RecallBackend | None] | None = None,
) -> dict[str, Any]:
    recall1 = recall5 = 0
    rr_sum = 0.0
    wrong = 0
    stale = 0
    rows: list[dict[str, Any]] = []
    for case in cases:
        expected = set(case["expected"])
        stale_ids = set(case.get("stale", []))
        ranked = rank_case(case, top_k=top_k, recall_factory=recall_factory)
        top1 = set(ranked[:1])
        top5 = set(ranked[:top_k])
        if top1 & expected:
            recall1 += 1
        if top5 & expected:
            recall5 += 1
        rr = 0.0
        for i, doc_id in enumerate(ranked, start=1):
            if doc_id in expected:
                rr = 1.0 / i
                break
        rr_sum += rr
        if not (top1 & expected):
            wrong += 1
        # 有害情况：stale 文档排在正确文档之前（越权注入）
        first_expected = next((i for i, d in enumerate(ranked) if d in expected), None)
        stale_above = any(
            d in stale_ids and (first_expected is None or i < first_expected)
            for i, d in enumerate(ranked)
        )
        if stale_above:
            stale += 1
        rows.append(
            {
                "id": case.get("id"),
                "ranked": ranked,
                "expected": sorted(expected),
                "hit@1": bool(top1 & expected),
            }
        )
    n = len(cases) or 1
    return {
        "n": len(cases),
        "recall@1": round(recall1 / n, 4),
        "recall@5": round(recall5 / n, 4),
        "mrr": round(rr_sum / n, 4),
        "wrong_memory_injection_rate": round(wrong / n, 4),
        "stale_knowledge_injection_rate": round(stale / n, 4),
        "rows": rows,
    }
```

### `backend/src/agent/eval/topic_eval.py`

话题评测（本次改造：可注入嵌入后端）

```python
"""Topic prediction evaluator (deterministic, offline).

Input: a JSONL dataset of cases, each with topics (id/title/keywords), the
current topic, a message, and the expected mode. Thresholds come from
`agent.services.params.TopicPolicy`.

Output: switch / new-topic / in-topic metrics. No network, no paid models;
the embedding path uses a deterministic fake backend so it is reproducible.

传入 `embedding_factory` 时改用它提供的嵌入后端（例如内置 ONNX 模型），
并且**对每个用例都调用一次工厂**：后端实例会缓存话题向量，而同一个话题 id
在不同用例里会出现，复用实例会让后面的用例读到前面用例的向量。
工厂返回 `None` 时该用例退回规则路径（模型不可用时的降级行为）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agent.services.params import TOPIC, TopicPolicy


@dataclass
class _Fingerprint:
    topic_id: str
    title: str
    keywords: list[str]
    summary_preview: str = ""


class _StubTopics:
    def __init__(self, fps: list[_Fingerprint]) -> None:
        self._fps = fps

    def list_with_fingerprints(self) -> list[_Fingerprint]:
        return self._fps


class FakeEmbedding:
    """Deterministic bag-of-token embedding (no onnx, no network).

    Same input → same vector, so the onnx path can be evaluated reproducibly.
    """

    def __init__(self, dims: int = 64) -> None:
        self.dims = dims
        self._vectors: dict[str, Any] = {}

    def available(self) -> bool:
        return True

    def _vec(self, text: str):
        import hashlib

        import numpy as np

        from agent.selector.tokenize import tokenize

        v = np.zeros(self.dims, dtype=np.float32)
        for tok in tokenize(text):
            # 稳定哈希（不用内置 hash()：它受 PYTHONHASHSEED 影响、跨进程不确定）
            bucket = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16) % self.dims
            v[bucket] += 1.0
        n = float(np.linalg.norm(v)) or 1e-9
        return v / n

    def embed_texts(self, texts: list[str]):
        import numpy as np

        if not texts:
            return None
        return np.stack([self._vec(t) for t in texts])

    def topic_vector(self, topic_id: str):
        return self._vectors.get(topic_id)

    def update_topic_vector(self, topic_id: str, text: str) -> None:
        self._vectors[topic_id] = self._vec(text)


def load_cases(path: str | Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            cases.append(json.loads(line))
    return cases


def predict_mode(
    case: dict[str, Any],
    policy: TopicPolicy = TOPIC,
    *,
    embedding_factory: Callable[[], Any] | None = None,
) -> str:
    from agent.services.affinity import classify
    from agent.services.predict import TopicPredictor

    fps = [
        _Fingerprint(t["id"], t.get("title", ""), t.get("keywords", []))
        for t in case.get("topics", [])
    ]
    if embedding_factory is not None:
        embedding = embedding_factory()
    else:
        embedding = FakeEmbedding() if case.get("backend") == "fake_embedding" else None
    predictor = TopicPredictor(
        None,
        embedding,
        _StubTopics(fps),
        new_topic_threshold=policy.new_topic_threshold,
        rules_new_topic_threshold=policy.rules_new_topic_threshold,
        aux_topic_threshold=policy.aux_topic_threshold,
        rules_aux_topic_threshold=policy.rules_aux_topic_threshold,
        switch_delta=policy.switch_delta,
        aux_top_count=policy.aux_top_count,
    )
    prediction = predictor.predict(case["message"], current_topic_id=case.get("current_topic"))
    decision = classify(case["message"], prediction, case.get("current_topic"), [])
    return decision.mode.value


def evaluate(
    cases: list[dict[str, Any]],
    policy: TopicPolicy = TOPIC,
    *,
    embedding_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    counts = {"in_topic": 0, "switch": 0, "new_topic": 0}
    correct = {"in_topic": 0, "switch": 0, "new_topic": 0}
    pred_new_total = 0
    false_new = 0
    false_switch = 0
    actual_not_new = 0
    actual_not_switch = 0
    rows: list[dict[str, Any]] = []
    for case in cases:
        expected = case["expected"]
        predicted = predict_mode(case, policy, embedding_factory=embedding_factory)
        counts[expected] = counts.get(expected, 0) + 1
        if predicted == expected:
            correct[expected] = correct.get(expected, 0) + 1
        if predicted == "new_topic":
            pred_new_total += 1
            if expected != "new_topic":
                false_new += 1
        if expected != "new_topic":
            actual_not_new += 1
        if predicted == "switch" and expected != "switch":
            false_switch += 1
        if expected != "switch":
            actual_not_switch += 1
        rows.append({"id": case.get("id"), "expected": expected, "predicted": predicted})

    def rate(num: int, den: int) -> float:
        return round(num / den, 4) if den else 0.0

    return {
        "n": len(cases),
        "in_topic_accuracy": rate(correct.get("in_topic", 0), counts.get("in_topic", 0)),
        "switch_accuracy": rate(correct.get("switch", 0), counts.get("switch", 0)),
        "new_topic_precision": rate(correct.get("new_topic", 0), pred_new_total),
        "new_topic_recall": rate(correct.get("new_topic", 0), counts.get("new_topic", 0)),
        "false_new_rate": rate(false_new, actual_not_new),
        "false_switch_rate": rate(false_switch, actual_not_switch),
        "rows": rows,
    }
```

### `backend/src/agent/eval/run.py`

离线评测入口（本次改造：--embedding onnx）

```python
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
```


---

## 附录 B：测试代码


### `backend/tests/test_real_corpus.py`

测试

```python
# -*- coding: utf-8 -*-
"""真实语料层：只测离线可确定的部分。

真实模型调用不进 pytest（仓库约定：测试不得以真实 API Key 或联网为前提），
所以这里守的是筛选、去重、缓存幂等与预算守卫 —— 它们错了会让整套实验数字失真或超支。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.eval import real_corpus
from agent.eval.real_corpus import BudgetExceeded, BudgetGuard, read_jsonl, select_docs, write_jsonl


def test_select_docs_filters_by_length_and_dedups():
    rows = [
        ("标题A", "太短了"),
        ("标题B", "文" * 500),
        ("标题B", "文" * 500),
        ("标题C", "文" * 2500),
        ("标题D", "好" * 800),
    ]
    docs = select_docs(rows, n=10, min_len=400, max_len=2000)
    assert [d.title for d in docs] == ["标题B", "标题D"]


def test_select_docs_respects_limit():
    rows = [(f"T{i}", "文" * 500) for i in range(10)]
    assert len(select_docs(rows, n=3)) == 3


def test_budget_guard_stops_at_threshold_and_reports_spend():
    guard = BudgetGuard(max_usd=1.0, stop_at=0.9)
    guard.add(0.5)
    guard.add(0.3)
    assert guard.exhausted is False
    assert guard.spent == pytest.approx(0.8)
    with pytest.raises(BudgetExceeded):
        guard.add(0.2)
    assert guard.exhausted is True


def test_jsonl_roundtrip_is_stable(tmp_path: Path):
    path = tmp_path / "docs.jsonl"
    rows = [{"title": "甲", "text": "内容"}, {"title": "乙", "text": "内容2"}]
    write_jsonl(path, rows)
    assert read_jsonl(path) == rows
    write_jsonl(path, rows)
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_load_docs_uses_cache_without_network(tmp_path: Path, monkeypatch):
    cache = tmp_path / "wiki_docs.jsonl"
    write_jsonl(
        cache,
        [{"title": f"T{i}", "text": "文" * 500} for i in range(5)],
    )

    def _boom(*args, **kwargs):
        raise AssertionError("命中缓存时不应再联网")

    monkeypatch.setattr(real_corpus, "download_wiki_shard", _boom)
    docs = real_corpus.load_wiki_docs(3, cache_dir=tmp_path)
    assert len(docs) == 3
    assert docs[0].title == "T0"
    assert json.loads(json.dumps({"ok": True}))["ok"] is True
```

### `backend/tests/test_stress_corpus.py`

测试

```python
# -*- coding: utf-8 -*-
"""压力语料生成器：确定性、标注自洽、离线。

这些断言守住的是「标注可信」这件事：如果生成器给出的正确答案和它自己的构造规则
不一致（例如事实更新把旧值标成正确），后面所有实验结论都会是错的。
"""

from __future__ import annotations

from agent.eval.stress_corpus import CATEGORIES, content_overlap, generate
from agent.selector.tokenize import tokenize


def test_same_seed_gives_identical_corpus():
    a = generate(seed=7, n_memories=200, n_topics=10, n_queries=40)
    b = generate(seed=7, n_memories=200, n_topics=10, n_queries=40)
    assert a.memories == b.memories
    assert a.recall_cases == b.recall_cases


def test_every_recall_case_points_at_existing_memory():
    c = generate(seed=7, n_memories=200, n_topics=10, n_queries=40)
    known = {m.id for m in c.memories}
    for case in c.recall_cases:
        assert case.expected and set(case.expected) <= known
        assert set(case.stale) <= known


def test_fact_update_gold_is_newest_and_stale_is_older():
    c = generate(seed=7, n_memories=200, n_topics=10, n_queries=40)
    by_id = {m.id: m for m in c.memories}
    updates = [x for x in c.recall_cases if x.category == "fact_update"]
    assert updates
    for case in updates:
        gold = by_id[case.expected[0]]
        assert case.stale, "事实更新用例必须带一个旧值"
        for old in case.stale:
            assert by_id[old].topic_id == gold.topic_id
            assert by_id[old].age_days > gold.age_days


def test_same_topic_distractor_shares_topic_but_is_not_gold():
    c = generate(seed=7, n_memories=200, n_topics=10, n_queries=40)
    by_id = {m.id: m for m in c.memories}
    cases = [x for x in c.recall_cases if x.category == "same_topic"]
    assert cases
    for case in cases:
        gold = by_id[case.expected[0]]
        assert any(
            m.topic_id == gold.topic_id and m.id not in case.expected for m in c.memories
        )


def test_every_category_is_present_and_sizes_match_request():
    c = generate(seed=7, n_memories=200, n_topics=10, n_queries=60)
    assert len(c.memories) == 200
    assert len(c.recall_cases) == 60
    assert {x.category for x in c.recall_cases} == set(CATEGORIES)


def test_keyword_answerable_flag_matches_content_overlap():
    """这个标记必须由内容词重叠实算：它决定「模型在关键词答不对的子集上有多少增量」。"""
    c = generate(seed=7, n_memories=200, n_topics=10, n_queries=40)
    by_id = {m.id: m for m in c.memories}
    for case in c.recall_cases:
        overlap = any(content_overlap(case.query, by_id[m].text) for m in case.expected)
        assert case.keyword_answerable == overlap


def test_hard_categories_are_really_not_keyword_answerable():
    """同名但词不同的类别里，必须有一批真的「字面答不出」——否则模型没有增量空间。"""
    c = generate(seed=7, n_memories=1400, n_topics=20, n_queries=120)
    for category in ("cross_topic", "paraphrase", "recent_noise"):
        cases = [x for x in c.recall_cases if x.category == category]
        assert cases, f"{category} 一条用例都没有"
        disjoint = [x for x in cases if x.tier == "disjoint"]
        assert len(disjoint) >= len(cases) * 0.4, f"{category} 的零重合用例太少"
        assert all(not x.keyword_answerable for x in disjoint)


def test_mix_contains_both_easy_and_hard_queries():
    """两个子集都要有：只留简单题测不出上限，只留难题测不出真实可用性。"""
    c = generate(seed=7, n_memories=1400, n_topics=20, n_queries=120)
    hard = [x for x in c.recall_cases if not x.keyword_answerable]
    easy = [x for x in c.recall_cases if x.keyword_answerable]
    assert len(hard) >= len(c.recall_cases) * 0.3
    assert len(easy) >= len(c.recall_cases) * 0.3

    # 事实更新类必须两种问法都有：它同时承担「陈旧知识是否被取代」的检验
    updates = [x for x in c.recall_cases if x.category == "fact_update"]
    assert sum(not x.keyword_answerable for x in updates) >= len(updates) * 0.25
    assert sum(x.keyword_answerable for x in updates) >= len(updates) * 0.25


def test_difficulty_tiers_are_populated_and_consistent():
    """三档都要有，否则「从哪一档开始掉」就说不清；分层口径必须与重叠率一致。"""
    c = generate(seed=7, n_memories=1400, n_topics=20, n_queries=180)
    assert {x.tier for x in c.recall_cases} == {"literal", "partial", "disjoint"}
    for case in c.recall_cases:
        assert case.keyword_answerable == (case.overlap > 0)
        if case.tier == "literal":
            assert case.overlap >= 0.4
        elif case.tier == "partial":
            assert 0 < case.overlap < 0.4
        else:
            assert case.overlap == 0
```

### `backend/tests/test_stress_metrics.py`

测试

```python
# -*- coding: utf-8 -*-
"""指标计算属于纯函数，数字必须能手工核对。

这些断言是全部实验数字的地基：指标算错的话，三条臂的对照结论就没有意义。
"""

from __future__ import annotations

from agent.eval.stress_corpus import RecallCase
from agent.eval.stress_metrics import recall_metrics, stratify_recall


def _case(cid: str, expected: tuple, stale: tuple = (), hard: bool = False):
    return RecallCase(
        id=cid,
        category="fact_update" if stale else "same_topic",
        query="q",
        expected=expected,
        stale=stale,
        keyword_answerable=not hard,
    )


def test_recall_metrics_counts_first_and_top5_hits():
    cases = [_case("a", ("g1",)), _case("b", ("g2",)), _case("c", ("g3",))]
    ranked = [["g1", "x"], ["x", "g2"], ["x", "y"]]
    m = recall_metrics(ranked, cases, k=2)
    assert m["recall@1"] == 0.3333
    assert m["recall@2"] == 0.6667
    assert m["mrr"] == 0.5
    assert m["wrong_memory_injection_rate"] == 0.3333


def test_stale_knowledge_rate_counts_stale_above_gold():
    cases = [
        _case("a", ("new",), stale=("old",)),
        _case("b", ("new2",), stale=("old2",)),
    ]
    ranked = [["old", "new"], ["new2", "old2"]]
    m = recall_metrics(ranked, cases, k=2)
    assert m["stale_knowledge_injection_rate"] == 0.5


def test_stratify_recall_splits_hard_and_easy_subset():
    cases = [
        _case("a", ("g1",)),
        _case("b", ("g2",), hard=True),
        _case("c", ("g3",), hard=True),
    ]
    ranked = [["g1"], ["x"], ["g3"]]
    out = stratify_recall(ranked, cases, k=1)
    assert out["all"]["recall@1"] == 0.6667
    assert out["keyword_answerable"]["recall@1"] == 1.0
    assert out["keyword_unanswerable"]["recall@1"] == 0.5
```

### `backend/tests/test_stress_arms.py`

测试

```python
# -*- coding: utf-8 -*-
"""三条臂的接线：走的是生产入口，不是另写一套算法。

规则臂的用例完全离线；本地臂的用例要求本机有模型文件，没有就跳过。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.eval.embedding_backend import resolve_model_dir
from agent.eval.stress_arms import LocalEmbeddingArm, RulesArm
from agent.eval.stress_corpus import generate

HAS_MODEL = (resolve_model_dir() / "model.onnx").exists()


@pytest.fixture(scope="module")
def corpus():
    return generate(seed=11, n_memories=1400, n_topics=20, n_queries=24)


def test_rules_arm_recall_returns_one_ranking_per_case(corpus):
    arm = RulesArm(corpus)
    ranked, latencies = arm.recall(corpus.recall_cases, k=5)
    assert len(ranked) == len(corpus.recall_cases)
    assert len(latencies) == len(ranked)
    known = {m.id for m in corpus.memories}
    for ids in ranked:
        assert isinstance(ids, list) and set(ids) <= known
    literal = [
        (ids, case)
        for ids, case in zip(ranked, corpus.recall_cases)
        if case.keyword_answerable and case.category in ("fact_update", "same_topic")
    ]
    hits = sum(1 for ids, case in literal if set(ids) & set(case.expected))
    assert hits >= len(literal) * 0.8, "字面可答的用例，规则臂应当基本都能命中"


def test_rules_arm_topic_rows_have_valid_modes(corpus):
    rows, latencies = RulesArm(corpus).topic(corpus.topic_cases)
    assert len(rows) == len(corpus.topic_cases)
    assert all(r["predicted"] in ("in_topic", "switch", "new_topic") for r in rows)
    assert all(r["backend"] == "rules" for r in rows)


def test_rules_arm_entity_matches_name_and_alias(corpus):
    rows, _ = RulesArm(corpus).entity(corpus.entity_cases)
    hits = sum(1 for r in rows if r["predicted"] == r["expected"])
    assert hits == len(rows), "查询里直接写了实体名，规则臂应当全中"


def test_rules_arm_tool_puts_expected_tool_in_candidates(corpus):
    rows, _ = RulesArm(corpus).tool(corpus.tool_cases)
    hits = sum(1 for r in rows if r["expected"] in r["ranked"][:5])
    assert hits >= len(rows) * 0.8, "查询就是工具描述原文，至少应当进候选"


def test_rules_arm_dedup_flags_exact_name_and_ignores_novel(corpus):
    rows, _ = RulesArm(corpus).dedup(corpus.dedup_cases)
    by_expect = {True: [], False: []}
    for r in rows:
        by_expect[r["expected"]].append(r["predicted"])
    assert all(by_expect[True]), "同名新话题必须被拦下"
    assert not any(by_expect[False]), "全新话题不能误拦"


@pytest.mark.skipif(not HAS_MODEL, reason="本机没有内置模型文件")
def test_local_arm_uses_onnx_path(corpus):
    """只断言接线：本地臂必须真的走向量路径，并且返回结构完整的结果。

    「向量在语义题上能拿多少分」是实验要测的量，不是测试该断言的假设 ——
    把它写成 >= 某个比例，等于用测试替实验下结论。
    """
    arm = LocalEmbeddingArm(corpus)
    ranked, _ = arm.recall(corpus.recall_cases, k=5)
    assert len(ranked) == len(corpus.recall_cases)
    rows, _ = arm.topic(corpus.topic_cases)
    assert all(r["backend"] == "onnx" for r in rows)
    known = {m.id for m in corpus.memories}
    for ids in ranked:
        assert set(ids) <= known
    entity_rows, _ = arm.entity(corpus.entity_cases)
    assert len(entity_rows) == len(corpus.entity_cases)
    dedup_rows, _ = arm.dedup(corpus.dedup_cases)
    assert len(dedup_rows) == len(corpus.dedup_cases)
```

### `backend/tests/test_stress_jev_arm.py`

测试

```python
# -*- coding: utf-8 -*-
"""Jev 臂的接线：用假客户端验证「答案 → 判定」的映射，不发真实请求。"""

from __future__ import annotations

import pytest

from agent.eval.jev_client import JevAnswer
from agent.eval.stress_corpus import (
    DedupCase,
    EntityCard,
    EntityCase,
    StressCorpus,
    Topic,
    TopicCase,
    ToolSpecLite,
)
from agent.eval.stress_jev_arm import JevArm


class _FakeClient:
    def __init__(self, answers: dict) -> None:
        self.answers = answers
        self.calls = 0

    def ask(self, state, questions):
        self.calls += 1
        payload = self.answers
        if isinstance(payload, list):
            payload = payload[min(self.calls - 1, len(payload) - 1)]
        return JevAnswer(answers=payload, latency_ms=12.0)

    def summary(self):
        return {"calls": self.calls}


def _corpus() -> StressCorpus:
    return StressCorpus(
        seed=1,
        topics=[
            Topic(id="t_a", title="数据库迁移", keywords=("数据库",)),
            Topic(id="t_b", title="界面配色", keywords=("界面",)),
        ],
        entities=[EntityCard(id="e_1", name="小林", aliases=(), summary="负责后端")],
        tools=[ToolSpecLite(name="memory_search", description="检索历史记忆")],
    )


def test_recall_is_not_supported():
    arm = JevArm(_FakeClient({})).attach(_corpus())
    with pytest.raises(NotImplementedError):
        arm.recall([], k=5)


def test_topic_maps_choice_to_mode():
    client = _FakeClient(
        [
            {"topic": {"type": "choice", "choice": "t_a"}},
            {"topic": {"type": "choice", "choice": "t_b"}},
        ]
    )
    arm = JevArm(client).attach(_corpus())
    cases = [
        TopicCase(id="1", category="in_topic", message="继续", current_topic_id="t_a",
                  expected_topic_id="t_a", expected_mode="in_topic"),
        TopicCase(id="2", category="switch", message="换", current_topic_id="t_a",
                  expected_topic_id="t_b", expected_mode="switch"),
    ]
    rows, latencies = arm.topic(cases)
    assert [r["predicted"] for r in rows] == ["in_topic", "switch"]
    assert latencies == [12.0, 12.0]


def test_topic_maps_new_marker_to_new_topic():
    arm = JevArm(_FakeClient({"topic": {"type": "choice", "choice": "__new__"}})).attach(_corpus())
    case = TopicCase(id="3", category="new_topic", message="聊点别的",
                     current_topic_id="t_a", expected_topic_id=None, expected_mode="new_topic")
    rows, _ = arm.topic([case])
    assert rows[0]["predicted"] == "new_topic"
    assert rows[0]["predicted_topic"] is None


def test_entity_none_marker_becomes_null():
    arm = JevArm(_FakeClient({"card": {"type": "choice", "choice": "__none__"}})).attach(_corpus())
    rows, _ = arm.entity([EntityCase(id="1", query="谁负责后端", expected_card_id="e_1")])
    assert rows[0]["predicted"] is None


def test_dedup_thresholds_noul():
    for value, expected in ((0.9, True), (0.1, False)):
        client = _FakeClient({"duplicate": {"type": "noul", "noul": value}})
        arm = JevArm(client).attach(_corpus())
        case = DedupCase(
            id="1", candidate_name="数据库迁移", expected_duplicate=expected, duplicate_of="t_a"
        )
        rows, _ = arm.dedup([case])
        assert rows[0]["predicted"] is expected
```

### `backend/tests/test_stress_rerank.py`

测试

```python
# -*- coding: utf-8 -*-
"""重排三件套：原序、本地向量、Jev。全部离线，用假后端与假客户端。"""

from __future__ import annotations

import numpy as np

from agent.eval.jev_client import JevAnswer
from agent.eval.stress_rerank import (
    Candidate,
    ConditionalRerank,
    IdentityRerank,
    JevRerank,
    LocalVectorRerank,
)


class _FakeBackend:
    """查询与"含对字"的候选都是 [1,0]，其余是 [0,1]：于是正确候选更接近查询。"""

    def embed_texts(self, texts):
        rows = []
        for t in texts:
            rows.append([1.0, 0.0] if ("对" in t or t == "query") else [0.0, 1.0])
        return np.array(rows, dtype=np.float32)


class _FakeClient:
    def __init__(self, probs: dict) -> None:
        self.probs = probs
        self.calls = 0

    def ask(self, state, questions):
        self.calls += 1
        return JevAnswer(
            answers={k: {"type": "noul", "noul": v} for k, v in self.probs.items()},
            latency_ms=7.0,
        )


def _candidates():
    return [
        Candidate(doc_id="wrong", score=0.9, text="差不多的问题但不是它"),
        Candidate(doc_id="right", score=0.8, text="这条才对"),
    ]


def test_identity_keeps_order():
    assert IdentityRerank().rerank("q", _candidates()) == ["wrong", "right"]


def test_local_vector_rerank_promotes_semantically_closer_candidate():
    ranked = LocalVectorRerank(_FakeBackend()).rerank("query", _candidates())
    assert ranked == ["right", "wrong"]


def test_jev_rerank_sorts_by_noul_and_keeps_tail():
    client = _FakeClient({"c0": 0.1, "c1": 0.9})
    reranker = JevRerank(client)
    ranked = reranker.rerank("query", _candidates())
    assert ranked == ["right", "wrong"]
    assert client.calls == 1
    assert reranker.latencies_ms == [7.0]


def test_jev_rerank_degrades_to_input_order_on_failure():
    class _Boom:
        def ask(self, state, questions):
            raise RuntimeError("boom")

    reranker = JevRerank(_Boom())
    assert reranker.rerank("query", _candidates()) == ["wrong", "right"]
    assert reranker.failures == 1


def test_conditional_rerank_skips_when_top_is_clear():
    calls = []

    class _Spy:
        def rerank(self, query, candidates):
            calls.append(query)
            return [c.doc_id for c in reversed(candidates)]

    reranker = ConditionalRerank(_Spy(), relative_gap=0.2)
    clear = [Candidate("a", 1.0, ""), Candidate("b", 0.5, "")]
    fuzzy = [Candidate("a", 1.0, ""), Candidate("b", 0.98, "")]
    assert reranker.rerank("q1", clear) == ["a", "b"]
    assert reranker.rerank("q2", fuzzy) == ["b", "a"]
    assert calls == ["q2"]
    assert (reranker.triggered, reranker.skipped) == (1, 1)
```

### `backend/tests/test_stress_run.py`

测试

```python
# -*- coding: utf-8 -*-
"""跑批器：结构、分层、以及"缺条件就报错而不是悄悄降级"。"""

from __future__ import annotations

import json

from agent.eval.stress_corpus import generate
from agent.eval.stress_run import run_all
from agent.eval.stress_arms import RulesArm


def _small():
    return generate(seed=5, n_memories=700, n_topics=10, n_queries=30)


def test_run_all_reports_every_job_for_every_arm():
    corpus = _small()
    payload = run_all(corpus, [RulesArm(corpus)], k=5)
    assert set(payload) == {"rules"}
    arm = payload["rules"]
    assert set(arm) >= {"recall", "topic", "entity", "tool", "dedup", "wall_ms"}
    assert 0.0 <= arm["recall"]["all"]["recall@1"] <= 1.0
    assert arm["recall"]["by_tier"].keys() == {"literal", "partial", "disjoint"}
    assert arm["topic"]["accuracy"] >= 0.0
    assert "confusions" in arm["dedup"]
    assert arm["recall"]["latency_ms"]["p50_ms"] >= 0.0


def test_run_all_is_json_serialisable():
    corpus = _small()
    payload = run_all(corpus, [RulesArm(corpus)], k=3)
    text = json.dumps(payload, ensure_ascii=False)
    assert "recall@1" in text
```

### `backend/tests/test_jev_client.py`

测试

```python
# -*- coding: utf-8 -*-
"""Jev 客户端：请求形状、解析、错误处理。

全部用假的 httpx transport —— 仓库约定是测试不得以真实 API Key 或联网为前提。
密钥泄露那条断言尤其重要：错误信息里出现 key 原文就等于把它写进了日志。
"""

from __future__ import annotations

import json

import httpx
import pytest

from agent.eval import jev_client
from agent.eval.jev_client import JevClient, JevUnavailable


def _client(handler, **kwargs) -> JevClient:
    return JevClient(
        api_key=kwargs.pop("api_key", "sk-or-v1-TESTKEY"),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


def test_ask_posts_state_and_questions_then_parses_answers():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "answers": {"x": {"type": "noul", "noul": 0.9}},
                "usage": {"input_tokens": 12, "output_tokens": 3, "cost": 0.0000005},
            },
        )

    answer = _client(handler).ask({"message": "hi"}, {"x": {"type": "noul", "instructions": "?"}})
    assert captured["url"].endswith("/api/alpha/decisions")
    assert captured["body"]["model"] == "typesafe/jev-1.13"
    assert captured["body"]["state"] == {"message": "hi"}
    assert captured["auth"] == "Bearer sk-or-v1-TESTKEY"
    assert answer.answers["x"]["noul"] == 0.9
    assert answer.input_tokens == 12
    assert answer.cost_usd == pytest.approx(0.0000005)


def test_totals_accumulate_across_calls():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"answers": {}, "usage": {"input_tokens": 10, "cost": 0.000001}}
        )

    client = _client(handler)
    client.ask({"m": 1}, {})
    client.ask({"m": 2}, {})
    assert client.total_input_tokens == 20
    assert client.total_cost_usd == pytest.approx(0.000002)
    assert client.requests == 2


def test_missing_key_raises_jev_unavailable(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(jev_client, "_registry_key", lambda: "")
    with pytest.raises(JevUnavailable):
        JevClient().ask({"m": 1}, {})


def test_http_error_is_raised_without_leaking_the_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key sk-or-v1-SECRET"}})

    client = _client(handler, api_key="sk-or-v1-SECRET")
    with pytest.raises(JevUnavailable) as exc:
        client.ask({"m": 1}, {})
    assert "SECRET" not in str(exc.value)
    assert "sk-or" not in str(exc.value)
```

### `backend/tests/test_eval_embedding_backend.py`

测试

```python
# -*- coding: utf-8 -*-
"""评测接真实嵌入模型：默认路径不变，只有显式开开关才走模型。

这些用例都不联网，也不要求本机有模型文件（唯一需要真实模型的那条用
skipif 跳过），所以没有模型的机器和 CI 依然能全绿。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from agent.eval import embedding_backend
from agent.eval.retrieval_eval import load_cases as load_retrieval_cases
from agent.eval.retrieval_eval import rank_case
from agent.eval.topic_eval import evaluate as eval_topic
from agent.selector.base import RecallBackend, ScoredDoc

EVALS = Path(__file__).resolve().parents[1] / "evals"
RETRIEVAL_CASES = {
    c["id"]: c for c in load_retrieval_cases(EVALS / "retrieval" / "cases.jsonl")
}


class _FixedRecall(RecallBackend):
    """词表无关的召回后端：永远只返回一条指定文档。"""

    name = "fixed"

    def __init__(self, doc_id: str) -> None:
        self._doc_id = doc_id

    def available(self) -> bool:
        return True

    def index(self, docs) -> None:
        self._indexed = list(docs)

    def search(self, query: str, top_k: int) -> list[ScoredDoc]:
        return [ScoredDoc(doc_id=self._doc_id, score=1.0, source=self.name)]


class _CachingEmbedding:
    """确定性假后端，行为对齐 OnnxEmbeddingBackend：话题向量算出后就留在实例里。

    正是这个缓存让「每个用例一个新实例」成为必须：跨用例复用实例时，
    第二个用例会拿到上一个用例为同名话题算出的向量。
    """

    def __init__(self) -> None:
        self._topic_vectors: dict[str, np.ndarray] = {}

    @staticmethod
    def _vec(text: str) -> np.ndarray:
        if "阿尔法" in text:
            return np.array([1.0, 0.0], dtype=np.float32)
        return np.array([0.0, 1.0], dtype=np.float32)

    def available(self) -> bool:
        return True

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        return np.stack([self._vec(t) for t in texts])

    def topic_vector(self, topic_id: str) -> np.ndarray | None:
        return self._topic_vectors.get(topic_id)

    def update_topic_vector(self, topic_id: str, text: str) -> None:
        self._topic_vectors[topic_id] = self._vec(text)


def _topic_case(
    case_id: str, title: str, message: str, current: str, expected: str
) -> dict:
    return {
        "id": case_id,
        "message": message,
        "current_topic": current,
        "topics": [{"id": "t_x", "title": title, "keywords": []}],
        "expected": expected,
    }


def test_retrieval_eval_uses_injected_recall_backend():
    """注入召回后端后，排序结果必须由它决定，而不是默认的关键词后端。"""
    case = RETRIEVAL_CASES["fact_update_db"]
    assert rank_case(case)[0] == "sqlite_decision"

    ranked = rank_case(case, recall_factory=lambda: _FixedRecall("chatter_1"))
    assert ranked[0] == "chatter_1"


def test_topic_eval_builds_a_fresh_embedding_per_case():
    """同一个话题 id 出现在两个用例里时，第二个用例不能被第一个用例的向量污染。"""
    cases = [
        _topic_case("case_a", "阿尔法项目", "阿尔法 进展", "t_x", "in_topic"),
        _topic_case("case_b", "贝塔项目", "阿尔法 进展", "t_x", "new_topic"),
    ]
    metrics = eval_topic(cases, embedding_factory=_CachingEmbedding)
    assert metrics["rows"] == [
        {"id": "case_a", "expected": "in_topic", "predicted": "in_topic"},
        {"id": "case_b", "expected": "new_topic", "predicted": "new_topic"},
    ]


def test_build_backend_reports_missing_model(tmp_path: Path):
    backend, note = embedding_backend.build_backend(tmp_path)
    assert backend is None
    assert str(tmp_path) in note


LOCAL_MODEL_DIR = embedding_backend.resolve_model_dir()
HAS_LOCAL_MODEL = (LOCAL_MODEL_DIR / "model.onnx").exists() and (
    LOCAL_MODEL_DIR / "tokenizer.json"
).exists()


@pytest.mark.skipif(not HAS_LOCAL_MODEL, reason="本机没有内置模型文件")
def test_build_backend_loads_local_model():
    backend, note = embedding_backend.build_backend()
    assert backend is not None, note
    assert backend.available()
    assert backend.dims == 512
    vectors = backend.embed_texts(["数据库 迁移"])
    assert vectors is not None
    assert vectors.shape == (1, 512)


def test_run_cli_refuses_to_overwrite_baseline_with_model_run():
    from agent.eval.run import main

    with pytest.raises(SystemExit):
        main(["--embedding", "onnx", "--baseline"])


def test_run_cli_default_payload_shape_is_unchanged(capsys):
    """默认路径的产物形状必须与 baseline.json 一致（不加额外标记字段）。"""
    from agent.eval.run import main

    assert main([]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"topic_prediction", "retrieval", "anchor_continuation"}
```


---

## 附录 C：原始结果 JSON


### `backend/evals/stress_real_arms_200.json`

```json
{
  "rules": {
    "recall": {
      "all": {
        "n": 600,
        "recall@1": 0.5933,
        "recall@5": 0.6067,
        "mrr": 0.5993,
        "wrong_memory_injection_rate": 0.3933,
        "stale_knowledge_injection_rate": 0.0
      },
      "keyword_answerable": {
        "n": 364,
        "recall@1": 0.978,
        "recall@5": 1.0,
        "mrr": 0.9879,
        "wrong_memory_injection_rate": 0.0,
        "stale_knowledge_injection_rate": 0.0
      },
      "keyword_unanswerable": {
        "n": 236,
        "recall@1": 0.0,
        "recall@5": 0.0,
        "mrr": 0.0,
        "wrong_memory_injection_rate": 1.0,
        "stale_knowledge_injection_rate": 0.0
      },
      "by_category": {
        "cross_topic": {
          "n": 100,
          "recall@1": 0.94,
          "recall@5": 0.94,
          "mrr": 0.94,
          "wrong_memory_injection_rate": 0.06,
          "stale_knowledge_injection_rate": 0.0
        },
        "entity_ref": {
          "n": 100,
          "recall@1": 0.01,
          "recall@5": 0.03,
          "mrr": 0.02,
          "wrong_memory_injection_rate": 0.97,
          "stale_knowledge_injection_rate": 0.0
        },
        "fact_update": {
          "n": 100,
          "recall@1": 0.96,
          "recall@5": 0.96,
          "mrr": 0.96,
          "wrong_memory_injection_rate": 0.04,
          "stale_knowledge_injection_rate": 0.0
        },
        "paraphrase": {
          "n": 100,
          "recall@1": 0.81,
          "recall@5": 0.83,
          "mrr": 0.8175,
          "wrong_memory_injection_rate": 0.17,
          "stale_knowledge_injection_rate": 0.0
        },
        "recent_noise": {
          "n": 100,
          "recall@1": 0.02,
          "recall@5": 0.06,
          "mrr": 0.0383,
          "wrong_memory_injection_rate": 0.94,
          "stale_knowledge_injection_rate": 0.0
        },
        "same_topic": {
          "n": 100,
          "recall@1": 0.82,
          "recall@5": 0.82,
          "mrr": 0.82,
          "wrong_memory_injection_rate": 0.18,
          "stale_knowledge_injection_rate": 0.0
        }
      },
      "by_tier": {
        "literal": {
          "n": 111,
          "recall@1": 1.0,
          "recall@5": 1.0,
          "mrr": 1.0,
          "wrong_memory_injection_rate": 0.0,
          "stale_knowledge_injection_rate": 0.0
        },
        "partial": {
          "n": 253,
          "recall@1": 0.9684,
          "recall@5": 1.0,
          "mrr": 0.9825,
          "wrong_memory_injection_rate": 0.0,
          "stale_knowledge_injection_rate": 0.0
        },
        "disjoint": {
          "n": 236,
          "recall@1": 0.0,
          "recall@5": 0.0,
          "mrr": 0.0,
          "wrong_memory_injection_rate": 1.0,
          "stale_knowledge_injection_rate": 0.0
        }
      },
      "latency_ms": {
        "n": 600,
        "mean_ms": 0.23,
        "p50_ms": 0.21,
        "p90_ms": 0.35,
        "max_ms": 0.65
      }
    },
    "topic": {
      "n": 180,
      "accuracy": 0.3333,
      "confusions": {
        "in_topic→new_topic": 60,
        "switch→new_topic": 60,
        "new_topic→new_topic": 60
      },
      "latency_ms": {
        "n": 180,
        "mean_ms": 0.14,
        "p50_ms": 0.12,
        "p90_ms": 0.17,
        "max_ms": 0.29
      }
    },
    "entity": {
      "n": 0,
      "accuracy": 0.0,
      "confusions": {},
      "latency_ms": {
        "n": 0
      }
    },
    "tool": {
      "n": 0,
      "latency_ms": {
        "n": 0
      }
    },
    "dedup": {
      "n": 120,
      "accuracy": 0.5,
      "recall": 0.0,
      "false_positive_rate": 0.0,
      "confusions": {
        "tp": 0,
        "fp": 0,
        "tn": 60,
        "fn": 60
      },
      "latency_ms": {
        "n": 120,
        "mean_ms": 0.18,
        "p50_ms": 0.17,
        "p90_ms": 0.22,
        "max_ms": 0.54
      }
    },
    "wall_ms": 187.5
  },
  "local": {
    "recall": {
      "all": {
        "n": 600,
        "recall@1": 0.75,
        "recall@5": 0.8233,
        "mrr": 0.7758,
        "wrong_memory_injection_rate": 0.1767,
        "stale_knowledge_injection_rate": 0.0
      },
      "keyword_answerable": {
        "n": 364,
        "recall@1": 0.9835,
        "recall@5": 1.0,
        "mrr": 0.9892,
        "wrong_memory_injection_rate": 0.0,
        "stale_knowledge_injection_rate": 0.0
      },
      "keyword_unanswerable": {
        "n": 236,
        "recall@1": 0.3898,
        "recall@5": 0.5508,
        "mrr": 0.4466,
        "wrong_memory_injection_rate": 0.4492,
        "stale_knowledge_injection_rate": 0.0
      },
      "by_category": {
        "cross_topic": {
          "n": 100,
          "recall@1": 0.99,
          "recall@5": 1.0,
          "mrr": 0.9933,
          "wrong_memory_injection_rate": 0.0,
          "stale_knowledge_injection_rate": 0.0
        },
        "entity_ref": {
          "n": 100,
          "recall@1": 0.27,
          "recall@5": 0.51,
          "mrr": 0.3487,
          "wrong_memory_injection_rate": 0.49,
          "stale_knowledge_injection_rate": 0.0
        },
        "fact_update": {
          "n": 100,
          "recall@1": 1.0,
          "recall@5": 1.0,
          "mrr": 1.0,
          "wrong_memory_injection_rate": 0.0,
          "stale_knowledge_injection_rate": 0.0
        },
        "paraphrase": {
          "n": 100,
          "recall@1": 0.97,
          "recall@5": 0.99,
          "mrr": 0.9775,
          "wrong_memory_injection_rate": 0.01,
          "stale_knowledge_injection_rate": 0.0
        },
        "recent_noise": {
          "n": 100,
          "recall@1": 0.33,
          "recall@5": 0.46,
          "mrr": 0.3778,
          "wrong_memory_injection_rate": 0.54,
          "stale_knowledge_injection_rate": 0.0
        },
        "same_topic": {
          "n": 100,
          "recall@1": 0.94,
          "recall@5": 0.98,
          "mrr": 0.9575,
          "wrong_memory_injection_rate": 0.02,
          "stale_knowledge_injection_rate": 0.0
        }
      },
      "by_tier": {
        "literal": {
          "n": 111,
          "recall@1": 1.0,
          "recall@5": 1.0,
          "mrr": 1.0,
          "wrong_memory_injection_rate": 0.0,
          "stale_knowledge_injection_rate": 0.0
        },
        "partial": {
          "n": 253,
          "recall@1": 0.9763,
          "recall@5": 1.0,
          "mrr": 0.9845,
          "wrong_memory_injection_rate": 0.0,
          "stale_knowledge_injection_rate": 0.0
        },
        "disjoint": {
          "n": 236,
          "recall@1": 0.3898,
          "recall@5": 0.5508,
          "mrr": 0.4466,
          "wrong_memory_injection_rate": 0.4492,
          "stale_knowledge_injection_rate": 0.0
        }
      },
      "latency_ms": {
        "n": 600,
        "mean_ms": 2.39,
        "p50_ms": 2.32,
        "p90_ms": 3.02,
        "max_ms": 4.47
      }
    },
    "topic": {
      "n": 180,
      "accuracy": 0.3333,
      "confusions": {
        "in_topic→new_topic": 60,
        "switch→new_topic": 60,
        "new_topic→new_topic": 60
      },
      "latency_ms": {
        "n": 180,
        "mean_ms": 7.07,
        "p50_ms": 7.09,
        "p90_ms": 7.92,
        "max_ms": 9.74
      }
    },
    "entity": {
      "n": 0,
      "accuracy": 0.0,
      "confusions": {},
      "latency_ms": {
        "n": 0
      }
    },
    "tool": {
      "n": 0,
      "latency_ms": {
        "n": 0
      }
    },
    "dedup": {
      "n": 120,
      "accuracy": 0.4917,
      "recall": 0.6333,
      "false_positive_rate": 0.65,
      "confusions": {
        "tp": 38,
        "fp": 39,
        "tn": 21,
        "fn": 22
      },
      "latency_ms": {
        "n": 120,
        "mean_ms": 5.68,
        "p50_ms": 5.62,
        "p90_ms": 6.48,
        "max_ms": 7.13
      }
    },
    "backend": "onnx:bge-small-zh-v1.5:fp32:69a0b846f4f1",
    "wall_ms": 4138.3
  },
  "_corpus": {
    "memories": 200,
    "topics": 200,
    "entities": 0,
    "recall_cases": 600,
    "questions_cached": 200,
    "wall_s": 4.3,
    "budget": {
      "calls": 0,
      "spent_usd": 0.0,
      "max_usd": 1.0,
      "exhausted": false
    },
    "data_dir": "C:\\Users\\zxy\\AppData\\Local\\Temp\\qio-stress-cache\\qio-real-200b"
  }
}
```

### `backend/evals/stress_real_200.json`

```json
{
  "docs": 200,
  "memories": 200,
  "topics": 200,
  "entities": 0,
  "recall_cases": 600,
  "questions": 200,
  "failures": [],
  "adapter": {
    "model": "deepseek-v4-flash"
  },
  "budget": {
    "calls": 0,
    "spent_usd": 0.0,
    "max_usd": 1.0,
    "exhausted": false
  },
  "wall_s": 622.6
}
```

### `backend/evals/stress_rules_vs_local.json`

```json
{
  "rules": {
    "recall": {
      "all": {
        "n": 1000,
        "recall@1": 0.15,
        "recall@5": 0.414,
        "mrr": 0.253,
        "wrong_memory_injection_rate": 0.586,
        "stale_knowledge_injection_rate": 0.0
      },
      "keyword_answerable": {
        "n": 627,
        "recall@1": 0.2392,
        "recall@5": 0.6603,
        "mrr": 0.4035,
        "wrong_memory_injection_rate": 0.3397,
        "stale_knowledge_injection_rate": 0.0
      },
      "keyword_unanswerable": {
        "n": 373,
        "recall@1": 0.0,
        "recall@5": 0.0,
        "mrr": 0.0,
        "wrong_memory_injection_rate": 1.0,
        "stale_knowledge_injection_rate": 0.0
      },
      "by_category": {
        "cross_topic": {
          "n": 167,
          "recall@1": 0.0599,
          "recall@5": 0.1257,
          "mrr": 0.084,
          "wrong_memory_injection_rate": 0.8743,
          "stale_knowledge_injection_rate": 0.0
        },
        "entity_ref": {
          "n": 167,
          "recall@1": 0.1437,
          "recall@5": 0.7126,
          "mrr": 0.3182,
          "wrong_memory_injection_rate": 0.2874,
          "stale_knowledge_injection_rate": 0.0
        },
        "fact_update": {
          "n": 167,
          "recall@1": 0.497,
          "recall@5": 0.503,
          "mrr": 0.5,
          "wrong_memory_injection_rate": 0.497,
          "stale_knowledge_injection_rate": 0.0
        },
        "paraphrase": {
          "n": 166,
          "recall@1": 0.0663,
          "recall@5": 0.1386,
          "mrr": 0.0954,
          "wrong_memory_injection_rate": 0.8614,
          "stale_knowledge_injection_rate": 0.0
        },
        "recent_noise": {
          "n": 166,
          "recall@1": 0.0,
          "recall@5": 0.0,
          "mrr": 0.0,
          "wrong_memory_injection_rate": 1.0,
          "stale_knowledge_injection_rate": 0.0
        },
        "same_topic": {
          "n": 167,
          "recall@1": 0.1317,
          "recall@5": 1.0,
          "mrr": 0.5181,
          "wrong_memory_injection_rate": 0.0,
          "stale_knowledge_injection_rate": 0.0
        }
      },
      "by_tier": {
        "literal": {
          "n": 259,
          "recall@1": 0.4093,
          "recall@5": 0.973,
          "mrr": 0.6603,
          "wrong_memory_injection_rate": 0.027,
          "stale_knowledge_injection_rate": 0.0
        },
        "partial": {
          "n": 368,
          "recall@1": 0.1196,
          "recall@5": 0.4402,
          "mrr": 0.2228,
          "wrong_memory_injection_rate": 0.5598,
          "stale_knowledge_injection_rate": 0.0
        },
        "disjoint": {
          "n": 373,
          "recall@1": 0.0,
          "recall@5": 0.0,
          "mrr": 0.0,
          "wrong_memory_injection_rate": 1.0,
          "stale_knowledge_injection_rate": 0.0
        }
      },
      "latency_ms": {
        "n": 1000,
        "mean_ms": 29.63,
        "p50_ms": 29.28,
        "p90_ms": 37.91,
        "max_ms": 70.29
      }
    },
    "topic": {
      "n": 1000,
      "accuracy": 0.37,
      "confusions": {
        "in_topic→in_topic": 37,
        "switch→new_topic": 333,
        "new_topic→new_topic": 333,
        "in_topic→new_topic": 297
      },
      "latency_ms": {
        "n": 1000,
        "mean_ms": 0.13,
        "p50_ms": 0.1,
        "p90_ms": 0.18,
        "max_ms": 1.83
      }
    },
    "entity": {
      "n": 1000,
      "accuracy": 1.0,
      "confusions": {
        "e_0000→e_0000": 5,
        "e_0001→e_0001": 5,
        "e_0002→e_0002": 5,
        "e_0003→e_0003": 5,
        "e_0004→e_0004": 5,
        "e_0005→e_0005": 5,
        "e_0006→e_0006": 5,
        "e_0007→e_0007": 5,
        "e_0008→e_0008": 5,
        "e_0009→e_0009": 5,
        "e_0010→e_0010": 5,
        "e_0011→e_0011": 5,
        "e_0012→e_0012": 5,
        "e_0013→e_0013": 5,
        "e_0014→e_0014": 5,
        "e_0015→e_0015": 5,
        "e_0016→e_0016": 5,
        "e_0017→e_0017": 5,
        "e_0018→e_0018": 5,
        "e_0019→e_0019": 5,
        "e_0020→e_0020": 5,
        "e_0021→e_0021": 5,
        "e_0022→e_0022": 5,
        "e_0023→e_0023": 5,
        "e_0024→e_0024": 5,
        "e_0025→e_0025": 5,
        "e_0026→e_0026": 5,
        "e_0027→e_0027": 5,
        "e_0028→e_0028": 5,
        "e_0029→e_0029": 5,
        "e_0030→e_0030": 5,
        "e_0031→e_0031": 5,
        "e_0032→e_0032": 5,
        "e_0033→e_0033": 5,
        "e_0034→e_0034": 5,
        "e_0035→e_0035": 5,
        "e_0036→e_0036": 5,
        "e_0037→e_0037": 5,
        "e_0038→e_0038": 5,
        "e_0039→e_0039": 5,
        "e_0040→e_0040": 5,
        "e_0041→e_0041": 5,
        "e_0042→e_0042": 5,
        "e_0043→e_0043": 5,
        "e_0044→e_0044": 5,
        "e_0045→e_0045": 5,
        "e_0046→e_0046": 5,
        "e_0047→e_0047": 5,
        "e_0048→e_0048": 5,
        "e_0049→e_0049": 5,
        "e_0050→e_0050": 5,
        "e_0051→e_0051": 5,
        "e_0052→e_0052": 5,
        "e_0053→e_0053": 5,
        "e_0054→e_0054": 5,
        "e_0055→e_0055": 5,
        "e_0056→e_0056": 5,
        "e_0057→e_0057": 5,
        "e_0058→e_0058": 5,
        "e_0059→e_0059": 5,
        "e_0060→e_0060": 5,
        "e_0061→e_0061": 5,
        "e_0062→e_0062": 5,
        "e_0063→e_0063": 5,
        "e_0064→e_0064": 5,
        "e_0065→e_0065": 5,
        "e_0066→e_0066": 5,
        "e_0067→e_0067": 5,
        "e_0068→e_0068": 5,
        "e_0069→e_0069": 5,
        "e_0070→e_0070": 5,
        "e_0071→e_0071": 5,
        "e_0072→e_0072": 5,
        "e_0073→e_0073": 5,
        "e_0074→e_0074": 5,
        "e_0075→e_0075": 5,
        "e_0076→e_0076": 5,
        "e_0077→e_0077": 5,
        "e_0078→e_0078": 5,
        "e_0079→e_0079": 5,
        "e_0080→e_0080": 5,
        "e_0081→e_0081": 5,
        "e_0082→e_0082": 5,
        "e_0083→e_0083": 5,
        "e_0084→e_0084": 5,
        "e_0085→e_0085": 5,
        "e_0086→e_0086": 5,
        "e_0087→e_0087": 5,
        "e_0088→e_0088": 5,
        "e_0089→e_0089": 5,
        "e_0090→e_0090": 5,
        "e_0091→e_0091": 5,
        "e_0092→e_0092": 5,
        "e_0093→e_0093": 5,
        "e_0094→e_0094": 5,
        "e_0095→e_0095": 5,
        "e_0096→e_0096": 5,
        "e_0097→e_0097": 5,
        "e_0098→e_0098": 5,
        "e_0099→e_0099": 5,
        "e_0100→e_0100": 5,
        "e_0101→e_0101": 5,
        "e_0102→e_0102": 5,
        "e_0103→e_0103": 5,
        "e_0104→e_0104": 5,
        "e_0105→e_0105": 5,
        "e_0106→e_0106": 5,
        "e_0107→e_0107": 5,
        "e_0108→e_0108": 5,
        "e_0109→e_0109": 5,
        "e_0110→e_0110": 5,
        "e_0111→e_0111": 5,
        "e_0112→e_0112": 5,
        "e_0113→e_0113": 5,
        "e_0114→e_0114": 5,
        "e_0115→e_0115": 5,
        "e_0116→e_0116": 5,
        "e_0117→e_0117": 5,
        "e_0118→e_0118": 5,
        "e_0119→e_0119": 5,
        "e_0120→e_0120": 5,
        "e_0121→e_0121": 5,
        "e_0122→e_0122": 5,
        "e_0123→e_0123": 5,
        "e_0124→e_0124": 5,
        "e_0125→e_0125": 5,
        "e_0126→e_0126": 5,
        "e_0127→e_0127": 5,
        "e_0128→e_0128": 5,
        "e_0129→e_0129": 5,
        "e_0130→e_0130": 5,
        "e_0131→e_0131": 5,
        "e_0132→e_0132": 5,
        "e_0133→e_0133": 5,
        "e_0134→e_0134": 5,
        "e_0135→e_0135": 5,
        "e_0136→e_0136": 5,
        "e_0137→e_0137": 5,
        "e_0138→e_0138": 5,
        "e_0139→e_0139": 5,
        "e_0140→e_0140": 5,
        "e_0141→e_0141": 5,
        "e_0142→e_0142": 5,
        "e_0143→e_0143": 5,
        "e_0144→e_0144": 5,
        "e_0145→e_0145": 5,
        "e_0146→e_0146": 5,
        "e_0147→e_0147": 5,
        "e_0148→e_0148": 5,
        "e_0149→e_0149": 5,
        "e_0150→e_0150": 5,
        "e_0151→e_0151": 5,
        "e_0152→e_0152": 5,
        "e_0153→e_0153": 5,
        "e_0154→e_0154": 5,
        "e_0155→e_0155": 5,
        "e_0156→e_0156": 5,
        "e_0157→e_0157": 5,
        "e_0158→e_0158": 5,
        "e_0159→e_0159": 5,
        "e_0160→e_0160": 5,
        "e_0161→e_0161": 5,
        "e_0162→e_0162": 5,
        "e_0163→e_0163": 5,
        "e_0164→e_0164": 5,
        "e_0165→e_0165": 5,
        "e_0166→e_0166": 5,
        "e_0167→e_0167": 5,
        "e_0168→e_0168": 5,
        "e_0169→e_0169": 5,
        "e_0170→e_0170": 5,
        "e_0171→e_0171": 5,
        "e_0172→e_0172": 5,
        "e_0173→e_0173": 5,
        "e_0174→e_0174": 5,
        "e_0175→e_0175": 5,
        "e_0176→e_0176": 5,
        "e_0177→e_0177": 5,
        "e_0178→e_0178": 5,
        "e_0179→e_0179": 5,
        "e_0180→e_0180": 5,
        "e_0181→e_0181": 5,
        "e_0182→e_0182": 5,
        "e_0183→e_0183": 5,
        "e_0184→e_0184": 5,
        "e_0185→e_0185": 5,
        "e_0186→e_0186": 5,
        "e_0187→e_0187": 5,
        "e_0188→e_0188": 5,
        "e_0189→e_0189": 5,
        "e_0190→e_0190": 5,
        "e_0191→e_0191": 5,
        "e_0192→e_0192": 5,
        "e_0193→e_0193": 5,
        "e_0194→e_0194": 5,
        "e_0195→e_0195": 5,
        "e_0196→e_0196": 5,
        "e_0197→e_0197": 5,
        "e_0198→e_0198": 5,
        "e_0199→e_0199": 5
      },
      "latency_ms": {
        "n": 1000,
        "mean_ms": 0.03,
        "p50_ms": 0.02,
        "p90_ms": 0.05,
        "max_ms": 0.43
      }
    },
    "tool": {
      "n": 1000,
      "hit@5": 1.0,
      "mean_position": 1.98,
      "latency_ms": {
        "n": 1000,
        "mean_ms": 0.47,
        "p50_ms": 0.39,
        "p90_ms": 0.7,
        "max_ms": 3.71
      }
    },
    "dedup": {
      "n": 1000,
      "accuracy": 1.0,
      "recall": 1.0,
      "false_positive_rate": 0.0,
      "confusions": {
        "tp": 500,
        "fp": 0,
        "tn": 500,
        "fn": 0
      },
      "latency_ms": {
        "n": 1000,
        "mean_ms": 0.16,
        "p50_ms": 0.16,
        "p90_ms": 0.29,
        "max_ms": 0.74
      }
    },
    "wall_ms": 30676.7
  },
  "local": {
    "recall": {
      "all": {
        "n": 1000,
        "recall@1": 0.077,
        "recall@5": 0.249,
        "mrr": 0.1365,
        "wrong_memory_injection_rate": 0.751,
        "stale_knowledge_injection_rate": 0.0
      },
      "keyword_answerable": {
        "n": 627,
        "recall@1": 0.1212,
        "recall@5": 0.3876,
        "mrr": 0.2128,
        "wrong_memory_injection_rate": 0.6124,
        "stale_knowledge_injection_rate": 0.0
      },
      "keyword_unanswerable": {
        "n": 373,
        "recall@1": 0.0027,
        "recall@5": 0.0161,
        "mrr": 0.0081,
        "wrong_memory_injection_rate": 0.9839,
        "stale_knowledge_injection_rate": 0.0
      },
      "by_category": {
        "cross_topic": {
          "n": 167,
          "recall@1": 0.0599,
          "recall@5": 0.2395,
          "mrr": 0.1166,
          "wrong_memory_injection_rate": 0.7605,
          "stale_knowledge_injection_rate": 0.0
        },
        "entity_ref": {
          "n": 167,
          "recall@1": 0.024,
          "recall@5": 0.1317,
          "mrr": 0.0602,
          "wrong_memory_injection_rate": 0.8683,
          "stale_knowledge_injection_rate": 0.0
        },
        "fact_update": {
          "n": 167,
          "recall@1": 0.1976,
          "recall@5": 0.4671,
          "mrr": 0.2908,
          "wrong_memory_injection_rate": 0.5329,
          "stale_knowledge_injection_rate": 0.0
        },
        "paraphrase": {
          "n": 166,
          "recall@1": 0.0843,
          "recall@5": 0.2711,
          "mrr": 0.153,
          "wrong_memory_injection_rate": 0.7289,
          "stale_knowledge_injection_rate": 0.0
        },
        "recent_noise": {
          "n": 166,
          "recall@1": 0.0,
          "recall@5": 0.0,
          "mrr": 0.0,
          "wrong_memory_injection_rate": 1.0,
          "stale_knowledge_injection_rate": 0.0
        },
        "same_topic": {
          "n": 167,
          "recall@1": 0.0958,
          "recall@5": 0.3832,
          "mrr": 0.1974,
          "wrong_memory_injection_rate": 0.6168,
          "stale_knowledge_injection_rate": 0.0
        }
      },
      "by_tier": {
        "literal": {
          "n": 259,
          "recall@1": 0.1853,
          "recall@5": 0.5483,
          "mrr": 0.3116,
          "wrong_memory_injection_rate": 0.4517,
          "stale_knowledge_injection_rate": 0.0
        },
        "partial": {
          "n": 368,
          "recall@1": 0.0761,
          "recall@5": 0.2745,
          "mrr": 0.1433,
          "wrong_memory_injection_rate": 0.7255,
          "stale_knowledge_injection_rate": 0.0
        },
        "disjoint": {
          "n": 373,
          "recall@1": 0.0027,
          "recall@5": 0.0161,
          "mrr": 0.0081,
          "wrong_memory_injection_rate": 0.9839,
          "stale_knowledge_injection_rate": 0.0
        }
      },
      "latency_ms": {
        "n": 1000,
        "mean_ms": 16.58,
        "p50_ms": 16.94,
        "p90_ms": 18.6,
        "max_ms": 36.84
      }
    },
    "topic": {
      "n": 1000,
      "accuracy": 0.861,
      "confusions": {
        "in_topic→new_topic": 78,
        "switch→switch": 272,
        "new_topic→new_topic": 333,
        "in_topic→in_topic": 256,
        "switch→new_topic": 61
      },
      "latency_ms": {
        "n": 1000,
        "mean_ms": 4.53,
        "p50_ms": 4.35,
        "p90_ms": 5.39,
        "max_ms": 10.92
      }
    },
    "entity": {
      "n": 1000,
      "accuracy": 0.805,
      "confusions": {
        "e_0000→e_0000": 5,
        "e_0001→e_0061": 5,
        "e_0002→e_0002": 5,
        "e_0003→e_0003": 5,
        "e_0004→e_0024": 5,
        "e_0005→e_0015": 5,
        "e_0006→e_0006": 5,
        "e_0007→e_0007": 5,
        "e_0008→e_0008": 5,
        "e_0009→e_0009": 5,
        "e_0010→e_0010": 5,
        "e_0011→e_0011": 5,
        "e_0012→e_0012": 5,
        "e_0013→e_0013": 5,
        "e_0014→e_0014": 5,
        "e_0015→e_0015": 5,
        "e_0016→e_0016": 5,
        "e_0017→e_0017": 5,
        "e_0018→e_0018": 5,
        "e_0019→e_0019": 5,
        "e_0020→e_0190": 5,
        "e_0021→e_0061": 5,
        "e_0022→e_0022": 5,
        "e_0023→e_0013": 5,
        "e_0024→e_0024": 5,
        "e_0025→e_0015": 5,
        "e_0026→e_0026": 5,
        "e_0027→e_0027": 5,
        "e_0028→e_0008": 5,
        "e_0029→e_0029": 5,
        "e_0030→e_0030": 5,
        "e_0031→e_0031": 5,
        "e_0032→e_0032": 5,
        "e_0033→e_0033": 5,
        "e_0034→e_0054": 5,
        "e_0035→e_0035": 5,
        "e_0036→e_0036": 5,
        "e_0037→e_0017": 5,
        "e_0038→e_0038": 5,
        "e_0039→e_0039": 5,
        "e_0040→e_0040": 5,
        "e_0041→e_0061": 5,
        "e_0042→e_0042": 5,
        "e_0043→e_0043": 5,
        "e_0044→e_0014": 5,
        "e_0045→e_0045": 5,
        "e_0046→e_0046": 5,
        "e_0047→e_0047": 5,
        "e_0048→e_0048": 5,
        "e_0049→e_0049": 5,
        "e_0050→e_0050": 5,
        "e_0051→e_0051": 5,
        "e_0052→e_0032": 5,
        "e_0053→e_0053": 5,
        "e_0054→e_0054": 5,
        "e_0055→e_0055": 5,
        "e_0056→e_0056": 5,
        "e_0057→e_0057": 5,
        "e_0058→e_0048": 5,
        "e_0059→e_0069": 5,
        "e_0060→e_0070": 5,
        "e_0061→e_0061": 5,
        "e_0062→e_0032": 5,
        "e_0063→e_0043": 5,
        "e_0064→e_0064": 5,
        "e_0065→e_0065": 5,
        "e_0066→e_0066": 5,
        "e_0067→e_0057": 5,
        "e_0068→e_0088": 5,
        "e_0069→e_0069": 5,
        "e_0070→e_0070": 5,
        "e_0071→e_0071": 5,
        "e_0072→e_0072": 5,
        "e_0073→e_0073": 5,
        "e_0074→e_0074": 5,
        "e_0075→e_0075": 5,
        "e_0076→e_0066": 5,
        "e_0077→e_0017": 5,
        "e_0078→e_0088": 5,
        "e_0079→e_0069": 5,
        "e_0080→e_0080": 5,
        "e_0081→e_0081": 5,
        "e_0082→e_0082": 5,
        "e_0083→e_0083": 5,
        "e_0084→e_0084": 5,
        "e_0085→e_0085": 5,
        "e_0086→e_0066": 5,
        "e_0087→e_0087": 5,
        "e_0088→e_0088": 5,
        "e_0089→e_0089": 5,
        "e_0090→e_0090": 5,
        "e_0091→e_0091": 5,
        "e_0092→e_0032": 5,
        "e_0093→e_0093": 5,
        "e_0094→e_0094": 5,
        "e_0095→e_0095": 5,
        "e_0096→e_0096": 5,
        "e_0097→e_0097": 5,
        "e_0098→e_0088": 5,
        "e_0099→e_0099": 5,
        "e_0100→e_0100": 5,
        "e_0101→e_0101": 5,
        "e_0102→e_0112": 5,
        "e_0103→e_0043": 5,
        "e_0104→e_0104": 5,
        "e_0105→e_0105": 5,
        "e_0106→e_0106": 5,
        "e_0107→e_0107": 5,
        "e_0108→e_0108": 5,
        "e_0109→e_0109": 5,
        "e_0110→e_0110": 5,
        "e_0111→e_0111": 5,
        "e_0112→e_0112": 5,
        "e_0113→e_0113": 5,
        "e_0114→e_0114": 5,
        "e_0115→e_0115": 5,
        "e_0116→e_0116": 5,
        "e_0117→e_0117": 5,
        "e_0118→e_0118": 5,
        "e_0119→e_0119": 5,
        "e_0120→e_0120": 5,
        "e_0121→e_0061": 5,
        "e_0122→e_0112": 5,
        "e_0123→e_0123": 5,
        "e_0124→e_0104": 5,
        "e_0125→e_0125": 5,
        "e_0126→e_0126": 5,
        "e_0127→e_0127": 5,
        "e_0128→e_0128": 5,
        "e_0129→e_0149": 5,
        "e_0130→e_0130": 5,
        "e_0131→e_0131": 5,
        "e_0132→e_0112": 5,
        "e_0133→e_0133": 5,
        "e_0134→e_0134": 5,
        "e_0135→e_0135": 5,
        "e_0136→e_0136": 5,
        "e_0137→e_0137": 5,
        "e_0138→e_0138": 5,
        "e_0139→e_0149": 5,
        "e_0140→e_0150": 5,
        "e_0141→e_0141": 5,
        "e_0142→e_0112": 5,
        "e_0143→e_0143": 5,
        "e_0144→e_0144": 5,
        "e_0145→e_0145": 5,
        "e_0146→e_0146": 5,
        "e_0147→e_0147": 5,
        "e_0148→e_0148": 5,
        "e_0149→e_0149": 5,
        "e_0150→e_0150": 5,
        "e_0151→e_0151": 5,
        "e_0152→e_0152": 5,
        "e_0153→e_0153": 5,
        "e_0154→e_0154": 5,
        "e_0155→e_0155": 5,
        "e_0156→e_0156": 5,
        "e_0157→e_0157": 5,
        "e_0158→e_0158": 5,
        "e_0159→e_0159": 5,
        "e_0160→e_0160": 5,
        "e_0161→e_0161": 5,
        "e_0162→e_0162": 5,
        "e_0163→e_0163": 5,
        "e_0164→e_0164": 5,
        "e_0165→e_0165": 5,
        "e_0166→e_0166": 5,
        "e_0167→e_0167": 5,
        "e_0168→e_0168": 5,
        "e_0169→e_0169": 5,
        "e_0170→e_0170": 5,
        "e_0171→e_0171": 5,
        "e_0172→e_0172": 5,
        "e_0173→e_0173": 5,
        "e_0174→e_0174": 5,
        "e_0175→e_0175": 5,
        "e_0176→e_0176": 5,
        "e_0177→e_0177": 5,
        "e_0178→e_0178": 5,
        "e_0179→e_0149": 5,
        "e_0180→e_0180": 5,
        "e_0181→e_0181": 5,
        "e_0182→e_0192": 5,
        "e_0183→e_0183": 5,
        "e_0184→e_0184": 5,
        "e_0185→e_0185": 5,
        "e_0186→e_0186": 5,
        "e_0187→e_0187": 5,
        "e_0188→e_0188": 5,
        "e_0189→e_0189": 5,
        "e_0190→e_0190": 5,
        "e_0191→e_0191": 5,
        "e_0192→e_0192": 5,
        "e_0193→e_0193": 5,
        "e_0194→e_0194": 5,
        "e_0195→e_0195": 5,
        "e_0196→e_0196": 5,
        "e_0197→e_0197": 5,
        "e_0198→e_0198": 5,
        "e_0199→e_0199": 5
      },
      "latency_ms": {
        "n": 1000,
        "mean_ms": 2.55,
        "p50_ms": 2.45,
        "p90_ms": 3.07,
        "max_ms": 8.42
      }
    },
    "tool": {
      "n": 1000,
      "hit@5": 1.0,
      "mean_position": 2.0,
      "latency_ms": {
        "n": 1000,
        "mean_ms": 2.41,
        "p50_ms": 2.17,
        "p90_ms": 2.93,
        "max_ms": 129.61
      }
    },
    "dedup": {
      "n": 1000,
      "accuracy": 0.666,
      "recall": 1.0,
      "false_positive_rate": 0.668,
      "confusions": {
        "tp": 500,
        "fp": 334,
        "tn": 166,
        "fn": 0
      },
      "latency_ms": {
        "n": 1000,
        "mean_ms": 2.26,
        "p50_ms": 3.06,
        "p90_ms": 5.0,
        "max_ms": 10.02
      }
    },
    "backend": "onnx:bge-small-zh-v1.5:fp32:69a0b846f4f1",
    "wall_ms": 60475.1
  }
}
```

### `backend/evals/baseline_onnx.json`

```json
{
  "topic_prediction": {
    "n": 12,
    "in_topic_accuracy": 0.5,
    "switch_accuracy": 1.0,
    "new_topic_precision": 0.75,
    "new_topic_recall": 1.0,
    "false_new_rate": 0.3333,
    "false_switch_rate": 0.0
  },
  "retrieval": {
    "n": 8,
    "recall@1": 0.75,
    "recall@5": 1.0,
    "mrr": 0.8542,
    "wrong_memory_injection_rate": 0.25,
    "stale_knowledge_injection_rate": 0.0
  },
  "anchor_continuation": {
    "n": 5,
    "baseline": {
      "recall@1": 0.4,
      "recall@5": 1.0,
      "mrr": 0.6667,
      "wrong_memory_injection_rate": 0.2,
      "stale_memory_injection_rate": 0.0,
      "anchor_redundancy_rate": 0.0,
      "duplicate_injection_rate": 0.0,
      "anchor_distraction_rate": 0.3333
    },
    "focus_only": {
      "recall@1": 0.4,
      "recall@5": 1.0,
      "mrr": 0.6667,
      "wrong_memory_injection_rate": 0.2,
      "stale_memory_injection_rate": 0.0,
      "anchor_redundancy_rate": 0.0,
      "duplicate_injection_rate": 0.0,
      "anchor_distraction_rate": 0.3333
    },
    "anchor_distance": {
      "recall@1": 0.4,
      "recall@5": 1.0,
      "mrr": 0.6333,
      "wrong_memory_injection_rate": 0.4,
      "stale_memory_injection_rate": 0.0,
      "anchor_redundancy_rate": 0.0,
      "duplicate_injection_rate": 0.0,
      "anchor_distraction_rate": 0.6667
    },
    "decision": {
      "recall5_delta": 0.0,
      "wrong_rate_delta": 0.2,
      "anchor_distraction_rate": 0.6667,
      "verdict": "skip"
    }
  },
  "_run": {
    "embedding": "onnx",
    "model_dir": "C:\\Users\\zxy\\AppData\\Roaming\\qio\\models\\bge-small-zh-v1.5",
    "notes": [
      "onnx:bge-small-zh-v1.5:fp32:69a0b846f4f1"
    ]
  }
}
```


---

## 附录 D：运行命令与产物

### 本次实际执行过的关键命令

```powershell
# 真实语料
python -m agent.eval.real_run --step docs --n 200
# 真实记忆库 + 提问（DeepSeek）
python -m agent.eval.real_run --step store --n 200 --data-dir <临时目录> --out stress_real_200.json
# 话题 / 去重用例
python -m agent.eval.real_run --step cases --n 60 --data-dir <临时目录>
# A 臂 + B 臂
python -m agent.eval.real_run --step arms --data-dir <临时目录> --out stress_real_arms_200.json
# C 臂（话题 / 去重 / 实体 / 工具）与重排：见 stress_jev_arm.py / stress_rerank.py 的用法
uv run --frozen pytest tests/test_stress_rerank.py tests/test_stress_jev_arm.py -q
```

### 产物位置

| 产物 | 位置 |
| --- | --- |
| 真实记忆库 | `%TEMP%\qio-stress-cache\qio-real-200b\app.db` |
| 语料/提问/用例缓存 | `%TEMP%\qio-stress-cache\{wiki_docs,questions,real_cases,extra_cases}.jsonl/json` |
| 三臂结果 | `backend/evals/stress_real_arms_200.json` |
| 合成语料对照结果 | `backend/evals/stress_rules_vs_local.json` |
| 内置模型基线（8 条老用例） | `backend/evals/baseline_onnx.json` |

### 测试

本次新增测试共 9 个文件、40 条用例，全部离线可跑（真实模型调用不进 pytest）：

```bash
cd backend
uv run --frozen pytest -q
```

