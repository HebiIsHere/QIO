# 合并后修复与旧代码清理（P90）— 2026-10-26

> 本轮修复分支：fix/post-merge-integration-cleanup-20261025（未合入 main）。
> 基线：origin/main 1b03541eedd2dcaa9b82d6102108d717c71c6fb4。
> 状态：**已完成**（P90 阶段：实现、定向验证、真实界面、真机桌面、本地全量与完整 CI 均已记录，见 §9；本阶段未合入 main、未打包、未发布）。
> 随后另开的「后续修复阶段（P91）」为**进行中**：其最终 SHA / 完整 CI / 收口结论由 Lead 回填，见 §12。

## 0. 给用户的说明（非技术）

用户现在能从哪里操作新增能力：

1. **互动页的权限模式徽标**：打开互动页（/interactive）后，窗口最上方（品牌与设置之间）常驻一枚「权限模式」徽标；把底部对话收起或展开，它都在，不会压住卡片、工具栏和输入区。点它会进入设置页「工具与权限」，由用户自己切换模式。读取中会写「读取中…」，读不到会写「未知」并给原因，不会先假装成「受限」。
2. **审批结果与历史入口**：对话页和互动页输入区上方的 ⑤ 图标现在同时是「审批结果与历史」入口——没有待审批时也能点开，查看过去每一次审批的最终结果（已批准等待执行 / 执行中 / 执行成功 / 失败 / 失效 / 过期 / 中断待核对 / 拒绝 / 取消），刷新或重启后仍然能看。互动页把对话收起后，从右上角「任务」打开任务面板，里面有「审批结果」分区。
3. **非聊天页面**（设置、调试、星球）顶部保留一个统一审批入口，打开的是和聊天页同一个统一审批窗口，也能看结果与历史。旧的那套审批弹窗和入口已经不再加载。
4. **480 窄窗**：三条以上同类型待审批不再只显示一模一样的「权限」：标签会换行，并先显示对象身份（文件名/版本号等）再显示操作；鼠标悬停或读屏能拿到完整、互不相同的说明；键盘可以逐条切换，切换不会误提交、不会丢正在编辑的内容。
5. **批量审批**：互动页提交区点「打开批量审批」会真的打开任务面板的「待确认」分区；执行后在审批结果面板里逐项列出每个对象的成功/失败/跳过/未执行原因和整批汇总，不再只有一句整批「完成/失败」。
6. **读不到时不再装没事**：待审批恢复失败、历史读取失败时，⑤ 图标上会出现红色警示标，点开能看到具体原因和「重试」，不会显示成「没有待审批/没有历史」。
7. **材料影响确认与剩余撤回**：互动页的影响确认框承接了旧的「剩余撤回」能力：只处理这次明确列出的项目、关闭不撤回、失败可重试、处理完还有剩余时说明还剩多少、处理完能再次查看。

用户批准之后在哪里看最终结果：在 ⑤「审批结果与历史」（互动页收起态用「任务」→「审批结果」；非聊天页面用顶部入口）。「已批准/执行中」会明确写成等待执行，不会提前说已完成；执行结束、失败、失效、过期、中断待核对都会各自留下可回看的记录，刷新或重启不会消失。

本轮清理掉的旧代码（详见 §7）：17 个已不再挂载的旧互动/审批前端文件、它们对应的旧测试与 mock；旧浮层样式文件（新版仍在用的层级变量迁到新的公共样式层）；`batchOpen` 这个点了没有可见效果的死状态；没有生产入口的 QSlider / QioScrollArea；过时的能力迁移表；前端闲置的演示入口；以及文档里指向已删除文件的过期路径写法。

仍然存在的问题（不掩饰）：安装包/卸装形态、冻结 sidecar、系统关机注销、ask 的 ACK 兜底、用物理鼠标点托盘图标这几项没有在本机真机验证；真实模型链路、POSIX 平台没有本轮本地证据；旧 `/respond` 兼容路径的执行结果没有持久化历史（界面不会把它伪造进「结果与历史」）；个别历史文档里的长尾注释仍可能提到旧文件名（已标注删除）。

## 1. 基线与工作分支

| 项目 | 值 |
| --- | --- |
| 仓库 | HebiIsHere/QIO |
| 开工 fetch 的 origin/main | 1b03541eedd2dcaa9b82d6102108d717c71c6fb4 |
| 本分支 | fix/post-merge-integration-cleanup-20261025 |
| 本分支 HEAD（受测代码提交） | 0ebb0bf2a36400b62cb419e478db0035164eaf78（完整矩阵受测 SHA；此后仅追加 docs-only 回填提交，最终 HEAD 以「该提交所在分支 tip」为准） |
| 上一轮整合 | run 40196935297 / SHA 97b98368；定向复验 880880be；文档 docs/integration-3way-20261025.md |

## 2. 工作流、文件 owner 与分支

| 工作流 | 分支 | worktree | 主要交付 |
| --- | --- | --- | --- |
| Lead | fix/post-merge-integration-cleanup-20261025 | D:\qio-dev\fixq-lead | 冻结契约、能力对应表、集成、全量、CI、文档、推送 |
| A 审批与结果 | fix/pm-a-approval-results | D:\qio-dev\fixq-a | F02/F03/F04/F05（审批弹窗部分）/F06 |
| B 互动与几何 | fix/pm-b-interactive-mode | D:\qio-dev\fixq-b | F01/F05（互动部分）/C01 生产者/C02 + 旧互动能力迁移 |
| C 代码清理 | fix/pm-c-legacy-cleanup | D:\qio-dev\fixq-c | 引用图、17 旧文件、C03/C04/C05、旧测试清理 |
| D 桌面与工程记录 | fix/pm-d-desktop-ci-docs | D:\qio-dev\fixq-d | F07、CI 防重复、D01/D02 |
| E 独立验收 | verify/pm-e-acceptance | D:\qio-dev\fixq-e | 独立验收、反例、真实界面证据 |

共享文件唯一 owner（冻结）：

