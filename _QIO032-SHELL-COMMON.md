# QIO 0.3.2 · shell/交互外壳审计修复组（pm-qio032-shell）共同契约

> 本文件由 **Lead** 维护；四个子分支开工前必读，冲突时以本文件为准。
> 基线：`7445170d0f51756b2740eb7209d798540d8042a2`（v0.3.2 已知限制标注）。
> 父分支 / 父 worktree：`fix/pm-qio032-shell` / `D:\\qio-dev\\pm032-shell`。
> 本组不接收其他顶层对话（interactive / planet / records / settings 各组）的代码；不 fetch、不 pull、不 push、不触发 CI、不开 PR、不发布。

## 0. 分支与 worktree（已由 Lead 建好，勿再新建）

| 子 agent | 分支 | worktree | 主责 |
| --- | --- | --- | --- |
| shell-conversation | `fix/pm-qio032-shell-conversation` | `D:\\qio-dev\\pm032-shell-conversation` | ConversationView、conversationShell、对话布局、焦点、布局复位、页面接线；#38 #51 #83，接入 #47 #50 #78 #79 |
| shell-preview | `fix/pm-qio032-shell-preview` | `D:\\qio-dev\\pm032-shell-preview` | PreviewPanel、预览子组件、预览状态与标签持久化；#50 #52 #53 |
| shell-recovery | `fix/pm-qio032-shell-recovery` | `D:\\qio-dev\\pm032-shell-recovery` | Composer、发送队列、失败原文、对话任务、RecoveryInbox、恢复服务；#42 #77 #78 #79 |
| shell-navigation | `fix/pm-qio032-shell-navigation` | `D:\\qio-dev\\pm032-shell-navigation` | SettingsView/SettingsNav/SettingsSearchPanel/searchIndex/RegularSection/WindowChrome/PlanetView/PlanetDock/PageTools；#36 #37 #39 #47 #48 #66 #67 #75 |

所有 worktree 已 junction `frontend/node_modules` → `D:\\qio-dev\\qio\\frontend\\node_modules`。**不要 npm install、不要新增依赖、不要跑全量测试/构建/后端 pytest/cargo/CI、不要 push。**

## 1. 运行命令（在你的 worktree 里）

```powershell
cd D:\\qio-dev\\pm032-shell-<slice>\\frontend
npx vitest run src/<你的定向测试路径> [更多路径]     # 定向测试，先红后绿
npx vue-tsc --noEmit                                  # 交付前一次即可
```

禁止：`npm test`（全量）、`npm run build`、任何 git push/fetch、启动 CI、真实模型/真实 Key、联网测试。
需要看基线行为可读 `D:\\qio-dev\\pm032-shell\\`（父 worktree，基线同一 SHA）。

## 2. 写作纪律

- 只改你名下的文件（见 §3）；别人的文件只读。需要跨范围修改：**发消息给 lead**，不要自己改。
- 小步提交，提交信息中文 + 范围前缀（如 `fix(shell-layout): ...`、`test(preview): ...`）。每条提交只做一件事。
- 先写/改测试复现问题 → 跑到红 → 最小实现 → 绿。可以修改基线里“编码了被本项否定的旧行为”的断言，但必须在提交信息里注明“旧行为被本项否定”。
- 不写会过期的硬编码数字；不写会伪造成功的提示；失败必须如实。
- 颜色只用 `var(--*)`；字体三声部；不改设计令牌（需要新令牌先问 Lead）。
- 每条提交前自查：没有把密钥/真实 Key 写进代码、日志、测试输出。

## 3. 文件归属（唯一写入者）

