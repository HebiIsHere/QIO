# 全应用提示、通知与弹窗统一改造（2026-10-26）

- 任务分支：`fix/ui-feedback-unification-20261026`
- 基线：`fix/post-merge-followups-20261026` @ `e5c802533b4587fce5eb14a5e1d665cbf27f5023`（开工时核对远端，未变化）
- 集成 worktree：`D:\\qio-dev\\quf-lead`；并行开发分支：`wt/uf-20261026-a-foundation` / `-b-feedback` / `-c-chat` / `-d-pages`（各自独立 worktree，已合入本分支）
- 功能代码 SHA：`12700957`（在 `c941e2b0` 之后只做了互动锚点/对话展开按钮的原生 title → 统一用途说明替换；完整 CI 在最终 HEAD 上运行）
- 状态：**未合并 main、未发布、未打包正式版本**

## 1. 范围与边界

本次只改前端提示/通知/弹窗及其基础控件与视觉令牌；不重新设计对话页、互动板、记忆星球的结构，不改源文件操作约定、权限边界、审批真实期限、任务执行逻辑、恢复保护、模型能力与更新/关闭执行机制，不升级依赖、不改后端、不改 schema。

## 2. 公共能力（唯一来源）

### 2.1 基础控件（A）
- `frontend/src/styles/tokens.css`：唯一圆角令牌 `--qio-ui-radius: 14px`；用途说明层级 `--qio-z-tip`。
- `frontend/src/styles/base.css`：`.qio-btn/.qio-input/.qio-select/.qio-badge/.qio-tag/.qio-state/.qio-row/.qio-card/.qio-inline-edit/.qio-floating` 等本次涉及表面统一 14px；新增 `.qio-modal-*` 对称弹窗骨架与轻浮层动效；不使用全局 `*` 圆角规则。
- 新增 `components/ui/QIcon.vue` + `icons.ts`（47 个统一 24 viewBox 线性图标）、`QIconButton.vue`（aria-label 必填、14px 容器、触控 44×44、500ms 用途说明、键盘聚焦立即显示）、`QTipLayer.vue` + `composables/useQioTip.ts`（全局唯一浮层，Esc/滚动/换页/卸载消失）、`QModal.vue`（Teleport、三槽等宽标题栏、焦点陷阱/回焦、dismissible 控制 Esc/外部点击、接入仲裁）。
- `QConfirm.vue` 三档对称化（三槽标题、右上关闭图标按钮、按钮组居中同高配对同宽、窄窗纵排），保留原 props/事件/三档/Esc/焦点陷阱/焦点归还/危险档语义；layer 档接入仲裁。
- `QInput/QSelect/QCombo/QNumber` 补齐 busy/空状态/键盘（Home/End/Tab/Esc）并统一视觉；`QModal/QConfirm` 在更高优先级弹窗存在时 `v-show` 让位、恢复后保留 DOM 与状态。

### 2.2 反馈调度与审批（B）
- `stores/modalArbiter.ts`：同时只允许一个中央交互弹窗；优先级 close-ask 100 / onboarding 90 / impact 40 / recovery 35 / approval 30 / credential 20 / confirm 10 / editor 5；`suspendAll/resumeAll` 供关闭询问让位；通知在弹窗期间排队，已显示通知撤下并保留剩余时长。
- `stores/notifications.ts`：同一时间一条；任务失败 500ms 合并为「N 项任务失败了」；事件身份去重（任务身份+失败事件身份，重试后新失败可再次提醒）；约 5s 从真正显示计时；悬停/键盘暂停恢复剩余；不抢焦点；关闭只关本次；显示前过滤已恢复/已处理/过期；`onActivate` 供一次性设置提醒接续引导。
- `stores/globalIssues.ts` + `components/feedback/GlobalIssueEntry.vue`/`GlobalIssuePanel.vue`：一个入口显示独立问题数量，按根因去重，逐项原因/动作/诊断折叠，部分恢复只移除该项；接入 SSE 断线、凭据不可用/暂停/失效、模型能力受限、启动/重连恢复失败、更新安装失败且后端未恢复；对话页与设置/调试页都保留这一个入口。
- `stores/feedbackGate.ts`：输入/输入法组合、刚离开输入框（180ms）、其他中央弹窗、记忆星球覆盖层（`.planet-view.ball/tail` 不算覆盖层）、设置/调试页、窗口后台时不自动弹；星球关闭后恢复重试。
- `stores/unifiedApprovals.ts`：自动弹出接入统一门控；用户收起后已存在项不再自动弹、不连弹下一条，真正新到达仍弹；处理完一项回队列列表；到期禁止批准；恢复路径使用独立 `restored` 标记，不把「编辑中到达」误当用户稍后处理。
- 删除主对话 `MemoryQuestionReminder` 生产挂载及其文件/单测（`stores/memoryQuestions` 数据与星球本地处理保留）；知识候选卡主对话挂载移除（文件、数据与审批能力保留）。
- `GlobalFeedbackHost` 由 `App.vue` 挂一次；反馈浮层使用 `feedbackOffsets.ts` 按顶部提示条**实测高度**下移，不写死坐标。

