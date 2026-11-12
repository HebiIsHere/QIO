# i-state 接口增量 v1（2026-11-12 22:08，Lead 转录）

> 来源：i-state 发给 Lead 的接口说明。字段只增不改，旧导出保持兼容；实现与测试由 i-state 在
> fix/pm-qio032-i-state 落地。UI 分支只消费，不改 store。有疑问直接 send_message 给 i-state。

## 1) #20 整板读取失败只读门禁（stores/interactive.ts）
- boardLoadedOnce: boolean——是否成功读到过板面（首次失败=false）
- boardReadOnly: boolean——loadError != null 且已有上次成功数据（只读展示）
- boardEditBlockedReason: string | null——非空时编辑/保存/提交都被 store 拦截，UI 用它显示原因
- retryBoardLoad(): Promise<boolean>——重试整板读取（只读路径，不重建板面）
- loadError: string | null（既有）
- 门禁语义：boardReadOnly 时 commit() 拒绝应用编辑（返回 false，不改 board）、saveNow()/submit() 直接返回并给出原因；UI 再禁按钮。
- 首次失败（boardLoadedOnce=false）：UI 给「重试」「返回对话」两个入口；有上次数据：只读展示 + 重试。

## 2) #13 空提交与后端一致
- effectiveSubmissionChanges(candidate, baseline?): ExpressionLike[]——与后端 submission.diff_states 同口径（空白文字卡不计；关系/分组增删改与卡片增删/正文修改计；layout_only 不计）
- hasEffectiveSubmissionContent(candidate, baseline?): boolean
- store.submitBlockedReason: string | null
- store.canSubmit: boolean
- submit() 空提交时置 submitStatus="empty" + submitError，不发请求；后端仍权威。
- UI 只消费 canSubmit/submitBlockedReason，不要自己再算一套。

## 3) #03 提交区唯一主状态
- roundStatus: "idle"|"submitting"|"unknown"|"queued"|"processing"|"replied"|"awaiting_confirm"|"failed"|"settled"
- roundStatusLabel: string；roundSettled: boolean（终态；迟到事实不得回退，按 submission+attempt 单调等级守卫）；roundStatusDetail: string | null
- 旧 submitStatus / submissionStage / understandingStatus / executionStatus 保留。

## 4) #07 独立失败分别保留
- executionsLoadState: "idle"|"loading"|"ok"|"error"；executionsLoadedOnce；executionsLoadError（executionError 保留）
- intentsLoadState/intentsLoadedOnce/intentsLoadError 与 intentViewsLoadState/intentViewsLoadedOnce/intentViewsLoadError 既有；error+LoadedOnce=false → 「读取失败」；error+LoadedOnce=true → 「上次结果」；多来源各自独立。
- 单点重试：loadExecutions() / loadIntents() / loadIntentViews()，失败不清旧数据。

## 5) #44/#45/#46 批次导入
- ImportItemStatus = "pending"|"uploading"|"succeeded"|"failed"|"unreceived"
- ImportBatchItem = { key, name, status, error, attachmentId, cardId, boardId, taskId? }
- ImportBatch = { id, boardId, createdAt, limit, items }
- beginImportBatch(targets, opts?): ImportBatch——limit 默认 5；第 6 份起 unreceived 并保留（可重新导入）；同一时刻只保留最新批次为 current
- noteImportItem(batchId, itemKey, patch)——终态后不覆盖（后续成功不覆盖前面失败）；绑定守卫：卡片删除/替换 → failed + 迟到说明；boardId 不一致回执丢弃
- retryImportItem(batchId, itemKey)——只重置该 item（UI 重新拿 File 上传）
- importBatches / currentImportBatch / latestImportBatchId / dismissImportBatch
- 跨路由批次状态在 store（Pinia 不销毁）；UI 直接渲染 items，不要另存一份。

## 6) 既有能力（无需新增）
- #06：saveStatus="saved"+lastSavedAt；draftSavedKeySeq 版本守卫；draftConflicts；requestDraftClear/pendingDraftClearKeys；迟到回执由 draftKeySeq/版本守卫丢弃。
- #41：lastSubmission + understandingLoadError + intentViewsLoadedOnce 区分有效空态/读取失败/上次结果；loadUnderstanding()/refreshUnderstanding()/loadIntentViews() 重试。
- #80/#81：submitStatus="duplicate"（已提交过）+ lastSubmission；submitResultUnknown + acceptanceLookupPending/lastAcceptance + confirmAcceptanceAfterUnclear() 按原请求查证。
- #27：intentViews 原样透传服务端字段（含 authorization_valid），新增 authorization_invalidated/authorization_changes 无需改名，store 不裁剪。
- #59：无需新动作，store.commit(setGroupCollapsed(board, groupId, collapsed)) 即持久化。
- #74：batchAvailable（服务端事实）+ openTaskCenter("pending")/taskCenterFocus 已有；门槛由 UI 按 batchAvailable 与可处理项判断。

## 落地顺序
#20/#13（含门禁）→ #03 → #07 执行读取状态 → #44–46 批次。
