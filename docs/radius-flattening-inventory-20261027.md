# 圆角压平清单（2026-10-27 盘点）

> 回答「哪些胶囊/圆角被压平成 14px 了」。数据由脚本对比 `e5c80253`（统一改造前）→ `d8cdcc41`（本轮基线）的 CSS 逐条得出。

## 结论速览

- 压平发生在**上一轮**（`30cc0496` / `9bd613e0` / `e61f76a2` / `c066bbfa`，「全应用提示/通知/弹窗统一改造」），**不是**记忆星球页改版这一轮做的。
- 合计 **273 处** 圆角被改成 `--qio-ui-radius`（14px）：
  - **胶囊形（999px / `--r-pill`）→ 14px：51 处**
  - **圆形（50%）→ 14px：3 处**（按钮从圆变圆角方形，视觉变化最大）
  - 其它半径（6/8/10/12/16/20px、`--r-xs/sm/md/lg/xl`）→ 14px：219 处，分布在 56 个文件
- 本轮（记忆星球页改版）**只压平了 1 处胶囊**：话题概览里的关键词小标签（随本页「统一 14px」要求）；并在 2026-10-27 按用户要求把**聊天输入胶囊恢复**为胶囊形（`9e30693f`）。

## 一、胶囊形 → 14px（51 处）

### 对话页

| 文件 | 选择器 | 原来 | 现在 |
| --- | --- | --- | --- |
| `components/AttachmentChip.vue` | `.chip` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/Composer.vue` | `.input-pill` | `var(--r-pill)` | ~~`var(--qio-ui-radius)`~~ → 已恢复为胶囊（`9e30693f`） |
| `components/Composer.vue` | `.stop-btn` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/Composer.vue` | `.confirm-btn` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/Composer.vue` | `.draft-retry` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/Composer.vue` | `.attach-btn` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/DevTaskEntry.vue` | `.dev-task-entry` | `var(--r-pill)` | `var(--qio-ui-radius, var(--r-lg))` |
| `components/DevTaskEntry.vue` | `.dev-task-revoke` | `var(--r-pill)` | `var(--qio-ui-radius, var(--r-lg))` |
| `components/DevTaskEntry.vue` | `.dev-task-resume` | `var(--r-pill)` | `var(--qio-ui-radius, var(--r-lg))` |
| `components/DevTaskEntry.vue` | `.dev-task-abandon` | `var(--r-pill)` | `var(--qio-ui-radius, var(--r-lg))` |
| `components/MessageItem.vue` | `.queued-tag` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/MessageItem.vue` | `.sub-kind` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/MessageItem.vue` | `.task-locate, .preview-open` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/MessageItem.vue` | `.code-preview-btn` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/MessageStream.vue` | `.back-latest .count` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/TurnTimingPanel.vue` | `.tt-track` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/TurnTimingPanel.vue` | `.tt-fill` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/conversation/ChatColumn.vue` | `.topic-chip` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/conversation/ChatColumn.vue` | `.notice .kind` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/conversation/ComposerEntries.vue` | `.entry-btn .count` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/conversation/ConversationModeSwitch.vue` | `.mode-btn` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/conversation/PreviewPanel.vue` | `.pv-head-badge` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/conversation/SendIssuesPanel.vue` | `.head .count` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/conversation/TaskRailItem.vue` | `.task-need` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/conversation/TaskRailItem.vue` | `.task-approval-entry` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/conversation/TaskRailItem.vue` | `.task-action` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/credentials/CredentialForm.vue` | `.tag-chip` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `styles/base.css` | `.qio-badge` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `styles/base.css` | `.qio-glass--chip` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `styles/base.css` | `.qio-tag` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `styles/base.css` | `.qio-state` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `views/ConversationView.vue` | `.rail-badge` | `var(--r-pill)` | `var(--qio-ui-radius)` |

### 互动页

| 文件 | 选择器 | 原来 | 现在 |
| --- | --- | --- | --- |
| `components/interactive/BoardCard.vue` | `.participation` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/interactive/BoardCard.vue` | `.kind` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/interactive/ExecutionRecordPanel.vue` | `.erp-close` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/interactive/SubmitCluster.vue` | `.sc-pill` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/interactive/SubmitCluster.vue` | `.approval-demo` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/interactive/SubmitCluster.vue` | `.sc-details-icon` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/interactive/v2/InteractiveChromeTools.vue` | `.iw-search-field` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/interactive/v2/RecoveryPanel.vue` | `.rp-tag` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/interactive/v2/TaskCenterPanel.vue` | `.tc-tab` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/interactive/v2/TaskCenterPanel.vue` | `.tc-status` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `components/interactive/v2/TaskCenterPanel.vue` | `.tc-btn` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `views/InteractiveView.vue` | `.im-floating-chat :deep(.ic-toolbar .tb-btn)` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `views/InteractiveView.vue` | `.im-hud-left` | `var(--r-pill)` | `var(--qio-ui-radius)` |
| `views/InteractiveView.vue` | `.im-chip` | `var(--r-pill)` | `var(--qio-ui-radius)` |

