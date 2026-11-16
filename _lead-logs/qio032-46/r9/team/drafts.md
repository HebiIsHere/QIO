# Task 2 报告（drafts）—— 移除跨标签页草稿标记并清理旧键

工作树：`D:\qio-dev\q46-r9`（分支 fix/qio032-46-r9，基线/验收 SHA 645e5a1f）
红基线：`D:\qio-dev\_q46-r9-baseline`（跑完已用 `git checkout -- <文件>` 还原，工作区干净）
共享任务：task-2（owner=drafts）

## 结论摘要

- **A 删除标记路径：完成。** 跨标签页草稿同步标记的整条路径（键、类型、读取、记录、清除判定、同步成功后的写入）已删除，未留未使用符号（vue-tsc 通过）。
- **B 旧键清理：完成。** 落点在 `useInteractiveStore` setup 顶部（创建 store = 初始化草稿存储），只 `removeItem("qio.draft.sync-marker.v1")` 一次/幂等；不读值、不打正文日志、存储异常静默继续。测试证明真实未同步草稿、聊天草稿、正式内容都不受影响。
- **C 保留项核对：** `requestDraftClear` 等正式保存确认、按板记账/版本、后端逐键校验、真实草稿恢复记录都保留；**`expectLocal` 重读核对保留、未简化**（依据见下）。
- **D 测试：** 在原文件 `q46-r8-drafts-protect.test.ts` 上扩展（未新建文件），新增 7 个 r9 用例；并把原 2B 改写成「客户端不再主动阻止、后端逐键版本守卫拒绝、界面如实 error」的新口径（见“行为变化”）。
- **E 证据：** 红基线 3 failed / 10 passed；产品绿 13 passed；相邻 q46-r7-* + q46-r8-drafts-queue + f01-drafts-two-windows 共 8 文件 37 passed；`npx vue-tsc --noEmit` exit 0。

## 文件清单

| 文件 | 改动 |
| --- | --- |
| `frontend/src/stores/interactive.ts` | 只动草稿区：新增遗留键常量 + store 初始化清理；删除标记读写/判定与调用；expectLocal 注释写明保留依据 |
| `frontend/src/stores/__tests__/q46-r8-drafts-protect.test.ts` | 新增 r9 用例 4A–4G（7 个）；改写 2B；导入 `writeCardLocalDraft`；新增 CARD_A/KEY_A 常量 |
| `_lead-logs/qio032-46/r9/red/red-drafts-marker-task2-baseline.log` | 红基线日志 |
| `_lead-logs/qio032-46/r9/green/green-drafts-marker-task2-vitest.log` | 本文件绿灯日志 |
| `_lead-logs/qio032-46/r9/green/green-drafts-marker-task2-adjacent.log` | 相邻回归日志 |
| `_lead-logs/qio032-46/r9/green/green-drafts-marker-task2-vue-tsc.log` | 类型检查日志（空 = 通过） |
| `_lead-logs/qio032-46/r9/logs/probe-2b-reopen-visibility-after-blocked-removal.log` | 2B 探测：后端拒绝删除后重开可见性的实测日志 |

## A. 删除了哪些符号 / 分支（interactive.ts）

- `const DRAFT_SYNC_MARKER_STORAGE_KEY`、`type DraftSyncMarkerBook`
- `readDraftSyncMarkers()`、`recordDraftSyncMarker()`、`draftServerVersionAdvancedElsewhere()`
- `flushDrafts` 成功回执里「记录本键已同步到的服务端版本/正文」的 `recordDraftSyncMarker(...)` 调用块
- `clearDraft` 里「`!preservedLocal && cardId` → 读共享标记 → 版本更新则 `preservedLocal = true`」的整段分支
- 没有留下未使用的常量/函数/类型（vue-tsc `--noEmit` 通过）

新增（仅用于清理旧数据，不再读/写语义）：
- 模块级 `LEGACY_DRAFT_SYNC_MARKER_STORAGE_KEY = "qio.draft.sync-marker.v1"`（注释说明该机制已删除）
- store setup 顶部：
```ts
  try {
    globalThis.localStorage?.removeItem(LEGACY_DRAFT_SYNC_MARKER_STORAGE_KEY);
  } catch { /* 旧标记不参与任何新逻辑：删不掉不影响启动与草稿保存 */ }
```
  - 落点：`useInteractiveStore = defineStore("interactive", () => { ... })` 的第一段（任何页面使用互动 store 都会经过；测试里 `useInteractiveStore()` 即初始化）。
  - 幂等：本来没有该键时 removeItem 无副作用；每次新建 store 再执行一次也安全。
  - 只动这一个键：不枚举、不清理其它 `qio.draft.*`；不读取值（旧值含正文）、不打印内容。
  - 存储异常：`globalThis.localStorage` 访问与 removeItem 都在 try 内，异常静默继续（沿用草稿存储既有容错口径）。

