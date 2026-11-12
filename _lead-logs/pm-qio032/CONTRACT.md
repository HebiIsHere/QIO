# PM-QIO032 组内协作契约（CONTRACT）

> 所有子分支必读。BRIEF.md 是本轮任务原文；本文件规定文件归属、接口与验收纪律。
> 冲突时：用户任务书 > 本文件 > 各子分支自己的判断。发现契约问题先发消息给 Lead，不擅改。

## 0. 事实

- 仓库：`D:\qio-dev\qio`（bare 之外的唯一主仓；所有 worktree 由它创建）
- 审计基线 / 开工 SHA：`7445170d0f51756b2740eb7209d798540d8042a2`
- 父分支：`fix/pm-qio032-interactive`（worktree `D:\qio-dev\pm032-interactive`，Lead 专用）
- 共享环境：`D:\qio-dev\qio\backend\.venv`、`D:\qio-dev\qio\frontend\node_modules`（Junction 链接）
- 共享契约文档：`docs/interactive-mode-contract.md`、`docs/interactive-page-v2-contract.md`、
  `docs/architecture.md`（**只读**；本组不改）
- 状态文档 `docs/status.md` 由 Lead 统一更新，子分支**不要改 docs/**。

## 1. 开工步骤（每个子分支）

先检查分支/worktree 是否已存在；已存在就沿用，**不要删除已有工作**。

```powershell
cd D:\qio-dev\qio
git branch --list fix/pm-qio032-i-material
git worktree list
# 不存在时：
git worktree add D:\qio-dev\pm-qio032-i-material -b fix/pm-qio032-i-material 7445170d0f51756b2740eb7209d798540d8042a2
cd D:\qio-dev\pm-qio032-i-material
if (-not (Test-Path backend\.venv)) { New-Item -ItemType Junction -Path backend\.venv -Target D:\qio-dev\qio\backend\.venv }
if (-not (Test-Path frontend\node_modules)) { New-Item -ItemType Junction -Path frontend\node_modules -Target D:\qio-dev\qio\frontend\node_modules }
git rev-parse HEAD   # 必须是 7445170d...
```

提交只在本分支：小步、中文、带范围前缀，例如
`fix(interactive-material): 提交轮次只读提交时材料版本`。
**禁止** push / PR / CI / 合并到父分支 / 改其它 worktree / 改 main。

## 2. 文件归属（硬边界）

只写自己名下的文件；其余文件**一律不写**。需要别人范围改动时：发消息给该文件 owner 和 Lead，
说明「文件 + 所需改动 + 调用约定 + 用例」，由 owner 落地。

### i-material（后端）
- `backend/src/agent/interactive/materials.py`
- `backend/src/agent/interactive/board.py`（参与/可见性/授权纯函数部分）
- `backend/src/agent/interactive/board_store.py`（权限、迁移、版本读取相关；不重构保存流程）
- `backend/src/agent/interactive/board_tools.py`
- `backend/src/agent/tools/attachment_tools.py`（仅材料读取必需的增量）
- `backend/src/agent/api/preview_routes.py`（仅文件可读/预览事实必需时）
- 新增迁移只能**追加**；新增测试 `backend/tests/pm_qio032_material_*.py`
- 不写：`submission.py`、`api/interactive.py`、`records.py`、`runtime.py`

### i-submit（后端）
- `backend/src/agent/interactive/submission.py`
- `backend/src/agent/interactive/records.py`
- `backend/src/agent/interactive/intent_service.py`
- `backend/src/agent/interactive/intents.py`（只动审批/待确认/授权有效性相关，不动执行引擎）
- `backend/src/agent/interactive/runtime.py`
- `backend/src/agent/api/interactive.py`、`backend/src/agent/api/events.py`
- 新增测试 `backend/tests/pm_qio032_submit_*.py`
- 不写：`board_tools.py`、`materials.py`、`board_store.py`

### i-state（前端数据契约）
- `frontend/src/stores/interactive.ts`（唯一 writer）
- `frontend/src/services/interactive.ts`
- `frontend/src/interactive/drafts.ts`、`submission.ts`、`approval.ts`、`types.ts`、`materials.ts`、`serverFacts.ts`
- 仅当 #07 数据层确实需要时：`frontend/src/stores/conversationTasks.ts`（先与 i-tasks 对齐）
- 新增测试 `frontend/src/**/__tests__/pm032-state-*.test.ts`

### i-canvas（前端画布）
- `frontend/src/components/interactive/BoardCanvas.vue`（唯一 writer）
- `BoardCard.vue` / `AddMenu.vue` / `SelectionMenu.vue` / `BoardGroupFrame.vue` / `BoardLinkLayer.vue` /
  `BoardPreviewLayer.vue` / `BoardResizeLayer.vue` / `BoardCardAnchorLayer.vue` / `BoardGroupAnchorLayer.vue` /
  `CardInlinePreview.vue` / `CardAttachmentPreview.vue` / `FileCardMaterialPanel.vue` / `CardDraftHint.vue`
- `frontend/src/interactive/board.ts` / `geometry.ts` / `linkCurve.ts` / `linkEndpoints.ts` /
  `overlayPlacement.ts` / `resize.ts` / `cardLayout.ts` / `horizontalFit.ts` / `viewport.ts` /
  `cardDetail.ts` / `previewPlan.ts` / `attachmentPreview.ts` / `fileCardRef.ts` / `textCard.ts` / `toolbarLayout.ts`
- 新增测试 `frontend/src/components/interactive/__tests__/pm032-canvas-*.test.ts`
- 对 `interactive/board.ts` 的既有导出保持向后兼容（store 依赖它们）；新增 API 先发给 i-state。

### i-layout（前端页面/外壳）
- `frontend/src/views/InteractiveView.vue`（唯一 writer）
- `components/interactive/SubmitCluster.vue` / `BoardChangeList.vue` / `BoardToolbar.vue`
- `components/interactive/v2/InteractiveConversation.vue` / `InteractiveBookmarks.vue` /
  `InteractiveChromeTools.vue` / `SaveFailureBanner.vue` / `InteractiveWorkspace.vue`（页面壳必需时）
- `components/MessageStream.vue`：**只改 #16 互动专属空态**，其它模式空态保持原样
- 新增测试 `frontend/src/views/__tests__/pm032-layout-*.test.ts` 或 `components/interactive/__tests__/pm032-layout-*.test.ts`

### i-tasks（前端任务/审批/恢复）
- `components/interactive/v2/TaskCenterPanel.vue` / `taskCenter.ts` / `IntentLayer.vue` /
  `intentDisplay.ts` / `intentDisplayState.ts` / `RecoveryPanel.vue` / `recoverySummary.ts` /
  `RetryPanel.vue` / `retryApi.ts`
- 审批历史：`components/approval/ApprovalResultsPanel.vue`、`stores/approvalResults.ts`（如必须）
- 新增测试 `frontend/src/components/interactive/__tests__/pm032-tasks-*.test.ts`
- TaskCenterPanel 对外 props/emits 保持向后兼容（i-layout 已接线）；新增用可选 props。

### 全体禁止（需要时先问 Lead）
`frontend/src/stores/session.ts`、`components/Composer.vue`、`components/WindowChrome.vue`、
`stores/events.ts`、`App.vue`、`router*`、`stores/approvals.ts`、`components/conversation/*`、
`stores/ui.ts`、`stores/notifications.ts`、`backend/src/agent/prompts.py`、`interactive/models.py`（冻结共享面）、
`docs/**`、`scripts/**`、依赖/锁文件。

## 3. 跨分支接口

- **i-state 是前端互动数据契约的唯一设计者**：新增/变更 store 状态与动作时，先写清
  `名称 + 类型 + 语义 + 失败/迟到回执行为 + 对应用例`，用 send_message 同时发给 lead 和所有消费方
  （i-canvas / i-layout / i-tasks）。UI 分支只消费，不改 store。
- UI 分支不得引用「尚未发布」的 store API；需要的先发请求给 i-state，同一批里尽快实现。
- **i-material 是后端材料/版本/授权的唯一实现者**：把「提交轮次取材料」的函数签名与调用位置
  写成接口说明发给 i-submit（和 lead），由 i-submit 在 `submission.py` 接线。
- **i-submit** 保持 `records.py` 消息事实与执行记录对外读取接口兼容基线消费者；
  API 响应字段只增不改，旧字段语义不变。
- 任何跨分支共享文件（上面「全体禁止」里没有列出的边缘文件）先问 Lead，不要赌。

## 4. 验证纪律

- 后端定向：`cd <wt>\backend; $env:QIO_DATA_DIR='<wt>\.tmp-test-data'; $env:QIO_DISABLE_DB_CHECK='1'; & .\.venv\Scripts\python.exe -m pytest tests/<file>.py -q`
  （**不要**跑全量 pytest，Lead 统一跑。）
- 前端定向：`cd <wt>\frontend; npx vitest run src/<path>`
- 前端类型检查：交付前在**自己 worktree**跑一次 `npx vue-tsc --noEmit`（慢，一次即可；报错必须清零自己范围的错误）。
- 不要跑：`npm run build`、全量 `npm test`、`cargo`、真实网络/真实模型；模型一律 fake provider。
- 每条修改：先写失败用例证明缺陷（TDD 优先）；修完后该用例必须绿。纯视觉/几何也可用组件测试。
- 不新增依赖；不写会过期的硬编码数字；不降低既有测试覆盖（删除/跳过测试需 Lead 同意）。

## 5. 汇报（必须）

完成后（或到时间盒）用 `send_message` 给 `lead`，并把自己报告写到
`_lead-logs/pm-qio032/reports/<你的分支名>.md` 后在**自己分支**提交（合并时会随分支进入父分支）。

报告内容（短、真、可核对）：
1. 分支 + worktree + 全部 commit SHA；
2. 改动/新增文件清单；
3. 逐项结果表（你负责的每个 # 编号：已修复 / 部分修复 / 基线已满足 / 未做 + 证据：测试名或文件行）；
4. 跑过的定向命令与结果（成功/失败条数）；
5. 新增/变更的对外接口（store 字段、props、后端函数/API 字段）与消费方注意事项；
6. 已知缺陷、未做部分、需要 Lead 集成时注意的接缝。

## 6. 时间盒与优先级

- 先做能独立闭环、风险高的条目；再做文案/样式。修不完就如实报「未做/部分」，不要留下编译不过或半截状态。
- 卡住超过 15 分钟：send_message 给 lead（附复现/报错），不要长时间空转。
- 目标：先交一版可合并的工作分支（含测试），再迭代；不要为了凑条目破坏基线功能。
