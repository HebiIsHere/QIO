# 整合轮独立审查与迁移验证（E）

- **被检查的整合基线：** `c6a03b17423ccda606f72e1e81e3a610fd8e1d43`
  （我的 worktree 分支 `wt/irm-int-e-verify` 在它之上只多了两个文档（契约 `0ed432f`、整合轮记录 `1f36e10`），
  源码与 c6a03b1 逐字相同：`git diff --stat c6a03b1 HEAD` 只有这两个 .md）。
- **来源分支：** 可靠性线 `origin/fix/main-audit-reliability-8fa8452` = `4890870`；
  真实模型线 `origin/feat/interactive-real-model` = `7158152`；共同祖先 `origin/main` = `8fa8452`。
- **冻结契约：** `docs/integration-contract-20261011.md`（接口、编号、所有权、额度以它为准）。
- 本报告只写**我自己跑出来的证据**；引用他人的地方在 §6 单独标注来源与是否复核。

## 0. 结论摘要

1. 三个冲突文件（`docs/status.md`、`IntentStatusPopover.vue`、`stores/interactive.ts`）的重组**基本没有行为丢失**：
   13 处冲突逐条比对后，两侧「必须有」的行为都在整合结果里（§2）。
2. 9 个共同修改文件做行为符号级核对：两侧新增行为符号 **630 个，整合基线缺失 0 个**；
   新增字符串字面量 436 条，只有 2 条不在整合基线里，且两条都已定性为**改写而非丢失**（§3）。
3. **发现 1 处真实的集成回退（F10）**并实测复现：`stores/interactive.ts` 把「提交请求」与「提交后读取」
   又放回同一个 try，POST 明确成功后紧接的读取失败会被 catch 判成「提交结果未知」。
   定向用例 `src/acceptance-state-recovery/f10-submit-refresh.test.ts` 在 c6a03b1 上 **1 failed**（§4）。
   这是两条来源线各自成立、合起来互相否定的集成缺口（Lead 已确认并派给 A 修）。
4. 迁移验证 **43/43 通过**：空库、真实应用库升级、缺对象补偿、重复启动、合成存量库带数据升级、
   静态编号不变式；历史迁移 37/38 与来源分支**逐字一致**，`origin/main → c6a03b1` 的 schema 改动
   只有 1 处追加与 1 行补偿编号（§5）。
5. 本段未覆盖：前端全量 vitest / 全量 pytest / vue-tsc / 全仓评测（额度红线），
   因此**不能**声称「除 F10 外没有别的回退」（§7）。

## 1. 我跑过的命令与真实结果

| # | 命令（worktree 根 = `D:\qio-dev\qio-irm-int-e`） | 真实结果 |
| --- | --- | --- |
| 1 | `git merge-tree --write-tree --name-only 1b9cccd 7158152` | exit 1；冲突文件 3 个：`docs/status.md`、`frontend/src/components/interactive/IntentStatusPopover.vue`、`frontend/src/stores/interactive.ts`（与任务书一致） |
| 2 | `backend\.venv\Scripts\python.exe scripts\irm-int-verify\check_conflict_symbols.py --json scripts\irm-int-verify\out\symbol-check-c6a03b1.json` | 共同文件 9；新增符号 630 / 缺失 0；新增字面量 436 / 缺失 2 |
| 3 | `cd frontend; npm test -- src/acceptance-state-recovery/f10-submit-refresh.test.ts` | 3 tests \| 1 failed：F10 缺口（§4） |
| 4 | `backend\.venv\Scripts\python.exe scripts\irm-int-verify\verify_migrations.py --json scripts\irm-int-verify\out\verify-migrations-c6a03b1.json` | **43/43 通过**（空库 / 真实库 / 补偿 / 重启 / 存量带数据 / 不变式） |
| 5 | `cd backend; uv run --frozen pytest tests/test_interactive_migration_integration.py tests/test_fu_w3_b01_schema.py -q` | `13 passed`（定向两个文件） |
| 6 | `python scripts/check_docs.py` | 「文档一致性检查通过（47 个里程碑条目）」 |
| 7 | `git diff 7158152 c6a03b1 -- backend/src/agent/storage/schema.py` | **无输出（空 diff）**：37/38 与来源分支逐字一致 |
| 8 | `git diff origin/main c6a03b1 -- backend/src/agent/storage/schema.py` | 只有 1 个新增 hunk（`@@ -1254,0 +1255,85 @@`）+ 1 行改动（`COMPENSATION_VERSION = 36` → `38`） |

证据文件（随本提交一起在仓库里）：
`scripts/irm-int-verify/verify_migrations.py`、`scripts/irm-int-verify/check_conflict_symbols.py`、
`scripts/irm-int-verify/README.md`、`scripts/irm-int-verify/out/verify-migrations-c6a03b1.json`、
`scripts/irm-int-verify/out/symbol-check-c6a03b1.json`。

## 2. 三个冲突文件的行为审查（逐条）

方法：先用 `git merge-tree` 定位冲突文件，再用 `git merge-file --diff3` 把三份输入
（base = `8fa8452`、ours = `1b9cccd`、theirs = `7158152`）重放成带冲突标记的文本，
逐段读两侧的意图，再到整合基线源码里定位并判定语义。共 13 段冲突。

