# fix/pm-qio032-i-tasks 报告（i-tasks：任务/审批/恢复）

## 1) 分支 / worktree / commits

- 分支：`fix/pm-qio032-i-tasks`
- worktree：`D:\qio-dev\pm-qio032-i-tasks`
- 开工 SHA：`7445170d0f51756b2740eb7209d798540d8042a2`
- commits：
  - `70435dc7` fix(interactive-tasks): 任务面板空态分组计数与读取失败重试
  - `d86c5142` fix(interactive-tasks): 意图重确认与进度、部分完成、审批恢复文案
  - （报告与 #84 动作图标的小步提交见分支最新 HEAD；报告提交在最后一笔）

## 2) 文件清单

改动：
- `frontend/src/components/interactive/v2/TaskCenterPanel.vue`
- `frontend/src/components/interactive/v2/taskCenter.ts`
- `frontend/src/components/interactive/v2/IntentLayer.vue`
- `frontend/src/components/interactive/v2/intentDisplay.ts`
- `frontend/src/components/interactive/v2/RecoveryPanel.vue`
- `frontend/src/components/interactive/v2/recoverySummary.ts`
- `frontend/src/components/approval/ApprovalResultsPanel.vue`
- `frontend/src/views/__tests__/i2v2-a-task-entries.test.ts`（仅一条空态文案断言：`没有` → `暂无任务`，BRIEF #18 要求）
- `frontend/src/views/__tests__/i2v2-a-intent-read-failure.test.ts`（同上，空态文案断言改为 `暂无任务`）

新增（测试，契约文件命名 `pm032-tasks-*`）：
- `frontend/src/components/interactive/__tests__/pm032-tasks-task-center.test.ts`（13 例）
- `frontend/src/components/interactive/__tests__/pm032-tasks-intent.test.ts`（8 例）
- `frontend/src/components/interactive/__tests__/pm032-tasks-approval-recovery.test.ts`（5 例）

未改：InteractiveView.vue / SubmitCluster / BoardToolbar / BoardCanvas / store / services / docs（均非本分支范围）。

## 3) 逐项结果

