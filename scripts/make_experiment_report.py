# -*- coding: utf-8 -*-
"""把「六项工作是否该交给模型做」这次实验打包成一个自包含的 md。

用法（在仓库根目录）：

    python scripts/make_experiment_report.py

产物：docs/superpowers/notes/2026-09-24-six-jobs-experiment-full.md
它包含：实验说明、全部新增代码、全部测试代码、运行命令、原始结果 JSON。
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "superpowers" / "notes" / "2026-09-24-six-jobs-experiment-full.md"

MODULES = [
    ("backend/src/agent/eval/real_corpus.py", "真实语料层：下载/切片/缓存/预算守卫"),
    ("backend/src/agent/eval/real_run.py", "真实记忆库构建、提问与用例生成、装配与跑臂"),
    ("backend/src/agent/eval/stress_corpus.py", "合成压力语料生成器（对照用）"),
    ("backend/src/agent/eval/stress_metrics.py", "指标：召回/分类/二分类/排序/延迟"),
    ("backend/src/agent/eval/stress_arms.py", "A 臂（规则+BM25）与 B 臂（本地 embedding）"),
    ("backend/src/agent/eval/stress_jev_arm.py", "C 臂（Jev）：话题判定/实体/工具/去重"),
    ("backend/src/agent/eval/stress_rerank.py", "候选重排三件套 + 条件触发"),
    ("backend/src/agent/eval/stress_run.py", "合成语料的跑批器与 CLI"),
    ("backend/src/agent/eval/jev_client.py", "Jev 客户端（OpenRouter Decisions）"),
    ("backend/src/agent/eval/embedding_backend.py", "评测用的本地嵌入后端装配"),
    ("backend/src/agent/eval/retrieval_eval.py", "检索评测（本次改造：可注入召回后端）"),
    ("backend/src/agent/eval/topic_eval.py", "话题评测（本次改造：可注入嵌入后端）"),
    ("backend/src/agent/eval/run.py", "离线评测入口（本次改造：--embedding onnx）"),
]

TESTS = [
    "backend/tests/test_real_corpus.py",
    "backend/tests/test_stress_corpus.py",
    "backend/tests/test_stress_metrics.py",
    "backend/tests/test_stress_arms.py",
    "backend/tests/test_stress_jev_arm.py",
    "backend/tests/test_stress_rerank.py",
    "backend/tests/test_stress_run.py",
    "backend/tests/test_jev_client.py",
    "backend/tests/test_eval_embedding_backend.py",
]

RESULTS = [
    "backend/evals/stress_real_arms_200.json",
    "backend/evals/stress_real_200.json",
    "backend/evals/stress_rules_vs_local.json",
    "backend/evals/baseline_onnx.json",
]

HEADER = """# 六项工作是否该交给模型：一次真实环境实验的完整记录

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
uv run --frozen python -m agent.eval.real_run --step store --n 200 \\
  --data-dir "%TEMP%\\qio-stress-cache\\qio-real-200b" --out stress_real_200.json
# 3) 生成话题与去重用例
uv run --frozen python -m agent.eval.real_run --step cases --n 60 \\
  --data-dir "%TEMP%\\qio-stress-cache\\qio-real-200b"
# 4) 跑 A 臂与 B 臂
uv run --frozen python -m agent.eval.real_run --step arms \\
  --data-dir "%TEMP%\\qio-stress-cache\\qio-real-200b" --out stress_real_arms_200.json
# 5) C 臂（需要 OPENROUTER_API_KEY）与重排实验见附录 D 的命令
```

固定的随机种子与内容哈希缓存保证：**重跑命中缓存不再产生模型费用**。

---

## 附录 A：实验代码

以下文件是本次实验新增或改造的全部代码。

"""

FOOTER = """

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
| 真实记忆库 | `%TEMP%\\qio-stress-cache\\qio-real-200b\\app.db` |
| 语料/提问/用例缓存 | `%TEMP%\\qio-stress-cache\\{wiki_docs,questions,real_cases,extra_cases}.jsonl/json` |
| 三臂结果 | `backend/evals/stress_real_arms_200.json` |
| 合成语料对照结果 | `backend/evals/stress_rules_vs_local.json` |
| 内置模型基线（8 条老用例） | `backend/evals/baseline_onnx.json` |

### 测试

本次新增测试共 9 个文件、40 条用例，全部离线可跑（真实模型调用不进 pytest）：

```bash
cd backend
uv run --frozen pytest -q
```

"""


def fence(path: Path, title: str, label: str = "") -> str:
    body = path.read_text(encoding="utf-8")
    return f"\n### `{label or path.as_posix()}`\n\n{title}\n\n```python\n{body}```\n"


def main() -> int:
    parts: list[str] = [HEADER]
    for rel, title in MODULES:
        path = ROOT / rel
        if path.exists():
            parts.append(fence(path, title, rel))
    parts.append("\n\n---\n\n## 附录 B：测试代码\n\n")
    for rel in TESTS:
        path = ROOT / rel
        if path.exists():
            parts.append(fence(path, "测试", rel))
    parts.append("\n\n---\n\n## 附录 C：原始结果 JSON\n\n")
    for rel in RESULTS:
        path = ROOT / rel
        if not path.exists():
            continue
        try:
            pretty = json.dumps(json.loads(path.read_text(encoding="utf-8")), ensure_ascii=False, indent=2)
        except Exception:
            pretty = path.read_text(encoding="utf-8")
        parts.append(f"\n### `{rel}`\n\n```json\n{pretty}\n```\n")
    parts.append(FOOTER)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("".join(parts), encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