### 2.1 `docs/status.md`（1 段冲突）

| 侧 | 该侧在冲突段里的做法 | 出处 |
| --- | --- | --- |
| 可靠性线 | 删除 base 的整段（该线的 P34 写在自己的小节里） | ours = 0 行 |
| 真实模型线 | 新增 51 行「P34 — 互动模式真实模型理解与真实执行」小节 | theirs = 51 行 |

**整合基线的处置与证据**

| 必须有 | 整合基线行号 | 判定 |
| --- | --- | --- |
| 可靠性线 P34 保留（F01—F12） | 1619 `### P34 — 主线可靠性审计修复（F01—F12，2026-10-11）` | ✅ |
| 真实模型项改为编号 P35 | 1659 `### P35 — 互动模式真实模型理解与真实执行（2026-10-11 第九轮，整合编号）` | ✅（编号改写，契约 §3 要求） |
| P35 正文与来源分支逐字一致 | 1661—1707 与 theirs 的 51 行逐行相同（唯一差异是标题行与交叉引用） | ✅ |
| 两条线的交叉引用 | 1654—1655（P34 → 见 P35）与 1715（尚未完成 → 见 P35） | ✅ |
| 「尚无真实模型人工验收」的限制必须保留 | 1697—1699（已知限制第一条）、1715—1719（尚未完成第一段，含「不要把模拟验证说成真实模型验收」） | ✅ |
| 三处里程碑状态一致 | `python scripts/check_docs.py` → 通过（47 个里程碑条目） | ✅ |

**结论：无行为丢失。** 唯一差异是条目编号 P34 → P35 以及随之改写的三处交叉引用（预期行为）。

### 2.2 `frontend/src/components/interactive/IntentStatusPopover.vue`（1 段冲突）

| 侧 | 该侧在冲突段里的做法 | 出处 |
| --- | --- | --- |
| 可靠性线 | 空态改成三态：`store.intentsLoadState === "error"` 时说「清单读取失败：这里的空白不是『没有任务』…」 | ours 行 387 |
| 真实模型线 | 新增 `store.executionError` 的执行记录读取失败块；空态写成「现在没有需要管理的任务。」 | theirs 行 498 / 502 |

**整合基线的处置与证据**

| 必须有 | 整合基线行号 | 判定 |
| --- | --- | --- |
| 执行记录读取失败提示（真实模型线） | 509—511 `data-im="tasks-execution-error"` | ✅ |
| 清单刷新失败提示 + **只读**重试入口（F11，非冲突区） | 517—529 `data-im="tasks-refresh-failed"` / `tasks-refresh-retry`，文案说明「下面是最后一次成功读到的任务，不代表当前状态」 | ✅ |
| 空白不等于「没有任务」 | 531 三元表达式，error 分支保留可靠性线的原文 | ✅ |
| 真实执行进度 / 暂停 / 继续 / 取消 / 终态 | 详见 `SubmitCluster.vue` 与弹层任务行（符号核对 0 缺失） | ✅ |

**结论：无行为丢失**，且整合结果比任何单侧都全（同时有执行错误块与清单读取失败三态）。
唯一差异：非错误分支的兜底文案取真实模型线「现在没有需要管理的任务。」（可靠性线原文为
「现在没有执行中或已暂停的任务。」）——纯文案，语义等价。

### 2.3 `frontend/src/stores/interactive.ts`（13 段冲突）

| # | 冲突段内容 | 两侧做法 | 整合基线 | 判定 |
| --- | --- | --- | --- | --- |
| 1 | `SubmitStatus` 联合类型 | rel 新增 `\"unknown\"`；irm 新增 `SubmissionStage` | 92—100（SubmitStatus 含 `unknown`）+ 109—120（SubmissionStage 全量） | ✅ 并集 |
| 2 | 理解 / 执行的状态与计算属性（irm 92 行） | irm 新增，rel 无改动 | 212—260（understanding / understandings / understandingStatus / acceptedUnderstandingId / executions / understandingHistory / understandingIntents / executionsByIntent / executionStatus / liveExecutions / submissionStage） | ✅ 全在 |
| 3 | `loadIntents` 里合并列表带回来的执行记录 | irm 新增 | 1560—1561 `if (payload.executions?.length) mergeExecutions(payload.executions)` | ✅ |
| 4 | 提交开始时复位 `submitNote` | irm 新增 | 2593 | ✅ |
| 5 | `const sentAt = Date.now();` | irm 新增（时间兜底用），rel 无 | 2623（注释已改写为「不再作为『是不是这次提交』的判据」） | ✅ 变量保留 |
| 6 | 提交成功路径（**双方都改**） | rel：`applyCheckedCleared` → 只调 `refreshAfterSubmit`；irm：`delivery` 判定 + 直接刷新 + 受理/理解分支 | 2636—2689：`acceptedByReceipt` / `delivered === false` → failed、`submitStatus`、`applyCheckedCleared`（2659）、`acceptedUnderstandingId` / `understandingStatus = "accepted"` / `understanding.value = null`（2666—2683）、`refreshAfterSubmit`（2688） | ⚠️ 两侧语义都在，但**顺序组合出缺口**，见 §4 |
| 7 | 回执不明时的时间兜底认领（irm） | irm：`confirmAcceptanceAfterUnclear(sentAt)` → 命中就改判 `succeeded` | **未并入**：2690—2703 只保留 rel 的「unknown + 保留改动与勾选」；函数定义仍在 3272—3290，但**全文件无调用点**（死代码） | ⚠️ 见下方说明 |
| 8 | `approve` 防重复锁 | rel：`ensureFreshIntents` 前置核验；irm：`decideInFlight` 锁 + `try/finally` + `adoptExecution` | 2806—2826：锁先取（2814）→ 核验（2816，失败直接返回不放行）→ `adoptExecution`（2820）→ `finally` 释放（2824） | ✅ 两侧兼得，语义更强 |
| 9 | `reject` 同上 | 同上 | 2828—2844 | ✅ |
| 10 | `decideBatch` 同上 | 同上 | 2846—2865（`batchDecideInFlight` 2851 / 核验 2853 / 批量采纳 2859 / finally 2863） | ✅ |
| 11 | store 导出面（irm 30 项） | irm 新增导出，rel 无改动 | 3666—3700 全部导出（符号核对 0 缺失，含 `understanding*`、`executions*`、`acceptedUnderstandingId`、`startUnderstandingPolling`、`latestExecution`、`pauseExecution` / `resumeExecution` / `cancelExecution`） | ✅ |

