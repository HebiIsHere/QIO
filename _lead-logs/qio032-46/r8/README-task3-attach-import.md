# R8 store 定向测试（替换附件重试 / 旧保存结算 / 批次归板 / 撤销等待）— task-3 报告

- 任务：task-3「R8 红用例：附件替换/旧保存结算/批次归板/撤销等待（第9-12项）」
- 负责人：t-attach（并行团队，Lead = lead）
- 工作树：`D:\qio-dev\q46-r8`，detached HEAD `c9fedc60744911c088ebc082faceef266ca3048b`（与 origin 同 SHA）
- 红基线 worktree：`D:\qio-dev\_q46-r8-baseline`（同 SHA；产品代码零改动，只有各成员拷入的新测试文件）
- 依赖：两个工作树的 `frontend/node_modules`、`backend/.venv` 均为指向 `pm-qio032-integration` 的 junction，未重装
- 交付文件（只新增测试，**未改产品代码**）：
  - `frontend/src/stores/__tests__/q46-r8-attachment-replace.test.ts`（5 例：第 9 项 3 例、第 10 项 2 例）
  - `frontend/src/stores/__tests__/q46-r8-import-board.test.ts`（4 例：第 11 项 2 例、第 12 项 2 例）
- 命令（基线 / 产品工作树同一条）：
  `cd frontend; npx vitest run src/stores/__tests__/q46-r8-attachment-replace.test.ts src/stores/__tests__/q46-r8-import-board.test.ts`

## 一、结果对照（基线 c9fedc60，产品代码零改动）

| 运行对象 | 文件 | 用例 | 结果 | 原始输出 |
|---|---|---|---|---|
| **基线 c9fedc60**（干净 worktree，产品代码零改动） | 2 | 9 | **9 failed / 0 passed**（全部为断言失败，无测试自身报错） | `t3-attach-red.log` |
| 基线：第 9、10 项单独跑 | 1 | 5 | 5 failed | `t3-attach-red-item9-10.log` |
| 基线：第 11、12 项单独跑 | 1 | 4 | 4 failed | `t3-attach-red-item11-12.log` |
| 基线：含两个新测试文件的 `vue-tsc --noEmit` | — | — | **exit 0**（测试文件无类型错误） | `t3-attach-tsc-baseline.log` |

> 说明：基线 worktree 里同时存在其他队友拷入的 `q46-r8-drafts-*` / `q46-r8-select-cancel` 等测试文件（均未跟踪），
> 本次红证据只运行上面两个属于 task-3 的文件；`git status` 证明该 worktree 的产品代码没有任何改动。

## 二、逐用例红证据（基线 c9fedc60 → 失败断言原文 → 产品缺口）

### 四.9 替换文件失败后，重试应处理用户刚选的文件（`q46-r8-attachment-replace.test.ts`，3 例全 FAIL）

| # | 用例 | 基线 | 失败断言原文（节选） |
|---|---|---|---|
| 9-a | 不同文件名：old.txt → new.txt 上传失败后重试，必须重传 new.txt，成功后卡片/保存候选都是 new 的附件 ID | **FAIL** | `AssertionError: 重试关联没有重新上传用户刚选的 new.txt（复用了旧附件 ID）: expected 2 to be 3` |
| 9-b | 同名不同内容（都叫 same.txt）：第 2 版上传失败后重试必须重传第 2 版，不得复用第 1 版附件 ID | **FAIL** | `AssertionError: 同名文件重试复用了第 1 版的附件 ID（两次尝试没有被区分）: expected 2 to be 3` |
| 9-c | 旧上传回执晚于新尝试返回：不覆盖新关联，也不得成为后续重试的附件来源 | **FAIL** | `AssertionError: 重试用了迟到旧上传的附件 ID（没有重传本次尝试的 z.txt）: expected 3 to be 4` |

**缺口判断（interactive.ts，基线行号）**：

1. `settledAttachmentByCard`（7647）只按 `importCardMapKey(boardId, cardId)` 存「这张卡可复用的附件」，**不记录它属于哪一次尝试 / 哪个文件源**；
   `retryCardAttachment()`（8331-8345）无条件取该卡缓存作为 `reuseAttachment` 交给 `startCardAttachment()`。
   于是「卡片先成功关联过 old.txt」后，选 new.txt 上传失败 → 重试直接复用 `att_old.txt`，
   **不重传 new.txt**（9-a：上传次数停在 2，期望 3），并把 old 的附件写回卡片。
