# task-3 · R9 队友 async-owner 交付报告（Task 3 提交后回读归原板 + Task 4 撤回重试只在服务器事实确认后报成功）

> 产品工作树 `D:/qio-dev/q46-r9`（分支 `fix/qio032-46-r9`，基线/验收 SHA `645e5a1f0cda0669621e0c491fbe1ab75479a17b`）；红基线工作树 `D:/qio-dev/_q46-r9-baseline`（detached 同 SHA、产品代码零改动）。
> 只跑受影响的定向测试，未跑全量 / 未触发 CI / 未联网、未用真实 API Key、未提交 git。
> `frontend/src/stores/interactive.ts` 与队友（drafts）共享：本任务只用 `edit` 做小范围唯一字面替换，未用 `write` 整文件，未碰草稿区逻辑。
>
> **2026-11-16 12:10 返修（Lead 复核意见）**：按任务书第 3 项收紧「只有实际采纳的刷新才能结束等待」——`refreshAfterSubmit` 现用 `boardReadAdoption` 的读取代次前进判定整板读取是否真的采纳了板面事实；「回读比 knownServerSeq 旧、只合并草稿」的场景必须落到 error（不得 idle）。新增对应 store 用例并重采红/绿证据（见 1.3/1.4）。风险 1 已关闭。

## 0. 交付文件清单

| 类型 | 文件 | 说明 |
| --- | --- | --- |
| 产品代码 | `frontend/src/stores/interactive.ts` | Task 3 + Task 4 两处区域（下述函数） |
| 测试 | `frontend/src/stores/__tests__/q46-r8-submit-ownership.test.ts` | 原 5 例 → **10 例**（新增 5 例，Task 3；含返修新增的「旧读取只合并草稿」用例） |
| 测试 | `frontend/src/stores/__tests__/q46-r8-select-cancel.test.ts` | 原 4 例 → 9 例（新增 5 例，Task 4） |
| 红证据 | `_lead-logs/qio032-46/r9/red/red-r9-submit-ownership-task3-baseline.log` | 基线：`5 failed / 5 passed`（10 例；返修后重采） |
| 红证据 | `_lead-logs/qio032-46/r9/red/red-r9-select-cancel-task4-baseline.log` | 基线：`3 failed / 6 passed`（9 例） |
| 绿证据 | `_lead-logs/qio032-46/r9/green/green-r9-submit-ownership-task3.log` | `10 passed`（返修后重采） |
| 绿证据 | `_lead-logs/qio032-46/r9/green/green-r9-select-cancel-task4.log` | `9 passed` |
| 绿证据 | `_lead-logs/qio032-46/r9/green/green-r9-targeted-r7-r8-regression.log` | r7+r8 共 12 文件 `74 passed`（返修后重采） |
| 绿证据 | `_lead-logs/qio032-46/r9/green/green-r9-submit-adjacent-regression.log` | acceptance-state-recovery 全 13 文件 + p3c-retry-recovery + ImpactConfirmLayer + recovery-lead-state-facts 共 16 文件 `81 passed` |
| 绿证据 | `_lead-logs/qio032-46/r9/green/green-r9-vue-tsc.log` | 空输出 = `vue-tsc --noEmit` 通过（exit 0） |

---

## 1. Task 3：提交后回读只归发起板

### 1.1 根本原因（基线 645e5a1f 的实际行为）

`refreshAfterSubmit(intentsBefore, batchKey)` 与 `retrySubmitRefresh()` 全程使用**全局** `boardId` / `submitRefreshState` / `submitRefreshError`：

