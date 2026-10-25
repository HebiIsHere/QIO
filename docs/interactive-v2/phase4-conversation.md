# Phase 4-D 对话页回归与共享壳唯一性验收

> 角色：P4-D（只读验收）。工作区 `D:\qio-dev\qio-i2-p4-d`，分支 `qio-i2/p4-d`，基线 `e0af7d16`。
> 本文件是唯一写入；没有修改生产代码、测试或配置。
> 截图与原始测量结果存放在工作区外（`D:\qio-dev\_p4d-shots\`、`D:\qio-dev\_p4d-audit\`），不进入提交。

## 0. 结论（放行建议）

**主链路未回退，可以进入最终验收；窄窗有两个必须在合并前确认的问题（F-01 / F-02）。**

- 对话页三栏、话题栏、消息流、输入区、草稿、附件、输入区事项入口、失败恢复、预览、星球在 1440/1024/800 实测正常；草稿跨页面切换保留；预览可打开、可切回；星球可进入/退出；全量前端 436 文件 / 4072 用例与 `vue-tsc --noEmit` 全部通过。
- 对话 ⇄ 互动双向切换、审批唯一挂载、旧入口让位、切换不重复提交均通过实测与定向测试。
- **F-01（P1）**：互动页在窄窗展开底聊时，板面高度被压到 128px（480×640），低于布局纯函数为对话槽保留的 220px 下限。
- **F-02（P1）**：对话页在 320px 宽时输入胶囊只剩 93px，模式切换 + 星球固定槽把输入区挤到不可用；互动页 320 也存在工具栏/输入区拥挤。
- **F-03（提示）**：5300 实测环境比本 worktree 基线多 1 个提交（`55c7fe81`，互动页模式切换位置修复）；若严格按 `e0af7d16` 验收，互动页**没有**模式切换入口（详见 §5 的 F-03）。最终合并必须包含该修复。

## 1. 验收环境与方法

| 项 | 值 |
| --- | --- |
| 源码基线（本 worktree） | `e0af7d16`（`docs(interactive): 互动页 v2 交付说明与状态记录`） |
| 截图目标（Lead 指定 5300） | `http://127.0.0.1:5300`，由 Lead 的 Vite 开发服务器提供，cwd = `D:\qio-dev\qio-interactive-v2\frontend`，`VITE_QIO_BACKEND_URL=http://127.0.0.1:8799` |
| 5300 实际代码 | `55c7fe81`（= 基线 + 1 个提交：`fix(interactive): 模式切换回到输入胶囊外并与底聊同排`，仅改 `InteractiveView.vue` +43/−16） |
| 后端 | `http://127.0.0.1:8799`（隔离数据域，含 seeded 对话/板面：board_default、topic_739ac5f04573、msg_cv_1..、失败任务与排队任务） |
| 浏览器 | 真实 Chrome（Playwright），1440×900 / 1024×768 / 800×600 / 480×640 / 320×640；暗色/亮色/减少动画 |
| 前端全量 | `cd frontend; npx vitest run` → **436 files / 4072 tests passed**；`npx vue-tsc --noEmit` → 通过 |
| 定向批次 A（审批/壳/对话页） | 46 files / **477 tests passed** |
| 定向批次 B（草稿/附件/预览/星球/重试） | 16 files / **214 tests passed** |

只读边界：本任务没有改 App.vue、WindowChrome.vue、store、board.ts、任何组件或测试；`frontend/node_modules` 是指向 `D:\qio-dev\qio-interactive-v2\frontend\node_modules` 的 junction（仅用于运行测试，未提交）。

## 2. 对话页布局回归（1440/1024/800/480/320）

### 2.1 实测几何（暗色；5300）

| 视口 | 左任务栏 | 中列 | 消息流 | 输入胶囊 | 星球槽 | 横向溢出 |
| --- | --- | --- | --- | --- | --- | --- |
| 1440×900 | 292×820 @(8,72) | 1120×820 @(312,72) | 1120×642 @(312,134) | 842×54 @(457,809) | 96×96 @(1311,788) | doc 0 / stream 0 |
| 1024×768 | 自动收起（48px 贴边入口） | 948×666 @(68,72) | 948×526 @(68,134) | 720×54 @(196,685) | 80×80 @(928,672) | 0 |
| 800×600 | 自动收起 | 724×520 @(68,72) | 724×358 @(68,134) | 496×54 @(196,517) | 80×80 @(704,504) | 0 |
| 480×640 | 自动收起 | 404×520 @(68,72) | 404×382 @(68,134) | 253×54 @(143,529) | 56×56 @(408,528) | 0 |
| 320×640 | 自动收起 | 244×560 @(68,72) | 244×422 @(68,134) | **93×54 @(143,569)** | 56×56 @(248,568) | 0（无溢出，但输入区过窄，见 F-02） |

