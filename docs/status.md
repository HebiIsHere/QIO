# QIO 项目状态

**本文只回答一个问题：现在真的做到哪了。**

- 设计意图与分层 → `docs/architecture.md`
- 安装与运行 → `docs/SETUP.md`
- 协作约定 → `AGENTS.md`

最后核对：2026-09-12（`main` 分支）。核对方法见文末。

---

## 状态口径

| 状态 | 含义 |
| --- | --- |
| `completed` | 有实现、有自动化测试覆盖主路径，后续可以依赖 |
| `partial` | 主路径可用，但存在本节列出的已知限制 |
| `active` | 正在改，行为或接口可能变化 |
| `planned` | 只有设计，没有实现 |

**给后续编码 Agent 的硬性规则**

1. 不要在本文件写测试数量、事件数量、表数量这类会迅速过期的硬编码数字。要真实数字就去跑命令。
2. 一个能力只有在「代码 + 测试」都在仓库里时才允许标 `completed`。
3. 改完实现要同步改本文件。状态与代码冲突时，以代码为准，并立刻修正本文件。
4. 本文件与 `architecture.md`、`README.md` 的里程碑状态必须一致；`scripts/check_docs.py` 会检查这一点。

---

## 产品里程碑

### M0 — 骨架与事件协议

- **Status：** completed
- **Implementation：** `backend/src/agent/main.py`（`create_app` 工厂）、`api/server.py`（路由）、`api/events.py`（事件信封与类型）、`api/bus.py`（订阅扇出 + 重放缓冲）
- **Tests：** `backend/tests/test_events.py`、`test_events_bus.py`、`test_api_routes.py`
- **Known limitations：** `POST /api/events/test` 是开发用途的注入口，不构成产品功能；事件类型集合会随功能增长，数量不写死在文档里。
- **后续依赖：** 无（其余里程碑都建立在这一层上）。

### M1 — 存储层

- **Status：** completed
- **Implementation：** `storage/schema.py`（顺序迁移，当前 schema 版本见 `SCHEMA_VERSION`）、`storage/migrate.py`、`storage/db.py`（WAL）、`storage/archive.py`（冷归档）、`storage/settings.py`
- **Tests：** `backend/tests/test_storage.py`、`test_settings.py`
- **Known limitations：** 归档与维护由后台任务触发，默认不是常驻高频；SQLite 单写者，写入串行化由后端负责。
- **后续依赖：** M2/M6/M7/M8 的表都通过迁移追加；改动 schema 必须新增一条迁移，不能改历史迁移。

### M2 — 凭据层（BYOK）

- **Status：** completed
- **Implementation：** `credentials/store.py`（密钥进 keyring，元数据进 SQLite）、`credentials/policy.py`（按标签解析 + 快照）、`services/identify.py`（Key 识别与端点探测）
- **Tests：** `backend/tests/test_credentials.py`、`test_identify.py`、`test_tool_credentials.py`
- **Known limitations：** 只在 Windows 凭据库上验证过；预算以 token 计数为主。
- **后续依赖：** M3 适配层、M10 子 agent、embedding 选档都从这里取 Key。

### M3 — 模型适配层

- **Status：** completed
- **Implementation：** `adapters/base.py`（内部 `Completion` / `ChatMessage` / `ToolCall` 契约）、`adapters/native.py`、`adapters/text.py`、`adapters/anthropic.py`、`adapters/probe.py`、`adapters/model_context.py`、`adapters/errors.py`（内部错误分类）
- **Tests：** `backend/tests/test_adapters.py`、`test_anthropic_adapter.py`、`test_adapter_contract.py`、`test_adapter_errors.py`、`test_model_context.py`
- **Known limitations：** `unsupported` 档默认拒绝启动，需要用户显式打开强制继续；`text` 档的能力弱于 `native`（不切换话题）。
- **后续依赖：** M4 主循环、M5 选择器、M9 上下文组装都消费这一层的内部模型，不直接接触任何厂商 SDK 对象。