**第 7 段的说明（重点）：** 真实模型线原来的「回执丢失 → 按时间接近认领」逻辑**没有并入整合基线**。
这与冻结契约 §1.3 的目标一致（「删掉按 createdAt 接近 + 排除上次 submissionId 的认领逻辑」，
改为 `clientRequestId` 精确查证），也与 §3「未拿到回执 = unknown，绝不自动重复提交」一致；
但**目标路径当前还不存在**：全仓 grep 无 `client_request_id` 的板面提交幂等实现
（`backend/src/agent/api/interactive_store.py` 只有 `requestedVisible` / `baseStateVersion` / `confirmedCheckId`），
`confirmAcceptanceAfterUnclear` 是**不可达的死代码**。这是 A/B 本轮要补的契约缺口，
不是冲突重组导致的回退；第二段我会独立验收「有身份 → 精确查证 → 命中才认受理」。

## 3. 九个共同修改文件的行为符号级核对

「共同修改文件」= 两条来源分支相对 `origin/main` 都改过的文件（我独立算出的交集，恰好 9 个）。
脚本 `scripts/irm-int-verify/check_conflict_symbols.py` 对每一侧取「该侧引入、base 没有」的
标识符与字符串字面量，再在整合基线里核对（`out/symbol-check-c6a03b1.json` 有逐符号行号）。

| 文件 | rel 新增符号 | irm 新增符号 | 缺失符号 | 缺失字面量 |
| --- | --- | --- | --- | --- |
| `backend/src/agent/api/interactive_store.py` | 7 | 91 | 0 | 0 |
| `docs/status.md` | 21 | 20 | 0 | 0（编号改写见 §2.1） |
| `frontend/src/components/interactive/ChatDock.vue` | 5 | 71 | 0 | 0 |
| `frontend/src/components/interactive/IntentStatusPopover.vue` | 9 | 48 | 0 | 1（纯文案，见 §2.2） |
| `frontend/src/components/interactive/SubmitCluster.vue` | 23 | 57 | 0 | 0 |
| `frontend/src/interactive/submission.ts` | 0 | 3 | 0 | 0 |
| `frontend/src/interactive/types.ts` | 1 | 79 | 0 | 0 |
| `frontend/src/services/interactive.ts` | 13 | 20 | 0 | 0 |
| `frontend/src/stores/interactive.ts` | 49 | 113 | 0 | 1（时间兜底文案，见 §2.3 第 7 段） |
| **合计** | **128** | **502** | **0** | **2** |

Lead 点名的关键符号，逐一在整合基线里定位（行号）：

| 行为符号 | 整合基线行号 | 行为符号 | 整合基线行号 |
| --- | --- | --- | --- |
| `submitResultUnknown` | 185 | `applyCheckedCleared` | 2767（调用 2659） |
| `submitRefreshState` / `submitRefreshError` | 186 / 187 | `checkedAtSubmit` | 2613 |
| `retrySubmitRefresh` | 2743 | `confirmAcceptanceAfterUnclear` | 3272（**无调用点**） |
| `refreshAfterSubmit` | 2721（调用 2688） | `acceptedUnderstandingId` | 223 |
| `ensureFreshIntents` | 1578（审批 2816 / 2835 / 2853） | `startUnderstandingPolling` | 3371 |
| `decideInFlight` / `batchDecideInFlight` | 2803 / 2804 | `adoptExecution` / `mergeExecutions` | 3561 / 3526 |
| `intentsLoadState` / `intentsLoadError` | 200 / 201 | | |

