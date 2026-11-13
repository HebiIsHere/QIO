# task-4 · 三处残留文案清理（联网搜索 / 附件关联 / 更新失败）

> 执行者：frontend-copy（子智能体）。worktree：`D:\qio-dev\pm-qio032-integration`，分支 `fix/pm-qio032-integration`，起点 HEAD `551a2e99`。
> 时间：2026-11-14 03:24 – 04:10（Asia/Shanghai）。
> 纪律：只做本地定向验证；未触发 CI、未跑全量 pytest / 全量 npm test、未 git push / 未开 PR / 未合并 / 未打标签、**未 git commit**（Lead 统一按问题拆分提交）。
> 未改 `.github/workflows`、`docs/status.md`、迁移文件、后端；未碰 `stores/interactive.ts`（task-2）、`conversationPreview.ts` / `ConversationView.vue`（task-3）。

## 0. 一屏结论

| # | 位置 | 改动 | 定向测试 |
| --- | --- | --- | --- |
| 1 | 设置 → 模型与服务 → 联网搜索 | 删常驻判据说明；通道行显示真实状态（`data-channel-check-state`）；请求失败改「这次检查失败」并保留重试 | settings-content 23 ✓ / settings-search 16 ✓ / SettingsView 12 ✓ |
| 2 | 互动 → 附件关联成功 | `cardJobSuccessText` 缩成「已关联附件『文件名』」；跨页与当前页同一句、只显示一次 | fix025-attach-notice 13 ✓ / fix025-card-attach-notice 10 ✓ / pm032-canvas-import-batch 21 ✓ |
| 3 | 设置 → 关于与更新 → 更新失败 | 主界面留失败状态 + 下一步；原始错误 / 代理 / 日志进按需展开诊断；恢复与否读结构化 `recovery` | UpdateCard 15 ✓ / uf-updater-stages 8 ✓ / updater 12 ✓ / updaterFlow 10 ✓ |

合计 **10 个文件 / 140 条用例全绿**；`npx --no-install vue-tsc --noEmit` **EXIT=0**。

## 1. 改动文件清单

### 1.1 产品源码（5 个，全部在我的写入范围内）

| 文件 | 改动 |
| --- | --- |
| `frontend/src/views/settings/sections/SearchConfigGroup.vue` | ① 删掉常驻的 `<p class="set-hint">`「已配置」不代表「检查通过」…整段；② 新增 `checkState()`，通道行输出 `data-channel-check-state="unchecked\|ok\|failed\|off"`；③ 请求失败文案 `连接检查请求失败（不代表通道不可用）` → `这次检查失败`（重试保留，原始原因仍在技术详情）；④ 旧后端降级说明改为 `v-if="statusAvailable === false"` 条件渲染（只有真的没有状态接口才解释一次） |
| `frontend/src/components/interactive/AddMenu.vue` | `cardJobSuccessText` 由「已关联附件「X」：<来源说明>；板面关联是本地改动，提交后 QIO 才可见」缩成「已关联附件「X」」；移除不再使用的 `attachmentSourceNote` import；同步更新两处注释 |
| `frontend/src/components/UpdateCard.vue` | 失败行由 `<p>` 改 `<div>`：主行 `[data-update-failure]` 只放人话，下一步 `[data-update-next]` 按 `recovery` 三态给真实动作；原始错误 / 代理 / 日志路径移入 `<details data-update-diagnostics>`（默认折叠）；`backendRecovered` / `backendBroken` 改读 store 的 `recovery` 字段（不再 `message.includes(...)` 猜字符串）；全局异常项的 `diagnostics` 改放 `errorDetail` |
| `frontend/src/services/updater.ts` | 新增 `UpdateRecovery` / `UpdateFailure` / `UpdateFailureError`；`describeUpdateError` 返回结构化 `{kind, message, detail, recovery}`（`message` 不再拼「（原始信息：…）」，原文进 `detail`）；`restoreBackend` 返回 `{recovery, detail}` 而不是拼好的长句；安装失败路径抛 `UpdateFailureError`（人话 + 原始原因 + 恢复结果分开） |
| `frontend/src/stores/updater.ts` | state 新增 `errorDetail` / `recovery`；`_beginOperation` / `_cancelAutoCheckOperation` 一并保存与回滚这两个字段；`check()` / `download()` 的失败分支写入 `described.detail` / `described.recovery` |

### 1.2 测试（9 个）