| 文件 | owner |
| --- | --- |
| frontend/src/App.vue | A |
| frontend/src/stores/approvals.ts | A |
| frontend/src/stores/unifiedApprovals.ts | A |
| frontend/src/components/approval/**、ApprovalFacts.vue、ApprovalModal.vue、ApprovalEntry.vue | A |
| frontend/src/components/conversation/UnifiedApprovalLayer.vue、ComposerEntries.vue | A |
| frontend/src/views/InteractiveView.vue | B |
| frontend/src/components/interactive/** | B |
| frontend/src/stores/interactive.ts | B |
| frontend/src/interactive/**（几何/布局） | B |
| frontend/src/stores/security.ts、PermissionModeBadge.vue | B |
| 17 个旧文件删除、styles/interactive-shell.css、styles/layers.css、main.ts | C |
| components/interactive/v2/intentDisplay.ts、components/ui/QSlider.vue、QioScrollArea.vue、services/interactive.ts 成员 | C |
| .github/workflows/**、docs/integration-3way-20261025.md、frontend/src-tauri/** | D |
| docs/status.md、docs/architecture.md、本文 | Lead |

## 3. 后端能力 → 前端入口对应表

### 3.1 用户操作型（必须有可达入口）

| 后端能力 | 所属页面 | 实际入口 | 接口/事件 | 权威状态 | 成功/失败反馈 | 刷新与重启 | 验证证据 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 查看当前权限模式 | 对话页 / 互动页 / 设置页 | 对话页聊天头 PermissionModeBadge；互动页全局栏徽标（F01）；设置页「工具与权限」显示同一份 | GET /api/security/mode | stores/security（loaded/error/mode） | 读取中显示「读取中…」；失败显示未知+原因；不冒充默认值 | App 启动 boot() 调 security.load()；刷新/重启后从后端恢复 | B 报告 + E 矩阵1 真实界面 |
| 用户切换权限模式 | 设置页 | 设置 →「工具与权限」按钮（唯一用户入口） | PUT /api/security/mode（user_confirmed） | 后端返回 mode，stores/security.mode | 失败保持原模式并显示原因；不假装已切换 | 切换后两页与刷新一致；重启后 GET 恢复 | B 报告 + E 矩阵1 |
| 审批单个决定（允许/拒绝/稍后/三次确认） | 对话页 / 互动页 / 设置/调试/星球等非聊天路由 | 两聊天页：输入区⑤统一审批入口 + 自动弹出；非聊天路由：顶部 NonChatApprovalEntry + 同一 UnifiedApprovalModal | POST /api/approvals/{id}/decide、/confirm；SSE APPROVAL_REQUIRED/APPROVAL_RESULT | 后端 state/revision/confirmed_stage/expires_at（唯一权威） | 失败保留待办并显示原因；stale 显示「已有结局」；三次确认按钮是「第 N/M 次」 | 刷新/重启后 recoverPending 从 /pending 恢复；确认阶段以后端为准 | A 报告 + 后端 test_approval_api/lifecycle + E matrix6 |
| 查看审批最终结果与历史 | 两聊天路由 + 非聊天路由 | ⑤「审批结果与历史」（0 待办仍可见）；互动页收起态：HUD「任务」→任务面板「审批结果」分区；非聊天顶部入口 | GET /api/approvals/history + SSE APPROVAL_RESULT（含 detail） | frontend/src/stores/approvalResults.ts 按 approval_id 建键 | 加载中 / 读取失败+重试 / 真的没有历史 三态分开；逐项 item_id 状态；已批准/执行中明确「等待执行」 | 刷新/重启后从 history 重建；事件重连补齐；终态持久可回看 | A 报告 + E surface/matrix + B 报告 |
| 批量审批与逐项结果 | 互动页 | 提交区「打开批量审批」→ 任务面板「待确认」分区（唯一状态 tasksOpen） | 批量批准/拒绝（/api/interactive/intents/batch 等）；执行结果 detail.results | tasksOpen / taskRows + approvalResults 的 items | 逐项对象/状态/成功失败/未执行原因 + 整批汇总；部分失败真实；未执行不标成功 | 刷新后任务面板与审批历史恢复 | B 报告 + C 测试迁移 + E 矩阵7 |
| 恢复待审批失败重试 | 两聊天路由 + 非聊天路由 | ⑤入口可见失败标记 + 审批结果面板「重试恢复待审批」 | GET /api/approvals/pending（recoverPending） | approvals.recoveryError / recoveryBusy | 入口警示 + 面板原因 + 明确重试；不显示成「没有待审批」 | 重试只重读快照：不续期、不重复创建、不推进阶段 | A 报告 + E matrix5 |
| 材料影响确认与剩余撤回 | 互动页 | v2/ImpactConfirmLayer：确认/取消、剩余撤回继续、再次查看、失败重试 | 材料影响检查/保存与意图撤回接口（既有真实接口） | store.pendingImpact / pendingReverts / dismissedReverts | 关闭不撤回；只处理明确 decisionIds；部分完成按服务端摘要；失败可重试 | 刷新后从后端恢复 | B 报告 + 真实界面证据 |
| 任务暂停/继续/取消 | 互动页 | HUD「任务」→ TaskCenterPanel | pause/resume/cancel intent 接口 | store.taskRows | 进行中/失败反馈；取消任务≠停止聊天 | 刷新后恢复 | B 报告 + E 矩阵 |
| 发送异常/排队/草稿恢复 | 对话页 / 互动页 | 输入区入口①（发送异常与草稿）/④（排队），新版 SendIssuesPanel / QueuePanel | /api/turns、队列与恢复收件箱接口 | session stores / recoveryRecords | 失败原文、取消请求≠已取消、草稿保真 | 刷新后恢复 | C 的测试迁移 + B/E 证据 |

### 3.2 结果展示型（不新增按钮但结果必须可见）

| 后端能力 | 所属页面 | 实际入口 | 接口/事件 | 权威状态 | 成功/失败反馈 | 刷新与重启 | 验证证据 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 审批执行结果（executed/failed/invalidated/expired/interrupted_unverified/rejected/cancelled） | 两聊天路由 + 非聊天 | 结果面板/历史（按 approval_id）；SSE 实时更新 | GET /api/approvals/history；SSE APPROVAL_RESULT | stores/approvalResults.ts | 终态文案逐项区分；执行中不提前报成功 | 刷新后 history 重建；终态持久 | A 报告 + E surface/matrix |
| 批量逐项结果（item_id 级） | 互动页/两聊天页结果面板 | 审批结果面板逐项列表 + 整批汇总 | detail.results / detail.items（执行器写入 approval_history.detail） | 结果模型 items（真实 item_id/对象身份） | 成功/失败/跳过/未执行 + 原因；不按文件名猜 | 刷新后从 history detail 重建 | A + E 批量场景 |
| 待审批恢复读取失败 | 两聊天路由 + 非聊天 | ⑤入口 alert 标记 + 面板 | GET /api/approvals/pending 失败 | approvals.recoveryError | 可见原因 + 重试；不清空已有待办 | 重试可恢复；不伪造空集合 | E matrix5 |
| 审批历史读取失败 | 两聊天路由 + 非聊天 | 结果面板 | GET /api/approvals/history 失败 | results.historyError | 失败+重试，不显示为空历史 | 重试成功恢复列表 | E history-failed 证据 |
| 权限模式读取失败/未知 | 对话页/互动页/设置页 | 徽标 | GET /api/security/mode 失败 | security.error | 显示「权限模式未知」+原因入口 | 重试/刷新恢复 | E matrix1 |
| 子智能体预算可编辑/只读 | 统一审批弹窗 | ApprovalFacts 预算区 | 旧 tool_create：/respond overrides；生命周期：/decide（不支持 overrides） | payload.subagent_budget + 审批 state | waiting 可编辑；approved/executing/终态只读，无「可修改后批准」；未修改不发 overrides | 刷新后以后端 payload 为准 | A + E e3 |

### 3.3 后台自动型（不新增按钮）

| 能力 | 生效方式 | 说明 | 代码/接口 | 验证证据 |
| --- | --- | --- | --- | --- |
| 审批单次执行与防重复 | 后端执行器（用户点批准后自动） | 不需要按钮：领取用 CAS，重复领取/并发执行拿不到行；结果只收口一次 | `backend/src/agent/tools/approval.py`、`storage/approvals.py` |
| 三次确认阶段判定 | 后端 CAS（用户每次点确认后自动） | 不需要按钮：前端只发 stage=confirmed_stage+1，越级/重复/变化返回 409 | `storage/approvals.py::confirm`、`tests/test_approval_api.py` |
| 启动恢复与事件重连 | 应用启动/断线重连自动 | 不需要按钮：App boot 调 recoverPending + 事件流 APPROVAL_REQUIRED/RESULT 重放；失败在 ⑤ 入口与面板可见 | A 报告 + E matrix5 |
| 审批终态写入可读历史 | 后端（decision/finish/expire 时自动） | 不需要按钮：approval_history 每次审批一条最新结果；rejected/cancelled/expired 本轮起也可回看（不改 schema/接口形状） | `backend/src/agent/storage/approvals.py` + `tests/test_approval_api.py` |
| 数据库迁移 | 后端启动自动 | 本轮未改 schema、未改历史迁移；权限/审批数据升级行为不变 | `docs/status.md` P90 边界一节 |
| 身份核验/对象状态判定 | 后端执行前重核自动 | 本轮未改授权语义；失效时用户可在结果里看到 invalidated 原因 | 既有 R9 测试 + A 结果映射测试 |

## 4. F01–F07 逐项状态

### F01 — 互动页当前权限模式（completed）
- 实现：互动页通过 InteractiveConversation 的 #header-extra 常驻 PermissionModeBadge（收起/展开都在），复用 stores/security 唯一状态源，不新增浮层、不建立第二份模式状态；读取中/失败/未知如实显示，点击进入既有设置位置。
- 修改位置：`frontend/src/views/InteractiveView.vue`（挂载）、`frontend/src/components/security/PermissionModeBadge.vue`（复用）。
- 验证：B 报告 + b-f01 测试 + 1440/480 真实界面；E 矩阵1/2（对话页模式切换+刷新、互动页常驻）。
- 边界：480x600 收起态徽标位于既有悬浮对话状态条内，该行可能与板面一张卡下边缘重叠（既有覆盖区，非新增浮层）；徽标与工具栏/输入区/消息面板相交 0。

### F02 — 审批最终结果与历史进入新统一界面（completed）
- 新增 `frontend/src/stores/approvalResults.ts`：按 approval_id 建键，SSE APPROVAL_RESULT 与 GET /api/approvals/history 双来源；approved/executing 显示「等待执行」，终态（executed/failed/invalidated/expired/interrupted_unverified/rejected/cancelled）持久可回看；history 读取失败与「没有历史」分开。
- 统一弹窗新增「待审批 / 结果与历史」两视图（0 待办可开）；入口：两个聊天页 ⑤（0 待办但有历史/失败时仍可见，aria 含「审批结果与历史」）、互动页收起态经 HUD「任务」→ 任务面板「审批结果」分区、非聊天路由 NonChatApprovalEntry + 同一个 UnifiedApprovalModal。
- 后端最小改动：rejected/cancelled/expired 终态写入既有 approval_history（detail 带 kind/label；不改 schema、不改接口形状、不改授权/执行语义；已执行终态不被迟到写入覆盖）。
- 旧 ApprovalModal / ApprovalEntry / components/approval/UnifiedApprovalEntry 删除；旧「上次没执行」提示迁到 NonChatApprovalEntry。
- 验证：A 报告 + E e1（20/20 前端验收在候选通过）+ 真实界面。

### F03 — 批量逐项结果（completed）
- 后端原本已把逐项 `results`（item_id/status/reason）写入 ExecResult.detail → approval_history.detail，并在 executed 事件 extra.detail 发出；本轮核实后在前端建逐项模型（只认后端 item_id/对象身份，缺失显示「未提供对象身份」而不猜），并给 history/事件补 kind/label。
- 结果面板逐项列出成功/失败/跳过/未执行 + 原因 + 整批汇总（全部成功/部分/全部未完成）；未执行不标成功；480px 无横向溢出。
- 未新增逐项批准/逐项重试政策；后端不支持的恢复/重试如实说明。
- 验证：A 单测 + E 矩阵7 真实界面 + B 任务面板。

### F04 — 恢复失败可见可重试（completed）
- approvals store 增加 recoveryBusy/retryRecovery；⑤ 入口在恢复失败时显示可见红色警示标（data-test=approval-entry-error，无错误时不渲染），非聊天入口显示失败文字；面板分开显示「恢复待审批失败」与「历史读取失败」，各自原因+重试。
- 重试只重读快照，不续期、不重复创建、不推进确认阶段；失败不清空已有待办、不显示成「没有待审批」。
- 验证：A 单测 + E matrix5（Playwright 拦包 500）+ 真实界面截图。

### F05 — 窄窗审批队列与重复信息（completed）
- 480x600 队列标签换行；标签先显示对象身份（路径末段/版本号/项数/工具名，兜底审批 id 尾段）+ 操作摘要，title/aria 为完整未截断且互不相同的说明；键盘可达、切换不误提交、草稿保留（unifiedApprovals 草稿在切项时不丢）；状态/期限只在一处显示。
- 验证：A 真实界面 480 三条同前缀记录（可见文本与 title Set 去重=3、无横向溢出、切换 2/3）+ E 480 脚本。

### F06 — 子智能体预算（completed）
- 区分旧 tool_create 子智能体定义创建审批（走 /respond overrides，可编辑、只有用户真的改过才发 overrides）与 R9 生命周期审批（/decide 不支持 overrides → 只读显示，无编辑控件、无「可修改后批准」）。
- 后端新增测试证明旧路径用户批准的预算真的进入执行器（tools/lifecycle.py 读取 overrides 的位置）；lifecycle+overrides 在 store 层明确拒绝而不是假装接受。
- 验证：A 单测（前端只读/可编辑、未改不发 overrides）+ 后端 test_approval_budget_override + E e3。

### F07 — 真实桌面验证（completed，边界见 §10）
- D 在 fixq-d 构建 debug Tauri 壳（内嵌本轮候选 frontend dist + 真实 Python 后端，不用 stand-in sidecar、不用真实密钥），真机跑 7 场景 55/55：X/ask 三选、关闭到托盘、托盘恢复、托盘菜单退出、记住选择与重启后仍生效、系统 WM_CLOSE（tray/quit 覆盖）、挂起后端模拟阻塞退出（12s HTTP + 15s 等待 → taskkill /PID /T /F，28.1s 内 exit 0，3 个 pid 核验消失）、重复关闭不叠加弹窗不二次退出、退出后后端进程树清空。
- WindowChrome 观感与 R9 壳侧 CloseRequested/ACK/关闭设置协议同时保留；组件级 WindowChrome 测试 19 项另跑。
- 候选壳在最终候选树上重建后的结果见 §9.4（增量重建，同一套脚本）。
- 未验证：安装/卸载形态、冻结 sidecar onefile 进程模型、系统关机/注销、ask 的 1.8s ACK 兜底、物理鼠标点托盘图标、多显示器/DPI。

## 5. C01–C05 逐项状态

### C01 — 旧对话几何桥（completed）
- 新版 `components/interactive/v2/InteractiveConversation.vue` 成为 chatObstacleFrame 的唯一生产者：量消息面板+输入区的实际可见外接矩形（屏幕坐标），展开/收起/调高/窗口 resize/根元素 ResizeObserver 触发；收起/卸载发布 null；不读 localStorage、不编造矩形。
- BoardCanvas 消费不变；真实界面 1440 rect {290,459,860,420} 与可见内容一致、可用区域底边 776.36→459；480 {15,289,450,294}；卡片位置不变。
- 旧 chatFrame.ts 的持久化/默认框/尺寸限制删除，只保留 ChatFrame 类型；chatFrameState.ts 的 publish/observe 保留。

### C02 — batchOpen 无显示消费者（completed）
- SubmitCluster 批量入口只调 store.openTaskCenter('pending')；batchOpen 字段/导出/写入/旧 watcher/布局测试与断言全部删除；tasksOpen 保留，批量对象选择/资格/批准拒绝/部分失败行为保留。

### C03 — 全局旧浮层样式（completed）
- interactive-shell.css 删除；新建 `frontend/src/styles/layers.css` 作为唯一公共来源（--im-z-dialog、工具栏测量变量、板面 chrome 选择约束按真实钩子 [data-im=interactive-stage]）；main.ts 改 import；两个读 CSS 的守卫测试改指向并含「旧钩子不得再出现」负向断言。
- 旧 chat-panel/batch-list/tasks-popover/--im-geo-* 等失去生产者规则删除；高特异度旧规则未覆盖新版布局（真实界面核对）。

### C04 — 过时能力迁移表（completed）
- intentDisplay.ts 的 MIGRATED_CAPABILITIES/MigratedCapability 删除（运行时无消费者且与真实能力不符）；p3a 的「必须存在 needs-backend」断言删除；真实能力（暂停/继续/取消已接后端、任务面板承接批次）写进证据与本文档。

### C05 — 第二批候选与细粒度清理（completed）
- QSlider / QioScrollArea 空置组件删除，ui-catalog（frontend+scripts）与 scroll.css 对应规则同步清理；services/interactive.ts 的零消费者包装（含 fetchWaiting）删除；interactive/intentWorkbench.ts 的 WaitingResponse、interactive/chat.ts 的旧面板文案函数删除；stores/interactive.ts 的闲置演示入口（advanceDemo/createDemoIntents/demoMode）删除，api.advanceIntent 保留（真实剩余撤回仍在用），后端 /demo/advance 与 pendingDecision 能力保留。

## 6. D01–D02 记录更正

- D01（docs/integration-3way-20261025.md §6.4）：改为「一次完整矩阵运行完成（run 40196935297，9/10 job 绿 + 单条基线失败）；失败后定向复验；另一次重复完整矩阵（run 40202066826）已启动并执行部分检查后取消」。保留两个运行号与取消事实；明确 main 指向的 1b03541e 没有完成的完整矩阵通过记录，不把 cancelled 当通过；列出 workflow_dispatch 元数据 head_sha（249a6344）≠ 实际受测 SHA（97b98368/6f56d815/880880be）的对照表；更正「backend 树完全一致」为「97b98368→880880be 只改 backend/tests 与文档，backend/src 与 frontend 产品树相同、整个 backend 子树不同」。
- D02（同文件 §8）：删除 force-with-lease 强推 main 回旧提交的路径，改为保留历史的回退提交/回退 PR，写清多 merge commit 的回退范围与目标树核对；数据库回退独立说明（迁移只进不退、代码 revert 不降库、旧备份不得静默覆盖新数据）；本轮未执行回退、未强推、未改 main。

## 7. 删除清单

原则：先核实生产引用图，再确认新版承接位置，最后删；删除后重跑全仓引用扫描（代码级只剩注释/负向守卫）。完整证据：D:\qio-dev\_fixpm\evidence\C\（REFFRAPH.md、DELETE-LIST.md、TEST-MIGRATION.md、remaining-refs*）。

### 7.1 17 个旧前端文件（基线合计约 9,266 行）

| 文件 | 原有效能力 | 新版承接位置 | 验证 |
| --- | --- | --- | --- |
| components/interactive/ChatDock.vue | 旧互动对话窗、输入、权限徽标、对话显示框发布 | v2/InteractiveConversation（对话/输入）、互动页徽标（F01）、chatFrameState 生产者（C01） | B F01/C01 测试 + 真实界面 rect |
| components/interactive/FailedSendNotice.vue | 发送失败提示 | conversation/SendIssuesPanel + ComposerEntries 入口① | SendIssuesPanel 测试 |
| components/interactive/IntentBatchTray.vue | 批量托盘、批次批准/拒绝/摘要、跨浮层几何施加 | TaskCenterPanel＋store.taskRows＋tasksOpen（C02） | i2v2-a-task-entries / irl-d-submit-status |
| components/interactive/IntentStatusPopover.vue | 任务浮层、预览贴靠几何 | TaskCenterPanel / v2 IntentLayer | 任务面板测试 + 真实界面 |
| components/interactive/IntentPreviewCard.vue | 意图预览卡 | v2 IntentLayer 详情/预览 | p3a/意图层测试 |
| components/interactive/IntentWorkbench.vue | 目标/预期/依据/范围/版本/依赖冲突/账本/单条与同批确认/等待事项/结果卡/恢复预览 | v2 IntentLayer 详情、单条/批量确认、ExecutionRecordPanel、恢复入口 | p3a + 执行记录测试 |
| components/interactive/IntentResultCards.vue | 意图结果卡 | v2 IntentLayer 结果/执行记录 | 执行记录测试 |
| components/interactive/ImpactConfirmDialog.vue | 影响确认、pendingDecision、剩余撤回、再次查看、失败重试、部分完成 | v2/ImpactConfirmLayer（B 95d21550 迁移全部能力） | ImpactConfirmLayer 9 例 + 真实后端撤回证据 |
| components/interactive/BoardSearchPanel.vue | 板面搜索/定位 | v2/InteractiveChromeTools 搜索 + `qio:interactive:locate-card` | chrome tools 测试 + C 真实界面 |
| components/interactive/ResizeHandles.vue | 卡片缩放热区 | BoardResizeLayer 自带四边四角热区（通用缩放原语 resize.ts/useResizeGesture 保留） | 既有缩放测试 |
| components/SettingsFloat.vue | 右上角浮动设置入口 | WindowChrome 全局栏「设置」按钮 | WindowChrome/p1a 测试 |
| components/QueueChip.vue | 主 turn 排队气泡、逐条取消、取消请求≠已取消 | conversation/QueuePanel + entries.queueRows | QueuePanel 测试（4 例） |
| components/InterruptedTurnEntry.vue | 上次退出未执行消息入口（继续/忽略/全部忽略/失败兜底） | SendIssuesPanel interrupted 区 + RecoveryInbox | SendIssuesPanel 7 例 + fu-f01f10 迁移 |
| components/TopicSwitchPrompt.vue | 话题切换建议浮条 | entries.topicSwitch + ComposerEntries 展开区 | ComposerEntries 测试 |
| components/approval/UnifiedApprovalEntry.vue | 顶部固定审批入口 | ComposerEntries ⑤（聊天页）＋ NonChatApprovalEntry（非聊天路由）＋ UnifiedApprovalModal | approvalUiSingleEntry/nonChatApprovalEntry 测试 |
| interactive/overlayLayout.ts | 旧浮层几何计划（chat/batch 开关、贴靠、切换条） | 无生产者：v2 布局与新版几何不依赖；能力由 layers.css 层级变量与新几何桥承接 | C 引用图 + 真实界面 |
| interactive/overlayAvoid.ts | 浮层互相让开带宽几何 | 无生产者：同上 | C 引用图 |

### 7.2 其它被删符号 / 状态 / 样式 / 组件

- 状态：`stores/interactive.ts` 的 `batchOpen`（字段/导出/写入/watcher/断言）；`stores/approvals.ts` 的兼容 `lastResult` 与 `history/historyLoading/historyError/loadHistory`（结果事实统一到 approvalResults）；`stores/interactive.ts` 的闲置演示入口 `advanceDemo/createDemoIntents/demoMode`（保留 `api.advanceIntent` 给真实撤回）。
- 常量/类型/函数：`v2/intentDisplay.ts` 的 `MIGRATED_CAPABILITIES`/`MigratedCapability`；`services/interactive.ts` 的零消费者包装（fetchWaiting 等）；`interactive/intentWorkbench.ts` 的 `WaitingResponse`；`interactive/chat.ts` 的旧面板/按键文案函数；`interactive/chatFrame.ts` 的持久化/默认框/尺寸限制（只留 ChatFrame 类型）。
- 组件：`components/ui/QSlider.vue`、`components/ui/QioScrollArea.vue`（无生产入口）。
- 样式：`styles/interactive-shell.css` 整个文件（旧 chat-panel/batch-list/tasks-popover/--im-geo-* 等）；`scroll.css` 的 `.qio-scroll-area` 规则；`main.ts` 改引 `styles/layers.css`；两个读 CSS 的守卫测试改指向并含负向断言。
- 测试：旧互动展示层对应测试 35 个、旧审批展示层测试 4 个、QSlider/QueueChip/SettingsFloat/InterruptedTurnEntry/TopicSwitchPrompt 专属测试、chatFrame 持久化测试、overlay 布局测试等；6 处 `stubs: { SettingsFloat: ... }` 死桩。行为断言迁移对照见 §8。
- 文档：`docs/status.md` 历史记录中指向已删除文件的路径写法改为纯文本并标注删除（保留历史事实）；`docs/frontend-design.md`、`docs/frontend-components.md` 的滚动视口指引改为 `v-qio-scrollbar/useQioScrollbars`。

### 7.3 必要保留的兼容代码与真实消费者

- 旧 `/respond` 审批接口与前端 `respondById/_respondItem`：工具创建等旧 request 机制仍在使用；本轮只把旧 UI 删除，不动协议。
- 后端 `/demo/advance` 与 pendingDecision：真实剩余撤回仍调用；只清前端闲置包装，不删后端分支。
- `services/interactive.ts` / `intentWorkbench.ts` 中仍被新执行记录/意图层使用的成员；后端 intents/intent_service/execution/runtime 等新链模块。
- `sdfRings` 与测试 support/harness；历史迁移与旧数据库升级；`backend/src/agent/storage/archive.py` 冷归档（未启用但非替代品）。

## 8. 旧行为断言迁移对照

完整对应表见 D:\qio-dev\_fixpm\evidence\C\TEST-MIGRATION.md（含每条旧用例的断言要点与新位置）。摘要：

| 旧测试 | 旧断言要点 | 新验收位置 |
| --- | --- | --- |
| ApprovalModal.test / ApprovalDefer / r9-approval-modal-v2 | 审批窗内事实/推迟/历史/预算/焦点 | A 的 components/approval/__tests__（UnifiedApprovalModal、approvalResultsView、UnifiedApprovalLifecycle、approvalUiSingleEntry）＋ stores/__tests__/approvalResults |
| approvalInlineGating | 内联卡与全局弹窗互斥、按 approval_id 门控 | A 重写为 approvalUiSingleEntry（旧展示层不得出现、同一时刻一套入口/弹窗） |
| SettingsFloat.test | 设置入口可访问/点击进设置/拖动不误触 | WindowChrome.test + p1a-shared-global-bar.test；拖动由壳侧协议测试与 D 的真实桌面验证承担 |
| QueueChip.test | 停止/取消排队文案、取消中禁用、失败不假装已取消 | conversation/__tests__/QueuePanel.test（负向断言保留） |
| InterruptedTurnEntry.test | 未执行消息原文/原因/继续/忽略/全部忽略/失败保留 | conversation/__tests__/SendIssuesPanel.test（负向断言：不自动重放、失败不清空） |
| TopicSwitchPrompt.test | 只给选择不自动切换、confirm/reject 事件、busy | ComposerEntries.test（新增「保留当前走拒绝接口」） |
| fu-f01f10-recovery-single-source（组件层） | 两入口共用状态、不复活、不虚假剩余 | 同文件重写为「收件箱 + SendIssuesPanel 共用、收件箱拉不到时面板兜底」 |
| impactDialog / n2 / n3 等 | pendingDecision、明确 decisionIds、关闭不撤回、失败重试、部分完成、再次查看 | v2 ImpactConfirmLayer.test（9 例）＋ i2v2-a-impact-confirm / acceptance-page-impact（8+3） |
| ChatDock 组件级（irl-a-chatDock/p3b/chatDockAttachments/a-unknown） | 附件行、绑定、Enter/IME、失败保留原文、草稿竞态 | v2/InteractiveConversation + Composer/ComposerDock 用例；d4ChatDraftRace 保留 14 例；旧浮窗布局断言不搬 |
| 旧批量托盘/切换条（batchTray、SwitchBar、d5/d5b/d6） | 批次列表、批准/拒绝、几何 | TaskCenterPanel + taskRows：irl-d-submit-status、i2v2-a-task-entries（新增批量对象选择/部分失败断言） |
| overlayLayout/overlayAvoid/overlaySwitch*/irl-c-overlay-chat-frame | 旧跨浮层几何计划 | 新几何：conversationGeometryBridge.test（B）、geometry/conversationLayout/toolbarLayout、照相机视角真实界面 rect 证据 |
| resizeHandles / irl-verify-handles | 旧把手组件与键盘 | BoardResizeLayer 真实渲染与键盘用例；resize.ts/useResizeGesture 纯函数用例保留 |
| BoardSearchPanel / SettingsFloat 等负向守卫 | 旧组件不得再被引用 | InteractiveOldEntriesGone、FloatingToolbar「不许再引用」等负向断言保留并更新 |
| intentDisplay MIGRATED_CAPABILITIES 断言 | 「必须存在 needs-backend」 | 删除该断言；真实能力由 TaskCenterPanel/pause-resume-cancel 与 i2v2 用例验证（C04） |

