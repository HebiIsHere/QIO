# 互动页 v2 第二迭代实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax.

**Goal:** 在不抬高板面的前提下加入悬浮对话/工具栏、卡片尺寸驱动详细状态、点阵纸背景，并以新的执行记录/提交详情替换旧重叠入口。

**Architecture:** 纯前端改版为主。先把“详细状态”和“点阵”做成独立纯逻辑/CSS，再改 BoardCard 和 BoardCanvas；随后把对话与工具栏合并成 Board 上方的悬浮层；最后补记录/提交详情并删除旧组件。后端只读现有意图/提交事实，不改 schema。

**Tech Stack:** Vue 3、Pinia、TypeScript、Vitest、CSS var(--*) 令牌。

**Spec:** docs/superpowers/specs/2026-10-24-interactive-v2-refinement.md

## Global Constraints
- 颜色一律使用 var(--*)，禁止硬编码色值。
- 对话页视觉和交互不得回退。
- 提交给 QIO 与聊天发送永久分离。
- 卡片详细状态不使用板面缩放判断。
- 图片保持原图比例，不拉伸。
- 不引入真实 API Key 或联网测试。
- 每个任务先写失败测试，再实现，再跑定向测试并提交。

## Review Focus
- 320/480 窄窗：悬浮对话、工具栏、模式切换和输入区不能互相遮挡，页面不能结构横向滚动。
- 对话展开/收起：草稿、附件、排队、流式回复、失败恢复不丢失。
- 卡片尺寸边界：进入/退出阈值附近不抖动，组缩放和恢复路径一致。
- 图片极端比例：长图/竖图能进入完整图片；不能每张图都退回旧按钮。
- 旧入口删除后的唯一性：确认、状态、记录、提交详情各只有一个入口。

---

### Task 1: 卡片详细状态纯逻辑

**Files:**
- Create: frontend/src/interactive/cardDetail.ts
- Test: frontend/src/interactive/__tests__/cardDetail.test.ts

**Interfaces:**
- Produces:
  - type CardDetailInput = { width: number; height: number; aspectRatio?: number | null; previousDetailed?: boolean }
  - function shouldAutoDetail(input: CardDetailInput): boolean
  - function shouldShowImageDetailFallback(input: CardDetailInput): boolean
  - constants DETAIL_ENTER_WIDTH=420, DETAIL_ENTER_HEIGHT=280, DETAIL_EXIT_WIDTH=360, DETAIL_EXIT_HEIGHT=240, IMAGE_DETAIL_ENTER_AREA=117600, IMAGE_DETAIL_ENTER_SHORT=200, IMAGE_DETAIL_EXIT_AREA=100800, IMAGE_DETAIL_EXIT_SHORT=180
- Behavior:
  - ordinary enter >=420x280; exit <360 or <240; previousDetailed provides hysteresis.
  - image uses short side + area; extreme ratio fallback when not detailed and (aspectRatio >=3 or <=1/3) and not auto-detailable at current size.
  - no board scale input.

- [ ] Step 1: Write failing tests for ordinary enter/exit/hysteresis/image fallback.
- [ ] Step 2: Run npx vitest run src/interactive/__tests__/cardDetail.test.ts and confirm fail.
- [ ] Step 3: Implement cardDetail.ts.
- [ ] Step 4: Run test and pass.
- [ ] Step 5: Commit.

### Task 2: BoardCard 自动详细与图片兜底

**Files:**
- Modify: frontend/src/components/interactive/BoardCard.vue
- Test: frontend/src/components/interactive/__tests__/BoardCardAutoDetail.test.ts

- [ ] Step 1: Test ordinary card at 420x280 shows detailed content without button; at 359x239 returns simple; image fallback eye appears only for extreme ratio; eye enters/exits detail; no “展开预览” text.
- [ ] Step 2: Run test and confirm fail.
- [ ] Step 3: Replace previewExpanded button entry with computed auto detail; retain reading state; add image fallback eye; preserve scroll/page state.
- [ ] Step 4: Run test and existing card tests, pass.
- [ ] Step 5: Commit.

### Task 3: 悬浮对话壳与工具栏槽

