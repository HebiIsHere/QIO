# Fragment 重设计实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: 用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务执行；步骤用 `- [ ]` 勾选。

**Goal:** 把「一轮对话属于哪个 Topic / 哪个 Fragment」固定下来，让历史接续、队列顺序、封存与派生任务、历史路径上下文都按这份绑定工作，并把这些行为做到可验证。

**Architecture:** 轮次在**提交时**捕获导航意图、在**开始执行时**落实为一个持久绑定（`turn_bindings` 一行一轮）；Fragment 之间的来源关系用 `source_fragment_id` + 新增的关系字段表达；封存与摘要/索引等派生工作拆成「写入结束」与「轮次终态」两件事，派生工作落一张任务表并支持重试；上下文按本轮绑定的路径组织，不再无条件取「最近两段摘要」。

**Tech Stack:** Python 3.10 / FastAPI / SQLite（手写有序迁移）/ pytest；前端 Vue 3 + Pinia + Vitest；界面检查用 `scripts/ui-catalog/*.mjs`（Playwright）。

**Spec:** `docs/superpowers/specs/2026-09-21-fragment-redesign-spec.md`（本轮任务书全文）

## Global Constraints

- 每个 Topic 最多一个开放 Fragment；保留迁移 12 的 `idx_fragments_one_open_per_topic`。
- 同一 Turn 固定绑定一个 Topic 与 Fragment；绑定后不得因全局 Anchor 变化而搬动该轮消息。
- 浏览 / 展开历史 / 检索记忆 / 话题预测一律只读，不改写 Anchor，也不创建 Fragment。
- 封存后不追加、不搬动原始消息；摘要、索引、向量都是可重试的派生数据。
- 只追加新迁移，禁止改历史迁移（当前最高迁移号 12，新迁移从 13 开始）。
- 事务内不得 await 模型或网络调用；不得只捕获唯一约束错误后返回「成功」。
- 不得借本任务扩大工具授权；不自动发布、不自动合并。
- 验证门禁：`cd backend; uv run --frozen pytest`；前端 `npx vue-tsc --noEmit` + `npm test`；文档 `python scripts/check_docs.py`。

---

## 阶段 1：固定轮次归属 + 安全落实历史接续

### Task 1.1 迁移 13：绑定与接续意图两张表

**Files:**
- Modify: `backend/src/agent/storage/schema.py`（追加迁移 13）
- Test: `backend/tests/test_migration_13_bindings.py`

**Interfaces（Produces）:**
- `turn_bindings(turn_id TEXT PRIMARY KEY, topic_id TEXT NOT NULL, fragment_id TEXT, intent_id TEXT, intent_version INTEGER, write_state TEXT NOT NULL DEFAULT 'open', status TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)`
- `continuation_intents(intent_id TEXT PRIMARY KEY, topic_id TEXT NOT NULL, source_fragment_id TEXT, version INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'registered', resolved_fragment_id TEXT, request_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)`
- 索引：`idx_turn_bindings_topic(topic_id)`、`idx_continuation_intents_state(state)`

- [ ] 写失败测试：打开一个只有迁移 12 的旧库 → 应用迁移 → 版本=13、两张表存在、旧数据行数与内容不变。
- [ ] 跑测试确认失败（表不存在）。
- [ ] 追加迁移 13（只 `CREATE TABLE IF NOT EXISTS` + 索引，不重建既有表）。
- [ ] 跑测试确认通过。

### Task 1.2 绑定服务

**Files:**
- Create: `backend/src/agent/services/binding.py`
- Test: `backend/tests/test_turn_binding.py`

**Interfaces（Produces）:**
- `class TurnBindingService:`
  - `record_binding(turn_id, topic_id, fragment_id, *, intent_id=None, intent_version=None) -> dict`（幂等：同 turn_id 重复写同名值不报错，不同值抛 `BindingConflict`）
  - `binding_for(turn_id) -> dict | None`
  - `mark_write_closed(turn_id) -> None`
  - `register_intent(topic_id, source_fragment_id, *, request_id=None) -> dict`（覆盖式：同一用户「改选」时新建版本）
  - `peek_intent() -> dict | None`、`bump_intent_version()`、`consume_intent(intent_id, fragment_id)`、`clear_intent()`