原则执行：删除的读源码字符串断言仅限「只断言已删文件自身样式/存在」的段落；能力型负向断言（不自动重放、失败不静默、不假装成功、不伪造数据、失败不清空）全部在迁移后的新位置存在；未使用放宽断言、skip/xfail 或恢复旧组件来让测试变绿。

## 9. 验证台账

### 9.1 定向验证（开发期，非全量）

开发期只做必要定向验证（各工作流自跑，非全量），命令/日志在各自证据目录：

| 工作流 | 范围 | 结果 |
| --- | --- | --- |
| A 审批 | 前端审批定向 vitest；后端 test_approval_api / test_approval_budget_override / test_approval_lifecycle / test_approved_operations | 前端 20 文件 168 例全绿；后端 46 passed；真实界面 8746/5219 隔离实例截图 8 张 |
| B 互动 | npx vue-tsc --noEmit；审批+互动/视图/几何定向；真实界面 1440/480 几何测量 | 类型检查绿；16 文件 111 例全绿；几何 rect 与真实可见内容逐像素一致、收起/卸载 null |
| C 清理 | npx vue-tsc --noEmit；stores+interactive+components/interactive+views 定向；真实界面三路由 | 类型检查绿；253 文件 2469 例全绿；旧元素不存在、console/page error 0、480 无横向溢出 |
| D 工程/桌面 | verify_workflow_contract.py/--selftest/--scenarios、YAML safe_load、真机 F07 七场景 | 113 断言 PASS、15 项 selftest 0 失败、11 场景 0 不符；F07 55/55 |
| E 独立验收 | 前端 DOM 级验收用例、后端验收用例、真实界面 surface/480/matrix 脚本 | 前端 20/20、后端 6/6、surface 7/7、480 6/6、matrix 9/9（候选 b498ba85，另有限边界见 §10） |
| Lead 集成 | 每次合并后 vue-tsc + 定向（审批/互动/视图/审批后端），合并后修复 docs 路径引用 | 合并点均绿；approval 79 例、integrated 1853 例、后端审批 60 例等 |

