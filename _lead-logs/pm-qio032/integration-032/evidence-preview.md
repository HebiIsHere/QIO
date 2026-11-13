# task-3 · 代码预览：设置返回后恢复可用（返修证据）

- worktree：`D:\qio-dev\pm-qio032-integration`（分支 `fix/pm-qio032-integration`，基线 HEAD `551a2e99`）
- 归属：frontend-preview（task-3）
- 未提交（Lead 统一按问题拆分提交）；未触发 CI、未跑全量 npm test、未 push。

## 1. 根因（已定位并复现）

`frontend/src/views/ConversationView.vue` 的 `onBeforeUnmount` 调 `preview.dispose()`：

```ts
dispose(): void {
  this._disposed = true;   // store 闸门
  disposed = true;         // 模块级闸门
  ...
}
```

`ensureLoaded()` / `loadAttachment()` / `checkForUpdate()` 都以 `this._disposed || disposed` 为闸门。
**重新挂载后没有任何地方复位这两个标志** —— 从设置页返回对话后：

1. 重新打开同一段代码 → `openTarget` → `ensureLoaded` 第一行 `if (this._disposed || disposed) return;` 直接返回；
2. 运行时里没有内容 → 阅读器 `v-if="!runtime || runtime.phase === 'loading'"` 命中 → 永远显示「正在读取这个文件…」；
3. 整页刷新时模块重新求值、`disposed` 回到 `false`，所以「刷新后恢复」。

附带发现（同一处生命周期）：`dispose()` 中止在途请求后，若响应晚一步落地，`disposed` 已被 `resume` 复位，
迟到响应会写进重新挂载后的运行时（旧内容覆盖新内容）。用运行时世代 `epoch` 挡住。

## 2. 改动文件（精确清单）

| 文件 | 改动 |
| --- | --- |
| `frontend/src/stores/conversationPreview.ts` | 新增 `resume()`（与 `dispose()` 成对）；`dispose()`/`resume()` 推进模块级 `epoch`；`loadAttachment` 记录发起世代并在落地时比对；`ensureLoaded` 把「停在 loading 但没有在途请求」的残影当未开始重发；新增 `reconcileReplyCodeIdentities()`（临时消息 id → 持久化消息 id 交接）；`resetForTests` 推进 epoch |
| `frontend/src/views/ConversationView.vue` | `onMounted` 调 `preview.resume()`；新增 `session.messages` id 变化 watch → `preview.reconcileReplyCodeIdentities()`，卸载时停表 |
| `frontend/src/components/conversation/__tests__/cv-c-preview-remount.test.ts` | **新增** 17 个用例（见 §3） |

未改：`frontend/src/stores/interactive.ts`（task-2）、设置页 / `AddMenu.vue` / `UpdateCard.vue`（task-4）、后端、`.github/workflows`、`docs/status.md`、迁移文件。

## 3. 新增测试（`cv-c-preview-remount.test.ts`，17 passed）

**A. 卸载 → 重新挂载后预览恢复可用（store 级）**
1. `打开 → dispose → resume → 再次打开同一代码块：运行时进入 ready，正文可读` —— 断言 `runtimeOf(tabId).phase === "ready"` 且 `documentOf(tabId).text` 含正文。
2. `重新挂载后阅读器真的渲染出正文（不是只有 store 状态对）` —— 挂真实 `PreviewPanel`，断言 `[data-pv-loading]` 不存在、`[data-line]` 有 2 行。
3. `dispose 的清理语义不退化：在途请求被中止，计时器与守卫都清掉` —— 卸载后迟到的成功响应不得把运行时写成 ready。
4. `卸载期间在途请求被中止；重新挂载后新请求能发出（不被闸门吞掉）` —— 断言 `fetchAttachmentPreview` 调用次数 1 → 2，且最终 ready。
5. `卸载前留下的 loading 残影不会让重新挂载后的读取永远停住`。
6. `resume 是幂等的：重复调用不会重复发请求，也不清掉已有内容`。

**B. 对话页真实挂载 / 卸载 / 重新挂载（设置页往返，`ConversationView` 级）**
7. `打开预览 → 卸载（进设置）→ 重新挂载（返回对话）→ 再次打开同一代码块：正文可读` —— 真实 `ConversationView` + 真实 `PreviewPanel`，断言 `[data-pv-loading]` 不存在、`[data-line]` 2 行、正文含 `alpha`。
8. `卸载时在途请求被中止；重新挂载后当前标签的新请求能发出`。
9. `预览栏收起时重新挂载不发起读取（没有阅读器在等）`。