- [ ] 失败测试：记录绑定后可读回；同 turn 改值抛 `BindingConflict`；意图登记 → 抬版本 → 消费后状态与 `resolved_fragment_id` 正确；重复消费同 request_id 幂等。
- [ ] 实现服务（全部单条 SQL + 短事务，无 await）。
- [ ] 测试通过。

### Task 1.3 MemoryWriter 按显式绑定写入

**Files:**
- Modify: `backend/src/agent/memory/ingest.py`
- Test: `backend/tests/test_memory_writer_binding.py`

**Interfaces（Consumes）:** Task 1.2 的 `TurnBindingService`
**Produces:** `MemoryWriter.append_message(..., fragment_id: str | None = None, turn_id: str | None = None)`；传入 `fragment_id` 时校验「属于该 topic、仍开放、与 turn 绑定一致」，不一致抛 `BindingMismatch`；不传时保持旧行为（兼容系统通知路径）。

- [ ] 失败测试：绑定到 A 的轮次把消息写进 A（即使 A 不是该 topic 当前开放片段）；绑定与传入 fragment 不一致时抛错且不写消息。
- [ ] 实现。
- [ ] 测试通过 + 既有 ingest 测试不回归。

### Task 1.4 编排器：轮前定绑定，轮后不搬消息

**Files:**
- Modify: `backend/src/agent/services/turn_orchestrator.py`（`begin` / `persist` / `advance_anchor`）
- Modify: `backend/src/agent/services/app.py`（删除正常路径上的 `_move_message` 调用）
- Test: `backend/tests/test_turn_binding_orchestrator.py`

**Produces:**
- `begin()` 末尾：解析明确导航（`detect_explicit_navigation`）与接续意图 → 落实 topic/fragment → `bindings.record_binding(...)` → 再构建上下文。
- `persist()` 不再读 Anchor 决定 `final_topic`；一律写进 `ctx.bound_topic` / `ctx.bound_fragment`。
- `advance_anchor()` 加版本比较（旧轮次不得覆盖更新的导航）。

- [ ] 失败测试（复现规格里的验收行）：**甲先提交并被排队 → 用户改选从 A 继续 → 提交乙** → 甲仍写回原话题，乙写进 A 的接续片段。
- [ ] 失败测试：回答期间用户改选导航 → 当前轮仍写回 `ctx.bound_*`，不被搬走。
- [ ] 实现。
- [ ] 测试通过。

### Task 1.5 接续意图：点击历史只登记，执行时才落实

**Files:**
- Modify: `backend/src/agent/services/navigation.py`
- Modify: `backend/src/agent/api/server.py`（导航路由返回意图与版本）
- Modify: `backend/src/agent/tools/continue_tool.py`
- Test: `backend/tests/test_continue_intent.py`

**Produces:**
- `continue_from_history()` 拆成两步：
  - `register_continuation(topic_id, fragment_id) -> dict`（只登记意图，不建片段；返回 `intent_id/version/从…继续` 文案数据）
  - `apply_continuation(binding_service, topic_id, intent) -> NavigationResult`（在轮前落实：来源开放 → 直接进入；来源已封存且同话题已有开放片段 → **不再 INSERT 新开放片段**，把来源关系记到既有开放片段的延续记录上；只有话题没有开放片段时才新建）
- Agent 工具路径只登记意图，不影响当前轮绑定。

- [ ] 失败测试：**已关闭 A + 同话题已有开放 C，从 A 继续 → 无 UNIQUE 错误，C 的来源关系指向 A**（规格验收第一行）。
- [ ] 失败测试：选择当前开放片段 → 普通继续，不新建片段。
- [ ] 失败测试：只浏览 / 发送前改选 → 不产生任何片段。
- [ ] 失败测试：同 `request_id` 重发 → 同一落实结果，无重复片段。
- [ ] 实现。
- [ ] 测试通过。

### Task 1.6 写入入口清单

**Files:**
- Create: `docs/superpowers/notes/2026-09-21-fragment-redesign-write-paths.md`

**Produces:** 逐条列出所有写消息入口（普通回答、失败/取消、系统通知、子任务结果）以及各自如何使用固定绑定；导航事件与旧轮次收尾的覆盖规则。

- [ ] 用 `rg -n "append_message|memory.append" backend/src` 全量枚举，逐条记录。
- [ ] 与实现对照，缺失的入口补上绑定。