| 文件 | 改动 |
| --- | --- |
| `views/settings/__tests__/settings-content.test.ts` | 改：状态用例改按 `data-channel-check-state` 断言（不再断言长句）；#23 失败/成功用例加状态断言；请求失败用例改为断言「这次检查失败」+ 配置保留 + 重试真的重发。新增：旧后端无状态接口降级、检查接口 404 不冒充失败 |
| `views/__tests__/SettingsView.test.ts` | 改：无通道用例改为断言「未配置通道不显示检查状态」+ 常驻判据说明已删 |
| `components/interactive/__tests__/fix025-attach-notice.test.ts` | 改：成功文案断言改为精确等值「已关联附件「报告.pdf」」+ 反断言来源/本地改动不再出现。新增：浏览器上传来源下成功提示只有一句、只渲染一次 |
| `components/interactive/__tests__/fix025-card-attach-notice.test.ts` | 只加注释说明卡片内提示与 AddMenu 口径不同（卡片内保留「本地改动 / 提交后可见」，因为卡片没有别的提交状态承接） |
| `components/interactive/__tests__/pm032-canvas-import-batch.test.ts` | 改：跨页完成的两处断言改为精确等值同一句，并断言成功提示只渲染一次 |
| `components/__tests__/UpdateCard.test.ts` | 改：检查失败 / 下载失败用例改为断言主行 + 诊断分离。新增：代理与日志只在折叠诊断里、安装失败已恢复、安装失败未恢复（含全局异常项）、检查失败与安装失败下一步不同 |
| `components/__tests__/uf-updater-stages.test.ts` | 改：两处手工构造失败态改用 `store.recovery` + `store.errorDetail`（不再靠 message 子串） |
| `stores/__tests__/updater.test.ts` | 改：错误分类用例改为断言 `detail` 保留原文、`message` 是人话；检查失败用例加 `errorDetail` / `recovery` 断言。新增：`UpdateFailureError` 不被二次包装 |
| `services/__tests__/updaterFlow.test.ts` | 改：4 个失败用例改用 `failureOf()` 抓 `UpdateFailureError`，分别断言 `message` / `detail` / `recovery` |

## 2. 跑过的命令与结果（逐条）

### 2.1 定向 vitest（10 文件，最终一轮）

~~~powershell
cd D:\qio-dev\pm-qio032-integration\frontend
npx --no-install vitest run `
  src/views/settings/__tests__/settings-content.test.ts `
  src/views/settings/__tests__/settings-search.test.ts `
  src/views/__tests__/SettingsView.test.ts `
  src/components/interactive/__tests__/fix025-attach-notice.test.ts `
  src/components/interactive/__tests__/fix025-card-attach-notice.test.ts `
  src/components/interactive/__tests__/pm032-canvas-import-batch.test.ts `
  src/components/__tests__/UpdateCard.test.ts `
  src/components/__tests__/uf-updater-stages.test.ts `
  src/stores/__tests__/updater.test.ts `
  src/services/__tests__/updaterFlow.test.ts
~~~

结果（完整输出见 `copy-vitest.log`）：

```
 ✓ src/components/interactive/__tests__/fix025-card-attach-notice.test.ts (10 tests)
 ✓ src/views/__tests__/SettingsView.test.ts (12 tests)
 ✓ src/views/settings/__tests__/settings-search.test.ts (16 tests)
 ✓ src/views/settings/__tests__/settings-content.test.ts (23 tests)
 ✓ src/components/interactive/__tests__/pm032-canvas-import-batch.test.ts (21 tests)
 ✓ src/components/interactive/__tests__/fix025-attach-notice.test.ts (13 tests)
 ✓ src/components/__tests__/UpdateCard.test.ts (15 tests)
 ✓ src/components/__tests__/uf-updater-stages.test.ts (8 tests)
 ✓ src/stores/__tests__/updater.test.ts (12 tests)
 ✓ src/services/__tests__/updaterFlow.test.ts (10 tests)

 Test Files  10 passed (10)
      Tests  140 passed (140)
   Duration  32.28s
EXIT=0
```

### 2.2 类型检查

~~~powershell
cd D:\qio-dev\pm-qio032-integration\frontend
npx --no-install vue-tsc --noEmit
~~~

结果：**EXIT=0，无输出**（`copy-tsc.log` 为空文件）。

### 2.3 补丁卫生（只查我改的文件）

~~~powershell
cd D:\qio-dev\pm-qio032-integration
git diff --check -- <我改的 14 个文件>
~~~

结果：**EXIT=0**（无空白错误）。

### 2.4 未跑（按纪律）

- 未跑全量 `npm test` / 全量 `vitest run`；
- 未跑任何后端 pytest（本任务未改后端）；
- 未触发 CI、未 push、未 commit。

## 3. 三处改动的验收对照

### 3.1 联网搜索（SearchConfigGroup.vue）

| 任务要求 | 落地 | 验证 |
| --- | --- | --- |
| 删除常驻的「已配置不代表检查通过」及重复解释 | 整段 `set-hint` 删除；只剩旧后端降级时的一句条件说明 | settings-content「联网搜索通道行显示真实状态」断言 `not.toContain("「已配置」不代表「检查通过」")` |
| 通道行显示真实状态（如「已配置，尚未检查」） | 配置段 + 检查段两段；检查段带 `data-channel-check-state` | 断言 `data-channel-config` = 已配置 / 已填写地址，`data-channel-check-state` = unchecked |
| 请求失败用「这次检查失败」并保留重试 | `save.setNotice("err", "这次检查失败", …, {retry})` | 「检查请求失败」用例断言文案 + 配置保留 + 点重试真的重发一次并转 ok |
| 修改配置后旧检查结果失效 | 既有 `resetCheck()` 保留（未改） | #23 用例：改地址保存后回到 unchecked |
| 失败后配置仍保留 | 未改保存路径 | 同上用例断言输入框值仍是原地址 |
| 旧后端缺少检查接口时仍有准确反馈 | 状态端点 404 → 只显示配置状态 + 一句降级说明；检查端点 404 → 「当前版本暂不提供连接检查」 | 新增两个用例分别覆盖 |
| 旧测试改为验证状态和动作 | 3 个文件的状态断言全部改读 `data-channel-check-state` | 见 1.2 |

