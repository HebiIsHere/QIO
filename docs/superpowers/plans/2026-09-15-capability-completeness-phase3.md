# QIO 第三阶段：能力完整性、状态表达与可达性 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 QIO 后端已经具备、前端没有完整表达（或用户找不到入口）的能力，做成状态清楚、入口合理、不打扰用户的产品功能。

**Architecture:** 后端先收敛事件协议（什么事件仍属于协议、谁产生、谁消费），再补齐缺失的生产者（能力模式 / 降级 / 凭据状态 / 工具创建进度 / 高影响知识候选）与两处语义修正（Fragment 按「轮」封块、审批用行为化描述）；前端按「状态贴近发生位置」分层消费（工具卡 / 独立任务卡 / 工具创建卡 / 知识候选卡 / 审批卡 / 话题详情的记忆浏览），把 `MEMORY_INJECT` 与内部标识收进 Developer Mode。

**Tech Stack:** Python 3.12 + FastAPI + SQLite + pytest（后端）、Vue 3 + TypeScript + Pinia + Vitest（前端）。

**Spec:** 用户粘贴的《QIO 第三阶段开发任务：能力完整性、状态表达与可达性修复》。

## Global Constraints

- **禁止任何形式的「记忆强度」控制**（滑块、低/中/高、记忆深度、长期记忆用量、换个名字的同类设置）。是否检索、注入多少、如何平衡上下文一律由 QIO 内部决定。
- **禁止暴露内部推理**：Chain of Thought、hidden reasoning、system prompt、raw planning messages 不得进入任何 UI。
- **用户可见状态只用行为语言**：不出现 `TOOL_RUNNING` / `capability fingerprint` / `policy hash` / `sandbox profile` / credential id / keychain identifier 这类内部词（技术明细只进 Developer Mode / `/debug`）。
- **不破坏第一阶段**：Turn Lifecycle、`TURN_END` 唯一终态、Cancellation、Final Answer、SSE 去重、API 认证、凭据安全、审批安全、Shell / 文件边界、CSP。
- **不破坏第二阶段**：Planet 有限展示与持续出现新 Topic、Selected Topic ≠ Anchor Topic、Reference 不改 Anchor、Predictor 不自动切换、Fragment 历史不可修改、Fragment 原文按需加载。
- 不做程序化构筑物；不做第四阶段工作（全局视觉重构、全组件重设计、全量 Motion Pass、Planet Dock 视觉重做、大规模 CSS 清理）。
- 不做斜杠命令体系、不做对话页内嵌记忆面板（仍从 Topic Detail 进入）。
- 前端文案一律中文；颜色只用 `var(--*)`（`frontend/src/styles/tokens.css`），禁止硬编码色值。
- 状态反馈优先级：字段级 > 组件级（卡片内）> 任务级 > 全局；**失败必须比成功更持久，且不能静默失败（禁止只 `console.error`）**。
- 状态消失：成功不长期占据界面（按钮文案变化后恢复、卡片状态在原位收敛），不弹「注册成功！」这类大 Toast。
- 后端验证：`cd backend; uv run --frozen pytest`（必须全绿）；前端：`cd frontend; npx vue-tsc --noEmit` + `npm test`；文档：`python scripts/check_docs.py`。
- 改动实现必须同步 `docs/status.md` 与 `docs/architecture.md`；不写会过期的硬编码数字。
- 本阶段**不做 git commit**：工作区已有第一阶段未提交的改动（`git status` 有 50+ 条），提交会把别人的在途工作混进同一提交。
- 事件只能有三种结论：**保留**（产生 → 传输 → 消费 → 必要呈现）/ **内部使用**（不进前端协议）/ **删除**（没有实际作用）。禁止继续存在「后端发、前端完全忽略」或「前端写 case、后端永远不发」的半协议。

---

## 最终事件协议（本阶段收敛结果，唯一事实）