| 条目 | 结果 | 证据 |
|---|---|---|
| #18 空任务只显示「暂无任务」，保留审批历史；有任务只显示有内容的组及计数 | 已修复 | `taskEmptyText()` 返回「暂无任务」；`taskCountsSummary` 只拼非零分组；空任务时空态只有一句、任务分区标签「审批结果与历史」仍在。测试：pm032-tasks-task-center「空任务只显示…」「有任务时只显示有内容的组及计数」+ 纯逻辑用例；i2v2-a-task-entries / i2v2-a-intent-read-failure 空态断言同步更新 |
| #07 同一次任务读取失败只在入口标记；面板一处可重试错误；已有数据「上次结果」/首次「读取失败」；多个真正独立失败分别保留 | 已修复（入口标记由 i-layout 按其文件落地，已对齐选择器） | `taskReadFailures()` 只由真实读取状态产生 3 类独立失败（任务清单/旧任务记录/执行状态），`marker` 区分首次失败与上次结果，`emptyVisible` 保证读不到时不谎称「暂无任务」；面板每条失败一个 `data-im="task-read-error"` + 一个 `task-read-retry`，只重试对应读取。执行状态读取优先读 i-state 新增的 executionsLoadState/executionsLoadedOnce/executionsLoadError，未合并时降级到 executionError。测试：pm032-tasks-task-center「#07」4 例。i-layout 已确认在 task-entry 上加 data-im="tasks-entry-error" / aria-invalid 并移除重复的 intent-read-error |
| #27 首次未授权「待确认」；只有原授权因范围变化确实失效才「需重新确认」并在按钮前给真实变更 | 已修复（前端侧；后端事实字段由 i-submit 落地中） | `authorizationNotice()`：authorization_valid!==false→无提示；proposed/无授权快照→pending「待确认」；仅 `authorization_invalidated:true` / 曾进入执行状态 / 旧快照版本不符→reconfirm「需重新确认」；变更来自 `authorization_changes`（后端只增字段）或旧快照 scope 与当前 scope 的真实差异；无快照时不拼假差异，如实「服务端未返回具体变更」。IntentLayer 详情把变更放在确认按钮之前（`data-im="intent-reauth-change"`，DOM 顺序有断言）。测试：pm032-tasks-intent「#27」3 例。i-submit 已回复按此字段名实现（IntentView 只增字段） |
| #56 任务页与审批历史各自刷新当前分区；历史错误/空态/计数来自历史查询 | 已修复 | TaskCenterPanel 头部刷新按 tab 分流：任务→refreshAfterIntentAction，审批历史→approvalResults.loadHistory；ApprovalResultsPanel 增加 `data-test="approval-history-count"`（只用 history 查询行数，实时事件条数不计），错误/空态原本就来自 historyError/history 查询。测试：pm032-tasks-task-center「#56」+ pm032-tasks-approval-recovery「#56 计数来自历史查询」 |
| #74 只在进入批量选择且存在可处理待确认项时显示批量区，零项收起 | 已修复 | `batchAreaVisible(focus, eligibleCount)`：仅 focus==="pending"（批量入口打开）且 legacyPending>0 时显示批量区/勾选框；普通任务入口与零项都不显示。IntentLayer 批量条同样按 selectableCount>0 收起。测试：pm032-tasks-task-center「#74」4 例、pm032-tasks-intent「#74」2 例 |
| #84 任务面板拉开任务名/状态/操作层级，复用共享图标与次级按钮 | 已修复（视觉细节建议 Lead 合并后统一截图复核） | 任务名升为主信息（衬线同族/正文尺寸），状态胶囊带 `QIcon` 共享图标（play/pause/check/warning/clock），刷新/关闭改用共享 `QIconButton`，行内动作按钮统一加共享图标（暂停/继续/取消/批准/拒绝/执行记录/可恢复），动作按钮保持次级样式。测试：pm032-tasks-task-center「#84」 |
| 文案 8 审批历史空态只写「暂无审批结果」，失败保留重试 | 已修复 | ApprovalResultsPanel 空态文本精确为「暂无审批结果」；失败时保留错误 + 「重试读取历史」。测试：pm032-tasks-approval-recovery |
| 文案 10 部分完成显示标题、成功项、失败项和重试入口 | 已修复（在意图确认结果面板，与「部分完成」真实 summary 同源） | `partialConfirmView()` 从 summarizeConfirm 的真实 succeededIds/failedIds 生成标题「部分完成」/「确认未完成」、成功项、失败项；新增「重试失败项」只提交显式失败且仍可确认的 id（1 条走单项、多条走同批），不重复提交已成功项。测试：pm032-tasks-intent「文案 10」 |
| 文案 11 恢复列表空态「暂无可恢复的任务」 | 已修复 | `recoveryEmptyText()` 与 RecoveryPanel 服务端意图空态均改为「暂无可恢复的任务」。测试：pm032-tasks-approval-recovery |
| 文案 14 已确认的意图显示实际进度；重复确认真的被拦截时才提示原因 | 已修复 | `intentProgressText()` 用 operations_summary 显示「进度：已完成 X / 共 N 项 · 执行中…」；卡片不再常态显示「已经开始过/不能重复确认」，只有真的调用确认被 `confirmBlockReason` 拦住时才写 error 行；详情同样只在 needs_update/未知/缺身份时常态给拦截原因。测试：pm032-tasks-intent「文案 14」2 例 |

## 4) 定向测试命令与结果

```
cd D:\qio-dev\pm-qio032-i-tasks\frontend
npx vitest run src/components/interactive/__tests__/pm032-tasks-task-center.test.ts \
  src/components/interactive/__tests__/pm032-tasks-intent.test.ts \
  src/components/interactive/__tests__/pm032-tasks-approval-recovery.test.ts
# 3 files / 26 tests passed

npx vitest run <13 个既有相关测试文件：p3a-intent-layer、fu2c-confirm-failure-reason、i2v2-a-intent-display、
approvalResultsView、b-approval-results-reach、stores/approvalResults、p3c-retry-recovery、p3c-retry-api、
i2v2-a-task-entries、i2v2-a-intent-read-failure、acceptance-i2v2-page-tasks、acceptance-i2v2-page-recovery、
InteractiveOldEntriesGone>
# 13 files / 107 tests passed
```