---

## 阶段 2：写入结束与轮次终态分开，派生任务可恢复

### Task 2.1 派生任务表 + 服务

**Files:**
- Modify: `backend/src/agent/storage/schema.py`（迁移 14：`derived_tasks`）
- Create: `backend/src/agent/services/derived_tasks.py`
- Test: `backend/tests/test_derived_tasks.py`

**Produces:** 表 `derived_tasks(id, kind, fragment_id, content_version, state, attempts, last_error, run_after, created_at, updated_at)`；服务 `enqueue(kind, fragment_id, content_version)`、`claim_due(limit)`、`complete(id)`、`fail(id, error)`（带退避）、`recover_stale()`；同 `(kind, fragment_id, content_version)` 幂等。

- [ ] 失败测试：入队幂等；claim 只取到期项；fail 后 `attempts+1` 且 `run_after` 后移；complete 后不再被 claim；进程重启后 `running` 超时项可被重新认领。
- [ ] 实现 + 测试通过。

### Task 2.2 封存与摘要解耦

**Files:**
- Modify: `backend/src/agent/services/memory_lifecycle.py`
- Modify: `backend/src/agent/memory/fragment.py`（`close()` 拆成 `seal()` + 派生登记）
- Test: `backend/tests/test_seal_then_summarize.py`

**Produces:** `seal(fragment_id, reason, content_version)` 只做封存 + 登记任务；摘要执行器从任务表取任务，落库时校验 `content_version`，失败不影响对话。

- [ ] 失败测试：摘要抛错时封存仍成功、原文可读、任务进 retryable failure。
- [ ] 失败测试：迟到的摘要结果（内容版本更旧）不得覆盖新内容。
- [ ] 实现 + 测试通过。

### Task 2.3 写入结束 vs 轮次终态

**Files:**
- Modify: `backend/src/agent/services/turn_orchestrator.py`、`backend/src/agent/core/turn.py`
- Test: `backend/tests/test_write_end_vs_turn_end.py`

- [ ] 失败测试：写入结束后即可释放写入占用（`turn_bindings.write_state='closed'`），且 TURN_END 仍然只由 TurnManager 发一次。
- [ ] 失败测试：取消 / 失败 / 异常三条出口都释放占用且终态正确。
- [ ] 实现 + 测试通过。

---

## 阶段 3：历史关系与路径上下文

### Task 3.1 Fragment 关系字段与数据访问

**Files:** `backend/src/agent/storage/schema.py`（迁移 15：`boundary_reason`、`relation_type`、`same_stage`、`content_version`）、`backend/src/agent/memory/fragment.py`、`backend/tests/test_fragment_relations.py`

- [ ] 失败测试：来源必须在同话题、无自指、无环；祖先读取有深度上限与循环保护。
- [ ] 实现 + 测试通过。

### Task 3.2 路径上下文

**Files:** `backend/src/agent/services/context.py`、`backend/src/agent/services/injection.py`、`backend/src/agent/knowledge/inject.py`、`backend/tests/test_path_context.py`

- [ ] 失败测试（A→B→C 与 A→D 的 fixture）：D 的上下文不含 B/C 的决定；非路径内容只作为明确标注的参考。
- [ ] 失败测试：同一片段既被路径继承又被检索命中时只注入一次且优先级明确。
- [ ] 实现 + 测试通过。

### Task 3.3 提示消费与来源分离

**Files:** `backend/src/agent/graph/anchors.py`、`backend/src/agent/services/app.py`、`backend/tests/test_continuation_hint.py`

- [ ] 失败测试：首轮失败/取消 → 提示保留；首轮成功 → 提示撤下、来源关系仍可查。
- [ ] 实现 + 测试通过。

### Task 3.4 旧数据迁移与兼容

**Files:** `backend/src/agent/storage/schema.py`（迁移 16）、`backend/tests/test_legacy_fragment_relations.py`

- [ ] 失败测试：迁移 12 关掉的片段不被当作真实结束时间；可确认来源保留，不可确认标 `unknown`。
- [ ] 实现 + 测试通过。

---

## 阶段 4：边界策略与容量兜底

### Task 4.1 FragmentBoundaryPolicy（纯判断）

**Files:** Create `backend/src/agent/memory/boundary.py`、`backend/tests/test_boundary_policy.py`