`interactive_store.py`：`requestedVisible` 333—335、`baseStateVersion` 341—343、`confirmedCheckId` 351、
理解路由 591（GET）/ 611（retry）/ 674（cancel）全部在位。
`ChatDock.vue`：未决发送不回填原文重发 428—435（`session.pendingSendConfirm`），
材料 / 附件入口与 `pendingApprovals` 578—581 在位。
`SubmitCluster.vue`：unknown 状态块 396—407（含只读查证入口 `submit-unknown-refresh`）、
删除冲突二选一 435—466（`board-removal-keep-deleted` / `board-removal-restore-server`）、
真实理解与执行展示 324—380、518—529 在位。

**边界（必须说清）：** 这是**名字级**核对——它证明「来源线引入的符号没有整条消失」，
**不证明语义没有变化**。语义结论来自 §2 的逐段阅读与 §4 的实测。

## 4. 集成缺口：F10「提交成功被后续刷新失败改判成结果未知」（实测复现）

### 4.1 现象与复现

    cd frontend
    npm test -- src/acceptance-state-recovery/f10-submit-refresh.test.ts

真实输出（c6a03b1）：

    ❯ src/acceptance-state-recovery/f10-submit-refresh.test.ts (3 tests | 1 failed) 19ms
      × POST 已成功、后续 GET 失败：保留成功事实，并只给「重试刷新」
        AssertionError: 提交请求本身已经成功，必须返回成功结果: expected null not to be null
         ❯ src/acceptance-state-recovery/f10-submit-refresh.test.ts:80:47
      ✓ 服务端明确拒绝（4xx）：仍然是提交失败，且不进入刷新失败状态
      ✓ 没有回执（超时 / 断线）：单独表达「提交结果未知」，既不假定成功也不假定失败
    Test Files  1 failed (1)      Tests  1 failed | 2 passed (3)

即：POST 已经明确成功，仅因为紧随其后的板面 GET 失败，`submit()` 返回 `null`、
`submitStatus` 变成 `unknown`——契约 §3 第一条（提交请求与提交后读取分开）被破坏。

### 4.2 根因（c6a03b1 行号）

1. `frontend/src/stores/interactive.ts` **2661—2663** 在 POST 的同一个 `try` 里直接
   `await refreshBoardFromServer(); await refreshVisibleRange(); await loadIntents();`，
   **2665** 又直接 `recordNewIntents(...)`。
2. `refreshBoardFromServer()`（1370—1389）内部直接 `await api.fetchBoardState(...)`，**会抛错**
   （`loadIntents()` 自己吞错，F11 未回退：1563—1568）。
3. **2690** 的 `catch (err)` 用 `submitReceiptUnknown(err)`（2572—2576）判定：错误没有 `status`
   → `true` → **2699** `submitStatus.value = "unknown"`、2700 `submitResultUnknown = true`、2701 文案「提交结果未知」。
4. 可靠性线正确的包装 **2688** `await refreshAfterSubmit(...)`（2721—2737，自带 try/catch、
   失败只写 `submitRefreshError`）排在后面，**已经来不及**。

附带同一处组合问题：成功路径会刷新两遍（2661—2663 与 2725—2727）、
`recordNewIntents` 执行两次（2665 与 2729，同一个 batchKey），第二次是冗余副作用。

### 4.3 来源分支对照（为什么是「合起来才坏」）

| 版本 | 提交成功后的读取 | 结果 |
| --- | --- | --- |
| 真实模型线 `7158152` | 2411—2416：直接 `await refreshBoardFromServer()` / `refreshVisibleRange()` / `loadIntents()` / `recordNewIntents()`，**没有** `refreshAfterSubmit` | 该线自己内部一致（读取失败本来就会被 catch 处理成未知）——但语义不符合 F10 目标 |
| 可靠性线 `4890870` | 2518—2533：只调 `refreshAfterSubmit(...)`（2542—2558 自带 try/catch，失败只记 `submitRefreshError`） | F10 成立，定向用例通过 |
| 整合 `c6a03b1` | 2661—2663（来自真实模型线）**与** 2688（来自可靠性线）**并存** | 读取失败被误判成「结果未知」，且刷新 / 批次记录各做两次 |

### 4.4 处置与第二段的独立复验

Lead 已确认并派给 A：删掉 2661—2665 这四行（读取完全交给 2688 的 `refreshAfterSubmit`），
保留 2666—2683 的受理与理解分支（它们只依赖 `result`，不依赖那四行）。

第二段我会自己复跑并**自己下断言**（不引用 A 的结论）：
1. `npm test -- src/acceptance-state-recovery/f10-submit-refresh.test.ts` 全绿；
2. 自己写一条反例断言：成功提交路径上 `api.fetchBoardState` 只被调用**一次**、
   `recordIntentBatch` 只被调用**一次**（用 spy 计数，而不是看文案）；
3. 用同一组断言覆盖「读取失败 → `submitRefreshState='error'` 且 `submitStatus='succeeded'`」。

## 5. 独立迁移验证

### 5.1 历史迁移没有被改写（逐字证据）

- `git diff 7158152 c6a03b1 -- backend/src/agent/storage/schema.py` → **空**：
  整合后的 37（`_INTERACTIVE_REAL_MODEL_DDL` + 6 个 `board_intents` 新列）与 38（补偿入口）
  与真实模型线交付的文本**逐字一致**，没有被整合改动。
