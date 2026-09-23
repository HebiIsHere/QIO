# 首次引导 v2 实现计划（收集、落点与结束语义）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把首次引导从"填完就写、写完用不上"改成"一次核对、一次写入、写进去就真的会生效，并且能结束"。

**Architecture:** 引导在前端本地暂存，最后一步核对清单确认后一次性提交；后端新增一次性的引导提交接口，按"用户填写 = 直接生效 / 模型推测 = 待确认"落库；知识与话题各自增加"已结束"的表达与降权；自我信息统一到一个固定锚点，改名时迁移而不是新建。

**Tech Stack:** FastAPI + SQLite（pytest / TestClient）、Vue 3 + TypeScript + Pinia（vitest + vue-tsc）、现有设计令牌与 `QInput` / `QSelect`。

**Spec:** `docs/superpowers/specs/2026-08-18-onboarding-design.md`（v1 六步）+ 本次对话确认的 v2 决策（见下）。

## 决策记录（本轮已确认，来自对话）

1. **范围**：只做"引导本身做对"＋知识看得懂、阶段性信息会失效、自我信息稳定；**不做**"目标不建空话题"（目标仍会建话题）。
2. **生效方式**：先看核对清单，再写入；**用户自己填的**（称呼/背景/偏好/目标）确认后直接生效；**模型推测的**先记为"待确认"，不确认就不参与回答。
3. **写入时机**：走完所有步骤 → 清单核对 → 一次性写入；**中途关闭 = 当没填过**，重新打开也是空白（"本版本已展示过欢迎页"这条记录除外，它仍在打开时立刻写下）。
4. **阶段性信息**：由你说了算——你表示结束后，QIO 先标出来等你点头；**不设自动过期**；结束后仍然保留，但**权重降低**。
5. **改名**：信息整体跟着走；旧称呼记成"曾用名"；以前从对话里学到的称呼一并并进来；**始终只有一份"你"**。
6. **追问**：要有，按你的自由描述**现问**（模型生成）；可跳过；跳过**不出现在清单**；回到那一页补答后，结果出现在清单里。
7. **字段**：四项偏好（详略/语气/解释方式/协作方式）都问，每项可单独指定只在某个话题生效；"需要了解的限制"拆成**不要做什么**与**怎么表达**两类分别问；目标改为自由填写。
8. **必填**：新用户必须有**可用密钥**才能离开"连接模型"这一步；主页**已有内容的老用户**可以整场关闭引导；其余字段全部可跳过（含目标整步为空）。
9. **清单页**：能逐条改、逐条删；删掉 = 不写进去、不留痕。
10. **结束之后**：目标同时是"话题"和"目标知识"，结束要同时作用于两者；已结束话题**不出现在星球主视图**，只在单独的"已结束"分组或搜索里可见，QIO 的记忆检索仍能搜到；结束入口**两种都要**（对话中 QIO 标出等你点头 + 话题页手动标记）。
11. **版本弹窗**：只发生在"从低于 0.1.7 的版本升到 0.1.7 或更高"这一**一次性跨越**；此后 0.1.8、0.2.0 都不再弹。

## 我替你定的派生决定（评审时请确认，不同意直接改）

- **步骤结构**：欢迎 / 连接模型 / 认识你 / 偏好 / 目标 / 追问 / 核对并完成，共 7 步。原来的"完成"总结页被"核对清单"取代（清单本身就是总结）。
- **"主页有内容"的判据**：本地已存在**任一对话消息**（新装或从未聊过 = 没有内容）。
- **已结束知识的使用方式**：不再常驻注入；只有当本轮内容与它相关（关键词命中）时才作为参考出现，并排在当前信息之后。
- **已结束话题的位置**：星球主视图不显示；话题面板里单列"已结束"分组；搜索与记忆检索不受影响。
- **知识看得懂**：知识条目显示来源、适用范围、时间，并支持按来源筛选（数据已有，只是没展示）。
- **版本弹窗的实现**：设置里记住"已经为这次跨越弹过一次"，而不是"每个版本都弹"。

## Global Constraints

