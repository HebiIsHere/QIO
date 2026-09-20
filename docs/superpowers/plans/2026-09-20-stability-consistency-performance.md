# QIO 稳定性 / 逻辑一致性 / 性能修复 实施计划

> **For agentic workers:** 按任务逐条执行，每个任务以「先写失败测试 → 跑失败 → 最小实现 → 跑通过」为一个循环。

**Goal:** 在不改变 QIO 产品设计的前提下，修正 Turn 状态模型、取消语义、资源生命周期、用量语义与上下文预算，并消除 Memory / Markdown / 历史加载 / Adapter / EventBus 的长期性能缺陷。

**Architecture:** 后端 `TurnManager` 是 turn 生命周期的唯一真源（accepted → queued → running → terminal）；前端 `session` store 只承认 `TURN_START` 能把 turn 置为 active，`TURN_QUEUE` 快照为第二真源；Adapter 层负责把供应商差异归一化为 `ModelUsage`；Memory Selector 从「全量重建」改为「增量 upsert/remove」。

**Tech Stack:** Python 3.12 / FastAPI / asyncio / SQLite(WAL) / pytest；Vue 3 / Pinia / TypeScript / Vitest / unified+remark+highlight.js。

**Spec:** `C:\Users\zxy\.codex\attachments\2bb23918-f518-4e61-a88e-ce119bf5c91e\已粘贴的文本.txt`

## Global Constraints

- 不改变产品设计：Topic / Fragment / Memory 概念、主对话模式、Subagent 用法、Tool / Approval 流程、前端视觉语言与动画方向。
- 不把 Bug 当兼容性要求：queued turn 覆盖 active、shutdown 悬挂 wait 等异常行为必须修掉。
- 性能优化不得以降低体验实现：不关闭流式、不删动画、不降 Markdown 能力、不关 Memory。
- 改 schema 只能追加新迁移，禁止修改历史迁移（`backend/AGENTS.md`）。
- 任何日志 / 事件 / Trace / 错误 / 测试输出不得出现密钥原文。
- 测试不得依赖真实 API Key 或联网；模型调用一律 fake/mock provider。
- 改了实现必须同步 `docs/status.md`，并保证 `python scripts/check_docs.py` 通过。
- 验证命令：后端 `cd backend; uv run --frozen pytest`；前端 `npx vue-tsc --noEmit`、`npm test`。

---

## 已核实的问题清单（改动前事实）

