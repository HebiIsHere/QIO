# #46 返修（集成验收残留缺陷）· 上传回执必须与已加载的原板面匹配

- 任务：共享任务 `task-2`（#46 跨板回执只关联已加载的原板面）
- 分支 / HEAD：`fix/pm-qio032-integration` @ `551a2e99b90f111a3932a7ed3c0aa7874327cc2e`
- 执行者：frontend-46（子智能体）
- 日期：2026-11-14
- 结论：**已修复并本地定向验证通过**（新增 7 个用例全绿；受影响既有用例 44 个全绿；store / 互动组件 / 互动纯逻辑三组回归 278 文件 2705 用例全绿；`vue-tsc --noEmit` 0 错误）

---

## 1. 缺陷（已证实）

`frontend/src/stores/interactive.ts` 的 `selectBoard()` **先更新 `boardId` 再异步 `load()`**：

```ts
if (boardId.value !== id) boardId.value = id;
await load();          // 异步：A 的读取还没回来
```

切回 A 但 A 尚未读到时：`boardId` 已是 A，内存里的 `board` 仍是 B 的内容。
此时 A 的附件回执能通过 `applyImportAssociation()` 的**唯一**板面校验
（`boardId.value !== attempt.boardId`），把**含 B 卡片的对象**交给 `commit()` ——
于是 B 的卡片被写进 A 的板面，并被保存请求发到 A。

## 2. 修复（`frontend/src/stores/interactive.ts`）

### 2.1 板面「已加载」的真实身份（新增，约 :1165 / :2457）

| 记账 | 含义 |
| --- | --- |
| `boardContentBoardId` | 内存里的板面内容是在**哪块板面**下建立的（读取落地 / 保存回执 / 撤回恢复 / 候选合并 / 提交）。`selectBoard()` 只改 `boardId`，**不改它** |
| `boardContentIsPlaceholder` | 这块内容是不是「读取失败时补的空占位」（`load()` 的 catch 分支）。空占位的 boardId 就是当前板面 id，但它不是读到过的内容 |
| `boardReadsInFlight` | 正在读取的板面（按板面计数），由 `refreshBoardFromServer()` 的 `beginBoardRead` / `endBoardRead` 维护（含异常与「板面已切走」的提前返回） |
| `boardReadyWaiters` | 等原板面加载完成的回执（按板面分组）；读取落定 / 板面身份变化时唤醒并重新判定 |

`boardContentIsLoaded(targetBoardId)` 三个条件同时成立才算「已加载」：
1. 当前板面身份就是它（`boardId`）；
2. 内存里的内容是在这块板面下建立的（`boardContentBoardId`）；
3. 不是读取失败补的空占位。

第 2 条正是根治点：切回 A 但 A 还没读到时，`boardId` 已是 A、内容身份仍是 B → 判定「未加载」。

### 2.2 回执核对（`applyImportAssociation`，改为 async）

在原有「尝试 / 板面 / 原卡 / 原关联目标」核对之外，新增：

1. **原板面还在读 → 等它落定**（`waitForBoardReady`，15s 上限；读取落定 / 板面切走 / 超时都会重新判定）。
   已上传的附件与本次操作身份都保留：原板面可用后**继续关联**，不需要重新选文件或重新上传。
2. **等待期间有更新的尝试接手 → 沿用 `attemptValidity` 再核对一次**，旧回执按实际情况结束（`superseded`）。
3. **真正写入前再核对一次板面身份**（等待期间板面可能又被切走）。
4. 原板面读取失败留下空占位 / 没有在飞读取 → 如实结束这次回执（不写板面），文案说明「原板面还没有读到可用内容」。

两条路径都覆盖：**已有文件卡的关联**（`existingCard` 分支）与**无预建卡的批次建卡**（`createImportFileCard` 分支）——
两者都在 `applyImportAssociation` 的同一道门之后。

### 2.3 保持已通过的体验

- 上传中进入设置 / 返回互动页：执行器仍在 store，回执照常完成（既有 21 个 #46 用例全绿）；
- 批次上限、逐项重试、桌面原路径登记：未改动；
- 批次进度与单卡进度：未改动，完成后仍只结算一次（`finishImportAttempt` 未改）。

## 3. 新增测试

`frontend/src/stores/__tests__/pm032-import-board-identity.test.ts`（7 个用例，全部走 store 公开动作，不 mock store 内部）：

| # | 用例 | 断言要点 |
| --- | --- | --- |
| 1 | **A 上传 → 切到 B → 切回 A 且延迟 A 的读取** | A 加载前：保存请求数不变、保存请求里没有 `card_b`、板面仍是 B 的内容；A 加载后：`result.ok`、关联只发生一次（上传只调用 1 次）、保存请求的板面 ID 全是 A、卡片是 `card_a`、附件是 `att_a` |
| 2 | 原板面读取失败（空占位） | 回执不写板面，原因含「原板面」，保存请求里没有该卡 |
| 3 | 原卡已删除 | 迟到回执不重建卡、不写板面 |
| 4 | 原卡已换绑 | 迟到回执不覆盖新关联（仍是 `att_other`） |
| 5 | 旧回执晚到（同卡更新的尝试） | `supersededBy === "card"`，新关联保留 |
| 6 | 批次新建卡（无预建卡） | 跨板等待后只在原板面建**一张**卡、批次项 `succeeded`、保存请求全属于 A |
| 7 | 刷新 / 重读两块板 | B 内容完整（两张卡都在）、A 只有自己的卡、`board.boardId` 是 A |