- 用户可见文案全部中文；沿用现有语气与设计令牌，不新增依赖。
- 一次引导只写一次：核对清单确认前**不产生任何持久化内容**。
- 落库幂等：重复走引导不产生重复知识 / 重复实体卡 / 重复话题。
- 用户填写优先于模型推断；模型推断必须先"待确认"。
- 不为了保存资料而伪造聊天记录或片段。
- 前端验证：`npx vue-tsc --noEmit`、`npx vitest run`（在 `frontend/`）；后端验证：`python -m pytest tests -q`（在 `backend/`，用 `backend/.venv`）。
- 不执行 `git push`；每个 Task 结束提交一次。

---

### Task 1: 知识的"来源 / 范围 / 时间"可见 + "已结束"降权

**Files:**
- Modify: `backend/src/agent/knowledge/lifecycle.py`（结束标记的读写；不改状态机语义）
- Modify: `backend/src/agent/api/server.py`（知识载荷补字段；新增结束/恢复路由）
- Modify: `backend/src/agent/services/injection.py`（已结束条目的降权与条件注入）
- Test: `backend/tests/test_knowledge_lifecycle_marker.py`、`backend/tests/test_injection_ended_weight.py`

**Interfaces:**
- Produces:
  - `KnowledgeService.mark_ended(knowledge_id, *, reason) -> KnowledgeItem` / `resume(knowledge_id) -> KnowledgeItem`
  - 知识载荷新增：`source`（引导 / 对话 / 用户修正 / 维护）、`scope`（全局 / 话题名 / 实体名）、`ended`（是否已结束）、`ended_at`
  - 注入评分：已结束条目 `score = 0.5 * overlap`（无关键词命中则不进注入）；未结束条目维持现有 `surface_base + 0.5 * overlap`
- Consumes: 现有 `provenance`（JSON）、`node_ids`、`created_at`、`InjectionSource.list_active_for_node`

- [ ] **Step 1:** 写失败测试：结束一条知识后，载荷里 `ended=true`、`ended_at` 非空、`source`/`scope` 正确；注入计划里它不再常驻，只有关键词命中时才出现且分数低于同类未结束条目。
- [ ] **Step 2:** 跑测试确认失败（`python -m pytest tests/test_knowledge_lifecycle_marker.py tests/test_injection_ended_weight.py -q`）。
- [ ] **Step 3:** 实现标记读写与注入降权（标记写在 `provenance` 的 `ended_at` / `ended_reason`，不改状态机；沿用 `provenance` 已有的 `ignored_at` 写法）。
- [ ] **Step 4:** 跑测试确认通过 + 后端全量回归。
- [ ] **Step 5:** 提交 `feat(knowledge): 来源/范围/时间可见 + 已结束降权`。

---

### Task 2: 话题的"已结束"表达与分组数据

**Files:**
- Modify: `backend/src/agent/graph/nodes.py`（`meta` 上记录结束；沿用合并时写 `merged_into` 的既有做法）
- Modify: `backend/src/agent/api/server.py`（`/api/planet/overview` 与话题详情返回 `ended`；新增结束/恢复路由）
- Modify: `backend/src/agent/services/planet.py`、`backend/src/agent/services/retrieval.py`（主视图与联想起因排除已结束话题；记忆检索不受影响）
- Test: `backend/tests/test_topic_ended.py`

**Interfaces:**
- Produces: `NodeService.mark_topic_ended(node_id)` / `resume_topic(node_id)`；话题载荷新增 `ended` / `ended_at`；星球主视图与 `aux_topic` 联想排除已结束话题；`/api/planet/overview` 增加 `ended_topics`（单独的"已结束"分组数据）
- Consumes: `nodes.meta`（JSON，无需改表结构）

- [ ] **Step 1:** 写失败测试：结束话题后它不出现在主视图候选里、出现在 `ended_topics` 里、记忆检索（`Retriever.search`）仍能命中它的片段。
- [ ] **Step 2:** 跑测试确认失败。
- [ ] **Step 3:** 实现结束标记与分组/排除逻辑。
- [ ] **Step 4:** 跑测试确认通过 + 后端全量回归。
- [ ] **Step 5:** 提交 `feat(topics): 话题结束语义与已结束分组数据`。

