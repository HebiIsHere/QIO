# 六项工作三方对照实验 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在同一份构造语料上，用 A 规则+BM25 / B 本地 embedding / C 云端 Jev 三条臂跑完 qio 的六项判定工作，产出可对照的准确率与代价数字。

**Architecture:** 语料生成器（确定性、离线）负责造记忆/话题/实体/工具/查询与标注；三条臂各自封装成"接收同一个 `StressCorpus`、返回逐条判定结果"的适配器；跑批器只做调度与指标汇总，不关心臂内部实现。C 臂通过 OpenRouter 的 Decisions 接口调用 Jev，全部走同一个薄客户端。

**Tech Stack:** Python 3.11、pytest、numpy、httpx（均已装）；不新增依赖。

**Spec:** `docs/superpowers/specs/2026-09-23-six-jobs-benchmark-spec.md`

## Global Constraints

- 不新增第三方依赖；只用 pyproject 里已有的包。
- 不联网、不调用大模型的测试必须占绝大多数；C 臂的真实调用**不得**出现在 `pytest` 用例里。
- 密钥只从 `[Environment]::GetEnvironmentVariable('OPENROUTER_API_KEY','User')` 或 `OPENROUTER_API_KEY` 环境变量读取；任何输出、日志、异常消息都不得包含密钥原文。
- 不改生产链路、不改既有 `baseline.json` / `baseline_onnx.json`，不扩锚点延续评测。
- **本次不执行 git 写操作**（仓库有并行工作）；每个任务以"跑测试全绿"收尾。
- 语料与结果文件写在 `backend/evals/stress/` 与 `backend/evals/stress_results*.json`。
- 所有新增中文注释与文档使用中文；标识符用英文。

---

### Task 1: 语料生成器

**Files:**
- Create: `backend/src/agent/eval/stress_corpus.py`
- Test: `backend/tests/test_stress_corpus.py`

**Interfaces:**
- Produces: `generate(seed: int = 20260923, n_memories: int = 10_000, n_topics: int = 200, n_queries: int = 1_000, n_entities: int = 200, n_tools: int = 60) -> StressCorpus`
- Produces dataclasses: `Memory(id, text, topic_id, kind, age_days, entity_ids)`、`Topic(id, title, keywords)`、`EntityCard(id, name, aliases, summary)`、`ToolSpecLite(name, description)`、`RecallCase(id, category, query, expected, stale, keyword_answerable)`、`TopicCase(id, category, message, current_topic_id, expected_topic_id, expected_mode)`、`EntityCase(id, query, expected_card_id)`、`ToolCase(id, query, expected_tool)`、`DedupCase(id, candidate_name, expected_duplicate, duplicate_of)`、`StressCorpus(...)` 含上述各列表。
- 六个召回类别常量：`CATEGORIES = ("fact_update", "same_topic", "entity_ref", "cross_topic", "paraphrase", "recent_noise")`

- [ ] **Step 1: 写失败测试**（`tests/test_stress_corpus.py`）

```python
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

def test_keyword_answerable_flag_matches_keyword_overlap():
    # 标记必须由字面重叠算出来，不能手写：标注与 A 臂实际行为一致才可信
    c = generate(seed=7, n_memories=200, n_topics=10, n_queries=40)
    by_id = {m.id: m for m in c.memories}
    for case in c.recall_cases:
        tokens = set(tokenize(case.query))
        overlap = any(tokens & set(tokenize(by_id[m].text)) for m in case.expected)
        assert case.keyword_answerable == overlap
```

- [ ] **Step 2: 跑测试确认失败**：`cd backend; .venv\Scripts\python.exe -m pytest tests/test_stress_corpus.py -q` → ImportError
- [ ] **Step 3: 实现生成器**：话题词表 = 领域词 × 子领域词（如 `数据库` + `迁移/索引/备份`）；记忆文本按 `kind` 套模板并填槽；实体卡含 name + alias；工具 60 条手写名称与描述；查询按类别变换（`fact_update` 用话题词组合提问、`same_topic` 用同话题另一条记忆的措辞提问、`entity_ref` 用实体名/别名、`cross_topic` 用"之前聊过的{topic_title}"式引用、`paraphrase` 走同义词表替换、`recent_noise` 用无关近因片段做干扰）。`keyword_answerable` 必须由 `tokenize` 的重叠实算。
- [ ] **Step 4: 跑测试确认通过**：同上命令 → 全绿

### Task 2: 指标与结果结构

**Files:**
- Create: `backend/src/agent/eval/stress_metrics.py`
- Test: `backend/tests/test_stress_metrics.py`

**Interfaces:**
- Consumes: Task 1 的 dataclass
- Produces: `recall_metrics(ranked: list[list[str]], cases: list[RecallCase], k: int = 5) -> dict`（`recall@1`、`recall@5`、`mrr`、`wrong_memory_injection_rate`、`stale_knowledge_injection_rate`）、`topic_metrics(rows) -> dict`、`entity_metrics(rows) -> dict`、`tool_metrics(rows) -> dict`、`dedup_metrics(rows) -> dict`、`rerank_delta(before, after) -> dict`、`stratify(cases, metrics_fn) -> dict`（按类别 + 按 `keyword_answerable` 分层）

- [ ] **Step 1: 写失败测试**：用 3 条手工构造的排名结果断言 `recall@1 == 0.6667`、`mrr`、陈旧注入率；断言分层函数把 `keyword_answerable=False` 的子集单独算出来。
- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现指标**（纯函数，输入排名/判定，输出 dict；`rate = round(num/den, 4)`）
- [ ] **Step 4: 跑测试确认通过**

### Task 3: Jev 客户端（OpenRouter Decisions）