**C. 临时消息身份 → 持久化消息身份交接**
10. `同一条消息换 id：标签跟着迁移，不重复开标签，正文与位置保留` —— `replycode:local_1_1:0` → `replycode:msg_abc:0`，`scrollTop`/`search`/`text`/`phase` 全保留。
11. `正文对不上就不猜：旧标签原样保留，仍然可读`。
12. `两段一模一样的代码（候选不唯一）不迁移，避免把标签挂到错的消息上`。
13. `持久化身份的标签已经存在时合并成它，不留下两份同一段代码`。
14. `消息 id 还在列表里时什么都不做（不误迁移）`。

**D. 失败时给可理解的原因与可用动作，不无限读取**
15. `附件读取失败：进入 problem 态并给出原因，不是一直 loading`（404 → `problem.kind === "missing"`、`retryable === false`）。
16. `重新挂载后失败态不会被当成「还没读」而无限重发` —— 请求次数保持 1，仍是 problem。
17. `用户点「重试」才重新读取，成功后进入 ready`。

## 4. 实际跑过的命令与结果

### 4.1 新增用例（先红后绿）

```
cd D:\qio-dev\pm-qio032-integration\frontend
npx --no-install vitest run src/components/conversation/__tests__/cv-c-preview-remount.test.ts
```

首次运行：`2 failed | 12 passed`，失败行：

```
AssertionError: expected undefined to be 'ready'
 ❯ cv-c-preview-remount.test.ts:183:45   （重新挂载后新请求能发出）
 ❯ cv-c-preview-remount.test.ts:342:45   （重试后进入 ready）
```

> **诚实说明（红→绿的口径）**：这两条失败是**用例自身**写错了假设 —— 读到真实版本后标签 id 会从
> `attachment:att1` 更新为 `attachment:att1@v1`，用例却用旧 id 查运行时；修正后转绿。
> 它们**不是**根因的红。
>
> 根因的红**没有**在改动前的代码上实跑（本 worktree 与其他三个子智能体共享，把
> `conversationPreview.ts` 临时回退会干扰 Lead 与其他人的并发验证）。改用**等价证明**：
> 写一个临时用例，走完 `openReplyCode → closeTab → dispose()` 后**不调用 `resume()`**
> （= 修复前的重新挂载），再 `openReplyCode` 同一代码块 —— 断言运行时**根本没建起来**。
>
> ```
> cd frontend
> npx --no-install vitest run src/components/conversation/__tests__/__tmp-red-proof.test.ts
> ✓ __tmp-red-proof.test.ts (1 test) 88ms
> ```
>
> 断言：`expect(preview.runtimeOf("replycode:m1:0")).toBeNull()` —— **通过**，即旧行为下
> `ensureLoaded` 被闸门吞掉、运行时为 `null`，阅读器 `v-if="!runtime || runtime.phase === 'loading'"`
> 命中 → 永远停在「正在读取这个文件…」。临时用例跑完已删除，日志留在
> `_lead-logs/pm-qio032/integration-032/preview-red-proof.log`。
>
> 静态佐证：`ensureLoaded()` 第一行就是 `if (this._disposed || disposed) return;`，而 `dispose()`
> 把两者置 true 后**修复前全仓库没有任何复位点**（`resetForTests` 注释明确写「仅供测试」）。
>
> 新增用例 1/2/7 断言的正是这条路径（`dispose()` → `resume()` → 重开 → `phase === "ready"` 且
> `[data-pv-loading]` 不存在）。

最终：

```
✓ src/components/conversation/__tests__/cv-c-preview-remount.test.ts (17 tests) 404ms
Test Files  1 passed (1)
     Tests  17 passed (17)
```

### 4.2 复跑既有预览 / 焦点 / 读屏 / #47 布局用例

```
cd D:\qio-dev\pm-qio032-integration\frontend
npx --no-install vitest run `
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
  src/views/__tests__/ConversationViewShell.test.ts `
  src/views/__tests__/ConversationViewLayers.test.ts `
  src/views/settings/__tests__/settings-reset-layout.test.ts
```

```
 Test Files  14 passed (14)
      Tests  110 passed (110)
   Duration  13.44s
