# R7 store/组件回归测试（预判乱序、确认不串板、撤回重试）— task-3 报告

- 任务：task-3（spec-flow）「R7 store/组件回归测试（预判乱序、确认不串板、撤回重试）」
- 工作树：`D:\qio-dev\pm-qio032-integration`，分支 `fix/qio032-46-board-state`
- 开工基线 HEAD：`0dbc387f1ecdc2dfa21f658ce16dbe07a6fa9b3d`（与 origin 同 SHA）
- 交付文件（只新增测试，**未改产品代码**）：
  - `frontend/src/stores/__tests__/q46-r7-impact-ownership.test.ts`（7 例）
  - `frontend/src/stores/__tests__/q46-r7-cancel-recovery.test.ts`（4 例）
- 红基线运行方式：**独立干净 worktree** `D:\qio-dev\_r7-baseline-head`（`git worktree add --detach 0dbc387f`，
  `frontend/node_modules` 用 junction 复用），只把两个新测试文件拷进去。
  原因：共享工作树里 `frontend/src/stores/interactive.ts`、`ImpactConfirmLayer.vue` 正被其他成员并发修改
  （交付时 `git diff --stat` 显示 +725/−125），直接在主工作树跑会把「别人的在途修复」当成基线。
  **该 worktree 内产品代码零改动**（`git status` 只有两个新测试文件）。
- 命令（两个目录同一条）：`cd frontend; npx vitest run src/stores/__tests__/q46-r7-impact-ownership.test.ts src/stores/__tests__/q46-r7-cancel-recovery.test.ts`
- 结果对照：

| 运行对象 | 文件 | 用例 | 结果 | 原始输出 |
|---|---|---|---|---|
| **基线 0dbc387f**（干净 worktree） | 2 | 11 | **4 failed / 7 passed** | `baseline-head-0dbc387f.log` |
| 修复后工作树（Lead 通知已落地，未提交） | 2 | 11 | **11 passed**（3 次一致） | `green-r7-flow.log`、`current-worktree.log`、`current-worktree-rerun.log` |
| 基线 0dbc387f：既有 f1 撤回重试用例 | 1 | 2 | **1 failed**（第二例） | `baseline-f1-0dbc387f.log` |
| 共享工作树：既有 f1 撤回重试用例 | 1 | 2 | **2 passed** | `current-f1.log` |

## 一、逐条红证据（任务书编号 → 失败断言原文，基线 0dbc387f）

### 二.4 预判乱序（`q46-r7-impact-ownership.test.ts`）

| # | 用例 | 基线 | 失败断言（原文，节选） |
|---|---|---|---|
| 二.4-a | 旧预判先成功返回：旧响应被丢弃，确认仍对应最新候选 | PASS | — |
| 二.4-b | 新预判先成功返回、旧预判迟到：旧响应不覆盖较新的确认 | PASS | — |
| 二.4-c | 旧预判先失败返回：旧失败不写成当前预判失败，最新候选仍可确认 | PASS | — |
| 二.4-d | 409 恢复中的重新预判：旧响应迟到不许清理新恢复计划（保存必须继续） | **FAIL** | `AssertionError: 旧响应清掉了新恢复计划：重新预判返回「可以保存」后保存没有继续: expected 1 to be 2` |

### 二.5 确认结果只在 B 展示（`q46-r7-impact-ownership.test.ts`）

| # | 用例 | 基线 | 失败断言（原文，节选） |
|---|---|---|---|
| 二.5-a | store 层：B 点确认后切到 A，B 的保存失败迟到只写 B | PASS | — |
| 二.5-b | 组件层：A 上不渲染 B 的失败文案与重试按钮；B 上可见，点重试只向 B 发请求 | **FAIL** | `AssertionError: A 的页面上渲染出了 B 的保存失败文案（确认结果没有按板面归属）: expected '…null 需求…这次改动没有保存成功B 板保存失败：服务端 500重新核实并保存…' not to contain 'B 板保存失败'` |
| 二.5-c | 同板：较早的保存失败结果不覆盖较新的确认操作（重试成功后显示已保存） | PASS | — |

### 三.6 撤回重试响应式（`q46-r7-cancel-recovery.test.ts`）

| # | 用例 | 基线 | 失败断言（原文，节选） |
|---|---|---|---|
| 三.6-a | 回读失败 → 重试成功：cancelRecovery.active 变 false、reason 清空（响应式公开读取） | **FAIL** | `AssertionError: 重试成功后 cancelRecovery 仍是 active=true（响应式读取没有随状态更新）: expected true to be false` |
| 三.6-b | 同板失败后重试仍失败：如实更新真实原因（不假装成功），再重试成功才收敛 | **FAIL** | `AssertionError: 重试失败没有更新真实原因: expected '撤回还没有被服务器事实确认：第一次读取失败（503）' to contain '502'` |
| 三.6-c | B 的撤回失败晚于 A 的撤回开始：A 的撤回结果不被 B 的迟到失败覆盖 | PASS | — |
| 三.6-d | 组件层：取消后回读失败显示重试入口，点它重试成功后入口消失 | PASS | 见「诚实边界」 |

