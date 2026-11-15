# task-1 · R8 store 回归测试：草稿清除保护（第 2/3 项）+ 草稿读取与保存队列（第 4/5/6 项）

> 第八轮修复轮 · 交付物是**测试 + 基线红证据**（不改产品代码）。
> 红基线 worktree：`D:\qio-dev\_q46-r8-baseline`，detached `c9fedc60744911c088ebc082faceef266ca3048b`（产品代码零改动；红跑前后对该文件的 `git status` 为 clean）。
> 产品工作树：`D:\qio-dev\q46-r8`（Lead 正在改产品代码；本文件放进产品树同路径，本轮不要求它绿）。

## 1. 交付文件

| 文件 | 用例数 | 覆盖 |
| --- | --- | --- |
| `frontend/src/stores/__tests__/q46-r8-drafts-protect.test.ts` | 6（4 红 + 2 控制组） | 第 2 项：旧正式保存不得删除另一标签页的新恢复副本（未同步 / 已同步）；第 3 项：撤销正式编辑、撤销删除时保留对应草稿；控制组：删除写入失败保留、正常落库清除 |
| `frontend/src/stores/__tests__/q46-r8-drafts-queue.test.ts` | 7（4 红 + 3 控制组） | 第 4 项：迟到读取不得把新草稿显示成旧稿（两种返回顺序 + B→A→B）；第 5 项：一块板 503 不堵其他板；第 6 项：冲突板不卡队列；控制组：板面归属丢弃、读取期间新输入、无可发送项重复进入 |
| `_lead-logs/qio032-46/r8/t1-drafts-red.log` | — | **基线 c9fedc60 原始 vitest 输出**（两文件同跑一次，覆盖 13 例：8 failed / 5 passed） |
| `_lead-logs/qio032-46/r8/t1-drafts-queue-freeze-probe.log` | — | 第 6 项「浏览器一度无法响应」的基线探针证据（临时给基线源码加 `globalThis.__dbg` 打点后采集；采集完已从备份恢复并核对 `git status` clean，详见 §6） |
| `_lead-logs/qio032-46/r8/t1-drafts-current-product-rerun.log` | — | 信息性快照：产品工作树当前实现下同一组用例的结果（2026-11-16 01:29 采样，11 passed / 2 failed，见 §7） |

断言只落在：

- store 公开状态：`drafts` / `draftStateFor` / `draftConflictFor` / `pendingDraftClearKeys` / `board`；
- 本机恢复记录：`readCardLocalDraft(cardId)`（`frontend/src/interactive/drafts.ts` 公开函数）/ `localStorage`；
- 真实服务边界 mock 的调用参数与次数：`api.saveDrafts(boardId, drafts, {removed, baseRev, keyVersions})`、`api.saveBoardState(boardId, candidate, reason)`、`api.fetchBoardState(boardId)`。

没有断言任何内部 Map / 变量名（`pendingDraftClears`、`draftBooks`、`draftInFlight` 等一概不碰）。第 2 项的双标签页用「同一 jsdom `localStorage` + 两次 `setActivePinia(createPinia())` 得到两个独立 store 实例」模拟；第 6 项用 `vi.useFakeTimers({toFake:["setTimeout","clearTimeout"]})` 推进 600ms 防抖（原因见 §6：基线会在该路径上锁死事件循环）。

## 2. 运行命令

```powershell
# 基线红（先把两个测试文件复制到基线同路径）
Copy-Item -Force D:\qio-dev\q46-r8\frontend\src\stores\__tests__\q46-r8-drafts-*.test.ts D:\qio-dev\_q46-r8-baseline\frontend\src\stores\__tests__\
cd D:\qio-dev\_q46-r8-baseline\frontend
npx vitest run src/stores/__tests__/q46-r8-drafts-protect.test.ts src/stores/__tests__/q46-r8-drafts-queue.test.ts

# 产品工作树（Lead 实现后复跑）
cd D:\qio-dev\q46-r8\frontend
npx vitest run src/stores/__tests__/q46-r8-drafts-protect.test.ts src/stores/__tests__/q46-r8-drafts-queue.test.ts
```

## 3. 基线 c9fedc60 红证据（8 failed / 5 passed）

原始日志：`t1-drafts-red.log`（Test Files 2 failed (2) / Tests 8 failed | 5 passed (13)，Duration 11.46s）。

