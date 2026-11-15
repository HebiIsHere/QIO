# task-2 · R7 store 回归测试（草稿归属 / 版本 / 清理 + 后台冲突上限）

> 第七轮修复轮 · 交付物是**测试**（不改产品代码）。基线工作树 `D:\qio-dev\pm-qio032-integration`，开工 HEAD `0dbc387f1ecdc2dfa21f658ce16dbe07a6fa9b3d`。
> 红证据在 **HEAD 的独立 worktree**（`D:\qio-dev\_tmp-r7-baseline-spec-drafts`，`git worktree add --detach 0dbc387f`，node_modules 用 junction 指向主工作树）上采集，避免被本轮实现改动污染。

## 1. 交付文件

| 文件 | 用例数 | 内容 |
| --- | --- | --- |
| `frontend/src/stores/__tests__/q46-r7-drafts.test.ts` | 4 | A1/A2（双窗口并发、草稿版本/内容归属）+ B1/B2（正式保存后台成功清旧草稿） |
| `frontend/src/stores/__tests__/q46-r7-save-limit.test.ts` | 2 | C1（后台冲突计入自动恢复上限）+ C2（前台不切板时序同样 ≤4 次） |

断言只落在 **公开 API 与真实请求参数** 上：

- `api.saveDrafts(boardId, drafts, {removed, baseRev, keyVersions})` 的实际参数；
- `api.saveBoardState(boardId, candidate, reason)` 的实际参数（目标板面 / 候选 seq / 卡片集合）；
- store 公开状态：`drafts`、`draftConflicts`、`draftConflictFor`、`draftStateFor`、`pendingDraftClearKeys`、`draftSaveStatus/draftSaveError`、`saveStatus/saveError/dirty`、`pendingImpact`、`board`。

没有断言任何内部 Map / 变量名（`draftBoardByKey`、`draftBooks`、`saveRecoveryRoundByBoard` 等一概不碰）。

## 2. 运行命令

```powershell
cd D:\qio-dev\pm-qio032-integration\frontend
npx vitest run src/stores/__tests__/q46-r7-drafts.test.ts src/stores/__tests__/q46-r7-save-limit.test.ts
```

- 红（HEAD `0dbc387f`，独立 worktree）：`red-r7-store-baseline-0dbc387f.log` → **5 failed / 1 passed**。
- 绿（当前工作树，含本轮实现）：`D:\qio-dev\pm-qio032-integration\_lead-logs\qio032-46\r7-specs\green-r7-drafts.log` → **6 passed / 6**。

## 3. 用例 ↔ 任务书编号

| 用例 | 任务书条目（对照浏览器验收任务书编号） | 覆盖点 |
| --- | --- | --- |
| A1 | 一（≈ 浏览器验收第 1 项） | 双窗口并发：A 的草稿请求必须带 **A 自己读到的** `baseRev`（1，而不是 B 的 2）与逐键版本；同键冲突两份都保留；B 的草稿/冲突/状态不受影响 |
| A2 | 一（补充：两块板面先后有草稿写入在途） | A、B 各自请求只带自己板面的键与版本；A 的回执（rev 10）不进入 B 的请求（B 仍是 5）；最终两块板面的正文/状态互不串 |
| B1 | 二（≈ 浏览器验收第 2 项） | B 完成编辑 + `requestDraftClear`，PUT 在飞时切 A、放行成功：B 的待清除登记被消化、**真的向 B 发一次 `removed`**、服务端无该键；A 的草稿保持原状 |
| B2 | 二（「回执前又输入新版本」） | 登记之后用户又输入新版本：新草稿保留，旧回执不清除（登记被版本守卫作废、没有 `removed` 请求） |
| C1 | 二/三（≈ 浏览器验收第 3 项「后台冲突计入上限」） | B 保存后立刻切 A、409 在 A 上到达，共 4 轮：每轮都计入 B 的整轮恢复次数；同轮 PUT 恰好 4；达到上限后切板往返/重读不再自动发请求；回 B `saveStatus=error`、`dirty=true`、候选 2 张完整、`pendingImpact=null`；公开重试入口 `saveNow()` 只发 B 的 1 次 PUT 并成功，A 收到 0 次 |
| C2 | 二（前台控制组） | 不切板连续 409：同样恰好 4 次 PUT 后停止（已有行为的回归护栏） |