**既有用例复现**：`frontend/src/acceptance-state-recovery/f1-cancel-recovery-independent-changes.test.ts`
第二例在基线 HEAD 上红（`f1…test.ts:187 expected true to be false`，即 `store.cancelRecovery?.active` 未收敛），
与三.6-a 是同一根因；在在途修复代码上变绿。我另在 `q46-r7-cancel-recovery.test.ts` 里复刻了该场景并补了
「同板连续失败」与「跨板迟到失败」两条。

## 二、红证据对应的产品缺口（基线代码，不弱化、不臆测）

### 缺口 1（三.6）：`cancelRecovery` 是「普通 Map + computed」，computed 缓存不失效 → 响应式读取永远停在旧值

基线（`interactive.ts:1374-1380`）：

```ts
const cancelRecoveryByBoard = new Map<string, { boardId: string; active: boolean; reason: string | null }>();
const cancelRecovery = computed(() => cancelRecoveryByBoard.get(boardId.value) ?? null) as unknown as Ref<…>;
```

`settleCancelRecovery()`（:3161）用 `Map.set` 换掉对象；`boardId` 不变时 computed 不重算，
**`store.cancelRecovery` 永远是第一次读到的那个对象**。后果：
1. 重试成功后 `active` 仍为 true（三.6-a 红）；
2. 同板第二次重试失败的真实原因写不进去（三.6-b 红：仍是 503，看不到 502）；
3. 界面「重新核对服务器事实」的文案/状态读取同一个 computed，会同样停在旧值。

### 缺口 2（二.5）：`ImpactConfirmLayer` 的 `outcome` 没有板面身份 → B 的失败文案与重试按钮渲染在 A 上

基线（`ImpactConfirmLayer.vue:132`）`outcome = ref<{kind,title,text,paused} | null>`，无 `boardId`；
`confirm()` 的 `await store.confirmImpact()` 返回时用户已经切到 A，它照样把 B 的 `save_failed` 结果写进
同一个 `outcome`，而结果面板的 `v-if` 只看 `!open && !deferred && outcome` —— 于是 **A 的页面上出现
「这次改动没有保存成功：B 板保存失败：服务端 500」与「重新核实并保存」按钮**（二.5-b 红）。
点这个按钮会走 `store.saveNow()`，发的是**当前板面 A** 的保存 —— 串板风险。

### 缺口 3（二.4）：`continueBoardRecovery()` 的旧步骤结束时会 `clearSaveRecoveryPlan()`，把新步骤的恢复计划一起清掉

基线（`interactive.ts:3017-3031`）：旧步骤（已被新候选的步骤接管）返回后，`currentPlan.planId === plan.planId`
仍然成立（同一轮恢复共用 planId），于是照常 `clearSaveRecoveryPlan(owner)`。新步骤随后返回「不需要影响确认（ok）」时
`recoveryPlanFor(owner)` 已为 null → 不 `scheduleSave()` → **保存就此停住（只有 1 次 PUT，永远等不到第 2 次）**（二.4-d 红）。

## 三、修复代码上的复跑（Lead 通知「实现已落地」）

Lead 于 2026-11-15 13:35 通知实现已落地（工作树内 `interactive.ts` / `ImpactConfirmLayer.vue` 等为修复改动，**未提交**）。
在修复后的工作树上复跑同一份用例：**11 passed**（`green-r7-flow.log`；此前连跑 2 次同样 11 passed：
`current-worktree.log` / `current-worktree-rerun.log`），全量 `vue-tsc --noEmit` exit 0。
修复改法与三个缺口一一对应（仅记录事实）：

- `cancelRecoveryByBoard` 改成 `ref<Record<…>>`，`cancelRecovery` 仍是 computed 但读响应式对象；
  另加 `opSeq` 代次，迟到回执不许结算较新的撤回操作（对应缺口 1）；
- `ImpactConfirmLayer` 的 `outcome` 增加 `boardId`、新增 `visibleOutcome`（只在当前板面显示），
  `confirm()` 在**点击那一刻**取定 `ownerBoardId`（对应缺口 2）；
- `continueBoardRecovery()` 只让「仍然拥有处理权」的步骤清理计划 / 展示确认 / 安排下一次保存（对应缺口 3）。

**green 证据**：`green-r7-flow.log`（Lead 要求的最终复跑输出，11 passed / 2 files，2026-11-15）。

## 四、测试设计说明（为什么不是「测内部实现」）

- 全部走 store **公开动作**：`selectBoard` / `commit` / `saveNow` / `confirmImpact` / `cancelImpact` /
  `retryCancelRecovery`；组件用 `@vue/test-utils` 挂载 `ImpactConfirmLayer.vue`，
  断言落在公开的 `data-im` 钩子（`impact-confirm` / `impact-confirm-submit` / `impact-confirm-outcome` /
  `impact-confirm-retry` / `impact-confirm-recovery-retry`）。
