# qio 判断工作实验报告（机器可读版）

日期：2026-09-27　仓库：`qio`

> 本文件面向自动化消费：每个数字都带 `source`（原始记录位置）、`n`（样本量）、
> `status`（valid / invalid / unverified）。结论只从 `valid` 的条目得出。

```json
{
  "experiment_id": "qio-embedding-vs-rules-vs-jev",
  "date": "2026-09-27",
  "overall_status": "partial",
  "status_legend": {
    "valid": "数字由逐条记录重算或可直接从结果文件核验，且实验装置已知正确",
    "invalid": "已知实验装置错误，数字作废，仅保留以说明错因",
    "unverified": "数字存在但缺少逐条记录，无法独立核验"
  },
  "arms": {
    "A": "规则 + 关键词（BM25）+ 时效/亲和加权",
    "B": "本地 embedding：bge-small-zh-v1.5 fp32 ONNX，CPU，512 维",
    "C": "云端判定模型：typesafe/jev-1.13（OpenRouter decisions API）",
    "D": "B 的候选 + C 的重排/判定"
  },
  "corpora": {
    "wiki200": {
      "memories": 200,
      "topics": 200,
      "text_source": "wikimedia/wikipedia 20231101.zh train-00002（正文写入 fragments.summary）",
      "questions": 600,
      "source": "evals/stress_real_arms_200_fixed.json, evals/runs/real-arms-200/*"
    },
    "scenarios_frozen": {
      "scenarios": 18,
      "cases": 31,
      "dev": 20,
      "test": 11,
      "holdout": 12,
      "note": "人工核验、含多答案与旧版本标注；冻结记录 evals/scenarios/FROZEN.json",
      "source": "evals/runs/frozen-scenarios/*, evals/runs/four-way/*"
    },
    "topic_cases": {
      "in_topic": 200,
      "out_of_topic": 24,
      "note": "in_topic 的消息形如「继续 <话题名> 这个话题」；out_of_topic 为人工撰写的无关提问",
      "source": "evals/runs/topic-arms/20260927T154331-80060b"
    }
  }
}
```

## 1. 有效结果（可以直接拿去做决定）

### 1.1 找相关记忆（召回）

```json
[
  {"job": "recall", "corpus": "wiki200", "n": 600, "arm": "A", "metric": "recall@5", "value": 0.877, "status": "valid"},
  {"job": "recall", "corpus": "wiki200", "n": 600, "arm": "B", "metric": "recall@5", "value": 0.813, "status": "valid"},
  {"job": "recall", "corpus": "wiki200", "n": 600, "arm": "A", "metric": "recall@1", "value": 0.810, "status": "valid"},
  {"job": "recall", "corpus": "wiki200", "n": 600, "arm": "B", "metric": "recall@1", "value": 0.655, "status": "valid"}
]
```

### 1.2 四方案对照（冻结场景集；召回@1 / 召回@5）

```json
[
  {"config": "weights_0.4_0.25_0.35", "split": "dev",     "n": 20, "A": [0.65, 0.65], "B": [0.20, 0.25], "C": [0.45, 0.85], "D": [0.80, 0.85], "status": "valid"},
  {"config": "weights_0.4_0.25_0.35", "split": "holdout", "n": 12, "A": [0.50, 0.60], "B": [0.20, 0.30], "C": [0.50, 0.90], "D": [0.90, 0.90], "status": "valid"},
  {"config": "weights_1_0_0",         "split": "dev",     "n": 20, "A": [0.65, 0.65], "B": [0.40, 0.65], "C": [0.70, 0.95], "D": [0.95, 1.00], "status": "valid"},
  {"config": "weights_1_0_0",         "split": "holdout", "n": 12, "A": [0.50, 0.60], "B": [0.40, 0.80], "C": [0.70, 1.00], "D": [0.90, 1.00], "status": "valid"}
]
```

其中 `C` = 关键词与向量按倒数排名合并（RRF，`backend/src/agent/eval/hybrid_recall.py`），
`D` = 在 `C` 的候选上让 Jev 逐条判断"是否真的回答了这个问题"再排序。
来源：`evals/runs/four-way/20260927T153233-999936/cases.jsonl`（128 条逐条记录）。

### 1.3 话题判定（绑定正确的用例 + 校准后的判断线）

