# 第三阶段 · Agent E：跨话题（explicit/implicit）与无 embedding 的 Topic fallback 实验

两个结论先行：

1. **跨话题：没有可上线的改动。** 把 46 条拆成 explicit 30 / implicit 16 之后分别看：
   explicit 的 R@1 只有 0.100；把「目标话题的当前状态」当检索上下文（P2）**只有在状态文本
   自己就含答案（泄漏 46/46）时才好看**，换成不含答案的同话题其他记忆（无泄漏对照）反而
   比 P0 更差（R@5 0.267 → 0.067）。已知话题但不注入状态（P1）与 P0 完全一致。
2. **无 embedding 的 fallback：发现并定位了一个真实的量纲错配，修法有数据支持。**
   规则层分数实际落在 0~0.22，而门槛是 0.2 —— 结果是 72.8% 的延续句被判成新话题。
   按真实量纲重标定后：acc 0.4237 → **0.8305**，假新 0.728 → **0.185**，切换召回 0 → **0.909**；
   分层 5 折折内选参的诚实估计同样是 0.8305（不是过拟合）。
   需要改 params.py / affinity.py，**已按约定写成 Cross-route 请求，本轮不动生产代码**。

复现命令（backend 目录下；本机 %TEMP% 不可写，先设 TEMP）：

```powershell
$env:TEMP='<worktree>\.build-tmp'; $env:TMP=$env:TEMP
uv run --frozen python evals/cross_topic/explicit_state.py --json evals/cross_topic/explicit_state.json
uv run --frozen python evals/topic_fallback/run_fallback_arms.py --diagnose --sweep --cv --json evals/topic_fallback/results.json
uv run --frozen pytest -q tests/test_p3_memory_topic_experiments.py
```

---

## E1 不复做上一阶段的实验

上一阶段已验证无收益、本轮**未重做**：query rewrite / topic fingerprint expansion /
relation expansion / 单纯扩大 candidate pool。依据与数据在
`backend/evals/EXPERIMENTS-CROSSTOPIC.md` 与 `backend/evals/cross_topic/`：
瓶颈不在候选生成（oracle 给对话题 top-5 一点不变），也不在排序层缺维度（只有 4/46 属于
「进池却没排上」），而是查询与记忆正文之间本来就缺共同信号。

---

## E2 把跨话题拆成两类（分开报，不混在一起）

| 组 | 子类 | n |
| --- | --- | --- |
| explicit（查询里有目标话题的区分性词面） | explicit_title / keyword_only / mixed_language / multi_topic / long_span_old | 30 |
| implicit（只有指代或复查措辞） | pronoun_only / conclusion_review | 16 |

---

## E3~E6 explicit 的新实验：目标 Topic 当前状态作为检索上下文

臂（`evals/cross_topic/explicit_state.py`，全部走生产检索路径，排序公式未动）：

* **P0** 当前生产行为
* **P1** 已知目标 Topic，但不注入状态（生产 `topic_hints` 候选扩充）
* **P2** 目标 Topic current state 作为检索上下文（`query + state` 合成**一次**检索 ——
  单一查询、单一打分来源，避免上一阶段「两把尺子混排」的坑）
* **P3** 状态直接作为 context，不做额外检索

状态文本来源（E4：只用真实记忆正文推导，**不人工编写 summary**）：

| 来源 | 构造 | 泄漏情况 |
| --- | --- | --- |
| `latest` | 目标话题最新一条仍然成立的记忆正文（≈ 生产「最近封存片段摘要」） | **46/46 泄漏** |
| `others` | 目标话题其余仍然成立的记忆正文拼接（构造上不含期望值） | 0/46 |

答案泄漏判据：状态文本覆盖期望记忆正文的词元 ≥60%，或逐字包含。

### 结果

| 状态来源 | 臂 | explicit R@1 | explicit R@5 | implicit R@1 | implicit R@5 | 未泄漏子集 R@1 / R@5 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| latest | P0 | 0.100 | 0.267 | 0.000 | 0.125 | n=0 | 基线 |
| latest | P1 | 0.100 | 0.267 | 0.000 | 0.125 | n=0 | 与 P0 完全一致 |
| latest | P2 | **1.000** | 1.000 | 1.000 | 1.000 | n=0 | **全部样本泄漏 → 不构成证据** |
| latest | P3 | 状态含期望 1.000 | — | 1.000 | — | n=0 | 同上 |
| others | P0 | 0.100 | 0.267 | 0.000 | 0.125 | 0.100 / 0.267 | 基线 |
| others | P1 | 0.100 | 0.267 | 0.000 | 0.125 | 0.100 / 0.267 | 无收益 |
| others | P2 | 0.000 | **0.067** | 0.000 | 0.062 | 0.000 / 0.067 | **比 P0 更差** |
| others | P3 | 状态含期望 0.000 | — | 0.000 | — | 0.000 | 状态里没有答案 |

普通集回归（P0）：R@1 0.847 / R@5 0.917 / MRR 0.873 / wrong 0.153 —— 与基线逐字段一致。

### 判读（E7：不满足上线条件）

* 「topic current state 当检索上下文」的好成绩**完全来自答案泄漏**：状态就是答案本身时，
  P2/P3 自然是 1.000。去掉泄漏后它比现状更差（R@5 0.267 → 0.067）——因为把状态词并进查询
  等于给查询加了大量与「要找哪条记忆」无关的词。