| 事件 | 产生方 | 消费方 | 用户可见 | 用途 |
| --- | --- | --- | --- | --- |
| `TURN_START` | `core/turn.py::TurnManager` | `stores/events.ts` | 是（全局轻状态） | 一轮开始；`notify` 字段区分系统轮 |
| `TURN_END` | 同上（`finally`，恰好一次） | `stores/events.ts` | 是 | 唯一终态 + 最终回答权威来源 |
| `TURN_QUEUE` | 同上 | `QueueChip.vue` | 是（有排队时） | 排队/取消快照 |
| `ASSISTANT` | `core/loop.py` | `stores/events.ts` | 是 | 流式正文 / 工具前中间话 |
| `TOOL_START` | `core/loop.py`（转发 `tool/start`） | `stores/events.ts` → 工具卡 | 是 | 工具开始执行（卡片立即进入运行态） |
| `TOOL_END` | 同上（`tool/end`） | `stores/events.ts` → 同一张工具卡 | 是 | 结果 / 失败原因 / 耗时，原地更新 |
| `SUBAGENT_STATUS` | `tools/task_manager.py` | `stores/events.ts` → 独立任务卡 | 是 | 独立任务 queued/running/done/failed |
| `TOOL_CREATE_STATUS`（新） | `tools/dev_tools.py`、`tools/lifecycle.py` | `stores/events.ts` → 工具创建卡 | 是 | 同一张卡的创建阶段推进 |
| `KNOWLEDGE_CANDIDATE`（新） | `services/memory_lifecycle.py` + `turn_orchestrator` 收尾 | `stores/events.ts` → 对话内确认卡 | 是（回答完成后） | 高影响知识候选的保存/修改/忽略 |
| `APPROVAL_REQUIRED` | `tools/approval.py` | `stores/approvals.ts` | 是 | 需要用户决定的操作 |
| `APPROVAL_RESULT` | `tools/approval.py` | `stores/approvals.ts` | 是（卡片状态） | 授权的结局（单次使用） |
| `CAPABILITY` | `services/app.py`（每轮 begin，模式变化时） | `stores/events.ts` | 否（正常不显示） | 适配档位（native/text/unsupported） |
| `FALLBACK` | `services/app.py`（进入兼容文本模式那一次） | `stores/events.ts` → 一次性轻提示 | 是（仅降级时） | 能力降级说明 |
| `CREDENTIAL_STATUS` | `api/server.py`（凭据增删改）+ `turn_orchestrator`（不可用） | `stores/events.ts` | 部分是（仅当阻止功能） | 凭据可用性（不含内部 id） |
| `ANCHOR` | `services/app.py::_publish_anchor_event` | `stores/events.ts` | 是（话题行） | 当前位置变化 |
| `TOPIC_SWITCH_SUGGESTED` | `services/turn_orchestrator.py` | `TopicSwitchPrompt.vue` | 是 | 推测切换待确认 |
| `USAGE` | `core/loop.py` | `stores/events.ts` | 否（仅 Developer Mode） | 单轮 token/迭代/工具计数 |
| `WARNING` | `services/app.py::make_warning`、`core/loop.py` | `ConversationView.vue` | 是 | 非致命提示（不含内部枚举） |
| `ERROR` | `services/app.py::make_error`、`core/loop.py` | `ConversationView.vue` | 是 | 出错了（不承担结束 turn 的职责） |
| ~~`MEMORY_INJECT`~~ | — | — | 删除 | 内部机制；开发者改看 `/api/traces/{turn_id}` 的 `injection` |

前端 `frontend/src/services/events.ts` 的 `EVENT_TYPES` 必须与后端 `EventType` **集合完全相等**（由 Task 1 的测试守卫）。

---

## 文件结构

**后端（修改）**

- `backend/src/agent/api/events.py` — 事件集合：删 `MEMORY_INJECT`，加 `TOOL_CREATE_STATUS` / `KNOWLEDGE_CANDIDATE`。
- `backend/src/agent/core/turn.py` — `TURN_START` 带 `notify`。
- `backend/src/agent/core/loop.py` — `TOOL_START` / `TOOL_END` 带 `call_id` 与耗时。
- `backend/src/agent/services/app.py` — `CAPABILITY` / `FALLBACK` 生产者；`CREDENTIAL_STATUS`（主循环无凭据）。
- `backend/src/agent/services/turn_orchestrator.py` — turn begin 处宣告能力与凭据状态；收尾发出知识候选。
- `backend/src/agent/memory/fragment.py`、`memory/ingest.py`、`services/turn_orchestrator.py`、`api/server.py` — Fragment 按「轮」封块 + 设置字段正名。
- `backend/src/agent/tools/registry.py`、`tools/approval_present.py`（新） — 审批用行为化描述 + 授权范围。
- `backend/src/agent/tools/dev_tools.py`、`tools/lifecycle.py`、`services/app.py` — 工具创建进度事件。
- `backend/src/agent/services/memory_lifecycle.py`、`api/server.py` — 高影响知识候选事件 + 忽略接口。

**前端（修改）**

- `frontend/src/services/events.ts` — `EVENT_TYPES` 与后端一致。
- `frontend/src/stores/session.ts` — 工具卡 / 独立任务卡 / 工具创建卡 / 知识候选卡 / 全局状态。
- `frontend/src/stores/events.ts` — 事件路由。
- `frontend/src/components/MessageItem.vue` — 工具卡（运行态/耗时/失败原因/详情折叠）、独立任务卡。
- `frontend/src/components/ToolCreationCard.vue`（新）、`components/KnowledgeCandidateCard.vue`（新）。
- `frontend/src/composables/useActionFeedback.ts`（新）— 统一「进行中 / 成功 / 失败（可重试）」反馈。
- `frontend/src/views/PlanetView.vue` — Topic Detail 记忆浏览（摘要 → 片段 → 原文分页 → 从这里继续）+ 反馈统一。
- `frontend/src/components/planet/KnowledgePanel.vue`、`planet/EntityPanel.vue` — 反馈走同一 composable。
- `frontend/src/components/ApprovalModal.vue` — 授权范围（一次性 / 长期生效）。
- `frontend/src/views/SettingsView.vue`、`services/api.ts` — 记忆封块字段正名。