- 结论：三栏在 1440 可见；≤1180 左栏按契约**临时**收起（不写偏好），右侧预览关闭时无占位；输入区固定在底部、消息流不被盖住；**任何视口都没有横向滚动**（`document.scrollWidth == clientWidth`，`stream.scrollWidth == clientWidth`）。
- 1440 截图：`D:\qio-dev\_p4d-shots\p4d-conversation-1440-dark.png`；亮色 `p4d-conversation-1440-light.png`；减少动画 `p4d-conversation-1440-reduced.png`。
- 窄窗截图：`p4d-conversation-1024.png` / `-800.png` / `-480.png` / `-320.png`。

### 2.2 草稿、附件、输入区事项入口、失败恢复

| 能力 | 实测证据 | 判断 |
| --- | --- | --- |
| 草稿跨页面保留 | 对话页输入 `P4-D 草稿一致性检查（切换页面后应保留）` → 切到互动页 → 浏览器返回对话页，输入框值**逐字保留**；反向在互动页输入 → 通过互动页模式菜单切回对话页，值仍保留（`result2.json` `draftBeforeSwitch/afterBack/backToConversation`） | 未回退 |
| 草稿/失败入口 | 注入 2 条本机失败原文（`qio.chat.failedSend.v1`，仅在浏览器 profile 内）后，对话页输入区上方出现入口①（发送异常与草稿恢复，计数 2）；点开面板列出 2 行失败原文、真实原因与「找回原文 / 与当前文字互换 / 不再保留」；截图 `p4d-conversation-entries.png`、`p4d-conversation-entries-panel.png` | 未回退 |
| 同一入口在互动页复用 | 切到互动页后，同一入口①出现在底部互动对话的输入区上方（`.ic-entries`），计数与内容一致；点开面板同样 2 行；截图 `p4d-interactive-entries.png`、`p4d-interactive-entries-panel.png`。切换过程 **POST /api/turns = 0**（不自动重发） | 未回退 |
| 附件 | 消息流中 seeded 附件「对话页开发方案.md」正常；输入区胶囊内附件按钮与占位文案存在；Composer 附件链路定向测试 `ComposerAttachments` / `ComposerAttachmentRejection` / `acc_d_composer_*` 全过 | 未回退 |
| 失败恢复/重试 UI | `FailedSendNotice`、`final-b3-notice-visibility`、`p3c-retry-*` 定向全过；本机注入的失败原文面板与互动页一致 | 未回退 |
| 五入口 | 组件层：`ComposerEntries.test.ts`、`entries.test.ts`、`p3c-composer-extra.test.ts` 全过；实测：无事项时不渲染入口行（不常驻空入口），有事项时出现且计数真实；同一时刻最多展开一个 | 未回退（无事项时行不出现是设计） |

### 2.3 审批

| 检查 | 结果 |
| --- | --- |
| 统一层唯一挂载 | DOM：对话页 `[data-unified-approval-layer]` = **1**；互动页 = **1**；设置页 = 0 |
| 旧入口让位 | 对话/互动页 `.approval-entry` = 0、`.modal-mask`（旧 ApprovalModal）= 0；源码：`App.vue:122` `<ApprovalEntry v-if="!isChatRoute" />`、`App.vue:127` `<ApprovalModal v-if="!isChatRoute && !approvals.inlineClaimed" />`；`isChatRoute` 见 `App.vue:39-41`（conversation + interactive） |
| 统一入口位置 | 互动页不再挂浮动入口（`InteractiveView.vue:191-192` 只挂层；入口由输入区事项行第⑤项提供）；对话页同样由 `ComposerEntries` 第⑤项提供；`UnifiedApprovalEntry` 的 floating 变体只在测试里使用 |
| 暂缓后手动找回 / 不反复自动弹 | 定向 `approval/__tests__/p3c-approval-entry.test.ts`（6 用例）覆盖：自动弹出一次→关闭→`retryAutoOpen()` 多次与重新挂载都不再弹；入口仍可点开；两实例挂载 warn；DOM 计数测试 `approvalInlineGating` 全过 |
| 旧入口在非聊天页仍可用 | 设置页 `.top-notes` 存在，旧入口按 `pendingCount` 显示（本数据 0 条待办，因此 DOM 无按钮，符合条件渲染；App.vue 门控由 `approvalInlineGating.test.ts` 断言） |
| 未覆盖 | 当前隔离数据域没有待审批项（`/api/runtime/state` 的 `approvals: []`），**未能**截图验证真实待审批样式、口令/知识两类按钮与「稍后处理」视觉；该部分以定向测试与 P3-C 组件证据为准（见 §7 F-05） |