- 断言落在**真实 API 调用参数**与**store 对外可见状态**上：`checkMaterialImpact(boardId, stateVersion, candidate)`、
  `saveBoardState(boardId, candidate, opLabel, {checkId, stateVersion})` 的目标板面 / 候选正文 / 确认句柄；
  `pendingImpact.checkId/boardId/stateVersion/candidateKey`、`saveStatus` / `saveError` / `cancelRecovery` / `dirty` / 卡片集合。
- mock 只打在**服务边界**（`services/interactive`），用受控 promise 制造「请求在飞 / 返回顺序 / 失败」；
  不 mock store 逻辑。`candidateKey` 的期望值由测试自己的 `comparable()`（与 PUT 正文同口径的公开形状）独立算出，
  不复用 store 内部实现。
- 只依赖**基线已存在**的公开 API 与字段，不依赖本轮修复新增的字段命名（如在途的 `opSeq`、`visibleOutcome` 均未出现在断言里）。

## 五、诚实边界

1. **基线红是在干净 worktree 上跑的**，不是共享工作树；共享工作树当时已有别人的在途修复。
   两个测试文件本身在基线 worktree 上 `vue-tsc --noEmit` exit 0（产品代码仍为 0dbc387f）。
2. 三.6-d（组件层重试入口）在基线 HEAD 上就是绿的：组件 `cancel()` 里读 `store.cancelRecovery` 时是**本次会话第一次读**，
   拿到的是新值；真正红的是「重试后再次读取」（三.6-a/b）。这条保留为回归护栏，不冒充红证据。
3. 二.4-a/b/c 与二.5-a/c 在基线就是绿的（round 4/5 已修），本文件把它们固化成回归护栏；
   不声称这些是「本轮新修」。
4. 本轮只做 store/组件层定向验证：**未跑全量测试、未触发 CI、未 push、未起真实后端/浏览器**。
   真实浏览器端的同类断言由 task-1 harness 覆盖。
5. 基线 worktree `D:\qio-dev\_r7-baseline-head` 暂保留供复跑；用完可
   `git worktree remove D:\qio-dev\_r7-baseline-head --force`（node_modules 是 junction，需 `--force`）。
6. 共享工作树当前 `git diff` 里 `interactive.ts` / `ImpactConfirmLayer.vue` / `AddMenu.vue` /
   `pm032-import-board-identity.test.ts` 的改动**不是我做的**（我只新增两个 `q46-r7-*impact-ownership/cancel-recovery` 测试文件）。

## 六、给 Lead 的下一步建议

1. 修复定稿后复跑：
   `cd frontend; npx vitest run src/stores/__tests__/q46-r7-impact-ownership.test.ts src/stores/__tests__/q46-r7-cancel-recovery.test.ts`
   期望 11 passed，并把输出存回本目录（例如 `final-green.log`）。
2. 三.6 的响应式缺口建议同批复跑既有 `f1-cancel-recovery-independent-changes.test.ts`、
   `recovery-lead-state-facts.test.ts`、`closure-lead-r1r2r3r5.test.ts`、`pm032-board-save-isolation.test.ts`
   （在途代码上这 5 个文件 62 例已全绿）。
3. 建议顺带确认 `ImpactConfirmLayer` 的「重新核实并保存」按钮在**非原板面**不可见后，原板面是否仍能从
   现有入口（同板失败态 + 重试按钮）处理 B 的失败 —— 本文件二.5-b 已断言「切回 B 后按钮可见可点且只发 B 的 PUT」。
## 七、收尾回执（Lead 2026-11-15 13:35 通知后）

- 按 Lead 要求复跑并把 green 证据写到 `_lead-logs/qio032-46/r7-specs/green-r7-flow.log`：
  **2 files / 11 tests 全绿**（impact-ownership 7 例 + cancel-recovery 4 例）。
  说明：impact-ownership 现在 7 例（Lead 通知时是 6 例）—— 我在收尾前补了二.5-c
  「同板较早失败结果不覆盖较新确认操作」一条，专门覆盖任务书「同板较早结果不能覆盖较新确认操作」。
- 「B 上点重试按钮只发 B 请求」已在组件层用例里断言（`q46-r7-impact-ownership.test.ts` 二.5-b 结尾）：
  切回 B → `[data-im="impact-confirm-retry"]` 可见可点 → 点击后统计
  `saveBoardState` 的 boardId：B 恰好 +1 次、A 0 次。
- 回归复跑（最终修复代码上）：`f1-cancel-recovery-independent-changes`、`recovery-lead-state-facts`、
  `closure-lead-r1r2r3r5`、`ImpactConfirmLayer` → **4 files / 36 tests 全绿**
  （原始输出 `green-related-existing.log`）。
- 未覆盖边界：本轮不跑全量、不触发 CI、不 push；真实浏览器端的同类断言由 task-1 的 harness 覆盖；
  未验证 Tauri 原生窗口 / 真实多窗口并发。