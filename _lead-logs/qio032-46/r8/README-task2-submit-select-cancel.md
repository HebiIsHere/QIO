# task-2 · R8 红用例（第 1 项「提交归属」+ 第 7 项「切板代次」+ 第 8 项「撤回代次」）

> 第八轮修复轮 · 交付物是**测试**（不改产品代码）。基线工作树 `D:/qio-dev/_q46-r8-baseline`（detached `c9fedc60744911c088ebc082faceef266ca3048b`，产品代码零改动，依赖 junction 指向 pm-qio032-integration）。
> 用例通过与失败都只由 store 公开动作驱动；断言只落在 store 公开状态 + services 边界的真实 API mock 调用参数 / 次数。

## 1. 交付文件

| 文件 | 用例数 | 任务书条目 |
| --- | --- | --- |
| `frontend/src/stores/__tests__/q46-r8-submit-ownership.test.ts` | 5 | 一.1「在 B 点提交，后端却受理 A」 |
| `frontend/src/stores/__tests__/q46-r8-select-cancel.test.ts` | 4 | 三.7「较早的切板请求不得覆盖较新的选择」+ 三.8「同板面的旧撤回回执不得结束新撤回」 |

mock 只打在 services 边界：`services/interactive`（`fetchBoards` / `fetchBoardState` / `saveBoardState` / `checkMaterialImpact` / `submitBoard` / `fetchAcceptance` / `fetchIntents` …）。
没有 mock store 内部逻辑，也没有断言任何内部 Map / 变量名。

## 2. 运行命令

```powershell
# 基线（红）：
cd D:/qio-dev/_q46-r8-baseline/frontend
npx vitest run src/stores/__tests__/q46-r8-submit-ownership.test.ts src/stores/__tests__/q46-r8-select-cancel.test.ts

# 产品工作树（当前实现）：
cd D:/qio-dev/q46-r8/frontend
npx vitest run src/stores/__tests__/q46-r8-submit-ownership.test.ts src/stores/__tests__/q46-r8-select-cancel.test.ts
```

## 3. 基线红证据（c9fedc60）

- 合并原始输出：`red-r8-task2-combined.log` → `Test Files 2 failed (2) / Tests 9 failed (9)`。
- 分文件原始输出：`red-r8-submit-ownership-baseline.log`（5 failed）、`red-r8-select-cancel-baseline.log`（4 failed）。
- 全部失败都是**断言失败**（AssertionError），不是测试自身报错 / 超时。
- 基线 HEAD 与产品工作树开工 HEAD 同为 `c9fedc60744911c088ebc082faceef266ca3048b`。

### 3.1 逐用例：基线实际 vs 期望 vs 产品缺口

