# 记忆检索排序简化（单层统一排序）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: 本计划按任务逐条执行；每个任务先写失败测试，
> 再写最小实现。步骤用 `- [ ]` 跟踪。

**Goal:** 把「召回奖励 + 检索再加权」的两层业务排序收敛成一次统一排序；默认策略只按底层检索
相关程度排序，所有可选奖励默认关闭，数量与配置各只有一个权威来源。

**Architecture:** 候选阶段（`Selector`）只把底层召回分、来源与稳定身份原样带出来，按原始相关度
截候选池；唯一业务排序入口是新的 `agent/services/ranking.py`，由 `Retriever` 调用一次，
可选奖励（话题亲和 / 时效 / 关键词 / 实体）与可选重排都在此生效，权重全部集中在
`services/params.py`，默认 0 即纯相关性。

**Tech Stack:** Python 3.12、pytest、现有 BM25/ONNX 召回后端、`agent.eval.*` 离线评测。

**Spec:** 用户 2026-09-29 任务书（`memory retrieval ranking simplification`）。

## Global Constraints

- 不引入 Jev；不切换 BM25/embedding 默认后端；不新增混合召回。
- 不改查询上下文构造、话题导航、工具路由、实体识别、片段切分。
- 现有未提交改动全部保留；本次不 `git commit` / `push` / 发布。
- 后端验证：`cd backend; uv run --frozen pytest`（必须全绿）；
  改了 runtime/检索后重跑 `uv run --frozen python -m agent.eval.run` 与 `python scripts/check_docs.py`。
- 日志 / Trace / 评测输出不得出现密钥原文；新增 Trace 字段遵守 `agent/trace/redact.py`。

---

## 现状（已核实，来自当前源码）

调用链（自动注入，主 Turn）：

1. `turn_orchestrator.py:359` → `AppContext.build_injection()` → `ContextAssembler.build_injection()`
2. `context.py:407` 写死 `top_k=6` → `InjectionAssembler.build(..., top_k=6)`
3. `injection.py:346` → `Retriever.search(query, anchor_topic_id, top_k=6)`
4. `retrieval.py:127` → `Selector.select(query, top_k=6*2=12, anchor_topic_id)`
5. `selector.py:166` → `recall.search(query, top_k=12*3=36)`
6. `selector.py:187` 对每条候选 `score += rule_score(doc, ctx)`（anchor 0.5 / entity 0.3 /
   keyword 0.2 / recency ≤0.4），排序后截 12 条
7. `retrieval.py:151` 再次加权：`0.4*相关性归一 + 0.25*时效衰减 + 0.35*话题亲和`，并入实体卡，
   截 6 条
8. `injection.py:404` 身份去重 → `InjectionBudget.plan()`（`min_score=0.05`）→ 渲染注入

主动检索：`tools/memory_search.py:36` → `Retriever.search(top_k=clamp(1..20))`，同样经过
步骤 4–7。子 agent 复用同一 `Retriever`。

配置来源：`services/params.py:RETRIEVAL`（0.4/0.25/0.35）与
`services/retrieval.py:RetrievalConfig`（同样的默认值）**各有一份**；生产 `app.py:125`
用 `Retriever(...)` 不传 config → 直接吃 `RetrievalConfig` 默认值，**`params.RETRIEVAL`
对生产无效**。`RetrievalConfig.fingerprint_top` 只被定义、从未被读取。

数量：候选池 12、最终返回 6（自动）/ ≤20（主动）、底层召回 36；两个乘数（×2、×3）隐含在
调用链里。

## 目标结构

```
Selector.select(query, candidate_pool)      # 只按原始相关度取候选（无业务奖励）
        ↓ MemoryCandidate(doc_id, relevance, sources, topic_id, keywords, entity_ids, created_at)
ranking.rank(candidates, ctx, policy)       # 唯一业务排序入口（默认 relevance）
        ↓ RankedCandidate(relevance, factors, score, rank)
Retriever.search(...)                       # 只做编排：话题指纹 → rank() → 实体卡隔离合并
        ↓ RetrievalHit(relevance, factors, score, rank, strategy)
InjectionAssembler.build(...)               # 身份去重 → 预算 → 渲染（记录 dropped 原因）
```