**Files:**
- Create: `backend/src/agent/eval/jev_client.py`
- Test: `backend/tests/test_jev_client.py`

**Interfaces:**
- Produces: `JevClient(api_key: str | None = None, model: str = "typesafe/jev-1.13", base_url: str = "https://openrouter.ai/api/alpha/decisions", timeout: float = 30.0, http_client=None)`；`ask(state, questions, *, model=None) -> JevAnswer`；`JevAnswer(answers: dict, input_tokens: int, cost_usd: float, latency_ms: float)`；`JevUnavailable(Exception)`；`mask(secret) -> str`
- 读取顺序：显式参数 → 进程环境变量 → Windows 注册表 User 作用域（`winreg`，失败返回空）

- [ ] **Step 1: 写失败测试**（全部用假的 httpx transport，不发真实请求）

```python
def test_ask_sends_state_questions_and_parses_answers():
    captured = {}
    def handler(request):
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"answers": {"x": {"type": "noul", "noul": 0.9}},
                                         "usage": {"input_tokens": 12, "cost": 0.000001}})
    client = JevClient(api_key="sk-or-v1-TEST", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    ans = client.ask({"message": "hi"}, {"x": {"type": "noul", "instructions": "?"}})
    assert captured["body"]["model"] == "typesafe/jev-1.13"
    assert ans.answers["x"]["noul"] == 0.9
    assert ans.input_tokens == 12

def test_missing_key_raises_without_leaking_anything(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(jev_client, "_registry_key", lambda: "")
    with pytest.raises(JevUnavailable):
        JevClient().ask({"m": 1}, {})

def test_http_error_message_is_masked():
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "bad key sk-or-v1-SECRET"}})
    client = JevClient(api_key="sk-or-v1-SECRET", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(JevUnavailable) as exc:
        client.ask({"m": 1}, {})
    assert "SECRET" not in str(exc.value)
```

- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现客户端**：POST JSON（UTF-8），Bearer 头；解析 `answers` / `usage.input_tokens` / `usage.cost`；累计 `requests`、`input_tokens`、`cost_usd`；错误一律转成 `JevUnavailable` 且消息经过掩码。
- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: 真实小样验证（手动，不进 pytest）**：20 条查询跑通，记录并发延迟与单价

### Task 4: 三条臂的适配器

**Files:**
- Create: `backend/src/agent/eval/stress_arms.py`
- Test: `backend/tests/test_stress_arms.py`

**Interfaces:**
- Consumes: Task 1 语料、Task 3 客户端、既有生产的 `Selector` / `Retriever` / `TopicPredictor` / `ToolRouter` / `CreateTopicTool` / `OnnxEmbeddingBackend`
- Produces: 每个臂一个类，统一方法签名 `recall(corpus, cases) -> list[list[str]]`、`topic(corpus, cases) -> list[dict]`、`entity(corpus, cases) -> list[dict]`、`tool(corpus, cases) -> list[dict]`、`dedup(corpus, cases) -> list[dict]`、`rerank(corpus, cases, base_ranked) -> list[list[str]]`：
  - `RulesArm()`（A）
  - `LocalEmbeddingArm(model_dir=None)`（B）
  - `JevArm(client)`（C，`recall` 直接 `raise NotImplementedError`)

- [ ] **Step 1: 写失败测试**：用 3 条记忆的小语料断言 A 臂 `recall` 返回的排名里包含期望 id；用假 embedding 断言 B 臂话题判定走的是向量路径（`backend_used == "onnx"`）；断言 C 臂 `recall` 抛 `NotImplementedError`；断言 C 臂 `dedup` 在假客户端返回 0.9 时判为重复、返回 0.1 时判为不重复。
- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现三个臂**（A 臂全部用生产默认路径；B 臂每用例新建 `OnnxEmbeddingBackend`，与 `embedding_backend.build_backend` 一致；C 臂把业务问题翻成 choice/noul 问题）
- [ ] **Step 4: 跑测试确认通过**

### Task 5: 跑批器与 CLI

**Files:**
- Create: `backend/src/agent/eval/stress_run.py`
- Test: `backend/tests/test_stress_run.py`

**Interfaces:**
- Consumes: Task 1–4
- Produces: `run_corpus(corpus, arms, k=5) -> dict`（每臂每工作的指标 + 分层 + 延迟分位）；CLI `python -m agent.eval.stress_run --arm rules|local|jev --n-memories 10000 --n-queries 1000 --out backend/evals/stress_results.json`

- [ ] **Step 1: 写失败测试**：用 20 条规模的小语料 + 假臂断言输出结构（含 `arms`、`per_job`、`stratified`、`cost` 键），且 `--arm jev` 在无 key 时报错退出而不是静默按规则跑。
- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现跑批器与 CLI**（A/B 两臂可在同一进程内跑；C 臂用线程池并发，默认 8 路，遇到 `JevUnavailable` 累计失败数而不是中断）
- [ ] **Step 4: 跑测试确认通过**

### Task 6: 全量运行与结论

- [ ] **Step 1:** 生成并落盘语料（10,000 / 200 / 1,000）：`python -m agent.eval.stress_run --dump-corpus`
- [ ] **Step 2:** 跑 A 臂 + B 臂（本机、零成本），确认耗时与内存
- [ ] **Step 3:** 跑 C 臂小样（50 条）核对成本与延迟，再跑全量
- [ ] **Step 4:** 写结论记录 `docs/superpowers/notes/2026-09-23-six-jobs-results.md`，含两问的直接回答、分层表、代价表、边界说明
- [ ] **Step 5:** 更新 `docs/status.md` 的对应条目；跑 `pytest` 与 `python scripts/check_docs.py`