## 3. 场景迁移清单（原入口 → 新入口/组件 → 保留或改变的交互 → 证据）

| # | 区域/原先提示 | 新位置与组件 | 保留 / 改变 | 证据 |
| --- | --- | --- | --- | --- |
| 1 | 对话/互动页顶部零散审批入口 | 输入区上方既有统一审批入口 + `UnifiedApprovalModal` | 保持唯一入口；不新增顶部通用审批入口 | `approvalUiSingleEntry.test.ts`、真实应用截图 |
| 2 | 审批到达即抢焦点 | `feedbackGate` + `unifiedApprovals.maybeAutoOpen` | 输入/输入法组合不弹；失焦 180ms 后弹；设置/星球不弹；收起不连弹 | `uf-approval-gate.test.ts` 9 例；真实应用 `scenes-live.mjs` PASS |
| 3 | 后台任务失败无统一提示 | `TopNotice` + `useTaskFailureNotices` | 顶上单条、500ms 合并、约 5s、暂停/恢复、点击定位、不重放历史失败 | 单元测试 + `_uf-shots/feedback` 截图 |
| 4 | 连接/模型/凭据散落页内提示 | `GlobalIssueEntry`/`GlobalIssuePanel` | 仅一个入口 + 数量；逐项原因与动作；部分恢复；设置/调试页也可见 | `uf-global-issues.test.ts`/`uf-global-issue-view.test.ts`；`extra-01/03` 截图 |
| 5 | 输入框工具条原生 title 用途提示 | `vQioTip` + `QIconButton` | 500ms 悬停、键盘聚焦立即、触控长按；无原生 title；可访问名称保留 | `QTip.test.ts`、`QIconButton.test.ts`；ui-catalog 截图 |
| 6 | 确认层各写一套标题/按钮 | `QConfirm` 三档 + `.qio-modal-*` 骨架 | 保留三档语义/焦点/Esc；标题居中、按钮居中同高同宽、窄窗纵排 | `QConfirm.test.ts`；ui-catalog 1200/480/320 截图 |
| 7 | 审批弹窗标题被关闭按钮挤偏 | `UnifiedApprovalModal` 三槽标题栏 | 标题中心偏差 0px；关闭按钮不挤正文；正文长事实保持左对齐 | 真实 DOM 测量 + `look-02` 截图 |
| 8 | 弹窗期间通知浮在最上层 | `notifications.suspendForModal/resumeFromModal` | 已显示通知撤下、保留剩余时长、弹窗结束后恢复；不堆叠 | 单元测试 + `scenes-feedback` PASS |
| 9 | 关闭询问与业务弹窗叠层 | `modalArbiter` + `WindowChrome` close-ask | 只一个交互层；业务弹窗 v-show 让位、取消后恢复上下文/草稿 | D 的 `uf-window-chrome-close.test.ts` |
| 10 | 主对话知识候选卡 / 记忆整理提醒 | 移除主对话挂载；统一审批入口与星球本地处理承接 | 数据、审批能力、星球三类本地事项保留；不再双入口/重复计数 | `UnifiedApprovalLayer.vue` 注释、证据文件 C/B |
| 11 | 星球单项归档中央确认 | 条目内 `QConfirm inline` | 取消不执行、不改业务数据；只归档条目本身 | D 定向测试 + ui-catalog |
| 12 | 整批撤回记忆整理的简易确认 | `MaintenanceRestoreDialog` 中央弹窗 | 摘要、受影响条目、可恢复/不可恢复原因、修改前后快照、恢复约束、确认/取消；部分不可恢复如实说明 | `uf-maintenance-restore-dialog.test.ts`；`showcase-01..04` 截图 |
| 13 | 设置页常驻 toast 与原生控件 | 分区原位反馈 + `QSelect`/自定义开关 | 成功短暂收回、失败持久；凭据删除后果确认与本地/厂商区分保留；权限多次确认未减少 | D 定向测试；`extra-03` 截图 |
| 14 | 更新提示与 100% 进度误导 | `UpdateCard` + `updater` 真实阶段 | 下载/校验/结束后端/安装分阶段；无总大小不画百分比；100%≠安装完成；入口会结束当前运行的说明保留 | `uf-updater-stages.test.ts`；设置截图 |
| 15 | 更新失败混在通知里 | 恢复时分处：已恢复留更新卡片；未恢复进全局异常入口 | 真实动作（完全退出后重启/重试），不假装重试能解决一切 | `extra-03` 截图 + 测试 |
| 16 | 启动等待无解释 | `BootStatus` | >10s 才显示「已等待 N 秒」；技术细节折叠；失败真实原因+重试 | `uf-boot-status.test.ts`；`showcase-05/06` |
| 17 | 初始设置未完成的常驻横幅 | `SetupIncompleteNotice` 一次性中性通知 + 设置页主动入口 | 只显示一次；排队未显示不算已提醒；点击接续原引导；不伪造完成 | `uf-setup-incomplete-notice.test.ts`；`fb-08` 截图 |
| 18 | 关闭确认原生 checkbox 与含糊文案 | `WindowChrome` 对称确认 + 自定义复选 | 三选项明文保留；只说明退出会结束当前运行；记住选择行为保留；失败在可见界面可重试/取消；不锁死 busy | `uf-window-chrome-close.test.ts`；真机未验证项见 §6 |
| 19 | 发送/提交失败只有页内红字 | 原位反馈 + 结果未知/已提交刷新失败分离 | 明确失败/结果未知/已提交未更新分开；保留原文与草稿；只读核对、不重复提交 | C 定向测试（`closure-c-*`、`final-a3-*` 等） |
| 20 | 文本卡原生 `prompt` 编辑链接 | 卡片内网址输入行 | 新增/修改/移除就地完成；非法 scheme 行内报错；Enter 确认、输入法组合不误确认、Esc 取消回焦 | `textCardEditor.test.ts` 20 例 |
| 21 | 影响确认可被 Esc/外部点击取消 | `ImpactConfirmLayer` + 仲裁 | 只有「确认改动/取消本次改动」作答；更高层可让位且状态保留 | `ImpactConfirmLayer.test.ts` 12 例 |
| 22 | 卡片/任务栏/预览用的字符图标与原生 title | `QIcon/QIconButton/vQioTip` | 任务栏 tabs 键盘（←/→/Home/End）、图标按钮可访问名称；预览失败/源文件失效原位处理 | C 定向测试；真实应用审计 `titleAttrsConv=0` |
| 23 | 卡片文字链接与关系连线易混 | 文字链接用卡片内网址行；关系连线保留对象附近编辑 | 两类功能不混；点击/选择/拖动/缩放/链接操作区分保留 | C 证据与交互测试 |
| 24 | 调试页错误与空列表混淆 | `DebugView` 分区错误 + 重试 | 加载失败≠没有记录；技术详情留在查询区 | `uf-debug-view-errors.test.ts` |
| 25 | 通知遮住非聊天页顶部提示条 | `feedbackOffsets.ts` | 按顶部提示条实测高度下移，不写死坐标 | `uf-feedback-offsets.test.ts`；`extra-03` 截图 |