### 审批/反馈

| 文件 | 选择器 | 原来 | 现在 |
| --- | --- | --- | --- |
| `components/approval/ApprovalResultsPanel.vue` | `.ar-state` | `var(--r-pill)` | `var(--qio-ui-radius, var(--r-lg))` |
| `components/approval/NonChatApprovalEntry.vue` | `.nonchat-approval-entry` | `var(--r-pill)` | `var(--qio-ui-radius, var(--r-lg))` |
| `components/approval/NonChatApprovalEntry.vue` | `.interrupted-note` | `var(--r-pill)` | `var(--qio-ui-radius, var(--r-lg))` |
| `components/approval/UnifiedApprovalModal.vue` | `.ua-chip` | `var(--r-pill)` | `var(--qio-ui-radius, var(--r-lg))` |
| `components/conversation/UnifiedApprovalLayer.vue` | `.ua-layer-notice` | `var(--r-pill)` | `var(--qio-ui-radius, var(--r-lg))` |

## 二、圆形 → 14px（3 处）

| 界面 | 文件 | 选择器 | 原来 | 现在 |
| --- | --- | --- | --- | --- |
| 全局栏/窗口 | `components/WindowChrome.vue` | `.conversation-bar .chrome-btn` | `50%` | `var(--qio-ui-radius)` |
| 审批/反馈 | `components/approval/UnifiedApprovalModal.vue` | `.ua-close` | `50%` | `var(--qio-ui-radius, var(--r-lg))` |
| 记忆星球页 | `components/planet/EntityCandidates.vue` | `.ec-mark` | `50%` | `var(--qio-ui-radius)` |

## 三、其它半径 → 14px（按文件汇总）

