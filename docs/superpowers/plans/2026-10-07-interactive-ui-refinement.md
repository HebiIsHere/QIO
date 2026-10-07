# 互动板修复与前端审美优化实施方案（2026-10-07 第二轮）

## 1. 实际基线

- 远端核对：`origin/feat/interactive-frontend-layout` = **`453857857cb7d0a12234ce6a3baa7128ab50eca7`**（自参考提交以来**没有新提交**）；
  产品实现对应 `13066119a56eed19b9f48a7b8ec202db03e4d243`，之后的 `4538578` 只加了验收脚本。
  `origin/main` = `6e073e9`，`fix/unified-process-audit`、`feat/unified-process-attachments-streaming` 未动。
- 本轮分支：`fix/interactive-ui-refinement`（从 `4538578` 起），集成工作区 `D:\qio-dev\qio-ui`；
  四个子工作区 `qio-ui-a/b/c/d`（`wt/ui-a-visual` / `wt/ui-b-rules` / `wt/ui-c-drafts` / `wt/ui-d-overlay`）。
- 不合并 `main`、不合并另两条开发线、不改历史迁移、不新增数据库结构。
- **用户规则优先**：本提示词里的规则覆盖此前写偏的本地契约与测试（见契约 §9）。

## 2. 五项修复的落点（已勘察确认）

| # | 问题 | 现状（代码位置） | 目标 |
| --- | --- | --- | --- |
| 1 | 默认组名 | 后端 `models.default_group_name(index)` → 「组 N」；前端 `board.ts` 的 `DEFAULT_GROUP_NAME_PREFIX = "组"` + `nextDefaultGroupName` → 「组 N」 | 统一为**「默认组名」**；`defaultName` 只表示「系统默认名」；组身份按 **id**；历史「组 N」不被批量覆盖 |
| 2 | 批次猜测 | `approval.ts:637` 用 `createdAt` 截断到秒做第三级归批（实测把两批各两项误并成四项） | **删除时间推断**；只用会话记录与真实 `submissionId`；都不可靠时各自成批 |
| 3 | 批量列表过早消失 | `batchesWithList` 按**待审批数** ≥4 过滤，处理掉一项就消失 | 按**该批总量** ≥4 判定资格，只要还有未处理项就保留入口；显示剩余数；处理完再消失 |
| 4 | 聊天草稿不持久 | `session.ts:423` 的 `draft` 只在内存，新建状态即空 | 按会话/话题持久保存；发送成功才删；失败恢复且不覆盖新输入；旧回执不覆盖新草稿 |
| 5 | 卡片草稿静默失败 | `stores/interactive.ts:300` 的 `.catch(() => undefined)` | 真实保存状态 + 失败原因 + 重试；离开编辑器前落盘；重试不建卡、不提交、不调 QIO |

## 3. 并行分工（同一时间一个文件只有一个写者）

| 角色 | 只写这些文件 |
| --- | --- |
| **主智能体** | `frontend/src/views/InteractiveView.vue`、`frontend/src/interactive/types.ts`、`frontend/src/services/interactive.ts`、`frontend/src/interactive/{drafts,overlayLayout}.ts` 的**冻结骨架**、`scripts/interactive-verify/fe-scenarios.mjs`、`docs/**`、集成与最终验收 |
| **A 布局与审美** | `components/interactive/BoardToolbar.vue`、`AddMenu.vue`、`BoardSearchPanel.vue`、`BoardCard.vue`、`BoardGroupFrame.vue`、新建 `SelectionMenu.vue`、`components/interactive/__tests__/BoardToolbar.test.ts` |
| **B 批次与分组规则** | `frontend/src/interactive/board.ts`、`frontend/src/interactive/approval.ts`、`backend/src/agent/interactive/models.py`、`backend/src/agent/interactive/board.py`、`frontend/src/interactive/__tests__/{board,approval}.test.ts`、`backend/tests/test_interactive_board.py` |
| **C 草稿与恢复** | `frontend/src/stores/session.ts`、`frontend/src/stores/interactive.ts`、`frontend/src/interactive/drafts.ts`、`components/interactive/ChatDock.vue`、新建 `CardDraftHint.vue`、`frontend/src/interactive/__tests__/drafts.test.ts`、`frontend/src/stores/__tests__/draftPersistence.test.ts` |
| **D 浮层与独立验收** | `frontend/src/interactive/overlayLayout.ts`、`components/interactive/IntentBatchTray.vue`、`IntentStatusPopover.vue`、`ImpactConfirmDialog.vue`、`frontend/src/styles/interactive-shell.css`、`frontend/src/interactive/__tests__/overlayLayout.test.ts`、`docs/interactive-ui-verify.md`、`scripts/interactive-verify/fe-verify-d.mjs` |