## 4. 视觉约束完成情况

- **对称布局**：QModal/QConfirm 标题中心偏差实测 **0–0.0078px**，左右槽宽差 0–0.0156px；审批弹窗标题偏差 0px、左右内边距 55/55；顶部通知文字中心偏差 0px、左右槽各 28px；按钮组中心偏差 0、同高 36px、配对按钮等宽；320/480 窄窗纵排；长 URL/任务名自然换行或截断，不撑破组件。
- **14px 圆角**：本次涉及表面使用唯一 `--qio-ui-radius`；扫描确认审批弹窗、按钮、队列标签、确认层、输入框、下拉、徽标、通知、菜单、列表操作项均为 14px；仅保留状态圆点/星球球体/媒体内容/图标内部几何等契约例外。
- **图标按钮**：统一 QIcon/QIconButton；aria-label 必填、无原生 title、悬停 500ms/键盘聚焦/触控长按显示统一用途说明；桌面 32×32、触控 44×44；含义不明确或后果重要的动作保留文字。
- **自定义控件**：无原生 `alert/confirm/prompt`；原生 `select`/checkbox 外观替换为 QSelect/自定义外观（保留真实 input 语义与键盘）；加载/错误/禁用/处理中/空状态齐全。
- **深浅外观/动效**：全部使用现有颜色与动效令牌；遮罩轻变暗+轻模糊；减少动态效果下仍有可见过渡、无位移/弹跳；动画不作为业务提交路径。
- **窄窗**：通知在 1024/480/320 宽均在视口内（320 宽为 296px 上限、最多两行）；弹窗在 1024/480/320 不横向溢出；必要纵向滚动使用 QIO 统一滚动样式。
- **Windows 缩放**：本轮未逐一截取 125%/150% 系统缩放（见 §6 未验证）；组件使用相对单位与 max-width，无固定坐标修补。

