# #47 · 恢复可用的「还原默认布局」— 返修证据

- 执行者：frontend-47（子代理）
- Worktree：`D:\qio-dev\pm-qio032-integration`（分支 fix/pm-qio032-integration，基线 7445170d）
- 日期：2026-11-14
- 约束遵守：未切分支 / 未 git checkout·commit·push·stash·reset；只改工作树；未跑全量测试；fake/mock 模型与 API；未改依赖/版本号；未删/跳过既有测试。
- 本文件在仓库树之外（`D:\qio-dev\_lead-logs\integration-032\`），未写入 worktree。

## 1. 改动文件与意图

| 文件 | 意图 |
| --- | --- |
| `frontend/src/conversation/layoutPrefs.ts`（+61/-0） | 新增三个纯函数：`cloneLayoutPrefs`（发出时快照/深拷贝）、`restoreDefaultLayout`（**只**重置左右栏宽/左栏收起/预览开合/手动全窗口，保留标签、当前标签、位置、任务页签）、`layoutPrefsFingerprint`（键顺序无关的内容指纹，忽略 updatedAt）。既有 `resolveConversationLayout` / `sanitizeLayoutPrefs` 一行未改（`git diff --numstat` = `61 0`）。 |
| `frontend/src/stores/conversationShell.ts` | 保存改为**串行链**（同一时刻一个写请求，后发排在后面，服务端最后写入的一定是最后的状态）；回执只有在「写出去的快照 === 当前界面状态」时才回填，迟到旧回执直接丢弃；`load()` 返回是否成功并去重（`_loading`），新增 `layoutLoaded` 标记；新增 `resetLayout()`（未读布局时先读再还原；成功/失败都返回真实结果）；`flush()` 改为 `Promise<boolean>`（`void shell.flush()` 既有调用不受影响）。 |
| `frontend/src/views/settings/sections/RegularSection.vue` | 「窗口行为」下恢复「布局」行 + 「还原默认布局」按钮（`data-settings-key="regular.window.reset-layout"`），接到 conversationShell 的既有持久化；用共享保存模型 `saveNow`：保存成功后才给一次短成功反馈，失败给明确错误 + 技术详情 + 「重试」（重放同一次还原）。保留 #39 的固定入口/关闭行为等设置。 |
| `frontend/src/views/settings/searchIndex.ts` | 恢复「还原默认布局」索引项（标题/说明/关键词，无 fold，直接定位到按钮）；改写头部注释：#39「贴靠后自动隐藏」仍删除，#47「还原布局」恢复且对应真实控件。集成冲突里保留的短文案与 `memory.fragment.max-tokens` 的 `fold: "memory.advanced"` 未回退。 |
| `frontend/src/views/settings/__tests__/settings-reset-layout.test.ts`（新增） | 7 个用例：只改布局字段、收起后重开回原标签/位置、启动直接进设置先读后还原、两条竞态用例、失败+重试、窄窗推导不写偏好。 |
| `frontend/src/conversation/layoutPrefs.test.ts`（+4 用例） | `restoreDefaultLayout` / `cloneLayoutPrefs` / 指纹 / 还原后窄窗推导的纯函数护栏。 |
| `frontend/src/views/settings/__tests__/settings-content.test.ts` | 更新被 #47 反转的既有断言（见 §4），`A_OWNED_KEYS` 加回 `regular.window.reset-layout`。 |
| `frontend/src/views/settings/__tests__/settings-search.test.ts` | 更新被 #47 反转的搜索断言；新增「搜索定位到还原按钮」；保留 `memory.advanced` fold 断言。 |
| `frontend/src/views/settings/__tests__/settingsApiMock.ts` | 补 `saveConversationLayout` 替身（默认原样回显，与真实后端一致），供 #47 用例延迟/拒绝。 |

未触碰：`backend/**`、`docs/**`、`frontend/src/components/interactive/**`、`frontend/src/stores/interactive.ts`、`frontend/src/components/conversation/**`。工作树里其它 M 文件（backend/materials.py、interactive 相关等）是同一 worktree 其他成员的改动，不是我改的。

## 2. 逐条需求 → 实现 → 证据

> 运行命令（定向，不是全量）：
> `cd D:\qio-dev\pm-qio032-integration\frontend; npx vitest run src/conversation/layoutPrefs.test.ts src/views/settings/__tests__/settings-search.test.ts src/views/settings/__tests__/settings-content.test.ts src/views/settings/__tests__/settings-reset-layout.test.ts src/views/__tests__/ConversationViewShell.test.ts`
> 结果：**5 files / 70 tests 全绿**（exit 0）。

### R1 恢复设置操作与搜索项
- 实现：RegularSection「窗口行为 → 布局」行 + 按钮；searchIndex 条目 `regular.window.reset-layout`；点搜索结果能进「常规」并高亮该行。
- 证据：
  - `settings-content.test.ts > A 拥有的所有设置项都渲染出 data-settings-key`（含 `regular.window.reset-layout`）。
  - `settings-content.test.ts > 窗口行为：贴靠隐藏开关仍移除（#39）；还原默认布局恢复并说明保留标签/位置（#47）`。
  - `settings-search.test.ts > #47 搜索「还原默认布局」进入常规并定位到恢复按钮`（`data-search-hit` 点击后 `data-settings-page=regular`、行 `is-revealed`、按钮文案「还原默认布局」）。

### R2 只更新布局字段；保留预览标签及顺序、当前标签、各标签搜索词和阅读位置、聊天内容、草稿及当前话题
- 实现：`restoreDefaultLayout(prefs)` 只改 `leftWidth / leftCollapsed / rightWidth / rightOpen / previewFull`；不碰 `previewTabs`（含 `position.search / scrollTop / page…`）、`activePreviewTabId`、`taskTab`。聊天/草稿/当前话题本来就不在 `conversation_layout` 里（各自持久化），还原动作不调用任何其它接口。
- 证据：
  - `settings-reset-layout.test.ts > 栏宽/左栏收起/预览开合/手动全窗口回到默认；标签顺序、当前标签、搜索词与位置、任务页签保留`：断言保存 payload 与内存都只有布局字段变；两个标签顺序 `[a,b]`、当前 `a`、位置 `{scrollTop:120, search:"预算"}`/`{page:3, zoom:1.25}`、`taskTab=history` 全部保留；`api.saveConversationLayout` 只调 1 次。
  - `layoutPrefs.test.ts > #47 restoreDefaultLayout 只重置布局字段…`（纯函数不就地改输入）。

### R3 默认布局收起预览，重新打开回到原标签与原位置（含刷新后恢复）
- 实现：默认 `rightOpen=false`、`previewFull=false`，但 `previewTabs/activePreviewTabId/position` 原样保留；标签位置仍在 `conversationShell.prefs`，重新打开预览时 `conversationPreview.activeTab/positionOf` 直接读它。
- 证据：`settings-reset-layout.test.ts > 默认布局收起预览；重新打开回到原标签与原位置（刷新后按保存值恢复）`：
  - 还原后 `shell.rightVisible === false`；
  - 把保存 payload 走 `sanitizeLayoutPrefs` 模拟刷新读回：`rightOpen=false`、`activePreviewTabId="attachment:a"`、两个标签及位置不变；
  - `shell.setRightOpen(true)` 后 `preview.activeTab.id === "attachment:a"`、`preview.positionOf("attachment:a") === {scrollTop:120, search:"预算"}`；宽窗回到分栏（`effectivePreviewFull=false`、`splitRightVisible=true`）。

### R4 「拖动分隔线后立刻进设置并还原」的竞态；晚到旧回执不得覆盖还原结果；回对话后新标签/阅读位置不被旧数据覆盖
- 实现：
  1. 写请求**串行**（`_saveChain`）：旧写在途时，还原的写排在它后面 → 服务端最终值一定是还原后的；
  2. 回执只在 `layoutPrefsFingerprint(当前 prefs) === 发出时指纹` 时回填：等待期间还原/新开标签/滚位置都会让指纹变化，旧回执被丢弃（不是覆盖）；
  3. `resetLayout()` 先应用默认布局再 `flush()`，返回真实保存结果。
- 证据（两条专门用例）：
  - `settings-reset-layout.test.ts > #47 竞态… > 拖动分隔线后立刻还原：旧保存回执迟到，不覆盖还原后的布局`：旧保存（`leftWidth=380`）在途时点还原；旧回执返回旧布局后，`shell.prefs` 仍是 `LEFT_DEFAULT/RIGHT_DEFAULT/false`，`saveConversationLayout` 第 2 次写入的也是还原后的布局（顺序证明串行）；`saveError === ""`。
  - `settings-reset-layout.test.ts > #47 竞态… > 还原保存回执迟到：回到对话后新开的标签与新阅读位置不被旧回执覆盖`：还原请求携带旧标签 `[a,b]` 发出后，模拟回对话新开 `c`、把 `a` 的搜索词/滚动改成 `{scrollTop:999, search:"新搜索"}`；旧回执（`[a,b]`）到达后当前仍是 `[a,b,c]`、当前标签 `c`、新位置保留；随后 `flush` 的写入包含新标签/新位置（新状态自己落盘，不依赖旧回执）。

### R5 保存成功后给一次短反馈
- 实现：`saveNow` 的 success 回调只在 `commit`（`resetLayout` 真实保存）成功后触发，文案「已还原默认布局（预览标签与阅读位置保留）」，走 `NOTICE_OK_MS=4000` 自动收起（短反馈）。
- 证据：`settings-reset-layout.test.ts > 栏宽/左栏收起…` 与 `> 保存失败…重试成功后才给成功短反馈`：`.set-notice.ok` 含「已还原默认布局」，失败时无 ok 提示、有 err 提示。

### R6 保存失败明确显示失败并提供重试
- 实现：`resetLayout()` 返回 false 时 `commit` 抛错 → `useQioSettingsSave` 给出「还原默认布局失败」+ 技术详情（`shell.saveError` 真实原因）+ 「重试」按钮（重放同一次还原，含保存）；内存保留还原结果（与 store 既有"失败保留当前内存状态并可重试"一致，不显示成已保存）。
- 证据：`settings-reset-layout.test.ts > #47 失败与窄窗 > 保存失败：明确显示失败（含技术详情）并可重试；重试成功后才给成功短反馈`：`.set-notice.err` 有「还原默认布局失败」、`[data-settings-action="retry"]` 存在、`details` 含 `network down`、`shell.saveError` 含真实原因；点重试后 err 消失、ok 出现、`api.saveConversationLayout` 共 2 次、第二次 payload 是还原后的布局。

### R7 #39：固定星球/设置入口、Windows 贴靠保留；贴靠隐藏仍删除
- 实现：未改 floatingState / PlanetDock / WindowChrome / 入口贴靠；RegularSection 只恢复「还原布局」，两个「贴靠后自动隐藏」开关与索引仍然不存在；还原按钮不再调用 `resetFloatPositions`，只走对话布局偏好。
- 证据：
  - `settings-content.test.ts > 窗口行为…`：两个 hide 开关与 `regular.window.planet-hide/settings-hide` 仍不存在；`regular.window.close-behavior` 仍在。
  - 全仓 grep：`frontend/src` 内 `planet-hide|settings-hide` 只剩测试断言（索引/控件已无）。
  - `ConversationViewShell.test.ts`（未改，仍绿）：窄窗自动收起/恢复、预览贴边入口、全窗口 Esc、双击分隔条恢复默认宽度等既有行为不变。

### R8 双击分隔线仍是单侧快捷操作，设置按钮负责整套
- 实现：未改 ConversationView 的 `@dblclick="resetWidth('left')"` / 右侧同理；设置页按钮调用整套 `resetLayout()`。
- 证据：未改动的既有用例 `ConversationViewShell.test.ts > 双击分隔条恢复默认宽度` 仍绿（11 tests 全绿）。

### R9 窄窗临时折叠/全窗口继续由现有布局逻辑计算，且不被还原后的手动偏好覆盖
- 实现：`restoreDefaultLayout` 只清除手动偏好（`leftCollapsed=false / previewFull=false`）；`resolveConversationLayout` 仍按实际宽度推导临时收起/临时全窗口，且**不写回** prefs；保存 payload 里也永远是手动偏好（不会把临时状态写进去）。
- 证据：
  - `settings-reset-layout.test.ts > 窄窗临时收起/临时全窗口由布局推导，不写成手动偏好；拉宽后自动恢复`：还原后 `viewportWidth=680` → `leftVisible=false`、`autoCollapsedLeft=true`，但 `prefs.leftCollapsed=false`；打开预览 → `effectivePreviewFull=true`，但 `prefs.previewFull=false`；落盘 payload 的 `leftCollapsed/previewFull` 仍是 false；拉宽到 1440 → 自动恢复三栏+分栏。
  - `layoutPrefs.test.ts > #47 还原后的默认偏好仍按窄窗规则推导临时收起/临时全窗口，不写回手动偏好`（纯函数层同样证明）。
  - 既有 `ConversationViewShell.test.ts` 的「窄窗临时收起但 prefs.leftCollapsed 仍 false、变宽恢复」「1920 保存 700px 右栏，窄窗只显示收缩不改偏好」仍绿。

### R10 集成冲突里保留的 fold 不回退（搜「单段长度」先展开「记忆 · 高级选项」）
- 实现：`memory.fragment.max-tokens` 的 `fold: "memory.advanced"` 与短文案未动。
- 证据：`settings-search.test.ts` 收紧后的断言：`maxTokens?.fold === "memory.advanced"`、`searchSettings("单段长度")[0].key === "memory.fragment.max-tokens"`；`settings-content.test.ts` 中「高级选项默认收起」断言仍绿。

## 3. 跑过的命令与结果

| 命令 | 结果 |
| --- | --- |
| 基线（改动前）`npx vitest run src/views/settings/__tests__/settings-search.test.ts src/views/settings/__tests__/settings-content.test.ts src/conversation/layoutPrefs.test.ts src/views/__tests__/ConversationViewShell.test.ts` | 4 files / **58 passed** |
| 改动后定向 5 文件（见 §2） | 5 files / **70 passed**，exit 0 |
| 定向后的 4 文件复跑（layoutPrefs/settings-search/settings-content/ConversationViewShell） | **66 passed**（首轮修 test 脚本自身错误后） |
| 更广回归：`npx vitest run src/views/settings src/conversation src/components/conversation src/__tests__/pmqio032-shell-acceptance.test.ts src/views/__tests__ src/stores/__tests__ src/services/__tests__/uf2-notice-navigation.test.ts src/window-frame-contract.test.ts` | 196 files / **1620 passed, 1 failed**（失败与本项无关，见下） |
| 类型检查 `npx vue-tsc --noEmit` | **exit 0**（无输出） |

### 与 #47 无关的既有失败（已记录，未改他人文件）
- 失败用例：`src/services/__tests__/uf2-notice-navigation.test.ts > uf2- 没有页面宿主时兜底检查左栏：宽窗可见照常定位；窄窗收起则如实失败`，断言 `shell.viewportWidth = 1000` 时 `narrow.ok === false`，实际为 `true`。
- 单独复跑同文件：仍 1 failed / 10 passed（可稳定复现，不是测试顺序问题）。
- 判定为**合并 #83 后的既有失效断言**，与本项改动无关：
  - `git diff --numstat -- frontend/src/conversation/layoutPrefs.ts` = `61 0`（只新增函数，`resolveConversationLayout` 一行未动）；
  - `conversationShell.ts` 的 diff 只改了持久化/还原与注释，`leftVisible/autoCollapsedLeft/viewportWidth` 等 getter 无行为改动；
  - `layoutPrefs.test.ts` 的 #83 用例明确要求 1000px 仍三栏并排（旧 1180 断点已删），而该 stale 用例仍把 1000 当「窄窗收起」。
- 该文件不在本任务写入范围，未修改；建议 Lead 把它分给对应 owner 或作为合并遗留统一修。

## 4. 被改写的既有断言与理由（逐条）

1. `settings-content.test.ts` `A_OWNED_KEYS`：加回 `"regular.window.reset-layout"`。
   - 理由：#47 明确恢复该控件；覆盖用例要求 A 拥有的设置项都能渲染 data-settings-key。
2. `settings-content.test.ts > 常规：…`：`expect(regular.text()).not.toContain("还原默认布局")` → `toContain("还原默认布局")`；用例名同步。
   - 理由：#39 曾因"入口固定、还原浮动位置失效"移除；#47 重新定义为"还原对话页布局偏好"，控件真实可用，旧断言方向被用户定稿否定。
3. `settings-content.test.ts > 窗口行为：…`：原断言"两个贴靠隐藏开关 + 还原默认布局都已移除" → 拆分：贴靠隐藏开关/hide key **仍不存在**（保留 #39），`reset-layout` **存在**且文案说明保留预览标签/阅读位置；关闭行为仍在。
   - 理由：只恢复被 #47 指定的功能，不恢复已失效的贴边隐藏；防止测试失去 #39 保护。
4. `settings-search.test.ts` 失效入口用例：
   - 移除：`reset-layout` 不得出现在索引 / `searchSettings("还原默认布局") === []`；
   - 改为：索引必须含 `regular.window.reset-layout`，`searchSettings("还原默认布局")[0]` 命中、`searchSettings("重置布局")` 命中；
   - 保留：`searchSettings("贴边") === []`（#39）、免密钥/单段长度文案逐字、`memory.advanced` fold 与「单段长度」命中。
   - 理由：#47 要求搜索项恢复；其余断言是别的任务定稿，不能顺手回退。
5. 新增：`settings-reset-layout.test.ts`（7 用例）、`layoutPrefs.test.ts`（4 用例）、搜索定位用例；无删除/跳过既有测试。
6. `settingsApiMock.ts` 增加 `saveConversationLayout` 替身（默认回显）——纯测试基础设施补齐，不改变任何断言。

## 5. 未完成项 / 限制 / 自主产品决定

### 未完成 / 限制
- **未做真实应用 + IAB/桌面壳的视觉验收与手动点击验收**（本子任务在共享 worktree 内，后端/桌面壳未启动；避免与其它成员互相干扰）。验收步骤已用确定性用例覆盖到状态与持久化层，建议 Lead 在集成后按原验收步骤跑一次真机：宽窗调宽 → 收左栏 → 开两个预览标签并记录搜索词/滚动 → 进设置点还原 → 回对话重开预览 → 刷新；再测窄窗还原后拉宽、保存失败重试、迟到旧回执。
- 成功短反馈沿用共享保存模型：**同一分区还有其它未解决的保存错误时**，按 #65 既有语义优先显示错误（成功提示让位），不是本项引入。
- 保存失败时内存保留还原结果（不假装已保存，刷新后回到服务端旧值），这是 conversationShell 既有"失败保留当前内存状态并可重试"口径。
- 未改 `docs/**`（`docs/status.md`、`scripts/ui-catalog/README.md` 里仍有"还原默认布局"的旧表述，按纪律留给 Lead/文档 owner 同步）。

### 自主产品决定
1. 还原按钮沿用原位置与 key：常规 → 窗口行为 → 标题「布局」、`data-settings-key="regular.window.reset-layout"`、按钮文案「还原默认布局」；行说明明确"预览标签、搜索词和阅读位置保留"。
2. `taskTab`（左栏当前/历史）不属于"左右栏宽/收起/预览开合/全窗口"，保持不动（用户正在看历史时还原布局不应把他踢回当前）。
3. 还原前若尚未读过服务端布局（启动直接进设置），先 `load()` 再还原并保存：否则会用空默认标签把服务端已有标签/阅读位置清掉；读取失败则不写、报错可重试（有专门用例）。
4. 保存改串行 + 内容指纹回执守卫（不是比较时间戳）：既保证服务端最终值，又支持"同一内容重复保存/服务端不回显"的既有 mock 场景。
5. `flush()` 返回 `Promise<boolean>` 并等真实结果，供设置页在**保存成功之后**才给成功反馈；既有 `void shell.flush()` 调用保持兼容。
6. 还原失败保留内存还原结果并给「重试」，不自动回滚到旧布局（避免用户点了还原却看起来没生效，同时明确告知保存失败）。