- `git diff origin/main c6a03b1 -- backend/src/agent/storage/schema.py` → 只有
  `@@ -1254,0 +1255,85 @@`（追加 37/38 定义）与 `COMPENSATION_VERSION = 36` → `38` 一行改动。
  **1254 行之前（迁移 1—36）一个字节都没动**。
- `git diff origin/main c6a03b1 -- backend/src/agent/storage/migrate.py` → 只在 `REQUIRED_OBJECTS`
  追加 `board_understanding` / `board_executions`（+5 索引、+6 列），没有删除或改写既有清单项。

### 5.2 运行验证（`scripts/irm-int-verify/verify_migrations.py`，43/43 通过）

| 部分 | 场景 | 关键断言与实测 |
| --- | --- | --- |
| A | 空库 | 版本 38（= `SCHEMA_VERSION`）；`missing_objects` 为空；`board_understanding` / `board_executions` / 4 个索引 / 6 个新列都在；版本行 35 条无重复 |
| B | **真实应用库升级**：`C:\Users\zxy\AppData\Roaming\qio\app.db`（v22、22 张表、4 行数据） | 22 → 38；对象齐全；既有表行数一行不变；新增 14 张表全部能在未应用的迁移语句里找到出处；真实模型两张表存在 |
| C | 缺对象补偿 | 删 `board_understanding` 表 + `idx_board_executions_board` 索引 + `board_intents.goal` 列 → 按对象逐项检出 → 重放补偿后全部补齐、复核为空、补回的表可用、**可重复触发** |
| D | 重复启动 | 同一库连续启动 3 次：版本稳定 [38,38,38]、版本行 35 条不重复、无缺对象 |
| E | 合成存量库（停在 36、带业务数据） | 升级 36 → 38 后：草稿正文逐字不变、板面 seq=7 与正文不变、旧意图行保留且 6 个新列为默认值（`''` / `[]`）、提交记录保留 |
| I | 静态不变式 | `SCHEMA_VERSION` = 最大迁移号；补偿编号在 MIGRATIONS 里；**补偿 == 上一条完整迁移的全部语句 + 本轮新增迁移**（38 == 36 + 37）；真实模型对象首现于迁移 37；历史迁移（<37）不含真实模型对象；`REQUIRED_OBJECTS` 每一项都能在补偿迁移里找到对应的建对象语句 |

### 5.3 既有定向测试（佐证，不替代 5.2）

    cd backend
    uv run --frozen pytest tests/test_interactive_migration_integration.py tests/test_fu_w3_b01_schema.py -q
    → 13 passed

### 5.4 诚实边界

- 补偿是**按名字**的校验：同名不同形（形状损坏）判不出来 —— 这是 `migrate.py` 文档里写明的既有边界，不是本轮引入。
- 我用的「真实库」是本机 `%APPDATA%\qio\app.db`（v22，22 张表，4 行），它是真实的应用库但**不是生产库**；
  另加了一个停在 36 的合成存量库来覆盖「已并入 main 的互动对象 + 本轮新增」这条路径。
- 迁移 39（`client_request_id` 列 + 唯一索引）与补偿 40 是本轮 B 的新增，**本段基线还没有**；
  第二段我会用同一个脚本在新候选 SHA 上复跑（脚本按 `SCHEMA_VERSION` / `COMPENSATION_VERSION` 动态读，
  不变式 I3 会自动核对 `40 == 38 + 39`）。

## 6. 证据分级

**我亲自跑出 / 亲自读出的（可复现，命令见 §1）：**
冲突文件定位（merge-tree）、13 段冲突重放与逐段阅读、9 文件符号级核对（脚本 + JSON）、
F10 定向用例失败、迁移 43/43、后端迁移定向 13 passed、`check_docs.py`、四处 git diff 逐字比对、
`client_request_id` 全仓现状 grep、`confirmAcceptanceAfterUnclear` 无调用点的 grep。

**引用他人的（标注来源与复核情况）：**

| 内容 | 来源 | 我的复核 |
| --- | --- | --- |
| 「三条来源分支文件的并集无遗漏（rel 76 / irm 63 / 整合 130，交集 9）」 | Lead | 我用 `git diff --name-only` 独立算出同一组数字与同一份 9 文件交集 ✅ |
| F10 集成缺口的定性（两条线各自成立、合起来互相否定） | Lead（在我预警之后） | 我独立读过 IRM 2411—2416 与 REL 2518—2533 / 2542—2558，结论一致；Lead 引用 IRM「2403-2416」是同一段的更大范围 ✅ |
| 「整合基线 c6a03b1 三处冲突」 | 任务书 / 冻结契约 | 我用 `git merge-tree` 独立复现同一组三个文件 ✅ |
| A/B/C/D 各线的实现结论 | 各子智能体自述 | **本段未采信、未引用**；第二段在集成候选上自己跑 |

## 7. 未覆盖项与不确定项

1. **不做全量**：本段没有跑前端全量 vitest、后端全量 pytest、`npx vue-tsc --noEmit`、全仓评测、CI。
   因此本报告**不能**被读成「整合基线整体全绿」；它只覆盖上面列出的定向项。