### 3.1 反证（证明用例真的能抓到缺陷）

临时把新增的两道门禁改成 `if (false && …)` 后重跑：

```
Tests  3 failed | 4 passed (7)
- "A 加载后回执没有完成关联：回执到达时当前板面不是发起上传时的原板面（board_a）…"
- "原板面读取失败（空占位）时回执不写板面" → 实际落到「原文件卡已不存在」
- "批次新建卡" → 原板面建了 2 张卡（B 的 card_b + 新建卡）
```

恢复后 7/7 全绿（见 `46b-vitest-new.log`）。

## 4. 实际执行的命令与结果

工作目录：`D:\qio-dev\pm-qio032-integration\frontend`

### 4.1 新增用例

```
npx --no-install vitest run src/stores/__tests__/pm032-import-board-identity.test.ts
```

```
 ✓ src/stores/__tests__/pm032-import-board-identity.test.ts (7 tests) 655ms
     ✓ A 上传 → 切到 B → 切回 A 且延迟 A 的读取：A 加载前不写板面，加载后只关联一次且都属于 A  635ms
 Test Files  1 passed (1)
      Tests  7 passed (7)
```

日志：`46b-vitest-new.log`

### 4.2 受影响的既有用例（#46 执行器 / 附件提示）

```
npx --no-install vitest run src/components/interactive/__tests__/pm032-canvas-import-batch.test.ts \
  src/components/interactive/__tests__/fix025-attach-notice.test.ts \
  src/components/interactive/__tests__/fix025-card-attach-notice.test.ts
```

```
 ✓ src/components/interactive/__tests__/fix025-card-attach-notice.test.ts (10 tests) 413ms
 ✓ src/components/interactive/__tests__/pm032-canvas-import-batch.test.ts (21 tests) 6988ms
 Test Files  3 passed (3)
      Tests  44 passed (44)
```

日志：`46b-vitest-existing.log`

### 4.3 类型检查

```
npx --no-install vue-tsc --noEmit
```

```
tsc-exit=0        （无输出 = 0 错误）
```

日志：`46b-tsc.log`（0 字节）

### 4.4 回归（定向范围，非全量 npm test）

```
npx --no-install vitest run src/stores src/components/interactive src/interactive
```

```
 Test Files  278 passed (278)
      Tests  2705 passed (2705)
   Duration  153.90s
```

另外单独跑过板面读取相关的高风险文件（20 个文件 / 131 用例，全绿）：
`f08-stale-read-version`、`f10-submit-refresh`、`f1-cancel-recovery-independent-changes`、
`f3-cross-page-stale-reconfirm`、`draftPersistence`、`final-lead-m1`、`final-lead-m13`、
`p2-int-store`、`recovery-lead-state-facts`、`d5bCardsIsolation.verify`、`d6CardProtect`、
`irm-d-understanding-store`、`irm-d-execution-store`、`final-a3-01-late-read-draft`、
`final-a3-05-conflict-paths`、`closure-a-r4-reopen`、`p1-closed-card-shows-promoted`、
`recovery-a-integration-n6-store-input`、`IntentExecutionMessage`、`i2v2-a-intent-read-failure`。

## 5. 改动文件

| 文件 | 改动 |
| --- | --- |
| `frontend/src/stores/interactive.ts` | 新增板面内容身份记账（`boardContentBoardId` / `boardContentIsPlaceholder` / `boardReadsInFlight` / `boardReadyWaiters`）与 `boardContentIsLoaded` / `waitForBoardReady`；`refreshBoardFromServer` 维护读取在飞计数并在各落地分支标记内容身份；`load()` 失败分支标记空占位；`selectBoard()` 切板时清空内容身份并唤醒等待方；`applyImportAssociation` 改为 async 并新增「等原板面落定 + 写入前再核对」两道门禁；`executeImportAttempt` 改为 await |
| `frontend/src/stores/__tests__/pm032-import-board-identity.test.ts` | **新增**，7 个 #46 定向用例 |

未改动：`frontend/src/components/interactive/AddMenu.vue`（task-4）、`conversationPreview.ts` / `ConversationView.vue`（task-3）、后端、`.github/workflows`、`docs/status.md`、迁移文件。未 `git commit`。

## 6. 仍未验证的边界

1. **真实界面 / 真实后端**：本轮只做 store 层定向测试，没有起应用做截图验收（跨板切换 + 延迟读取的时序在真实网络下未实测）。
2. **15s 等待上限**：原板面读取超过 15s 未落定时回执按「未读到可用内容」结束（不写板面，可重试关联）。真实慢网络下的表现未实测。
3. **多标签页 / 多窗口**：另一个窗口把同一块板面改掉、本窗口回执等待期间板面被外部推进的场景，只覆盖了「读取落定后按服务器事实合并」这一条既有路径。
4. **`boardContentBoardId` 的赋值点**：读取落地、保存回执、撤回恢复、候选合并、提交这五处已覆盖；若将来新增第三条「整块替换 board.value」的路径，需要同步补上赋值（否则该路径下回执会被判「未加载」而拒绝，属安全侧失败）。
5. **批次项「未接收」上限 / 逐项重试 / 桌面原路径登记**：沿用既有行为，本轮未改动也未新增用例（既有 21 个 #46 用例覆盖）。