| 界面 | 文件 | 处数 | 变化 |
| --- | --- | --- | --- |
| 互动页 | `components/interactive/BoardCard.vue` | 15 | `.card` var(--r-lg)→var(--qio-ui-radius); `.chip-btn` var(--r-lg)→var(--qio-ui-radius); `.blk :deep(code)` var(--r-lg)→var(--qio-ui-radius); `.blk-code` var(--r-lg)→var(--qio-ui-radius); `.code-copy` var(--r-lg)→var(--qio-ui-radius); `.link-edit` var(--r-lg)→var(--qio-ui-radius); `.legacy-note` var(--r-lg)→var(--qio-ui-radius); `.editor` var(--r-lg)→var(--qio-ui-radius); `.field input` var(--r-lg)→var(--qio-ui-radius); `.btn` var(--r-lg)→var(--qio-ui-radius); `.attach-status-action` var(--r-lg)→var(--qio-ui-radius); `.preview-placeholder` var(--r-lg)→var(--qio-ui-radius); `.more-panel` var(--r-lg)→var(--qio-ui-radius); `.tb` var(--r-lg)→var(--qio-ui-radius); `.card.is-cover > .card-head, .card.is-cover > .card-meta` var(--r-lg)→var(--qio-ui-radius) |
| 对话页 | `styles/base.css` | 14 | `.qio-input` var(--r-md)→var(--qio-ui-radius); `.qio-btn` var(--r-md)→var(--qio-ui-radius); `.qio-btn.mini` var(--r-sm)→var(--qio-ui-radius); `.qio-card` var(--r-lg)→var(--qio-ui-radius); `.qio-select` var(--r-md)→var(--qio-ui-radius); `.qio-select-menu` var(--r-md)→var(--qio-ui-radius); `.qio-select-menu .opt` var(--r-sm)→var(--qio-ui-radius); `.qio-glass` var(--r-lg)→var(--qio-ui-radius); `.qio-glass--panel` var(--r-xl)→var(--qio-ui-radius); `.qio-floating` var(--r-lg)→var(--qio-ui-radius); `.qio-row` var(--r-md)→var(--qio-ui-radius); `.qio-inline-edit` var(--r-md)→var(--qio-ui-radius); `.qio-confirm--inline` var(--r-md)→var(--qio-ui-radius); `.qio-confirm--popover,.qio-confirm--layer` var(--r-lg)→var(--qio-ui-radius) |
| 记忆星球页 | `views/PlanetView.vue` | 13 | `.webgl-fallback .wf-card` 14px→var(--qio-ui-radius); `.load-error` 10px→var(--qio-ui-radius); `.planet-loading` 10px→var(--qio-ui-radius); `.topic-hint` 20px→var(--qio-ui-radius); `.detail-identity` 10px→var(--qio-ui-radius); `.mode-btn` 20px→var(--qio-ui-radius); `.panel-toggle` 12px 0 0 12px→var(--qio-ui-radius) 0 0 var(--qio-ui-radius); `.topic-list li` 10px→var(--qio-ui-radius); `.fragment-item` 10px→var(--qio-ui-radius); `.fragment-raw` 0 8px 8px 0→0 var(--qio-ui-radius) var(--qio-ui-radius) 0; `.entity-tag` 20px→var(--qio-ui-radius); `.anchor-error` 8px→var(--qio-ui-radius); `.detail-error .err-text` 8px→var(--qio-ui-radius) |
| 互动页 | `components/interactive/SubmitCluster.vue` | 10 | `.sc-chip` var(--r-lg)→var(--qio-ui-radius); `.u-label` var(--r-lg)→var(--qio-ui-radius); `.approvals` var(--r-lg)→var(--qio-ui-radius); `.mini` var(--r-lg)→var(--qio-ui-radius); `.failure` var(--r-lg)→var(--qio-ui-radius); `.blocked` var(--r-lg)→var(--qio-ui-radius); `.details` var(--r-lg)→var(--qio-ui-radius); `.submit` var(--r-lg)→var(--qio-ui-radius); `.sc-panels` var(--r-lg)→var(--qio-ui-radius); `.sc-process-label` var(--r-lg)→var(--qio-ui-radius) |
| 记忆星球页 | `components/planet/MemoryPendingPanel.vue` | 8 | `.mp-tab` 8px→var(--qio-ui-radius); `.mp-item` var(--r-lg)→var(--qio-ui-radius); `.mp-error` 8px→var(--qio-ui-radius); `.mp-warn` 8px→var(--qio-ui-radius); `.mp-notice` 8px→var(--qio-ui-radius); `.mp-batch` var(--r-md)→var(--qio-ui-radius); `.mp-detail` var(--r-lg)→var(--qio-ui-radius); `.mp-preview-result, .mp-restore-result` var(--r-md)→var(--qio-ui-radius) |
| 对话页 | `views/ConversationView.vue` | 8 | `.left-pane` var(--r-lg)→var(--qio-ui-radius); `.right-pane` var(--r-lg)→var(--qio-ui-radius); `.splitter::after` 5px→var(--qio-ui-radius); `.collapsed-rail` var(--r-lg)→var(--qio-ui-radius); `.preview-overlay` var(--r-lg)→var(--qio-ui-radius); `.rail-drawer` var(--r-lg)→var(--qio-ui-radius); `.preview-reopen` var(--r-sm) 0 0 var(--r-sm)→var(--qio-ui-radius) 0 0 var(--r-sm); `.recovery-panel` var(--r-xl)→var(--qio-ui-radius) |
| 审批/反馈 | `components/ApprovalFacts.vue` | 7 | `.lifecycle` var(--r-md)→var(--qio-ui-radius, var(--r-lg)); `.qio-explanation` var(--r-md)→var(--qio-ui-radius, var(--r-lg)); `.af-ops` var(--r-md)→var(--qio-ui-radius, var(--r-lg)); `.af-pre` var(--r-sm)→var(--qio-ui-radius, var(--r-lg)); `.adv-raw` var(--r-sm)→var(--qio-ui-radius, var(--r-lg)); `.cap-box` 8px→var(--qio-ui-radius, var(--r-lg)); `.budget-box` 8px→var(--qio-ui-radius, var(--r-lg)) |
| 审批/反馈 | `components/approval/UnifiedApprovalModal.vue` | 7 | `.ua-modal` var(--r-lg)→var(--qio-ui-radius, var(--r-lg)); `.ua-nav-btn` var(--r-sm)→var(--qio-ui-radius, var(--r-lg)); `.ua-source` var(--r-md)→var(--qio-ui-radius, var(--r-lg)); `.ua-stale` var(--r-md)→var(--qio-ui-radius, var(--r-lg)); `.ua-refresh-btn` var(--r-sm)→var(--qio-ui-radius, var(--r-lg)); `.ua-error` var(--r-md)→var(--qio-ui-radius, var(--r-lg)); `.ua-btn` var(--r-md)→var(--qio-ui-radius, var(--r-lg)) |
| 互动页 | `components/interactive/CardInlinePreview.vue` | 7 | `.cip` var(--r-lg)→var(--qio-ui-radius); `.cip-close, .cip-action, .cip-tools button, .cip-tab` var(--r-lg)→var(--qio-ui-radius); `.cip-jump` var(--r-lg)→var(--qio-ui-radius); `.cip-canvas-box` var(--r-lg)→var(--qio-ui-radius); `.cip-pre` var(--r-lg)→var(--qio-ui-radius); `.cip-img-detail-state p` var(--r-lg)→var(--qio-ui-radius); `.cip-img-detail-actions button` var(--r-lg)→var(--qio-ui-radius) |
| 对话页 | `components/Composer.vue` | 6 | `.attach-menu` var(--r-md)→var(--qio-ui-radius); `.attach-menu-item` var(--r-sm)→var(--qio-ui-radius); `.path-input` var(--r-xs)→var(--qio-ui-radius); `.path-btn` var(--r-xs)→var(--qio-ui-radius); `.attach-reject` var(--r-md)→var(--qio-ui-radius); `.drop-hint` var(--r-lg)→var(--qio-ui-radius) |
| 互动页 | `components/interactive/BoardToolbar.vue` | 6 | `.board-toolbar` var(--r-lg)→var(--qio-ui-radius); `.tb-notice` var(--r-lg)→var(--qio-ui-radius); `.tb-btn` var(--r-lg)→var(--qio-ui-radius); `.tb-help-pop` var(--r-lg)→var(--qio-ui-radius); `.tb-annotate` var(--r-lg)→var(--qio-ui-radius); `.tb-annotate-item` var(--r-lg)→var(--qio-ui-radius) |
| 设置页 | `views/SettingsView.vue` | 6 | `.tab` 0 var(--r-sm) var(--r-sm) 0→0 var(--qio-ui-radius) var(--qio-ui-radius) 0; `.avail` var(--r-sm)→var(--qio-ui-radius); `.seg` var(--r-md)→var(--qio-ui-radius); `.theme-opt, .motion-opt, .scroll-opt` var(--r-sm)→var(--qio-ui-radius); `.adv-fold` var(--r-md)→var(--qio-ui-radius); `.license-text` var(--r-sm)→var(--qio-ui-radius) |
| 对话页 | `components/MessageItem.vue` | 5 | `.system-fact` var(--r-xs)→var(--qio-ui-radius); `.copy-btn:focus-visible` var(--r-xs)→var(--qio-ui-radius); `.tool-card` var(--r-lg)→var(--qio-ui-radius); `.tool-status` 20px→var(--qio-ui-radius); `.subagent-card` var(--r-lg)→var(--qio-ui-radius) |
| 对话页 | `components/conversation/ChatColumn.vue` | 5 | `.topic-main` var(--r-lg)→var(--qio-ui-radius); `.topic-details` var(--r-md)→var(--qio-ui-radius); `.notice` var(--r-md)→var(--qio-ui-radius); `.message-stage` var(--r-lg)→var(--qio-ui-radius); `.skip-link:focus` var(--r-sm)→var(--qio-ui-radius) |
| 对话页 | `components/conversation/TaskRailItem.vue` | 5 | `.task-item` var(--r-md)→var(--qio-ui-radius); `.task-trigger:focus-visible` var(--r-xs)→var(--qio-ui-radius); `.task-output-preview` var(--r-sm)→var(--qio-ui-radius); `.task-part-text` var(--r-sm)→var(--qio-ui-radius); `.task-confirm` var(--r-sm)→var(--qio-ui-radius) |
| 互动页 | `components/interactive/AddMenu.vue` | 5 | `.tb-btn` var(--r-lg)→var(--qio-ui-radius); `.add-pop` var(--r-lg)→var(--qio-ui-radius); `.pop-item` var(--r-lg)→var(--qio-ui-radius); `.add-status` var(--r-lg)→var(--qio-ui-radius); `.status-action` var(--r-lg)→var(--qio-ui-radius) |
| 对话页 | `components/TurnProcess.vue` | 4 | `.tp-toggle` var(--r-xs)→var(--qio-ui-radius); `.tp-outcome-pre` var(--r-sm)→var(--qio-ui-radius); `.tp-cur-detail-toggle` var(--r-xs)→var(--qio-ui-radius); `.tp-approval` var(--r-md)→var(--qio-ui-radius) |
| 全局栏/窗口 | `components/WindowChrome.vue` | 4 | `.chrome-btn` var(--r-sm)→var(--qio-ui-radius); `.conversation-bar .wc-btn` var(--r-sm)→var(--qio-ui-radius); `.close-ask` var(--r-lg)→var(--qio-ui-radius); `.close-busy` var(--r-md)→var(--qio-ui-radius) |
| 审批/反馈 | `components/approval/ApprovalResultsPanel.vue` | 4 | `.ar-alert` var(--r-md)→var(--qio-ui-radius, var(--r-lg)); `.ar-btn` var(--r-sm)→var(--qio-ui-radius, var(--r-lg)); `.ar-legacy` var(--r-md)→var(--qio-ui-radius, var(--r-lg)); `.ar-entry` var(--r-md)→var(--qio-ui-radius, var(--r-lg)) |
| 互动页 | `components/interactive/CardAttachmentPreview.vue` | 4 | `.thumb` var(--r-xs)→var(--qio-ui-radius); `.btn` var(--r-xs)→var(--qio-ui-radius); `.cover-reason` var(--r-xs)→var(--qio-ui-radius); `.cover-loading` var(--r-xs)→var(--qio-ui-radius) |
| 记忆星球页 | `components/planet/EntityCandidates.vue` | 4 | `.ec-head` var(--r-md)→var(--qio-ui-radius); `.ec-panel` var(--r-md)→var(--qio-ui-radius); `.ec-row` var(--r-md)→var(--qio-ui-radius); `.ec-error` var(--r-sm)→var(--qio-ui-radius) |
| 对话页 | `views/DebugView.vue` | 4 | `.list-sec, .detail-sec` 12px→var(--qio-ui-radius); `.row` 9px→var(--qio-ui-radius); `.badge` 20px→var(--qio-ui-radius); `.json` 8px→var(--qio-ui-radius) |
| 审批/反馈 | `components/approval/KnowledgeApprovalBody.vue` | 3 | `.content` var(--r-md)→var(--qio-ui-radius, var(--r-lg)); `.edit-area` var(--r-md)→var(--qio-ui-radius, var(--r-lg)); `.detail-retry` var(--r-sm)→var(--qio-ui-radius, var(--r-lg)) |
| 对话页 | `components/conversation/PreviewPanel.vue` | 3 | `.pv-tab-main:focus-visible` var(--r-xs)→var(--qio-ui-radius); `.pv-tab-close` var(--r-xs)→var(--qio-ui-radius); `.pv-retry` var(--r-xs)→var(--qio-ui-radius) |
| 对话页 | `components/conversation/SendIssuesPanel.vue` | 3 | `.notice` var(--r-sm)→var(--qio-ui-radius); `.item` var(--r-md)→var(--qio-ui-radius); `.actions .qio-btn` var(--r-sm)→var(--qio-ui-radius) |
| 对话页 | `components/conversation/TaskRail.vue` | 3 | `.rail-tabs` var(--r-sm)→var(--qio-ui-radius); `.rail-tabs button` var(--r-xs)→var(--qio-ui-radius); `.rail-feedback` var(--r-sm)→var(--qio-ui-radius) |
| 对话页 | `components/conversation/preview/PreviewPdfViewer.vue` | 3 | `.pv-canvas-pan` var(--r-sm)→var(--qio-ui-radius); `.pv-busy` var(--r-xs)→var(--qio-ui-radius); `.pv-mini-btn` var(--r-xs)→var(--qio-ui-radius) |
| 对话页 | `components/conversation/preview/PreviewReader.vue` | 3 | `.pv-search-input` var(--r-xs)→var(--qio-ui-radius); `.pv-mini-btn` var(--r-xs)→var(--qio-ui-radius); `.pv-page-jump` var(--r-xs)→var(--qio-ui-radius) |
| 对话页 | `components/conversation/preview/PreviewStructuredPane.vue` | 3 | `.pv-block.is-hit, .pv-slide.is-hit` var(--r-xs)→var(--qio-ui-radius); `.pv-sheet-tab` var(--r-xs)→var(--qio-ui-radius); `.pv-grid-pan` var(--r-sm)→var(--qio-ui-radius) |
| 记忆星球页 | `components/planet/EntityPanel.vue` | 3 | `.e-item` var(--r-lg)→var(--qio-ui-radius); `.chip` 20px→var(--qio-ui-radius); `.e-load-error` 8px→var(--qio-ui-radius) |
| 记忆星球页 | `components/planet/KnowledgePanel.vue` | 3 | `.create-form` 10px→var(--qio-ui-radius); `.k-item` var(--r-lg)→var(--qio-ui-radius); `.k-load-error` 8px→var(--qio-ui-radius) |
| 对话页 | `components/ui/QCombo.vue` | 3 | `.qio-combo-trigger` var(--r-md)→var(--qio-ui-radius); `.qio-combo-menu` var(--r-md)→var(--qio-ui-radius); `.qio-combo-opt` var(--r-sm)→var(--qio-ui-radius) |
| 对话页 | `components/AttachmentChip.vue` | 2 | `.act` 8px→var(--qio-ui-radius); `.more-menu` var(--r-md)→var(--qio-ui-radius) |
| 对话页 | `components/NarrativeStage.vue` | 2 | `.nhead:focus-visible` var(--r-xs)→var(--qio-ui-radius); `.callrow` var(--r-sm)→var(--qio-ui-radius) |
| 对话页 | `components/conversation/ComposerEntries.vue` | 2 | `.panel` var(--r-md)→var(--qio-ui-radius); `.simple .qio-btn` var(--r-sm)→var(--qio-ui-radius) |
| 对话页 | `components/conversation/QueuePanel.vue` | 2 | `.row` var(--r-md)→var(--qio-ui-radius); `.row-actions .qio-btn` var(--r-sm)→var(--qio-ui-radius) |
| 对话页 | `components/conversation/preview/PreviewImageViewer.vue` | 2 | `.pv-pan` var(--r-sm)→var(--qio-ui-radius); `.pv-mini-btn` var(--r-xs)→var(--qio-ui-radius) |
| 互动页 | `components/interactive/FileCardMaterialPanel.vue` | 2 | `.kind-tag` var(--r-xs)→var(--qio-ui-radius); `.relocate-btn` var(--r-xs)→var(--qio-ui-radius) |
| 互动页 | `components/interactive/v2/ImpactConfirmLayer.vue` | 2 | `.ic-deferred` var(--r-lg)→var(--qio-ui-radius); `.ic-item` var(--r-md)→var(--qio-ui-radius) |
| 互动页 | `components/interactive/v2/InteractiveChromeTools.vue` | 2 | `.iw-search-results` var(--r-lg)→var(--qio-ui-radius); `.iw-search-result` var(--r-lg)→var(--qio-ui-radius) |
| 互动页 | `components/interactive/v2/RecoveryPanel.vue` | 2 | `.rp-block` var(--r-lg)→var(--qio-ui-radius); `.rp-btn, .rp-link` var(--r-lg)→var(--qio-ui-radius) |
| 互动页 | `components/interactive/v2/RetryPanel.vue` | 2 | `.retry-refresh, .retry-btn, .retry-link` var(--r-lg)→var(--qio-ui-radius); `.retry-item` var(--r-lg)→var(--qio-ui-radius) |
| 互动页 | `components/interactive/v2/SaveFailureBanner.vue` | 2 | `.save-failure` var(--r-lg)→var(--qio-ui-radius); `.act` var(--r-lg)→var(--qio-ui-radius) |
| 互动页 | `components/interactive/v2/TaskCenterPanel.vue` | 2 | `.tc` var(--r-lg)→var(--qio-ui-radius); `.tc-row` var(--r-md)→var(--qio-ui-radius) |
| 互动页 | `interactive/TextCardEditor.vue` | 2 | `.fmt` var(--r-xs)→var(--qio-ui-radius); `.editor` var(--r-xs)→var(--qio-ui-radius) |
| 互动页 | `views/InteractiveView.vue` | 2 | `.im-floating-chat :deep(.ic-stream)` var(--r-lg)→var(--qio-ui-radius); `.im-notice` var(--r-sm)→var(--qio-ui-radius) |
| 对话页 | `components/DevTaskEntry.vue` | 1 | `.dev-task-panel` var(--r-2)→var(--qio-ui-radius, var(--r-lg)) |
| 对话页 | `components/MessageStream.vue` | 1 | `.empty .chip` 20px→var(--qio-ui-radius) |
| 对话页 | `components/RecoveryInbox.vue` | 1 | `.recovery-inbox` var(--r-lg)→var(--qio-ui-radius) |
| 对话页 | `components/ToolCreationCard.vue` | 1 | `.create-card` var(--r-lg)→var(--qio-ui-radius, var(--r-lg)) |
| 对话页 | `components/conversation/preview/PreviewTextPane.vue` | 1 | `.pv-code` var(--r-sm)→var(--qio-ui-radius) |
| 对话页 | `components/credentials/CredentialForm.vue` | 1 | `.target-warning` var(--r-md)→var(--qio-ui-radius) |
| 互动页 | `components/interactive/ExecutionRecordPanel.vue` | 1 | `.erp` var(--r-lg)→var(--qio-ui-radius) |
| 对话页 | `components/onboarding/OnboardingWizard.vue` | 1 | `.onboarding-card` 20px→var(--qio-ui-radius) |
| 对话页 | `components/ui/QNumber.vue` | 1 | `.q-number` 10px→var(--qio-ui-radius) |
| 对话页 | `views/settings/CredentialModal.vue` | 1 | `.modal` var(--r-lg)→var(--qio-ui-radius) |