* explicit 的真正瓶颈仍然是：查询与「结论」那条记忆之间没有共同词面（P0 R@1 0.100），
  而 implicit（16 条）连目标话题都无从谈起（R@1 0.000）。
* 所以本轮**不改生产行为**；implicit 如实保留为未解决。

### E4 的硬限制：本机拿不到真实的 Topic/Fragment summary

| 命令 | 输出 |
| --- | --- |
| 扫描本机 QIO 数据库（`app.db` 等 3 处） | `messages: 22, fragments: 2, sealed: 0, with_summary: 0, topics: 2` |

即：**本机没有任何封存片段摘要可用于评测**。因此 P2/P3 的状态只能用真实**记忆正文**推导
（`latest` / `others`），其中 `latest` 是生产摘要的替身、且必然泄漏。
**「用真实模型生成的 topic summary 会怎样」在本机无法验证** —— 需要一份带封存片段摘要的真实库，
或 CI 里用记录式夹具。这条如实写进 Not Fixed，不当作已解决。

---

## E8 无 embedding 时的 Topic fallback

数据：`backend/evals/topic_threshold/cases.jsonl`（118 条，12 类），强制关 embedding
（`TopicPredictor(embedding=None)` → 规则层），判定仍走生产入口
`predict() + affinity.classify()`。

### 规则层到底有什么信号（`--diagnose`）

* 分数全部落在 **0 ~ 0.22**：`score = |查询词元 ∩ 话题关键词| / |查询词元|`，量级很小；
* 期望 `new_topic` 的 26 条里 **25 条 top=0**（真新话题=没有任何词面命中）；
* 期望 `switch` 的 11 条里 10 条 top>0 且 current=0（换话题时当前话题天然没命中）；
* 期望 `in_topic` 的 81 条里 44 条 current>0（另一半连一个词都没命中）。

**根因**：现有阈值 `rules_new_topic_threshold=0.2` /
`rules_incumbent_threshold=0.2` 是按另一个量纲定的；在这个分数带上几乎什么都拦不到，
于是「没有 owner + 现任阈值也够不着」→ 直接判新话题 → **假新话题 72.8%**。

### 四方案（118 条）

| 方案 | acc | 继续召回 | 新话题召回 | 假新 | 切换 | 说明 |
| --- | --- | --- | --- | --- | --- | --- |
| A 当前 fallback | 0.4237 | 0.309 | 0.962 | **0.728** | 0.000 | 量纲错配 |
| B 保守留在当前话题 | 0.6864 | 1.000 | 0.000 | 0.000 | 0.000 | 从不自动建新话题 |
| C（只改两条既有 rules_*） | **0.7458** | 0.790 | 0.923 | 0.304 | 0.000 | 生产安全变体 |
| **C（含 rules 专用 switch 门槛）** | **0.8305** | 0.790 | 0.923 | **0.185** | **0.909** | 需要新增两个孪生参数 |
| D 低置信交给后续机制 | 0.6780 | 0.988 | 0.000 | 0.120 | 0.000 | 62 次交接，其中真新话题 40.3% |

参数（扫描网格按真实量纲，见 `results.json` 的 `sweep`）：
* C-safe：`rules_new_topic_threshold=0.02, rules_incumbent_threshold=0.02`，其余不动；
* C-full：再加 `switch_threshold=0.02, switch_delta=0.02`；
* 0.01~0.05 是一片平台（同一成绩），取 0.02 作为带余量的中间值。

**分层 5 折（折内选参、折外计分）**：C-safe **0.7458**、C-full **0.8305** ——
与全量成绩完全相同，且每折都选中同一组参数 → 不是过拟合。

### 结论与建议（Cross-route）

* **A 必须换掉**：0.4237 的准确率、72.8% 的假新话题，是「embedding 不可用时把用户上下文拆散」。
* 若只允许改两条既有参数：`rules_new_topic_threshold 0.2 → 0.02`、
  `rules_incumbent_threshold 0.2 → 0.02` → acc 0.4237 → 0.7458（无需新参数、
  有测试证明不影响 onnx 路径）。
* 再加 `rules_switch_threshold / rules_switch_delta` 两个孪生参数（规则量纲 0.02/0.02）
  → acc **0.8305**、切换召回 **0.909**；`switch_threshold/switch_delta` 是 onnx 与规则共用的，
  直接改会破坏第一阶段已标定的 onnx 行为，所以必须走孪生参数。
* B/D 作为「宁可保守」的备选：B 的代价是从此不会自动建新话题（新话题召回 0）；
  D 保留了这个信息（62 次交接里有 40.3% 真的是新话题），代价是准确率低 15pt。
  如果产品更怕「拆散上下文」而不是「少建话题」，D 比 C 更稳妥 —— 这需要产品判断，不由实验单独决定。

---

## 文件清单

| 文件 | 内容 |
| --- | --- |
| `evals/cross_topic/explicit_state.py` | explicit/implicit 拆分 + P0~P3 + 泄漏检查 |
| `evals/cross_topic/explicit_state.json` | 上者的原始结果（两种状态来源） |
| `evals/topic_fallback/run_fallback_arms.py` | fallback A/B/C/D + 分数诊断 + 扫描 + 分层 5 折 |
| `evals/topic_fallback/results.json` | 上者的原始结果 |
| `tests/test_p3_memory_topic_experiments.py` | 9 条护栏（拆分、泄漏判据、C/B/D 质量、onnx 不受 rules_* 影响、结果入库） |