**Produces:** `decide(turn_input, recent_turns, stage_state, capacity) -> BoundaryDecision{action, reason, boundary_turn, evidence, policy_version, confidence}`；纯函数、不写库、不调模型。

- [ ] 失败测试：规格规则表逐条（否定/引用/假设不算指令；短回复“好，继续”结合上文；单次插话不切；切换话题/间隔不单独证实阶段结束；容量到点按完整轮次分块且保持同阶段）。
- [ ] 实现 + 测试通过。

### Task 4.2 容量：max_tokens 参与 + 轮数兜底

**Files:** `backend/src/agent/memory/fragment.py`、`backend/tests/test_fragment_capacity.py`

- [ ] 失败测试：用户轮数按绑定统计（系统通知/工具消息不计）；单轮超容量时整轮保留后再封存；容量封存后下一轮同时触发阶段变化只建一段。
- [ ] 实现 + 测试通过。

### Task 4.3 观察/启用开关与切分日志

**Files:** `backend/src/agent/config.py`、`backend/src/agent/services/turn_orchestrator.py`、`backend/tests/test_boundary_modes.py`

- [ ] 失败测试：`off` 不评估；`shadow` 只记录建议且不实际切分；`enabled` 只对「确定性边界」生效。
- [ ] 实现 + 测试通过。

### Task 4.4 离线评测集与报告（观察项）

**Files:** `backend/src/agent/eval/boundary_cases.jsonl`、`backend/src/agent/eval/boundary_eval.py`、`docs/superpowers/notes/2026-09-21-boundary-eval.md`

- [ ] 用规格验收矩阵中的阶段案例建标注集（含独立变体），规则样本与留出样本分开。
- [ ] 跑评测并记录误切/漏切/延迟/成本；**不声称生产准确率**。

---

## 阶段 5：界面适配与验证

### Task 5.1 接续相关界面行为

**Files:** `frontend/src/stores/session.ts`、`frontend/src/components/Composer.vue`、`frontend/src/views/PlanetView.vue`、`frontend/tests`（vitest）

- [ ] 点击历史后显示「将从所选记录继续」，未发送不显示已创建的新片段。
- [ ] 正在回答时允许改选后续接续位置，当前回答所属记录不变；排队消息不被追溯改向。
- [ ] 首轮失败/取消 → 可理解的提示；成功后提示消失但来源仍可查看。
- [ ] 不暴露 request_id / revision 等内部术语。

### Task 5.2 设置与文案

**Files:** `frontend/src/views/SettingsView.vue`

- [ ] 说明「根据讨论进展分段，长度用于控制单段规模」；数值配置不变更用户已有数值；观察模式如实表述。

### Task 5.3 上一轮 UI 问题修复（第 3 点除外）

**Files:** `frontend/src/views/ConversationView.vue`、`components/Composer.vue`、`components/MessageStream.vue`、`components/ui/QConfirm.vue`、`components/TopicSwitchPrompt.vue`、`stores/events.ts`、`stores/session.ts`、`backend/src/agent/api/bus.py`

- [ ] 底部三块（知识候选卡 / 话题切换条 / 兼容模式条）抬到输入框上方，按钮可点（1440/820/620 三档验证）。
- [ ] QConfirm 的 inline / popover 档 Esc 可关；关闭后焦点归还。
- [ ] 事件重放：新连接不重放旧消息，只补发「当前状态」类事件；带 Last-Event-ID 的重连仍补齐。
- [ ] 凭据 paused / revoked 有可见说明。
- [ ] 同步中显示短提示；加载历史不再清掉期间到达的提醒。
- [ ] 3D 不可用说明独立成块，不与对话内容重叠。

---

## 验证与交付

- [ ] `cd backend; uv run --frozen pytest` 全绿
- [ ] `cd frontend; npx vue-tsc --noEmit`、`npm test` 全绿
- [ ] `python scripts/check_docs.py`
- [ ] 视觉：用 `scripts/ui-catalog/` 的隔离实例重采受影响状态（接续提示、生成中改选、失败/成功提示、设置页、窄窗口、3D 降级）
- [ ] 交付报告：基线差异 / 原问题—改动—新行为对照表 / 新时序与权威状态 / 迁移编号与回退限制 / 默认启用的边界 / 实际测试结果 / NOT RUN 项 / 未校准项