### M4 — 主循环

- **Status：** completed
- **Implementation：** `core/loop.py`（PLANNING → TOOL_EXEC → OBSERVING → DONE）、`core/budget.py`（迭代 + token 双预算，native 128 / text 64）、`core/guard.py`（重复失败护栏）
- **Tests：** `backend/tests/test_loop.py`、`test_loop_continue.py`、`test_runaway_guard.py`、`test_budget_defaults.py`
- **Known limitations：** 迭代上限可在设置中覆盖，但不会无限；护栏按「同一调用重复失败」计数，命中后 WARN/BLOCK/HALT。
- **后续依赖：** M9 上下文组装与 M10 工具执行都挂在主循环的规划步上。

### M5 — 选择器与工具路由

- **Status：** completed
- **Implementation：** `selector/`（规则层、BM25、ONNX 本地嵌入、远程嵌入）、`services/tool_router.py`（查询只嵌入一次 + 工具描述批量缓存 + 条件暴露）
- **Tests：** `backend/tests/test_selector.py`、`test_tool_router.py`、`test_tool_router_cache.py`、`test_embedding_identity.py`、`test_onnx_backend.py`、`test_remote_embedding.py`
- **Known limitations：** 远程嵌入与本地 ONNX 都需要额外配置或模型文件；缺失时降到 BM25 / 规则层，功能可用但召回质量下降。精排层默认关闭。
- **后续依赖：** M9 检索排序、M10 工具候选展示都复用这一层。

### M6 — 记忆域

- **Status：** completed
- **Implementation：** `memory/ingest.py`、`memory/fragment.py`、`memory/summary.py`、`memory/index.py`、`services/memory_lifecycle.py`（封块、滚动摘要、预算压力整理）
- **Tests：** `backend/tests/test_memory.py`、`test_injection_short_term.py`、`test_turn_short_term.py`
- **Known limitations：** 摘要与知识提炼依赖主模型调用，失败时降级为原文直引；索引是机械生成，模型不可写。
- **后续依赖：** M9 注入以此为素材；M12 维护任务读取片段做整理。

### M7 — 知识域

- **Status：** completed
- **Implementation：** `knowledge/lifecycle.py`（状态机 + supersedes 版本链）、`knowledge/verify.py`（分层验证）、`knowledge/inject.py`、`tools/knowledge_correction.py`（对话式纠错）
- **Tests：** `backend/tests/test_knowledge.py`、`test_knowledge_correction.py`、`test_knowledge_mgmt_api.py`
- **Known limitations：** 高影响类别必须用户确认；隐式反馈与自动整理属后续项，见本文末「尚未完成」。
- **后续依赖：** M9 只把 `active` 条目纳入注入面。

### M8 — 图导航层

- **Status：** completed
- **Implementation：** `graph/nodes.py`、`graph/edges.py`、`graph/anchors.py`、`graph/topics.py`、`graph/layout.py`、`entities/`（识别、抽取、卡片）、`tools/topic_tools.py`、`tools/entity_tools.py`
- **Tests：** `backend/tests/test_graph.py`、`test_anchor_event.py`、`test_entities_recognizer.py`、`test_entity_cards.py`、`test_entity_correct.py`、`test_entity_extract.py`、`test_entity_inject.py`、`test_entity_retrieval.py`、`test_topic_tools.py`、`test_topic_dedup.py`
- **Known limitations：** 实体懒创建依赖提及计数阈值；星球视图的布局计算在前端完成，后端只提供数据。
- **后续依赖：** M9 的亲和度与 M10 的 `switch_topic` / `create_topic` 都依赖锚点。

### M9 — 注入与检索