### 9.2 本地全量轮次（仅一次）

候选：最终代码/测试 HEAD 0ebb0bf2（后续仅可能追加 docs-only 回填提交；backend 树 `7df218fe` 在 58fe47ec→0ebb0bf2 未变，所以后端全量结果对最终候选有效；frontend 产品/测试树在最终候选上除 E 的类型修复与 docs 外未变）。

| 项目 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量 | `cd backend && uv run --frozen pytest -q`（592 个测试文件、收集 5929 项；在 backend 树 7df218fe 上跑，该树在 58fe47ec→0ebb0bf2 未变） | **exit 0**（无 failed/error；日志尾部为既有 ResourceWarning/PytestUnraisableException 与未 await 协程的警告噪音，不影响退出码；日志 `_fixpm/evidence/lead-full-backend-58fe47ec.log`） |
| 前端全量 | `cd frontend && npx vitest run` | 446 文件 / 3984 例：首跑 1 failed / 3983 passed。失败项为 `src/views/__tests__/i2v2-a-recovery-entry.test.ts` 第 3 例（`recoveryEntry(wrapper)[0]` 为 undefined）；失败项定向复验：同文件隔离连跑 5 次全过、同 harness 的 `src/views/__tests__` 组 21 文件 198 例全绿。归因：同机并发后端全量 + 桌面构建的全量首跑环境/时序抖动（该用例与改动路径无直接关系），非产品回归；同一提交的 CI frontend job（干净 runner）全套通过，未复现 |
| 类型检查 | `npx vue-tsc --noEmit` | exit 0（0 error） |
| 生产构建 | `npx vite build` | exit 0（9.18s，dist/ 未提交） |
| 文档一致性 | `python scripts/check_docs.py` / `--selftest` | 通过（99 个里程碑；selftest 6/6） |
| 第三方声明 | `python scripts/licenses/check_third_party.py` / `--selftest` | 通过（锁文件零变化、未改依赖） |
| 发布闸门自检 | `python scripts/release_gate.py --selftest` | 通过 |
| 离线评估 | `uv run --frozen python -m agent.eval.run` 在候选与基线 1b03541e 各跑一次 | 两次输出逐字节相同（topic/retrieval/anchor 指标一致） |
| CI 触发规则自检 | `python scripts/targeted-verify/verify_workflow_contract.py` / `--selftest` / `--scenarios` | 113 断言 PASS / 15 项 selftest 0 失败 / 11 场景 0 不符 |

