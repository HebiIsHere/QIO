# PM-QIO032 · i-layout 报告（fix/pm-qio032-i-layout）

> 子智能体：i-layout（互动页页面级接线与外壳） · 基线 7445170d · 交付第二版（2026-11-12 23:20）

## 1. 分支 / worktree / commits

- 分支：`fix/pm-qio032-i-layout`
- worktree：`D:\qio-dev\pm-qio032-i-layout`
- 基线：`7445170d0f51756b2740eb7209d798540d8042a2`
- commits：
  - `1a32b24f` fix(interactive-layout): 页面壳面板空间/失败门禁/单一聊天入口与提交区状态文案
  - `65e130f5` fix(interactive-layout): 工具区连续承载面、Esc 先取消拖动、任务入口失败标记与测试口径
  - 报告提交：见本文件所在 commit

## 2. 改动 / 新增文件清单

源码（全部在 CONTRACT §2 i-layout 名下）：
- `frontend/src/views/InteractiveView.vue`
- `frontend/src/components/interactive/BoardToolbar.vue`
- `frontend/src/components/interactive/SubmitCluster.vue`
- `frontend/src/components/interactive/BoardChangeList.vue`
- `frontend/src/components/interactive/v2/InteractiveConversation.vue`
- `frontend/src/components/interactive/v2/InteractiveBookmarks.vue`
- `frontend/src/components/interactive/v2/InteractiveChromeTools.vue`
- `frontend/src/components/interactive/v2/conversationLayout.ts`（交互区纯函数，i-layout 消费/维护）
- `frontend/src/components/interactive/v2/layout.ts`（仅追加面板空间规则纯函数）
- `frontend/src/components/MessageStream.vue`（**只改 #16 互动专属空态**：新增可选 prop `emptyVariant`，其它模式空态保持原样）

测试：
- 新增 `frontend/src/views/__tests__/pm032-layout-shell.test.ts`（30 个用例）
- 更新既有测试（原因逐条见 §6）：conversationLayout / InteractiveConversation / a-unknown-consistency / i2v2-a-intent-read-failure / p1b-v2-chrome-tools / submitClusterPanels

## 3. 逐项结果表