### 截图证据索引（不在仓库中，按脚本可再生产）
- ui-catalog：`frontend/e2e-shots/uf-a/`（23 张，深/浅色、1200/480/320；含图标、图标按钮、用途说明、QModal/QConfirm 长文案/忙碌/三选项、表单状态、徽标）
- 真实应用：`D:\\qio-dev\\_uf-shots\\feedback\\`（单项/合并失败通知、窄窗、弹窗期间通知撤下/恢复、设置页延后审批、星球覆盖层延后审批、一次性设置提醒、全局异常入口/面板）
- 真实应用补充：`D:\\qio-dev\\_uf-shots\\extra\\`（全局多异常、审批队列列表、更新失败未恢复、长任务名 320）
- 真实应用弹窗：`D:\\qio-dev\\_uf-shots\\look\\`（审批弹窗 1280/1024/480/320、深色对话页、设置、互动）
- 场景展示页：`D:\\qio-dev\\_uf-shots\\showcase\\`（整批恢复弹窗 1280/1024/480/320/浅色、启动慢/失败）
- 复跑：`python scripts/e2e_up.py`（后端 8734 + Vite 5199，隔离 QIO_DATA_DIR）后运行 `scripts/ui-feedback-20261026/scenes-live.mjs`、`scenes-feedback.mjs`、`scenes-extra.mjs`、`look.mjs`；ui-catalog 用 `frontend/ui-catalog/uf-shots.mjs`；展示页用 `frontend/visual/shots-feedback.mjs`。

## 5. 验证

| 项 | 命令/动作 | 结果 |
| --- | --- | --- |
| 基线类型检查 | `npx vue-tsc --noEmit` @ e5c80253 | 通过（0 error） |
| 基线定向抽样 | approval/ui 测试 | 10 文件 79 用例通过 |
| 集成类型检查 | `npx vue-tsc --noEmit` @ 最终功能 SHA | 通过（0 error） |
| 全量前端轮（功能 SHA `bb3b7896`；后续仅测试/UI 小改，最终 CI 覆盖最终 HEAD） | `npx vitest run` | 477 文件：470 通过 / 7 失败；失败项中 6 个为旧断言/测试隔离问题，已修并定向通过；1 个 `i2v2-a-intent-flow`「页面卸载后不再继续轮询」在当前基线同样可复现（并行批跑时 7≠2，单跑通过），属基线既有 flaky，未改产品逻辑 |
| 修复后定向复跑 | appBoot/approvalUiSingleEntry/router-aliases/PlanetDock/OnboardingGating/eventBufferOverflow | 6 文件 22 用例全部通过 |
| 全量后端轮 | `cd backend; uv run --frozen pytest` | 结果见最终报告（后端未改） |
| 真实应用场景脚本 | `scenes-live / scenes-feedback / scenes-extra / look` | 全部检查 PASS（通知中心/圆角/暂停/撤下恢复、审批门控、设置页/星球延后、一次性提醒、全局多异常、更新失败、长名窄窗） |
| ui-catalog 几何 | `node ui-catalog/uf-shots.mjs` | 71 项检查、0 失败；23 张截图 |
| 场景展示页几何 | `node visual/shots-feedback.mjs` | 整批恢复标题中心 -0.01px、14px、320/480/1024 无横向溢出；启动状态检查全部通过 |
| 全量 CI | 本分支 push 触发 GitHub Actions；首个 push 的运行在最终视觉审计修复（互动锚点/展开按钮原生 title）后由新提交取代，未作为完成结果；**唯一一次完成的完整 CI 在最终 HEAD 上运行** | 结果与 run ID 见最终交付消息；CI 基础设施/排队失败与代码失败分开记录 |