2. 该缓存也不区分「同名不同内容」：`reuseAttachment` 只带附件对象，没有内容指纹/尝试代次（9-b 同一现象）。
3. `executeImportAttempt()` 在 `waitUntilSettled` 之后、`applyImportAssociation()` 之前**无条件**
   `settledAttachmentByCard.set(...)`（8068-8081）——身份核对（被更新的尝试取代）在后面的
   `applyImportAssociation` 里才做。于是迟到的旧上传回执虽然不写板面，却会覆盖掉该卡的「可复用附件」缓存；
   之后重试就拿**迟到的旧上传**的附件 ID 去覆盖更新的关联（9-c：上传次数停在 3，期望 4）。
4. 9-a/9-b 同时断言「替换成功前卡片保留旧附件」（基线 PASS，属于前置/诚实性断言）。

### 四.10 旧保存失败只影响它请求里包含的关联（`q46-r8-attachment-replace.test.ts`，2 例全 FAIL）

| # | 用例 | 基线 | 失败断言原文（节选） |
|---|---|---|---|
| 10-a | 单卡：B 的旧保存（候选不含新附件）在飞 → 关联新附件 → 切 A → 旧保存失败迟到：新附件不得被标失败；回 B 后包含新附件的保存成功才变成功 | **FAIL** | `AssertionError: 旧保存失败把不含它的新关联标成了失败: expected 'failed' to be 'processing'` |
| 10-b | 批量：同一时序（批次项关联在旧保存之后写入）：旧保存失败不得把批次项标失败；包含新附件的保存成功后才标已接收 | **FAIL** | `AssertionError: 旧保存失败把不含它的批次项标成了失败: expected 'failed' to be 'awaiting_save'` |

**缺口判断（interactive.ts，基线行号）**：

- `settleCardImportSaveForBoard(targetBoardId, ok, error, savedState)`（8391-8476）在 `ok === false` 时
  遍历该板**所有** `awaitingSave` 的单卡 job（8406-8424）与所有 `awaitingSaveImportItems`（8460-8475），
  **不核对该关联是否包含在这次失败请求的候选里**（失败回执没有 server state，而基线也没有保留请求候选做核对）。
- 调用点（切板后 `boardId !== ownerBoardId` 的失败分支 2950-3004）同样对原板整批结算。
  于是「关联写入之后才发出的 PUT 失败」会把之后才关联、其保存在后续请求里的新附件一起标失败；
  后续真正包含它的保存成功时，因为 job 已不是 `awaitingSave`，成功结算直接跳过（8414 的 `continue` 前已被 loop 条件过滤），
  失败状态永远留着。
- 正确的核对依据已存在：一次保存的 `attempt.snapshot`（该请求自己的候选）与成功回执的 `result.state`。
  失败结算也应只处理「snapshot 里包含该卡+该附件」且仍有效的尝试（与成功结算同一口径）。

### 四.11 批量导入的反馈和重试入口归原板（`q46-r8-import-board.test.ts`，2 例全 FAIL）

| # | 用例 | 基线 | 失败断言原文（节选） |
|---|---|---|---|
| 11-a | B 拖入 2 个文件开始批次 → 上传期间切 A：A 不显示 B 的批次项/汇总/进行中/重试；B 的批次按原板维护、回 B 可见可重试；只 B 建卡、上传不重复、同批其他项不变 | **FAIL** | `AssertionError: A 上显示了 B 的批次（反馈没有按板面归属）: expected { id: 'imp_1_...', ... } to be null` |
| 11-b | 重试核对发起时的板面/批次/项/目标卡：点重试（上传挂起）后切 A，A 不出现 B 的重试/进行中；回执不写 A；回 B 重试复用附件、只 B 建卡 | **FAIL** | `AssertionError: 点击重试后切到 A：A 上出现了 B 批次的重试入口: expected { id: 'imp_1_...', ... } to be null` |

**缺口判断（interactive.ts，基线行号）**：