2. **F10 之外的集成回退风险**：符号级核对证明「符号没有整条消失」，但语义回退只有被测到的才会暴露。
   已发现 1 处（F10）。同类「两条线各自动作叠加」的风险点还有：提交成功路径的刷新次数、
   `recordNewIntents` 的批次记录、`settleAfterIntentChange` 与 `adoptExecution` 的先后；第二段会针对性看。
3. **`confirmAcceptanceAfterUnclear` 是死代码**，且契约 §1.3 的精确查证链路（`clientRequestId`）在基线还不存在；
   本段无法验证「精确匹配才认受理」，只能在第二段验收。
4. **迁移**：形状级损坏（同名不同形）不在校验范围；真实库只有 4 行数据，数据保全的强度靠 §5.2 E 的合成库补足。
5. 迁移 39/40、`fetchAcceptance`、`submission.acceptance_lookup`、理解后台任务统一生命周期
   （`AppContext.background` 注册、`understanding_stopped` / `understanding_unfinished` 阶段）
   在 c6a03b1 都还没有，属本轮 B/C 的工作，本段不给结论。

## 8. 第二段待办（等 Lead 通知后在新候选 SHA 上执行）

1. 复跑本报告 §1 的全部定向项，并复核 A 对 F10 的修复（含我自己写的「只刷新一次 / 只记一次批次」断言）。
2. 关键闭环：提交 → 受理 → 材料读取 → 理解 → 意图 → 审批 → 真实执行 → 结果卡（定向用例 + 我自己构造的断言）。
3. 契约红线的定向验证：未获准材料不进上下文、聊天与板面提交分离、模型操作是用户可做操作的子集、
   两窗口草稿互不覆盖、本地删除不被无关服务器更新复活、回执丢失、两窗口并发、时钟偏差、重启查证、
   同身份重试不认错提交且不重复调用模型、旧理解不覆盖新提交、任务读取失败不显示「没有任务」、
   重复单项/批量审批不重复执行、核验失败不放行。
4. 迁移：在新 SHA 上复跑 `verify_migrations.py`（应看到 39/40 与 `client_request_id` 唯一索引）。
5. 逐项给出「我跑的命令 / 真实输出 / 结论」，区分亲自证据与引用证据。
---

# 第二段：集成候选 `1105c3f` 上的定向验收（E 亲自跑）

## 9.1 候选、方式与边界

- **候选 SHA：** `1105c3f`（Lead 整合 worktree `D:\qio-dev\qio-irm-int` 的 HEAD）。
  我把这个提交 checkout 到**我自己的**临时分支 `wt/irm-int-e-cand` 上跑，跑完删除该临时分支；
  Lead 的 worktree 全程只读，没有改。
- 我的验证脚本以**未跟踪文件**叠加在候选之上（只加 `scripts/irm-int-verify/**`，**没有改任何产品代码**），
  所以结果对候选本身成立。
- **活体后端**（优先项 1 用）：在候选代码上启动真进程 ——
  `QIO_DATA_DIR=<临时目录> QIO_DEV_INSECURE=1 QIO_DISABLE_DB_CHECK=1 backend/.venv/Scripts/python.exe -m uvicorn agent.main:create_app --factory --port 8848`，
  用真 HTTP 驱动，跑完 kill（临时库，不碰用户数据）。
- 本段所有证据文件：`scripts/irm-int-verify/out/*-1105c3f.*`（见 9.6）。

## 9.2 优先项 1：真实后端 × 真实前端**字段名**端到端（活体，不是夹具）

工具：`scripts/irm-int-verify/probe_wire.py`（抓服务端**原样 JSON**）+
`scripts/irm-int-verify/probes/e-wire-identity.probe.test.ts`（把真实 `services/interactive.ts` 与
`stores/interactive.ts` 指向活体后端跑断言）。

命令与结果：

    # 1) 真后端 + 真 HTTP：建板面 → 保存状态 → 带 clientRequestId 提交 → 同身份重试 → 精确查证 → 未用身份 → 同身份不同内容
    ./backend/.venv/Scripts/python.exe scripts/irm-int-verify/probe_wire.py --base http://127.0.0.1:8848 \
        --json scripts/irm-int-verify/out/wire-1105c3f.json
    [1] POST /boards -> 200   [2] PUT /state -> 200   [3] POST /submissions -> 200
    [4] 同身份重试 -> 200 replayed=True   [5] GET /acceptance（命中）-> 200
    [6] GET /acceptance（没这个身份）-> 404   [7] 同身份不同内容 -> 409

    # 2) 前端：VITE_QIO_BACKEND_URL 指向活体后端，真实 services + store 打真后端
    cd frontend; $env:VITE_QIO_BACKEND_URL="http://127.0.0.1:8848"
    npm test -- src/__irm_int_e_probe__/e-wire-identity.probe.test.ts
    [e-wire] mode=live  base=http://127.0.0.1:8848
    ✓ 活体：真实 services + 真后端（提交 → 查证 → 换身份 404 → 同身份不同内容 409） 340ms
    ✓ 活体：store 级查证 —— 命中恢复受理事实，换身份保持 unknown              381ms
    ✓ 夹具：抓下来的服务端原样 JSON 同样能通过同一组判定
    Test Files 1 passed (1)   Tests 3 passed (3)

