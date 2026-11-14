# task-6 · 预览b：只在确认同一回复时迁移标签（返修证据）

- worktree：`D:\qio-dev\pm-qio032-integration`（分支 `fix/pm-qio032-integration`，基线 HEAD `acdb671c`）
- 归属：frontend-preview2（task-6）
- 未提交（Lead 统一按问题拆分提交）；未触发 CI、未跑全量 npm test、未 push。

## 1. 根因（已复现）

`frontend/src/stores/conversationPreview.ts` 的 `reconcileReplyCodeIdentities()` 旧实现：

```ts
const live = new Set(messages.map((m) => String(m.id || "")));
...
if (!messageId || live.has(messageId)) continue;      // 只判断「在不在列表里」
const matches = candidates.filter(
  (m) => codeBlockText(String(m.content || ""), blockIndex) === text,   // 只按正文找候选
);
if (matches.length !== 1) continue;
```

两个缺陷：

1. **已绑定正式 `msg_` id 的标签也会被改绑**。原消息只是暂时不在当前列表里（切换话题、
   历史分页、列表刷新），只要当前另一条回复有唯一一段相同代码，标签就被改绑到那条回复上
   —— 这就是用户报的「代码预览标签绑错回复」。
2. **临时标签按正文猜归属**。不同回复完全可能包含同一段代码，正文相同不能证明是同一条回复；
   旧实现没有轮次身份，也没有「同一轮可能有多条助手消息」的概念。

## 2. 修复（`frontend/src/stores/conversationPreview.ts`）

### 2.1 归属规则

| 标签当前身份 | 行为 |
| --- | --- |
| 正式消息 id（`msg_…`） | **永不迁移**，继续指向原消息（原消息暂时离开列表不是改绑理由） |
| 临时消息 id（`local_<ts>_<seq>`） | 只有能确认「同一条回复」时才交接：轮次身份（turnId）相同 + 该轮里恰好一条助手消息在同一 blockIndex 的代码块正文逐字相同 |
| 身份确认不了（无轮次身份 / 候选不唯一 / 正文对不上） | 原标签原样保留：正文还在内存里，仍然可读，**不迁移也不关闭** |

代码正文、语言与块序号只用于**校验**，不单独确定消息归属。

### 2.2 轮次身份从哪来

新增模块级观察表 `observedTurnByMessage`（有界 400 条，按插入顺序淘汰）：

- `reconcileReplyCodeIdentities()` 每次被调用时，先把列表里助手消息的 `turnId` 记下来；
- 临时消息消失之后，仍然知道它属于哪一轮（这是交接能成立的前提）；
- **从未观察到**就不猜（保守，不误挂）；
- `resetForTests()` 清空该表，用例之间隔离。

### 2.3 合并时保住用户刚操作的位置

新增 `mergeIntoExistingTab(oldId, keepId)`：正式标签已经打开时合并成一个。

- 当前激活的是**临时**标签 → 把它的滚动/搜索词交给留下的正式标签（用户刚操作的位置保住）；
- 当前激活的是**正式**标签 → 正式标签自己的位置不被旧临时位置覆盖；
- 合并后激活项落在留下的标签上（不会因为关掉临时标签跳到别的文件）；
- 正文只在内存里：正式标签若是刷新后从布局恢复的（自己没有正文），把临时标签记住的正文
  交给它并重新准备运行时 —— 否则合并完会变成「内容不在当前页面」。

### 2.4 未改动（按要求保留）

- `resume()` / `dispose()` / `epoch` 重新挂载逻辑**一行未动**（本轮已通过的返修）；
- 未碰 `stores/interactive.ts`、`stores/session.ts`、设置页、UpdateCard、后端、`.github/workflows`、
  `docs/status.md`、迁移文件。

## 3. 改动文件

| 文件 | 改动 |
| --- | --- |
| `frontend/src/stores/conversationPreview.ts` | 重写 `reconcileReplyCodeIdentities()`（正式 id 不迁移 + 轮次身份确认）；新增 `mergeIntoExistingTab()`；新增模块级 `observedTurnByMessage` 观察表与 `isTemporaryMessageId()`；`resetForTests()` 清空观察表 |
| `frontend/src/components/conversation/__tests__/cv-c-preview-identity.test.ts` | **新增** 12 个用例（两组验收） |
| `frontend/src/components/conversation/__tests__/cv-c-preview-remount.test.ts` | 交接那 5 个用例补上轮次身份（旧用例编码的是「只按正文匹配」的旧契约，与新要求冲突） |

## 4. 新增 / 修改的测试

### 4.1 `cv-c-preview-identity.test.ts`（新增，12 passed）

