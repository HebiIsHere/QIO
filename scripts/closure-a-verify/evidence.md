# closure-A / 反例 R4 证据：本机删除重试 ≠ 整份草稿清除

> 本文件的所有数字都来自 worktree `qio-cl-a`（分支 `wt/closure-a-drafts`，基线 `1da2172`）的实跑输出；
> 原始日志与 `scripts/closure-a-verify/` 同目录：`baseline-red.txt`、`unit-output.txt`、`component-output.txt`、
> `regression-interactive.txt`。

## 0. 层次标注（先说清证据的边界）

| 层次 | 本文件做了什么 | 谁负责剩下的 |
| --- | --- | --- |
| 纯逻辑 / 真实 localStorage | `drafts.ts` 目的化原语的单测（14 项） | — |
| **组件级重开等价物** | 真实 `BoardCard.vue`（内部真实 `CardDraftHint.vue`）真实按钮 + 真实 store + **同一份本机 localStorage + 新建 pinia/store 实例** | **真浏览器进程关闭重开由验收角色 D 负责**（不在本文件断言的范围内） |
| 集成态（含 store 接线） | 临时应用 lead 的 `store-lead-r4-only.diff` 跑绿后 `git restore` 还原 | store 接线作者是 lead，我的分支不提交该文件 |

## 1. 反例 R4 与修复点

- 触发：本机/服务器两份稿冲突 → 用户选「用服务器上的」→ `removeItem` 临时失败 → 存储恢复后点重试 → 关闭重开。
- 基线实际：重试写下一条 `kind=cleared` 记录（整份草稿清除的依据）。重开后用户选择保留的服务器稿变空、
  草稿集合不再含该键，服务器那份也会被删除。
- 根因：`stores/interactive.ts` 的 `pendingLocalRemovals` 只记了「版本」，丢掉了用户决定的真实目的，
  重试路径一律按「补写 cleared 依据」（整份草稿清除）处理。
- 本路（A）交付：`drafts.ts` 把目的显式化（`LocalRemovalPurpose = "remove-local-copy" | "clear-draft"`），
  并给出按目的分派、且保留三种版本守卫口径的重试原语；store 侧接线由 lead 按同一约定落地。

## 2. 红线：基线 store（未接线）

```
powershell -NoProfile -File scripts/closure-a-verify/verify.ps1 -BaselineRed
  ---- src/interactive/__tests__/closure-a-local-removal-purpose.test.ts (exit=0) ----
   Test Files  1 passed (1)   /   Tests  14 passed (14)
  ---- src/components/interactive/__tests__/closure-a-r4-reopen.test.ts (exit=1) ----
   Test Files  1 failed (1)   /   Tests  2 failed | 5 passed (7)
  基线红复现成功：2 failed | 5 passed，且含 kind=cleared 证据
```

两条失败的原文（`baseline-red.txt`）：

```
×【组件/DOM】重试只删本机冗余副本：磁盘上不许出现 cleared 依据
  AssertionError: 本机冗余副本应当被删掉（基线：留下了一条 kind=cleared）: expected { text: '', …(5) } to be null
  Received: { "boardId": "board_default", "kind": "cleared", "seq": 1, "text": "", "updatedAt": …, "version": 2 }

×【组件/DOM + 关闭重开】重开后服务器稿、输入框、请求集合都保留正确正文
  AssertionError: ★重开后用户选择保留的服务器稿必须还在: expected '' to be '服务器那份草稿，用户选择了保留'
```

这两条正好对应反例的两个症状：**写入 `kind=cleared`** 与 **重开后服务器稿变空**。

## 3. 绿线：临时应用 lead 的「仅 R4」store 补丁

```
powershell -NoProfile -File scripts/closure-a-verify/verify.ps1 -StorePatch <store-lead-r4-only.diff>
  已临时应用 store 补丁
  ---- 单元用例 ----  Test Files 1 passed (1)  /  Tests 14 passed (14)
  ---- 组件用例 ----  Test Files 1 passed (1)  /  Tests  7 passed (7)
  全绿：单元 14 + 组件 7 用例
  已还原 frontend/src/stores/interactive.ts（未提交 store 改动）
```