| # | 问题 | 位置 | 证据 |
| --- | --- | --- | --- |
| 1 | queued turn 被取消后仍留在 `asyncio.Queue`，worker 取出后设置 `_active` 并发 `TURN_START` | `core/turn.py::_work/_cancel` | `cancel()` 只从 `_pending` 移除，`_work` 对 `ctx.cancelled` 的判断在 `TURN_START` 之后 |
| 2 | `shutdown()` 不 resolve queued turn 的 future | `core/turn.py::shutdown` | 只 `worker.cancel()`，`_futures` 中排队项永不结束 |
| 3 | `wait()` 超时不清理 future | `core/turn.py::wait` | `asyncio.wait_for` 超时后 `_futures` 仍持有该 future |
| 4 | SEND 返回即把 `activeTurnId` 设为该 turn | `frontend/src/stores/session.ts::send` | `this.activeTurnId = res.turn_id` 无 queued 判断 |
| 5 | 因为 4，`TURN_END(A)` 被 `tid !== activeTurnId` 过滤丢弃 | `frontend/src/stores/events.ts` TURN_END 分支 | 过滤条件以 activeTurnId 为基准 |
| 6 | 默认存在整轮 output token 预算 51200 | `core/budget.py` | `token_budget: int = DEFAULT_TOKEN_BUDGET`，`loop.__init__` 无参时落到默认值 |
| 7 | Anthropic 用量被当 OpenAI 字段读，输出闸回退到 `total_tokens` | `core/loop.py::_tokens_of/_plan` | `usage.get("completion_tokens", usage.get("total_tokens"))` |
| 8 | TextAdapter 合法纯文本被判为解析失败，且不返回 usage | `adapters/text.py::complete` | `ok = parsed is not None`；`usage=None` |
| 9 | EventBus 每订阅者无界队列 | `api/bus.py` | `asyncio.Queue()` 无 maxsize |
| 10 | TaskManager 先标 running 再抢 semaphore | `tools/task_manager.py::_run` | `record.status = "running"` 在 `async with self._semaphore` 之前 |
| 11 | `await_result` 超时后 waiter 永久残留 | `tools/task_manager.py::await_result` | timeout 分支直接 return，未从 `_waiters` 摘除 |
| 12 | Task 记录与 `full_content` 永久驻留内存 | `tools/task_manager.py` | `_records` 无上限、无 TTL、无裁剪 |
| 13 | Memory Selector 每次封块全量重建 | `services/app.py::_refresh_selector` + `memory_lifecycle` | 全表 SELECT → 全量 `Selector.load` |
| 14 | 向量检索每次 search 重组矩阵 | `selector/onnx.py::search`、`selector/remote.py::search` | `np.stack(list(self._vectors.values()))` |
| 15 | Markdown 每个动画帧全量重解析 + 全量高亮 | `MarkdownContent.vue` | `renderSource = source.slice(0, shown)` 且 `shown` 每帧变化 |
| 16 | 进入 Topic 加载全部历史消息 | `services/app.py::session_messages` + `api/server.py::session_context` | 无 limit，`ORDER BY created_at` 全量 |
| 17 | 每轮重建 Adapter / HTTP client，Anthropic 每轮 probe | `services/app.py::build_adapter_for_credential` | `AsyncOpenAI(...)` 每轮新建；`probe_anthropic` 每轮实打一次请求 |
| 18 | Approval 绑定校验在真实 API 路径上不生效 | `api/server.py::respond_approval` | 未传 turn_id / session_id / digest |
| 19 | 多步业务写入无事务（`isolation_level=None`） | `storage/db.py` | 全 autocommit |

---

## Workstream 划分（互不重叠的文件所有权）

| WS | 负责范围 | 文件 |
| --- | --- | --- |
| A | Turn 状态机 + 预算默认值（后端） | `core/turn.py`、`core/budget.py`、`core/loop.py`、`api/server.py`(turns)、`services/app.py`(budget/turn 接线) |
| B | 前端 Turn 状态 + Stop 目标 | `stores/session.ts`、`stores/events.ts`、`components/Composer.vue` |
| C | ModelUsage + 上下文预算 + Adapter 生命周期 | `adapters/*.py`、`services/context.py`、`services/token_budget.py` |
| D | Subagent TaskManager + EventBus backpressure | `tools/task_manager.py`、`api/bus.py` |
| E | Memory Selector 增量 + 向量矩阵 | `selector/*.py`、`services/memory_lifecycle.py` |
| F | Streaming Markdown + 历史分页（前端） | `components/MarkdownContent.vue`、`views/ConversationView.vue`、`services/api.ts` |
| G | Approval 绑定 + DB 事务/不变量 | `tools/approval.py`、`storage/*`、`api/server.py`(approvals) |

共享文件（`services/app.py`、`api/server.py`）由 root 统一收口，避免并行冲突。

---

## Task 1: Turn 状态模型与墓碑取消（WS A）

**Files:** `backend/src/agent/core/turn.py`、`backend/tests/test_turn_manager.py`、`backend/tests/test_turn_lifecycle_protocol.py`

**Interfaces:**
- Produces: `TurnContext.status ∈ {accepted, queued, running, completed, failed, cancelled, unavailable}`；`TERMINAL_STATUSES` 不变。

