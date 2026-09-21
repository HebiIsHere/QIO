# Tool Terminal State Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让主 Turn 中「服务器已经知道的工具最终结果」在 `TOOL_END` 丢失后仍能通过 runtime snapshot + reconciliation 恢复到前端；`unknown` 只用在本机/服务端都真的不知道结果时。

**Architecture:** 后端新增一个进程级、纯内存、有界的轻量权威状态 `ToolExecutionState`（active + recent terminal tool executions）。`AgentLoop` 在工具开始/结束时写入它，`GET /api/runtime/state` 的 `tools` 字段直接由它生成；`EventBus` 仍然只是实时通知渠道。前端把工具卡的真实语义扩展成 `running / success / failed / cancelled / unknown`，RESYNC 时按 `tool_call_id` 用快照核对本地卡片：服务器有终态就以服务器为准，服务器还在跑就保持运行中，两边都不知道才收口为「结果未收到」。

**Tech Stack:** Python 3.12 + FastAPI + pytest（`uv run --frozen pytest`），Vue 3 + Pinia + Vitest（`npx vitest run` / `npx vue-tsc --noEmit`）。

**Spec:** 用户本轮任务书《QIO Tool 最终状态可恢复修复任务》（33 节，含 10 组专项测试与 16 条验收条件）。本计划是它的实现展开，验收以任务书为准。

**执行状态（2026-09-21）：** 六个 Task 全部按 TDD 走完（先写失败测试 → 看它失败 → 实现 → 全绿）。
后端 `uv run --frozen pytest` 全绿、前端 `npx vitest run` / `npx vue-tsc --noEmit` / `npm run build` 全绿、
`python scripts/check_docs.py` 通过；工具卡五种语义做了真实页面的截图核对
（`frontend/e2e-shots/ui-catalog/toolrec/`，该目录不进仓库）。

## Global Constraints

- EventBus 保持有界内存；不新增磁盘级完整事件日志；RESYNC buffer 仍只在内存里。
- 只维护 active + recent terminal Tool execution；不保存完整 Tool 输出；不建设第二套永久历史。
- Tool 身份必须用 `tool_call_id`，并同时携带 `turn_id` 做一致性校验（不得跨 Turn 串状态）。
- Subagent 内部 Tool（`turn_id = subagent:*`）不进主 UI / 主 snapshot；Subagent 只恢复 Task 级状态。
- `unknown` 只在服务器真的无法确认结果时使用；`cancelled` 不得混成 `failed`。
- active Turn 的 terminal Tool state 不得被 TTL / max-count 提前清理。
- 不改动 Memory Selector / Markdown Streaming / Session Pagination / ModelUsage / Output Token Policy / Adapter Pool / Credential / Maintenance / Approval / Planet / Topic·Fragment。
- 日志、事件、错误信息不得出现密钥原文；新增输出路径必须过 `agent/trace/redact.py`（本计划只输出工具名 / call_id / 截断错误摘要，无凭据内容）。

---

## File Structure

| 文件 | 责任 |
| --- | --- |
| `backend/src/agent/core/tool_state.py`（新建） | 权威的轻量 Tool execution 状态：写入、retention、snapshot 投影 |
| `backend/src/agent/core/loop.py`（改） | 工具 start/end 时写入权威状态；`active_tools()` 读它；TOOL_END 带 `status` |
| `backend/src/agent/tools/registry.py`（改） | 取消路径在 tool/end 上显式标记 `cancelled`，不把 cancelled 说成普通失败 |
| `backend/src/agent/services/app.py`（改） | 进程级 `ToolExecutionState` 实例 + `tool_executions()`（排除 subagent/内部 turn） |
| `backend/src/agent/services/turn_orchestrator.py`（改） | 主 Turn 的 `AgentLoop` 注入进程级状态 |
| `backend/src/agent/api/server.py`（改） | `/api/runtime/state.tools` 改为终态可恢复的执行事实 |
| `backend/tests/test_tool_state.py`（新建） | 状态容器语义 + retention（任务书测试 10） |
| `backend/tests/test_tool_recovery.py`（新建） | 端到端恢复：TOOL_END 丢失 → snapshot 仍有终态（测试 1-9） |
| `frontend/src/services/api.ts`（改） | `tools` 快照类型带上 status / error_summary |
| `frontend/src/stores/session.ts`（改） | `toolStatus` 真实语义 + `reconcileTools()` 核对 |
| `frontend/src/stores/events.ts`（改） | TOOL_END 传 status；RESYNC 应用快照时核对工具卡 |
| `frontend/src/components/MessageItem.vue`（改） | 区分「运行中/成功/失败/已取消/结果未收到」的文案与 `data-state` |
| `frontend/src/stores/__tests__/toolRecovery.test.ts`（新建） | 前端核对语义（测试 1-9 的前端侧） |
| `docs/status.md`（改） | 记录本轮保证 + 两条接受的限制 |

---

### Task 1: 权威 Tool execution 状态容器

**Files:**
- Create: `backend/src/agent/core/tool_state.py`
- Test: `backend/tests/test_tool_state.py`