---

### Task 3: 引导一次性提交（落点、自我锚点、待确认）

**Files:**
- Modify: `backend/src/agent/services/onboarding.py`（新增提交入口；字段扩展；自我锚点）
- Modify: `backend/src/agent/api/server.py`（`POST /api/onboarding/submit`；`GET /api/onboarding/status` 增加 `has_content`）
- Modify: `backend/src/agent/entities/cards.py`（自我卡按固定锚点定位；改名时迁移 + 曾用名 + 合并）
- Test: `backend/tests/test_onboarding_submit.py`、`backend/tests/test_self_card_rename.py`

**Interfaces:**
- Produces:
  - `POST /api/onboarding/submit` body：
    ```
    {
      "name": "祠莎",
      "background": "学生",              // 学习/工作背景（可空）
      "current_focus": "开发 QIO",        // 最近主要在做什么（可空）
      "current_focus_ended": false,
      "interests": ["agent 记忆"],        // 长期关注（可空）
      "familiarity": "刚入门",            // 熟悉程度（可空）
      "limits": {"dont_do": "...", "how_to_talk": "..."},  // 两类限制（可空）
      "preferences": [{"kind": "verbosity|tone|explanation|collaboration",
                       "value": "简洁", "scope": {"type": "global"| "topic", "topic_title": "..."}}],
      "goals": ["把 QIO 的记忆问题做完"],
      "inferred": [{"content": "...", "category": "user_profile|goal", "reason": "..."}]
    }
    ```
  - 返回：`{"written": [...], "pending": [...], "topics": [...], "self_card_id": "..."}`
  - 自我锚点：`EntityCardService.find_self_card()` / `ensure_self_card(name)`，名字变化时把属性与关系迁到同一张卡，旧名字进 `aliases`
- Consumes: `NodeService.get_or_create_user_root()`、`KnowledgeService`（`verified_by="user"`）、Task 1/2 的结束标记

- [ ] **Step 1:** 写失败测试：
  - 提交后：`user_profile` 知识**挂在"你"这个锚点上**（不再是空挂载）；偏好按 `scope` 分别挂到"你"或话题；目标同时产生一条 `goal` 知识与一个同名话题。
  - `inferred` 列表写进去后状态是"待确认"，且不参与注入；`written` 列表直接"生效"。
  - 改名：属性跟着走、旧名进 `aliases`、**不新增实体卡**；若已有从对话里学到的别的称呼卡，一并合并。
  - 幂等：连续提交两次不产生重复。
- [ ] **Step 2:** 跑测试确认失败。
- [ ] **Step 3:** 实现提交入口、字段落点、自我锚点与合并。
- [ ] **Step 4:** 跑测试确认通过 + 后端全量回归。
- [ ] **Step 5:** 提交 `feat(onboarding): 一次性提交与自我锚点`。

---

### Task 4: 版本弹窗规则改为"一次性跨越"

**Files:**
- Modify: `backend/src/agent/services/onboarding.py`、`backend/src/agent/api/server.py`
- Test: `backend/tests/test_onboarding_version_gate.py`（改造既有 `test_onboarding_api.py` 对应用例）

**Interfaces:**
- Produces: `MIGRATION_VERSION = "0.1.7"`；`show_wizard = (not wizard_seen) or (welcome_version < MIGRATION_VERSION <= app_version and not migration_shown)`；`mark_seen()` 同时写 `onboarding.migration_shown=true`

- [ ] **Step 1:** 写失败测试：`0.1.5 → 0.1.7` 弹一次、`0.1.7 → 0.1.8` **不弹**、`0.1.6 → 0.1.9` 弹一次（因为历史低于 0.1.7）、全新安装仍弹。
- [ ] **Step 2:** 跑测试确认失败。
- [ ] **Step 3:** 实现规则并与 `mark_seen` 对齐。
- [ ] **Step 4:** 跑测试确认通过 + 后端全量回归。
- [ ] **Step 5:** 提交 `fix(onboarding): 版本更新弹窗改为一次性跨越 0.1.7`。

---

### Task 5: 前端向导改造（暂存、不可跳过、追问、清单）

