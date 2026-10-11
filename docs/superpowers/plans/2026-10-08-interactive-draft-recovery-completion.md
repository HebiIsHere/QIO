# 草稿恢复补齐与失败处理实施方案（2026-10-08 第四轮）

## 1. 实际基线

- 远端核对：`origin/fix/interactive-draft-races-ui-polish` = **`4b16595b849d2392feb0d6991eb8d11a29f5876d`**
  （`git ls-remote` 与 `git log 4b16595..origin/...` 均为空 —— **远端没有更新**，本文缺陷全部仍然存在）。
- 本轮分支：`fix/interactive-draft-recovery-completion`（从 `4b16595` 起），集成工作区 `D:\qio-dev\qio-recover`；
  四个子工作区 `qio-recover-a/b/c/d`（`wt/rec-a-card` / `wt/rec-b-send` / `wt/rec-c-layout` / `wt/rec-d-verify`）。
- 不合并 `main`、不合并另两条线、不改数据库结构与迁移、不接真实 QIO 能力。

## 2. 已核实的缺陷与源码位置

| # | 问题 | 现状位置 |
| --- | --- | --- |
| 1 | 首次输入在防抖前刷新恢复不到 | `stores/interactive.ts:281 refreshBoardFromServer` 只遍历服务器/内存里已有的草稿键（`300`），**本地独有记录发现不了** |
| 2 | 清除不同步、会复活 | `clearDraft(446)` 只删内存与本地记录，不登记待同步删除；`flushDrafts(497)` 依赖 `hasUnsavedDrafts`，清空后不发请求 |
| 3 | 本地写入失败被忽略 / 跨卡片时间误判 | `setDraft(461)` 忽略 `writeDraft` 的返回值；恢复时用**服务器整个草稿集合的 updatedAt** 与本卡片本地副本比较 |
| 4 | 普通对话没有失败原文入口 | `Composer.vue` 只在失败时调 `retryFailedSend()`，无恢复 UI；`failedSend` 只在内存，刷新即丢 |
| 5 | 未绑定话题迁移后清理找旧位置 | `stores/session.ts` 的 `settleSend`/`bind` 无稳定记录身份，迁移后按旧键清理 |
| 6 | 提交失败原因默认不可见 | `SubmitCluster.vue:110 failureText` 读 `lastSubmission`（上一次结果），`submitError`（本次原因）藏在详情 |
| 7 | 窄窗口切换条无真实布局 | `IntentBatchTray.vue:185 switchBarStyle` 只给 `bottom/height`，关闭一个面板后不预留高度；缺样式/焦点/命中 |

## 3. 冻结接口（契约 §11）

```ts
// interactive/drafts.ts（A 实现，主智能体先给签名）
export function cardDraftKey(cardId: string): string;        // "card:<id>"        与服务器同步的草稿
export function cardLocalDraftKey(cardId: string): string;   // "card-local:<id>"  本机恢复副本（尚未上传）
export function cardIdFromDraftKey(key: string): string | null;  // 两种键都要认
export function isCardDraftKey(key: string): boolean;
export function listLocalCardDraftIds(): string[];           // ★枚举本机独有记录（恢复要能发现它们）

// stores/interactive.ts（A 实现）
export function hasCardDraft(cardId: string): boolean;       // 服务器键或本机键任一存在（空串也算存在）
export function cardDraftText(cardId: string): string;       // 服务器键优先，其次本机键
export function clearDraft(key: string): void;               // 登记「待同步删除」，由 flushDrafts 真正发出
export function draftProtectionStatus(cardId: string): { local: "ok" | "failed"; server: DraftSaveState; error: string | null };

// stores/session.ts（B 实现）
export interface FailedSend { id: string; topicId: string | null; text: string; draftId: string; at: number }
// failedSend 必须可持久化（刷新/关闭重开后仍在），清理只依据「受理成功」或用户显式操作
// sendAttribution() 返回带稳定身份（draftId）的归属；settleSend 按身份定位记录，不按旧存储位置或文字相等
```

## 4. 分工与文件所有权（同一文件同一时刻只有一个写者）

| 角色 | 负责 | 只写这些文件 |
| --- | --- | --- |
| **主智能体** | 接口冻结、集成、最终验收、文档、交付 | `views/InteractiveView.vue`、`interactive/types.ts`、`services/interactive.ts`、`scripts/interactive-verify/fe-scenarios.mjs`、`docs/**` |
| **A 卡片草稿** | 本地记录发现、删除同步、恢复顺序、本地写入失败、跨卡片时间 | `stores/interactive.ts`、`interactive/drafts.ts`、`components/interactive/BoardCard.vue`、`CardDraftHint.vue`、`stores/__tests__/d5*.test.ts` |
| **B 文字发送恢复** | 两个入口的恢复 UI、失败原文持久化、未绑定话题迁移 | `stores/session.ts`、`components/Composer.vue`、`components/interactive/ChatDock.vue`、新建 `components/interactive/FailedSendNotice.vue`、相关用例 |
| **C 布局与审美** | 切换条真实布局、提交失败默认可见、提交区信息顺序、样式令牌 | `components/interactive/IntentBatchTray.vue`、`interactive/overlayLayout.ts`、`SubmitCluster.vue`、`interactive/submission.ts`、`styles/interactive-shell.css` |
| **D 独立验收** | 按用户行为写反例、基线验证、真实浏览器与证据 | 自己的用例/脚本/截图/`docs/interactive-draft-recovery-verify.md` |

`ChatDock.vue` 归 **B**（恢复 UI + 无效 HTML 结构 + 输入区可读性）；C 的视觉诉求通过变更请求交给 B。

## 5. 验收方法

1. D 先在实际基线 `4b16595` 上按**用户行为**验证缺陷（刷新后文字丢失、清除后复活、默认界面缺原因等），
   记录失败输出；修复后同用例必须通过。仅因「新函数不存在/选择器改名」而失败不算缺陷证据。
2. 分层证据：状态/单元 · 组件/DOM · 真实浏览器 · 真实请求 · 视觉，五层分别标注，不混称端到端。
3. 正常关闭重开必须用**同一持久化浏览器配置**与真实操作证据；同页面刷新不算关闭重开。
4. 命令：`npx vue-tsc --noEmit`、`npx vitest run`（全量）、`cd backend; uv run --frozen pytest`、
   `python scripts/check_docs.py`；修复后按**最终提交**重跑。
5. 视觉：三档尺寸 × 两主题 + 480px 边缘；聊天展开、长失败原文、长提交原因、批量列表、两者同时请求展开；
   输入框后有密集卡片文字与关系线；截图进仓库 `docs/interactive-ui-screenshots/`，报告用仓库相对路径引用。
6. 负载敏感用例（`eventBufferOverflow.verify.test.ts`）偶发超时：记录首次结果与复验，不反复跑到绿。

## 6. 语言

主智能体与全部子智能体全程中文（方案、协调、进度、注释、新文档、提交说明、最终报告）；
标识符、路径、命令与第三方原始输出保留原文。