### 2.4 预览

- 对话页点击回复代码块预览胶囊（`.code-preview-btn`，1440 下共 2 个）→ 右侧预览面板打开：`[data-preview-panel]` @(1012,72) 420×820，标签数 1，消息列同时收缩（composer 842 → 436），`document.scrollWidth == clientWidth`（0 溢出）。
- 截图：`D:\qio-dev\_p4d-shots\p4d-conversation-preview-open.png`；1440 关闭态见 `p4d-conversation-1440-dark.png`。
- 定向：`cv-c-preview-panel` / `cv-c-preview-reader` / `cv-c-preview-resources` / `cv-c-preview-logic` / `cv-c-preview-store` 全过（16 文件批次内）。
- 已知限制（非本轮引入）：回复代码块正文只在内存，刷新后标签仍在但需要从原消息重新打开；若持久化时右侧是打开的，窄窗口会以全窗口预览恢复并显示「这条内容不在当前页面里」的说明——这与交付文档 §5 的已知限制一致。

### 2.5 记忆星球

- 对话页右下角入口存在（`[data-planet-entry]`，1440 @(1311,788) 96×96）；点击后全屏星球打开，canvas 1439×900、overlay 1 个，左上「收起星球」可退出；退出后无残留。
- 截图：`D:\qio-dev\_p4d-shots\p4d-conversation-planet.png`。
- 定向：`PlanetDock.test.ts`、`PlanetView.test.ts`（含主题同步/连续体/结束分组）全过。
- 互动页没有星球入口（`InteractiveView` 不挂 PlanetDock；统一层用 `:show-memory="false"`），与 Phase 1-D 审计「互动页无星球落点」的结论一致；这是当前设计，不是回退。

## 3. 对话 ⇄ 互动 ⇄ 设置：壳与一致性

### 3.1 品牌/顶栏

| 路由 | WindowChrome classes | 品牌 | 顶栏 | `--titlebar-height` | 设置入口 | 互动挂载点 |
| --- | --- | --- | --- | --- | --- | --- |
| 对话 `#/` | `window-chrome conversation-bar` | 轨道 logo + `PERSONAL AGENT` | 52px 圆角卡片 @(8,8)、宽 1424 | 70px | 有 | 无 |
| 互动 `#/interactive` | `window-chrome conversation-bar interactive-bar` | 同上（logo + PERSONAL AGENT） | 同上 | 70px | 有 | `#qio-interactive-chrome-extra`（「已保存」+「搜索」） |
| 设置 `#/settings` | `window-chrome` | 经典「• QIO」字标 | 32px 透明标题条 | 32px | 无（设置页本身） | 无 |

- 两个聊天路由的品牌、顶栏高度、窗口控制区完全一致；设置页按 Phase 1-A 契约回到经典窄标题条（`WindowChrome.vue:52-58` `hasGlobalBar`，`:156` class，`:184-201` 品牌/挂载点）。这是**设计差异**，不是不一致。
- 截图：对话/互动 1440、设置页 `p4d-settings-from-ui.png`（从互动页点设置进入；返回后各状态保留）。

### 3.2 主题与字体

- 暗色：`html[data-theme=dark]`（默认）；亮色：初始化 `localStorage.qio-theme=light` 后两个聊天页均为 `data-theme=light`，截图 `p4d-conversation-1440-light.png` / `p4d-interactive-1440-light.png`；设置页主题切换后回聊天页一致。
- 三声部字体实测（两个聊天页一致）：
  - 标题/话题/板面名：`"Noto Serif SC", … serif`；
  - 正文/输入：`system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei UI", … sans-serif`；
  - 数据/时间/技术：`"Cascadia Mono", Consolas, … monospace`。