```json
[
  {"job": "topic", "n": 208, "arm": "A",        "threshold": 0.20, "in_topic_keep": 0.21, "out_of_topic_correct_new": 1.00, "overall_action_accuracy": 0.30, "status": "valid"},
  {"job": "topic", "n": 208, "arm": "B",        "threshold": 0.55, "in_topic_keep": 0.93, "out_of_topic_correct_new": 1.00, "overall_action_accuracy": 0.94, "status": "valid"},
  {"job": "topic", "n": 208, "arm": "B",        "threshold": 0.70, "in_topic_keep": 0.37, "out_of_topic_correct_new": 1.00, "overall_action_accuracy": 0.44, "status": "valid"}
]
```

来源：`evals/runs/topic-arms/20260927T154331-80060b/cases.jsonl`。
`threshold` 是判定"是否已有话题"的判断线，生产默认值为 0.70。

### 1.4 判断线校准（在题内 200 条 / 题外 24 条上的两条曲线）

```json
{
  "in_topic_top1": {"n": 200, "argmax_topic_correct": 0.99, "p10": 0.562, "median": 0.664, "min": 0.507},
  "out_of_topic_top1": {"n": 24, "p90": 0.431, "median": 0.377, "max": 0.542},
  "sweep": [
    {"threshold": 0.70, "keep_correct_topic": 0.37, "false_keep_out_of_topic": 0.00},
    {"threshold": 0.60, "keep_correct_topic": 0.78, "false_keep_out_of_topic": 0.00},
    {"threshold": 0.55, "keep_correct_topic": 0.93, "false_keep_out_of_topic": 0.00},
    {"threshold": 0.50, "keep_correct_topic": 1.00, "false_keep_out_of_topic": 0.04},
    {"threshold": 0.40, "keep_correct_topic": 1.00, "false_keep_out_of_topic": 0.38}
  ],
  "status": "valid"
}
```

### 1.5 排序公式的结构性影响

```json
[
  {"observation": "纯余弦召回下，正确答案落在前 10 之外的比例", "corpus": "scenarios_frozen/dev", "value": "1/20", "status": "valid"},
  {"observation": "同一话题内正确答案的名次（4 条抽样）", "value": "全部第 1", "status": "valid"},
  {"observation": "同一话题内正确答案相对最强干扰项的分数差", "cosine": 0.219, "bm25": 1.88, "status": "valid"},
  {"observation": "去掉时效权重后纯向量的召回@5（留出集）", "from": 0.30, "to": 0.80, "status": "valid"},
  {"observation": "去掉时效权重后规则+关键词的召回@5（留出集）", "from": 0.60, "to": 0.60, "status": "valid"}
]
```

### 1.6 拒答（库中没有答案）

```json
{
  "product_state": "不存在拒答路径：任何提问都会返回 5 条记忆",
  "score_separation": {"in_topic_median": 0.664, "in_topic_min": 0.507, "out_of_topic_median": 0.377, "out_of_topic_max": 0.542},
  "best_single_threshold": {"threshold": 0.5, "false_reject_answerable": 0.20, "correct_reject_unanswerable": 0.67},
  "conclusion": "单靠分数无法可靠拒答；该判断适合交给判定模型",
  "status": "valid"
}
```

## 2. 已作废的结果（附错因，供追溯）

```json
[
  {"result": "记忆检索对比：A 0.607 / B 0.823（前 5 命中）", "why_invalid": "记忆正文从未进入检索链路，实际只比较了标题", "cause": "跳过模型摘要时未把正文写入 fragments.summary", "fixed": "是", "superseded_by": "§1.1"},
  {"result": "话题判定 A 0.333 / B 0.333 / C 0.794", "why_invalid": "用例引用了旧库的话题编号，换库后全部悬空（current_topic 为空），且判断线未校准", "cause": "用例跨库复用 + 阈值 0.7 未按真实分数分布校准", "fixed": "是（用例绑定守卫 + 校准）", "superseded_by": "§1.3 / §1.4"},
  {"result": "合成语料上的结论（本地模型召回不如关键词）", "why_invalid": "语料为模板生成，同话题内存在大量同句式近重复条目，标注不唯一", "cause": "语料构造缺陷", "fixed": "是（改用真实语料）", "superseded_by": "§1.1"},
  {"result": "陈旧知识注入率 = 0（说明系统处理良好）", "why_invalid": "该语料未标注被更新的旧事实，0 只是没有测", "cause": "缺少标注", "fixed": "部分（人工场景已有旧版本标注）", "superseded_by": "§3 待复核项"}
]
```