### 3.2 附件关联成功（AddMenu.vue）

| 任务要求 | 落地 | 验证 |
| --- | --- | --- |
| 缩成「已关联附件『文件名』」 | `cardJobSuccessText` 只返回这一句 | fix025-attach-notice 精确等值断言 |
| 来源信息保留在已有的材料详情 | `FileCardMaterialPanel.vue` 的 `sourceNote` 未改（本来就在「材料详情」里） | 反断言：成功提示不含「工作区材料源文件 / 浏览器上传」 |
| 跨页完成与当前页完成同一句、只显示一次 | 两条路径都走 `cardJobSuccessText` | pm032-canvas-import-batch 两处跨页用例改为精确等值 + 断言只渲染 1 个提示节点 |

**边界说明（重要）**：`BoardCard.vue` 卡片内的成功提示**没有**跟着缩（仍保留「…；板面关联是本地改动，提交后 QIO 才可见」）。理由：卡片内没有别的提交状态承接这句话，删掉会让用户以为关联已经提交给 QIO；而 AddMenu 的提示对应的是「刚建的新卡」，卡片的提交状态就在卡上。已在 `fix025-card-attach-notice.test.ts` 加注释固化这个差异。

### 3.3 更新失败（UpdateCard.vue / services/updater.ts / stores/updater.ts）

| 任务要求 | 落地 | 验证 |
| --- | --- | --- |
| 主界面保留可理解的失败状态和实际可用的下一步 | `[data-update-failure]` 人话 + `[data-update-next]` 按 recovery 三态给动作 | UpdateCard 三个新用例分别覆盖检查失败 / 安装失败已恢复 / 安装失败未恢复 |
| 原始错误、代理配置、日志路径移到按需展开的诊断 | `<details data-update-diagnostics>` 默认折叠，含「原始错误：…」+ 代理 + 日志 | 断言 `open` 属性不存在、诊断里含原文、主行不含原文 |
| 分别验证检查失败、安装失败和后端恢复失败 | 三个用例 + updaterFlow 的 4 个编排用例 | 见 1.2 |
| 安装和重新打开的实际时序沿用现有流程 | 未改 `downloadAndInstall` 的编排顺序（check → download → prepare → install → restore） | updaterFlow「顺序必须是 check → download → 结束后端 → install」仍绿 |
| Windows 真机结论有证据后再更新 | 未改任何真机结论文案 | — |

## 4. 仍未验证的边界

1. **未做界面截图 / 真机视觉复核**：本节点无浏览器运行条件，结论基于源码 + jsdom 定向单测。建议 Lead 在验收时按截图过一遍：① 设置 → 模型与服务 → 联网搜索通道行；② 互动页添加文件后的成功提示；③ 设置 → 关于与更新 的失败态（含展开诊断）。
2. **未验证真实 Tauri 壳里的更新失败路径**：`UpdateFailureError` 的 `recovery` 依赖 `qio_restore_backend` 的真实返回；单测用替身覆盖了 restored / unrecovered 两种，真机未跑。
3. **未验证旧后端（无 `/api/settings/search/status`）的真实联调**：单测用 404 替身覆盖，未起旧版后端实测。
4. **未跑全量前端测试**：只跑了 10 个定向文件；`stores/interactive.ts` 在本轮被 task-2 并发修改，我未跑其全量相关用例（task-2 自己负责）。
5. **`QIcon` 在 SearchConfigGroup.vue 里是未使用的 import**（HEAD 基线就有，非本次引入），未清理以免扩大 diff。

## 5. 与其它成员的交叉

- 执行期间 `frontend-46` 正在改 `stores/interactive.ts`（新增 `loadedBoardContentId` / `boardContentIsLoaded` 门禁）。该改动一度让 `pm032-canvas-import-batch`（20 failed）与 `fix025-attach-notice`（7 failed）全红——根因是测试夹具直接赋值 `store.board` 而不走 `load()`，`loadedBoardContentId` 恒为空。已通过 `send_message` 把现象、根因定位（`stores/interactive.ts:6093` 的失败文案 + `:6075/:6109` 的门禁）与建议同步给 `frontend-46`；其修正落定后我复跑两个文件均转绿（21 ✓ / 13 ✓）。
- 我**未修改** `stores/interactive.ts`，也未修改 `frontend-46` 的任何文件。
