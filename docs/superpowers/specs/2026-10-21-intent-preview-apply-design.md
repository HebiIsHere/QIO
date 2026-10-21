# 2026-10-21 意图预演与批准即落地（P73 · Stage 1 + Stage 2）

- 基线：tag `v0.2.10`（commit `0c326d45`），分支 `feat/interactive-intent-preview`。
- 状态：Stage 1 + Stage 2 实现完成（定向验证 + 测试实例真实链路；未跑全量 / CI）。
- 阶段：Stage 1 = 预演 + 预演编辑（位置 / 删除）+ 批准即落地；Stage 2 = 对话接入板面、
  免提交提案、无计划意图自动续作、预演成组 / 连线、预演正文编辑（§8）。
- 关联分析：0.2.9 旧链（独立理解模型 + 单 JSON preview/operations + execution.py 原子落地）
  在 0.2.10 P71 被整体退役，新 `IntentSpec` 只允许目标 / scope，确认只写授权、执行靠
  下一次消息续作 —— 视觉预演与「批准即落地」因此丢失（见 `docs/qio-refactor-contract.md`
  §13.1/§13.3/§13.10、`docs/architecture.md` §18.1）。

## 1. 目标（Stage 1）

1. 意图提案可携带**结构化操作计划 operations**；服务端派生 **preview**（虚线幽灵卡 /
   组框 / 关系线）并在板面渲染。
2. 预演可编辑：拖动幽灵卡改落点、删除计划中的新增卡；编辑只允许发生在未批准意图上。
3. **批准即落地**：confirm 在同一请求内原子应用计划（卡 / 组 / 关系一次成型），
   写操作账本与执行记录；不再需要用户再发一条消息。
4. 工作台与板面两个批准入口都触发落地并刷新板面。
5. 失败不半落地；已确认的任务不能再改预演。

## 2. 两个阶段的边界

- Stage 1（已完成）：预演 + 预演可编辑（位置 / 删除）+ 批准即原子落地。
- Stage 2（已完成，见 §8）：普通对话轮读板面（board 模式，无需提交）、免提交提案、
  无操作计划意图确认后由服务端自动续作、预演层成组 / 连线、预演正文编辑。
- 不在本设计内：把旧 `execution.run_execution` 重新接回主循环；桌面壳 / 安装包改动。

## 3. 数据模型

- 复用 `board_intents.preview` / `board_intents.operations` 既有列（0.2.9 已存在，无需迁移）。
- `operations`：仅允许 `models.ALLOWED_INTENT_OPS`：
  `add_reply_card / add_text_card / move_card / group_cards / order_group / add_link`；
  add 类必须带非空正文；单意图最多 20 条。
- `preview`：**只由 operations 派生**（`execution.preview_from_operations`），
  与真实落地共用 `placement.plan_card_placement`，预览坐标 = 落地坐标。
- `scope`：有 operations 时由服务端从 operations 派生
  （allowed_actions / max_new_cards / max_new_groups / max_new_links），
  无 operations 时保持模型输入。
- `authorization`：confirm 时额外记录 `board_seq`（落地前板面版本 CAS 依据）。
- 账本：落地时为每条 op 写一行 `board_operations`（status=applied，result 含真实 id）。
- 执行记录：`board_executions` 一行（status=done，plan/steps），供审计与结果卡引用。
- 意图 `applied`：记录 `cardIds/groupIds/linkIds` 与 `previewIdMap`。

## 4. 工具与 API

### 4.1 `board_propose_intents` 扩展

`intents[].operations?: object[]`（可选，向后兼容）。服务端：
- 校验 op 白名单、形状、add 类非空正文、数量上限；
- 有 operations 时派生 scope、派生 preview 并随意图持久化；
- 无 operations 时行为与 0.2.10 完全一致。

### 4.2 读取

- `GET /api/interactive/boards/{board_id}/intent-views` 与
  `GET /api/interactive/boards/{board_id}/intents` 返回 `intent.preview` 与
  `intent.operations`。

### 4.3 编辑预演

`PATCH /api/interactive/intents/{intent_id}/plan`

```
body: { boardId: string, operations: object[] }
resp: { ok: true, intent: <intent view> }  // 含 preview / operations
```

- 仅 `proposed` / `awaiting_confirmation` 可改；`approved/running/completed` 返回 409
  `{ error: "not_editable", detail } `。
- 服务端重新校验 ops → 重算 scope 与 preview → version+1、清空 authorization 后持久化。
- 拖动：前端把对应 add op 的 `x/y` 或 `place` 改为新坐标。
- 删除幽灵卡：前端移除该 add op，以及引用它的 group/link op。

### 4.4 批准即落地

`POST /api/interactive/intents/{intent_id}/confirm`、
`POST /api/interactive/intents/batch-confirm`：先写授权（同现有语义），
若意图 operations 非空，则在**同一请求内**执行：

1. 重新校验 status / plan / authorization；
2. 校验当前板面 `seq` 与授权记录的 `board_seq` 一致，否则返回
   `{ ok: false, reason: "board_changed" }`（意图保持 awaiting_confirmation，不写库）；
3. 单事务应用全部 ops：`board_store.write_board_cas` → 写账本 / 执行记录 →
   意图 `completed` + applied + finished_at；
4. 任一 op 失败整体回滚（意图保持 approved，可重试 / 放弃），返回结构化原因；
5. 已 completed 的意图重复确认返回 done，不重复落地。

响应新增：`applied: {cardIds, groupIds, linkIds}`、`execution: {id, status, steps}`。

## 5. 前端

