# task-6 · R10 队友 cancel-retry-ui 交付报告（Task 3 撤回提示跟随原板与最终结果）

> 产品工作树 `D:/qio-dev/q46-r9`（分支 `fix/qio032-46-r9`，起点 / 验收 SHA `70e475c2d4393f6eb6a1a28491078b7a6f32cf4b`）；红基线工作树 `D:/qio-dev/_q46-r10-baseline`（detached 同 SHA、产品代码零改动、node_modules junction）。
> 只跑受影响的定向测试，未跑全量 / 未触发 CI / 不联网、未用真实 API Key、未 `git add/commit/push`、未改 `docs/status.md`。
> `frontend/src/stores/interactive.ts` 与其他队友共享：本任务只用 `edit` 做小范围唯一字面替换（仅 `retryCancelRecovery()` 区域），未用 `write` 整文件、未重构无关区域。

## 0. 交付文件清单

| 类型 | 文件 | 说明 |
| --- | --- | --- |
| 产品代码 | `frontend/src/components/interactive/v2/ImpactConfirmLayer.vue` | `retryCancelRecovery()` 冻结 `ownerBoardId`/`opSeq`，结果只更新对应操作、写回原板、以 store 事实为准（`:314-347`） |
| 产品代码 | `frontend/src/stores/interactive.ts` | `retryCancelRecovery()` 失败分支先看「该 owner+opSeq 是否已被同板采纳读取确认」（`:7855-7907`） |
| 测试 | `frontend/src/stores/__tests__/q46-r8-select-cancel.test.ts` | 原 9 例 → **11 例**（新增 2 例，第十轮 Task 3；并把「取消+回读失败」装置提升到模块作用域供两轮共用） |
| 测试 | `frontend/src/components/interactive/v2/__tests__/ImpactConfirmLayer.test.ts` | 原 12 例 → **15 例**（新增 3 例，第十轮 Task 3；新增 service mock 与 B 板装置） |
| 红证据 | `_lead-logs/qio032-46/r10/red/red-r10-select-cancel-task3-baseline.log` | 基线：`Tests 1 failed | 10 passed (11)` |
| 红证据 | `_lead-logs/qio032-46/r10/red/red-r10-impact-confirm-task3-baseline.log` | 基线：`Tests 3 failed | 12 passed (15)` |
| 绿证据 | `_lead-logs/qio032-46/r10/green/green-r10-task3-cancel-retry-targeted.log` | 3 文件 `Tests 30 passed (30)` |
| 绿证据 | `_lead-logs/qio032-46/r10/green/green-r10-task3-adjacent-regression.log` | 16 文件 `Tests 89 passed (89)` |
| 绿证据 | `_lead-logs/qio032-46/r10/green/green-r10-vue-tsc.log` | `exit code: 0`，`output lines: 0` |

---

## 1. 根本原因（基线 70e475c2 的实际行为）

1. **组件结果归属错**：`ImpactConfirmLayer.retryCancelRecovery()` 在 `await store.retryCancelRecovery()` **之后**才调用 `outcomeBoardId()`（= 结果回来时的 `store.boardId`）写 outcome。B 点「重新核对」→ 切 A → 结果回来时 outcome 被标成 A：A 上显示 B 的撤回提示，回到 B 反而看不到结果。
2. **结果不以 store 事实为准**：同一次撤回已被同板另一次真正采纳的读取确认（`cancelRecovery.active === false`）后，这次 retry 请求迟到失败，组件无条件显示「撤回还没有被服务器事实确认」，把已确认说回等待/失败。
3. **store 谎报失败**：`interactive.ts retryCancelRecovery()` 的 `catch`（请求抛错）分支只看「这次请求失败」，不看这次撤回是否已被确认 → 返回 `{ok:false}` 并写失败原因；界面的失败提示据此覆盖已确认结论。`!readAdvanced/!opStillCurrent/!seqReached` 分支同理（`!applied` 分支在 r9 已有一条显式确认检查，但失败结算没有统一出口）。
4. **旧操作结果覆盖新操作**：同板又发起新的撤回（`opSeq` 递增）后，旧「重新核对」的迟到失败仍会写成当前提示。

