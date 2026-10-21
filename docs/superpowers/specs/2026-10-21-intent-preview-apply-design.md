# 2026-10-21 意图预演与批准即落地（P73 · Stage 1）

- 基线：tag `v0.2.10`（commit `0c326d45`），分支 `feat/interactive-intent-preview`。
- 状态：Stage 1 实现中；Stage 2 事项见 §8。
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

## 2. 非目标（Stage 2，见 §8）

- 普通对话轮读板面（不依赖提交绑定）。
- 对话请求与「提交给 QIO」完全解耦（免提交提案 / 执行）。
- 无 operations 意图确认后的自动续作（本阶段仍走既有续作路径）。
- 在预演层直接新建关系 / 分组的 UI（本阶段只渲染关系与组，不支持预演上连线）。

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
- 命令：
  - `cd backend; & 'D:\qio-dev\qio\backend\.venv\Scripts\python.exe' -m pytest tests/test_p3_plan_preview_apply.py -q`
  - `cd frontend; npx vitest run <受影响测试文件>`
  - `npx vue-tsc --noEmit`（类型检查，不是全量测试）

## 8. Stage 2 事项（本阶段不做，已记录）

1. 普通对话轮可读板面（只读绑定，无需提交）。
2. 对话请求与「提交给 QIO」解耦（免提交提案 / 执行）。
3. 无 operations 意图确认后的自动续作（architecture §18.1 写的「自动续作」接线）。
4. 预演层直接建立关系 / 分组（本阶段只渲染）。
5. 预演编辑的内容编辑（改卡片正文），本阶段只支持位置与删除。