```

基线对照（改动前同一批 10 个预览文件）：`Test Files 10 passed (10) / Tests 62 passed (62)` —— 无回归。

### 4.2b 相邻接线用例（预览桥 / 消息区预览反馈 / 0.3.2 外壳验收）

```
npx --no-install vitest run src/components/__tests__/MessageItemPreviewFeedback.test.ts `
  src/__tests__/pmqio032-shell-acceptance.test.ts `
  src/components/conversation/__tests__/bridges.test.ts
```

```
 Test Files  3 passed (3)
      Tests  21 passed (21)
```

### 4.2c 整目录回归（对话组件 + 对话逻辑 + 视图层）

```
npx --no-install vitest run src/components/conversation/__tests__ src/conversation/__tests__
→ Test Files  22 passed (22) / Tests  206 passed (206)

npx --no-install vitest run src/views/__tests__
→ Test Files  31 passed (31) / Tests  237 passed (237)
```

（这两个目录是本次改动会波及的全部范围；仍不是全量 `npm test`。）

### 4.3 类型检查（只对改动范围，一次）

```
cd D:\qio-dev\pm-qio032-integration\frontend
npx --no-install vue-tsc --noEmit
```

```
TSC_EXIT=0
```

日志：`preview-vitest.log`（110 passed）、`preview-tsc.log`（空 = 无错误）、`preview-red-proof.log`（旧行为证明）。

## 5. 逐条对照任务要求

| 要求 | 实现 | 证据 |
| --- | --- | --- |
| 1. 重新挂载后能接收新的加载请求；旧请求与计时器仍能正常清理 | `resume()` 复位两个闸门并推进 `epoch`；`dispose()` 的中止/清计时器/清守卫逻辑一字未改，只多推进 `epoch` | 用例 1/3/4/5/6 |
| 2. 回复代码已有正文时，重开直接进入可读状态 | `ensureLoaded` → `prepareContentRuntime` 走 `documents` 正文（原有路径，闸门放开后即生效） | 用例 1/2/7 |
| 3. 临时消息身份与持久化消息身份交接，避免重复开标签 | `reconcileReplyCodeIdentities()` + ConversationView 的 `session.messages` id watch；只在「旧 id 已不在列表 + 正文逐字相同 + 候选唯一」时迁移 | 用例 10–14 |
| 4. 失败时显示可理解的原因及可用动作，避免无限读取 | 失败态是真实结论，重新挂载不自动重发；`problem` 带 title/detail/retryable，用户点「重试」才重读 | 用例 15/16/17 |

## 6. 仍未验证的边界（如实列出）

1. **真机窗口往返未跑**：本项只做 jsdom 级定向验证，没有起真实应用做「对话 → 设置 → 返回 → 重开预览」的截图验收（Lead 的 e2e 脚本 `e2e-integration-032/ui-verify.mjs` 可覆盖）。
2. **临时 id → 持久化 id 的真实时序未在真机观察**：`reconcileReplyCodeIdentities` 的触发依赖 `session.messages` 的 id 变化；真实后端在 `loadHistory` 快照接手时的 id 替换时序未实测，只按 store 语义与单测覆盖。若真实链路里旧消息是「就地改 id」而不是「整条替换」，watch 仍会触发（键是 id 串），但**同一时刻两条消息正文相同**的极端场景会按「候选不唯一」不迁移（保守，不误挂）。
3. **`resume()` 只在 `ConversationView` 挂载时调用**：如果将来有别的页面也 `dispose()` 这个 store（目前没有），需要同样配对调用 `resume()`。
4. **PDF / 图片 / Office 的重新挂载路径**：用例覆盖了附件（image envelope）与回复代码；PDF 的 pdf.js 文档对象在卸载时由 `PreviewPdfViewer` 自己 destroy，重新挂载会重新 `fetchAttachmentBytes`，本次未单独加用例（既有 `cv-c-preview-resources.test.ts` 覆盖 destroy 语义）。
5. **窄窗自动全窗口 + 设置往返**：`resume()` 里 `if (!this.isOpen) return` 用的是 `shell.rightVisible`（用户偏好 + 有标签），窄窗临时全窗口时该值仍为 true，行为一致；但「窄窗下从设置返回」没有单独用例。
6. **未跑全量 `npm test`**（纪律要求）：只跑显式文件 + `src/components/conversation/__tests__`、`src/conversation/__tests__`、`src/views/__tests__` 三个目录。
7. **`session.messages` id watch 的开销**：键是全部消息 id 拼接串，每来一条消息/一次历史快照都会重算一次（O(n) 字符串拼接）。当前消息量级下可忽略；若将来单话题消息数上千，可改成只比较「回复代码标签涉及的那几个 id」。