- 批次数据层本身是按板面记的（`ImportBatch.boardId`、`ImportBatchItem.boardId`，beginImportBatch 7500-7515），
  但「当前该显示哪一批」只有**全局一份**：`latestImportBatchId`（7483）→ `currentImportBatch`（7485-7487）→
  `currentImportBatchCounts`（7521-7523）/`hasImportBatchInFlight`（7525-7528）。
- `AddMenu.vue` 的 `activeImportBatchId` 是组件本地状态（279-284），只在挂载时从 `store.currentImportBatch` 取一次；
  切板（组件不卸载）时只重刷单卡提示（609-617），批次区继续显示**全局**最近一批。
  于是 B 的批次项、失败与「重试」按钮原样出现在 A 上（11-a/11-b 红）。
- 期望的最小公开契约（本轮测试断言的就是它）：`store.currentImportBatch`、`currentImportBatchCounts`、
  `hasImportBatchInFlight` 按**当前板面**取值（A 没有批次时为 null / 零计数 / false），
  `importBatchById(batchId)` 仍能按 id 取到原板批次（B 的进度继续按 B 维护）。
  组件侧在 `watch(() => store.boardId)` 里按当前板面恢复批次入口即可（无需新增状态名）。

### 四.12 撤销附件关联后结束失效的等待（`q46-r8-import-board.test.ts`，2 例全 FAIL）

| # | 用例 | 基线 | 失败断言原文（节选） |
|---|---|---|---|
| 12-a | 单卡：关联写入但保存未确认（awaiting save）→ 用户 undo 撤销关联 → 等待状态立即结束（不留 processing/awaitingSave/active）、旧保存成功回执迟到不复活、撤销后的保存确认无该关联；redo 重做复用已上传附件，只在包含该关联的保存成功后显示成功 | **FAIL** | `AssertionError: 撤销关联后附件提示仍停在处理中（不会结束）: expected false to be true` |
| 12-b | 批量：同一时序的批次项（awaiting_save）→ undo → 项状态立即结束、不显示在途；旧保存成功回执不复活；重跑该项复用附件，只在包含该关联的保存成功后标已接收 | **FAIL** | `AssertionError: 撤销关联后批次项仍停在「关联待保存 / 上传中」: expected 'awaiting_save' not to be 'awaiting_save'` |

**缺口判断（interactive.ts，基线行号）**：

- `awaitingSave` 的结束只有一条路径：保存回执 `settleCardImportSaveForBoard`（单卡 8422、批次 8467）。
  用户 `undo()`（3340-3354）只是把板面内容恢复成关联前、置脏重排保存，**没有重新核对**「这次关联还在不在候选里」。
- 于是撤销后：job 仍是 `awaitingSave / processing`、`activeCardImportForBoard` 仍返回它（8348-8356），附件提示一直「处理中」（12-a/12-b 红）；
  撤销前发出、撤销后返回的旧保存成功回执，其 `result.state`（快照）里包含该关联 → `savedStateHasAttachment` 为真 →
  把已撤销的关联结算成成功（与用户撤销后的板面矛盾）。
- 期望的最小公开契约：候选里已不存在「该卡 + 该附件 ID 的关联」时（undo/redo/commit 清空关联/删卡等公开动作后），
  对应的单卡 job 必须离开 `processing/awaitingSave`（`activeCardImportForBoard` 为 null），批次项离开 `uploading/awaiting_save`；
  迟到回执不能把它结算成成功。若用户 redo / 重跑该项让关联重新出现在候选里，已上传附件 ID 可复用（不再上传），
  等包含该关联的保存回执成功后才显示成功。

## 三、测试设计说明（为什么不是「测内部实现」）

- 全部经由 store **公开动作**驱动：`selectBoard` / `commit` / `undo` / `redo` /
  `startCardAttachment` / `retryCardAttachment` / `beginImportBatch` / `setImportItemSource` /
  `runImportBatch` / `runImportItem`。