**Files:**
- Modify: `frontend/src/components/onboarding/OnboardingWizard.vue`、`frontend/src/components/onboarding/steps.ts`
- Create: `frontend/src/components/onboarding/OnboardingReview.vue`、`frontend/src/components/onboarding/OnboardingFollowUp.vue`
- Modify: `frontend/src/stores/onboarding.ts`、`frontend/src/services/api.ts`
- Test: `frontend/src/components/onboarding/__tests__/OnboardingWizard.v2.test.ts`、`frontend/src/components/onboarding/__tests__/OnboardingReview.test.ts`

**Interfaces:**
- Consumes: `POST /api/onboarding/submit`（Task 3）、`identifyCredential`、`api.getOnboardingStatus()` 的 `has_content`
- Produces: 7 步向导；草稿只存在组件状态里（关闭即丢，重开空白）；`OnboardingReview` 逐条改/删后再提交；`OnboardingFollowUp` 调 `api.suggestFollowUps({description})`（新增端点，模型按描述现问，失败/无密钥则不出现）

- [ ] **Step 1:** 写失败测试：
  - 新用户（`has_content=false`）在"连接模型"步**不能离开**，除非密钥可用；老用户（`has_content=true`）可以整场关闭；
  - 关闭再打开 → 草稿为空；
  - 跳过追问 → 清单里**没有**追问项；回到追问页补答 → 清单里出现该项；
  - 清单里删掉一条 → 提交请求体里没有它；
  - 清单里改一条 → 提交请求体里是新值。
- [ ] **Step 2:** 跑测试确认失败。
- [ ] **Step 3:** 实现向导、追问页与清单页（含 `suggestFollowUps` 后端端点）。
- [ ] **Step 4:** 跑测试确认通过 + `npx vue-tsc --noEmit` + 前端全量。
- [ ] **Step 5:** 提交 `feat(onboarding): 七步向导、追问与核对清单`。

---

### Task 6: 知识页与话题页的"看得懂"和"已结束"

**Files:**
- Modify: `frontend/src/components/planet/KnowledgePanel.vue`（显示来源/范围/时间 + 来源筛选 + "标记结束/恢复"）
- Modify: `frontend/src/views/PlanetView.vue`（话题面板里的"已结束"分组 + 手动标记结束）
- Modify: `frontend/src/services/api.ts`
- Test: `frontend/src/components/planet/__tests__/KnowledgePanel.visibility.test.ts`、`frontend/src/views/__tests__/PlanetEndedGroup.test.ts`

- [ ] **Step 1:** 写失败测试：知识条显示来源/范围/时间；按来源筛选生效；标记结束后列表显示"已结束"；星球主视图不出现已结束话题、`已结束` 分组里出现。
- [ ] **Step 2:** 跑测试确认失败。
- [ ] **Step 3:** 实现两处界面与接口方法。
- [ ] **Step 4:** 跑测试确认通过 + 类型检查 + 前端全量。
- [ ] **Step 5:** 提交 `feat(planet): 知识来源可见与已结束分组`。

---

### Task 7: 预览与验收（控制器执行）

**Files:**
- Modify: `scripts/ui-catalog/onboarding-preview.mjs`（补：新用户不可跳过、老用户可关闭、追问、清单、已结束分组）

- [ ] **Step 1:** 干净数据域起服务，跑七步向导，逐页截图（含追问页与核对清单）。
- [ ] **Step 2:** 验证关键结果：提交后画像知识挂在"你"这个锚点上并能进入注入；改名不新增卡；话题结束后主视图消失、分组出现、搜索仍能找到。
- [ ] **Step 3:** 把截图与验证命令、结果整理给你。

## Self-Review

- 决策 1–11 都有对应任务：1（范围）→ Task 1/2/3；2、3（生效与写入时机）→ Task 3/5；4、10（结束与呈现）→ Task 1/2/6；5（改名）→ Task 3；6（追问）→ Task 5；7、8、9（字段、必填、清单）→ Task 5；11（版本）→ Task 4。
- 未采纳项（目标不建空话题）没有进入任何任务，符合范围。
- 派生决定单独列出，供你在评审时确认或推翻。