**文档**

- `docs/architecture.md`（事件表、可见性、Feature Reachability、Tool Creation 产品流程、Knowledge Candidate、Subagent 语义、Approval 表达原则）、`docs/status.md`（实现 + 已知限制）、`docs/release-phase3.md`（验收报告）。

---

### Task 1: 事件协议收口（后端集合 + 前端集合 + 守卫测试）

**Files:**
- Modify: `backend/src/agent/api/events.py`、`frontend/src/services/events.ts`
- Create: `backend/tests/test_event_protocol.py`

**Interfaces:**
- Produces: `EventType.TOOL_CREATE_STATUS = "TOOL_CREATE_STATUS"`、`EventType.KNOWLEDGE_CANDIDATE = "KNOWLEDGE_CANDIDATE"`；删除 `EventType.MEMORY_INJECT`。

- [ ] **Step 1: 写失败测试**（`backend/tests/test_event_protocol.py`）：
  1. 前端 `EVENT_TYPES` 集合 == 后端 `{e.value for e in EventType}`（解析 `frontend/src/services/events.ts` 里 `EVENT_TYPES` 数组的字符串字面量）；
  2. 每个保留事件在 `backend/src/agent` 里都有生产者（源码扫描 `EventType.<NAME>` 出现于 `make_event(` 调用或 `_emit(` 调用）；
  3. `MEMORY_INJECT` 不再存在，前端也不再有消费分支。
- [ ] **Step 2: 跑测试确认失败** → `cd backend; uv run --frozen pytest tests/test_event_protocol.py -q`
- [ ] **Step 3: 改 `events.py` 与 `services/events.ts`**：加两个新类型、删 `MEMORY_INJECT`（同时删 `stores/events.ts` 的 `MEMORY_INJECT` case 与 `session.memoryInject` 展示）。
- [ ] **Step 4: 跑测试确认通过** → `uv run --frozen pytest tests/test_event_protocol.py tests/test_events.py -q`，前端 `npm test -- events`

### Task 2: CAPABILITY / FALLBACK 生产者

**Files:**
- Modify: `backend/src/agent/services/app.py`、`services/turn_orchestrator.py`
- Test: `backend/tests/test_capability_events.py`

**Interfaces:**
- Produces: `AppContext.announce_capability(adapter, turn_id=None) -> None`：发 `CAPABILITY {adapter, model, turn_id}`；当 `adapter.mode == "text"` 且与上次宣告的模式不同 → 再发 `FALLBACK {from: "native", to: "text", reason: "model_without_native_tool_calls", message: "当前模型不支持原生工具调用，已使用兼容模式"}`。
- Consumes: `AdapterMode`（`adapters/base.py`）。

- [ ] **Step 1: 写失败测试**：native 适配器 → 只有 `CAPABILITY`、没有 `FALLBACK`；text 适配器 → 首次有 `FALLBACK`，连续两轮只发一次；`unsupported` 不发 `FALLBACK`（那是启动拒绝，不是降级）。
- [ ] **Step 2: 跑测试确认失败** → `uv run --frozen pytest tests/test_capability_events.py -q`
- [ ] **Step 3: 实现**：`AppContext.__init__` 加 `self._announced_mode: str | None = None`；`announce_capability` 幂等（模式不变不重复发 FALLBACK）；`turn_orchestrator.begin` 在 adapter 构建成功后调用。
- [ ] **Step 4: 跑测试确认通过** → `uv run --frozen pytest tests/test_capability_events.py tests/test_turn_lifecycle_protocol.py -q`

### Task 3: CREDENTIAL_STATUS：不可用原因进协议，前端只显示人话

**Files:**
- Modify: `backend/src/agent/services/turn_orchestrator.py`、`frontend/src/stores/events.ts`、`frontend/src/stores/session.ts`
- Test: `backend/tests/test_credential_status_events.py`、`frontend/src/stores/__tests__/credentialStatus.test.ts`

**Interfaces:**
- Produces（后端）：turn begin 无可用主循环凭据时发
  `CREDENTIAL_STATUS {status: "unavailable", scope: "main_loop", reason_code: "no_credential", message: "当前没有可用的模型凭据（设置 → 凭据）"}`，
  与既有 `WARNING` 并存（警告负责显示，状态负责状态）。
- Produces（前端）：`useEventStore.credentialNotice: { kind: "unavailable" | "paused" | "revoked"; message: string } | null`；
  只对「影响当前功能」的状态给 `session.warning` 一次性提示，**不得**把 `key_id` 显示给用户。