**第一组：不同回复 / 话题包含同一段代码，原标签不被改绑**
1. `已绑定正式消息 id 的标签：原消息离开列表后仍指向原消息，正文与阅读位置不变` —— 断言 `moved === 0`、标签 id 不变、`scrollTop/search` 不变、正文与运行时仍 ready。
2. `正式 id 的标签：同一段代码在另一条回复里出现多次也不改绑`。
3. `临时标签：从未观察到这条消息的轮次身份时不猜（不迁移、不关闭，正文仍可读）`。
4. `临时标签：候选属于另一轮（轮次身份对不上）时不迁移`。
5. `临时标签：同一轮里两条助手消息都有同一段代码（候选不唯一）时不迁移`。
6. `临时标签：正文对不上就不猜（旧标签原样保留，仍然可读）`。
7. `消息 id 还在列表里时什么都不做（不误迁移）`。

**第二组：同一回复从临时 id 转为正式 id**
8. `标签跟着迁移，正文、搜索与滚动保留，只留一个标签` —— `moved === 1`、id 变 `replycode:msg_abc:0`、`scrollTop 42 / search print` 保留、旧运行时释放。
9. `迁移后重开同一段代码只保留一个标签，正文与搜索可用`。
10. `正式标签已经打开时合并成一个，保住用户刚操作的那个标签的位置` —— 用户正在读临时标签 → 它的 `scrollTop 88 / search print` 跟到留下的正式标签。
11. `正式标签是当前标签时合并：保留正式标签自己的位置，不被旧临时位置覆盖` —— `scrollTop 5` 不被 88 覆盖。
12. `正式标签没有正文（刷新后从布局恢复）时合并：正文交给留下的标签，仍然可读` —— 合并前是 `problem`，合并后 `ready` 且正文可读。

### 4.2 `cv-c-preview-remount.test.ts`（修改 5 个交接用例）

新增 `observeTemporary()` 辅助：先让临时消息在列表里被观察到（记下 turnId），再断言交接。
用例语义不变（迁移 / 不猜 / 候选不唯一 / 合并 / 不误迁移），只是补上「轮次身份」这个前提 ——
旧写法（只给正文、不给 turnId）在新契约下**必须不迁移**，这正是本次修复的核心。

## 5. 实际跑过的命令与结果

### 5.1 先红（修复前，反例确认失败原因）

```
cd D:\qio-dev\pm-qio032-integration\frontend
npx --no-install vitest run src/components/conversation/__tests__/cv-c-preview-identity.test.ts
```

```
 ❯ cv-c-preview-identity.test.ts (11 tests | 5 failed)
   × 已绑定正式消息 id 的标签：原消息离开列表后仍指向原消息，正文与阅读位置不变
     AssertionError: 正式 id 的标签永远不迁移: expected 1 to be +0
   × 临时标签：从未观察到这条消息的轮次身份时不猜（不迁移、不关闭，正文仍可读）
     AssertionError: 确认不了身份就不迁移: expected 1 to be +0
   × 临时标签：候选属于另一轮（轮次身份对不上）时不迁移
   × 正式标签已经打开时合并成一个，保住用户刚操作的那个标签的位置
   × 正式标签是当前标签时合并：保留正式标签自己的位置，不被旧临时位置覆盖
 Test Files  1 failed (1)
      Tests  5 failed | 6 passed (11)
```

第 1 条失败就是用户报的缺陷本身：**正式 id 的标签被改绑了**（`moved` 返回 1）。
日志：`preview2-red.log`。

### 5.2 后绿（修复后）

```
npx --no-install vitest run src/components/conversation/__tests__/cv-c-preview-identity.test.ts
→ Test Files 1 passed (1) / Tests 12 passed (12)
```

### 5.3 定向回归（19 个显式文件）

```
npx --no-install vitest run `
  src/components/conversation/__tests__/cv-c-preview-identity.test.ts `
  src/components/conversation/__tests__/cv-c-preview-remount.test.ts `
  src/components/conversation/__tests__/cv-c-preview-50-53-state.test.ts `
  src/components/conversation/__tests__/cv-c-preview-panel.test.ts `
  src/components/conversation/__tests__/cv-c-preview-reader.test.ts `
  src/components/conversation/__tests__/cv-c-preview-resources.test.ts `
  src/components/conversation/__tests__/cv-c-preview-feedback.test.ts `
  src/components/conversation/__tests__/d-preview-52-tab-focus.test.ts `
  src/components/conversation/__tests__/d-preview-esc-layers.test.ts `
  src/components/conversation/__tests__/d-preview-pdf-search.test.ts `
  src/components/conversation/__tests__/d-preview-tabs-keyboard.test.ts `
  src/conversation/__tests__/cv-c-preview-store.test.ts `
  src/conversation/__tests__/cv-c-preview-logic.test.ts `
  src/views/__tests__/ConversationViewShell.test.ts `
  src/views/__tests__/ConversationViewLayers.test.ts `
  src/views/settings/__tests__/settings-reset-layout.test.ts `
  src/components/__tests__/MessageItemPreviewFeedback.test.ts `
  src/__tests__/pmqio032-shell-acceptance.test.ts `
  src/components/conversation/__tests__/bridges.test.ts