## 4. 基线（HEAD `0dbc387f`）红证据 · 逐条实际/期望

命令：`npx vitest run src/stores/__tests__/q46-r7-drafts.test.ts src/stores/__tests__/q46-r7-save-limit.test.ts`（在 `0dbc387f` 的独立 worktree 里）
结果：`Tests 5 failed | 1 passed (6)`，原始输出见 `red-r7-store-baseline-0dbc387f.log`。

| 用例 | 失败的断言（原始消息） | 实际值 | 期望值 | 对应产品缺口 |
| --- | --- | --- | --- | --- |
| A1 | `baseRev 用了别的板面的存簿版本（A 实际读到的是 1）` | `baseRev = 2`（读 B 时全局 rev 被推到 2） | `baseRev = 1` | 草稿存簿版本（rev / keyVersions）是**全局**的：切到 B 读到 rev2 后，回到 A 写草稿会带 A 从未读过的 baseRev=2 → 服务端认为「无并发改写」而静默覆盖另一窗口刚写进 A 的内容 |
| A2 | `B 的 baseRev 用了 A 的存簿版本（B 实际读到的是 5）` | `baseRev = 10`（A 的回执 rev 被 B 的请求用上） | `baseRev = 5` | 同上：一块板面的回执/读取会推进另一块板面请求的基线版本，A、B 并发的草稿写入互相串版本 |
| B1 | `后台成功之后没有真的向 B 发一次草稿清除（removed）` | 0 次 `removed`（且 `pendingDraftClearKeys` 仍含 `card:card_b`） | ≥1 次发往 B 的 `removed` | 正式保存的回执落在「非当前板面」时走的是提前 return 的后台分支，**没有消化 `pendingDraftClears`**：B 的旧草稿永远清不掉（服务端仍有该键，刷新/切回就复活） |
| B2 | `登记之后又输入的新版本没有被版本守卫作废` | `pendingDraftClearKeys = ['card:card_b']` | 不含 `card:card_b`（登记作废、草稿保留 "第二版"） | 同上：后台成功不处理登记，版本守卫也就没有机会把「登记后又被改过」的旧登记作废（草稿内容本身在基线侥幸保留，但登记一直挂着） |
| C1 | `达到上限后切板往返 / 重读又自动发了保存` | `PUT = 5`（4 轮后台 409 结束后再 B→A→B + 过防抖又发了一次） | `PUT = 4` | 409 在**切走之后**到达时只把恢复计划塞进原板会话，**没有调用整轮计数**（`noteRecoveryConflict` 只在当前板面的恢复分支里），所以后台冲突永远不计入上限；切板往返可让 B 的写入无界 |
| C2 | （通过） | 4 次 PUT 后停止 | 4 | 前台时序在第五轮已修复，本用例是回归护栏；基线即绿 |

> 说明：C1 的循环只驱动 4 轮；基线在「第 4 次之后再次 B→A→B」时发出第 5 次写入，说明计数没有生效（若继续驱动会继续增发，即无界）。

## 5. 修复后绿证据（当前工作树，含 Lead 本轮实现）

- `green-r7-drafts.log`：`Test Files 2 passed (2) / Tests 6 passed (6)`，逐例：

```
✓ q46-r7-drafts.test.ts (4 tests)
   ✓ A1 本窗口写 A 的草稿：baseRev / 逐键版本必须是 A 自己读到的版本，同键冲突两份都保留，B 不受影响
   ✓ A2 两块板面的草稿写入先后返回：各自请求的板面 / 版本 / 键互不串
   ✓ B1 B 的正式保存后台成功：B 的旧草稿被清掉（登记消化 + 真的发 removed），A 的草稿保持
   ✓ B2 B 在回执前又输入新版本：新草稿保留，不被后台成功的旧回执清掉
✓ q46-r7-save-limit.test.ts (2 tests)
   ✓ C1 B 保存后立刻切 A、409 在 A 上到达：每轮都计数，总 PUT ≤ 4，切板往返不增加，回 B 失败态保留候选，重试只发 B 一次
   ✓ C2 前台不切板时序：同样最多 4 次 PUT，之后不再自动重发
```