- 输入区：两页都渲染同一个 `Composer.vue`（`#composer-input`、相同占位「继续讨论，或添加文件…」、相同附件按钮与发送/停止逻辑）；互动页通过 `ComposerEntries` + `Composer :chat-binding` 复用（`InteractiveConversation.vue:342-355`），没有第二套简化输入模型。

### 3.3 模式切换（双向、不重复提交）

5300（`55c7fe81`）实测：

| 检查 | 对话页 | 互动页 |
| --- | --- | --- |
| 入口 | `#composer-mode-switch` 在输入胶囊左侧（`ComposerDock.vue:23`） | `#composer-mode-switch` 在底部互动对话输入区左侧（`im-composer-row`，由 `55c7fe81` 加入；本 worktree `e0af7d16` 尚无） |
| 当前态 | `aria-label="切换页面模式（当前为对话模式）"`，菜单「对话模式 · 当前」+「互动模式 · 一起整理材料与关系」 | `aria-label="切换页面模式（当前为互动模式）"`，菜单「对话模式 · 边聊边整理材料」+「互动模式 · 当前」 |
| 导航 | 菜单点「互动模式」→ `#/interactive` | 菜单点「对话模式」→ `#/` |
| 状态保留 | 见 §2.2：草稿双向逐字保留；附件/审批/排队都在 store/本机存储，组件只导航（`ConversationModeSwitch.vue:50-57`） | 同上 |
| 重复提交 | 全程 `POST /api/turns = 0`（`result.json` / `result2.json`） | 同上 |

- 截图：`p4d-mode-menu-conversation.png`、`p4d-interactive-mode-menu.png`、`p4d-conversation-after-ui-switch.png`。
- 发现：互动页菜单里「边聊边整理材料」在 1440 下右端略被菜单宽度裁到（菜单 `min-width: 196px`），属外观小问题（F-04）。

## 4. 唯一挂载 / 旧入口让位 / 不重复提交（源码 + 实测）

1. **唯一的全局审批层**：`ConversationView.vue:536` 与 `InteractiveView.vue:192` 各挂一次 `UnifiedApprovalLayer`；两个聊天路由互斥，DOM 实测每页恰好 1 个；层内部自带重复挂载 warn 守卫（P3-C）。
2. **旧入口只在非聊天页**：`App.vue:120-127`——`ApprovalEntry` 包在 `!isChatRoute` 里、`ApprovalModal` 同时按 `!isChatRoute && !approvals.inlineClaimed`；聊天页 DOM 实测旧入口/旧弹窗为 0。
3. **入口唯一**：聊天页唯一审批入口是输入区事项行第⑤项（`ComposerEntries.vue`，`unified.open()`）；互动页不再挂浮动 `UnifiedApprovalEntry`（源码只有组件定义与测试引用）；`approvals.inlineSuppressed` 让过程区内联卡让位（`UnifiedApprovalLayer` 挂载期置 true）。
4. **不重复提交**：模式切换只 `router.push`（`ConversationModeSwitch.vue:51-57`），不发请求、不清 store；实测切换与往返 0 次 `POST /api/turns`。审批提交防重由统一 store 的 `submittingKey`/单次消费保证（`unifiedApprovals.test.ts`、`approvalRouting.test.ts`、`p3c-approval-entry` 全过）。
5. **设置页保留旧路径**：非聊天页仍由 `App.vue` 渲染旧入口/旧弹窗（有待办时才出现），保证后台审批在设置/调试页仍可见；不与聊天页的统一层并存。

## 5. 发现与风险

