# R7 store 回归测试（任务隔离、附件状态机、批量导入）— 基线红证据

- 任务：task-4（spec-attach）
- 工作树：`D:\qio-dev\pm-qio032-integration`，分支 `fix/qio032-46-board-state`
- 受测基线 HEAD：`0dbc387f1ecdc2dfa21f658ce16dbe07a6fa9b3d`（与 origin 同 SHA）
- 基线运行方式：**独立干净 worktree** `D:\qio-dev\_r7-spec-attach-baseline`（`git worktree add --detach 0dbc387f`，node_modules 用 junction 复用），
  只把两个新测试文件拷进去 —— 因为共享工作树里 `frontend/src/stores/interactive.ts` 正被其他成员并发修改（`git diff --stat` 显示 +389/−69），
  直接在主工作树跑会把「别人的在途修复」当成基线。**产品代码零改动**（worktree 内 `git status` 只有两个新测试文件）。
- 命令：`cd frontend; npx vitest run src/stores/__tests__/q46-r7-task-isolation.test.ts src/stores/__tests__/q46-r7-attachment-state.test.ts`
- 结果：**2 文件 9 例 → 6 failed / 3 passed**
- 原始输出：`_lead-logs/qio032-46/r7-specs/red-baseline-0dbc387f.log`

## 一、逐条红证据（任务书编号 → 失败断言）

### 三.7 任务隔离（`q46-r7-task-isolation.test.ts`）

| # | 用例 | 结果 | 失败断言（原文） |
|---|---|---|---|
| 三.7-a | B 有运行中任务、A 首次读取失败：A 的 intents / intentViews 不使用 B 的清单 | **FAIL** | `AssertionError: A 显示了 B 的任务清单（任务清单没有按板面归属）: expected [ 'task_b_running' ] to deeply equal []` |
| 三.7-b | 同上：A 的普通保存不发影响预判，PUT 只指向 A 且成功 | **FAIL** | `AssertionError: A 的普通保存因为 B 的运行中任务发了影响预判：[{"boardId":"board_a","stateVersion":3,"cardIds":["c_mwhdoa841p0uo"]}]: expected 1 to be +0` |
| 三.7-c | A 自己真的有运行中任务时，影响预判照常发往 A（隔离 ≠ 关掉预判） | PASS | — |
| 三.7-d | 同板刷新失败保留同板旧数据（F11 口径不被改坏） | PASS | — |
| 三.7-e | 晚到的整板读取与任务清单不覆盖当前板数据与保存错误 | PASS | — |

### 四.8 单卡附件状态机（`q46-r7-attachment-state.test.ts`）

| # | 用例 | 结果 | 失败断言（原文） |
|---|---|---|---|
| 四.8-a | 上传成功但关联保存失败：状态区分待保存/失败/成功，失败保留附件 ID 且不发成功提示 | **FAIL** | `AssertionError: 保存回执还没到就发了成功提示：["processing","succeeded"]: expected true to be false` |
| 四.8-b | 重试关联：只上传 1 次、复用同一 attachmentId，保存回执成功后才变成成功 | **FAIL** | `AssertionError: 保存回执还没到就发了成功提示: expected true to be false` |

### 四.9 批量导入（`q46-r7-attachment-state.test.ts`）

| # | 用例 | 结果 | 失败断言（原文） |
|---|---|---|---|
| 四.9-a | 关联已写入但保存回执未到：批次项不得标成功；保存失败后仍不得成功 | **FAIL** | `AssertionError: 保存回执还没到就把批次项标成了「已接收」: expected 'succeeded' not to be 'succeeded'` |
| 四.9-b | 重试复用附件 ID、不重复建卡、同批其他项不变；成功回执后对应项才成功 | **FAIL** | `AssertionError: 写入失败的第 1 项被标成了成功: expected 'succeeded' not to be 'succeeded'` |

## 二、产品缺口语义（不弱化断言，如实记录）

### 缺口 1（三.7）：任务清单没有按板面归属

`loadIntents()`（interactive.ts:3787）与 `loadIntentViews()`（:3830）的失败分支按 F11 保留「最后一次成功读到的清单」，
但**没有记录这份清单属于哪块板面**。`resetSubmissionStatusForBoard()`（:6256）在切板时清了 `intentViews`，
却**没有清 `intents` / `conflicts` / `batchAvailable` / `recoverNotice` / `intentsLoadState`**。