1. 在 B 提交、切到已经加载好的 A 后，B 的提交后读取继续按**当前**板面执行（`refreshBoardFromServer()` 无参 → A；`refreshVisibleRange()` → A；`refreshIntentFacts()` → `loadIntents()`/`loadIntentViews()` → A），并且 `recordNewIntents()` 把 A 上读到的新任务记进 **B 的提交批次**（跨板串批次）。
2. 回读失败时 catch 直接写全局 `submitRefreshState = "error"`：错误落在 A 上；回到 B 只看到 B 被冻结成 `pending`（或之后永远 pending，因为延后刷新不会被消费）。
3. 没有按板刷新代次：B→A→B 后旧一次刷新的迟到失败会覆盖新一次刷新的成功结果。
4. 子读取返回值没有检查：`refreshBoardFromServer` 返回 false（读取未采纳 / 已切走）、`refreshVisibleRange` 失败、任务清单读取失败都被忽略，只要没抛错就置 idle。**尤其**：回读比已知服务器版本更旧时 `refreshBoardFromServer` 返回 true，但只合并草稿、没有采纳板面事实 —— 旧实现把它当「刷新成功」直接置 idle（返修后已收紧，见 1.4）。
5. `load()` 消费 `deferredRefreshAfterSubmit` 时先写全局 pending 再调无主刷新；`retrySubmitRefresh()` 也不保留原板上下文。

### 1.2 改动落点（`interactive.ts`）

| 落点 | 行（当前文件） | 内容 |
| --- | --- | --- |
| `SubmitRefreshContext` 接口 | 6348 | `{ boardId, gen, intentsBefore[], batchKey }`（冻结的刷新上下文） |
| `BoardSubmissionSession.lastRefreshContext` 字段 | 6387 | 会话级「最近一次刷新上下文」，`retrySubmitRefresh` 复用 |
| `submitRefreshGenByBoard` | 6399 | `Map<boardId, number>` 按板刷新代次（只增） |
| `captureSubmissionSession` | 6433 | 离开板面时把 `lastRefreshContext` / `deferredRefreshAfterSubmit` / `deferredCheckedClear` / `deferredUnderstandingPolling` 原样带走（此前 capture 会把延后刷新清成 null） |
| `submitRefreshContextFor` / `rememberSubmitRefreshContext` | 6507 / 6511 | 读写原板冻结上下文（只写会话，不碰显示状态） |
| `refreshAfterSubmit(ownerBoardId, intentsBefore, batchKey)` | 6977 | 入口冻结板面/代次/批次/任务基线；每步显式带 ownerBoardId；每个 await 后校验「代次仍最新 + 板面仍原板」；离开原板→未开始步骤写回原板 `deferredRefreshAfterSubmit`（保持 pending）；已返回的真实失败经 `patchSubmissionFor` 归原板；`refreshBoardFromServer` 返回 false / `refreshVisibleRange` 返回 false / `loadIntents` 返回 false 都算本次未完成（error），只有实际采纳才置 idle；旧代次一律不写状态 |
| **整板读取「实际采纳」判定（返修点）** | 7024-7040 | 进入整板读取前记 `readSeqBefore = boardReadSeq`；返回 true 后再看 `adoptedReadFor(ownerBoardId)`：`adopted !== null && adopted.readSeq > readSeqBefore` 才算「这次刷新实际采纳了板面事实」；否则 `fail("…没有被采纳（只合并了草稿）…")` 归原板，**不得置 idle**。并发 load() 在本次刷新开始后采纳了同一块板面的事实同样算已采纳（记取代次前进即可） |
| `retrySubmitRefresh()` | 7090 左右 | 复用原板冻结上下文（板面/批次不变）；点击后立刻切板 → 延后回原板收尾，返回 ok:false + 可重试说明；不在新板写状态/发读取 |
| `refreshVisibleRange(target)` | 4465 | 返回 boolean：true = 这次读取真的被采纳；false = 已切走 / 被更新读取取代 / 失败（此前是 void，无法判断完成） |
| `load()` 消费延后刷新 | 4766-4774 | 不再先写全局 `submitRefreshState="pending"`，直接走 `refreshAfterSubmit(boardId.value, deferred...)` 同一入口 |
| `submit()` 调用点 | 6885 | 传 `ownerBoardId` |
| `confirmAcceptanceAfterUnclear()` 调用点 | 8227 | 传 `ownerBoardId`（查证恢复的提交后刷新同样归原板） |

### 1.3 红/绿证据（逐用例）

红命令（基线工作树，先 `Copy-Item` 改后的测试文件过去，跑完 `git checkout --` 还原，已验证基线 `git status` 干净）：