## 6. 未验证 / 环境限制

1. **Tauri 桌面真机行为**（真实系统关闭、托盘、退出失败、真实更新下载/校验/安装、Windows 100/125/150% 缩放与拖拽视觉）未在本轮逐项真机复验；组件与状态机有单测/组件级证据，浏览器模拟不作为真机通过证据。
2. 一次性设置提醒与通知在**真实 Tauri 壳**中的最终视觉未单独截图（浏览器真实应用已验证）。
3. 后台任务失败通知的**真实后端任务数据**端到端未构造（环境无法快速生成失败任务）；通知调度、任务快照基线/去重/显示前核对有定向测试，视觉证据来自真实组件 + 真实应用注入 store 状态。
4. 整批恢复的真实后台维护批次数据未构造；视觉证据来自真实组件展示页，数据链路由 D 的定向测试覆盖。
5. 真实读屏软件未验证；有 aria/焦点相关自动化断言。
6. `i2v2-a-intent-flow` 的「卸载后停止轮询」在并行全量下为基线既有 flaky（基线同条件失败），单跑通过；未在本次范围内改其产品逻辑。

## 7. 用户会看到的变化（非技术语言）

- 通知与确认不再东一块西一块：真正需要你知道的失败会在窗口顶部居中短提示（多项合并成一句），其他地方保持安静；通知可以停留查看、鼠标移上去不会消失，点它可以去任务栏找到对应任务。
- 审批、恢复、影响确认、关闭询问同一时刻只会有一个主导窗口；你在打字或正在星球里时不会被打断，离开输入框后才会提醒；收起审批不会丢申请。
- 所有弹窗标题居中、按钮大小一致、在窄窗口自动竖排；圆角统一、图标按钮有清楚的悬停说明；深色/浅色都一致。
- 设置、凭据、调试、更新、启动、关闭都在发生的位置说明真实状态；更新不会再把“下载到 100%”说成装好了；关闭失败不会只写进已经看不见的窗口。
- 主对话不再重复显示知识候选卡和记忆整理提醒；这些事项在记忆星球里处理，审批入口仍能处理知识保存。

## 8. 明确未改变

- 初始引导：步骤、凭据必填与验证、称呼必填、跳过/关闭限制、进入主页门槛、资料保存时机、迁移逻辑均未改；普通更新不会重开引导；一次性提醒不影响原有向导触发状态。
- 权限与审批：权限模式切换与多次确认未减少；审批真实截止时间未延长或重置；批准/拒绝/执行/取消的语义未变。
- 源文件规则：仍为源文件引用，不新增副本策略；自动修复/定位机制保留。
- 恢复保护：后续修改保护、历史不删除、部分不可恢复如实说明保留。
- 更新与关闭机制：Windows 更新仍下载/校验/停止本实例/安装；关闭/托盘机制与“记住选择”行为保留。

## 9. 合并材料

- 分支：`fix/ui-feedback-unification-20261026`（基于基线 e5c80253，未合并 main）
- 提交：A/B/C/D 各分支合入本分支的 merge commits，加 Lead 的 App 总装、测试隔离修复、非对话页全局异常入口与整批恢复对称收口、文档提交。
- 冲突说明：共享测试文件（SettingsView/DesignGuards/会话测试）按新控件语义与测试隔离修正；C/D 在 ConversationView 与 UnifiedApprovalLayer 的接线以 B 的公共接口为准；无业务机制冲突未决。
- 审查入口：`docs/ui-feedback-unification-20261026.md`（本文）、`docs/status.md` 本轮记录、各 Agent 证据 `D:\\qio-dev\\_UF-EVIDENCE-{A,B,C,D}.md`（工作区证据，不随仓库提交）。