## 2. 改动落点

### 2.1 组件 `ImpactConfirmLayer.vue`（`:304-347`）

- 点击时冻结 `ownerBoardId = store.boardId` 与 `opSeq = store.cancelRecovery?.opSeq`；
- `await` 后：`store.cancelRecovery` 存在且 `opSeq` 不同 → **丢弃旧结果**（`return`，不写 outcome）；
- 同一次撤回 `active === false`（已被同板采纳读取确认）→ 即使 `result.ok === false` 也只写 `kind: "cancelled"`，绝不显示等待/失败；
- 其余情况按 retry 返回值写结果，**一律 `boardId: ownerBoardId`**（`writeOutcome` 闭包），A 上 `visibleOutcome` 不显示，切回 B 才显示。

### 2.2 store `interactive.ts retryCancelRecovery()`（`:7855-7907`）

- 新增 `confirmedForThisOp()`：`cancelRecoveryByBoard[owner]` 的 `opSeq === opSeq` 且 `!active`；
- 失败结算统一收口到 `settleUnconfirmed()`：已被确认 → `return { ok: true }`（不谎报废；`!applied`、`!readAdvanced/!seqReached`、`catch` 三个分支共用）；
- `settleCancelRecovery(false,...)` 未改：它自身只在 `active` 时生效（`:3742` 早退），已确认的撤回不会被迟到失败重开；`opSeq` 不匹配时也不结算新操作 —— 迟到失败只影响对应 `opSeq`（既有第九轮口径保留，本轮新增用例锁定）。

## 3. 红/绿证据（逐用例）

红命令（基线工作树，先复制改后测试文件→跑→`git checkout --` 还原，已验证基线 `git status --porcelain` 为空、HEAD 仍为 `70e475c2`）：

```powershell
cd D:/qio-dev/_q46-r10-baseline/frontend
# 先 Copy-Item 两个改后的测试文件到基线
npx vitest run src/stores/__tests__/q46-r8-select-cancel.test.ts
# → Test Files 1 failed (1) | Tests 1 failed | 10 passed (11)
npx vitest run src/components/interactive/v2/__tests__/ImpactConfirmLayer.test.ts
# → Test Files 1 failed (1) | Tests 3 failed | 12 passed (15)
```

| # | 新增用例（层） | 基线失败断言（原文摘） | 绿 |
| --- | --- | --- | --- |
| ① | 同板采纳读取已确认后，重试请求迟到失败：不重开等待且返回 ok:true（store） | `AssertionError: 撤回已被同板采纳读取确认，迟到的失败重试却谎报失败: expected false to be true` | ✓ |
| ② | B 点「重新核对」后切到 A：A 不出现 B 的撤回提示，切回 B 才显示结果（组件） | `AssertionError: B 的迟到重试结果写进了 A 的页面: expected true to be false` | ✓ |
| ③ | 同一次撤回已被同板采纳读取确认：迟到的失败重试不重新显示等待/失败（组件） | `AssertionError: 已确认的撤回被迟到的失败重试重新显示成等待/失败: expected 'cancelled_unconfirmed' to be 'cancelled'` | ✓ |
| ④ | 重试在飞时同板又开始新的撤回：旧结果被丢弃，不覆盖新一次撤回的提示（组件） | `AssertionError: 旧撤回的迟到重试覆盖了新一次撤回的提示: expected '…旧重试迟到失败…' not to contain '旧重试迟到失败'` | ✓ |
| ⑤ | 切板后重试结果只结算原板：不在当前板建立撤回状态（store，护栏） | —（基线即绿：既有 owner 板面身份保护） | ✓ |

