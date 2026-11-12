# PM-QIO032 · i-state 交付报告

- 分支：`fix/pm-qio032-i-state`
- worktree：`D:\qio-dev\pm-qio032-i-state`
- 开工 SHA：`7445170d0f51756b2740eb7209d798540d8042a2`
- 交付 commit：
  1. `103c0126` fix(interactive-state): #03/#07/#13/#20/#26/#44-46 互动数据契约增量
  2. `a47347fa` fix(interactive-state): #26 订阅 INTERACTIVE_INTENTS_CHANGED（Lead 授权最小改动）
- 说明：worktree 曾因半截 checkout 出现索引损坏（4270 个 D、M=0），已按 Lead 指示删除 stale index.lock 后
  `git reset --hard 7445170d` 恢复；此后每次提交前均确认 status 无大规模 D。

## 1. 文件清单

| 文件 | 类型 | 内容 |
| --- | --- | --- |
| `frontend/src/interactive/submission.ts` | 改（+155） | #13 有效改动判定（effectiveSubmissionChanges / hasEffectiveSubmissionContent / isBlankTextCard / cardHasEffectiveContent）；文案 1 查证无记录改为「暂未查到这次提交」 |
| `frontend/src/stores/interactive.ts` | 改（+~430） | #20 只读门禁与重试；#13 提交门 canSubmit/submitBlockedReason；#03 roundStatus 主状态（单调）；#07 executionsLoadState/LoadedOnce/Error；#26 applyInteractiveIntentsChanged；#44-46 批次导入数据层 |
| `frontend/src/services/events.ts` | 改（+3） | EVENT_TYPES 只增 `INTERACTIVE_INTENTS_CHANGED`（Lead 授权） |
| `frontend/src/stores/events.ts` | 改（+~20，一个 case + 1 import） | 只增 `case "INTERACTIVE_INTENTS_CHANGED"` → `useInteractiveStore().applyInteractiveIntentsChanged(event.data)`；其它事件处理不动（Lead 授权） |
| `frontend/src/stores/__tests__/pm032-state-contract.test.ts` | 新增 | 11 个定向用例，覆盖 #03/#07/#13/#20/#26/#44-46 |

## 2. 逐项结果