- 断言只落在**公开状态**与**真实 API mock 的调用参数/次数**上：
  - 公开状态：`board.cards[].meta.file.attachmentId`、`cardImportJobs`（phase / awaitingSave / attachment /
    pendingAttachment / error）、`activeCardImportForBoard`、`currentImportBatch` /
    `currentImportBatchCounts` / `hasImportBatchInFlight`、`importBatchById(...).items[].status/attachmentId`、
    `saveStatus`、`boardId`；
  - API 事实：`attachments.uploadAttachment` 的调用次数与**被上传的 File 对象**（同一对象用引用比较，
    同名不同内容用不同 File 对象 + 内容=回执 id 区分）、`api.saveBoardState(boardId, candidate)`
    的板面 id 与候选里的卡片附件 id。
- mock 只打在**服务边界**（`services/interactive`、`services/attachments`），用受控 promise 制造
  「上传挂起 / 保存挂起 / 保存失败 / 旧回执迟到」；不 mock store 内部逻辑，不依赖新增内部字段命名。
- 「一次保存请求自己的候选」由 `saveBoardState` 的第二个参数直接读出，用来证明「旧保存不含新附件」这一前提。
- 时间：store 的保存防抖是 450ms，用例统一用 `setTimeout(600ms)` + `flushPromises()` 等它真的发出；
  不使用 fake timers，避免干扰 store 内部轮询/防抖的既有实现。

## 四、给实现（Lead）的最小公开契约清单

1. **第 9 项**：`retryCardAttachment(cardId)` 只能复用**当前这次等待恢复的尝试**取得的附件；
   当前尝试还没取得附件（上传失败）时必须重传本次的文件源（同名不同内容也要重传）；
   上传成功写入「可复用事实」前必须先通过尝试代次核对（迟到旧上传不得覆盖该卡的可复用事实）。
   替换成功前卡片保留原附件。
2. **第 10 项**：保存失败结算（单卡 + 批次）必须按**这次请求的候选**（`attempt.snapshot`）核对
   「候选里是否包含该卡 + 该附件 ID」，不包含就保持等待；成功结算仍用回执 state 核对。
3. **第 11 项**：`currentImportBatch` / `currentImportBatchCounts` / `hasImportBatchInFlight`
   按**当前板面**取值；`importBatchById` 保持按 id 可取（原板进度继续维护）。
   重试沿用 batchId / itemKey / item.cardId / item.boardId（现有 `runImportItem(batchId, itemKey)` 已带归属）。
4. **第 12 项**：撤销 / 清空 / 删除等公开动作让关联从候选消失后，对应的单卡 job / 批次项要立即离开
   `processing / awaitingSave`（`activeCardImportForBoard` 为 null），迟到保存回执不得把它结算成成功；
   关联重新出现（redo / 重跑该项）时复用已上传附件，只在包含该关联的保存回执成功后显示成功。

> 注：以上契约只使用了基线已有的公开字段/动作名（`currentImportBatch`、`cardImportJobs`、
> `activeCardImportForBoard`、`undo`/`redo` 等），没有要求实现新增特定命名字段。

## 五、诚实边界

1. 基线红是在**干净 worktree** `D:\qio-dev\_q46-r8-baseline`（同 SHA、产品代码零改动）上跑的，
   不是 Lead 正在改的产品工作树；红证据也不含其他队友的在途修复。
2. 九条用例在基线上均为**断言失败**（无 `TypeError` / 语法 / 测试自身异常）；两个测试文件在基线上
   `vue-tsc --noEmit` exit 0。
3. 每个用例里同时包含若干**基线即 PASS 的前置/诚实性断言**（如「替换成功前卡片保留旧附件」「前提：旧保存在飞」
   「前提：upload 只调用一次」）。它们不是红点，用来保证失败确实发生在目标缺口上，而不是场景没搭起来。
4. 第 9-c 用例在基线上先在「迟到回执被当成成功」处红过一次；已修正为按**该次迟到尝试自己的
   attemptId** 断言（`cardImportJobs[String(lateAttemptId)]`），最终红点是「重试复用了迟到旧上传的附件」。
5. 本次未跑全量测试、未改产品代码、未 commit/push、未动 `docs/status.md`。
   两个测试文件已放在产品工作树 `D:\qio-dev\q46-r8` 的相同路径，等 Lead 的实现落地后再复跑绿。
6. 复跑绿时若某条在实现上用了不同的公开入口/状态名，请以第「四」节的最小契约对齐后再改断言，
   不要放宽成「只测实现细节」。