- [ ] **Step 1: 写失败测试**（后端）：无凭据的 turn → 事件流里同时有 `WARNING` 与 `CREDENTIAL_STATUS(unavailable)`，且 `CREDENTIAL_STATUS` 不含 `key_id`。
- [ ] **Step 2: 写失败测试**（前端）：`CREDENTIAL_STATUS{status:"unavailable"}` → `session.warning` 是人话且不含 `key_`；`{status:"active"}` → 不产生任何提示。
- [ ] **Step 3: 跑测试确认失败** → `uv run --frozen pytest tests/test_credential_status_events.py -q`；`cd frontend; npx vitest run src/stores/__tests__/credentialStatus.test.ts`
- [ ] **Step 4: 实现**：后端在 `begin` 的 `adapter is None` 分支补发事件；前端 `events.ts` 加 `case "CREDENTIAL_STATUS"`（映射表：unavailable→「当前没有可用的模型凭据…」、paused→「凭据已暂停…」、revoked→「凭据已失效…」、active/deleted→清空提示、其余忽略）。
- [ ] **Step 5: 跑测试确认通过**

### Task 4: Fragment 按「轮」封块（语义修正）

**Files:**
- Modify: `backend/src/agent/memory/fragment.py`、`memory/ingest.py`、`services/turn_orchestrator.py`、`services/app.py`、`api/server.py`、`frontend/src/services/api.ts`、`frontend/src/views/SettingsView.vue`
- Test: `backend/tests/test_fragment_turn_semantics.py`（新）、`backend/tests/test_memory.py`、`backend/tests/test_api_routes.py`

**Interfaces:**
- Produces: `FragmentManager(max_turns: int = DEFAULT_MAX_TURNS)`；`FragmentManager.turn_count(fragment_id)`；`should_close(fragment)` 用 **用户消息条数**（一轮 = 一条 user + 其后的 assistant）；工具消息不计入。
- 设置键 `fragment.max_turns`；`GET /api/settings/memory` → `{fragment_max_turns}`；`PUT` 接受 `fragment_max_turns`（1–30）；旧键 `fragment.max_messages` 只在读取时作为迁移回退（读到就写回新键）。
- 前端 `api.getMemorySettings/updateMemorySettings` 字段改名；`SettingsView` 文案仍是「轮」，但现在是真轮。

- [ ] **Step 1: 写失败测试**：`max_turns=3` 时 6 条消息（3 轮）后 `should_close` 为真；中间插入 5 条工具消息不影响计数；`turn_count` 等于 user 消息数。
- [ ] **Step 2: 跑测试确认失败** → `uv run --frozen pytest tests/test_fragment_turn_semantics.py -q`
- [ ] **Step 3: 实现后端**（改名 + 计数 + 设置键迁移回退）。
- [ ] **Step 4: 改前端字段与文案**（`api.ts`、`SettingsView.vue`）。
- [ ] **Step 5: 跑测试确认通过** → `uv run --frozen pytest tests/test_fragment_turn_semantics.py tests/test_memory.py tests/test_api_routes.py -q`；`cd frontend; npx vue-tsc --noEmit; npx vitest run src/views/__tests__/SettingsView.test.ts`

### Task 5: 工具运行卡片（TOOL_START → TOOL_END 原地更新）

**Files:**
- Modify: `backend/src/agent/core/loop.py`、`frontend/src/stores/session.ts`、`stores/events.ts`、`components/MessageItem.vue`
- Test: `frontend/src/stores/__tests__/toolRuntime.test.ts`（新）、`components/__tests__/MessageItem.test.ts`

**Interfaces:**
- Produces（后端）：`TOOL_START {tool, call_id, arguments, presentation, turn_id}`；`TOOL_END {tool, call_id, ok, error, content_preview, duration_ms, presentation, turn_id}`。
- Produces（前端）：`StreamMessage.callId?: string`、`toolRunning?: boolean`、`toolDurationMs?: number`；
  `session.startTool(callId, tool, presentation?)` / `session.finishTool(callId, ok, error, preview, presentation?, durationMs?)`；
  匹配规则：同 `callId` 就地更新；找不到就补一条（重连重放、丢帧时不丢结果）。

- [ ] **Step 1: 写失败测试**：`TOOL_START` 立刻产生 `toolRunning: true` 的卡；随后的 `TOOL_END(ok)` 让**同一条**消息变成完成（`messages.length` 不变）；`TOOL_END(ok=false)` → 卡片失败 + 人话原因（含 `error` 原文只进详情）；没有 `TOOL_START` 时 `TOOL_END` 仍出现一张卡。
- [ ] **Step 2: 跑测试确认失败** → `npx vitest run src/stores/__tests__/toolRuntime.test.ts`
- [ ] **Step 3: 实现后端** `loop.py`：`_on_pipeline_start` 带 `call_id` + `presentation`（`registry._present` 只有在 `tool/end` 才有结果；运行时用 `present_call` 的结果即可）；`_on_pipeline_end` 带 `call_id` + `duration_ms`（在 `_guarded_execute` 里记时长）。
- [ ] **Step 4: 实现前端** session / events / MessageItem：运行态显示「运行中」而不是 ✓；结束时原地更新并补耗时；失败显示 `toolError` 人话，完整输出仍在折叠详情里。
- [ ] **Step 5: 跑测试确认通过** → `uv run --frozen pytest tests/test_tool_event_isolation.py tests/test_loop.py -q`；`npx vitest run src/stores/__tests__/toolRuntime.test.ts src/components/__tests__/MessageItem.test.ts`

