# #46 · 离开互动页后继续上传和关联 — 证据（frontend-46）

- 工作树：`D:\qio-dev\pm-qio032-integration`（分支 fix/pm-qio032-integration，基线 7445170d）
- 范围：frontend 只有 4 个文件被改（2 源码 + 2 定向用例），未切分支 / 未 commit / 未新增依赖 / 未改版本号
- 结论：**完成**。上传、等待附件可用、最终关联已移到 store 执行器（生命周期长于页面组件）；
  回执按「原板面 / 原卡 / 原关联目标 / 本次尝试」核对；返回互动页显示真实进度 / 已完成 / 具体失败原因；
  定向用例全绿、类型检查通过。真实应用与 Windows 原生拖放未在本环境验证（见「限制」）。

## 一、改了哪些文件（+ 意图）

| 文件 | 改动意图 |
| --- | --- |
| `frontend/src/stores/interactive.ts` | 新增「导入执行器」：上传 → waitUntilSettled → 身份核对 → 建卡 / 关联；执行器与导入源（File / 桌面路径）存在 store（Pinia 生命周期 > 页面组件）。新增单卡导入终态 `cardImportJobs`（返回页面时显示一次，ack 后不再重复）。新增 `importBatchSummaryText`（批次进度文案唯一入口）。导入写板面统一走公开 `store.commit` action（与界面同一撤销 / 脏标记 / 保存语义）。 |
| `frontend/src/components/interactive/AddMenu.vue` | 只保留「选文件 + 显示」：删掉组件内的上传 / 等待 / 关联和卸载时「结果未知」标记；把文件源交给 store；批次区渲染 store 事实；回挂时恢复批次进度，并按既有提示入口显示单卡导入的真实终态。 |
| `frontend/src/components/interactive/__tests__/pm032-canvas-import-batch.test.ts` | 改写卸载场景，新增 #46 定向用例（20 个 #46 场景 + 原有 #44/#45 场景）。 |
| `frontend/src/components/interactive/__tests__/fix025-attach-notice.test.ts` | 仅改写 1 条既有断言（见第四节）：卸载后迟到成功回执现在会完成原卡关联。 |