### 9.3 完整 CI 轮次（仅一次）与定向复验

- **触发方式**：把修复分支推送到 `origin/fix/post-merge-integration-cleanup-20261025`（该分支此前不存在于远端，push 是唯一一次会跑完整矩阵的远端事件；未开 PR、未推其它分支、未动 main）。
- **运行**：**run 40219310525**，SHA `0ebb0bf2a36400b62cb419e478db0035164eaf78`，event=push，2026-10-25T18:02:07Z → 18:46:56Z（约 44m49s）。
- **逐 job 结论（10/10 全部 success，无 skipped / 无 failed / 无 cancelled）**：ci dedup preflight ✓ / docs consistency ✓ / frontend（typecheck + 测试 + 构建）✓ / rust (ubuntu) ✓ / rust (windows) ✓ / frozen worker (windows) ✓ / install e2e (windows) ✓ / backend (py3.11) ✓ / backend (py3.12) ✓ / backend (windows-latest) ✓。
- **失败与复验**：本轮完整矩阵无失败，因此**没有**使用 `Re-run failed jobs`、也没有针对 CI 失败做定向复验；未降低任何门禁、未跳过任何 job。
- **重复触发核对**：push 之后检查 `gh run list`，该 SHA 只有这一个 CI run（无 PR 事件、无第二完整矩阵）；`fix/pm-*`、`verify/pm-*` 分支从未 push。
- **后续文档回填**：本文件与 status.md 的 CI 运行号回填使用 `[skip ci]` 提交推送，不产生第二次完整矩阵；最终 HEAD 与受测 SHA 的差异仅为 docs（可用 `git diff --name-only 0ebb0bf2..HEAD` 核对）。
- **与上一轮台账的区分**：上一轮 main 的重复完整矩阵 run 40202066826（cancelled）已由 D01 更正记录；本轮完整 CI 是新的独立一轮，不重跑、不覆盖上一轮记录。