### Task 6: 独立任务卡（Subagent）

**Files:**
- Modify: `backend/src/agent/tools/task_manager.py`、`frontend/src/stores/session.ts`、`stores/events.ts`、`components/MessageItem.vue`
- Test: `frontend/src/stores/__tests__/subagentCard.test.ts`（新）

**Interfaces:**
- Produces（后端）：`SUBAGENT_STATUS {task_id, tool, status, display_name, goal, ok, content_preview, error}`（`goal` = 该任务的目标描述，来自工具参数；不含内部推理）。
- Produces（前端）：`StreamMessage.role === "subagent"` + `taskId`；`session.upsertSubagent(taskId, {status, goal, ok, preview, error})`；卡片文案使用「独立任务」，状态词：开始 / 进行中 / 已完成 / 失败。

- [ ] **Step 1: 写失败测试**：`SUBAGENT_STATUS(running)` 建卡；`done` 原地更新为已完成且结果可见；`failed` 显示可理解原因；同一 `task_id` 永远只有一张卡；卡片里不出现 reasoning/system prompt 字段。
- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现**（前端按 task_id upsert；后端补 `goal`/`display_name`）。
- [ ] **Step 4: 跑测试确认通过** → `uv run --frozen pytest tests/test_subagent.py tests/test_subagent_integration.py -q`；`npx vitest run src/stores/__tests__/subagentCard.test.ts`

### Task 7: 工具创建单卡流程

**Files:**
- Modify: `backend/src/agent/tools/dev_tools.py`、`tools/lifecycle.py`、`services/app.py`、`frontend/src/stores/session.ts`、`stores/events.ts`、`components/MessageItem.vue`
- Create: `frontend/src/components/ToolCreationCard.vue`
- Test: `backend/tests/test_tool_create_events.py`（新）、`frontend/src/stores/__tests__/toolCreation.test.ts`（新）

**Interfaces:**
- Produces（后端）：`TOOL_CREATE_STATUS {group_id, phase, tool_name, label, detail, ok, turn_id}`，
  `phase ∈ {proposal, building, testing, testing_passed, testing_failed, waiting_approval, registering, ready, failed}`；
  `group_id` = 开发工作区 id（`create_tool` 生成，后续 `dev_*` 用 `workspace` 参数），同一工具创建流程共用一张卡。
- Produces（前端）：`StreamMessage.role === "tool_creation"` + `groupId`；`session.upsertToolCreation(groupId, {phase, label, detail, ok, toolName})`；
  卡片状态文案：提案 / 构建中 / 测试中 / 测试通过 / 测试失败 / 等待确认 / 注册中 / 已创建 / 创建失败。

- [ ] **Step 1: 写失败测试**（后端）：`create_tool` → `proposal`；`dev_write_file` → `building`；`dev_run_tests` → `testing` 然后 `testing_passed`；`dev_submit_tool` 的审批前 → `waiting_approval`、注册后 → `ready`；注册失败 → `failed`；全部事件 `group_id` 相同。
- [ ] **Step 2: 写失败测试**（前端）：同一 `group_id` 的连续事件只保留一张卡且状态就位；失败卡显示可理解原因；`ready` 卡出现「现在可以使用」。
- [ ] **Step 3: 跑测试确认失败**
- [ ] **Step 4: 实现后端**：dev 工具通过注入的 `bus` 发事件；`ToolLifecycle._approve_and_register` 在 approvals.request 前后发 `waiting_approval` / `registering` / `ready` / `failed`；子 agent 型工具跳过测试阶段时直接 `waiting_approval`。
- [ ] **Step 5: 实现前端**：单卡原地更新；默认只显示工具名 + 当前状态 + 一行说明；`查看详情` 里才给文件与工作区（`detail`）。
- [ ] **Step 6: 跑测试确认通过** → `uv run --frozen pytest tests/test_tool_create_events.py tests/test_dev_workflow_integration.py tests/test_tool_lifecycle.py -q`；`npx vitest run src/stores/__tests__/toolCreation.test.ts`

### Task 8: 高影响 Knowledge 在对话内自然确认