没有改 backend/**、docs/**、settings/**、conversation /**、BoardCard.vue、InteractiveView.vue（无需新增第二通知条，故未接线 InteractiveView）。

## 二、逐条需求 → 实现 → 证据

### 需求 1：执行在生命周期长于页面组件的位置（复用导入批次状态；切设置 / 关提示 / 重进都不丢回执）
- 实现：`stores/interactive.ts` 新增执行器（`runImportItem` / `runImportBatch` / `startCardAttachment`）。
  上传、`waitUntilSettled`、`commit` 全部在 store 内执行；组件卸载只取消显示层订阅，不取消任务。
  关提示（批次区「关闭」）只删显示事实，在飞回执仍会建卡 / 关联；切去设置在 store 里继续。
- 证据（pm032-canvas-import-batch.test.ts，21 tests 全绿）：
  - 「发起上传 → 去设置 → 上传成功 → 回互动页：原卡已关联、只关联一次、批次为成功、没有要求重选文件」
  - 「回互动页后才落定：先显示真实进度，收到回执后显示已接收（不重复建卡）」
  - 「重新挂载（打开设置再回来）后仍能看到本批真实进度与未接收项」
  - 「关闭批次提示不取消已发出的上传：回执仍按原批次完成建卡与关联」
  - 「单卡关联：上传发出后去设置，回执仍完成原卡关联；返回时显示真实结果」

### 需求 2：上传发出时记录身份；无预建卡也在原板面建卡关联；三种来源都核对
- 实现：`beginImportAttempt` 在上传发出前记录 `boardId / batchId / itemKey / cardId / expectedAttachmentId / sourceKind`。
  无预建卡（拖放批次）在**回执到达时、原板面**建卡并一次性写入关联（`createImportFileCard`，单次 commit）。
  浏览器上传走 `uploadAttachment(file, {sourceKind:"browser_upload"})`；桌面原路径沿用既有
  `registerSourceAttachment(path, sourceKind)`；已有文件卡（添加 / 替换）走 `startCardAttachment`，只在原卡上关联。
- 证据：
  - 「六件拖放：只接收 5 件，第 6 件标「未接收」并保留在清单里」（无预建卡、只建 5 张卡）
  - 「批次项绑定已有文件卡（添加 / 替换）：在原卡上关联，不新建卡」
  - 「桌面原路径登记：沿用既有 registerSourceAttachment，回执后建卡并关联」
  - 「首件上传失败…」/「「未接收」的第六份可重新导入…」（失败 / 未接收项重试只作用于该项）

### 需求 3：回执到达时核对原板面 / 原卡 / 原关联目标 / 本次尝试
- 实现（`applyImportAssociation`，任一不成立都不写板面）：
  1) 尝试仍是最新（同批次项 `latestImportAttemptByItem` / 同卡 `latestImportAttemptByCard`）→ 旧回执丢弃；
  2) 当前 `boardId` ≠ 原板面 → 不写进「碰巧打开的另一张板」，记具体失败原因；
  3) `attempt.cardId` 的原卡已删除 / 被替换 → 不重建卡；
  4) 原关联目标 `expectedAttachmentId` ≠ 卡上当前 attachmentId → 不覆盖新附件。
- 证据：
  - 「切到另一块板面：迟到回执不写进当前板面；回到原板面重试不需要重选文件」（另一板 0 张卡，失败原因含「原板面」，回原板面重试成功且未重新选文件）
  - 「单卡关联：原卡已删除 → 迟到回执不重建卡、不如实声称成功」
  - 「单卡关联：原卡已换成另一份文件 → 迟到回执不覆盖新关联」
  - 「同一批次项重试产生更新尝试后：旧回执不覆盖新结果、不重复建卡」（旧回执 `supersededBy === "item"`，卡仍 1 张、附件仍 att_new）
  - 「同一张卡上更新的尝试：旧回执不覆盖新关联」（`supersededBy === "card"`）
  - 「已接收项不能被后续回执覆盖成失败」（终态不被覆盖；沿用既有用例）

### 需求 4：返回互动页显示真实进度 / 已完成 / 具体失败原因；只在该重选时要求重选；单批上限与逐项重试保留；文案集中一个入口
- 批次（拖放 / 多文件）：
  - 返回时批次区直接读 store 事实：`上传中` / 等待 / 已接收 / 失败原因逐项可见；汇总文案唯一入口
    `importBatchSummaryText`（新增「/ 进行中 N 份」，仍在同一批次区，不新增常驻通知条）。
  - 5 份上限、「未接收」文件名保留、未接收项「重新导入」、失败项「重试」只重传该项：沿用并有用例。
  - 文件源存在 store：重试**不需要重新选文件**；只有整页刷新（源丢失 / `needsReselect`）才要求重选。
- 单文件（AddMenu 选文件，没有批次面板；组长补充要求）：
  - 呈现选型：store 保存单卡导入终态 `cardImportJobs`（uploading / processing / succeeded / failed +
    具体 error + attachment），终态**保留到用户看过或关闭**（acknowledged）。返回互动页时由 store 的
    `activeCardImportForBoard` / `unacknowledgedCardImportForBoard` 取真实状态，走**既有**
    `useTransientNotice` 提示入口显示一次（复用同一个 `data-im="add-material-status"` 提示行，
    **没有新增第二个常驻通知条**）。AddMenu 精确接线点：
    `onMounted → restoreCardImportNotice()`、`onImportJobEvent()`、`showCardImportSnapshot()`、
    关闭按钮 `dismissNotice()`（关闭即 ack）。
  - 何时消失：成功提示沿用既有契约约 3 秒自动消失（消失的只是提示，卡片 / meta 关联保留）；
    失败提示不自动消失，保留到用户关闭或「重试关联」；批次区不自动消失，保留到用户点「关闭」。
    已展示 / 已关闭的终态被 ack，下次返回不重复弹。
  - 重试不需要重选：返回后的失败提示提供「重试关联」，用 store 里保存的文件源直接重跑；
    只有无源（整页刷新）才回退到「重新选择文件」。
- 证据：
  - 「单卡关联：离开期间完成 → 返回后仍能看到真实终态（不依赖离开期间就过期的瞬时提示）」
    （回挂后提示含「已关联附件 / 离开完成.txt」，3.2s 后提示消失但 meta 关联保留，再次回挂不再重复弹）
  - 「单卡关联：离开期间失败 → 返回后看到具体失败原因；重试沿用内存里的文件（不要求重选）」
    （提示含「磁盘配额不足」；点「重试关联」后 `pickBrowserFile` 只被调用 1 次 = 初次选文件）
  - 「批次区不自动消失…」+「拖放文件名与状态都渲染在批次区…」+「六件拖放…」+「首件上传失败…」

## 三、跑过的命令与结果

| # | 命令（cwd = D:\qio-dev\pm-qio032-integration\frontend） | 结果 |
| --- | --- | --- |
| 1 | `npx vitest run src/components/interactive/__tests__/pm032-canvas-import-batch.test.ts`（改动前基线） | 8 passed（含旧断言「离开页面 → 记结果未知」） |
| 2 | 同上（改动后） | **21 passed** |
| 3 | `npx vitest run` 11 个相关文件（pm032-canvas-import-batch / fix025-attach-notice / fix025-independent-verify / a-unknown-consistency / image-card-simplification / irl-c-new-card / stores/pm032-state-contract / BoardToolbar / pm032-canvas-toolbar-render / InteractiveViewFloating / InteractiveOldEntriesGone） | **11 files / 100 tests passed** |
| 4 | `npx vue-tsc --noEmit` | **exit 0**（目视无输出） |
| 5 | `npx vitest run src/components/interactive/__tests__`（更宽的交互页回归扫描） | 83 files / 761 tests：758 passed，**3 failed 全在 2 个与本次无关的文件**（见第六节） |
| 6 | 单独复跑上述 2 个文件 | recovery-a-integration-n6 单独通过（并行负载下 flaky）；pm032-canvas-group-ops 2 条稳定失败（与本卡无关，见第六节） |

未跑后端测试：本次改动是纯前端（backend/** 未改）。

## 四、被改写的既有断言与理由（必须声明）

1. `pm032-canvas-import-batch.test.ts` 原用例
   **「离开页面时仍在飞的上传：如实记「结果未知」并给重试入口，不永远转圈」** —— **删除并替换**为
   「发起上传 → 去设置 → 上传成功 → 回互动页…」等 13 条 #46 用例。
   理由：这条断言固化的正是 #46 要修的缺陷（组件卸载即把在飞上传标「结果未知」、丢弃成功回执）。
   定稿要求回执必须继续完成，不能再用「结果未知」当期望值。旧用例里的有效意图
   （不永远转圈、有恢复入口）由新用例以「真实进度 → 已接收 / 具体失败原因 + 对应重试入口」覆盖。

2. `fix025-attach-notice.test.ts`「卸载后才落定的成功结果：不写板面、不复活提示」→
   **「卸载后才落定的成功结果：不复活提示，但 #46 起回执仍完成原卡关联」**。
   断言改为：计时器仍不复活（`vi.getTimerCount() === base`）、`waitUntilSettled` 被调用、
   `card.meta.file.attachmentId === "att_late"`、卡片未删除。
   理由：#46 定稿第 1/2 条要求「上传发出后离开页面不能让成功回执丢掉」；原断言要求「不写板面」与定稿冲突。
   同一文件顶部第 5 条说明文案同步更新。
   保留未改的相关边界：**「卸载后迟到的选择结果：不复活提示、也不写板面」** —— 此时上传**尚未发出**，
   组件卸载后不再发起新上传（既有契约，仍绿）。

## 五、限制 / 未验证（诚实标注）

- **真实应用界面验证未做**（本环境只有 jsdom 组件测试）：Lead 要求真实应用复验「延迟上传 → 切设置 →
  回互动页」的批次状态与文件卡；jsdom 里状态机已跑通（用例见上）。
- **Windows 原生拖放：未验证**（需要真实 Tauri 壳；jsdom 里 `isDesktopShell()` 为 false，走浏览器 fallback）。
  桌面原路径登记逻辑只用 mock 覆盖了 `registerSourceAttachment(path, sourceKind)` 调用与回执落定。
- **浏览器拖放：只有 jsdom 组件测试证据**，不是真实浏览器 E2E；真实浏览器 / 真实文件系统未验证。
- 单卡导入的「终态恢复」只在 Pinia 生命周期内有效；整页刷新后 store 重置（文件源也丢），
  此时如实要求重新选择文件（`needsReselect` 文案）。

## 六、与本次无关的失败（记录，未改别人文件）

宽扫 `src/components/interactive/__tests__` 时出现 3 个失败，均不在本次改动范围：

1. `pm032-canvas-group-ops.test.ts`（2 条，「#60 解除有组级连线的组…」）：`SelectionMenu` 里的
   `.qio-confirm` / 确认按钮没出现。单独复跑稳定复现。
   核查：`SelectionMenu.vue` / `QConfirm.vue` / 该测试文件 `git status` 均为 clean；
   `stores/interactive.ts` 的 diff hunk 只有「imports / ImportBatch 类型区 / dismissImportBatch 2 行 /
   导入执行器 / return 导出」，没有动组 / 确认相关逻辑。判定为基线或其它成员改动导致，未修改。
2. `recovery-a-integration-n6-store-input.test.ts`（1 条，「code：代码语言随正文一起写入并恢复」）：
   宽扫（多 worker 并行）时失败，**单独复跑通过** → 并行负载下的 flaky，与本次改动无关。

## 七、自主产品决定

1. **呈现方式（响应组长补充要求）**：单文件导入不新建批次面板、也不新增第二个常驻通知条，
   而是把终态放进 store（`cardImportJobs`，保留到 ack），回挂时用**既有**瞬时提示入口显示一次。
   保护的是「离开期间完成 / 失败 → 返回仍能看到真实终态」；成功 3 秒后自动消失、失败/重试保留、
   看过或关闭后不重复弹。
2. **重试不重选**：文件源存在 store（关提示 / 切路由不丢），批次项与单卡「重试关联」都直接复用；
   只有整页刷新导致源丢失时才要求重新选择，并在文案里说明需要重新提供文件。
3. **导入写板面统一走公开 `store.commit` action**（`commitImportBoard`），不另开写板面路径：
   保持与界面同一套撤销栈 / 脏标记 / 保存调度语义（也让既有测试的 commit 替身能覆盖导入路径）。
4. **无预建卡的导入一次 commit 建卡 + 写入关联**（`createImportFileCard` 合并 meta），
   避免两条撤销记录 / 中间态空卡。
5. **关闭批次提示不取消在飞上传**；同时在关闭时释放该批在内存里的文件源（在飞回执自带源，不受影响），
   避免 File 句柄长期滞留。