| # | 状态 | 证据 / 说明 |
|---|---|---|
| #05 | 部分（UI 闭环，端到端待 Lead） | `v2/layout.ts` 新增 `PANEL_SIDE_BY_SIDE_MIN_WIDTH=1121` / `panelSpaceMode()`；页面壳窄窗（≤1120）任务面板与提交详情互相明确关闭（`onSubmitPanelOpen` / watch `store.tasksOpen` → `closeSubmitDetails`），关闭只收面板不改各自数据；聊天草稿/回复由会话层保留。pm032 用例覆盖 1120/1121/800 边界。**800×600 与 1121/1120 真机命中待 Lead E2E** |
| #71 | 部分/基线已满足 | 层级不变量测试：执行记录 z=+24/+25 > 聊天 +20 > 任务面板 +10，任务+聊天同时打开遮不住执行记录；纳入同一空间规则。**视觉 E2E 待 Lead** |
| #14 | 已修复 | BoardToolbar 删除「标注（未开放）」入口、菜单、样式与状态（源码不再含 annotate/未开放）；pm032「#14」用例 |
| #15 | 已修复（结构） | 对话卡内：工具槽一排（BoardToolbar）在输入区一排上方；板面「添加」在工具栏，随消息附件在 ComposerEntries；聊天开合仍输入区旁单入口。pm032「#15」 |
| #16 | 已修复 | MessageStream 新增可选 `emptyVariant="interactive"`：互动空态「发消息，或提交板面 / 在下方输入框发消息；板面改动准备好后点「提交」」；默认空态（含话题星球）逐字未变；InteractiveConversation 传该 prop。pm032「#16」 |
| #20 | 部分（UI 全部实现；首次失败区分依赖 i-state 字段） | 有上次数据：只读横幅 + 重试 + 画布/工具栏 `inert`；首次失败：整页「板面没有读取出来」+「重试」「返回对话」。优先读 i-state `boardLoadedOnce/boardReadOnly/boardEditBlockedReason/retryBoardLoad`（可选读取，合并后自动生效）；基线回退 loadError+board。i2v2 板面失败用例已改为基线等价路径，pm032 另测源码结构。**首次失败真机区分待 i-state 合并后 Lead E2E** |
| #40 | 已修复（层叠 + 输入法优先） | 统一用共享 `useEscapeLayer`：对话展开（动态注册）/ 提交详情面板 / 任务面板（页面壳）/ 书签栏各自注册；后打开者优先，`isComposingKeyEvent` 保证中文输入法先取消选字，确认框走同一层级栈天然优先；拖动高度时 Esc 先取消拖动（不回退展开态）。Esc 只收显示：不清草稿、不停止回复。pm032「#40」+ InteractiveConversation Esc 用例 |
| #41 | 部分 | 提交详情区分：读取失败（`pendingError` + 「重试读取」）、成功为空（「本次没有改动」）、有上次结果（分开说明）；`understandingLoadError` 维度的第三态未接（i-state 建议用 lastSubmission+understandingLoadError，基线 store 已有 understandingLoadError，但详情面板语义待与 i-tasks/i-state 对齐）。pm032 只断言已有结构 |
| #55 | 已修复 | 板内搜索无命中：结果层保留并显示「没有找到卡片」；清空关键词收起。旧用例（断言“不渲染任何结果层/说明”）按新验收改写为断言新行为（见 §6）。pm032「#55」 |
| #73 | 已修复 | 书签栏：单行紧凑、关闭入口在列表头部、打开时注册 Esc 可收起、长列表 `flex:1 + overflow-y:auto` 单独滚动。pm032「#73」 |
| #76 | 已修复 | 悬浮工具区改为一枚连续、轻量承载面（半透明 + 模糊 + 胶囊），按钮退回透明表面；卡片文字/连线只从承载面后方经过，不从按钮间隙穿出。pm032「#76」 |
| #80（UI） | 已修复 | 重复提交主状态「已提交过」+「查看原进度」→ 打开上次提交结果面板（原受理记录，不再次执行）。pm032「#80」 |
| #81（UI） | 已修复 | 回执待确认底部只留一行「提交结果待确认，改动已保留」+「查提交结果」；查证/重新读取与 `acceptanceLookupNote`（i-state 已改「暂未查到这次提交」）收进按需展开的提交详情。pm032「#81」 |
| #86 | 已修复 | 删除消息滚动区上方重复关闭按钮 `data-ic-close` 与其样式；唯一入口为输入区旁 `data-ic-toggle`。InteractiveConversation 用例改为断言“只有一个入口且消息区无关闭按钮” |
| #88 | 已修复（组件实现 + 纯函数） | 新增 `conversationComposerLayout()`（按实测宽度，非视口）：不足时 stacked（模式入口移到输入框上方、与输入框同 860 列，圆点完整可见）；够宽才 inline；工具组与输入框同轴（无 `padding-left:92px`）。悬浮对话卡最大宽 980→1180 给宽窗留空间。jsdom 首帧默认 stacked。pm032「#88」 |
| #03（UI） | 已修复/部分 | 提交区唯一主状态：优先 `roundStatusLabel`（i-state），回退既有 submitStatus；unknown/duplicate 用任务书措辞；`roundStatusDetail` 进按需展开详情；轮次迟到事实不倒退由 store 保证。pm032「#03」 |
| #13（UI） | 已修复 | 提交按钮 `:disabled="busy || !canSubmit"`，禁用时提示 `submitBlockedReason`（回退“没有可提交的有效改动：空白卡不算…”）；不再自算第二套口径。pm032「#13」 |
| 文案 1 | 已修复（UI） | 待确认只写「提交结果待确认，改动已保留」；「暂未查到这次提交」由 store 的 acceptanceLookupNote 提供，UI 不改写。pm032「文案1」 |
| 文案 2 | 已修复 | BoardChangeList 标题「本次改动 · N 项」；行内类别名只出现一次（summary 前缀重复类别时剥离）。旧用例同步（见 §6）。 |
| 文案 3 | 已修复 | 详情按钮 aria-label / 悬停提示统一「提交详情」。 |
| 文案 4 | 已修复 | 展开入口只写「展开对话」（去掉「点开继续对话」与展开方向说明）；无真实状态时状态行安静。 |
| 文案 5 | 已修复 | 任务入口悬停/标题只写「互动任务」（失败时标题「互动任务（读取失败）」）。 |
| 文案 13 | 已修复 | 收起只写「收起对话」；折叠后有进行中回复时状态行显示真实状态（正在回复/排队/草稿）。 |
| #07（入口部分，i-tasks 请求） | 已修复 | tasks-entry 在 `intentViewsLoadState==='error'` 时加 `data-im="tasks-entry-error"` + `aria-invalid` + 标题「互动任务（读取失败）」，同一次失败只标记一次；移除消息区重复的 intent-read-error 横幅（重试在 TaskCenterPanel，由 i-tasks 实现） |