## 3. 待复核（有数字但无逐条记录，或装置仍不完备）

```json
[
  {"item": "云端模型的话题判定", "old_value": 0.794, "reason": "在错配用例上测得，未重跑", "status": "unverified"},
  {"item": "云端模型的话题去重", "old_value": 0.917, "reason": "无逐条记录；且生产去重规则非单调（0.60 判重复、0.80 放行）尚未解释", "status": "unverified"},
  {"item": "云端模型的实体匹配 / 工具路由", "old_value": "1.00 / 1.00", "reason": "无逐条记录；实体卡为标题合成，工具仅 14 条", "status": "unverified"},
  {"item": "候选重排（条件触发）", "old_value": "召回@1 0.748 → 0.828", "reason": "无逐条记录；阈值在同一批数据上挑出，测试集已被看过", "status": "unverified"},
  {"item": "端到端是否有帮助", "old_value": null, "reason": "未做：需在相同回答模型与上下文预算下比较最终回答质量、旧事实误用、用户纠正次数、等待时间", "status": "not_started"}
]
```

## 4. 建议动作（按证据强度排序）

```json
[
  {"action": "把话题判定的判断线从 0.70 调到 0.55", "evidence": "§1.4：保留正确话题 0.37→0.93，误留题外仍为 0.00", "risk": "题外样本仅 24 条，建议先用真实对话再校一次"},
  {"action": "召回改为关键词与向量合并（RRF），不要单用任一路", "evidence": "§1.2：留出集前 5 命中 B 0.30 / A 0.60 → C 0.90", "risk": "合并后第 1 名命中偏低（0.50），需要后续重排"},
  {"action": "重定排序权重，尤其是时效项", "evidence": "§1.5：去掉时效后纯向量留出集 0.30→0.80，规则臂不变", "risk": "权重需按场景分别定，规则臂在测试集上会略降"},
  {"action": "补上拒答路径，并用判定模型而不是分数阈值", "evidence": "§1.6：单阈值最优仍误拒 20% 的正常提问", "risk": "引入联网与延迟"},
  {"action": "重排与拒答交给判定模型", "evidence": "§1.2 的 D 方案在留出集上第 1 名命中 0.90", "risk": "数字尚未逐条留痕，需重跑后才能采纳"}
]
```

## 5. 复现入口

| 用途 | 位置 |
| --- | --- |
| 逐条记录（唯一事实源） | `backend/evals/runs/*/cases.jsonl` + 同目录 `meta.json`（含 git 版本与配置） |
| 聚合函数 | `agent.eval.experiment_log.aggregate()` / `aggregate_by()` |
| 场景集与冻结记录 | `backend/evals/scenarios/{handmade,holdout,out_of_topic}.jsonl`、`FROZEN.json` |
| 混合召回 | `agent/eval/hybrid_recall.py` |
| 重排三件套 | `agent/eval/stress_rerank.py` |
| 云端判定臂 | `agent/eval/stress_jev_arm.py`、`agent/eval/jev_client.py` |
| 三条臂与跑批 | `agent/eval/stress_arms.py`、`agent/eval/real_run.py` |

```bash
# 冻结校验（场景文件被改动会报 changed）
python -m agent.eval.scenario_corpus
# 全量测试
cd backend && uv run --frozen pytest -q
```

## 6. 已知的实验装置缺陷（未来必须避免）

```json
[
  {"defect": "记忆正文未落盘", "impact": "整批召回结论方向相反", "guard": "已加入测试与文档；核对 fragments.summary 非空"},
  {"defect": "用例引用旧库编号", "impact": "话题用例在定义上无法做对", "guard": "bind_topic_cases() 逐条校验 + 4 条测试"},
  {"defect": "判断线未按真实分数分布校准", "impact": "丢掉 63% 判对的结果", "guard": "本次给出两条曲线与推荐值"},
  {"defect": "汇总数字手写进文档", "impact": "第三方无法复核", "guard": "改用 cases.jsonl + aggregate() 重算"},
  {"defect": "云端异常时丢弃尾部候选", "impact": "云端超时会导致候选缺失", "guard": "已修 + 测试"}
]
```