补丁来源：`D:\qio-dev\_briefs\store-lead-r4-only.diff`（lead 提供，只含 R4 的 8 个 hunk）。脚本在
`finally` 里用 `git restore` 还原；跑完后 `git status --short` 只剩我自己的文件，store 未被提交。

## 4. 回归（未改 store、只带我的 `drafts.ts` 增量）

```
cd frontend; npx vitest run src/interactive src/components/interactive src/stores
  Test Files  1 failed | 88 passed (89)
  Tests       2 failed | 833 passed (835)

唯一 2 条失败 = 本文件第 2 节的**故意基线红**（closure-a-r4-reopen.test.ts），其余 88 个文件全绿。
```

`npx vue-tsc --noEmit` 退出码 0（在有上述两个新测试文件的状态下跑的）。

## 5. store 接线约定（本次由 lead 落地；与我的 `drafts.ts` 原语同口径）

1. 登记表形状（替换原来的 `Map<string, number | null>`）：
   `pendingLocalRemovals: Map<string, { purpose: LocalRemovalPurpose; expectVersion: number | null; guard: "version" | "absent" | "object" }>`
2. 登记点与目的：
   - `resolveDraftConflict(cardId, "server")` 清理本机候选 → `purpose: "remove-local-copy"`；
   - `restoreLocalCardDrafts`（无主记录清理 / 服务器已有同样一份）→ `remove-local-copy`；
   - `flushDrafts` 服务器保存成功后的本机清理 → `remove-local-copy` + `guard: "absent"`（登记时本来就没有记录）；
   - `clearDraft` 写 cleared 依据失败 → `purpose: "clear-draft", guard: "version"`。
3. 重试分派：`remove-local-copy` 只删记录（`absent` → `removeCardLocalDraftIfUnchanged`；
   `version` → `removeCardLocalDraft(cardId, expectVersion)`；`object` → `removeCardLocalDraft(cardId)`），
   **绝不写 cleared**；`clear-draft` 才走 `ensureCardLocalClear` 幂等补写。
4. 我的 `drafts.ts` 原语（`retryLocalRemovalByPurpose` / `LocalRemovalIntent` / `cardLocalRecordRole` …）
   与上述规则等价，供将来对齐；**既有 `removeCardLocalDraft` / `removeCardLocalDraftIfUnchanged` /
   `ensureCardLocalClear` 的语义与签名未改**（既有 9 + 8 项 drafts 测试仍全绿）。

## 6. 未完成 / 未验证 / 已知边界（如实列出）

1. **真浏览器进程关闭重开未做**：本文件是组件级等价物（新 store 实例 + 保留 storage）；由 D 负责真进程验收。
2. **闭态重试入口不可见（观察项，不在我的文件所有权内）**：`BoardCard.vue` 只在「有未决冲突」时于关闭态渲染
   `CardDraftHint`；用户若在关闭态点「用服务器上的」，删除失败后没有可见的重试入口（需先点「编辑」）。
   本用例按「编辑器打开」的真实路径点按钮。修法在 `BoardCard.vue`（C/lead 的文件），我没有改。
3. **store 未消费我的新原语**：lead 用既有 API 实现了同等语义（双份实现，语义一致）；我的新原语目前只有
   单测覆盖。若要对齐需单开一次接线（我已在汇报里说明）。
4. **旧登记形状（只有版本号）的兼容只在原语层**：`normalizeLocalRemovalIntent` 会判为 `"unknown"` 并拒绝
   两种破坏性动作；store 侧的登记点已全部带 purpose，所以这条路径在当前代码里不会被触发。
5. 后端未改动，未跑后端测试（R4 是纯前端草稿层问题）。
6. 我未 push、未合并任何分支，未提交 `stores/interactive.ts`。