### 9.4 真实界面与桌面验证

- **真实界面（1440x900 / 480x600，隔离数据目录 + fake/mock provider + Playwright/CDP，截图与 JSON 在各证据目录）**：
  - A：0 待办从 ⑤ 打开「结果与历史」并读到批量逐项/拒绝/过期记录；480 三条同前缀申请可见文本与 title 互不相同、无横向溢出、切换 2/3；实时 executed/failed/invalidated 进入同一列表；恢复失败/历史失败可见+重试。
  - B：互动页徽标收起/展开可见且与卡片/工具栏/输入区/消息面板相交（1440）为 0；对话展开发布 rect `{290,459,860,420}` 与可见内容一致、板面可用区域底边 776.36→459、卡片位置不变，收起/卸载 chatObstacleFrame() 为 null；480 发布 `{15,289,450,294}`；任务面板「审批结果」分区（1440 454x139、480 430x139）可读真实结果；影响确认/剩余撤回界面（paused 拒绝原文、failed 重试逐项摘要、关闭不撤回、再次查看）。
  - C：互动/对话/设置三路由旧元素不存在、console/page error 0；layers.css 规则在真实页面生效（user-select=none、is-floating 透明表面、z-index=80）；480 工具栏窄档无横向溢出。
  - E：21 张截图覆盖 surface 7/7、480 审批 6/6、matrix 9/9（模式切换刷新、0 待办历史、恢复失败重试、三次确认 1/3、并发乱序不串、刷新不丢历史、批量逐项部分成功）；矩阵 11 由 B 的真实后端链路 8/8 补齐、矩阵 12/13 有限边界见 §10。
