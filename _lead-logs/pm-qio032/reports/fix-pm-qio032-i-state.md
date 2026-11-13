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
| #41 | **已修复（补理解读取三态）** | 新增 `understandingLoadState/understandingLoadError/understandingLoadedOnce`：error+LoadedOnce=false=读取失败（可重试）；error+LoadedOnce=true=上次结果（旧内容保留）；ok+空记录=成功读取为空（这才是「没有理解过程 / 本次没有改动」）。既有 `lastSubmission/understandingError***/intentViewsLoadedOnce` 与 `refreshUnderstanding()` 重试入口保留。UI 由 i-layout 接线 |
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
# => Test Files 1 passed (1) / Tests 13 passed (13)（含 #41 读取三态 2 条）

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
- **#41**：`understandingLoadState`、`understandingLoadError`、`understandingLoadedOnce`（重试入口仍是 `refreshUnderstanding()`）。
- **#26**：`applyInteractiveIntentsChanged(payload: InteractiveIntentsChangedPayload): boolean`；事件类型加入 `EVENT_TYPES`，`stores/events.ts` 新 case。
- **#44-46**：`beginImportBatch(targets, opts?)`、`noteImportItem(batchId,itemKey,patch)`、`retryImportItem(batchId,itemKey)`、`dismissImportBatch(batchId?)`、`importBatchById(id)`、`importFileSupportedLimit()`、`importBatches/currentImportBatch/latestImportBatchId`；类型 `ImportItemStatus/ImportBatchItem/ImportBatch`（模块级导出）。
- 既有导出全部保留（submitStatus/submissionStage/understandingStatus/executionStatus/loadIntents/loadIntentViews/loadExecutions/requestDraftClear/draftConflicts 等）。

## 5. 未做 / 集成风险

1. **#26 端到端**：i-submit 后端事件已落地（8 项后端定向测试绿），前端两处订阅已接；真实 SSE 端到端需 Lead 在合并后跑一次 E2E（本分支单测用合成事件验证）。
2. **#44-46 UI 接线**：store 数据层与用例完成，i-canvas 尚未把 `beginImportBatch/noteImportItem/retryImportItem` 接到拖放/上传流程（接口 v1 已发布并确认）；跨路由真实进度需要它把上传回执写入 store，否则只有数据层能力。
3. **#03/#20/#41 的界面呈现**在 i-layout；store 只保证数据与门禁。若 UI 仍自行拼状态会与主状态不一致。
4. **#27 「真实变更事实」**依赖 i-submit/i-material 的 `authorization_changes` 字段落地；store 原样透传，不做裁剪。
5. 本分支未改后端、未改 docs/依赖/锁文件、未 push。

---

## 第三轮（合并父分支 + #80 重复提交 + ImportBatch 复核）

- **合并父分支**：`git merge fix/pm-qio032-interactive` → fast-forward 到 `9f80b0cf`（并入 Lead 对 store #13 空提交门的调整与 E2E 台账/脚本）；工作区提交前确认无大规模 D。
- **#80 已修复（前端数据层）**：
  - 新增 `submissionBaseline`（服务端上次成功提交摘要）、`pendingSubmissionChanges`（服务端重算的未提交改动）、`acceptedBaseline`（按板面本机持久化的「上次成功受理 after 快照」）。
  - `submit()` 在「已成功受理 && 相对上次成功提交基准没有有效改动」时**不发 POST**：`submitStatus="duplicate"`、`roundStatusLabel="已提交过"`、`submitNote` 说明「已提交过：这次内容与上次成功提交相同…」；原 `lastSubmission` 不被覆盖，`latestAcceptedSubmission` 供「查看原进度」。
  - 判据只用精确事实（三选一）：① 会话内成功回执的 `after` 快照；② 刷新后本机记住、且仍是服务器最新成功提交的 `after` 快照；③ 服务端刚按当前基准算出的 `pending`（`baselineSeq === baseline.seq`）。证明不了就正常提交，不用时间/近似判断。
  - 有真实改动正常提交；上次 failed / unknown 不判重复（允许重试）；待提升草稿视为真实改动。
  - `replayed=true` 回执按原记录处理：`submitNote` 明确「原受理记录」、不重置理解状态为 accepted、不当成新的「已受理」。
  - 服务端回执 empty / duplicate 时保留原受理记录（不被没有新工作的回执覆盖）。