## 4. 定向测试命令与结果

```powershell
cd D:\qio-dev\pm-qio032-i-layout\frontend
npx vitest run --reporter=dot \
  src/views/__tests__/pm032-layout-shell.test.ts \
  src/components/interactive/v2/__tests__/conversationLayout.test.ts \
  src/components/interactive/v2/__tests__/InteractiveConversation.test.ts \
  src/components/interactive/__tests__/a-unknown-consistency.test.ts \
  src/views/__tests__/i2v2-a-intent-read-failure.test.ts \
  src/components/interactive/__tests__/BoardToolbar.test.ts \
  src/components/interactive/__tests__/submitClusterPanels.test.ts \
  src/components/interactive/__tests__/SubmitClusterFailure.test.ts \
  src/components/interactive/__tests__/irl-d-submit-status.test.ts \
  src/components/interactive/__tests__/p1b-v2-chrome-tools.test.ts \
  src/components/interactive/__tests__/p1b-v2-layout.test.ts \
  src/components/interactive/__tests__/p1b-v2-workspace.test.ts \
  src/components/interactive/__tests__/p1b-v2-visual-guards.test.ts \
  src/interactive/__tests__/irl-c-layout.test.ts \
  src/views/__tests__/InteractiveViewFloating.test.ts \
  src/views/__tests__/InteractiveOldEntriesGone.test.ts \
  src/components/__tests__/MessageStream.test.ts
# 结果：Test Files 17 passed (17) / Tests 196 passed (196)

npx vue-tsc --noEmit
# 结果：exit 0（自己范围 0 错误）
```

未跑：全量 pytest / npm test / build / cargo / 真实网络或模型（按 CONTRACT §4）。

## 5. 新增/变更对外接口与消费方注意

