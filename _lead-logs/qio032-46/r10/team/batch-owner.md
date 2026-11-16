# Task 2（共享任务 task-5）：查证成功后按原提交归组 —— batch-owner 报告

- 产品工作树：`D:\qio-dev\q46-r9`（分支 `fix/qio032-46-r9`，HEAD `70e475c2`，= 计划起点，未改分支基线）
- 红基线：`D:\qio-dev\_q46-r10-baseline`（同一 SHA，产品代码零改动；每次跑完已 `git checkout --` 还原，末尾 `git status` 为空）
- 时间：2026-11-16 13:36–13:46（+08:00）
- 未 git add/commit/push；未改 docs/status.md；未跑全量 / CI；未联网、未用真实 Key（全部 fake provider mock）

## 一、改动落点（只改这 2 个文件，interactive.ts 全部用 edit 小范围替换）

`frontend/src/stores/interactive.ts`
1. `recordNewIntents(before, batchKey, submissionId?)`：
   - 传了 `submissionId` 时，只记录 `intent.submissionId === submissionId` 的意图（服务端明确归属）；
   - 意图没有 `submissionId` 字段时，仅在 `before.size > 0`（可信增量基线）时按原增量逻辑保留；
   - 不传 `submissionId` 的既有调用方（理解同步 / 演示入口）行为完全不变；
   - 旧任务绝不写进本次批次的本地映射（`recordIntentBatch` 不收到旧任务 id）。
2. `confirmAcceptanceAfterUnclear(ownerBoardIdArg?, intentsBaseline?)`：
   - submit() 的未知回执 catch 里传**发起提交时冻结的 `intentsBefore`**；`restorePendingSubmission()`（页面重载）无基线 → 传 undefined/null；
   - 查证成功后的刷新上下文带 `submissionId = acceptance.submissionId`；
   - owner 当前：`refreshAfterSubmit(owner, baseline ?? intentIdSet(), lookupBatchKey, { submissionId })`；
   - owner 不在当前：`deferredRefreshAfterSubmit = { intentsBefore: baseline ?? [], batchKey, submissionId }`（不再写死空基线）。
3. `SubmitRefreshContext` / `BoardSubmissionSession.deferredRefreshAfterSubmit` 增加 `submissionId?: string | null`。
4. `refreshAfterSubmit(ownerBoardId, intentsBeforeInput, batchKey, options?: { submissionId? })`：
   - 冻结上下文记录 `submissionId`；重新延后（deferRemaining）时一并保存；收尾时 `recordNewIntents(..., submissionId)`。
5. `retrySubmitRefresh()` 复用冻结上下文里的 `submissionId`。
6. `load()` 消费 deferred 时把 `submissionId` 一并传入同一刷新入口。
7. 未新增任何 submit POST；查证路径仍只有 identity 精确查证（`fetchAcceptance`），无时间/最近记录推断。

`frontend/src/stores/__tests__/q46-r8-submit-ownership.test.ts`（+188 行，新增 describe「一.1c 查证成功后按原提交归组（第十轮）」）
- 用例 A（红→绿）：B 有旧任务 i_old(sub_old) → B 提交 POST 未知回执 → **A 显示时** fetchAcceptance 成功(sub_new) → 切回 B，fetchIntents 返回 [i_old(sub_old), i_new(sub_new)] → 断言 `recordIntentBatch` 只有 1 次且为 (`session:submission:sub_new`, `["i_new"]`)、`batchKeyOf(i_old)=="submission:sub_old"`、`submitBoard` 只 1 次、切回 B 可见「按身份查证确认受理」。
- 用例 B（红→绿）：owner 当前（B）时查证成功，B 本地提交时清单为空、刷新后出现 [i_old(sub_old), i_new(sub_new)] → 断言同样只记 `["i_new"]`、旧任务留 `submission:sub_old`、submitBoard 只 1 次。
- 用例 B 是「按服务端归属」而非本地增量的守卫：基线代码用 `intentIdSet()`（当时为空）当增量基线，同样会把旧任务并进本次批次，所以也是红→绿。

## 二、红证据（_q46-r10-baseline，未改产品代码）

命令（在 `D:\qio-dev\_q46-r10-baseline\frontend`）：
```
npx vitest run src/stores/__tests__/q46-r8-submit-ownership.test.ts
```
- 日志：`_lead-logs/qio032-46/r10/red/task2-q46-r8-submit-ownership.log`
- 结果：`Tests 2 failed | 10 passed (12)`，exit 1；两条新用例都因同一原因红：
  - `expected [ 'i_old', 'i_new' ] to deeply equal [ 'i_new' ]`（旧任务被记进本次提交批次）；
  - 红日志中其余 10 条既有用例通过。
- 采集方式：把改过的测试文件复制到基线跑 → `git -C D:\qio-dev\_q46-r10-baseline checkout -- frontend/src/stores/__tests__/q46-r8-submit-ownership.test.ts` 还原；基线 `git status --porcelain` 为空。

## 三、绿证据（产品工作树）

| 命令（`cd D:\qio-dev\q46-r9\frontend`） | 结果 | 日志 |
| --- | --- | --- |
| `npx vitest run src/stores/__tests__/q46-r8-submit-ownership.test.ts` | 12/12 通过，exit 0 | `green/task2-q46-r8-submit-ownership.log`；并发改动进入共享树后复跑 `green/task2-q46-r8-submit-ownership-rerun.log` 仍 12/12 |
| `npx vitest run src/stores/__tests__/q46-r7-attachment-state.test.ts src/stores/__tests__/q46-r7-cancel-recovery.test.ts src/stores/__tests__/q46-r7-drafts.test.ts src/stores/__tests__/q46-r7-impact-ownership.test.ts src/stores/__tests__/q46-r7-save-limit.test.ts src/stores/__tests__/q46-r7-task-isolation.test.ts src/acceptance-state-recovery/n8-submit-read-once.test.ts` | 7 files / 29 tests 全通过，exit 0 | `green/task2-regression-r7-n8.log` |
| `npx vue-tsc --noEmit` | exit 0，无输出 | （终端输出） |

## 四、风险与未验证项

1. **并发同一文件**：`interactive.ts` 同时被 Task 3 队友改（`retryCancelRecovery/settleCancelRecovery` 区域，行号约 7870）。我的改动都在独立区域，最后复跑所有权用例 12/12 通过；但 Lead 集成时请以最终合并态再跑一次定向回归。我的 diff 只包含上述 7 处落点，没有动 Task 3 区域。
2. **基线为空的可信度口径**：没有 `submissionId` 字段的意图在「基线为空」时不再按增量归批（宁可不进本地批次映射，也不误合并）。若未来后端不再返回 intent.submissionId 且页面重载后才查证，批量列表可能退化（不会误合并）。当前后端返回该字段（types.ts Intent.submissionId），符合计划口径。
3. **未验证**：真实服务端行为（只读单测 + fetch mock）；Windows 安装版/视觉检查不在本任务范围（Lead 验收）；未跑全量前端测试、未跑后端 pytest。
4. 既有 `_tmp-*` 日志与其他队友文件不是我写的；我未改 `docs/status.md`、未提交 git。