后果链：
1. B 载入运行中任务 → 切 A → A 的 `fetchIntents` 失败 → `intents` 仍是 B 的清单（`intentsLoadState="error"` 但内容串板）；
2. `saveNow()` 的影响预判门（:2405）判据是 `activeIntents.value.length > 0` —— 全局值，于是 **A 的普通编辑因为 B 的任务发了预判**；
3. 预判请求目标板面是 A（`runImpactCheck(ownerBoardId, ...)`），服务端一旦按这份错的清单返回受影响任务，A 的普通保存就被 B 的任务拦住。

**需要的语义**：`intents` 一族状态必须与 `intentViews` 同口径按板面归属 —— 切板时清空并进入新板面自己的加载/失败状态；
影响预判门只能看**当前板面**的运行中任务。

### 缺口 2（四.8）：单卡关联的「待保存」判定依赖全局 saveStatus，重试时直接标成功

`finishImportAttempt()`（:7423）的 `awaitingSave` 判据是
`result.ok && boardId===attempt.boardId && dirty && saveStatus!=="saved" && saveStatus!=="error"`。
`saveStatus === "error"` 时 `awaitingSave=false` → `phase="succeeded"` → **发成功提示**，
而这次关联的保存根本没成功（`settleCardImportSaveForBoard` 只在保存回执路径上被调用，此时还没到）。
重试关联（`retryCardAttachment` → `startCardAttachment`）在 `saveStatus` 仍为 error 时同样直接落成 succeeded。

**需要的语义**：关联写入板面 ≠ 保存成功。只要这次候选还没拿到保存回执，就必须是「关联待保存」；
保存失败后必须是「关联保存失败」（保留附件 ID + 可重试入口），**任何情况下都不许发成功提示**。

### 缺口 3（四.9）：批次项完全不看保存回执

`finishImportAttempt()` 的批次分支（:7425-7444）只看 `result.ok`（= 关联是否写入板面），
**完全不看这次候选的保存回执**。写入（PUT）失败时批次项照样 `noteImportItem(status:"succeeded")`，
`currentImportBatchCounts.received` 也把它算成已接收 —— 界面显示「已接收」，而板面并没有保存成功。

**需要的语义**：批次项的成功必须等保存回执；写入失败时该项不得标成功（保留附件 ID 供重试复用），
同批其他项不受影响，成功回执后对应项才成功。

## 三、测试设计说明（为什么这些断言不是「测内部实现」）

- 全部走 store 的**公开动作**：`selectBoard` / `commit` / `startCardAttachment` / `retryCardAttachment` /
  `beginImportBatch` / `setImportItemSource` / `runImportItem` / `onImportJob`；
- 断言落在**真实 API 调用参数**（`checkMaterialImpact(boardId, stateVersion, candidate)`、
  `saveBoardState(boardId, candidate)`、`uploadAttachment` 调用次数）与**store 对外可见状态**
  （`intents` / `intentViews` / `activeIntents` / `intentsLoadState` / `saveStatus` / `saveError` /
  `pendingImpact` / `cardImportJobs` / `activeCardImportForBoard` / `unacknowledgedCardImportForBoard` /
  `importBatchById().items` / `currentImportBatchCounts`）；
- mock 只打在**服务边界**（`services/interactive`、`services/attachments`），不 mock store 逻辑；
- 保存回执用可控开关（ok / fail / hold）制造「关联已写入、保存回执未到」的真实窗口，
  并在放行前断言 `saveStatus === "saving"` 作为前提，避免「请求还没发出就断言」的假红。

## 四、诚实边界

- 本轮只做 store 层定向验证，**未跑全量测试、未触发 CI、未 push**；
- 未做真实浏览器 / 真实后端验证（那属于 task-1 harness 的范围）；
- 界面渲染断言（AddMenu.vue 的提示文案）本轮**未覆盖**：缺口 2 的「不发成功提示」是在 store 的
  `onImportJob` 事件与 `cardImportJobs` 快照上断言的（AddMenu 的 `onImportJobEvent` 直接消费这两者，
  `result.ok && result.attachment` 即 `succeed()`），因此 store 层红即等价于界面会显示成功提示；
  但「按钮确实可点」这类 UI 断言留给 task-1 的浏览器用例。