- `interactive/v2/conversationLayout.ts`：新增 `CONVERSATION_INLINE_MIN_WIDTH`、`conversationComposerLayout(width)`；`conversationStatusText()` 的 idle 返回值由「点开继续对话」改为空串（文案 4）。消费方：InteractiveConversation（本分支）、测试。
- `interactive/v2/layout.ts`：新增 `PANEL_SIDE_BY_SIDE_MIN_WIDTH`、`PanelSpaceMode`、`panelSpaceMode(width)`，既有 Workspace 布局导出不变。
- `SubmitCluster.vue`：新增 emit `panel-open`、expose `closePanel()` / `isPanelOpen()`；新增内部可选读取 i-state 字段 `canSubmit / submitBlockedReason / roundStatusLabel / roundStatusDetail`（全部可选，未落地时回退既有状态）。**不改 store**。
- `BoardToolbar.vue`：新增 emit `panel-open`、expose `closeSubmitDetails()` / `submitDetailsOpen()`；删除标注入口相关 DOM/data（`annotate-*`）——若有 E2E 选择器依赖需同步删。
- `InteractiveConversation.vue`：删除 `data-ic-close`；根节点新增 `data-ic-layout="inline|stacked"`；MessageStream 以 `empty-variant="interactive"` 使用新空态；Esc 层 id `interactive-conversation`。
- `MessageStream.vue`：新增可选 prop `emptyVariant?: "default" | "interactive"`（默认 default，其它入口逐字不变）。
- `InteractiveBookmarks.vue`：Esc 层 id `interactive-bookmarks`。
- `InteractiveView.vue`：根节点新增 `data-im-panel-space="side-by-side|switch"`；新增 `data-im="board-load-failed" | "board-load-retry" | "board-load-back" | "submit-duplicate" 等选择器；任务入口失败标记 `data-im="tasks-entry-error"`。
- 依赖 i-state 承诺接口（可选读取，合并后自动生效）：`boardLoadedOnce / boardReadOnly / boardEditBlockedReason / retryBoardLoad / canSubmit / submitBlockedReason / roundStatusLabel / roundStatusDetail`。字段名如需微调，只改 3 处 computed 的读取即可。
- 与 i-tasks 约定：TaskCenterPanel 内不注册 Esc（页面壳注册 tasksOpen 一层）；任务入口失败标记仅页面壳；面板内错误/重试 `task-read-error` 由 i-tasks 实现。

## 6. 变更过的既有测试（逐条说明，未删除/跳过，未弱化覆盖）

1. `v2/__tests__/conversationLayout.test.ts`：idle 状态文案由「点开继续对话」改为空串 —— 文案 4 明确要求去掉该句；其余断言（回复中/排队/草稿）不变。
2. `v2/__tests__/InteractiveConversation.test.ts`：
   - 原「点击右上角圆形按钮收起」改为「消息区无 `data-ic-close`、只有一个 `data-ic-toggle`、点击同一入口收起」——#86 直接要求移除重叠关闭按钮，属规格变更而非弱化；
   - 高度拖动用例新增 Esc 语义：拖动中 Esc 先取消拖动（恢复拖动前高度、对话保持展开），把「取消拖动」与「收起面板」的层级关系写死。
3. `components/interactive/__tests__/a-unknown-consistency.test.ts`：unknown 底部文案断言改为「提交结果待确认，改动已保留」+「查提交结果」；「按请求身份/不会自动重复提交」断言移到按需展开的提交详情（**信息仍在，且更严格地断言了它不在底部常驻**）；查证按钮点击与“不会顺带提交”断言保留。
4. `views/__tests__/i2v2-a-intent-read-failure.test.ts`：按 #07 改为「任务入口只标记一次 + 消息区无重复横幅」；重试改走 store 的单点重试（TaskCenterPanel 内 UI 由 i-tasks 分支提供，避免本分支依赖未合并 UI）；板面失败用例在基线 store 上走「只读 + 重试」等价路径，并注明 `boardLoadedOnce` 合并后才区分首次失败（页面壳两入口已由 pm032 源码用例锁定）。旧核心断言（不清旧内容、迟到失败不覆盖、重试恢复意图）全部保留。
5. `components/interactive/__tests__/p1b-v2-chrome-tools.test.ts`：旧用例断言「无命中不渲染结果层/无说明文字」，与 #55 新验收相反，改为断言「结果层保留 + 没有找到卡片 + 清空关键词收起」；搜索框只含 query 的约束保留并加强。
6. `components/interactive/__tests__/submitClusterPanels.test.ts`：详情里改动断言由完整字符串「新增注释：本次要提交的那条」改为「本次要提交的那条」+ 断言类别「新增注释」在详情文本中恰出现 1 次 —— 文案 2 要求每行只出现一次类别，属规格变更；数据不串用、上次结果不掺入改动列表等断言不变。

## 7. 未做 / 集成风险

1. **真机 E2E 未做**（按分工由 Lead）：800×600 单开/组合开面板、1121/1120 实际边界与中心命中、Tab/Esc、净白/暗紫晶主题、中文输入法。本分支只做组件/纯函数级证据。
2. #05/#71 的“并排”几何：本分支实现「窄窗明确切换 + 层级不遮挡」；宽窗任务面板与提交详情本身不重叠（一个右上、一个底部工具栏上方），未新增并排容器。若 Lead E2E 发现 1121+ 仍有重叠，需要 TaskCenterPanel 定位配合（i-tasks 文件）。
3. #20 首次失败与「上次结果」的区分、#03 全轮次标签、#13 门禁原因：依赖 i-state 分支（commit 103c0126）合并；本分支已按字段可选消费，合并前走基线回退。合并后请跑一次真机失败注入验证。
4. #41 仍缺 `understandingLoadError` 维度的第三态与失败重试入口（当前 pendingExpressions 失败已可重试；理解/意图读取失败重试入口在任务面板）。
5. i-tasks 的 TaskCenterPanel 错误/重试 UI 与 Esc 层级、i-state 的 `acceptanceLookupNote` 文案变更需在父分支合并后联调；本分支未改它们文件。
6. 未自造 store 状态：所有跨分支数据均以可选读取 + 基线回退消费，类型检查在本 worktree 通过（vue-tsc exit 0）。