**Files:**
- Modify: `backend/src/agent/services/memory_lifecycle.py`、`services/turn_orchestrator.py`、`api/server.py`、`frontend/src/stores/session.ts`、`stores/events.ts`、`views/ConversationView.vue`
- Create: `frontend/src/components/KnowledgeCandidateCard.vue`
- Test: `backend/tests/test_knowledge_candidate_event.py`（新）、`frontend/src/stores/__tests__/knowledgeCandidate.test.ts`（新）

**Interfaces:**
- Produces（后端）：回答完成后（`post_turn` 之后、`TURN_END` 之前）为每个本轮新建的**高影响**候选发
  `KNOWLEDGE_CANDIDATE {knowledge_id, category, content, impact: "high", reason, turn_id}`（低影响候选不出现）。
- Produces（后端）：`POST /api/knowledge/{id}/ignore` → `{ok: true}`：状态转 `revoked` 并在 `provenance` 写 `{"ignored_at": ...}`；被忽略的候选不再出现在事件里（同一 `content` 不再重复提示）。
- Produces（前端）：`session.knowledgeCandidates: KnowledgeCandidate[]`；`saveCandidate(id)`（`verifyKnowledge`）、`ignoreCandidate(id)`（新接口）、`editCandidate(id, content)`（内联轻编辑 → `reviseKnowledge`）；成功/失败都在卡内表达。

- [ ] **Step 1: 写失败测试**（后端）：高影响候选 → 事件在 `TURN_END` **之后到达顺序上位于回答完成后**且包含 `knowledge_id`；忽略后再跑一轮，同一 content 不再出现事件；低影响候选不产生事件。
- [ ] **Step 2: 写失败测试**（前端）：TURN_END 之前不显示候选卡；TURN_END 之后出现；保存调用 `verifyKnowledge`、忽略调用 `ignore`、修改进入内联编辑并调用 `reviseKnowledge`；忽略后本地立即移除。
- [ ] **Step 3: 跑测试确认失败**
- [ ] **Step 4: 实现后端**（收集本轮高影响候选 → 收尾发事件；新增 ignore 接口）。
- [ ] **Step 5: 实现前端**（候选卡 + 三个动作）。
- [ ] **Step 6: 跑测试确认通过** → `uv run --frozen pytest tests/test_knowledge_candidate_event.py tests/test_knowledge.py tests/test_knowledge_mgmt_api.py -q`；`npx vitest run src/stores/__tests__/knowledgeCandidate.test.ts`

### Task 9: Memory 浏览链路（Topic Detail）

**Files:**
- Modify: `backend/src/agent/api/server.py`、`frontend/src/views/PlanetView.vue`、`frontend/src/services/api.ts`
- Test: `backend/tests/test_topic_detail_fields.py`（新）、`frontend/src/views/__tests__/PlanetViewFragments.test.ts`（新）

**Interfaces:**
- Produces（后端）：`GET /api/graph/topics/{id}` 增补 `summary`、`keywords: string[]`、`last_activity`、`message_count`（话题总消息数）；`fragments[]` 保留 `fragment_id / summary / created_at / closed_at / message_count`（不内联 messages）。
- 复用：`GET /api/fragments/{id}/messages?offset&limit`（分页原文）。

- [ ] **Step 1: 写失败测试**（后端）：详情含摘要/关键词/最近活动/片段数，且**不**含 messages 数组。
- [ ] **Step 2: 写失败测试**（前端）：详情渲染标题 → 摘要 → 最近活动/片段数 → 片段列表（时间 + 摘要 + 消息数）；点「查看原文」才调 `fragmentMessages`；原文有只读提示（「这是过去发生过的内容」）；超过一页时出现「继续读取」并追加；每个片段有「从这里继续」并调 `continueFromHistory`。
- [ ] **Step 3: 跑测试确认失败**
- [ ] **Step 4: 实现**（后端字段 + 前端分层与按需加载）。
- [ ] **Step 5: 跑测试确认通过** → `uv run --frozen pytest tests/test_topic_detail_fields.py tests/test_planet_api_layers.py -q`；`npx vitest run src/views/__tests__/PlanetViewFragments.test.ts`

### Task 10: Knowledge / Entity 操作反馈统一（禁止静默失败）

**Files:**
- Create: `frontend/src/composables/useActionFeedback.ts`
- Modify: `frontend/src/views/PlanetView.vue`、`components/planet/KnowledgePanel.vue`、`components/planet/EntityPanel.vue`
- Test: `frontend/src/composables/__tests__/useActionFeedback.test.ts`（新）、`frontend/src/views/__tests__/PlanetViewFeedback.test.ts`（新）

**Interfaces:**
- Produces: `useActionFeedback()` → `{ stateOf(key): "idle" | "busy" | "ok" | "failed", errorOf(key): string, run(key, fn, opts?: {okText?: string}): Promise<boolean> }`；成功文案短暂显示后回到 `idle`，失败保留直到下次尝试（失败比成功持久）。