- 基线 worktree `D:\qio-dev\_r7-spec-attach-baseline` 保留，供修复后复跑对照；用完可 `git worktree remove`。
## 五、修复后复跑（在途修复代码，非本轮交付基线）

Lead 通知前，共享工作树里 `frontend/src/stores/interactive.ts` 已被其他成员改到 +560/−92（含本轮缺口 1/2/3 的修复）。
在该在途代码上复跑同一份用例：

- 命令：`cd frontend; npx vitest run src/stores/__tests__/q46-r7-task-isolation.test.ts src/stores/__tests__/q46-r7-attachment-state.test.ts`
- 结果：**2 文件 9 例全绿（9 passed）**
- 原始输出：`_lead-logs/qio032-46/r7-specs/green-inflight-fixed.log`

对照结论（同一份用例、同一台机器、只差产品代码）：

| 用例 | 基线 0dbc387f（干净 worktree） | 在途修复代码（共享工作树） |
|---|---|---|
| 三.7-a A 的 intents/intentViews 不使用 B 的清单 | FAIL | PASS |
| 三.7-b A 的普通保存不发影响预判、PUT 只指向 A | FAIL | PASS |
| 三.7-c A 有真实任务时预判照常发往 A | PASS | PASS |
| 三.7-d 同板刷新失败保留同板旧数据 | PASS | PASS |
| 三.7-e 晚到读取不覆盖当前板数据与保存错误 | PASS | PASS |
| 四.8-a 待保存/失败/成功三态 + 失败保留附件 ID + 不发成功提示 | FAIL | PASS |
| 四.8-b 重试只上传 1 次、复用 attachmentId、回执后才成功 | FAIL | PASS |
| 四.9-a 批次项保存回执未到不得标成功 | FAIL | PASS |
| 四.9-b 重试复用附件 ID、不重复建卡、同批其他项不变 | FAIL | PASS |

**注意**：上表右列是**在途代码**（未提交、仍在变动），只作为「这份用例确实能区分修复前后」的对照证据，
不作为本轮最终验收结论；最终绿证据应由 Lead 在修复定稿后复跑。

## 六、类型检查

- 干净基线 worktree（`_r7-spec-attach-baseline`，含两个新测试文件）：`npx vue-tsc --noEmit` → **exit 0**（两个测试文件本身类型干净）。
- 共享工作树当前 `npx vue-tsc --noEmit` 有 1 条错误，**与本轮测试文件无关**，来自其他成员在途的产品改动：
  `src/components/interactive/AddMenu.vue(269,7): error TS2741: Property 'awaiting_save' is missing in type ... but required in type 'Record<ImportItemStatus, string>'`
  —— 在途代码给 `ImportItemStatus` 新增了 `awaiting_save`，但 AddMenu 的 `IMPORT_STATUS_TEXT` 还没补这一项。**已上报 Lead**。

## 七、给 Lead 的提醒（跨成员风险）

1. **共享工作树并发写**：本轮开工时 `interactive.ts` 干净，中途被其他成员改成 +560/−92。基线红证据因此改用独立 worktree
   `D:\qio-dev\_r7-spec-attach-baseline`（`git worktree add --detach 0dbc387f`，node_modules 用 junction 复用，产品代码零改动）。
   该 worktree 保留，可随时复跑；用完 `git worktree remove D:\qio-dev\_r7-spec-attach-baseline --force`。
2. **在途代码的 `awaiting_save` 状态**：新增的 `ImportItemStatus` 成员会让 `AddMenu.vue` 的
   `Record<ImportItemStatus, string>` 类型检查失败（`vue-tsc` exit 1）。修复者需要同步补 `IMPORT_STATUS_TEXT.awaiting_save`，
   否则本轮「类型检查 exit 0」的验收口径过不去。
3. **批次项新增的 `awaiting_save` 状态**：我的用例断言的是 `status !== "succeeded"`（不绑定具体新状态名），
   所以无论修复者用 `awaiting_save` 还是别的名字，用例都成立 —— 这是有意为之，避免测试依赖新增内部字段命名。