| 编号 | 严重度 | 问题 | 证据 | 建议 / 归属 |
| --- | --- | --- | --- | --- |
| F-01 | **P1** | 互动页窄窗（480×640）展开底聊后，板面区只剩 **128px** 高，明显低于布局纯函数为对话槽保留的 220px 下限；底聊占据 420px，板面被压成一条 | `result2.json narrow480`（board h=128、composer region h=420）；截图 `p4d-interactive-480-chat.png`；根因：`InteractiveView.vue:306-323` 把底聊放在 **composer 槽**里自行展开，而 `InteractiveWorkspace` 的对话槽 `#chat` 未被使用（`InteractiveWorkspace.vue:73/82/90/170`，`layout.chat` 恒 false），`chatMaxHeight`/板面最小高度预算因此从未生效 | Lead：把 `InteractiveConversation` 接进 `#chat` 槽，或在 composer 槽展开时按 `layout.chatMaxHeight` 与板面最小高度自行夹取（推荐前者，一处预算）；加窄窗用例 |
| F-02 | **P1** | 对话页 320px：输入胶囊 **93px** 宽，模式切换（收起文字后仍有按钮/箭头）+ 星球槽（56px）+ 间距把输入区挤到不可用；互动页 320px 底部同样拥挤（浮动工具栏覆盖板面卡片，输入区只剩贴边按钮） | `result.json` conversation320 composer w=93、planet x=248 w=56；截图 `p4d-conversation-320.png` / `p4d-interactive-320.png` | Lead/前端：<360px 时把模式切换移入输入胶囊 leading 或下一行、缩小/暂隐星球槽，并设输入区最小可用宽；320 不在现行正式最小宽度（契约验收 480 起），但既然截图覆盖则建议修 |
| F-03 | 提示 | 5300 实测代码比本 worktree 基线多 1 个提交：`55c7fe81`（互动页模式切换位置修复）。严格按 `e0af7d16` 审计，互动页**没有**模式切换入口（只有浏览器返回） | `git -C D:\qio-dev\qio-interactive-v2 log`：`e0af7d16` → `55c7fe81`；diff 仅 `InteractiveView.vue`（导入/渲染 `ConversationModeSwitch` + `.im-composer-row` 布局） | Lead：合并时确认包含 `55c7fe81`；本报告的截图与双向切换结论对应 5300（55c7fe81） |
| F-04 | P3（外观） | 互动页模式菜单第一项说明「边聊边整理材料」右端被裁（菜单 `min-width: 196px` 容不下长说明） | 截图 `p4d-interactive-mode-menu.png`；`ConversationModeSwitch.vue:211-224` 菜单宽度 | 前端：菜单加宽或对 `.item-note` 做省略号/换行（对话页同样组件，但对话页菜单左侧空间更足） |
| F-05 | 证据限制 | 隔离数据域没有待审批项（`/api/runtime/state.approvals=[]`），因此无法用真实弹窗/入口截图验证「有 N 项申请等待确认」与「暂缓后手动找回」的视觉；当前 DOM 只有层、没有入口（入口按 `count>0` 条件渲染，属预期） | DOM 计数：layer=1、unifiedEntry=0；API `/api/runtime/state` | Lead：最终验收前用一条真实/伪造的待审批数据截一次「入口 + 弹窗 + 暂缓后入口」；行为证据以 `p3c-approval-entry.test.ts`（6 用例）与 `UnifiedApprovalModal.test.ts` 为准 |
| F-06 | 证据限制 | 星球用 headless Chrome + SwiftShader 渲染，画面与字体/环比例只确保「能打开/能退出/无残留」，未替代实机 GPU 视觉检查 | `p4d-conversation-planet.png`、`PlanetView.test.ts` | Lead：Tauri 实机再做一次视觉检查（交付文档已知边界） |
| F-07 | 已知限制 | 持久化的「回复代码块」预览标签刷新后没有正文：页面会恢复右侧预览（窄窗为全窗口）并显示「这条内容不在当前页面里」；本轮截图首次在 1024 遇到该覆盖层（随后重置布局偏好） | 交付文档 §5 / `conversation-page-v2-merge-notes.md` §5；实测 `pv-problem` 拦截点击 | 非本轮回归；如需降低打扰，可在窄窗恢复失败时不自动全窗口、只亮贴边入口（Lead 决策） |
| F-08 | P3（观察） | 320px 对话页消息正文列只有 244px，长中文/链接几乎逐字换行；无横向溢出但可读性差 | `p4d-conversation-320.png` | 同 F-02：320 不是正式最小宽度，若产品要求支持则需专门的窄窗排版 |

## 6. 定向测试与验证清单