- [ ] **Step 1: 写失败测试**（composable）：`busy` → `ok` 自动回到 `idle`；失败保留 + `errorOf` 有人话 + 再次调用可重试。
- [ ] **Step 2: 写失败测试**（PlanetView）：知识「修正」失败时不再只有 console，而是卡片内可见错误 + 可重试；成功显示「已保存」后恢复。
- [ ] **Step 3: 跑测试确认失败**
- [ ] **Step 4: 实现**：三个使用点都改成同一 composable；KnowledgePanel / EntityPanel 的全局 toast 改为对应卡内的状态行（保留原有成功语义）。
- [ ] **Step 5: 跑测试确认通过** → `npx vitest run src/composables/__tests__/useActionFeedback.test.ts src/views/__tests__/PlanetViewFeedback.test.ts src/components/planet/__tests__/`

### Task 11: 审批可理解性（行为化描述 + 授权范围）

**Files:**
- Create: `backend/src/agent/tools/approval_present.py`
- Modify: `backend/src/agent/tools/registry.py`、`frontend/src/components/ApprovalModal.vue`
- Test: `backend/tests/test_approval_present.py`（新）、`frontend/src/components/__tests__/ApprovalModal.test.ts`

**Interfaces:**
- Produces: `describe_tool_call(tool_name: str, arguments: dict) -> dict` →
  `{"description": "想修改当前项目中的 3 个文件", "explanation": "…", "access": ["/path/a", "/path/b"], "scope": "once" | "long_term", "detail": "实际命令 / 工作目录"}`；
  `_approval_policy` 的 payload 增补这些字段（保留 `tool` / `arguments` 原样，供开发者详情）。
- Produces（前端）：审批卡新增一行「授权范围：仅这一次 / 长期生效（工具注册后一直可用）」。

- [ ] **Step 1: 写失败测试**（后端）：`fs_write`（多文件）→ 描述是「想修改当前项目中的 N 个文件」且 access 是路径；`run_shell` → 「想运行一条 shell 命令」且 detail 含实际命令；`run_program` → 「想运行一个程序」；`proc_kill` → 「想结束一个进程」；未登记工具 → 回落为工具中文名，不编造。
- [ ] **Step 2: 写失败测试**（前端）：审批卡显示「授权范围」；工具注册类审批显示「长期生效」；普通工具执行显示「仅这一次」。
- [ ] **Step 3: 跑测试确认失败**
- [ ] **Step 4: 实现**
- [ ] **Step 5: 跑测试确认通过** → `uv run --frozen pytest tests/test_approval_present.py tests/test_approval_binding.py -q`；`npx vitest run src/components/__tests__/ApprovalModal.test.ts`

### Task 12: 全局状态只表达整体情况 + 开发者模式归口

**Files:**
- Modify: `frontend/src/components/MessageStream.vue`、`views/DebugView.vue`、`frontend/src/stores/session.ts`
- Test: `frontend/src/components/__tests__/MessageStream.test.ts`、`frontend/src/views/__tests__/DebugView.test.ts`（如无则新建）

**Interfaces:**
- Produces: `session.activity: "idle" | "waiting" | "generating" | "tool" | "approval" | "subagent" | "notify"`（由 store 从现有事件推导，不新增协议）；
  Composer/消息流底部只显示一条轻文案：正在处理 / 正在使用工具 / 等待确认 / 正在处理独立任务 / 正在整理独立任务的结果。
- Produces: `/debug` 单轮详情显示该轮 `injection`（哪些 Fragment / Knowledge / Entity 进入上下文），作为 `MEMORY_INJECT` 删除后的开发者入口。

- [ ] **Step 1: 写失败测试**：运行中 + 工具运行 → 全局文案是「正在使用工具」，且界面不出现 `TOOL_START` / `TOOL_RUNNING` / `MODEL_WAIT` 这类内部词；等待审批 → 「等待确认」；独立任务运行 → 「正在处理独立任务」。
- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 跑测试确认通过** → `npx vitest run src/components/__tests__/MessageStream.test.ts` + `npx vue-tsc --noEmit`

### Task 13: 文档同步（架构 / 状态 / 可达性清单）

**Files:**
- Modify: `docs/architecture.md`、`docs/status.md`
- Create: `docs/release-phase3.md`

**Interfaces:**
- Produces：`architecture.md` 新增/更新「事件协议与可见性」表、「Tool Creation 产品流程」、「Knowledge Candidate 流程」、「Subagent 产品语义」、「Approval 表达原则」、「Feature Reachability 清单」。

- [ ] **Step 1: 写事件表**（与 Task 1 的测试同一集合；文档不写数量）。
- [ ] **Step 2: 写可达性清单**：功能 / 入口 / 自动触发位置 / 用户能否完成 / 是否开发者能力。
- [ ] **Step 3: 更新 `docs/status.md`** M 相关条目（新增实现 + 已知限制），保持三处里程碑状态一致。
- [ ] **Step 4: 跑 `python scripts/check_docs.py`** → 0 失败