## 6. 测试口径（避免假红/假绿的关键点）

1. **草稿按板面显示**：本轮实现里 `store.drafts` / `draftConflicts` / `draftStateFor` 只反映当前板面的簿子。因此凡是断言「B 的草稿内容/状态」都在 `await store.selectBoard(BOARD_B)` 之后；在 A 上只断言「没有向服务端为 B 的键发 `removed` / 没有 `saveDrafts` 目标为 B」这类跨板可观察事实。
2. **mock 的服务端存簿必须是可变且自洽的**：`saveDrafts` mock 会把 `drafts` / `removed` 应用到「服务端存簿」，`fetchBoardState` 每次都返回当前存簿（含 `rev` / `keyVersions`）。否则清除落地后重读还会合并回旧稿，造成假失败（B1 的初版就是这个问题）。
3. **每个板面的读取都带 `rev` / `keyVersions`**：A2 早期红在「B 的 keyVersions undefined」，原因是 mock 的 B payload 没带逐键版本；修复方式是 bookA/bookB 都带 rev + keyVersions，且每次读取都返回对应板面的书。
4. **C1 用真实计时器**（450ms 保存防抖）：串行 4 轮切板 + 重试整例约 5.4s，因此给该 `it` 显式 `20_000` 超时（未改 vitest 配置）。C2 用 `vi.useFakeTimers()` + `advanceTimersByTimeAsync` 驱动。
5. **同一时刻只允许一个草稿请求在飞**：A2 利用这个既有约束（A 的请求挂起后 B 的 flush 会等它），断言 A 的回执（rev 10）不会进入 B 的请求。

## 7. 发现的产品缺口 / 风险

1. **已修复（基线红 → 当前绿）**：草稿存簿版本全局化导致的跨板 baseRev/逐键版本串板（A1/A2）；后台成功不回填服务端旧草稿清理（B1/B2）；后台 409 不计入整轮上限导致切板往返可无界写入（C1）。
2. **实现中间态观察（已消失，建议 Lead 复核）**：在 Lead 实现尚未完成时，A2 曾出现「A 的草稿在已保存成功之后被再写一次」的重复 PUT（3 次调用：A、B、A）。最终实现下本文件已绿，但我的 A2 断言只要求「每块板面至少 1 次、且参数只属于自己板面/版本」，**没有**把「同一候选只写一次」写成硬断言。建议在最终提交前顺手确认：A2 场景下 `saveDrafts` 调用次数不应多于「两块板面各 1 次」（若实现允许重复幂等写，请明确口径）。
3. **没有覆盖到的**：真实浏览器 / 真实后端落盘（本轮 store 层用 mock 的服务边界，只证明请求参数与前端状态）；跨浏览器多窗口（另一窗口的写入是 mock 回执模拟）；`vue-tsc --noEmit` 未跑（留给 Lead 收尾统一跑，避免与进行中的实现编辑冲突）。

## 8. 下一步建议

1. 最终提交前把这两个文件加入定向测试清单，与 `pm032-board-save-isolation.test.ts` 一起跑（草稿按板面簿化会触及既有 26 例，建议同批复跑）。
2. 若 Lead 调整了「后台 409 计数」的位置（计数在 `handleSaveConflict` 统一做，还是后台分支单独做），C1 的断言仍然只依赖可观察结果（PUT 次数 / 失败态 / 重试次数），无需改测试。
3. 建议把「`serverDraftRev` / `serverDraftKeyVersions` 已按板面记账」写进 `docs/status.md` 的「已知待补」移除项，并注明证据文件名（本目录的 red/green 日志）。