# AppContext 拆分与 Agent Trace 设计（含职责审计）

日期：2026-09-10
状态：Trace 已实现；AppContext 拆分设计待执行

## 1. 第一阶段：AppContext 职责审计

现状：`backend/src/agent/services/app.py` 共 1124 行、~40 个方法。逐方法职责归类：

| 方法 | 责任域 | 生命周期 |
|---|---|---|
| `__init__` | dependency wiring / tool bootstrap / restored tools / service 注册 | **process** |
| `_build_embedding_backend` | model/provider creation（本地 ONNX） | process |
| `_restore_tools` | restored tool registration | process |
| `resolve_main_ref` / `_loop_max_iterations` / `_loop_token_budget` | credential/config 解析 | process |
| `build_adapter` / `build_adapter_for_credential` | model/provider creation | process |
| `_build_tool_lifecycle` | tool bootstrap（dev workflow） | process |
| `_refresh_selector` | memory（selector 刷新） | process |
| `_user_root_id` / `_topic_entity_ids` / `_entity_card_topics` | graph/entities | process |
| `current_topic` / `_ensure_default_topic` | graph/topic | process |
| `session_messages` / `_move_message` | memory | process |
| `_route_tools` | turn orchestration（工具路由） | turn（每轮） |
| `_record_tool_call` | trace/audit（tool trace） | turn |
| `_publish_anchor_event` / `_on_tool_anchor_result` / `_publish_turn_queue` | notification（SSE） | 事件驱动 |
| `_format_notice` / `_handle_subagent_notify` / `_execute_notify_turn` | subagent / notification | turn（系统 turn） |
| `run_turn` | turn orchestration（facade） | turn |
| `_execute_turn` | **turn orchestration + context + memory + knowledge + topic 混杂** | turn |
| `_focus_block` / `_short_term_items` / `_topic_note` / `build_injection` | context injection | turn |
| `_close_fragment` / `_extract_knowledge` / `consolidate` | fragment lifecycle / knowledge / memory | turn（post-turn） |
| `_knowledge_snapshot_provider` | knowledge | turn |

**结论**：process-lifetime 职责（wiring / provider / tool bootstrap / 长期服务）与 turn-lifetime 职责（编排 / 注入 / post-turn 生命周期）**混在同一类**，且 `_execute_turn` 一个函数里混合了 6+ 个领域细节。

## 2. 第二阶段：目标模块边界

```
AppContext / RuntimeServices   —— 长生命周期依赖（保留为 composition root / facade）
TurnManager                    —— turn 生命周期（已实现：single-flight + FIFO + cancel）
TurnOrchestrator               —— 单轮编排（begin→context→loop→persist→post-turn→finish）
ContextAssembler               —— context 构造（short-term / focus / topic_note / entity / injection）
MemoryLifecycle                —— fragment close / summary / knowledge extraction / consolidation
AgentLoop                      —— planning/tool/observing 状态机（已实现）
```

理想 `run_turn`：`begin turn → build context → execute loop → persist/commit → post-turn lifecycle → finish turn`。

**执行状态**：
- ✅ `TurnManager`、`AgentLoop`（已有）
- ✅ `ContextAssembler`（`agent/services/context.py`）：focus / short-term / topic note / entity / injection 已迁出，AppContext 保留薄封装
- ✅ `MemoryLifecycle`（`agent/services/memory_lifecycle.py`）：fragment close / knowledge extraction / consolidation 已迁出
- ✅ `TurnOrchestrator`（`agent/services/turn_orchestrator.py`）：`_execute_turn` 已拆成具名阶段 `begin → build_context → execute_loop → persist → post_turn → finish`
- AppContext 从 **1124 行降到 713 行**（−37%）

## 3. 第三～七阶段：Agent Trace（已实现）

### 数据模型（`agent/trace/model.py`）
- Identity：turn_id / started_at / ended_at / duration_ms / status
- Topic：initial / predictor_backend / scores / suspected_new / final / operation
- Context：items(surface,item_id,tokens,score,preview) / total_tokens / budget(truncated,needs_consolidation) / dropped
- Model calls：seq / adapter_mode / model / input+output tokens / latency / tool_calls / error
- Tools：call_id / tool / args_preview / ok / error / duration / policy / result_preview
- Writes：messages / fragments_closed / summaries / knowledge / supersedes
- Warnings：code + message

默认**不保存大段原文**：只存 item id、sanitized preview、token 数、分数/原因。

### 脱敏（`agent/trace/redact.py`）
覆盖 API key（sk-/pk-/rk-）、Authorization、Bearer、cookie、`QIO_KEY_*`、password/secret/token/key 字段、工具 schema 声明的 secret 参数。`total_tokens` 等计数字段不受影响。Trace 默认 safe-to-inspect。

### 存储（`TraceStore` + migration v10）
单表 `turn_traces`（JSON 列，匹配 QIO 现有 SQLite 风格），索引 `started_at`、`status`；读端容忍损坏 JSON。

### API
- `GET /api/traces?limit&offset`（列表 + total，不含重字段）
- `GET /api/traces/{turn_id}`（完整、已脱敏）
- `GET/PUT /api/settings/trace`（开关）

### Viewer
`/debug` 最小可折叠视图：列表 + 详情（Topic / Context / Model / Tools / Writes / Warnings / Output），遵循现有设计规范（tokens、mono、衬线标题）。

## 4. 待办
- AppContext 拆分（TurnOrchestrator / ContextAssembler / MemoryLifecycle）
- notify turn 的 trace 记录