### Task 14: 全量验证、真实运行与验收报告

**Files:**
- Create: `docs/release-phase3.md`（Task 13 已建，这里补实测结果）

**Interfaces:**
- Consumes: 全部改动。

- [ ] **Step 1: 后端全绿**：`cd backend; uv run --frozen pytest -q`
- [ ] **Step 2: 前端类型 + 测试**：`cd frontend; npx vue-tsc --noEmit; npm test`
- [ ] **Step 3: 文档一致性**：`python scripts/check_docs.py`
- [ ] **Step 4: 真实运行**（`python scripts/e2e_up.py` + 应用内浏览器）：场景 1 记忆浏览、2 高影响知识、3 工具运行、5 降级、6 凭据失败、7 独立任务、8 审批、9 知识/实体修改；工具创建（场景 4）若无法真实跑通 → 如实写「未运行」并说明原因。
- [ ] **Step 5: 视觉检查**：工具卡是否太吵、审批是否过于技术化、知识候选是否打断回答、独立任务是否过度占空间、Topic Detail 是否像后台管理页、状态文字是否过多、是否出现大量徽章。
- [ ] **Step 6: 按 spec 第 116 节结构写报告**（修改摘要 / 原有问题 / 事件表 / Tool Creation / 用户可见状态 / 内部状态 / Capability Reachability / Tests / Manual Verification / 剩余问题），未跑的项目写「未运行」。

---

## Self-Review

**1. Spec coverage**

| spec 段落 | 落在哪个 Task |
| --- | --- |
| 一~五（目标、原则、禁止记忆强度、用户只控边界） | Global Constraints（无记忆强度设置；用户控件只留意图/边界/权限） |
| 六~十三（Memory 浏览链路、从 Fragment 继续、不做历史树） | Task 9 |
| 十四~十九（高影响 Knowledge 自然确认、不打断、忽略不重复、Panel 不再是唯一入口） | Task 8 |
| 二十~三十一（Tool Creation 单卡、提案/构建/测试/权限/审批/注册/失败） | Task 7（+ Task 11 审批可理解） |
| 三十二~三十四（TOOL_START 消费、TOOL_END 原地更新、卡片默认信息） | Task 5 |
| 三十五~三十八（运行状态贴近发生位置、全局只表达整体） | Task 12（+ Task 5/6/7 的卡片级状态） |
| 三十九~四十二（Capability/Fallback 完成或删除、降级低干扰一次性、正常不显示） | Task 2 |
| 四十三~四十六（Credential Status 用户可理解部分、不暴露内部标识） | Task 3 |
| 四十七~五十二（事件表、三选一、禁止半协议、MEMORY_INJECT 不用户可见、Memory Debug 进开发者） | Task 1 + Task 12 |
| 五十三~五十六（Topic Detail 信息完整但不做后台管理页） | Task 9 |
| 五十七~六十（Fragment 大小语义修复、工具消息不计轮） | Task 4 |
| 六十一~六十五（Subagent 产品表达、独立任务卡、结果自然返回） | Task 6 |
| 六十六~七十（Approval 五个问题、行为化文案、危险动作更明确） | Task 11 |
| 七十一~七十四（Knowledge / Entity 反馈统一，不做视觉重构） | Task 10 |
| 七十五~七十九（可达性审查、能力入口原则、无入口能力必须决定归属） | Task 13（清单）+ Task 12（开发者归口） |
| 八十~八十九（状态反馈层级、少用 Toast、字段/组件/任务/全局、加载状态、错误人话、原始错误进开发者） | Task 10（层级）+ Task 5/6/7（卡片内）+ Task 12（全局）+ Task 3（凭据人话） |
| 九十~九十四（不暴露推理、一二阶段兼容、不做程序化构筑物、不做第四阶段） | Global Constraints |
| 九十五~一百零六（测试项） | 各 Task 的测试步骤 + Task 14 |
| 一百零七~一百零八（真实运行 9 场景 + 视觉验证） | Task 14 |
| 一百零九~一百一十四（信息密度、状态消失、成功/失败反馈、开发者模式、Trace 不产品化） | Global Constraints + Task 10 + Task 12 |
| 一百一十五~一百一十七（文档同步、输出报告、验收标准） | Task 13 + Task 14 |

**2. Placeholder scan**：无 TBD / 「稍后实现」；每个 Task 都有测试项、命令、接口契约与关键实现规则。

**3. Type consistency**：`call_id`（后端）↔ `StreamMessage.callId`（前端）；`group_id`（后端）↔ `groupId`（前端）；`task_id` ↔ `taskId`；`fragment_max_turns` 在 Task 4 的后端/前端/测试三处同名；`TOOL_CREATE_STATUS.phase` 枚举在 Task 7 后端与前端状态文案表一一对应。