淘汰/交接说明：`stores/session.ts` 与 `stores/interactive.ts` 本轮**交给 C 独占**（草稿持久化必须单一写者），
主智能体不再改这两个文件；`styles/interactive-shell.css` 交给 D（跨浮层避让与并排策略）。

## 4. 冻结接口（骨架已落盘，各自实现，签名不改）

```ts
// interactive/drafts.ts（C 实现）：草稿持久化的纯逻辑（存储、序号、过期回执判定）
export interface DraftRecord { text: string; updatedAt: number; seq: number }
export function draftStorageKey(scope: "chat" | "card", id: string): string;
export function readDraft(key: string): DraftRecord | null;
export function writeDraft(key: string, text: string, seq: number): { ok: boolean; error?: string };
export function removeDraft(key: string): void;
export function isStaleReceipt(receiptSeq: number, currentSeq: number): boolean;

// interactive/overlayLayout.ts（D 实现）：跨浮层的可用区域与并排策略（纯几何，可单测）
export interface OverlayInput {
  viewport: { width: number; height: number };
  stage: { top: number; height: number };
  toolbarTop: number;
  chatOpen: boolean;
  batchOpen: boolean;
  chatMin: { width: number; height: number };
  batchMin: { width: number; height: number };
}
export interface OverlayGeometry {
  mode: "side-by-side" | "stacked" | "switched";
  chatMaxWidth: number; chatMaxHeight: number; chatRight: number;
  batchMaxWidth: number; batchMaxHeight: number; batchRight: number;
  gap: number;
}
export function planOverlayGeometry(input: OverlayInput): OverlayGeometry;
```

## 5. 验收方法

1. 规则与恢复 8 条（提示词第四节）：默认命名全路径、同秒两批不误合、批量入口保留到处理完、草稿跨刷新/话题恢复、
   失败与旧回执、卡片草稿失败可重试、未勾选注释与删除路径、聊天与提交请求互相独立。
2. 审美对照：1440×900 / 1024×768 / 800×600 / 最小窗口 × 暗色与亮色，改前改后同尺寸同数据截图，
   测量工具栏高度（800×600 ≤ 约 96px）、消息可读区、浮层相交面积（原 45080px² 必须归零）、按钮可点范围。
   截图**提交进仓库** `docs/interactive-ui-screenshots/`，不放 `%TEMP%` 了事。
3. 命令：`npx vue-tsc --noEmit`、`npx vitest run`、`cd backend; uv run --frozen pytest`、`python scripts/check_docs.py`。
4. 反例测试：旧实现必须过不了新规则（默认组名、时间归批、待审批数阈值、草稿恢复各一条）。
5. 修掉上一轮验收脚本的时序问题（等待真实保存/提交结果，不用固定延迟掩盖）；健康探测 130ms 现象在相同环境复核并记录。
6. 独立复核（D）亲自读源码 + 起应用操作，产出 `docs/interactive-ui-verify.md`，不采信开发方报告。

## 6. 语言

主智能体与全部子智能体：方案、分工、协调、进度、注释、新增文档、提交说明、最终报告**一律中文**。