| # | 结果 | 证据 / 说明 |
| --- | --- | --- |
| #06 | **基线已满足**（本轮只补契约说明） | 已保存：`saveStatus="saved"`+lastSavedAt（saved 仅保存成功回执后置位）；只清「本次已保存版本」：`draftSavedKeySeq`/版本守卫 + `requestDraftClear`→`pendingDraftClearKeys`；冲突保留双份：`draftConflicts`；迟到回执不删新草稿：`draftKeySeq` 版本比较。既有用例：`final-lead-m07-clear-binding.test.ts`（登记后未保存不清 / 保存成功才按登记版本清 / 登记后新输入不清）；`final-a2-clear-protection.test.ts`(9)、`final-a2-drafts-removal-result.test.ts`(12) 23:06 定向跑绿 |
| #03 | **已修复（store 侧）** | 新增唯一主状态 `roundStatus/roundStatusLabel/roundStatusDetail/roundSettled`（idle/submitting/unknown/queued/processing/replied/awaiting_confirm/failed/settled）。单调守卫：按 `submissionId:attempt` 记最高阶段，迟到理解事实（accepted/understanding）不能把已 replied/failed/settled 降级。用例：「受理→理解→已回复后，迟到的『理解中』事实不把主状态降级」 |
| #07 | **已修复（数据层）** | 新增 `executionsLoadState/executionsLoadError/executionsLoadedOnce`；失败不清空 executions。语义：error+LoadedOnce=false=读取失败；error+LoadedOnce=true=上次结果；重试=`loadExecutions()`。与 `intentsLoadState`/`intentViewsLoadState` 三者独立、互不覆盖。用例：「首次失败=读取失败；重试成功=上次结果可保留」 |
| #13 | **已修复（前端判定 + 门禁）** | `submission.ts` 新增有效改动判定：空白文字卡不算（trim 后无正文且非旧 code/url 迁移卡）；关系/分组/删除/正文修改算；布局变化不算。store 新增 `canSubmit/submitBlockedReason`，`submit()` 空提交不发请求、置 `submitStatus="empty"` + 下一步提示。已与 i-submit 对齐后端口径（其差异说明：删除已提交卡片算有效改动，与前端一致）。用例 3 个（空白卡/有正文/关系） |
| #20 | **已修复（数据层门禁 + 重试）** | 新增 `boardLoadedOnce/boardReadOnly/boardEditBlockedReason/retryBoardLoad()`；`commit()` 在读取失败期间直接返回不应用（不改 board、不排保存）；`saveNow()`/`submit()` 拒绝并发明确原因。首次失败 reason 含「重试读取或返回对话」，有上次数据含「只能查看」。用例 2 个 |
| #26 | **已修复（前端数据 + 订阅）** | `applyInteractiveIntentsChanged(payload)`：按 `intent_id` upsert（重复事件/重连不建重复卡）、非当前板面忽略、未知 id 不编造卡片并触发一次 300ms 去抖权威 intent-views 刷新；不重置视口/选择/面板。`services/events.ts` EVENT_TYPES + `stores/events.ts` case 已接（Lead 授权）。用例：重复事件幂等、非当前板面不落地、未知 id 不造卡、EVENT_TYPES 包含 |
| #27 | **平台已就绪（UI/后端在本轮他人范围）** | store `intentViews` 原样透传服务端字段（含 `authorization_valid`，以及 i-submit 新增的 `authorization_invalidated/authorization_changes`），不裁剪不改名；已告知 i-tasks 直接读。判断「首次未授权=待确认 / 范围变化=需重新确认」的展示由 i-tasks 的 taskCenter/IntentLayer + i-submit 事实字段落地；store 无需新增映射 |
| #41 | **基线已满足（store 侧能力）** | 提交详情所需三种来源既有：`lastSubmission`（有效空态/上次结果）、`understandingError`+`refreshUnderstanding()/retryUnderstanding()`、`intentViewsLoadedOnce/LoadState`+`loadIntentViews()`；成功读取为空由服务端回执 `before.empty` 表达。UI 区分由 i-layout 接线 |
| #44 | **数据层已修复（UI 由 i-canvas 接线）** | `beginImportBatch` 单批上限 5，第 6 份起 `status="unreceived"` 保留在 items（可重新导入）；`ImportBatchItem{key,name,status,error,attachmentId,cardId,boardId,taskId}` 提供「已接收/未接收」明细。用例覆盖 6 份批次 |
| #45 | **数据层已修复** | `noteImportItem` 终态（succeeded/failed）不被后续回执覆盖（后续成功不覆盖前面的失败）；`retryImportItem` 只把对应文件重置为 pending，同批其它项不动。用例覆盖 |
| #46 | **数据层已修复（跨路由 + 迟到守卫）** | 批次状态在 Pinia store（路由切换不销毁），绑定 `boardId/cardId/taskId`；迟到成功回执若绑定卡片已删除/替换 → 记 failed 并说明，不关联任何卡片。用例覆盖；跨路由真实进度依赖 i-canvas 在 UI 中把上传回执写入 store（接口已发布） |
| #59 | **基线已满足** | `board.ts` 已有 `setGroupCollapsed`（写 group.collapsed 纯显示状态，随板面保存、后端 diff/可见范围不读），i-canvas 用 `store.commit` 即可，无需 store 新动作（已回复 i-canvas） |
| #74 | **基线已满足** | `batchAvailable`（服务端事实）+`openTaskCenter("pending")`/`taskCenterFocus` 已有；批量区显示门槛由 UI 按可处理项计算（已告知 i-layout/i-canvas） |
| #80/#81 | **基线已满足（UI 由 i-layout 接线）** | 重复提交：`submitStatus="duplicate"`+`lastSubmission` 查原进度；回执未知：`submitResultUnknown`+`acceptanceLookupPending/lastAcceptance/confirmAcceptanceAfterUnclear()` 按原请求身份查证，查不到保持未知；文案 1 后半句「暂未查到这次提交」已在 `acceptanceLookupText` 内 |
| 文案 9 | **不在 store 范围** | 「请先确认改动影响，再重试保存」应在 `components/interactive/v2/RecoveryPanel.vue:325`（i-tasks 文件，现有近似文案「保存被影响确认挡住了：先在提交区处理这次影响确认，再重试保存」）；已告知 i-tasks。store 相关说明：`pendingImpact`/影响确认门在 store 已有真实事实 |

