# 跨话题召回：实验记录（第二阶段 · Agent E，2026-10-02）

结论先行：**三个方案都没有通过「跨话题有收益 且 普通集不退化」的门槛，生产默认保持现状
（CROSS_TOPIC 全部开关关闭）**。跨话题 R@1 低的主要来源不是候选生成、也不是排序层缺维度，
而是这类查询与记忆正文之间**本来就缺共同词面/语义**：46 条里 15 条连底层召回都找不到，
其余大多数是「召回到了却不该排第一」。详见下面的缺口分解。

复现命令（backend 目录下；本机 %TEMP% 不可用，先设 TEMP）：

```powershell
$env:TEMP='<可写目录>'; $env:TMP=$env:TEMP
uv run --frozen python evals/cross_topic/build_queries.py
uv run --frozen python evals/cross_topic/run_cross_topic.py --all --summary-from oldest --json evals/cross_topic/results_oldest.json
uv run --frozen python evals/cross_topic/diagnose_gap.py --json evals/cross_topic/diagnosis.json
uv run --frozen pytest -q tests/test_cross_topic_recall.py
```

---

## 1. 数据集

* 记忆：复用 `evals/retrieval_ranking/corpus.json` 的 67 条（10 个话题），**不复制**，避免两份事实来源。
* 跨话题查询：`evals/cross_topic/queries_cross_topic.json` —— **46 条**，7 个子类，
  每条带 `note` 说明为什么属于这一类；锚点话题一律不是目标话题：

| 子类 | n | 形态 |
| --- | --- | --- |
| explicit_title | 10 | 「之前聊过的云南旅行那件事，结论是什么」 |
| keyword_only | 6 | 「那个 sqlite 的事后来怎么定的」（只出现关键词） |
| pronoun_only | 8 | 「那个方案后来定了吗」（**没有任何词面线索**） |
| conclusion_review | 8 | 「上次那个结论再确认一下」 |
| multi_topic | 5 | 一句话里两个话题，问后一个 |
| mixed_language | 5 | 「之前说的 cache 那套方案定了吗」 |
| long_span_old | 4 | 目标话题最早记忆 ≥130 天前 |

ground truth 口径：`expected` = 目标话题**最新一条仍然成立**的事实；
同时报告**话题级**指标 `topic_hit@1/@5`（是否召回目标话题的任意一条记忆）——
产品真正需要的是「把那个话题找回来」，不是精确到某一条。

---

## 2. 基线（生产现状 P0，真实 ONNX bge-small-zh-v1.5 fp32）

| 臂 | 集合 | R@1 | R@5 | MRR | topic@1 | topic@5 | wrong 注入 | stale 注入 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| P0（向量） | 跨话题(46) | **0.065** | 0.217 | 0.106 | **0.435** | 0.804 | 0.935 | 0.152 |
| P0（向量） | 普通(72) | **0.847** | 0.917 | 0.873 | — | — | 0.153 | 0.028 |
| P0_bm25 | 跨话题(46) | 0.043 | 0.087 | 0.062 | 0.348 | 0.587 | 0.957 | 0.130 |
| P0_bm25 | 普通(72) | 0.694 | 0.806 | 0.741 | — | — | 0.306 | 0.056 |

子类（P0 向量）：explicit_title R@1 0.200 / topic@1 0.700；keyword_only 0.000 / 0.667；
pronoun_only 0.000 / 0.125；conclusion_review 0.000 / 0.125；multi_topic 0.200 / 0.200；
mixed_language 0.000 / 0.600；long_span_old 0.000 / 0.750。

**指代类（pronoun / conclusion_review，共 16/46）的 topic@1 只有 0.125** —— 查询里没有目标话题的
任何词面线索，这不是排序能解决的问题。

---

## 3. 缺口分解（`diagnose_gap.py`，46 条）

| 层级 | 命中 | 说明 |
| --- | --- | --- |
| 底层召回（放宽到 30）可见期望文档 | 31/46 | embedding 本身能找到 |
| 进入生产候选池（前 10） | 14/46 | Selector 截断后 |
| 进入最终 top-5 | 10/46 | 排序后 |
| 缺口分解 | 候选池截断 **17** / 排序排不上 **4** / 召回就找不到 **15** | |

对照实验：**只把候选池放宽（2×5 → 2×20）再取前 5 → 10/46 掉到 6/46（更差）**。
原因是最终分数 = 相关度 + 规则分项，规则分项让最终顺序对候选池大小**不再单调**：
扩池放进来的新候选带着自己的 anchor/keyword 分项，会把原本排第一的正确记忆挤下去。
所以「扩池」不是免费的，更不是本问题的解。

判读：
* 15/46 连召回都到不了 → 查询与记忆正文没有共同信号，属于**数据/表达鸿沟**；
* 17/46 在召回里可见但被候选池截断 → 扩池理论上能让它们进池，但实测 top-5 反而更差；
* 只有 4/46 是「池内、却没排进 top-5」→ 排序层可救的空间很小。

---

## 4. 方案 A / B / C 实测

三个方案都只改**候选生成/查询文本**，排序公式一行未动（严格保持单层排序）。