**服务端原样 JSON（命中查证）** —— 字段名与冻结契约 §1.2 逐项对上：

    {"acceptance": {
       "clientRequestId": "creq_e_wire_0001",
       "submissionId": "sub_61c5be39bf19",
       "status": "succeeded",
       "delivery": {"delivered": true, "status": "accepted",
                    "understandingId": "u_b83d93100930", "reason": "…", "detail": "…",
                    "background": {...}, "marking": {...}, "note": "E 字段名接缝"},
       "understandingId": "u_b83d93100930",
       "understandingStatus": "failed",
       "baseStateVersion": 1,
       "note": "E 字段名接缝",
       "createdAt": "2026-10-11T14:06:12.409280+00:00",
       "replayed": false}}

未命中：`404 ` + `{"acceptance": null, "reason": "没有这个提交请求身份的受理记录（creq_e_never_used_9999）：结果未知，不推断成功；请保留原内容与勾选，不要盲目重发。"}`

活体断言（前端侧，全部通过）：

| 断言 | 结果 |
| --- | --- |
| 提交响应里 `submission.clientRequestId` 与发出的身份**逐字相同** | ✅ |
| `delivery.status === "accepted"` 且 `delivery.understandingId` 非空 | ✅ |
| `acceptance.baseStateVersion` 是 **number** 且等于本次 seq | ✅ |
| `acceptance.note` 是字符串（本次为空串） | ✅ |
| `acceptance.submissionId` 非空且等于提交响应里的 id | ✅ |
| `acceptanceMatchesIdentity(identity, acceptance)` 判为「本次提交」 | ✅ |
| 换一个 clientRequestId 查同一板面 → 404 → `acceptance:null` → 判定为「不是本次」 | ✅ |
| 同身份不同内容 → 409 `request_identity_conflict`，`isRequestIdentityConflict(err)` 为真 | ✅ |
| **store 级**：命中后 `submitStatus="succeeded"`、`lastAcceptance.submissionId` 正确、`submitNote` 非空 | ✅ |
| **store 级**：查不到时 `submitStatus` **保持 `unknown`**、`submitResultUnknown` 仍为 true、`acceptanceLookupNote` 如实说明「还没确认」 | ✅ |

**诚实说明：** 这次活体后端里理解最终是 `understandingStatus: "failed"` —— 本环境没有可用的授权模型凭据，
理解请求确实建立了、也确实尝试执行并如实记成失败。**这不等于真实模型验收完成**（见 9.7）。
夹具模式（`mode=fixture`）用抓下来的原样响应体跑同一组判定，1 passed / 2 skipped，结果是夹具级证据。

## 9.3 优先项 2：迁移与提交身份（候选上的独立复跑）

    ./backend/.venv/Scripts/python.exe scripts/irm-int-verify/verify_migrations.py --json out/verify-migrations-1105c3f.json
    → 43/43 通过（SCHEMA_VERSION=40 / COMPENSATION_VERSION=40 / 迁移 37 条）

| 关键点 | 实测 |
| --- | --- |
| 空库 | 版本 40、REQUIRED_OBJECTS 全齐、版本行 37 条无重复 |
| 真实应用库（v22、22 张表、4 行数据） | 22 → 40，既有表行数一行不变，新增表都能在未应用迁移里找到出处 |
| 缺对象补偿（删表 / 索引 / 列） | 逐项检出 → 补偿补齐 → 复核为空 → 可重复触发 |
| 重复启动 3 次 | 版本稳定 [40,40,40]，版本行不重复 |
| 合成存量库（停在 36、带数据） | 升级后草稿 / 板面 / 提交 / 意图逐字不变，新列取默认值 |
| **I3 补偿不变式** | `MIGRATIONS[40] == MIGRATIONS[38] + MIGRATIONS[39]` ✅ |
| **I4/I5 历史迁移** | 真实模型对象仍首现于迁移 37；<37 的迁移不含它们 ✅ |
| **I6 清单覆盖** | REQUIRED_OBJECTS 每一项（含 `board_submissions.client_request_id` 与 `idx_board_submissions_client_request`）都能在补偿迁移里找到建对象语句 ✅ |

    ./backend/.venv/Scripts/python.exe scripts/irm-int-verify/verify_submission_identity.py --json out/verify-identity-1105c3f.json
    → 33/33 通过

要点：S1.5 同身份重试用**原 baseStateVersion** 拿到 200 与 `replayed=true`（身份判定排在 stale_state 之前，
我在第一段提的口径成立）；S2 同身份不同 note → 409 `request_identity_conflict`；S3 同身份不同版本 → 409；
S5「最近一条」诱饵与 S6「时间接近」诱饵都返回 404 + `acceptance:null`；S7 三个身份三条提交、无重复行。

## 9.4 优先项 3：F10 spy 复验（我自己的断言，不看 A 的结论）

    cd frontend
    npm test -- src/__irm_int_e_probe__/e-submit-read-once.probe.test.ts src/acceptance-state-recovery/f10-submit-refresh.test.ts
    ✓ f10-submit-refresh.test.ts (3 tests)
    ✓ e-submit-read-once.probe.test.ts (2 tests)
    Test Files 2 passed (2)   Tests 5 passed (5)