## 3. 定向验证（命令与结果）

```powershell
cd D:\qio-dev\pm-qio032-i-state\frontend
npx vitest run src/stores/__tests__/pm032-state-contract.test.ts
# => Test Files 1 passed (1) / Tests 11 passed (11)

npx vitest run src/stores/__tests__/final-lead-m07-clear-binding.test.ts src/interactive/__tests__/final-a2-clear-protection.test.ts src/interactive/__tests__/final-a2-drafts-removal-result.test.ts
# => Test Files passed / Tests 21+ passed（#06 基线证据）

npx vue-tsc --noEmit
# => exit 0，无输出（自己范围类型错误为 0）
```

## 4. 新增/变更对外接口（UI 分支只消费，不改 store）

- **#20**：`boardLoadedOnce:boolean`、`boardReadOnly:boolean`、`boardEditBlockedReason:string|null`、`retryBoardLoad():Promise<boolean>`；`commit` 在读取失败期间不生效（签名不变、仍返回 void）。
- **#13**：`canSubmit:boolean`、`submitBlockedReason:string|null`；`interactive/submission.ts` 新增 `effectiveSubmissionChanges()/hasEffectiveSubmissionContent()/isBlankTextCard()/cardHasEffectiveContent()`。
- **#03**：`roundStatus`（模块级导出类型 `RoundStatus`）、`roundStatusLabel`、`roundStatusDetail`、`roundSettled`。
- **#07**：`executionsLoadState`、`executionsLoadError`、`executionsLoadedOnce`。
- **#26**：`applyInteractiveIntentsChanged(payload: InteractiveIntentsChangedPayload): boolean`；事件类型加入 `EVENT_TYPES`，`stores/events.ts` 新 case。
- **#44-46**：`beginImportBatch(targets, opts?)`、`noteImportItem(batchId,itemKey,patch)`、`retryImportItem(batchId,itemKey)`、`dismissImportBatch(batchId?)`、`importBatchById(id)`、`importFileSupportedLimit()`、`importBatches/currentImportBatch/latestImportBatchId`；类型 `ImportItemStatus/ImportBatchItem/ImportBatch`（模块级导出）。
- 既有导出全部保留（submitStatus/submissionStage/understandingStatus/executionStatus/loadIntents/loadIntentViews/loadExecutions/requestDraftClear/draftConflicts 等）。

## 5. 未做 / 集成风险

1. **#26 端到端**：i-submit 后端事件已落地（8 项后端定向测试绿），前端两处订阅已接；真实 SSE 端到端需 Lead 在合并后跑一次 E2E（本分支单测用合成事件验证）。
2. **#44-46 UI 接线**：store 数据层与用例完成，i-canvas 尚未把 `beginImportBatch/noteImportItem/retryImportItem` 接到拖放/上传流程（接口 v1 已发布并确认）；跨路由真实进度需要它把上传回执写入 store，否则只有数据层能力。
3. **#03/#20/#41 的界面呈现**在 i-layout；store 只保证数据与门禁。若 UI 仍自行拼状态会与主状态不一致。
4. **#27 「真实变更事实」**依赖 i-submit/i-material 的 `authorization_changes` 字段落地；store 原样透传，不做裁剪。
5. 本分支未改后端、未改 docs/依赖/锁文件、未 push。