---

### Task 1: 配置唯一权威来源（params.py）

**Files:**
- Modify: `backend/src/agent/services/params.py`

**Interfaces:**
- Produces: `RankingPolicy(strategy="relevance", affinity_weight=0.0, recency_weight=0.0,
  keyword_weight=0.0, entity_weight=0.0, recency_half_life_days=30.0, rerank_candidate_cap=12)`；
  `RetrievalLimits(candidate_pool=12, inject_return_limit=6, memory_search_default_k=5,
  memory_search_max_k=20)`；实例 `RANKING` / `LIMITS`。

- [x] **Step 1: 写失败测试** `backend/tests/test_ranking_config.py`
  断言 `params.RANKING.strategy == "relevance"` 且四个奖励权重为 0；`params.LIMITS` 三个数量
  各自独立；`RankingPolicy(strategy="relevance", recency_weight=0.3)` 抛 `ValueError`
  （纯相关性模式下奖励权重不得非零 —— 不留「表面可调、实际无效」的配置）。
- [x] **Step 2: 跑测试确认失败**（`RankingPolicy` 尚不存在）。
- [x] **Step 3: 实现**
- [x] **Step 4: 跑测试确认通过**

### Task 2: 候选阶段只按原始相关度（Selector）

**Files:**
- Modify: `backend/src/agent/selector/base.py`、`backend/src/agent/selector/selector.py`
- Delete: `backend/src/agent/selector/rules.py`
- Test: `backend/tests/test_selector.py`、`backend/tests/test_selector_incremental.py`

**Interfaces:**
- Produces: `Selector.select(query, *, candidate_pool: int = 12) -> list[MemoryCandidate]`；
  `MemoryCandidate(doc_id, relevance, sources, topic_id, title, token_estimate, created_at,
  keywords, entity_ids)`（`score` → `relevance`，删除 `merge_score`）。

- [x] **Step 1: 写失败测试**：同样的文本、不同的 `created_at`/话题/实体，`Selector.select`
  返回 **相同顺序相同分数**（奖励不再偷偷改变分数）；`recall.search` 收到的 `top_k`
  等于 `candidate_pool`（无隐藏 ×3）。
- [x] **Step 2: 跑测试确认失败**
- [x] **Step 3: 实现**：`select` 只做「召回 → 合并同 doc 多来源 → 按 `(-relevance, doc_id)` 排序 →
  截 `candidate_pool`」；无后端的降级路径用词项重合度当相关分（仍是检索相关度，不是业务奖励）。
- [x] **Step 4: 更新受影响的既有测试**（`test_selector.py` 里 3 条断言旧奖励的用例改写为
  新契约；`test_selector_incremental.py` 去掉 `now=`）。
- [x] **Step 5: 跑测试确认通过**

### Task 3: 唯一业务排序入口（ranking.py）

**Files:**
- Create: `backend/src/agent/services/ranking.py`
- Test: `backend/tests/test_ranking.py`

**Interfaces:**
- Produces: `RankingContext(query, anchor_topic_id=None, entity_names=(), fingerprint_scores={},
  now=None)`、`RankedCandidate(doc_id, relevance, factors, score, rank, ...)`、
  `rank(candidates, *, ctx, policy, decay=None, kind_of=None, rerank=None) -> list[RankedCandidate]`。
  纯相关性模式下 `score == relevance`、`factors == {}`；同分按 `doc_id` 稳定。