- **#44-46 ImportBatch 复核 + 小接口**：新增模块级纯函数 `importBatchCounts`（total/received/failed/unreceived/pending/uploading）、store `currentImportBatchCounts`、`hasImportBatchInFlight`、`markImportItemUploading`；#45「重试只重置对应项」语义不变并新增用例。
- **定向测试**：
  - 新增 `frontend/src/stores/__tests__/pm032-state-duplicate-submit.test.ts`（7 用例）：未改动 no POST + 已提交过 + 原记录、有改动正常、失败可重试、replayed 回执、刷新后本机快照判重、服务端 pending 判重、过期 pending 不据此判重。
  - `pm032-state-contract.test.ts` +1（批次汇总计数）。
  - `npx vitest run src/stores/__tests__/pm032-state-contract.test.ts src/stores/__tests__/pm032-state-duplicate-submit.test.ts` → **21/21 绿**；`npx vue-tsc --noEmit` → exit 0。
- **新增接口**：`latestAcceptedSubmission`、`hasAcceptedSubmission`、`submissionBaseline`、`pendingSubmissionChanges`、`currentImportBatchCounts`、`hasImportBatchInFlight`、`markImportItemUploading`、`importBatchCounts()`（模块级）+ `ImportBatchCounts` 类型；`types.ts` 的 `pending` 修正为服务端真实形状 `PendingSubmissionChanges`（旧消费方读 `payload.pending.expressions` 仍兼容）。
- **风险**：刷新后的重复识别依赖本机快照（localStorage 按板面）；本机存储不可用且服务端 pending 过期时退回服务端权威判定（不会误判重复，只会多一次服务端判定，服务端仍按 empty/duplicate 处理）。i-layout 的「已提交过 / 查看原进度」UI 已存在，store 状态现在能触发它。
### 第三轮补充：既有用例的最小适配（需 Lead 复核，未删除/未跳过任何用例）

#80 让「未改动重复提交」不再发 POST，这与两条基线用例的假设冲突（它们用「同内容再提交一次」来构造第二次提交）。适配方式：在第二次提交前加一处真实改动（addCard 文本卡），让第二次提交确实是「有改动的新提交」，用例原本要验证的理解归属 / 未知受理语义不变：

- frontend/src/interactive/__tests__/a-understanding-ownership.test.ts（a、b 两例）：第二次提交前 store.commit(addCard(...))。
- frontend/src/interactive/__tests__/irm-d-understanding-store.test.ts（「只有上一次提交留下的理解记录」一例）：同上。
- 未删除 / 未跳过任何用例；断言与覆盖点保持原意。

回归结果（148 用例全绿）：
- 4 文件 32/32：a-understanding-ownership(3)、irm-d-understanding-store(8)、pm032-state-duplicate-submit(7)、pm032-state-contract(14)
- 11 文件 116/116：n7/n8/n5-acceptance、f10-submit-refresh、final-lead-m1、irl-verify-permission-status、recovery-lead-state-facts、submitFailureBrief、textCardUnified、p1-closed-card-shows-promoted、pm032-layout-shell
- npx vue-tsc --noEmit → exit 0
### 第三轮收尾：再次合并父分支最新（含其它分支第二轮）

- git merge fix/pm-qio032-interactive → fast-forward 到 d8ba9b60（包含 i-canvas #44-46 批次 UI 接线、i-layout 提交面板、i-material/i-submit #28 等）；合并前确认我的提交已被父分支包含，且 HEAD..父分支 对本人名下文件 diff 为空。
- 集成验证（合并后）：pm032-canvas-import-batch.test.ts（i-canvas 新增，8 用例，直接消费 ImportBatch API）+ 我方 store 用例 → 29/29 绿；pm032-layout-submit-panel.test.ts（i-layout）→ 8/8 绿。说明 #44-46 UI 接线与提交面板消费我的接口正常。
- 最终 HEAD：d8ba9b60（我的两次提交 1754b8b8 / e37cf86e 已在父分支中）；工作区干净。