## 四、本轮（记忆星球页改版）动过的圆角

| 文件 | 变化 | 说明 |
| --- | --- | --- |
| `frontend/src/components/Composer.vue` | `.input-pill`：14px → `--r-pill` | 2026-10-27 按用户要求恢复胶囊形（唯一反向操作） |
| `frontend/src/views/PlanetView.vue` | `.detail-keywords .kw`：胶囊 → 14px | 本轮唯一被压平的胶囊（话题概览的关键词小标签） |
| 本轮新增文件（ManagementArea / MemoryTab / PageTools / GlobalSearchPanel / PendingItems / MaintenanceRecords 等） | 全部 `--qio-ui-radius` | 新页面按本页规则从 0 起统一 14px，不存在「原来是胶囊被压平」 |

## 五、若要恢复，建议顺序（按视觉影响）

1. **圆形按钮**：`.conversation-bar .chrome-btn`（全局栏）、`.ua-close`（审批弹窗关闭）、`.ec-mark`（星球页）。
2. **对话页高频胶囊**：`.chip`（附件）、`.code-preview-btn`（代码预览）、`.topic-chip`、`.mode-btn`（模式切换）、`.task-action`、`.back-latest .count`、`.attach-btn` / `.confirm-btn` / `.draft-retry`。
3. **通用原语**（改一处影响全站）：`.qio-badge` / `.qio-tag` / `.qio-state` / `.qio-glass--chip`（原胶囊）、`.qio-input` / `.qio-btn`（原 10px）。
4. **互动页**：`.im-chip` / `.tc-tab` / `.tc-status` / `.tb-btn` / `.iw-search-field`（原胶囊）。
5. 面板/菜单类（220 处里的绝大多数）：多为 8/10/12px → 14px，视觉差异小，建议保持现状。