- **真机桌面（D）**：最终代码 HEAD 0ebb0bf2 上完整 `npm run build`（vue-tsc + vite）通过，用最终 frontend 树 `18a47556` 真实编译壳（内嵌 dist + 真实 Python 后端），7 场景 55/55：ask 三选、关闭到托盘、托盘恢复、托盘菜单退出、记住选择并重启后生效、系统 WM_CLOSE（tray/quit）、挂起后端阻塞退出（12s HTTP + 15s 等待 → taskkill /PID /T /F，28s 内 exit 0、pid 全部消失）、重复关闭不叠加不二次退出；exe 42,083,840 bytes，sha256 `5D75CBD9FA70D1DC4FD4275C35EB898BC9AD04B5A299B0805B1AA27D0549568B`。D 阶段（main 前端树）与最终候选树两次构建均 55/55，WindowChrome 组件测试 19/19。边界见 §10。

## 10. 未验证与边界

- **真实模型/厂商链路**：本轮全部模型调用使用 fake/mock provider；离线评估用离线夹具。
- **POSIX 平台**：本机验证全部在 Windows；Linux 由 CI（py3.11/py3.12）覆盖，那是 CI 事实不是本地结论。
- **安装形态与桌面边界**（D 的 F07 报告 §5 如实列出）：NSIS 安装/卸载形态（由 CI install e2e 覆盖，本地未打包）、冻结 sidecar onefile 进程模型（由 CI frozen worker 覆盖，本地 debug 走真实 Python 后端）、系统关机/注销（WM_QUERYENDSESSION/ENDSESSION 无法无人值守触发，只验 WM_CLOSE + RunEvent::Exit）、ask 的 1.8s ACK 超时兜底（前端总能 ACK，未构造故障）、物理鼠标点任务栏托盘图标（用 tray-icon 原生回调消息 + 菜单键选，状态是真实 Win32 状态）、多显示器/DPI/最大化和更新安装。
- **480x600 收起态徽标**：徽标位于既有悬浮对话状态条内，该行可能与板面一张卡下边缘重叠（1440 与卡片相交 0；始终与工具栏/输入区/消息面板相交 0）。这是既有悬浮布局的覆盖区，不是本轮新增浮层。
- **矩阵 11（材料冲突→影响确认→剩余撤回）**：组件级 9 例全绿；B 在候选 689ffa97 上用真实后端（真实 GET /intents 读取的 revert 数据 + 真实 POST /demo/advance decisionIds 路径）跑通 8/8：影响确认命中运行中任务并「等待确认，尚未保存」、取消不落库、剩余撤回读取待决定项、失败重试显示服务端原文、部分完成摘要（processed/reconfirmed/remaining）、关闭不撤回（DB 前后一致）、再次查看、Escape 不撤回。E 的隔离库不能自然产生 pendingDecision，按其记录为 LIMITED，不伪造数据；判定依据是 B 的真实后端链路证据。
- **旧 /respond 兼容路径**：执行结果没有持久化历史，界面不会把它伪造进「结果与历史」；工具创建等旧 request 机制仍可用。
- **历史入口**：无待审批、无历史、无错误时 ⑤ 不占位（空入口不占屏幕）；有待办、有历史或读取失败时可达。
- **既有边界不变**：真实回收站/符号链接/跨卷移动等文件系统边界沿用仓库既有结论；执行前核对到系统调用仍有竞态；受限子进程不是安全沙箱。
- 本轮未改数据库 schema、未改历史迁移、未改版本号/锁文件；无需数据库回退步骤。

## 11. 合并准备

- **分支状态**：修复分支已推送 origin（独立分支，未合入 main、未打包、未发布、未改版本号）。main 保持 1b03541e 不动。
- **本轮唯一一次完整 CI**：由集成分支 push 触发 ci.yml 完整矩阵（含 install e2e、冻结 worker、三平台后端、前端 typecheck+测试+构建、rust 两平台、docs）。若失败：修复提交带 [skip ci] 推送（不产生第二次完整矩阵），再用 targeted-verify.yml 指定 ref + 完整 40 位 SHA + 平台 + pytest/vitest 筛选定向复验；最终 HEAD 若含仅文档回填提交同样 [skip ci]，并记录「受测 SHA 与最终 HEAD 的差异仅为 docs」。
- **CI 防重复规则**：ci.yml push 排除 fix/pm-* 与 verify/pm-*（并行子工作分支永不 push，仅防误推）；concurrency 以实际受测提交为键（PR head SHA 回退 github.sha）；新增 workflow_dispatch + expected_sha（checkout 后校验 HEAD==输入 SHA），但它需默认分支已有该触发器，本轮不依赖。main push 与 PR 门禁与 job 图均未改动。
- **未来合入 main 的重复触发处理**：PR 运行的受测提交是 PR head SHA，main push 运行的受测提交是 merge SHA，两者是不同的提交、各自一次完整矩阵；concurrency 不会把它们当作重复。若仓库保护要求两者都跑，按「不同 SHA 两次完整运行」如实记录，不冒充去重；减负方案（择一）：用 fast-forward/rebase 合并使 main SHA 与已验证 head 相同，或 PR 上只跑 targeted、main push 作为唯一完整门禁。本轮没有创建 PR。
- **合并材料**：本文档（能力对应表、逐项状态、删除清单、测试迁移、验证台账、未验证边界）+ 各工作流证据目录 + 分支提交历史（全程 merge --no-ff，保留工作流边界）。
- **数据库**：本轮无 schema/迁移变更；若未来同分支需要回退，按 docs/integration-3way-20261025.md §8 的「保留历史的回退提交/PR + 数据库独立处理」执行，不用强推。