```powershell
cd D:/qio-dev/_q46-r9-baseline/frontend
npx vitest run src/stores/__tests__/q46-r8-submit-ownership.test.ts
# → Test Files 1 failed (1) | Tests 5 failed | 5 passed (10)
```

| # | 新增用例 | 基线失败断言（原文摘） | 绿（产品工作树） |
| --- | --- | --- | --- |
| 1 | B 的提交后回读在 A 打开时失败：A 保持 idle，错误只回到 B | `B 的回读失败写进了 A 的刷新状态: expected 'error' to be 'idle'` | ✓ |
| 2 | B 的提交后回读在 A 打开时成功：A 不被刷新也不再记 B 的批次，回 B 后收敛为 idle | `B 的提交后回读继续对 A 发起了读取: fetchIntents/fetchVisibleRange +1`（断言 =`readCounts()` 逐板请求数） | ✓ |
| 3 | B→A→B 之后新刷新成功：旧刷新迟到的失败不得覆盖新结果 | `旧代次的迟到失败覆盖了更新一次的成功结果: expected 'error' to be 'idle'` | ✓ |
| 4 | 在 B 点重试刷新后立刻切到 A：不在 A 写状态、不对 A 发读取 | `重试刷新在切板后仍报告成功: expected true to be false` | ✓ |
| 5（返修新增） | B 提交后回读只合并了旧草稿（未采纳板面事实）：不得置 idle，重试拿到新版本才收敛 | `回读只合并了旧草稿（没有采纳板面事实），刷新却被当成成功（idle）: expected 'idle' to be 'error'` | ✓ |

断言口径：`fetchBoardState` / `fetchVisibleRange` / `fetchIntents` 按 **boardId 分板计数**（`readCounts()`）、两块板 store 状态（`submitRefreshState/Error`）、批次归属（`approval.recordIntentBatch` 调用次数）、`submitBoard` 只调用一次。

用例 5 步骤：`openABothLoaded` → 编辑 B → `submit()`（保存拿到 seq 8 → knownServerSeq=8；提交后回读默认 mock 返回 seq 7，比已确认版本旧 → 只合并草稿）→ 断言 `submitRefreshState === "error"` 且 `submitRefreshError` 含「没有被采纳」→ `retrySubmitRefresh()` 的 mock 返回 seq 9（真正采纳）→ 断言 `ok:true`、idle、错误清空、`submitBoard` 仍只 1 次。

透明说明（返修时同步调整了 3 个既有新用例的 mock 数据，不是放宽断言）：用例 2/3/4 回到原板后 / 重试时的 `fetchBoardState` 现在返回**保存/提交后的版本**（seq 8 / seq 9），这与真实服务端在这些时刻的事实一致；收紧前它们依赖「回读返回 seq 7 也当成功」的旧行为。断言的目标（A 不被刷新、旧代次不覆盖、切板不写 A）未变。

绿命令与结果：

```powershell
cd D:/qio-dev/q46-r9/frontend
npx vitest run src/stores/__tests__/q46-r8-submit-ownership.test.ts
# → Test Files 1 passed (1) | Tests 10 passed (10)
```

### 1.4 返修：整板读取「实际采纳」收紧（风险 1 关闭）

- 进入整板读取前 `const readSeqBefore = boardReadSeq;`（`interactive.ts:7024`）。
- `await refreshBoardFromServer(ownerBoardId)` 之后仍先校验 `isLatestGen()` / `ownerIsCurrent()`；返回 false 维持原 defer/fail 分支。
- 返回 true 时再取 `const adoptedAfterRefresh = adoptedReadFor(ownerBoardId)`（`interactive.ts:7036`）：只有 `adopted !== null && adopted.readSeq > readSeqBefore` 才继续；否则 `fail("这次提交后的板面读取没有被采纳（只合并了草稿），页面没有刷新；请点「重试刷新」")`，状态落 error、保留重试入口，**不置 idle**。
- 没有改 `refreshBoardFromServer` 的返回语义（`load()` 等既有调用面依赖它）；「未采纳」由 `boardReadAdoption`（Task 4 新增的采纳事实记录）判定。
- 并发的 `load()` 读取若在本次刷新开始后采纳了同一块板面的事实，同样满足 `readSeq > readSeqBefore` → 按「板面确实被刷新」接受。