断言口径：
- 组件用例只看**真实组件 DOM**（`[data-im="impact-confirm-outcome"]` 的 `data-outcome` / `data-board-id` / 文案）＋**真实 store 状态**（`store.cancelRecovery.active/reason`、`store.boardId`）；③ 允许用 `vi.spyOn(store.retryCancelRecovery)` 只控制「返回失败 + 到达时序」，而「已确认」这一事实由**真实** `store.refreshBoardFromServer(BOARD_B)` 的采纳读取产生，不是 mock 出来的状态；
- ①②④ 都是走真实 store 动作：经 UI「取消本次改动」建立等待态、点「重新核对」由真实 store 发起回读、切板用真实 `store.boardId`。

绿命令与结果：

```powershell
cd D:/qio-dev/q46-r9/frontend
npx vitest run src/stores/__tests__/q46-r8-select-cancel.test.ts src/components/interactive/v2/__tests__/ImpactConfirmLayer.test.ts src/stores/__tests__/q46-r7-cancel-recovery.test.ts
# → Test Files 3 passed (3) | Tests 30 passed (30)
npx vue-tsc --noEmit
# → exit code 0，无输出
```

## 4. 定向回归（产品工作树）

| 命令 | 结果 | 日志 |
| --- | --- | --- |
| 两个任务测试文件 + `q46-r7-cancel-recovery`（3 文件） | `Test Files 3 passed (3)` / `Tests 30 passed (30)` | `green/green-r10-task3-cancel-retry-targeted.log` |
| `src/acceptance-state-recovery` 全 13 文件 + `closure-lead-r1r2r3r5` + `recovery-lead-state-facts` + `pm032-board-save-isolation`（16 文件） | `Test Files 16 passed (16)` / `Tests 89 passed (89)` | `green/green-r10-task3-adjacent-regression.log` |
| `npx vue-tsc --noEmit` | exit 0，0 行输出 | `green/green-r10-vue-tsc.log` |

基线工作树在红证据采集后已 `git -C D:/qio-dev/_q46-r10-baseline checkout -- <两个测试文件>`，`git status --porcelain` 为空（已验证），产品代码零改动。

## 5. 未验证项 / 风险（诚实清单）

1. **未跑全量 `npm test` / CI / 后端 pytest / Rust cargo**（按本轮分工由 Lead / 其他队友负责）。
2. **同类跨板归属问题仍在其它入口**：`confirm()` 已在 r7 冻结归属，但 `cancel()` 与 `retrySave()` 仍在 `await` 之后调用 `outcomeBoardId()` —— 与本轮 Task 3 同源，但不在任务书范围内，**本轮未改**（需 Lead 决定是否另派）。
3. **跨板 opSeq 语义按需求字面实现**：`store.cancelRecovery` 是「当前板」的 computed。B 的 retry 在飞时若用户切到 A 并在 A 上发起撤回（A 有不同 `opSeq`），组件按「opSeq 已被新撤回替换 → 丢弃旧结果」处理，B 的旧结果不会写回 B。若 Lead 认为应改为「按 owner 板查询」，需要 store 暴露按板只读接口（本轮未扩接口，避免扩大改动面）。
4. **已确认事实的跨板可读性**：组件在 A 上无法直接读到 B 的 `cancelRecovery.active`；这类时序由 store `confirmedForThisOp()` 兜底（真实 retry 会返回 `ok:true`），组件侧重在「结果到达时仍在 B」的场景锁定。
5. **共享文件并发**：交付时 `interactive.ts` 还包含队友 r10 改动（`recordNewIntents` / `refreshAfterSubmit` / `confirmAcceptanceAfterUnclear` 区域，非本任务）；我只 `edit` 了 `retryCancelRecovery()` 小范围。Lead 集成后建议再跑一次本报告第 4 节两条命令。
6. 红基线口径：本轮基线红用例 = store 1 条（①，新增）+ 组件 3 条（②③④，新增）= **4 条**；用例 ⑤ 是防回归护栏（基线即绿），不计红。