| 用例 | 场景 | 基线结果 | 失败断言原文（原始消息） | 对应产品缺口判断 |
| --- | --- | --- | --- | --- |
| 2A | 标签页 1 正式保存挂起；标签页 2 输入新稿未同步；放行标签页 1 成功回执 | **FAIL** | `标签页 1 的旧正式保存回执把标签页 2 还没同步的新恢复副本删掉了: expected '' to be '标签页 2 还没同步的新文字'`（实际本机记录被写成 kind:"cleared"、text=""、version 被推进） | `clearDraft()` → `writeCardLocalClear()` 无条件重写共享 `localStorage` 记录：登记清除时没有记「当时的本机记录版本 + 内容指纹」，清理前也没有重读实际存储核对「仍是这次正式编辑用过的同一份」 |
| 2B | 标签页 2 的新稿已 `saveDrafts` 成功（服务端 rev 3→4、该键版本 4）；标签页 1 旧回执随后到达 | **FAIL** | `旧正式保存回执仍对已被另一标签页更新过的草稿键发起了 removed: expected 1 to be +0` | 后端草稿删除没有核对「所消费的版本」：旧回执用标签页 1 读到的 `keyVersions[KEY]=3` / `baseRev=3` 直接发 removed，而服务端已是版本 4；正确口径应在发请求前用记录版本/服务端版本守卫，保留更新版本并走既有冲突语义 |
| 3A | 完成编辑 → 正式保存挂起 → `undo()` → 放行旧成功回执 + 再做无关修改并保存成功 | **FAIL** | `撤销后的无关保存成功仍向服务器发起了针对该草稿的 removed: expected 1 to be +0` | 待清除登记（`pendingDraftClears`）没有绑定「本次正式编辑结果」：`undo()` 不作废本板登记；`applyDraftClearsForSave()` 只看到「板面保存成功 + 登记版本仍匹配」，没有核对回执/候选里这张卡的这次编辑是否仍然生效 |
| 3B | 删除卡片 → 撤销删除 → 删除回执 / 无关保存成功 | **FAIL** | `撤销删除后，删除回执/无关保存成功仍清掉了这张卡的草稿: expected 1 to be +0` | 同 3A：删除后的草稿清理沿用同一登记，但撤销删除没有让该登记失效；删除回执只证明「板面保存成功」，不能证明这次删除仍然有效 |
| 4A | B 第 1 版读取挂起 → 切 A → 另一窗口把 B 推到第 2 版 → 切回 B 读到第 2 版 → 放行旧第 1 版响应 | **FAIL** | `迟到的第 1 版读取把界面改回了旧稿（正文与版本没有成对应用）: expected 'B 的草稿第 1 版' to be 'B 的草稿第 2 版'` | `refreshBoardFromServer()/mergeDraftsFromPayload()` 没有草稿读取代次/逐键版本校验：迟到的 payload 里 `serverDrafts` 直接覆盖已读到的新正文；版本记账（rev2/keyVer2）只增不减，于是出现「显示第 1 版正文 + 拿第 2 版版本继续保存」的不成对状态 |
| 4B | 同上，但第 1 版响应在**第二次读取落定之前**到达（B→A→B） | **FAIL** | `更早的读取在第二次读取落定前把界面改成了旧稿: expected 'B 的草稿第 1 版' not to be 'B 的草稿第 1 版'` | 迟到响应只按 `boardId` 归属丢弃；板面事实有 `readSeq/boardReadAppliedSeq` 保护，但草稿合并路径在 `readIsStale` 分支仍会调用 `mergeDraftsFromPayload()` —— 读取代次校验没有覆盖草稿正文 |
| 5A | B 输入草稿（防抖中）→ 切 A 编辑 → A 的 `saveDrafts` 返回 503 | **FAIL** | `A 板的草稿保存失败把 B 板还等在队列里的草稿堵住了（B 一直显示保存中、请求也没发）: expected 0 to be greater than or equal to 1` | `flushDrafts()` 一次请求失败后 break 出整块板循环，收尾只把**当前板**未保存键标 error；别的板面在自己的簿子里永远停在 `saving`、也不会再被发出。失败作用域没有限定到「该板该版本」，失败后也没有继续处理队列里其他板 |
| 6A | B 制造同键双窗口冲突（两份文字都保留）→ C 输入草稿 → 防抖前切 A | **FAIL** | `B 板存在待选择的草稿冲突时，C 板的草稿保存被卡住（队列停在冲突板上）: expected 0 to be greater than or equal to 1` | 循环每轮都从 `draftBooks` **重建** `pendingBoards`，只剩待选冲突的 B 永远排在第一；跳过 B 只 shift() 了本轮局部数组，下一轮又重建回 B —— 8 轮全在空转，C 永远排不到。且该「同步退出」路径会把在途引用留成一份已完成 Promise（见 §6），后续任何 `flushDrafts()` 自旋 → 浏览器无响应 |