- [x] **Step 1: 写失败测试**：默认策略下排序与 `relevance` 完全一致；`recency_weight>0` 时报
  `ValueError`（只能显式用 `strategy="weighted"`）；`weighted` 模式下 `factors` 如实记录贡献；
  传入的 `rerank` 只在入口内执行一次、不会被后续公式覆盖。
- [x] **Step 2: 跑测试确认失败**
- [x] **Step 3: 实现**
- [x] **Step 4: 跑测试确认通过**

### Task 4: Retriever 只做编排 + 一次排序

**Files:**
- Modify: `backend/src/agent/services/retrieval.py`（删除 `RetrievalConfig`，改吃
  `policy` / `limits`；`RetrievalHit` 增加 `relevance` / `factors` / `rank` / `strategy`）
- Modify: `backend/src/agent/services/__init__.py`

**Interfaces:**
- Consumes: Task 1 的 `RANKING` / `LIMITS`、Task 3 的 `rank()`。
- Produces: `Retriever(selector, topics, *, policy=None, limits=None, conn=None, decay=None)`；
  `Retriever.search(query, *, anchor_topic_id=None, entity_names=None, top_k=None, now=None)`。

- [x] **Step 1: 写失败测试**：候选池 = `max(LIMITS.candidate_pool, top_k)`；底层召回请求数
  = 候选池（无隐藏倍增）；显式 `candidate_pool=30` 不会请求 180 条；实体卡与普通记忆的
  跨类型合并保持隔离并有单独来源标记。
- [x] **Step 2: 跑测试确认失败**
- [x] **Step 3: 实现**
- [x] **Step 4: 跑测试确认通过**（含改写 `tests/test_decay.py` 排序层用例为纯相关性契约）

### Task 5: 注入层记录原因 + 数量参数集中

**Files:**
- Modify: `backend/src/agent/services/injection.py`、`backend/src/agent/services/context.py`、
  `backend/src/agent/tools/memory_search.py`、`backend/src/agent/services/turn_orchestrator.py`、
  `backend/src/agent/trace/model.py`

**Interfaces:**
- `InjectionAssembler.build(..., top_k=None, entity_names=None)`；`InjectionPlan.dropped`
  记录 `{item_id, reason}`（`dedupe` / `below_min_score` / `budget`）；`InjectionPlan.ranking`
  记录生效配置；Trace `InjectionItem` 增加 `relevance` / `factors` / `rank` / `strategy`。

- [x] **Step 1: 写失败测试**：自动注入与主动检索都走同一排序语义；`dropped` 能区分
  「重复删除」与「预算不足」；集中配置改动真的影响生产调用路径（不是只改一个没人读的常量）。
- [x] **Step 2: 跑测试确认失败**
- [x] **Step 3: 实现**
- [x] **Step 4: 跑测试确认通过**

### Task 6: 新旧排序最小行为对照（eval）

**Files:**
- Create: `backend/src/agent/eval/ranking_eval.py`
- Test: `backend/tests/test_ranking_eval.py`
- Modify: `backend/src/agent/eval/retrieval_eval.py`、`anchor_eval.py`、`stress_arms.py`

- [x] **Step 1: 写失败测试**：`legacy_rank()`（旧双层，评测参考）与
  `relevance_rank()`（新默认）在同一份候选成员上给出可对照的排序与
  `old→new` 翻转条数；同一底层召回上限、同一最终预算的整链路对照；
  多答案是「命中任一」还是「覆盖全部」在记录里写明。
- [x] **Step 2: 跑测试确认失败**
- [x] **Step 3: 实现**
- [x] **Step 4: 跑 `python -m agent.eval.ranking_eval` 产出对照记录**

### Task 7: 文档与全量验证

- [x] `docs/status.md`（M9 段 + 已知限制）、必要的结构/配置说明同步。
- [x] `cd backend; uv run --frozen pytest` 全绿。
- [x] `uv run --frozen python -m agent.eval.run` 对比基线；必要时重新记录基线并说明原因。
- [x] `python scripts/check_docs.py`。
