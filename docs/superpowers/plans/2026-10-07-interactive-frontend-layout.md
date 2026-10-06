# 互动板前端改版实施方案（2026-10-07）

## 1. 实际基线

- 远端 `feat/interactive-foundation` 顶端 = **`66b0e22`**，与提示词给的参考提交一致（已用 `git ls-remote` 核对，**没有更新**）。
- 本轮分支：`feat/interactive-frontend-layout`，从 `66b0e22` 新建；总工作区 `D:\qio-dev\qio-fe`。
- 子智能体工作区（同一基线）：`qio-fe-a`（`wt/fe-a-layout`）、`qio-fe-b`（`wt/fe-b-board`）、`qio-fe-c`（`wt/fe-c-chat`）、`qio-fe-d`（`wt/fe-d-approval`）。
- **不碰**：`fix/unified-process-audit`、`feat/unified-process-attachments-streaming`、`main`；不改数据库结构、不改迁移编号、不开发附件系统与任务执行机制。
- 参考文件 `QIO_Interaction_Definition.md` 与 `qio-layout-prototype.html` **本机不存在**（工作区、仓库、临时目录、下载目录都查过）。按提示词里已写明的规则实施；原型里的模拟聊天、演示提交、计时完成等一律不复制。

## 2. 当前界面与目标的差异

| 目标 | 现状 | 本轮改法 |
| --- | --- | --- |
| 板面为主体，操作浮在板面上，无常驻右侧栏 | 右侧常驻「QIO 回复与任务」栏 + 板面下方常驻提交面板 | 去掉右侧栏；底部改横向悬浮工具栏；提交进工具栏右端；两侧浮层独立 |
| 顶部只留身份 / 导航 / 简洁状态 | 顶部有身份 + 保存状态 + 收起按钮 | 顶部只留身份、回到对话、保存与提交状态、任务与演示入口 |
| 添加菜单统一五类卡片 | 工具栏里五个平铺按钮 | 收进「添加」菜单；文字卡片不再单独常驻 |
| 右下独立聊天入口 | 无（聊天在对话页） | 悬浮按钮 + 透明底悬浮对话框，复用会话 store |
| 选中卡片出现局部工具栏 | 操作在卡片底部常驻一行按钮 | 改为选中后浮出的局部工具栏（含注释勾选框） |
| 平移 / 空格框选 / 滚轮缩放 | 空白拖动即框选；无缩放 | 空白拖动 = 平移；空格 + 拖动 = 框选；滚轮以指针为中心缩放 |
| 连接点拖线建链 | 关系模式里依次点两张卡 | 选中后显示连接点，从连接点拖到目标卡片 |
| 重叠成组有明确提示 | 已有自动成组，但无「松开后合并成组」提示 | 拖动覆盖到明确目标时提示，松手才成组 |
| 批量列表只在同批 ≥4 出现，默认收起 | 只要待审批 ≥4 就出现，且常驻在右侧栏 | 按批分组、右上入口默认收起、不抢用户的展开决定 |
| 影响确认在页面中央 | 顶部横幅 | 中央确认框（保留键盘焦点与取消语义） |
| 聊天与提交互相独立 | 无聊天 | 文字发送只发文字，不带板面；板面提交走原路径 |

## 3. 分工与文件边界（同一时间只有一个人改同一个文件）

| 角色 | 只写这些文件 |
| --- | --- |
| **主智能体** | `frontend/src/views/InteractiveView.vue`、`frontend/src/stores/interactive.ts`、`frontend/src/interactive/types.ts`、`frontend/src/services/interactive.ts`、本计划、`docs/interactive-mode-contract.md`、`docs/status.md`、集成与验收文档 |
| **A 布局与视觉** | `frontend/src/components/interactive/BoardToolbar.vue`、`AddMenu.vue`、`BoardSearchPanel.vue`、`BoardPreviewLayer.vue`（仅样式适配）、`frontend/src/styles/interactive-shell.css`（新建，层级与避让变量）、A 自己的截图脚本 |
| **B 板面交互** | `frontend/src/components/interactive/BoardCanvas.vue`、`BoardCard.vue`、`BoardGroupFrame.vue`、`BoardLinkLayer.vue`、`frontend/src/interactive/viewport.ts`（新建）、`frontend/src/interactive/board.ts`、`frontend/src/interactive/__tests__/{board,boardCanvas}.test.ts`、`__tests__/viewport.test.ts` |
| **C 聊天与状态** | `frontend/src/components/interactive/ChatDock.vue`（新建）、`SubmitCluster.vue`（新建）、`BoardChangeList.vue`、`frontend/src/interactive/chat.ts`（新建）、`__tests__/chat.test.ts`、`__tests__/submission.test.ts` |
| **D 审批与独立验收** | `frontend/src/components/interactive/IntentBatchTray.vue`（新建）、`IntentPreviewCard.vue`、`IntentStatusPopover.vue`（新建）、`ImpactConfirmDialog.vue`（新建）、`frontend/src/interactive/approval.ts`、`__tests__/approval.test.ts`、`docs/interactive-frontend-verify.md`（复核报告） |

被淘汰的组件：`ReplyPanel.vue`、`SubmitPanel.vue`、`IntentImpactNotice.vue`、`IntentBatchList.vue`（能力分别移入 C 的 `SubmitCluster` / `ChatDock`、D 的 `IntentBatchTray` / `ImpactConfirmDialog`）。删除动作放在集成阶段由主智能体做，避免子智能体互相删文件。

## 4. 会话复用方案（C）

- 直接用 `stores/session.ts`：`messages`、`turnRunning`、`draft`、`send(text)`、`lastError`。
- 事件订阅由 `App.vue` 统一建立（`events.connect()`），**聊天面板不再新建订阅**，避免重复轮次与重复写入。
- 聊天草稿复用 `session.draft`（与卡片草稿 `store.drafts["card:<id>"]` 天然分开，互不覆盖）。
- 悬浮面板只负责容器尺寸、滚动与生命周期；不套用依赖整页宽度的主对话布局。
- 输入法：沿用对话页既有判断（`e.isComposing || keyCode === 229`），中文选字时不发送。
- 未接入的能力（真实 QIO 板面理解）继续如实显示未接入，不用模拟回复。

## 5. 需要新增的前端状态（主智能体，写在 store）

`chatOpen`、`batchOpen`、`tasksOpen`、`batches`（按批分组）、`listBatches`（同批待审批 ≥4）、`nonPendingIntents`（执行中 / 已暂停 / 已结束）。
批次的判定：优先用服务端返回的 `submissionId`；演示入口一次生成的四项共用同一创建秒，按「submissionId + 创建秒」分组；**不同批次不累加**。

## 6. 验证方法

1. 前端：`npx vue-tsc --noEmit`、`npm test`（含新增的 viewport / chat / approval 用例）。
2. 后端回归：`cd backend; uv run --frozen pytest`（本轮不改后端，回归必须仍全绿）。
3. 文档：`python scripts/check_docs.py`。
4. 实机：起真实后端 + 前端 dev server，用 `scripts/visual_probe.mjs` 走提示词第七节的 12 个场景并截图（含重叠成组连续截图、透明聊天、同批四项列表、窄窗口）。
5. 独立复核（D）：读源码 + 实机复现，产出问题清单，不采信其他子智能体的「已完成」。

## 7. 语言

主智能体与全部子智能体的计划、指令、汇报、注释、文档与提交说明**一律中文**。
