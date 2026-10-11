# 整合轮冻结契约（2026-10-11 可靠性 × 真实模型）

整合分支：`integrate/reliability-interactive-real-model-20261011`
整合起点：`origin/main` = `8fa845255c118a7104b1de8e37298ca745924482`
来源一：`fix/main-audit-reliability-8fa8452` = `4890870eddfa6f0f15e600bbbeecc03740714ff7`（合并提交 `1b9cccd`，无冲突）
来源二：`feat/interactive-real-model` = `7158152d84b0b24cac5156f4862a59e22f204dc4`（合并提交 `c6a03b1`，三处冲突按行为重组）
整合基线（本轮所有子工作树的起点）：`c6a03b17423ccda606f72e1e81e3a610fd8e1d43`

本文件是**接口冻结**：改它要主智能体同意。这里只写跨模块必须一致的东西，不写实现细节。

---

## 1. 稳定提交请求身份（缺口一）

### 1.1 语义（不可协商）

1. **一次提交请求有稳定身份**：由前端在发出提交**之前**生成，随请求送到服务端，落库可持久恢复。
2. **同身份重试返回原事实**：不重复创建提交、不重复创建理解请求、不重复调用模型。
3. **同身份但内容不同必须拒绝**：不得把另一个窗口/另一次内容当成同一次提交的受理结果。
4. **查证只认精确匹配**：时间接近、最近一条理解记录、板面出现新任务、板面变化都**不能单独**作为受理证据。
5. **查不到 / 查询失败继续「结果未知」**：保留原内容与勾选，不盲目重发、不改判失败。

### 1.2 服务端

* 迁移 **39** 给 `board_submissions` 追加：`client_request_id TEXT NOT NULL DEFAULT ''`，并加唯一索引
  `idx_board_submissions_client_request`（`board_id, client_request_id`，`client_request_id <> ''` 的部分索引）。
  补偿入口切到 **40**（= 38 的全部语句 + 39 的新对象），`REQUIRED_OBJECTS` 同步列出新列与新索引。
  **禁止改历史迁移 37/38 之外已交付的内容；37/38 原样保留。**
* `submission.submit_board(..., client_request_id: str = "")`（B 负责）：
  * 同 `(board_id, client_request_id)` 已存在记录 → 直接返回**原记录**的事实
    （`submission` = 原行、`delivery` = 原 delivery、`understandings` = 原理解请求），
    `replayed: true`，不落新行、不建新理解请求、不启动后台理解；
  * 同 `(board_id, client_request_id)` 但 `base_state_version` 或 `note` 与记录不一致 →
    按冲突拒绝（HTTP 409，`error="request_identity_conflict"`），**不落新提交**、不改基准、不调用模型；
  * 空字符串 = 旧客户端，行为与今天完全一致（不参与幂等）。
* `submission.acceptance_lookup(conn, board_id, client_request_id)`（B 负责）：
  按**精确身份**返回 `None` 或事实字典，不做时间/最近推断。
* API（B 负责，在 `backend/src/agent/api/interactive_store.py`）：

  ```
  POST /api/interactive/boards/{board_id}/submissions        （既有路由）
       body 增加可选 "clientRequestId": str
  GET  /api/interactive/boards/{board_id}/submissions/acceptance?clientRequestId=<id>
       200 {"acceptance": {...}} | 404 {"acceptance": null, "reason": "..."}
  ```

  查证响应形状（冻结，D/A 都按它写断言）：
  ```json
  {
    "acceptance": {
      "clientRequestId": "…",
      "submissionId": "…",
      "status": "succeeded",
      "delivery": {"delivered": true, "status": "accepted", "understandingId": "…", "reason": null, "detail": null},
      "understandingId": "…",
      "understandingStatus": "accepted",
      "baseStateVersion": 12,
      "note": "…",
      "createdAt": "2026-10-11T12:00:00+00:00",
      "replayed": false
    }
  }
  ```

  查不到时 `404`，body 为 `{"acceptance": null, "reason": "…"}`——**不是**错误推导的成功。

### 1.3 前端（A 负责）

* `frontend/src/interactive/types.ts`：`SubmissionResult` 增加可选 `replayed?: boolean`；
  `SubmissionRecord` 增加可选 `clientRequestId?: string`；
  新增 `AcceptanceLookup`（与上面 JSON 一一对应）。
* `frontend/src/services/interactive.ts`：
  * `submitBoard(boardId, payload, baseStateVersion, confirmedCheckId, clientRequestId)`
    —— 末位新增可选参数，body 带 `clientRequestId`；
  * `fetchAcceptance(boardId, clientRequestId): Promise<{ acceptance: AcceptanceLookup | null; reason?: string }>`
    —— 404 时**不抛**，返回 `acceptance: null`。
* `frontend/src/interactive/submission.ts`：请求身份的纯函数（生成/校验/是否可用于查证），A 定义并导出。
* 查证路径唯一：`confirmAcceptanceAfterUnclear` 必须改为
  「有本次 `clientRequestId` → `fetchAcceptance` 精确匹配 → 命中才认受理」。
  **删掉按 `createdAt` 接近 + 排除上次 submissionId 的认领逻辑。**