**Interfaces:**
- Produces:
  - `ToolExecutionState(ttl_seconds=1800.0, max_records=200, clock=None)`
  - `.start(turn_id, tool_call_id, tool_name) -> dict`（写入 running，返回记录投影）
  - `.finish(turn_id, tool_call_id, status, tool_name=None, error=None) -> dict | None`（`status ∈ {success, failed, cancelled}`）
  - `.snapshot(active_turn_id=None) -> list[dict]`（running + recent terminal；非 active turn 的 running 投影成 `unknown`）
  - `.prune(active_turn_id=None) -> None`
  - 记录形状：`{turn_id, tool_call_id, tool_name, status, started_at, ended_at, error_summary}`

- [ ] **Step 1: 写失败测试**（写入 running→terminal、快照投影、`unknown` 只给非 active 的 running、active turn 的 terminal 不被 prune、TTL/max 回收、同 call_id 覆盖更新、跨 turn 同名不串）
- [ ] **Step 2: 运行 `uv run --frozen pytest tests/test_tool_state.py -v`，确认失败**
- [ ] **Step 3: 实现 `tool_state.py`**
- [ ] **Step 4: 重跑，全绿**

### Task 2: AgentLoop 写入权威状态 + TOOL_END 带 status + cancelled 语义

**Files:**
- Modify: `backend/src/agent/core/loop.py`
- Modify: `backend/src/agent/tools/registry.py`
- Test: `backend/tests/test_tool_recovery.py`

**Interfaces:**
- Consumes: Task 1 的 `ToolExecutionState`
- Produces:
  - `AgentLoop(..., tool_state: ToolExecutionState | None = None)`（缺省时自建，保持现有单机构造可用）
  - `AgentLoop.tool_executions() -> list[dict]`（本 loop 的权威记录投影）
  - `AgentLoop.active_tools() -> list[dict]`（保持旧形状 `{turn_id, tool_call_id, tool_name}`，只含 running）
  - `TOOL_END` 事件载荷新增 `status`（`success | failed | cancelled`）

- [ ] **Step 1: 写失败测试**：工具成功/失败/取消后 `loop.tool_executions()` 的 status 与 error_summary；`TOOL_END.data["status"] == "cancelled"` 的取消用例
- [ ] **Step 2: 运行并确认失败**
- [ ] **Step 3: 实现**（loop 在 `_on_pipeline_start` / `_on_pipeline_end` 写入；registry 取消路径标 `cancelled=True`）
- [ ] **Step 4: 重跑，全绿**（含既有 `test_tool_event_payload.py`、`test_active_tools.py`）

### Task 3: runtime snapshot 暴露可恢复的 Tool 执行事实

**Files:**
- Modify: `backend/src/agent/services/app.py`（`self.tool_state`、`tool_executions()`）
- Modify: `backend/src/agent/services/turn_orchestrator.py`（注入）
- Modify: `backend/src/agent/api/server.py`（`tools` 字段）
- Test: `backend/tests/test_tool_recovery.py`（HTTP 层）

**Interfaces:**
- Consumes: Task 2 的 `AgentLoop.tool_state`
- Produces: `GET /api/runtime/state.tools = [{turn_id, tool_call_id, tool_name, status, started_at, ended_at, error_summary}]`，排除 `subagent:*` 等内部 turn

- [ ] **Step 1: 写失败测试**：主 Turn 工具跑完后 `/api/runtime/state` 仍能看到它的终态；`subagent:*` 的内部工具不出现
- [ ] **Step 2: 运行并确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 重跑，全绿**（含 `test_runtime_state.py`）

### Task 4: 前端工具卡真实状态 + RESYNC 核对

**Files:**
- Modify: `frontend/src/services/api.ts`
- Modify: `frontend/src/stores/session.ts`
- Modify: `frontend/src/stores/events.ts`
- Modify: `frontend/src/components/MessageItem.vue`
- Test: `frontend/src/stores/__tests__/toolRecovery.test.ts`

**Interfaces:**
- Produces:
  - `StreamMessage.toolStatus?: "running" | "success" | "failed" | "cancelled" | "unknown"`
  - `session.reconcileTools(records: ToolSnapshotEntry[])`
  - 优先级：server terminal > 本地 running；snapshot 之后到达的实时事件 > snapshot；不得跨 turn / 跨 call_id

- [ ] **Step 1: 写失败测试**（服务器知道 success/failed/cancelled、服务器说还在跑、两边都不知道→unknown、同名多次调用、跨 Turn 不串、快照+buffered TOOL_END 顺序）
- [ ] **Step 2: 运行 `npx vitest run src/stores/__tests__/toolRecovery.test.ts` 确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 重跑 + `npx vitest run` + `npx vue-tsc --noEmit`**

### Task 5: 文档记录设计边界

**Files:**
- Modify: `docs/status.md`

- [ ] **Step 1: 新增本轮条目**（保证 + 两条接受的限制 + 已知限制）
- [ ] **Step 2: `python scripts/check_docs.py`**

### Task 6: 全量验证

- [ ] `cd backend; uv run --frozen python -m compileall src`
- [ ] `cd backend; uv run --frozen pytest`
- [ ] `cd frontend; npx vitest run`
- [ ] `cd frontend; npx vue-tsc --noEmit`
- [ ] `cd frontend; npm run build`
- [ ] 未跑到的项在最终报告里写 `NOT RUN` + 原因