## B. 保留并核对的保护

- `requestDraftClear()` 仍只登记，真正清除由 `applyDraftClearsForSave` 在「回执证明本次正式编辑真的落库 + 登记仍有效」后执行（3A/3B/3C/3D 覆盖撤销/删除撤销/保存失败/正常完成）。
- 登记后继续输入新版本：`hasContent && keySeq !== entry.version` 作废登记（4C）。
- 按板草稿簿子：回执处理按 owner 板面写回（4F，保存期间切板）。
- 后端逐键版本校验：`flushDrafts` 仍携带 `keyVersions`，服务端按版本拒绝过期删除（2B）。
- 真实草稿恢复记录：本机 `card-local:<id>` 记录身份重读（expectLocal）、版本守卫、失败可见与重试（2A/4D/4G）。

## C. expectLocal 结论：**保留，不简化**

任务要求先验证 ① 同一窗口内迟到回执、② 本机清除失败重试、③ 重启恢复三条路径；三条都有定向用例且为绿：

- ① 4C「同一窗口内旧保存响应延迟时重新编辑同一张卡」：旧回执不清新草稿（由登记版本守卫 + 新输入代次覆盖，绿）。
- ② 4D「本机清除写入失败后重试」：失败如实可见、磁盘仍是编辑草稿，重试先补写 cleared 依据再完成清除（绿）。
- ③ 4G「退出重开后恢复未同步草稿」：新 store + 同一 localStorage 仍恢复未同步草稿（绿）。

但这三条并不能证明 `expectLocal` 可以收掉：它保护的是**本机恢复记录本身**，不是被删掉的共享标记。2A 用例（另一标签页的新稿还没同步、旧正式保存回执到达）正是 `expectLocal` 的身份重读在兜底——登记时本页没有记录（version/fingerprint 为 null），回执到达时另一标签页已写下新记录：若删掉重读核对而直接写 cleared，就会覆盖那份唯一的恢复副本（r8 已修复的缺陷复活）。没有任何其它机制为这条路径兜底，因此**保留**，并在 `clearDraft` 的 expectLocal 注释里写明了依据。

## D. 测试扩展（q46-r8-drafts-protect.test.ts，13 tests）

新增 r9 用例（断言只看 store 公开状态、`readCardLocalDraft`/localStorage、`api.saveDrafts`/`api.saveBoardState` 调用参数）：

- **4A** 旧标记（含可识别旧正文）+ 真实未同步草稿 + 正式内容各一份 → 初始化 store 后仅旧标记键消失；真实草稿内容/版本、无关聊天草稿键、正式内容、服务器草稿事实都不变；旧标记正文不会被恢复进编辑器。
- **4B** 草稿同步成功后不再写入旧标记键（连续两版同步都为 null）。
- **4C** 同一窗口内旧保存响应延迟时重新编辑同一张卡：新草稿与恢复副本保留、旧登记作废、不发 removed。
- **4D** 本机清除写入失败（模拟 QuotaExceededError）→ 草稿保留、失败可见；重试补写 cleared 依据后由服务器确认清除（服务端草稿清掉、磁盘不再留旧正文）。
- **4E** 正式保存等待期间另一张卡写入草稿：旧回执只处理自己的键，另一张卡恢复副本保留且不串板。
- **4F** 保存期间切板：回执按原板清理（B 的本机依据变 cleared），当前 A 的草稿/正文/本机副本不动，不对 A 发 removed。
- **4G** 退出重开后恢复未同步草稿（不再依赖旧标记）。

既有用例保持：2A（expectLocal 保护未同步新恢复副本）、3A/3B/3C/3D（撤销/删除撤销/写入失败控制组/正常完成清理）。

## E. 逐条命令与结果（红→绿）

红基线（把更新后的测试文件复制到 `_q46-r9-baseline` 跑，跑完 `git -C D:\qio-dev\_q46-r9-baseline checkout -- frontend/src/stores/__tests__/q46-r8-drafts-protect.test.ts` 还原；已确认基线 `git status --short` 为空）：

```
cd D:\qio-dev\_q46-r9-baseline\frontend
npx vitest run src/stores/__tests__/q46-r8-drafts-protect.test.ts
=> Test Files 1 failed | Tests 3 failed | 10 passed
   失败：2B（旧实现被标记兜住、根本不发删除请求，无法验证后端版本守卫）
         4A（初始化后旧标记键仍存在）
         4B（同步成功后仍在写旧标记键；实测写入 {"board_b":{"card:card_b":{"version":4,"text":"同步到服务端的草稿"}}}）
```
日志：`_lead-logs/qio032-46/r9/red/red-drafts-marker-task2-baseline.log`

