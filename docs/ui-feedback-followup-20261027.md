# 提示系统功能缺陷与 CI 测试数据修复（第二轮）· 交付说明

日期：2026-10-27（Asia/Shanghai）
基线：`6dc57885203c7179cf53b142c71fe332fc8c0821`（上一轮最终功能 SHA）
分支：`fix/ui-feedback-followup-20261027`
说明：本文件只描述本轮修复范围与验证口径；逐项命令、真实输出、浏览器截图与定向 CI 运行记录见交付台账（仓库外 `_UF2-VERIFY.md` / `_UF2-DELIVERY.md` 与各 Agent 证据文件）。

## 1. 范围与结论

本轮按用户任务书修复 A01—A09 对应的 F01—F09，以及已定位的 CI 主失败 F10；Windows `Event loop is closed` 作为独立待定位项取证。**A10（视觉 P2：整页全部视觉要求覆盖）按用户要求暂缓，本轮未修**，本文不把任何视觉项计为通过。

| 编号 | 结论 | 摘要 |
| --- | --- | --- |
| F01 / A01 | 已修复 | 中央弹窗统一活动判断（整体挂起 / 被更高优先级取代 / 注册被拒）；隐藏层保留 DOM/草稿/阅读位置但不可交互、不抢 Esc/Tab；高优先级关闭后恢复可见与焦点。 |
| F02 / A02 | 已修复 | 任务订阅、轮询、首份快照改为应用级引用计数生命周期；页面切换不再停掉全局数据；点击通知先保证任务列表真实可见（含窄窗抽屉）再定位整组任务。 |
| F03 / A03 | 已修复 | 通知成员各自保留事件身份、任务身份与文案来源；过滤/合并/恢复后从剩余有效成员重新生成文案、数量、完整读屏文案与点击目标。 |
| F04 / A04 | 已修复 | 挂起恢复前与新显示共用同一有效性核对；已解决的旧通知不再恢复。 |
| F05 / A05 | 已修复 | 中央弹窗结束放行时重新去重/过滤/合并等待期间的失败；只出现一条「N 项任务失败了」；计时按规则（新成员给一次完整展示机会）。 |
| F06 / A06 | 已修复 | hover 与 focus 分别记录暂停原因；全部解除才恢复计时；内部按钮间移动焦点不误解除。 |
| F07 / A07 | 已修复 | 入队与显示/恢复前统一按 `isAttentionItem()` 判断：`failed + requiresAttention=false` 不提醒。 |
| F08 / A08 | 已修复 | 审批到期改由到期时钟推进（自然过期、切换/让位/后台恢复/卸载清理），到期移除批准入口；期限与后端校验不变。 |
| F09 / A09 | 已修复 | 审批列表返回恢复真实 DOM `scrollTop`、可视锚点、展开与焦点；锚点被移除时恢复相邻项；不自动进入下一条详情。 |
| F10 | 已修复（测试数据） | 旧成功用例补齐原提交的真实 `resume_state.topicId` 绑定；未放宽产品归属保护，FU3 负向用例保持通过。 |
| Windows Event loop | 未定位 / 未宣称解决 | 已有日志证明是 asyncio 子进程 transport 在事件循环关闭后析构的 `PytestUnraisableExceptionWarning`（Windows Proactor），非本轮测试失败项；缺完整归因信息，未无依据改生命周期。 |
| A10 | **按用户要求暂缓，本轮未修** | 不做圆角/居中/原生控件/用途提示/图标扫尾。 |

## 2. 关键改动

- `frontend/src/stores/modalArbiter.ts`：新增 `activeId` / `isActive(id)` / `isVisible(id)`（已打开 && 无整体挂起 && 栈顶活动项），保留旧 getter 兼容；审批、影响确认、凭据、恢复、引导、QModal/QConfirm 等生产中央弹窗统一接入。
- `frontend/src/components/approval/UnifiedApprovalModal.vue`、`unifiedApprovals.ts`：让位、到期时钟、列表阅读位置恢复。
- `frontend/src/components/feedback/GlobalFeedbackHost.vue`：应用级持有任务数据（`tasks.acquireGlobal()/releaseGlobal()`）。
- `frontend/src/stores/conversationTasks.ts`：引用计数生命周期、`focusTasks(ids)` 集合定位、`globalRefCount`。
- `frontend/src/views/ConversationView.vue`：不再启动/停止全局任务订阅与轮询；注册通知导航的页面可见性宿主。
- `frontend/src/services/noticeNavigation.ts`（新）：通知点击 → 路由/覆盖层/左栏/窄窗抽屉真实可见 → 整组任务定位；有中央弹窗时 blocked 不导航。
- `frontend/src/stores/notifications.ts`、`useTaskFailureNotices.ts`、`TopNotice.vue`：成员文案/身份、恢复前过滤、弹窗结束重新合并、分别暂停、`requiresAttention` 过滤、点击落点与失败说明。
- `frontend/src/components/interactive/v2/ImpactConfirmLayer.vue`、`QModal.vue`、`QConfirm.vue`、`MaintenanceRestoreDialog.vue`、`CredentialModal.vue`、`OnboardingWizard.vue`：统一活动判断与隐藏层交互收口（不改外观）。
- `backend/tests/test_p3_d_submission_resume.py`：仅正向用例补真实原话题绑定与断言。
- `backend/tests/test_r9g_acceptance_e2e.py`：测试装置同步调用 async 工厂导致「协程未 await」；改为同步函数，真正覆盖 `ReviewUnavailable` 分支。

产品业务规则（审批真实期限、提交层与后端校验、引导/权限/源文件/恢复保护/更新/关闭机制、互动续作原话题归属）未改变。

## 3. 验证口径（摘要）

- 前端：`npx vue-tsc --noEmit` 通过；定向 Vitest（本轮新增/修改与直接受影响文件，50 个文件 371 条用例）通过。未跑全量前端测试（按本轮用户额度）。
- 后端：F10 原失败用例 + `tests/test_fu3_chat_topic_binding.py` → 6 passed；`test_p3_d_submission_resume.py` 整文件 9 passed；`test_r9g_acceptance_e2e.py` 39 passed。未跑全量后端 pytest。
- 真实浏览器（隔离后端 + 假厂商，不联网/无真实 Key）：
  - F02 跨页通知链路 20/20：真实工具执行失败 → 任务快照 → 设置页通知 → 点击 → 对话页任务列表整组展开定位；页切换订阅/轮询计数稳定；重载不重放历史失败；窄窗抽屉落点。
  - F01/F08/F09 22/22：真实 DOM `display/inert/焦点`、阅读期间自然过期、28 条列表 `scrollTop=500` 恢复与相邻项恢复。
  - F04/F05/F06 8/8：弹窗期间 10 项分时失败结束后合并为一条、hover+focus 分别暂停、挂起恢复前过滤已解决成员。
- CI：本轮只使用仓库既有的 `targeted-verify.yml` 做定向复验；不触发完整矩阵、不修改门禁。运行记录（run ID / 完整受测 SHA / 筛选参数）见仓库外交付台账。

## 4. 未覆盖与边界

- A10 视觉 P2 未修（用户要求暂缓）。
- Windows `Event loop is closed` 未定位（缺 tracemalloc / JUnit artifact / 整套 GC 时点）；不宣称已解决或 CI 全绿。
- 真实应用输入法组合、触控长按、跨窗口焦点在浏览器环境下能力有限；相关行为以既有定向单测与代码路径为准，证据文件已如实标注。
- 本轮未执行完整 CI 矩阵；旧 run 40369680168 的 "Re-run failed jobs" 会跑旧 SHA，不能证明新提交，故未使用。