### 控制组（基线即绿，属回归护栏，不是红用例）

| 用例 | 场景 | 说明 |
| --- | --- | --- |
| 3C | 删除写入失败：草稿与清除登记都保留 | 既有行为（只有保存成功才消化登记）的护栏，防止本轮修复把失败分支一起清掉 |
| 3D | 正常完成编辑且正式内容落库：该键 removed 会发、本机记录被清 | 既有行为护栏，防止本轮修复把「该清的」也留住 |
| 4C | 第 1 版响应在用户已经切到 A 时到达 | 既有板面归属保护可控，防止修复引入跨板写入 |
| 4D | 晚到的读取响应尊重读取期间输入的新草稿 | 既有 `memoryIsNewer` 保护可控，防止修复把用户新输入一起回退 |
| 6B | 没有可发送项时重复进入 `flushDrafts()` 不悬挂、不发请求 | 既有「无需发送时提前 return」路径可控（基线在 `draftInFlight` 赋值之前 return，所以不会踩到 §6 的自旋） |

> 5 例控制组在基线 PASS 是**有意保留的护栏**（与第七轮 C2 同一做法），红证据仍以 8 个 FAIL 用例为准。

## 4. 用例 ↔ 任务书编号

| 任务书条目 | 用例 |
| --- | --- |
| 二·2（旧正式保存不得删除另一标签页的新恢复副本） | 2A（新稿未同步）、2B（新稿已同步） |
| 二·3（撤销正式编辑时保留对应草稿） | 3A（撤销正式编辑）、3B（撤销删除）、3C/3D（控制组） |
| 二·4（迟到的读取不得把新草稿显示成旧稿） | 4A（旧响应后到）、4B（旧响应先到，第二次读取未落定）、4C/4D（控制组） |
| 二·5（一块板的保存失败不得堵住其他板） | 5A |
| 二·6（冲突板不得卡住队列） | 6A（+ 6B 控制组） |

## 5. 建议的修复方向（供 Lead 参考）

1. **第 2/3 项（清除登记绑定 + 版本守卫）**
   - `requestDraftClear(key)` 登记时冻结：原板、卡片 id、草稿写入代次、**本机记录版本 + 内容指纹**（`readCardLocalDraft/localRecordFingerprint`）、以及服务端该键已知版本；
   - `applyDraftClearsForSave(attempt)` 执行清除前重新读实际存储，按版本/指纹核对「记录仍是这次正式编辑用过的那一份」；不一致（另一标签页已写新稿）就保留记录、撤掉登记；
   - 收到成功回执时还要核对「回执/本次保存快照确实保存了这次编辑或删除」（不能只凭板面保存成功）；
   - `undo()/redo()/撤销删除` 必须让本板相关的待清除登记失效；`removed` 请求发出前用「本机记录版本 / 服务端 keyVersion」守卫，版本已被另一窗口推进时不发 removed（保留更新版本，走既有冲突/保留语义）。
2. **第 4 项（读取代次 + 草稿版本成对应用）**
   - `refreshBoardFromServer()` 记录「已发出的最新读取代次」与「已应用的读取代次」，迟到响应（readSeq < 最新已发出/已应用）不得进入 `mergeDraftsFromPayload()`；
   - `mergeDraftsFromPayload()` 逐键比较 payload `keyVersions[key]` 与本地已知版本：payload 版本更低时整键（正文+版本）丢弃或只登记冲突，不能只覆盖正文；同时保留 `memoryIsNewer`（读取期间新输入优先）。
3. **第 5 项（失败作用域 + 队列继续）**
   - 一次 `saveDrafts` 失败只把**这次请求所属板面**的键标 error/保留重试入口，然后继续处理 `pendingBoards` 里的下一块板；收尾不得把别的板的键标 error 或留在 `saving`。