---

## 2. 理解后台任务的统一生命周期（缺口二）

* 生产路径必须经 `AppContext.background`（`agent.services.background.BackgroundTasks`）登记，
  名字固定 `interactive-understanding:<understandingId>`，并提供**底层执行单元探针**
  （理解记录是否已到终结状态）。不得再只挂在 `submission._BACKGROUND_TASKS` 上。
* 关闭语义复用既有注册表：进入关闭后 `register` 抛 `BackgroundClosed` → 调用方**拒绝新调度**并如实说明；
  有界等待/取消由 `BackgroundTasks.shutdown` 负责，**不准用固定 sleep 假定任务已退出**。
* 关闭结果进 `AppContext.aclose()` 的 `CloseReport`：新增阶段名
  `understanding_stopped` / `understanding_unfinished`（C 负责加在 `services/lifecycle.py`）。
* 关停/重启中断的理解：理解记录必须有明确状态（`interrupted` 或既有 `failed` + 可读原因），
  可从原快照恢复；旧实例 / 旧 attempt 的迟到结果**不得**写入当前记录、不得重复生成意图。
* 没有应用上下文的测试兜底（裸 `create_task`）允许保留，但生产路径必须走注册表。

---

## 3. 冲突处理后的既有行为（不得回退）

* 提交请求与提交后读取分开：明确成功后刷新失败 = 「提交成功，页面刷新失败」，保留受理事实与身份，
  给只读重试入口，绝不自动重复提交。
* 未拿到回执 = `submitStatus='unknown'` + `submitResultUnknown=true`；`SubmitCluster.vue` 的类型、
  合成状态、按钮与文案对 `unknown` 必须一致。
* 勾选代次保护（`checkedAtSubmit`）、草稿逐键三态协议、删除冲突选择、服务器版本保护、
  任务清单读取失败保留最后成功清单并标过期风险、真实执行记录随清单一起接收——全部保留。
* 单项/批量审批：防重复锁在**核验之前**取得、`finally` 释放、核验失败不放行、成功后采纳真实执行记录。
* `IntentStatusPopover.vue` 同时保留清单刷新失败提示 + 只读重试，与真实执行进度/暂停/继续/取消/终态。
* `ChatDock.vue` 同时保留材料/附件入口与未决发送处理；受理情况未知时不得自动放回原文重发。
* 文档：可靠性 **P34** 保留，真实模型项编号 **P35**；`docs/status.md` 三处里程碑状态由 `scripts/check_docs.py` 校验。
  「尚无真实模型人工验收」的限制必须保留，不得把接口接通/mock 验证说成真实模型验收完成。

---

## 4. 编号与所有权

* 下一个可用迁移号：**39**；补偿入口：**40**（`storage/schema.py` 的 `COMPENSATION_VERSION`）。
  B 独占；别人需要迁移必须先协调。
* 文件所有权（共享文件不得并发覆盖）：

| 范围 | 所有者 |
| --- | --- |
| `backend/src/agent/api/interactive_store.py`、`backend/src/agent/storage/{schema,migrate}.py`、`backend/src/agent/interactive/understanding.py` | B |
| `backend/src/agent/interactive/submission.py`：**按函数分** —— `submit_board` / `acceptance_lookup`（含 client_request_id 幂等与查证）归 B；`_schedule_understanding` / `schedule_understanding` / `cancel_background_understanding` 及理解关闭接线归 C；两边都不许改对方函数的语义，Lead 按函数粒度集成 | B + C |
| `backend/src/agent/services/{app,lifecycle}.py`、`backend/src/agent/interactive/execution.py`、`backend/src/agent/services/derived_tasks.py` | C |
| `frontend/src/interactive/{types,submission}.ts`、`frontend/src/services/interactive.ts`、`frontend/src/stores/interactive.ts`、`frontend/src/components/interactive/*.vue` | A |
| `.github/workflows/*`、`scripts/targeted-verify/**`、`docs/verify-ledger-integration-20261011.md` | D |
| `scripts/irm-int-verify/**`、`docs/integration-review-20261011.md`、视觉与迁移证据 | E |
| `docs/interactive-mode-contract.md`、`docs/status.md`、本文件、最终集成与全量验证 | Lead |

* 子智能体禁止：无筛选 pytest、全前端 vitest、全仓评测、完整 CI、`npm ci` / `uv sync` 全量重装。
  类型检查 `npx vue-tsc --noEmit` 允许（定向验证必需），但同一 worktree 内不要重复跑。

## 5. 验证额度（全程共用）

* **本地全量只允许一轮**，**全量 CI 只允许一次**，均由主智能体在冻结候选 SHA 上执行。
* 开发期只做定向验证；失败后只重跑**失败用例/node id**，不重跑已通过集合。
* D 负责提供可指定 SHA、指定失败用例、指定失败 CI 项的定向入口，并维护验证台账。