**Files:**
- Modify: frontend/src/components/interactive/v2/InteractiveConversation.vue
- Modify: frontend/src/components/interactive/v2/InteractiveWorkspace.vue
- Modify: frontend/src/views/InteractiveView.vue
- Test: frontend/src/components/interactive/v2/__tests__/InteractiveConversation.test.ts
- Test: frontend/src/views/__tests__/InteractiveViewFloating.test.ts

**Interfaces:**
- InteractiveConversation adds a #toolbar slot rendered after panel, before composer bar.
- InteractiveWorkspace switches to board-full layout; floating chat is page-owned, not composer slot.
- Page keeps one #composer and one #toolbar; board remains unpressed.

- [ ] Step 1: Test toolbar slot exists and expanding messages does not change board height; collapse keeps toolbar/input; board click collapses.
- [ ] Step 2: Run tests and fail.
- [ ] Step 3: Implement floating chat container and slot; remove composer layout reservation.
- [ ] Step 4: Run tests and pass; visual screenshot 1440/480.
- [ ] Step 5: Commit.

### Task 4: 工具栏精简与提交状态图标

**Files:**
- Modify: frontend/src/components/interactive/BoardToolbar.vue
- Modify: frontend/src/components/interactive/SubmitCluster.vue
- Create: frontend/src/components/interactive/SubmitDetailsPanel.vue
- Test: frontend/src/components/interactive/__tests__/BoardToolbar.test.ts
- Test: frontend/src/components/interactive/__tests__/SubmitDetailsPanel.test.ts

- [ ] Step 1: Test no search button, add/undo/redo/annotate/bookmark/submit remain; submit status is icon-only; clicking opens details.
- [ ] Step 2: Run tests and fail.
- [ ] Step 3: Remove search, create status icon and details panel; keep separate chat send.
- [ ] Step 4: Run tests and pass; screenshot.
- [ ] Step 5: Commit.

### Task 5: 点阵纸背景

**Files:**
- Modify: frontend/src/components/interactive/BoardCanvas.vue
- Test: frontend/src/components/interactive/__tests__/boardGrid.test.ts

- [ ] Step 1: Test light theme uses white surface and 24px accent dot grid; dark uses dark surface; grid is behind cards.
- [ ] Step 2: Run test and fail.
- [ ] Step 3: Implement CSS variables/pseudo-element; no hardcoded colors.
- [ ] Step 4: Run test and pass; screenshots light/dark.
- [ ] Step 5: Commit.

### Task 6: 执行记录入口

**Files:**
- Create: frontend/src/components/interactive/ExecutionRecordPanel.vue
- Modify: frontend/src/components/interactive/v2/IntentLayer.vue
- Modify: frontend/src/components/interactive/BoardCard.vue
- Test: frontend/src/components/interactive/__tests__/ExecutionRecordPanel.test.ts

- [ ] Step 1: Test opening from intent and result card “更多”; record shows confirmation, scope, operations, result, failure, retry.
- [ ] Step 2: Run test and fail.
- [ ] Step 3: Implement panel and entry; reuse existing intent/ledger data.
- [ ] Step 4: Run tests and pass.
- [ ] Step 5: Commit.

### Task 7: 删除旧入口

**Files:**
- Delete: IntentBatchTray.vue, IntentStatusPopover.vue, IntentWorkbench.vue, DemoIntentEntry.vue, ChatDock.vue
- Modify: InteractiveView.vue and old tests.
- Test: frontend/src/views/__tests__/InteractiveOldEntriesGone.test.ts

- [ ] Step 1: Test no old entry DOM and no imports.
- [ ] Step 2: Run test and fail.
- [ ] Step 3: Remove imports/usage/files/tests; ensure new entries remain.
- [ ] Step 4: Run full front tests and typecheck.
- [ ] Step 5: Commit.

### Task 8: 整体验收

- [ ] Run npx vue-tsc --noEmit.
- [ ] Run npx vitest run.
- [ ] Run npm run build.
- [ ] Start servers and capture 1440/1024/800/480/320 light/dark.
- [ ] Verify conversation page no regression, submit/send separation, no horizontal overflow.
- [ ] Update docs/status.md and docs/interactive-v2/delivery.md.
- [ ] Commit.