- [ ] `submit()`：等待中的 turn 状态置 `queued`，立即可执行的置 `accepted`。
- [ ] `_work()`：出队后先判 `ctx.status in TERMINAL_STATUSES or ctx.cancelled` → 直接跳过，不设 `_active`、不发 `TURN_START`、不发 `TURN_END`。
- [ ] `cancel(turn_id)`（queued 分支）：置 terminal、移出 `_pending`、resolve future、广播 TURN_QUEUE。
- [ ] `wait()`：超时或异常路径用 `try/finally` 从 `_futures` 摘除。
- [ ] `shutdown()`：先 `_closed=True` 停止受理；把所有 queued turn 置 cancelled 并 resolve future；再取消 worker；最后清空 `_pending`/`_futures`。
- [ ] 测试：`test_queued_turn_never_emits_turn_start_after_cancel`、`test_shutdown_resolves_queued_futures`、`test_wait_timeout_does_not_leak_future`。

## Task 2: 默认关闭整轮 output token 预算（WS A）

**Files:** `backend/src/agent/core/budget.py`、`backend/src/agent/core/loop.py`、`backend/tests/test_budget_defaults.py`

- [ ] `DEFAULT_TOKEN_BUDGET = 0`（0 = 不限），`IterationBudget.token_budget` 默认 0。
- [ ] `AgentLoop.__init__` 统一为 `IterationBudget(max_iterations=..., token_budget=token_budget or 0)`。
- [ ] `raise_limits` 在 `token_budget == 0` 时只追加迭代。
- [ ] 迭代上限 / guard / provider 上限不变。
- [ ] 测试：`test_default_turn_has_no_output_token_cap`、`test_user_configured_budget_still_enforced`。

## Task 3: ModelUsage 统一（WS C）

**Files:** `backend/src/agent/adapters/base.py`、`native.py`、`anthropic.py`、`text.py`、`backend/src/agent/core/loop.py`

- [ ] `ModelUsage(input_tokens, output_tokens, total_tokens)` dataclass；`Completion.usage: ModelUsage | None`。
- [ ] 三个 Adapter 各自把供应商字段归一化为 `ModelUsage`。
- [ ] `loop._tokens_of` 只读 `usage.output_tokens`；trace 的 input/output 从 `ModelUsage` 取。
- [ ] USAGE 事件保持 `tokens`（= output）并新增 `input_tokens/output_tokens/total_tokens`。
- [ ] 测试：`test_usage_normalized_across_adapters`。

## Task 4: Context 预算贴近真实请求（WS C）

**Files:** `backend/src/agent/adapters/base.py`、`services/context.py`、`services/token_budget.py`、`services/app.py::build_injection`

- [ ] `BaseAdapter.request_overhead_tokens(tools, messages)`：native 基础开销；text 计入其合成 system prompt + 工具说明块；anthropic 计入 system/工具帧开销。
- [ ] ContextAssembler 使用真实 system prompt 与 adapter overhead，而不是猜常量。
- [ ] 测试：`test_text_adapter_overhead_counted`。

## Task 5: Adapter / ModelClient 生命周期（WS C，与 root 协作）

**Files:** `backend/src/agent/services/app.py`

- [ ] 缓存 key = `(provider, endpoint, key_id, model, credential_version)`。
- [ ] Anthropic probe 结果进缓存，不再每轮实打。
- [ ] credential 变更（updated_at 或删除/暂停）→ 失效对应缓存。
- [ ] app shutdown 统一 close。

## Task 6: Subagent TaskManager（WS D）

**Files:** `backend/src/agent/tools/task_manager.py`、`backend/tests/test_subagent.py`

- [ ] 提交即 `queued` 并发事件；**获得 semaphore 之后**才 `running`。
- [ ] `await_result` 所有出口 `try/finally` 摘除 waiter。
- [ ] 记录保留策略：最大条数 + TTL；完成后按需释放 `full_content`。
- [ ] 测试：`test_queued_task_not_marked_running`、`test_wait_timeout_cleans_waiter`、`test_task_records_bounded`。

## Task 7: EventBus backpressure（WS D）

**Files:** `backend/src/agent/api/bus.py`、`backend/tests/test_events_bus.py`