## 12. 后续修复阶段（P91，2026-10-26 后续轮）

> **阶段区分**：§1–§11 是 **P90「合并后修复与旧代码清理」的已完成事实**（该阶段未合入 main、未打包、未发布）；本节记录随后另开的 **P91「后续修复轮」**，它**已完成**（实现/定向/真实界面/本地全量/完整 CI/失败项定向复验/独立验收均已记录；未合入 main、未打包、未发布、未改版本号）。收口事实见下。

- **基线**：`origin/fix/post-merge-integration-cleanup-20261025` = `373fda40`（P90 分支 tip）；`origin/main` 仍为 `1b03541e`（未前进）。
- **分支**：Lead 集成分支 `fix/post-merge-followups-20261026`；各工作流独立 worktree 分支：A 话题归属 `fix/fu2-topic-attribution`、B 审批后端 `fix/fu2-approval-backend`、C 审批前端 `fix/fu2-approval-frontend`、D 通知投递 `fix/fu2-notification-delivery`、E 文档许可 `docs/fu2-docs-license`、F 独立验收 `verify/fu2-acceptance`。
- **范围（按共享契约，完成情况以收口验证为准）**：
  - **A 互动续作话题归属**：确认 / 恢复 / 重试启动续作 Turn 时，话题优先取原提交保存的 `binding.resumeState.topicId`；当前选中话题不得覆盖它；binding 缺失或话题无法核对时明确失败（不静默回退、不启动 Turn、不写绑定、不重复执行）。
  - **B 审批后端**：旧 `request()` 落库行（state/operation 为空）在输出中明确 `legacy=true`，不得由 `row_to_dict` / `state_of` 伪造 `state=waiting`；恢复保留原 `turn_id/session_id/request_digest/created_at/expires_at` 绑定事实，旧审批状态用独立事实（如 `interrupted_legacy`）；`/api/approvals/{id}/decide` 对 legacy 明确拒绝，`/respond` 继续服务 legacy（单次消费与绑定校验不变）；旧路径执行结束写 `approval_history`（PK `approval_id`，后写真实终态，detail 含 ok/kind/label/error）并发 `APPROVAL_RESULT`（`phase=executed/failed`、`ok`、`summary`、`detail`），「已批准 / 执行中」不得显示成「执行成功」；支持 `overrides` 的旧 `tool_create` 路径必须让执行器采用用户批准值，不支持 `overrides` 的路径收到时显式 400 拒绝（`overrides_unsupported`），不静默丢弃。
  - **C 审批前端**：按 `approval_id` 合并 `pending` / `history` / 事件；legacy 或无 state 项只走 `/respond`，绝不调 `/decide`、不补 state；重连（SSE onopen）、RESYNC、回前台主动补读；读取失败可见可重试；已批准等待执行与执行成功/失败严格区分；预算只有端到端支持 `overrides` 的旧路径可编辑。
  - **D 子任务通知可靠投递**：新增**只追加**迁移 71（不改 1..70；`task_id` 主键、`payload`、`created_at`、`delivered_at`、`attempts`、`last_error`、`origin`）；enqueue 只做持久化去重、不算交付成功；只有通知轮被 turn manager 接受后才写 `delivered_at`；失败保留待重试、重启继续投递；同一 `task_id` 至多一次、FIFO。
  - **E 文档与许可**：本文件区分 P90 已完成与 P91 进行中；`docs/status.md` 增加 P91 与最后核对；`THIRD-PARTY-NOTICES.md` / `docs/compliance.md` 按实际组件核对概括（本轮未改依赖，不重新生成声明）。
- **状态**：**已完成（completed）**。五项修复与清理均实现并验证；未合入 main、未打包、未发布、未改版本号。
- **收口事实（Lead 回填）**：
  - 受测代码 SHA：本地全量与独立验收 `8cb18828`（产品代码）；完整 CI `48b01f0c`（同一产品代码 + docs）；失败修复仅测试提交 `0c47e11b`（[skip ci] 推送）。最终 HEAD 为本文件所在提交（docs-only 回填），可用 `git diff --name-only 0c47e11b..HEAD` 核对。
  - 完整 CI：run **40257319813**，SHA `48b01f0c`，event=push；10 job：9 success + `backend (py3.12)` 1 failure（其余 install e2e / frozen worker / rust×2 / frontend / docs / backend py3.11 / backend windows 全部 success，无 skipped/cancelled）。失败原因是测试 helper 在主线程直连读取执行器正在写的同一 sqlite 连接（py3.12 抛 `sqlite3.InterfaceError`），与产品代码无关（py3.11 与 Windows 同代码全绿；本地 py3.11 重复通过）。
  - 定向复验：`targeted-verify` py3.12 run **40263546217** success、py3.11 run **40263563419** success（均指定 ref + 完整 SHA `0c47e11b` + 两个测试文件，workflow 内 `assert checked-out commit equals requested SHA` 通过；未重跑第二次完整矩阵）。
  - 本地全量：后端 601 文件 / 6013 项 **exit 0**；前端 452 文件 / 4025 项 **全通过（exit 0）**；`vue-tsc --noEmit` 0 error；`vite build` 成功；docs/license/release gate/workflow 契约检查通过；离线评估与基线逐字节相同。
  - 独立验收（F）：五项反例全绿、保真项 6/6、回归 101+81 passed、真实界面 480x600 4/4；LIMITED：通知 AppContext 端到端未构造、真实网络故障注入未做、跨崩溃 at-least-once 窗口保留。
  - 最终结论：未合入 main / 未打包发布 / 未改版本号；合并材料见 `docs/post-merge-followups-20261026.md`。
  - 收口后追加修复（定向验证、未触发全量/CI）：普通互动聊天续作 `resolve_chat_binding` 同规则修复（原提交 topicId 优先；原提交存在但话题无法核对时明确 400 拒绝，不回退当前话题、不静默改绑）；定向测试 5 例（基线 2 红→绿），改动以 `[skip ci]` 推送。
- **未验证边界（本轮默认口径）**：真实模型/厂商链路（一律 fake/mock）、POSIX 平台（本地仅 Windows）、安装包形态与系统关机/注销、真机桌面壳（除非收口时另有新证据）——最终以收口证据为准。