4. **第 6 项（跳过只剩冲突的板 + 在途引用清理）**
   - 选下一块待处理板前先过滤「没有任何可发送更新/清除（只剩待选冲突）」的板，用剩余集合推进而不是每轮重建后永远取第一块；
   - 在途引用改成「先建 promise 再赋值，finally 只清自己」：

```ts
const flight = (async () => { ... })();
draftInFlight = flight;
try { await flight; } finally { if (draftInFlight === flight) draftInFlight = null; }
```

     否则「同步退出 / 没有任何可发送项」时 IIFE 的 `finally` 先于外层赋值执行，`draftInFlight` 会留下一个**已完成但永不清空**的 Promise，下一次 `while (draftInFlight) await draftInFlight;` 就变成微任务自旋（浏览器卡死）。

## 6. 第 6 项「浏览器一度无法响应」的基线探针证据

`t1-drafts-queue-freeze-probe.log` 是在**基线 worktree 上临时加 `globalThis.__dbg` 打点**后跑 6A 的同一场景采集的（打点文件 `interactive.ts` 已从 `.dbg-bak` 还原，`git status -- frontend/src/stores/interactive.ts` 为空；还原后文件长度与原文件一致）。日志显示：

```
DBG flush:start
DBG flush:before-wait-inflight
DBG flush:after-wait-inflight
DBG flush:draftwork=true
DBG flush:round=0
DBG flush:owner=board_b keys=0 removals=0
DBG flush:skip-continue round=0
...（round 1..7 全部 skip，C 一次都没被选中）
DBG flush:assigned          ← 外层赋值发生在 IIFE 同步执行完（finally 已清空）之后
DBG flush:awaited
...
probe-before-flush2
DBG flush:start
DBG flush:before-wait-inflight   ← 之后再无任何日志 / 心跳（事件循环被微任务自旋占满）
```

解释：这次 flush 没有任何 await（全部是跳过的板），所以 IIFE 同步执行完毕、`finally { draftInFlight = null }` 先执行；随后外层 `draftInFlight = (async…)()` 又把它赋成一份**已 resolve 的 Promise**，之后再没人清空它。下一次 `flushDrafts()` 的 `while (draftInFlight) await draftInFlight;` 每次都 await 同一份 resolved Promise，循环永不退出（微任务饥饿）——这就是任务书第 6 项复现里的「浏览器也一度无法响应」。因此 6A 用假计时器推进防抖，并在收尾 `vi.clearAllTimers()`，既保留红证据又不让基线缺陷占住测试进程。

## 7. 产品工作树当前实现下的信息性快照（非验收）

`t1-drafts-current-product-rerun.log`（2026-11-16 01:29 采样）：**11 passed / 2 failed**。

- 已随 Lead 当前实现转绿：2A、3A、3B、5A、6A（以及全部控制组）。
- 仍红、属于本轮任务书明确要求但当前实现尚未覆盖的两个子场景：
  1. **2B（第 2 项后半）**：`旧正式保存回执仍对已被另一标签页更新过的草稿键发起了 removed: expected 1 to be +0` —— 需要在发 `removed` 前做服务端/本机版本守卫（见 §5.1）。
  2. **4B（第 4 项反向顺序）**：`更早的读取在第二次读取落定前把界面改成了旧稿` —— 迟到的草稿合并路径还没有读取代次校验（见 §5.2）。

以 Lead 实现定稿后的复跑为准；本任务不修改产品代码。

## 8. 已知边界

- 这是 **store 层定向用例**：跨标签页的「另一窗口」用同一 jsdom `localStorage` + 独立 store 实例模拟，服务端草稿存簿是可变 mock（含 rev / keyVersions / removed 版本守卫），**不是**真实浏览器 + 真实后端；浏览器验收由 task-4 的 harness 覆盖。
- 第 2 项浏览器层还要求「重新打开编辑器恢复新文字」的真实 UI 路径，本文件用「新建 store 实例 + `selectBoard` 后 `drafts`」覆盖数据层恢复来源。
- 第 6 项的「flushDrafts 不悬挂」没有写成基线红断言（基线会微任务自旋、任何计时器/超时断言都会一起被饿死），改用 6A 的请求级证据 + 6B 控制组 + §6 探针日志记录；实现侧修复后建议 Lead 在 6A 追加一次「重复进入」断言或由浏览器 harness 覆盖。