**shell-conversation**
- `frontend/src/views/ConversationView.vue`（唯一写者）
- `frontend/src/stores/conversationShell.ts`
- `frontend/src/components/conversation/ChatColumn.vue`
- `frontend/src/conversation/layoutPrefs.ts`、`layoutPrefs.test.ts`
- `frontend/src/views/__tests__/ConversationView*.test.ts` 等对话页测试
- 只读：PreviewPanel / Preview 子组件、RecoveryInbox、TaskRail、Composer*、PlanetView、WindowChrome、settings/*

**shell-preview**
- `frontend/src/components/conversation/PreviewPanel.vue`
- `frontend/src/components/conversation/preview/*`（全部子组件）
- `frontend/src/stores/conversationPreview.ts`
- `frontend/src/conversation/previewTypes.ts`
- `frontend/src/components/conversation/previewBridge.ts`
- `frontend/src/components/conversation/__tests__/cv-c-preview-*.test.ts`、`d-preview-*.test.ts` 等预览测试
- 只读：conversationShell（布局偏好读取由 conversation 负责）、ConversationView

**shell-recovery**
- `frontend/src/components/Composer.vue`
- `frontend/src/components/conversation/ComposerDock.vue`、`ComposerEntries.vue`、`QueuePanel.vue`、`SendIssuesPanel.vue`、`TaskRail.vue`、`TaskRailItem.vue`、`taskLocator.ts`、`entries.ts`
- `frontend/src/components/RecoveryInbox.vue`
- `frontend/src/components/ContinueBar.vue`
- `frontend/src/stores/conversationTasks.ts`、`frontend/src/services/conversationTasks.ts`
- `frontend/src/services/recoveryApi.ts`
- `frontend/src/stores/session.ts`（仅发送身份/失败回执/草稿取回相关段落）
- 相关测试：`components/__tests__/*Recovery*`、`Composer*`、`QueuePanel`、`TaskRail`、`SendIssuesPanel`、`stores/__tests__/*recovery*` 等
- 只读：ConversationView、ChatColumn、PreviewPanel、settings/*、WindowChrome

**shell-navigation**
- `frontend/src/views/SettingsView.vue`、`views/settings/**`（SettingsNav、SettingsSearchPanel、SettingRow、SettingSection 等）
- `frontend/src/views/settings/searchIndex.ts`
- `frontend/src/views/settings/sections/RegularSection.vue`
- `frontend/src/components/WindowChrome.vue`
- `frontend/src/views/PlanetView.vue`
- `frontend/src/components/PlanetDock.vue`、`frontend/src/components/planet/PageTools.vue`
- `frontend/src/composables/useFloatingWindow.ts`、`floatingState.ts`、`useQioTip.ts`、`planetContinuum.ts`（仅入口相关段落）
- `frontend/src/services/quitGuard.ts`（仅关窗文案/说明相关段落）
- `frontend/src/router.ts`（仅 #48 的来源/返回位置所需）
- 相关测试：`views/__tests__/SettingsView.test.ts`、`settings/__tests__/*`、`WindowChrome.test.ts`、`views/__tests__/PlanetView.test.ts`、`r17-c-planet-nav.test.ts`、`PlanetDock.test.ts` 等
- 只读：ConversationView、ChatColumn、PreviewPanel、RecoveryInbox、Composer；`ManagementArea.vue` 接口保持不变（避免通过改它的接口解决 #75，优先 PlanetView 容器定位）
- **跨组注意**：settings/planet 的其他顶层对话在各自 worktree 并行改同类文件；不要看/不要合并它们的代码，只按本组任务改。

**Lead（不要动）**：`frontend/src/conversation/contracts.ts`、`frontend/src/components/approval/*`、`frontend/src/components/conversation/UnifiedApprovalLayer.vue`、`frontend/src/App.vue`、`frontend/src/styles/*`、`docs/**`、`scripts/**`、`_reports/**`、父分支 git 操作。
共享可写（尽量别动，动了必须告诉 Lead 具体行）：`frontend/src/services/api.ts`、`frontend/src/utils/windowFrame.ts`。

## 4. 跨分支冻结接口（本组集成契约，必须遵守）

1. **未完成事项打开事件**（recovery 发 → conversation 收）：
   `window.dispatchEvent(new CustomEvent("qio:open-recovery", { detail: { recordId?: string } }))`。
   conversation 现有监听升级为读取 `detail.recordId`，作为 `focus-record-id` 传给 `<RecoveryInbox :focus-record-id="...">`。
2. **RecoveryInbox**：新增可选 prop `focusRecordId?: string | null`（变化时滚动并聚焦同一条；目标已不存在时给真实反馈）；新增 `defineExpose({ handleEscape(): boolean })`——消费了 Esc（如收了内层详情）返回 true，否则 false。conversation 在 Esc 处理时先调它，false 再关弹窗。弹窗背景限制与焦点归还在 conversation 页实现。
3. **PlanetView**（navigation 实现，conversation 消费）：保留 `:open`、`:seq`、`:initial-tab`、`:focus-pending-id` 与 `@close`；其根层必须是 `role="dialog"` + `aria-modal="true"` + `data-planet-overlay`，Tab 在星球内循环；打开时内部控件可用、退出后不抢页面焦点。conversation 负责：打开时给背景（workspace 行/预览层/恢复层）加 `inert`，关闭后把焦点还给星球入口（`[data-planet-entry]`）。
4. **PreviewPanel**（preview 实现，conversation 消费）：保留 `@close`、`@toggle-full`；全窗口模式根节点带 `data-preview-full`；preview 内部消费 Esc（关闭阅读子层等）时必须 `preventDefault()`，conversation 只在 `!event.defaultPrevented` 时退出全窗口。conversation 负责全窗口时背景 `inert` + 收起后焦点归还给触发的标签/按钮。
5. **布局偏好唯一来源**：`stores/conversationShell.ts` 的 `prefs`（服务端 `/api/settings/ui` 的 `conversation_layout`）。preview 只通过 shell store 读写标签/阅读位置；recovery/navigation 不写。conversation 必须用**实际内容视口宽度**（`utils/windowFrame.contentViewport().width`）计算布局，不再用硬编码 1440 做夹取/断点判断。
6. **ChatColumn 槽位**（conversation 保持）：`#planet` 槽渲染 `<PlanetDock anchored>`；Composer/ContinueBar/TaskRail 等既有子组件保持基线接口（recovery 只能加可选 props/events，不能删改既有接口）。
7. **TaskRail 任务动作**：`open_recovery` 动作点击时 recovery 负责发 §4.1 事件（带 recordId），conversation 负责打开恢复窗口并聚焦；目标消失时 recovery 给真实反馈。
8. 任何需要新增跨分支 import 模块的做法都禁止；只通过上述 props/events/CustomEvent 通信。

## 5. 逐项修复（本组任务书全文，按主责分派）

### shell-conversation
- **#38** 星球覆盖对话时，下层控件不可通过 Tab、Enter 或鼠标操作；星球内的可用控件正常，退出归还焦点。
- **#51** 全窗口预览打开后隔离背景对话。Tab、Enter 和鼠标不能操作下层；收起后恢复原触发焦点。普通分栏保持双方可操作。
- **#83** 依据中央实际可用宽度安排任务栏、预览和输入区。保证输入可用，减少断点两侧的宽度和中心跳变；用户的手动布局偏好仍有效。
- 接入 **#47**（恢复实际可调的栏宽、收起状态等布局偏好；保留文件标签、阅读位置和内容；星球与设置入口仍按固定布局显示；保存失败时避免成功假象）、**#50**（大窗手动全窗口预览返回分栏；小窗自动全窗口时「返回对话」和 Esc 收起预览并保留标签、搜索和阅读位置）、**#78/#79**（页面接线：打开恢复窗口、聚焦目标、ESC 分层、背景限制、焦点归还）。

### shell-preview
- **#50** 大窗手动全窗口预览返回分栏；小窗自动全窗口时「返回对话」和 Esc 收起预览，并保留标签、搜索和阅读位置。
- **#52** 标签快捷键使用实际焦点所指标签。乙选中但焦点在关闭甲时，操作不能误关乙。
- **#53** 切文件前立即记录当前标签最新搜索词和位置。先改词再立刻切走也能保留；旧标签的迟到写入不能覆盖后来编辑。

### shell-recovery
- **#42** 请求提交时锁定原话题和请求身份。甲的失败回执只能在甲处理，切到乙后不得回填乙；甲已有新草稿时从失败事项主动取回，保留两份内容。
- **#77** 重试保存原记录及原失败动作。忽略失败只能重试忽略，开始前检查动作仍有效。
- **#78** 从任务栏点某条「去处理」时，恢复窗口滚动并聚焦到同一条；目标已消失时给真实反馈。
- **#79** 未完成事项弹窗限制背景交互，Esc 先收详情再收弹窗，关闭后归还焦点。

### shell-navigation
- **#36** 星球收起、搜索、管理按钮根据可见窗口边界和标题栏实际底边定位。透明留白不能吃掉按钮上沿或左边，顶部拖动区不得抢点击。
- **#37** 进入星球时清除入口悬停、聚焦提示及迟到定时器；退出后等重新悬停或聚焦。
- **#39** 保留定稿 A：星球固定在对话输入区旁、设置固定在全局顶栏。移除两个入口已失效的贴边隐藏设置、搜索索引和提示，系统窗口贴靠继续工作。
- **#47** 恢复实际可调的栏宽、收起状态等布局偏好。保留文件标签、阅读位置和内容，星球与设置入口仍按固定布局显示。保存失败时避免成功假象。（与 conversation 配合：store/消费方在 conversation；设置侧读取/保存失败反馈与入口固定布局在 navigation）
- **#48** 设置保存进入来源及需要恢复的位置，从互动进入就回原板面；直接打开设置时默认返回对话。
- **#66** 设置搜索用分类标题区分结果，删掉结果行重复的分类。
- **#67** 设置搜索 Esc 关闭后把焦点交还搜索入口；中文输入法选字优先。
- **#75** 星球内部管理导航整体避开标题栏，宽窗右侧布局和小窗上下布局分别验证。

## 6. 18 处文案（逐条定位；`[owner]` 已分派）

1. 文件预览标题只保留一次「只读」，关标签提示用「关闭预览」。**[preview]**
2. 审批关闭用「稍后处理」，需要时只说明一次申请会保留。**[Lead]**（components/approval/*）
3. 发送异常取消的中间状态用「正在取消」，收到回执后再更新。**[recovery]**
4. 草稿取回写清与当前草稿互换、两份保留；按钮「交换草稿」。**[recovery]**
5. 未完成事项用一个标题「未完成事项 · N」。**[recovery 组件 + conversation 去掉页面重复标题]**
6. 删除未完成事项通用导语，把具体后果放在对应动作旁。**[recovery 组件 + conversation 去掉页面通用导语]**
7. 多个动作共用的禁用原因只显示一次，不同原因分别保留。**[recovery]**
8. 去掉「去处理」成功后的背景说明，失败才提示。**[recovery]**
9. 任务列表悬停只写「刷新任务列表」。**[recovery]**
10. 话题说明的关闭提示只写「收起话题说明」。**[conversation]**
11. 队列常态用「待发送消息」，取消回执用实际状态，去掉两处后端确认解释。**[recovery]**
12. 继续／停止操作条保留真实暂停原因和按钮。**[recovery]**
13. 删除两种已失效贴靠隐藏设置对应的搜索结果。**[navigation]**
14. 免密钥搜索结果说明用「使用免密钥搜索服务」。**[navigation]**
15. 单段长度搜索说明用「单段长度上限（按估算字符数计）」。**[navigation]**
16. 关窗说明只交代托盘时继续运行、退出后有效待审批事项的去向。**[navigation]**
17. 话题说明为空用「暂无话题说明」。**[conversation]**
18. 常态历史续接不另写说明；选了历史片段时提示下一条从哪里继续。**[conversation]**

Lead 另负责：审批关闭文案、未分派杂项、最终集成、docs/status.md、验收证据。

## 7. 交付与汇报（必须）

完成实现与定向验证后：

1. 在 **父 worktree** 写报告：`D:\\qio-dev\\pm032-shell\\_reports\\<slice>.md`（`shell-conversation` / `shell-preview` / `shell-recovery` / `shell-navigation`），内容：
   - 分支名 + 全部 commit SHA；
   - 改动/新增文件精确清单；
   - 逐项（#编号/文案号）修复说明与复现证据（测试文件+用例名+红→绿）；
   - 跑过的定向命令与结果（成功/失败条数）；
   - 没做到/已知限制/需要 Lead 集成时注意的接缝；
   - 未验证环境（真机窗口贴靠、真实浏览器、中文输入法等）。
2. 用 `send_message` 给 lead 发一段**短**汇报（≤20 行）：分支、HEAD SHA、测试结果、未完成项、风险。
3. 不要自行合并父分支、不要 push、不要更新 docs/status.md（Lead 统一做）。

## 8. 验收口径（Lead 最终会跑）

- 故障注入抓发送与恢复请求，逐项核对话题 ID、事项 ID、动作、请求次数；模拟甲失败回执晚到、忽略返回 503、恢复目标多条、快速换预览标签。
- 800×600 内容区、宽窗、断点两侧：窗口边界与按钮命中；Tab / Enter / Esc / 鼠标 / 中文输入法；设置往返与布局复位后标签、阅读位置都在。
- 前端 `npx vue-tsc --noEmit` + 相关 vitest 全绿；`python scripts/check_docs.py`。
- 真机 Windows 普通窗/最大化/系统贴靠如无实机，单列 UNVERIFIED。