| # | 用例 | 基线失败断言（原文） | 实际 → 期望 | 对应产品缺口判断 |
| --- | --- | --- | --- | --- |
| 1 | 提交归属：B 保存后在 A 上放行 | `提交发到了切板后的 A（应发往发起提交的 B）: expected 'board_a' to be 'board_b'` | `submitBoard('board_a', …, seq=3, …)` → 必须 `board_b`，且 `baseStateVersion = 8`（B 本次保存确认的版本） | `submit()` 没有在入口固定发起板面与本次提交身份；`await saveNow()` 之后再读全局 `boardId` / `board.value.seq` / `submissionIdentityFor()`，提交目标、版本与 `clientRequestId` 台账（`qio.interactive.pending-submission.<board>`）全部跟着切板漂到 A |
| 2 | B 保存失败：不提交、原因与重试入口留在 B | `B 的保存失败后仍从 A 发出了提交: expected "vi.fn()" to not be called at all, but actually been called 1 times` | `submitBoard` 1 次（A）→ 0 次 | 保存失败的归属只写进了 B 的会话（`writeSaveResultToStashedSession`），但 `submit()` 的门禁读的是全局 `saveStatus/saveError/dirty`（A 是干净的），于是照样从 A 发出提交；B 的失败 / 候选 / 重试入口没有和这次提交绑定 |
| 3 | B 等待影响确认：确认后只提交 B | `B 还在等待影响确认却发出了提交: expected "vi.fn()" to not be called at all, but actually been called 1 times` | `submitBoard` 1 次（A）→ 0 次（确认前） | 影响确认按板面存进 B 的会话，但 `submit()` 的 `blocked` 判定只看全局 `pendingImpact`（此刻是 A 的 null），B 的待确认没有拦住提交 |
| 4 | B 的提交回执在 A 打开时才到 | `B 的提交回执写进了 A 的提交状态: expected 'succeeded' to be 'idle'` | A 上 `submitStatus = succeeded` 且 `lastSubmission = B 回执` → A 必须保持 `idle` / null；且提交后对 A 的整板 / 可见范围 / 任务 / 理解查询次数不得增加 | 提交回执、`refreshAfterSubmit()`、理解查询与任务刷新都直接写全局状态，没有绑定「发起提交的板面」；切回 B 时 `resetSubmissionStatusForBoard` 又清掉全局事实，B 看不到自己的受理结果 |
| 5 | 后端已受理但浏览器没收到响应（unknown） | `受理查证发到了切板后的 A（应查发起提交的 B）: expected 'board_a' to be 'board_b'` | `fetchAcceptance('board_a', creq_…)` → 必须 `board_b` + 同一 `clientRequestId`；重试查证仍同板同身份、`submitBoard` 只 1 次 | 未决身份是按「当时的 `boardId`」落台账的（基线落在 A）；`confirmAcceptanceAfterUnclear()` 用 `boardId.value` 查证，切板后查错板、重试身份也随板改变（有重复受理风险） |
| 6 | 三.7 先 B 后 A：B 的旧清单迟到 | `旧的切板请求把当前板面改回了 B（覆盖了较新的选择）: expected 'board_b' to be 'board_a'` | `boardId = board_b`、A 的内容/保存状态被 B 覆盖 → 必须仍是 A，旧请求返回 `ok:false` | `selectBoard()` 在 `await fetchBoards()` 之后没有任何「选择代次」校验：旧请求继续切换 `boardId` 并 `load()`，覆盖较新的选择 |
| 7 | 三.7 交换到达顺序：A 读取在飞时 B 旧清单先到 | `B 的旧响应在 A 读取期间抢走了当前板面: expected 'board_b' to be 'board_a'` | `boardId = board_b` → 必须 A | 同上；旧请求不只抢 boardId，还抢走了 A 正在进行的整板读取（`load()` 代次被顶掉） |
| 8 | 三.7 最新目标 A 读取失败：错误只归这次选择 | `B 的旧响应在 A 读取失败后抢回了页面: expected 'board_b' to be 'board_a'` | B 的旧请求成功切换并清掉 A 的 503 错误 → 必须留在 A、保留 A 的错误、旧请求 `ok:false` | 失败提示（`loadError`）与加载结束都没有按选择代次归属；旧响应能改写较新选择的失败事实 |
| 9 | 三.8 同板两次撤回：旧成功回执迟到 | `较早的撤回成功回执结束了较新的撤回（新撤回被提前判成已确认）: expected false to be true` | `cancelRecovery.active = false` → 必须仍 `true`（第二次回读还在等） | `refreshBoardFromServer()` / 保存成功路径里的通用 `settleCancelRecovery(true, null, requestedBoardId)` 不带撤回代次（`opSeq`），旧撤回的回读成功会把更新的撤回判成已确认；随后第二次真实失败的原因无处落地（`settleCancelRecovery` 见 `active=false` 直接 return） |

### 3.2 三.8 的跨板护栏（为什么合并在用例 9 里）

任务书三.8 还要求「B 的撤回回执在 A 打开时到达，A 的 cancelRecovery 不受影响」。该子场景**单独跑在基线是绿的**：
`refreshBoardFromServer()` 在 `boardId !== requestedBoardId` 时提前返回，B 的回执不会写 A 的 `cancelRecovery`。
按「基线 PASS 的用例不单独计数」的口径，它作为**同一缺陷的跨板护栏**放在用例 9 的后半段（同一 `it` 内）：
在 A 上建立自己的撤回等待 → 放行 B 的迟到回执 → 断言 `cancelRecovery` 仍属于 A 且 `active = true` → 再放行 A 自己的回读收敛。
这样既覆盖了三.8 的验收第三句，也不留下一条基线即绿的独立用例。

## 4. 产品工作树绿证据（Lead 实现后）

- `green-check-task2-combined.log`：`Test Files 2 passed (2) / Tests 9 passed (9)`（本次修复后复跑）。
- 分文件：`green-check-task2-submit-ownership.log`（5 passed）、`green-check-task2-select-cancel.log`（4 passed）。
- 历史对照（实现进行中时）：`current-product-task2.log` → 9 failed。