- **Status：** completed
- **Implementation：** `services/context.py`（ContextAssembler）、`services/injection.py`、`services/retrieval.py`、`services/affinity.py`、`services/token_budget.py`（TokenBudgetPlanner + completion reserve + 预算分解）、`services/decay.py`（分类型时间衰减）、`services/params.py`（集中阈值）
- **Tests：** `backend/tests/test_service_injection.py`、`test_injection_short_term.py`、`test_token_budget.py`、`test_decay.py`、`test_affinity.py`、`test_focus.py`、`test_turn_no_duplicate_query.py`
- **Known limitations：** 注入上限是硬约束，强制项超预算时走确定性截断；阈值集中在 `services/params.py`，改动需要 eval 支撑（见 P3）。
- **后续依赖：** 无下游；被 P1/P2 的 turn 流水线调用。

### M10 — 工具创建生命周期

- **Status：** completed
- **Implementation：** `tools/creator.py`、`tools/lifecycle.py`、`tools/dev_tools.py`、`tools/dev_workspace.py`、`tools/tester.py`、`tools/sandbox.py`、`tools/policy.py`、`tools/approval.py`、`tools/subagent_tool.py`、`tools/task_manager.py`、`storage/tool_store.py`
- **Tests：** `backend/tests/test_tool_lifecycle.py`、`test_dev_tools.py`、`test_dev_workflow_integration.py`、`test_tool_policy.py`、`test_computer_sandbox.py`、`test_subagent.py`、`test_subagent_integration.py`、`test_tool_parallel_cancel.py`、`test_tool_registry_reversible.py`、`test_tool_restore.py`、`test_tool_store.py`、`test_tool_schema_present.py`、`test_tool_pipeline.py`、`test_tool_event_isolation.py`
- **Known limitations：** 受限子进程不是强安全隔离；高风险能力（联网、写用户文件、起进程）在没有可用 Docker 时直接拒绝执行，不做静默降级。子 agent 异步并行上限见 `tools/task_manager.py`。
- **后续依赖：** 无下游。

### M11 — 前端

- **Status：** completed
- **Implementation：** `frontend/src/views/`（对话页、星球页、设置页、调试页）、`frontend/src/components/`、`frontend/src/stores/`、`frontend/src/planet/`、`frontend/src-tauri/`（桌面壳）
- **Tests：** `frontend/src/**/__tests__/*.test.ts`、`frontend/src/smoke.test.ts`、`frontend/src/styles/tokens.test.ts`
- **Known limitations：** 桌面壳只在 Windows 上验证过；应用内浏览器有模块缓存，改前端后需带 `?fresh=N` 强刷。斜杠命令体系未实现。
- **后续依赖：** 无下游。

### M12 — 离线维护任务

- **Status：** completed
- **Implementation：** `services/maintenance.py`（矛盾扫描、Dreaming 整理、轨迹→工具候选）、`prompts.py`
- **Tests：** `backend/tests/test_m12_maintenance.py`、`test_m12_trace.py`
- **Known limitations：** 属离线任务，不在运行时关键路径上；失败被隔离，不影响主循环；需要模型调用时走主循环凭据。
- **后续依赖：** 无下游。

---

## 工程强化

以下不是产品里程碑，而是对已实现能力的重构与加固，按引入顺序编号。

### P1 — Turn Runtime 边界与并发契约

- **Status：** completed
- **Implementation：** `core/turn.py`（`TurnContext` + `TurnManager`：主 turn single-flight、FIFO 排队、可取消）、`services/turn_orchestrator.py`（单轮流水线）、`POST /api/turns/cancel`
- **Tests：** `backend/tests/test_turn_manager.py`、`test_turn_concurrency.py`、`test_turn_identity_events.py`、`test_turn_cancel_api.py`、`test_turn_no_duplicate_query.py`、`test_tool_event_isolation.py`
- **Known limitations：** 这是「可靠的单飞主 Agent + 明确排队」，不是多用户并发 Agent Server。契约细节见 `architecture.md` 的并发契约一节。
- **后续依赖：** P2 的 Trace 以 `turn_id` 为主键。

### P2 — Trace 与可观测性

