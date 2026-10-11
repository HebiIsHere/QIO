# 互动板草稿竞态与界面收敛实施方案（2026-10-08 第三轮）

## 1. 实际基线

- 远端核对：`origin/fix/interactive-ui-refinement` = **`224bc63d3c3b72c1897a147491748c4cb76429d7`**
  （`git ls-remote` + `git log 224bc63..origin/...` 均为空 —— **远端没有更新的修复**，本轮问题全部仍然存在）。
- 本轮分支：`fix/interactive-draft-races-ui-polish`（从 `224bc63` 起），集成工作区 `D:\qio-dev\qio-polish`；
  四个子工作区 `qio-polish-a/b/c/d`（`wt/polish-a-session` / `wt/polish-b-card` / `wt/polish-c-layout` / `wt/polish-d-verify`）。
- 不合并 `main`、不合并另两条开发线、不改数据库结构与历史迁移；不开发真实 QIO 板面理解、附件系统、任务执行机制。
- **用户已确认的规则优先于写偏的注释与测试**（互动契约 §9 + 本轮 §10）。

## 2. 已核实的六个问题与源码位置

| # | 问题 | 现状位置（已读码确认） |
| --- | --- | --- |
| 1 | 发送失败把原文恢复到**别的话题** | `ChatDock.vue:253 submit()` 在 `await session.send()` 之后用 `if (!draft.value.trim()) draft.value = snapshot` 恢复 —— `draft` 是**当前话题**的；`Composer.vue:88/98` 同样只看当前输入是否为空 |
| 2 | 成功的旧回执删掉后来写的新草稿 | `session.ts:2180 discardAfterSend(sentKey)`：`sentKey !== key` 时直接 `removeDraft(sentKey)`，**不带版本**；`session.ts:1965` 传的也只是键 |
| 3 | 慢保存失败后卡片新草稿永久停在 saving | `interactive.ts:382 flushDrafts()`：`if (draftInFlight) { await draftInFlight; return; }` 丢掉后来的版本；失败分支 `return` 在 `try` 内 → 第 426 行的「还有未保存就再排一次」**不会执行** |
| 4 | 空草稿不是有效状态 | `BoardCard.vue:162 startEdit()`：`store.draftFor(key) || props.card.content` —— 空字符串被判成「没有草稿」 |
| 5 | 几何模式变了却不落实面板切换 | `IntentBatchTray.vue applyGeometry()` 只更新 `cramped`，窗口变化只重测量，没有按新结果执行 `enforceSinglePane()` |
| 6 | 常驻模式改变已确认手势 | `BoardToolbar.vue` 的 选择/框选/连线 + `BoardCanvas.vue` 的 `mode === 'rect' || spaceDown` |

## 3. 并行分工与文件所有权（同一文件同一时刻只有一个维护者）

| 角色 | 负责 | 只写这些文件 |
| --- | --- | --- |
| **主智能体** | 共享接口与草稿状态归属、集成、最终验收、文档、交付 | `views/InteractiveView.vue`、`interactive/types.ts`、`services/interactive.ts`、`scripts/interactive-verify/fe-scenarios.mjs`、`docs/**`、`interactive/drafts.ts` 的**冻结签名骨架** |
| **A 会话草稿** | 问题 1、2 + 聊天说明精简与可读性 | `stores/session.ts`、`components/interactive/ChatDock.vue`、`components/Composer.vue`、`interactive/drafts.ts`、`interactive/chat.ts` |
| **B 卡片草稿** | 问题 3、4 + 关闭/刷新恢复边界 | `stores/interactive.ts`、`components/interactive/BoardCard.vue`、`components/interactive/CardDraftHint.vue` |
| **C 布局与手势** | 问题 5、6 + 提交区精简、令牌与硬编码色值 | `components/interactive/IntentBatchTray.vue`、`interactive/overlayLayout.ts`、`BoardToolbar.vue`、`BoardCanvas.vue`、`SubmitCluster.vue`、`IntentPreviewCard.vue`、`styles/interactive-shell.css` |
| **D 独立验收** | 按正确行为建反例、检查竞态与恢复、真机操作、视觉对照与报告 | 自己的验收脚本、独立用例、`docs/interactive-draft-races-verify.md`、证据目录 |

## 4. 冻结接口（主智能体先落盘，签名不改）

```ts
// interactive/drafts.ts（A 实现）
export interface DraftRecord { text: string; updatedAt: number; seq: number }
export function draftStorageKey(scope: "chat" | "card", id: string): string;
export function readDraft(key: string): DraftRecord | null;   // 不存在返回 null（正文为空也算存在）
export function hasDraftRecord(key: string): boolean;         // ★本轮新增：区分「没有草稿」与「存在但正文为空」
export function writeDraft(key: string, text: string, seq: number): { ok: boolean; error?: string };
export function removeDraft(key: string): void;
export function isStaleReceipt(receiptSeq: number, currentSeq: number): boolean;

// stores/session.ts（A 实现；ChatDock 与 Composer 共用同一份事实，不各写一套）
export interface FailedSend { topicId: string | null; text: string; draftSeq: number; at: number }
// state: failedSend: FailedSend | null
// action: retryFailedSend()  —— 只在原话题可用时把失败原文放回输入框，不自动发送
// action: discardFailedSend()
// action: sendAttribution() —— 点击发送那一刻确定的话题与草稿版本（请求必须用它）

// stores/interactive.ts（B 实现）
export function hasCardDraft(cardId: string): boolean;   // = hasDraftRecord(draftStorageKey("card", cardId))
export function cardDraftText(cardId: string): string;   // 存在则为草稿正文（可为空串）
// 关闭/刷新用的本地恢复副本：沿用同一套 helper，键用 draftStorageKey("card", "local-" + cardId)，
// 与服务端草稿（"card:" + cardId）分开；只用于编辑恢复，不提交、不发送。
```

## 5. 验收方法

1. **反例先行**：对 11 条交叉时序（提示词 §7 表格）各写「正确行为断言」的用例/探针，**旧基线（224bc63）必须失败**，
   修复后通过。禁止写「断言异常存在」的探针。
2. 慢请求与失败用**可控请求**（页面级拦截 + 明确的请求次数/版本/状态），等待真实保存结果，不用固定 sleep 掩盖。
3. 普通对话页与悬浮聊天**都**覆盖发送恢复；标注【实机】/【组件】/【纯函数】/【模拟失败】。
4. 回归：默认组名、未知批次不混合、四项批次处理到剩一项仍有入口。
5. 命令：`npx vue-tsc --noEmit`、`npx vitest run`、`cd backend; uv run --frozen pytest`、`python scripts/check_docs.py`。
6. 视觉：三档 × 暗/亮 + **480px 切换路径**（与桌面最小窗口 800×600 明确区分）；同尺寸同主题同数据的改前/改后对照，
   覆盖工具栏常态、聊天有消息且后方板面文字密集、聊天与批量列表同开、保存失败与恢复入口；
   测量工具栏高度、聊天可阅读区、浮层相交面积、连接点命中；**几何不相交不等于文字可读**，独立验收者要真的看图与滚动阅读。
7. 截图与原始结果随分支提交到 `docs/interactive-ui-screenshots/`，不留 `%TEMP%` 路径。
8. 关闭重开：若工具每次新建浏览器环境导致无法验证，**明确记未验证**，不把同次刷新当关闭重开。

## 6. 语言

主智能体与全部子智能体全程中文（方案、协调、进度、注释、新文档、提交说明、最终报告）；
标识符、路径、命令与第三方原始输出可保留原文。