- [ ] 每订阅者有界队列；同一 turn 的 `ASSISTANT` 只保留最新（累计语义），`USAGE` 只保留最新。
- [ ] 溢出时优先丢弃可合并/非关键事件；关键生命周期事件优先保留。
- [ ] 测试：`test_slow_subscriber_does_not_grow_unbounded`、`test_turn_lifecycle_events_survive_backpressure`。

## Task 8: Memory Selector 增量（WS E）

**Files:** `backend/src/agent/selector/selector.py`、`bm25.py`、`onnx.py`、`remote.py`、`services/memory_lifecycle.py`

- [ ] `Selector.upsert(doc, title, token_estimate)` / `Selector.remove(doc_id)`。
- [ ] `BM25Backend.upsert/remove` 维护 df/len/avgdl，结果与全量重建一致。
- [ ] ONNX/Remote 维护向量矩阵，脏标记驱动重建。
- [ ] 封块时只增量 upsert 新 index 行（`refresh_selector` 仍保留作全量修复）。
- [ ] 测试：`test_incremental_matches_full_rebuild`、`test_upsert_single_doc_does_not_reindex_all`。

## Task 9: Streaming Markdown（WS F）

**Files:** `frontend/src/components/MarkdownContent.vue`、`frontend/src/components/__tests__/MarkdownContent.test.ts`

- [ ] 解析频率与动画帧解耦：解析按批次节流（≤10 次/秒），渲染仍为 `source` 前缀 → DOM 与旧实现逐状态一致，无跳动。
- [ ] hljs 高亮结果按 `(lang, value)` 记忆化。
- [ ] `unified` processor 提升为模块级单例。
- [ ] `reveal` 结束/落定时立即做一次完整解析。
- [ ] 测试：长 Markdown / 大代码块 / 表格 / GFM 最终 DOM 正确；解析次数有上限。

## Task 10: 历史消息渐进加载（WS F + root）

**Files:** `services/app.py::session_messages`、`api/server.py`、`frontend/src/stores/session.ts`、`views/ConversationView.vue`、`services/api.ts`

- [ ] 后端返回最近 N 条 + `has_more` + `next_before` 游标；新增「更早」分页接口。
- [ ] 前端首屏只渲染最近一页；向上滚动加载更早；按 id 去重，不丢不重。
- [ ] reconnect 后不重复。

## Task 11: Approval 绑定落到真实路径（WS G）

**Files:** `tools/approval.py`、`api/server.py`、`frontend/src/stores/approvals.ts`、`services/api.ts`

- [ ] 前端提交 `turn_id` / `session_id` / `request_digest`；后端 `respond` 真正校验，不匹配 → 400。
- [ ] 正常审批体验不变（一次允许 → 操作继续）。

## Task 12: DB 事务与不变量（WS G）

**Files:** `storage/db.py`、`storage/migrate.py`、`storage/schema.py`、`services/memory_lifecycle.py`、`graph/anchors.py`

- [ ] `db.transaction(conn)` 上下文管理器。
- [ ] 多步写入（建 fragment + 更新 anchor + relation + memory_index upsert）包进一个事务。
- [ ] 新迁移追加约束前先做数据归一化；不得让已有库启动失败。

## Task 13: 状态序列测试与全量验证（root）

- [ ] 新增 `backend/tests/test_turn_state_sequences.py`：连续发送 / Stop / cancel queued / shutdown / subagent 并发 / wait timeout / approval / usage / 无预算。
- [ ] 新增前端 `stores/__tests__/turnSequences.test.ts`：SEND→queue→START / END 归属 / 重连重放。
- [ ] 跑 `uv run --frozen pytest`、`npx vue-tsc --noEmit`、`npm test`、`python scripts/check_docs.py`。
- [ ] 更新 `docs/status.md`。

---

## 自审（对照 spec 36 条验收）

1/2/3/4 → Task 1、8（前端）；5 → Task 3；6/7/8 → Task 2；9/10/11 → Task 6；12 → Task 8；13 → Task 9；14 → Task 10；15 → Task 5；16 → Task 11；17 → Task 12；18 → Task 7；19 → Task 3（TextAdapter）；20 → Task 13；21 → Task 13；22 → 全局约束。