| 批次 | 命令（frontend 下） | 结果 |
| --- | --- | --- |
| 类型 | `npx vue-tsc --noEmit` | 通过（0 错误） |
| A：壳/审批/对话页 | `npx vitest run src/components/__tests__/p1a-* src/components/__tests__/p3b-composer-binding.test.ts src/components/__tests__/WindowChrome.test.ts src/components/__tests__/Composer*.test.ts src/components/__tests__/d5ComposerRecovery.verify.test.ts src/components/__tests__/Approval*.test.ts src/components/approval/__tests__ src/components/conversation/__tests__ src/views/__tests__ src/__tests__/appBoot.test.ts src/__tests__/approvalInlineGating.test.ts src/components/interactive/__tests__/p1b-v2-* src/components/interactive/__tests__/p3c-composer-extra.test.ts src/components/interactive/v2/__tests__ src/stores/__tests__/approvalRouting.test.ts src/stores/__tests__/approvalInline.test.ts src/stores/__tests__/unifiedApprovals.test.ts` | 46 files / **477 passed** |
| B：草稿/附件/失败/预览/星球/重试 | `npx vitest run src/stores/__tests__/draftPersistence.test.ts src/stores/__tests__/final-b2-*.test.ts src/components/__tests__/FailedSendNotice.test.ts src/components/__tests__/final-b3-notice-visibility.test.ts src/components/__tests__/acc_d_composer_*.test.ts src/components/conversation/__tests__/entries.test.ts src/components/conversation/__tests__/ChatColumn.test.ts src/conversation/__tests__/cv-c-preview-*.test.ts src/components/__tests__/PlanetDock.test.ts src/views/__tests__/PlanetView.test.ts src/components/interactive/__tests__/p3c-retry-*.test.ts` | 16 files / **214 passed** |
| 全量 | `npx vitest run` | **436 files / 4072 tests passed**（与本分支交付文档一致） |

> 说明：两批定向与全量都跑在 `qio-i2-p4-d`（`e0af7d16`）；5300 的 Playwright 实测在 `55c7fe81`（仅多模式切换位置修复，不影响上述测试断言）。

## 7. 截图清单（工作区外，未提交）

目录：`D:\qio-dev\_p4d-shots\`

| 文件 | 内容 |
| --- | --- |
| `p4d-conversation-1440-dark.png` / `-light.png` / `-reduced.png` | 对话页三栏、话题栏、消息流、输入区、星球入口；亮色与减少动画 |
| `p4d-conversation-1024.png` / `-800.png` / `-480.png` / `-320.png` | 窄窗与自动收起左栏；320 暴露 F-02/F-08 |
| `p4d-conversation-preview-open.png` | 回复代码块 → 右侧 420px 预览面板，无横向溢出 |
| `p4d-conversation-planet.png` | 星球全屏、收起入口 |
| `p4d-conversation-entries.png` / `-entries-panel.png` | 事项行入口①与「发送失败的原文」面板（2 条注入的本机记录） |
| `p4d-interactive-entries.png` / `-entries-panel.png` | 互动页复用同一事项行与同一会话状态 |
| `p4d-mode-menu-conversation.png` / `p4d-interactive-mode-menu.png` | 双向模式菜单与当前项 |
| `p4d-conversation-after-ui-switch.png` | 互动页经菜单切回对话页，草稿仍保留 |
| `p4d-interactive-1440-dark.png` / `-light.png` | 互动页板面/工具栏/底聊条/输入区；亮色一致 |
| `p4d-interactive-chat-1440-dark.png` | 互动页展开底聊（1440 正常） |
| `p4d-interactive-480-chat.png` | 480×640 展开底聊，板面仅 128px（F-01） |
| `p4d-interactive-800.png` / `-1024.png` / `-320.png` / `-480.png` | 互动页窄窗档位 |
| `p4d-settings-1440-dark.png` / `p4d-settings-from-ui.png` | 设置页经典壳（QIO 字标/32px），无聊天栏与审批层 |

原始测量 JSON：`D:\qio-dev\_p4d-shots\result.json` / `result2.json` / `result3.json`。

## 8. 未覆盖 / 需要 Lead 决定

1. **真实待审批态的视觉**（F-05）：需要一条 pending approval（工具/权限或知识）才能看到入口、自动弹出、暂缓后再打开；当前只验证了「层唯一、旧入口不存在、行为测试通过」。
2. **320px 是否在验收范围**：布局函数与既有契约以 480 为最窄验收宽度；本报告按任务要求补测 320，发现两处拥挤。若 320 不在范围，F-02/F-08 可降级为已知限制；若在范围，需要 Lead 排期修。
3. **F-01 的修复归属**：需要在 `InteractiveView.vue`（Lead 所有）把底聊放进 `#chat` 槽，或让 composer 槽承担同样的高度预算；本任务只读，未改。
4. Tauri 实机（窗口拖动/按钮/字体渲染）与真实 GPU 星球视觉：按交付文档的已知边界，仍待最终阶段实机检查。
5. 合并材料：5300 已验证的 `55c7fe81` 必须在最终合并中；若最终 SHA 高于它，需重跑本报告 §6 的全量与关键截图。