我的探针实测计数（同一份探针在基线 c6a03b1 上是 `{board:2, intents:2, batches:2}` / 2 failed，见 §4）：

| 计数（提交成功后增量） | 候选实测 | 期望 |
| --- | --- | --- |
| `fetchBoardState` | 1 | 1 |
| `fetchIntents` | 1 | 1 |
| `recordIntentBatch` | 1 | 1 |
| `fetchVisibleRange` | 2 | 2（其中 1 次是保存成功路径里 fire-and-forget 的那句，与提交后刷新无关） |
| 读取失败时的提交事实 | `submitStatus="succeeded"`、`submitRefreshState="error"`、`submitError=null` | 同左 |

## 9.5 闭环与其他定向

    cd backend
    uv run --frozen pytest -q --junitxml=../scripts/irm-int-verify/out/junit-backend-1105c3f.xml \
      tests/test_interactive_real_model.py tests/test_interactive_execution.py tests/test_interactive_understanding.py \
      tests/test_interactive_store_understanding.py tests/test_interactive_materials.py tests/test_interactive_submission.py \
      tests/test_interactive_migration_integration.py tests/test_fu_w3_b01_schema.py tests/test_audit_f04_acceptance_receipt.py
    → pytest exit=0；JUnit：tests=135 failures=0 errors=0 skipped=0（36.7s）

其中闭环主用例 `test_interactive_real_model.py::test_real_path_submission_understanding_approval_execution`
（提交 → 受理 → 材料读取 → 理解 → 意图 → 审批 → 真实执行 → 结果卡与来源引用）通过；
另有 `test_invisible_never_reaches_model_context`（未获准材料不进上下文）、
`test_duplicate_submission_does_not_call_model_or_create_tasks`、
`test_repeated_approval_does_not_duplicate_execution_or_result`（重复审批不重复执行）通过。

    cd frontend
    npm test -- src/acceptance-state-recovery/f11-intents-load-failure.test.ts \
                src/interactive/__tests__/irm-d-understanding-store.test.ts \
                src/__irm_int_e_probe__/e-wire-identity.probe.test.ts
    → Test Files 3 passed (3)；Tests 13 passed | 2 skipped（跳过的是夹具模式下不该跑的活体用例）

## 9.6 证据文件（`scripts/irm-int-verify/` 下，均为最终版）

| 文件 | 内容 |
| --- | --- |
| `out/wire-1105c3f.json` | 活体后端七个步骤的**原样响应体**（含验收形状） |
| `out/wire-probe-live-1105c3f.txt` | 活体接缝探针的完整输出（mode=live，3 passed） |
| `out/verify-migrations-1105c3f.json` | 迁移验证 43/43（逐项 check） |
| `out/verify-identity-1105c3f.json` | 提交身份链路 33/33（逐项 check） |
| `out/probe-read-once-1105c3f.txt` | F10 + 只读一次探针输出（5 passed） |
| `out/probe-read-once-baseline-c6a03b1.txt` | 同一探针在**基线**上的失败输出（board/intents/batches 各 2 次） |
| `out/backend-targeted-1105c3f.txt`、`out/junit-backend-1105c3f.xml` | 后端定向 135 passed 的原始输出与机器可读结果 |
| `out/frontend-targeted-1105c3f.txt` | 前端定向 13 passed | 2 skipped |
| `out/symbol-check-c6a03b1.json`、`out/verify-migrations-c6a03b1.json`、`out/verify-identity-baseline-c6a03b1.json` | 第一段（基线）证据，保留作对照 |

脚本：`verify_migrations.py`、`verify_submission_identity.py`、`check_conflict_symbols.py`、
`probe_wire.py`、`run_stage2.py`、`probes/e-submit-read-once.probe.test.ts`、
`probes/e-wire-identity.probe.test.ts`、`acceptance-map.md`、`README.md`。

## 9.7 未覆盖 / 诚实清单（不得被读成「全部完成」）

1. **真实模型人工验收仍未完成**：活体后端这次理解结果是 `failed`（本环境没有可用的已授权模型凭据）。
   接口接通、服务端幂等、字段名接缝都验证了，**但真实模型的判断质量没有验证**，不得说成真实模型验收完成。
2. **安装包端到端（install-e2e）未跑**；**跨进程 / 跨窗口**（两个浏览器窗口、桌面壳多窗口）未跑，
   两窗口相关结论目前只来自单进程内的定向用例。
3. **全量没跑**：本段只跑定向（后端 9 个文件 135 用例、前端 4 个文件 + 2 个探针），
   唯一一轮本地全量与 CI 留给 Lead 在冻结 SHA 上执行。
4. 活体测试用 `QIO_DEV_INSECURE=1`（无会话令牌）跑：**认证 / 令牌失效路径不在本段覆盖范围**。
5. 我的探针是**临时复制进** `frontend/src/__irm_int_e_probe__/` 跑的，跑完立即删除；
   前端源码树没有留下任何探针文件（`git status` 干净）。
6. 迁移验证里的「真实应用库」是本机 `%APPDATA%\qio\app.db`（v22、22 张表、4 行），不是生产库；
   数据保全的强度由 9.3 的合成存量库补足。形状级损坏（同名不同形）仍不在校验范围。
