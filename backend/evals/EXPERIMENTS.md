# QIO 检索/话题/片段智能：实验记录（2026-10-02）

四组实验全部**离线、可复现、不联网、不需要 API Key**。真实 embedding 用本机
QIO 数据目录里的 bge-small-zh-v1.5(fp32)，脚本会自己找；找不到就明确报错，
不静默降级（CI 上跑的是记录下来的余弦快照）。

模型身份：`onnx:bge-small-zh-v1.5:fp32:69a0b846f4f1`

---

## C1 话题判定阈值（backend/evals/topic_threshold/）

```powershell
cd backend
uv run --frozen python evals/topic_threshold_curve.py --json evals/topic_threshold/curve_onnx.json
uv run --frozen python evals/topic_threshold_cv.py    --json evals/topic_threshold/cv_onnx.json
```

* 语料 104 条（延续 / 措辞变化 / 跨话题引用 / 真新话题 / **对抗性新话题** /
  模糊转移 / 无信号 / 中文短句 / 英文 / 中英混合）；判定走生产路径
  `TopicPredictor.predict() -> affinity.classify(policy=...)`。
* 旧实现（一条绝对阈值 0.7 同时管延续/切换/新建）：acc **0.279**，in_topic 召回 **0.056**，
  false_new **0.926**。
* 新实现（owner-first 三门槛）：acc **0.885**，in_topic 召回 **0.930**，
  真新话题召回 **0.826**（19/23），false_new **0.074**。
* **诚实估计**：分层 5 折、折内选参、折外计分 → acc **0.731**、真新话题召回 **0.696**。
  生产默认值是「池化成绩 + 敏感度表」选出来的，**不是折内最优**；两个数都要看。
  104 条样本上 accuracy 的 95% 置信区间约 ±0.06，参数之间的差异大多在噪声里 ——
  真正的结论是结构（三门槛）而不是某一个具体数字。
* 无组合能达到真新话题召回 ≥ 0.80（网格 900 组）：真新话题与延续在 0.30~0.45
  分数带里**天然重叠**（对抗样本「Excel 透视表」「云南白药」与当前话题共享语义/词面），
  单靠阈值无法分开；已如实记录，未用调参掩盖。

敏感度（base: ntt=0.42 inc=0.42 sw=0.55 delta=0.15 minchars=8 → acc 0.885 / new_R 0.826）：

| 维度 | −0.05 | +0.05 |
| --- | --- | --- |
| new_topic_threshold | acc 0.875 / new_R 0.696 | acc 0.885 / new_R 0.826 |
| incumbent_threshold | acc 0.875 / new_R 0.652 | acc 0.846 / new_R 0.826 |
| switch_threshold | acc 0.885 / new_R 0.826 | acc 0.865 / new_R 0.826 |
| switch_delta | acc 0.875 / new_R 0.826 | acc 0.856 / new_R 0.826 |
| min_new_topic_chars（±4 字） | acc 0.827 / new_R 0.870 | acc 0.808 / new_R 0.348 |

不是尖峰；只有 min_new_topic_chars 调大时真新话题召回会掉（短的新话题被长度护栏吞掉），
这是明确的取舍：短确认必须延续，代价是 4~7 个字的真新话题更容易被并进当前话题。

---

## C2/C3 检索排序（backend/evals/retrieval_ranking/）

```powershell
cd backend
uv run --frozen python evals/retrieval_ranking/build_corpus.py          # 重建语料
uv run --frozen python evals/retrieval_ranking/run_arms.py --all --json evals/retrieval_ranking/arms_after_refactor.json
uv run --frozen python evals/retrieval_ranking/sweep_weights.py --json evals/retrieval_ranking/weight_sweep.json
```

语料：67 条记忆 / 72 条带标签查询（事实更新、决策、偏好、人物、跨话题、切换、换词、噪声抵抗）。

| 臂 | R@1 | R@5 | MRR | wrong 注入 | stale 注入 |
| --- | --- | --- | --- | --- | --- |
| A 旧两层排序（rel .4 / rec .25 / aff .35） | 0.597 | 0.750 | 0.664 | 0.403 | 0.000 |
| B/C 单层 + 纯相关性 | 0.792 | 0.903 | 0.840 | 0.208 | 0.042 |
| D BM25 词面 | 0.625 | 0.806 | 0.693 | 0.375 | 0.056 |
| E BM25+向量 RRF | 0.667 | 0.903 | 0.775 | 0.333 | 0.069 |
| +时效（旧比例） | 0.389 | 0.819 | 0.536 | 0.611 | 0.014 |
| +话题亲和（旧比例） | 0.722 | 0.917 | 0.793 | 0.278 | 0.042 |
| **+规则分项 0.25（生产默认）** | **0.847** | **0.917** | **0.873** | **0.153** | 0.028 |

* 结论 1：两层 → 单层本身是最大的一笔收益（R@1 +19.5pt）。
* 结论 2：时效与话题亲和**关掉**（权重 0）：前者明显有害，后者在旧比例下也有害；
  权重扫描 + 分层 5 折（折内选权）里它们从未被选中。
* 结论 3：规则分项（anchor/entity/keyword）保留 0.25 —— 单层里只加一次时确实有效；
  它以前被算了两遍（Selector 一次 + Retriever 一次）。
* BM25 与 RRF 混合都不如向量单层 → 不作为默认召回。

---

## C4 分段边界的语义切分（backend/evals/boundary_semantic/）

```powershell
cd backend
uv run --frozen python evals/boundary_semantic/record_scores.py        # 记录真实余弦
uv run --frozen python evals/boundary_semantic/run_boundary_arms.py --json evals/boundary_semantic/result_onnx.json
```

44 条（七类边界 + 否定/引用/假设陷阱）：

| 臂 | acc | split 召回 | 误切 | 漏切 |
| --- | --- | --- | --- | --- |
| 确定性规则（生产现状） | 0.8182 | 0.333 | 0 | 8 |
| 规则 + 语义（th=0.35） | 0.8409 | 0.417 | 0 | 7 |
| 规则 + 语义（th=0.45） | 0.750 | 0.583 | 6 | 5 |

**结论：保持 shadow，不打开语义切分。** 增益只有 1 个用例，且阈值从 0.35 挪到 0.45
误切就从 0 涨到 6；「说不准就不切」是产品纪律，误切（把同一段对话切碎）比漏切贵。
new_goal 类漏切由话题层（C1）负责，片段边界不是唯一机制。

---

## C5 旧 relation_type='unknown'（backend/tests/test_legacy_relation_migration.py）

* 迁移只做可确认的推断：坏来源（不存在 / 跨话题）断开并落 unknown；
  合法同话题来源保留并落 history_reopen（无损）；unknown 行不编造 same_stage / 来源。
* 路径隔离实际由 `source_fragment_id` + 话题校验保证，与 relation_type 无关：
  unknown 行没有来源 → 不会成为祖先 → 不可能作为【路径前提】扩大上下文（实测通过）。
* 顺带发现并修掉一个**空转的断言**：`test_path_context` 原来只查【路径前提】的
  标题行里有没有别的片段（正文在下一行），等于没查；改成按整块判断后仍然通过。