`npx vue-tsc --noEmit`：本分支范围内 0 error（最终结论见与报告的最后一笔提交之间的运行；初跑发现的唯一错误在本分支新测试的 props 类型，已修复）。未跑 `npm test` / `npm run build` / 后端 / cargo（按 CONTRACT §4）。

## 5) 新增 / 变更对外接口（均向后兼容）

- `taskCenter.ts` 新增导出：`taskCountsSummary`、`taskReadFailures` / `TaskReadFailure` / `TaskReadFailureInput` / `TaskReadFailureKey`、`batchAreaVisible`、`taskStatusIcon`、`taskActionIcon`；`taskEmptyText()` 默认文案改为「暂无任务」（分区参数仍保留）。
- `intentDisplay.ts` 新增导出：`authorizationNotice` / `AuthorizationNotice` / `AuthorizationStateKind`、`intentProgressText`、`cardBlockText`、`partialConfirmView` / `PartialConfirmView`；`capabilityRows` 的 authorization 行改为按 authorizationNotice 输出（pending=待确认/waiting tone，reconfirm=保留「旧授权已失效」字样 + 真实变更/problem tone）。
- `recoverySummary.ts`：`recoveryEmptyText()` 文案改为「暂无可恢复的任务」（函数签名不变）。
- 组件 props/emits 未变：TaskCenterPanel 仍为可选 `focus` + `close`/`open-record`；ApprovalResultsPanel 仍零 props；RecoveryPanel deps/retryApi 测试缝不变。
- 消费方注意：IntentView 期望后端只增字段 `authorization_invalidated: boolean`（i-submit 已确认实现）与 `authorization_changes: string[]`；store 原样透传（i-state 已确认不裁剪）。executions 读取失败优先读 i-state 的 executionsLoadState/executionsLoadedOnce/executionsLoadError，未合并时自动降级。

## 6) 未做 / 部分 / 集成风险

1. **#27 的后端事实依赖 i-submit 本轮新增字段**：字段落地前，`awaiting_confirmation` 且无授权快照的意图会按「待确认」而不是「需重新确认」（不能证明曾授权，不夸大）；i-submit 已在实现 `authorization_invalidated` / `authorization_changes`，字段名不改，前端无需再动。
2. **#07 入口标记由 i-layout 落地**（InteractiveView.vue）。若其未合并，则「只在任务入口标记」缺入口侧一半；面板侧已完整。两侧选择器已对齐：`tasks-entry-error` / `task-read-error` / `task-read-retry`。
3. **#84 做了结构与 token 层面的层级/图标统一，未做真实应用截图复核**（子分支时间盒内未起应用）；建议 Lead 合并后在净白/暗紫晶、800×600 各看一眼任务面板。
4. **文案 10 的解释**：按「确认结果部分完成」实现（标题/成功项/失败项/重试入口都在确认结果面板）；RecoveryPanel 的 partially_completed 仍保留原「部分完成：以服务端账本为准」+ 恢复入口，未改成成功/失败分列（真实成功/失败项要从账本接口取，本分支不新增数据源）。若 Lead 认为文案 10 指的是恢复面板，需要 i-submit 提供账本逐项事实后再补。
5. `i2v2-a-intent-read-failure` 中 InteractiveView 的 `intent-read-error` 相关断言依赖 i-layout 去重改动；他们改完后该文件 1 条断言可能需再对齐（不是本分支文件）。

## 7) 合并注意（与 i-layout 的唯一文件重叠）

- 两分支都改了 `frontend/src/views/__tests__/i2v2-a-intent-read-failure.test.ts`：
  - i-layout 改的是「入口标记一次 + 不重复横幅」口径（并移除 InteractiveView 的 intent-read-error）；
  - 本分支只改一条空态文案断言（成功读到空集合后打开任务面板：`task-empty` 为「暂无任务」，BRIEF #18）。
  - 解决冲突时两边都保留：入口侧断言用 i-layout 版本，任务面板空态断言用本分支的「暂无任务」。
- i-layout 已确认 #07 入口标记（data-im="tasks-entry-error" / aria-invalid / title「互动任务（读取失败）」）与 Esc 层（id: interactive-task-center）落地；本分支 TaskCenterPanel 不注册 Esc，不会双层。
- 本分支其余文件与 i-layout / i-state / i-submit / i-canvas 无重叠。