---

## 2. Task 4：撤回重试只在服务器事实确认后报成功

### 2.1 根本原因（基线 645e5a1f 的实际行为）

1. `retryCancelRecovery()` 只看 `refreshBoardFromServer()` 没抛错（返回 true）就 `settleCancelRecovery(true)` 并返回 `ok:true`：**读到比撤回基线更旧的版本也算确认**（expectSeq=7 / 回读 6 → 直接关闭等待）。
2. `settleCancelRecovery` 的成功分支只在「facts 存在且 seq 有限」时才比较 expectSeq；facts 缺失 / `seq: NaN` 直接跳过校验 → 保存回执里 `{ seq: Number(result.seq) }` 在回执缺 seq 时是 NaN，**把未确认说成已确认**。
3. `refreshBoardFromServer` 不记录「哪一次读取真的被采纳」：重试无法证明本次回读被采纳、也无法证明采纳事实的版本到达基线（容易拿界面缓存 `knownServerSeq` 冒充）。

### 2.2 改动落点（`interactive.ts`）

| 落点 | 行（当前文件） | 内容 |
| --- | --- | --- |
| `boardReadAdoption` + `recordBoardReadAdoption` / `adoptedReadFor` | 1229-1238 | 按板记录**实际采纳**的读取事实 `{ readSeq, seq }`（readSeq 只增） |
| 采纳点①：候选合并采纳 `adoptServerFacts` 分支 | 4279 | `recordBoardReadAdoption(requestedBoardId, readSeq, payloadSeq)` |
| 采纳点②：整块采用服务器事实分支 | 4333 | 同上（仅这两个真正采纳分支；旧读取 / 只合并草稿不记录） |
| `settleCancelRecovery` | 3729 | 成功必须带**有限、有效 seq>0** 且 `>= expectSeq`；缺版本 / NaN / 0 / 旧版本 / opSeq 不匹配 → 不结算成功 |
| 保存回执结算（后台成功分支） | 3056-3058 | `{ seq: Number.isFinite(Number(result.seq)) ? Number(result.seq) : undefined }` — 不再把 NaN 传进去 |
| 保存回执结算（前台成功分支） | 3129-3131 | `{ seq: Number.isFinite(acceptedServerSeq) ? acceptedServerSeq : undefined }` |
| `retryCancelRecovery()` | 7803 | 捕获本次读取起始 `boardReadSeq`；只有「刷新实际采纳 + `adopted.readSeq > 起始` + `adopted.seq>=expectSeq` + `opSeq` 仍匹配」才 `settleCancelRecovery(true,...,{seq})` 并 `ok:true`；否则 `ok:false`（可重试原因）且保持 active；不使用 `knownServerSeq` 缓存；迟到成功/失败只按 opSeq 结算、满足条件只结算一次 |

### 2.3 红/绿证据（逐用例）

红命令（基线工作树，同样复制测试文件→跑→`git checkout --` 还原）：

```powershell
cd D:/qio-dev/_q46-r9-baseline/frontend
npx vitest run src/stores/__tests__/q46-r8-select-cancel.test.ts
# → Test Files 1 failed (1) | Tests 3 failed | 6 passed (9)
```

| # | 新增用例 | 基线失败断言（原文摘） | 绿（产品工作树） |
| --- | --- | --- | --- |
| ① | expectSeq=7 只读到 6：重试不结算；读到 7 才报成功并结束等待 | `服务器版本还没到撤回基线，重试却报告成功: expected true to be false` | ✓ |
| ② | 回读版本缺失（seq=0）：不结算成功，仍保留重试入口 | `回读没有可用版本依据却报告成功: expected true to be false` | ✓ |
| ③ | 保存回执缺 seq（`Number(undefined)=NaN`）：不得绕过版本校验结算撤回 | `缺版本的保存回执被当成服务器事实确认了撤回: expected false to be true` | ✓ |
| ④ | 切板后的回读不结算本板撤回，A 的撤回状态不受影响（护栏；基线也绿，防回归） | —（基线绿：既有板面身份保护） | ✓ |
| ⑤ | 同板连续两次撤回：旧的成功 / 失败迟到都不得关闭或改写较新的撤回（护栏；基线也绿） | —（基线绿：既有 opSeq 保护） | ✓ |