- **Status：** completed
- **Implementation：** `trace/model.py`、`trace/store.py`（`turn_traces` 表）、`trace/recorder.py`、`trace/redact.py`（统一脱敏）、只读 API `/api/traces`、前端 `/debug` 视图
- **Tests：** `backend/tests/test_trace_store.py`、`test_trace_api.py`、`test_trace_redact.py`、`test_trace_turn.py`
- **Known limitations：** Trace 存的是标识、摘要预览与计数，不是完整 prompt；默认脱敏后才是 safe-to-inspect，不要往里塞原文。
- **后续依赖：** 无下游；是调试与未来 eval 的事实来源。

### P3 — Context 预算、衰减策略与 Eval 框架

- **Status：** completed
- **Implementation：** `services/token_budget.py`、`services/decay.py`、`services/params.py`、`agent/eval/`（`topic_eval.py`、`retrieval_eval.py`、`run.py`）、数据 `backend/evals/`、基线 `backend/evals/baseline.json`
- **Tests：** `backend/tests/test_token_budget.py`、`test_decay.py`、`test_eval_regression.py`
- **Known limitations：** 评测集是小型、确定性、离线数据集，指标用于**防退化**，不代表生产质量；它不在运行时关键路径上，跑评测失败不应阻塞对话功能。
- **后续依赖：** 调整 topic/retrieval 阈值前应重跑并对比基线。

### P4 — 工具能力安全、沙箱与执行策略

- **Status：** completed
- **Implementation：** `tools/policy.py`（能力分级 + 指纹）、`tools/sandbox.py`（按策略收紧）、`tools/approval.py`（能力展示）、`services/tool_router.py`（缓存与条件暴露）
- **Tests：** `backend/tests/test_tool_policy.py`、`test_computer_sandbox.py`、`test_tool_router_cache.py`、`test_tool_router.py`
- **Known limitations：** 见 `architecture.md` 的沙箱安全契约；扩大能力必须重新审批，不能沿用旧授权。
- **后续依赖：** M10 的工具创建流程依赖这一层的策略判定。

### P5 — Provider 边界、依赖锁定与 CI

- **Status：** completed
- **Implementation：** `adapters/errors.py`、`adapters/base.py`（`finish_reason`）、`backend/uv.lock`、`.github/workflows/ci.yml`、`scripts/setup_env.ps1`
- **Tests：** `backend/tests/test_adapter_contract.py`、`test_adapter_errors.py`、`test_embedding_identity.py`；CI 本身在 push / PR 上执行
- **Known limitations：** 后端本地只在 Python 3.12 验证过，3.11 由 CI 矩阵验证；CI 只对 Rust 做 `cargo check`，不产出完整 Tauri 安装包。
- **后续依赖：** 无下游。

---

## 尚未完成

这些是最容易让后续 Agent 误判的地方，明确列出来：

- **斜杠命令体系**：未实现（工具创建入口在设置页与对话输入区）。
- **对话页内嵌的记忆/知识面板**：未实现，面板在星球页详情里。
- **片段级检索偏置**：从星球页「从这里开始」选中片段，目前只改变提示词（注意力偏置），
  不改变记忆检索的排序权重；检索侧只实现了话题级亲和（`anchor_topic_id`）。
- **记忆的类别化衰减**：只对已有可靠元数据（知识条目 vs 片段）做差异化；未引入模型生成的记忆分类字段。
- **多用户/多会话并发 Agent Server**：明确不做。当前是单机、单用户的 single-flight 主 turn。
- **非 Windows 平台**：keyring 与桌面壳只在 Windows 验证，Linux/macOS 未验证。

---

## 核对方法

文档里的状态可以自己复核，不需要信任本文件：

```powershell
# 1. 文档一致性检查（里程碑状态、被引用的路径与命令是否存在、是否出现硬编码数字）
python scripts/check_docs.py

# 2. 后端测试（uv 环境）
cd backend
uv run --frozen pytest

# 3. 评测基线（离线、确定性）
uv run --frozen python -m agent.eval.run

# 4. 前端
cd ..\frontend
npm ci
npx vue-tsc --noEmit
npm test
```

命令与 CI 一致，见 `.github/workflows/ci.yml`。