> 恢复方式：逐个把 `border-radius` 改回原值，并在 `frontend/src/design-guards.test.ts` 或对应组件测试里加断言锁住（与本次输入胶囊的做法一致）。

## 六、按提交归属（上一轮内部）

范围拆分（按选择器比对，不是按 diff 行数）：

| 范围 | 压平处数 | 说明 |
| --- | --- | --- |
| `e5c80253..6dc57885`（统一改造轮 `fix/ui-feedback-unification-20261026`） | **273**（胶囊 51 / 圆形 3 / 其它 219），涉及 60 个文件 | 全部压平都在这一轮 |
| `6dc57885..d8cdcc41`（后续修复轮 `fix/ui-feedback-followup-20261027`，即本基线的最后一轮） | **0** | 没有动任何圆角 |

统一改造轮内部，11 个提交动过 `border-radius`（按 diff 行统计，含同文件内规则重组，故行数大于实际形状变化处数）：

| 提交 | 删/增（行） | 主题 |
| --- | --- | --- |
| `c066bbfa` | 177 / 172 | 对话/互动全部表面统一 14px 圆角 |
| `45144864` | 62 / 68 | 设置/调试/记忆/引导/凭据的反馈与弹窗统一 |
| `e61f76a2` | 27 / 27 | 审批队列标签与结果状态行；B 归属组件外轮廓统一（去 999/8/10px） |
| `30cc0496` | 23 / 34 | 基础控件统一（14px 圆角 token、QIcon/QIconButton/QTip/QModal、QConfirm） |
| `abb0c0d0` | 7 / 21 | 顶部通知/全局异常入口与面板/任务失败监听 |
| `162d6bf3` | 3 / 4 | 审批复选框、开发任务入口与工具创建卡圆角 token |
| `9bd613e0` | 1 / 2 | 窗口控制与更新开关圆角统一 14px |
| `f4125fb5` | 1 / 1 | 对话页异常不再重复常驻 |
| `31b7ab8c` / `4e4cac8f` / `2af0628d` | 0 / 2~3 或 2 / 0 | 新增卡片/面板的表面圆角 |

> 除 2026-10-27 已恢复的 `.input-pill`（输入胶囊）外，其余 272 处目前仍是 14px。