- 板面预演层（`BoardCanvas.vue` / `BoardPreviewLayer.vue` / `IntentStatusPopover.vue`）
  识别新状态：`proposed / awaiting_confirmation / approved / completed`；
  `previewIntents` 过滤加入未批准 + 已确认未落地状态。
- 批准入口：
  - 板面 popover 批准新状态意图 → 调新 `confirmSingleIntent`（`services/interactive.ts`），
    成功后刷新 intents 与 board state（预览消失、卡出现）；
  - 工作台 `IntentWorkbench.vue` confirm 成功后同样刷新板面（事件或共享 store）。
- 预演可编辑：
  - 拖动幽灵卡 → 计算板面坐标 → `PATCH plan`（改 op 的 x/y）；
  - 幽灵卡上的删除按钮 → 移除该 add op 及依赖的 group/link op → `PATCH plan`。
- 类型：`frontend/src/interactive/types.ts` 的 Intent 增加 preview/operations 与新状态。

## 6. 安全与一致性

- 只有未批准意图可编辑；批准后编辑必须放弃 / 重新提案（409）。
- 落地使用 CAS：授权记录 board_seq，落地前核对；`write_board_cas` 单事务。
- 计划只含允许操作；add 类正文非空（不依赖模型二次生成）；上限 20 条。
- 重复确认幂等；失败整体回滚；账本 / 执行记录完整。
- 不修改旧 `execution.py` 的既有路径，只新增可复用的计划 / 应用函数。

## 7. 定向测试计划（不做全量 / 不跑 CI）

- `backend/tests/test_p3_plan_preview_apply.py`：
  1. 带 operations 的提案 → preview/scope 派生、状态 awaiting_confirmation；
  2. confirm → 板面新增卡/组/关系、intent completed、账本 applied、执行记录 done；
  3. 重复 confirm 幂等，不重复落卡；
  4. 批准后板面变化 → board_changed，未写入；
  5. PATCH plan 改位置 / 删卡 → preview 与 ops 更新；approved 后 PATCH 409；
  6. 非法 op / 空正文被拒绝。
- 前端定向 vitest：新状态过滤、批准路由、拖拽/删除参数映射。
- `backend/tests/test_p73_stage2_board_chat.py`（Stage 2）：board 模式绑定（默认 / 显式板面、
  拒绝 submissionId / 未知板面）、免提交提案与 preview、board 模式运行上下文里的
  board_propose_intents、no_operations 返回 intent + submission_id、confirm API 自动起续作轮
  （stub TurnManager 校验 reserve/activate 与 TurnContext 字段）、update_plan 的成组 /
  连线 / 正文编辑。
- 前端 Stage 2 定向：`intentPreviewEdit.test.ts` / `intentPreviewEdit.component.test.ts` /
  `boardIntentInbox.test.ts`（纯逻辑 + 组件）/ `conversationBoardMode.test.ts`。
- 命令：
  - `cd backend; & 'D:\qio-dev\qio\backend\.venv\Scripts\python.exe' -m pytest tests/test_p3_plan_preview_apply.py -q`
  - `cd frontend; npx vitest run <受影响测试文件>`
  - `npx vue-tsc --noEmit`（类型检查，不是全量测试）

## 8. Stage 2 实现（已完成，2026-10-21）

1. **普通对话轮可读板面**：`POST /api/turns` 新增 `mode: "board"`（可省略 boardId，
   默认 `board_default`）；`submission.resolve_chat_binding` 在 board 模式下拒绝携带
   submissionId，服务端核对板面存在后写 TurnContext.mode = "board" / board_id / topic。
   TurnOrchestrator 注入结构化 `interactive` 上下文块（mode / board_id / waiting），
   模式规则切换为 `PROMPTS["BOARD_CHAT_POLICY"]`；普通对话仍像普通对话一样回答。
2. **对话请求与提交解耦**：board 模式只暴露只读工具 + `board_propose_intents`
   （`tool_models.BOARD_CHAT_TOOLS`）；`board_propose_intents.submission_id` 可省略，
   没有提交身份时 `intent_service.propose_intents(submission_id="")` 正常落库并派生
   preview（resolved_question_ids 非空会被拒绝）。写操作仍由授权范围拦：没有用户确认的
   计划不能写板面。前端普通对话（`session.send`）默认携带 `{mode:"board"}`，对话视图用
   `BoardIntentInbox` 列出待确认板面意图（确认仍走 confirm，落地 / 续作结果如实转述）。
3. **无 operations 意图确认后的自动续作**：confirm / batch-confirm 的 `_apply_confirmed`
   遇到 `no_operations` 时，由服务端 `TurnManager.reserve` + `activate` 起一个续作轮
   （mode = interactive、board_intent_ids = 本次确认的意图、回落话题优先取原提交话题，
   消息前缀「互动板面自动续作」）；响应返回 `continuation: {enqueued, turn_id?}`。
   续作轮使用完整板面工具与授权范围，执行完用 `board_finish_intent` 声明结果，
   不需要用户再发一条消息。
4. **预演层建立关系 / 分组**：前端多选幽灵卡（Ctrl/⌘+点击）→「成组」追加
   `group_cards`、「连线」追加 `add_link`，经 `PATCH plan` 保存；服务端重算 preview，
   组框与虚线关系线随预演显示。只接受带稳定 ref/cardId 的新增卡（服务端落地按 ref 解析），
   重复组 / 重复关系 / 超过 20 条 / 空白正文本地拦截。
5. **预演正文编辑**：双击幽灵卡打开 Markdown 编辑器，保存 = 改对应 add op 的 content →
   `PATCH plan`；空白正文本地拦截，失败保留编辑器并返回服务端真实原因（不假成功）。