断言同时看 `retryCancelRecovery()` 的返回值与界面公开等待状态 `cancelRecovery.active/reason`；红用例全部是断言失败（AssertionError），不是测试自身报错/超时。

绿命令与结果：

```powershell
cd D:/qio-dev/q46-r9/frontend
npx vitest run src/stores/__tests__/q46-r8-select-cancel.test.ts
# → Test Files 1 passed (1) | Tests 9 passed (9)
```

---

## 3. 定向回归与类型检查（产品工作树当前状态，返修后重跑）

| 命令 | 结果 | 日志 |
| --- | --- | --- |
| `npx vitest run` q46-r7 全 6 文件 + q46-r8 全 6 文件（12 文件） | `Test Files 12 passed (12)` / `Tests 74 passed (74)` | `green/green-r9-targeted-r7-r8-regression.log` |
| `npx vitest run` `src/acceptance-state-recovery` 全 13 文件 + `p3c-retry-recovery` + `ImpactConfirmLayer` + `recovery-lead-state-facts`（16 文件） | `Test Files 16 passed (16)` / `Tests 81 passed (81)` | `green/green-r9-submit-adjacent-regression.log` |
| `npx vitest run` `pm032-board-save-isolation`（26 例，切板/保存/撤回归属） | `26 passed`（返修前运行，本次未受影响区域） | 会话内输出 |
| `npx vue-tsc --noEmit` | exit 0，无输出 | `green/green-r9-vue-tsc.log` |

基线工作树在两次红证据采集后均已 `git -C D:/qio-dev/_q46-r9-baseline checkout -- <测试文件>`，`git status --porcelain` 为空（已验证）。

---

## 4. 未验证项 / 风险（诚实清单）

1. ~~`refreshBoardFromServer` 的「更旧读取只合并草稿」分支返回 true 被当已采纳~~ —— **已关闭（返修）**：`refreshBoardFromServer` 返回语义未改，但 `refreshAfterSubmit` 改用 `boardReadAdoption.readSeq` 前进判定「实际采纳」；旧读取只合并草稿 → error + 重试入口，不置 idle（新增用例锁定）。
2. **通用读取仍可结算撤回**：`refreshBoardFromServer` 两个采纳分支里的 `settleCancelRecovery(true,...)` 是既有口径（任何该板被采纳的读取都能确认撤回，r7 用例明确要求「回到 B 的成功读取把撤回落到服务器事实」）。本轮加了 expectSeq/有效版本门，但「哪条读取」仍不只限 retry 的那条；retry 自身已严格用本次采纳事实判定。Lead 复核接受。
3. **跨板 await 点**：`refreshAfterSubmit` 内 `refreshBoardFromServer` / `refreshVisibleRange` / `loadIntents` / `loadIntentViews` 每个 await 后都做了「代次 + 板面」双校验，校验通过后才同步调用 `recordNewIntents`（其间无 await）。剩余理论竞态：校验通过后到同步写完成之间没有 await，已排除；`loadIntentViews` 内部另有自己的板面/代次保护（`resetSubmissionStatusForBoard` 会让 `intentViewsSeq` 前进）。
4. `submitRefreshGenByBoard` 只增不清：每个 store 实例每块板一条 number，无泄漏风险。
5. **共享文件并发**：`interactive.ts` 同时被 drafts 队友修改（草稿区）。本次返修交付时 12 文件 74 例 + 16 文件 81 例 + pm032-board-save-isolation 全部绿、`vue-tsc` 绿；若队友之后再改，Lead 最终集成时需重跑一次定向套件与 `vue-tsc`。
6. 未跑全量 `npm test` / CI / 后端 pytest / Rust `cargo test` / 浏览器或安装版验收（按本轮分工由 Lead/其他队友负责）。
7. 红证据的 2 个「护栏用例」（Task 4 的 ④⑤）在基线即为绿——它们是防回归锁定，不算本轮基线红用例；基线红用例为 Task 3 五条 + Task 4 三条。