```

```
 Test Files  19 passed (19)
      Tests  169 passed (169)
   Duration  18.35s
```

日志：`preview2-green.log`。

### 5.4 整目录回归（改动会波及的全部范围）

```
npx --no-install vitest run src/components/conversation/__tests__ src/conversation/__tests__
→ Test Files 23 passed (23) / Tests 218 passed (218)

npx --no-install vitest run src/views/__tests__
→ Test Files 31 passed (31) / Tests 237 passed (237)   （连跑 3 次均全绿）
```

### 5.5 类型检查

```
cd frontend; npx --no-install vue-tsc --noEmit
```

我的改动范围（`stores/conversationPreview.ts`、`components/conversation/**`、`conversation/preview*.ts`）：**0 error**。
仓库里另有 5 条 error，全部在他人写入范围（`stores/interactive.ts`、`stores/__tests__/pm032-board-save-isolation.test.ts`），
改动前即存在，与本次无关。

## 6. 逐条对照任务要求

| 要求 | 实现 | 证据 |
| --- | --- | --- |
| 1. 已绑定正式消息 ID 的标签继续指向原消息，不迁移 | `if (!isTemporaryMessageId(messageId)) continue;` | 用例 1/2 |
| 2. 优先复用稳定轮次身份；一轮多条助手消息要明确是哪条 | `observedTurnByMessage` 观察 turnId；候选必须同 turnId 且唯一 | 用例 3/4/5/8 |
| 3. 身份无法确认时原标签继续用已有正文与位置（不迁移、不关闭） | 无轮次身份 / 候选不唯一 / 正文不符 → `continue` | 用例 3/5/6 |
| 4. 确认后迁移标签、激活状态、搜索词与滚动位置；正式标签已打开时留一个并保住用户刚操作的位置 | `applyTabIdentity`（迁移运行时/正文/守卫/激活项）+ `mergeIntoExistingTab`（按激活项决定位置归属） | 用例 8/9/10/11/12 |
| 5. 保留重新挂载行为（resume/epoch 不动） | `resume()`/`dispose()`/`epoch` 一行未改 | `cv-c-preview-remount.test.ts` 17 passed |

## 7. 仍未验证的边界（如实列出）

1. **真机时序未观察**：`turnId` 在真实链路里的可得性只按 store 语义与单测覆盖。前端 `pushMessage` 会给
   实时消息写 `turnId: this.activeTurnId`，历史消息由 `_historyMessage` 从 `turn_id` 写入；但
   「临时消息出现时 `activeTurnId` 是否一定已就绪」没有在真机验证 —— 若某条临时消息出现时
   `activeTurnId` 还是 null，它的轮次身份观察不到，交接会**保守地不发生**（标签保留、仍可读，
   不会误挂），代价是同一段代码可能留下两个标签（旧临时 + 新正式）。这是有意的保守取舍。
2. **同一轮多条助手消息**：候选不唯一时不迁移（用例 5）。真实链路里若一轮确实有多条助手消息且
   都含同一段代码，交接不会发生 —— 同样保守，不误挂。
3. **观察表容量**：有界 400 条，超出按插入顺序淘汰。极端长会话里，若临时标签在 400 条消息之后
   才交接，观察记录可能已被淘汰 → 不迁移（保守）。当前量级下不会触发。
4. **未跑全量 `npm test`**（纪律要求）：只跑显式文件 + 三个目录。
5. **`src/views/__tests__` 的既有抖动**：整目录首跑出现过 1~2 个失败（`i2v2-a-intent-read-failure` /
   `ConversationViewNotices` / `i2v2-a-recovery-entry`），单独跑全通过、随后连跑 3 次整目录全绿。
   这些用例不 import 预览 store（grep 确认），失败点在意图/恢复入口的时序断言，属既有并发抖动，
   不是本次改动引入；未修改它们。
6. **真机窗口往返未跑**：本项只做 jsdom 级定向验证，没有起真实应用做「切换话题 → 原消息离开列表 →
   点另一条回复的同一段代码」的截图验收（Lead 的 e2e 脚本可覆盖）。