实现后复核的时序修正（Lead 复核点 1）：用例 3「B 等待影响确认」原先在 `await store.confirmImpact()` 之后才 `saveFlight.resolve()`，
而确认路径本身在等这次 PUT 的回执 —— 用例自己把自己锁死（基线因更早的断言先红而没有暴露）。
现改为「先发起 `const confirming = store.confirmImpact()` → `await settle()` → `saveFlight.resolve()` → `await confirming`」，
断言语义不变且更严格：
- 确认后 `saveStatus = saved`、`pendingImpact = null`；
- 再次提交只发出 **1 次**、目标为 **B**、`baseStateVersion` = B 确认保存后的当前版本（且 > 加载时的 7），A 的 PUT 为 **0 次**。

## 5. 测试口径（避免假红 / 假绿的关键点）

1. **提交链路完全走公开入口**：`store.commit()` 编辑 → `store.saveNow()`（受控 promise 挂起 PUT）→ `store.submit()`（真实 `await saveNow()` 加入在途保存）→ `store.selectBoard(A)` → 放行 / 拒绝保存回执。没有调用 `refreshBoardFromServer` 等非按钮路径。
2. **收据身份与板面一起断言**：`submitBoard` 的第 1 个参数（目标板面）、第 4 个参数（`baseStateVersion`）、第 6 个参数（`clientRequestId`）一起核对；并用 `localStorage` 的 `qio.interactive.pending-submission.<board>` 台账核对身份归属（公开可观察的持久化事实，不是内部变量）。
3. **提交后刷新的断言按目标板面计数**：在回执到达前后快照 `fetchBoardState / fetchVisibleRange / fetchIntents / fetchUnderstanding` 对 A 的调用次数，断言「没有新增 A 的读取」；不断言 B 一定读几次（实现可以选择在切回 B 时补刷新）。
4. **切板用例的 450ms 保存防抖**：用例内不依赖防抖（显式 `saveNow()`），`afterEach` 统一等 700ms 排空，避免跨用例污染请求记录。
5. **影响确认场景**：只有 `activeIntents` 非空时保存才先走 `checkMaterialImpact`；用例 3 / 9 通过覆盖 `fetchIntents`（仅 B 返回 running）来构造，mock 的 `affectedCheck` 带 `checkId` 与 `stateVersion`，保证 `confirmImpact()` 走真实保存路径。`confirmImpact()` 会等到 PUT 回执：受控保存严格按「发起确认 → settle → resolve 挂起 PUT → await 确认」驱动，用例自身不等待同一个未放行的 promise（否则死锁，见 §4）。
6. **失败态断言用现有语义**：用例 2 要求 B 的 `submitStatus = failed` 沿用现有同板行为（保存失败 → `submit()` 返回「板面没有保存成功，本次未提交」）；如果实现改为不写 submitStatus、只用 `saveError` 表达，需要 Lead 明确口径后同步改断言。

## 6. 建议的修复方向（供 Lead 参考）

1. **`submit()` 入口冻结提交上下文**：`{ boardId, seq 依据, identity, 影响确认, 草稿提升结果, 视口 }` 一次快照；所有 `await` 之后只用快照里的板面，不再读全局 `boardId / board.value`。提交版本用「发起板面本次保存确认的 seq」。
2. **提交结果 / 回执 / 刷新按板面记账**：仿 `boardSaveSessions` 建「提交会话」；回执到达时若已切走，写回原板会话，切回时恢复 `submitStatus / lastSubmission / submitNote / 理解事实`；`refreshAfterSubmit` 绑定原板面（或在原板重新可见时补刷新），不碰当前板。
3. **保存失败 / 待确认 / 候选变更的分支都在原板结算**：`submit()` 的门禁读「发起时板面的会话状态」，而不是全局 `saveStatus / pendingImpact`；失败原因与重试入口存回原板。
4. **受理查证与重试身份**：`pendingSubmission` 台账按「提交身份自带的 boardId」读写；`confirmAcceptanceAfterUnclear()` / 重试查证使用 `identity.boardId`（而非 `boardId.value`），同一身份不重复 POST。
5. **`selectBoard()` 选择代次**：入口 `const seq = ++boardSelectSeq`；`fetchBoards` 返回、整板读取、`loadError` 写入、`loading` 复位、路由跳转前都校验 `seq === boardSelectSeq`；过期选择返回 `{ok:false, reason}` 且不写任何状态。
6. **撤回代次贯穿回读结算**：`cancelImpact()` 的 `opSeq` 传给它引发的整板回读（不能只传 boardId）；`settleCancelRecovery` 的通用调用点（`refreshBoardFromServer` 各分支、前台保存成功）都要带「这次回读/保存属于哪次撤回操作」或至少在结算前核对当前记录仍是同一 `opSeq`；失败分支保持 `active=true` + 真实原因，供 `retryCancelRecovery()` 使用。