产品工作树（实现后）：

```
cd D:\qio-dev\q46-r9\frontend
npx vitest run src/stores/__tests__/q46-r8-drafts-protect.test.ts
=> Test Files 1 passed | Tests 13 passed
```
日志：`_lead-logs/qio032-46/r9/green/green-drafts-marker-task2-vitest.log`

相邻定向回归（q46-r7 全部 6 个 + q46-r8-drafts-queue + acceptance-state-recovery/f01-drafts-two-windows）：

```
npx vitest run src/stores/__tests__/q46-r7-attachment-state.test.ts src/stores/__tests__/q46-r7-cancel-recovery.test.ts src/stores/__tests__/q46-r7-drafts.test.ts src/stores/__tests__/q46-r7-impact-ownership.test.ts src/stores/__tests__/q46-r7-save-limit.test.ts src/stores/__tests__/q46-r7-task-isolation.test.ts src/stores/__tests__/q46-r8-drafts-queue.test.ts src/acceptance-state-recovery/f01-drafts-two-windows.test.ts
=> Test Files 8 passed | Tests 37 passed
```
日志：`_lead-logs/qio032-46/r9/green/green-drafts-marker-task2-adjacent.log`

类型检查：

```
npx vue-tsc --noEmit
=> exit 0（无输出）
```
日志：`_lead-logs/qio032-46/r9/green/green-drafts-marker-task2-vue-tsc.log`

## 行为变化与已知限制（Lead 已决定接受）

1. **原 2B 用例改写（有意，非放宽安全性）**：删除标记后，客户端不再能知道「另一标签页刚同步过更新的版本」。旧正式保存回执会带本页已知的旧 `keyVersions` 发出一次 removed，由**后端逐键版本守卫拒绝**（`blockedRemovals`），服务端更新的草稿与版本原样保留；界面如实显示「删除没有被服务器确认」（error），不假装已删除。测试断言：服务端草稿/版本不变、删除请求携带的版本确为旧版本、界面状态为 error。
2. **已知限制（探测已证实；Lead 2026-11-16 决定接受，不追加回滚机制）**：上述 2B 路径里，客户端在收到「后端拒绝」前已经写下本机 `cleared` 依据；后端拒绝后没有回滚该记录。于是**重开应用后**，这张卡上「另一标签页已同步的新草稿」在编辑器里不再恢复显示（数据仍在服务端、未被删除；只是本机恢复视图把它当成待清除依据隐藏）。实测日志：`logs/probe-2b-reopen-visibility-after-blocked-removal.log`（用例内临时断言 `reopenedTab.drafts[KEY_B]` 得到 `undefined` 后已移除，不留在测试文件中）。
   - 定性：**删除跨标签页机制后的「本机恢复可见性」降级，无数据丢失**（服务端草稿与版本原样保留，且后端逐键版本守卫会持续拒绝过期删除）。
   - Lead 决定：本轮桌面端已是**全机单实例、同一 QIO 窗口只有一个 WebView**，跨标签页不再是产品运行形态；本轮任务要求就是删除该共享标记。因此接受为已知限制，**不追加**「拒绝删除后按版本回滚本机 cleared 依据」之类机制（避免超出任务书、引入新的并发语义），由 Lead 在 `docs/status.md` 与交付报告中如实记录。

## 未验证项与风险

- 以上全部为 **store 层定向用例**：跨标签页用同一 jsdom `localStorage` + 独立 pinia/store 模拟，不是真实浏览器双标签页，也不是真实后端；浏览器/真实后端验收由 Lead/集成任务覆盖。
- 初始化清理依赖「使用互动 store 即初始化」：真实应用里互动页/事件层都会创建该 store；纯 `interactive/drafts.ts` 工具函数（测试直接用）不会触发清理，但它们也从不读旧标记。已 grep 确认：全前端只剩清理处引用该键，没有任何读标记代码。
- 旧键值里含草稿正文：清理只 removeItem、不读取、不记录；旧值短暂存在期间不会被本模块输出到日志。
- 并发：async-owner 同期在改 interactive.ts（submit/cancel 区）；本次只用 `edit` 做小范围唯一字面替换，未遇到 FS_STALE_VERSION；最终绿灯/类型检查是在当前合并工作区上跑的，但 Lead 提交前仍需整体 review 最终 diff 并重跑受影响用例。
- 未跑全量测试、未触发 CI、未 git add/commit/push、未改 docs/status.md。