| 臂 | 跨话题 R@1 | 跨话题 topic@1 | 跨话题 topic@5 | 普通集 R@1 | 结论 |
| --- | --- | --- | --- | --- | --- |
| P0（现状） | 0.065 | 0.435 | 0.804 | 0.847 | 基线 |
| A_rewrite（指纹补词改写） | 0.022 | 0.413 | 0.761 | **0.444** | 双向变差，**否决** |
| B_expand（指纹候选扩充，正确同源打分） | 0.065 | 0.435 | 0.783 | 0.847 | 无收益（与 P0 等价），**不启用** |
| C_relation（来源链扩展） | 0.065 | 0.413 | 0.696 | 0.847 | 无收益且 topic@5 略降，**不启用** |
| O_oracle（**诊断上界**：直接给出正确话题） | 0.065 | 0.435 | 0.804 | 0.847 | 与 P0 **完全相同** |

O_oracle 是最有信息量的一条：**把正确话题直接告诉检索层，top-5 一点没变**。
说明「候选生成拿不到目标话题」不是瓶颈 —— 目标话题的记忆本来就在池里（topic@5 0.804），
它们只是排不到前面。要改变排序结果，只能改排序信号（而第一阶段已用数据把
recency/affinity 关掉了，因为它们在普通集上有害）。

### C 的结构性诊断
`run_cross_topic.py --arm C_relation` 会打印：
```
[C 诊断] 来源链给出话题的查询 118/118；其中**全部等于锚点话题**（跨不出话题边界）118/118
```
原因在写入侧：`FragmentManager.validate_source` 要求来源必须属于同一话题
（「不能跨话题继承」），所以 `source_fragment_id` 链**在结构上**只能回指当前话题。
关系感知检索因此无法成为跨话题召回的手段；它最多能多召回一点当前话题的记忆。

---

## 5. 两次踩坑（都记在这里，避免以后重复）

1. **候选分数必须同源**。第一版 `_expand_candidates` 用「话题指纹文本」去召回，
   候选分数是相对**指纹**算的；把它和主召回（相对**用户查询**算的）放进同一个排序，
   等于比两把尺子。实测：跨话题 R@1 0.065→0.326 看似大涨，但**普通集 R@1 0.847→0.347**、
   wrong 注入 0.153→0.653 —— 那点涨幅是用普通集换来的。改成「同一个查询放宽候选池 + 按话题过滤」
   之后，普通集恢复 0.847，跨话题收益同时归零。护栏见
   `tests/test_cross_topic_recall.py::test_expansion_keeps_the_same_source_of_scores`。
2. **指纹摘要里可能装着答案**。评测里话题指纹的「摘要预览」如果取最新一条记忆的正文，
   而 ground truth 恰好也是「最新一条仍然成立的事实」，就等于把答案放进了输入：
   此时 O_oracle 的 R@1 是 1.000。用 `--summary-from oldest` 的无泄漏变体重跑，
   O_oracle 的 R@1 回到 0.065（topic@1 仍是 1.000）。两个数都在
   `results_latest.json` / `results_oldest.json` 里。

---

## 6. 决策（E3）

* **A/B/C 都不进生产默认**：A 双向变差；B/C 在「正确同源打分」下与现状等价，
  而在能拿到收益的那种写法下会打崩普通集。`params.CROSS_TOPIC` 三个开关全部默认 False，
  由 `tests/test_cross_topic_recall.py::test_cross_topic_switches_default_to_off` 钉住。
* 机制本身保留为**可选开关**（写作用域内、默认关）：它现在的价值是「有了可靠话题信号之后
  可以直接接进来」，而不是现在就打开。
* **cross-topic 召回的上限来自数据集本身的语义鸿沟**（同一句话在话题 A 的讨论里出现，
  再次提到时靠「那个方案」指代），不是排序问题：46 条里 15 条底层召回就找不到，
  17 条被候选池截断且扩池实测更差，只有 4 条是排序可救的。
* **建议（Cross-route，不在本任务范围内）**：真正可能有用的是**话题层**——
  当用户明确指向某个话题时，把该话题的**当前状态摘要**（topic fingerprint 的 summary_preview，
  生产上来自最近封存片段的摘要）作为一种候选注入，而不是再去记忆库里挑片段。
  这需要改 injection/topic 层，且要用真实话题摘要验证（本次的「上界」实验用了
  「最新记忆正文」近似，有构造成分，不足以作为生产证据）。

---

## 7. 指标口径与文件清单

| 文件 | 内容 |
| --- | --- |
| `evals/cross_topic/build_queries.py` | 46 条查询的确定性生成器（可重建） |
| `evals/cross_topic/queries_cross_topic.json` | 查询 + 子类 + note + oracle_topics |
| `evals/cross_topic/run_cross_topic.py` | 方案对比（P0/A/B/C/O/P0_bm25），普通集与跨话题分开报 |
| `evals/cross_topic/diagnose_gap.py` | 缺口分解 + 扩池对照 |
| `evals/cross_topic/results_latest.json / results_oldest.json` | 原始结果（含指纹摘要两种取法） |
| `evals/cross_topic/results_p0_abc.json` | **第一版（错误写法）**的原始结果：候选用指纹文本召回、分数与主召回不同源，B 的跨话题 R@1 0.326 但普通集塌到 0.486 —— 踩坑证据，不要引用它的增益 |
| `evals/cross_topic/diagnosis.json` | 缺口分解原始数据 |
