# 互动页 v2 交付说明

> 基线：`origin/feat/conversation-page-v2` = `13ec09357cd1477348635096a3999eb2e4ef8298`
> 集成分支：`feat/interactive-page-v2`（`D:\qio-dev\qio-interactive-v2`）；最终 HEAD 见 §7。
> 未合并 main、未打包、未发布。

## 1. 阶段与提交

| 阶段 | 分支 | commit | 内容 |
| --- | --- | --- | --- |
| 契约 | feat/interactive-page-v2 | a7bee58b / b482d83d / 93f6ad27 | 冻结 Phase 1–3 字段、文件归属、验收边界 |
| P1-A | qio-i2/p1-a | 9ad23356 | 共用全局栏、互动挂载点、双向模式切换 |
| P1-B | qio-i2/p1-b | 82b0e7d9 | InteractiveWorkspace / ChromeTools / 布局纯函数 |
| P1-C | qio-i2/p1-c | a79701ea | 旧能力与接口审计 |
| P1-D | qio-i2/p1-d | f8e2ea11 | 对话/审批复用与路由门控审计 |
| P2-A | qio-i2/p2-a | 5950e652 | 统一几何、确定性避让、组缩放纯逻辑 |
| P2-B | qio-i2/p2-b | d2c4fea7 | 组端点连线、收起概览、拖整组/组缩放组件 |
| P2-C | qio-i2/p2-c | fed3bbfd | 图片卡原比例、详情态、附件异步保护 |
| P2-D | qio-i2/p2-d | c716782e | 后端 participation/组端点/图片比例/权威版本 |
| P2-INT-CANVAS | qio-i2/p2-int-canvas | ab74232c | BoardCanvas 接线 |
| P2-INT-STORE | qio-i2/p2-int-store | e8374f8c | store/board 参与/组端点/折叠/版本 |
| P3-A | qio-i2/p3-a | 65288219 | 虚拟意图层、display、确认结果 |
| P3-B | qio-i2/p3-b | 6cd48bb6 | 底聊复用 MessageStream/Composer/五入口 |
| P3-C | qio-i2/p3-c | 80dc09e8 | 唯一审批入口、恢复/重试事项行、保存失败 |
| P3-D | qio-i2/p3-d | d345bc4f | 确认/恢复真实 Turn、持久重试、固定提示词 |
| P4-B/C/D | qio-i2/p4-b/c/d | eab0e81d / e11b4900 / 73e841dc | 后端、兼容、对话回归审计 |
| Lead 集成 | feat/interactive-page-v2 | 5c87ec8c…ac3dc209 | 外壳、审批门控、意图/底聊/书签/工具栏/接口/窄窗修复 |

## 2. 功能覆盖

### 已实现/迁移
- 共用 52px 圆角全局栏、70px 标题占位、轨道品牌、设置、窗口控制；浏览器不画不可用窗口按钮。
- 互动顶栏搜索 + 唯一保存状态；搜索浮层 Teleport body，不被截断。
- 双向模式切换；互动页模式切换位于输入胶囊外；App 门控旧审批入口/旧弹窗只在非聊天路由。
- InteractiveWorkspace：板面、底部工具栏、输入区、向上展开底聊、桌面右侧/窄屏下侧书签栏。
- 工具栏：添加、搜索、撤销、重做、标注占位（画笔/橡皮只显示“正在开发中，敬请期待”）、书签、提交。
- 书签栏列出真实 bookmarked 卡片，点击定位高亮；空态如实说明。
- 底聊复用 MessageStream/Composer/ComposerEntries：流式、停止、附件、失败恢复、排队、历史继续、话题建议、审批；展开/收起不影响回复、草稿、附件、排队。
- 事项行支持恢复/重试额外入口；保存失败常驻可重试；统一审批层唯一挂载。
- 卡片统一文字/文件；图片仍为 file 附件，原图比例、详情态铺满、异步身份/版本保护。
- 参与讨论 included/excluded 为唯一状态；组批量、选择不改参与、提交后保留。
- 组固定槽位、收起概览、拖整组、组缩放、组端点连线、跨组、解散清组端点线。
- 虚拟意图层：虚线、按 intent_id 的 display、单条/批量显式 ids、部分失败逐项；确认后服务端真实 Turn。
- 持久化重试：首次失败不占次数、最多 3 次自动、手动一次一次、重启不重置、耗尽只说明一次。
- 提示词固定文案集中在 `backend/src/agent/prompts.py` 的指定键。

### 保留占位/已知缺口
- 组缩放后重叠只如实提示；没有覆盖所有路径的自动避让；卡片拖动仍保留既有成组语义。
- 自动重试只在互动 Turn 收尾调度；进程启动不扫 pending；崩溃后 `board_intent_turns` 的 claiming/active 无实例归属回收，可能永久挡住新 Turn。
- 任意互动 Turn 收尾都可能调度整板 pending 重试（需限制边界）；成功但未 completed 的 Turn 可能使 retry 系列停在 running。
- 旧 IntentBatchTray/IntentStatusPopover 仍挂旧链，与新 IntentLayer 同页并存；暂停/继续/取消新链未全部迁移。
- DemoIntentEntry 未挂载（按任务书演示状态不属于生产功能）。
- 320px 输入胶囊偏窄；页面无结构横向滚动，板面内容按画布裁切/平移。
- 待审批/失败/重试/部分完成的真实 UI 链路主要靠定向测试，无完整真实数据截图。
- 桌面 Tauri 已启动并确认栏/窗口按钮存在；引导弹窗遮挡下的主界面未作为视觉通过证据，实机最终验收仍建议单列。

## 3. 验证
- 前端全量：`npx vitest run` → **436 files / 4072 tests passed**（最终 UI 轮）。
- 前端 `npx vue-tsc --noEmit`：通过；`npm run build`：通过；文档一致性：通过。
- 后端定向：P3-D 九文件重跑 **105 项全部通过**（含冻结面与 claiming 并发修复）；P4-C 后端 200+39 项通过；**完整后端全量未在最终 SHA 完成**。
- 关键失败已修：提示词冻结面同步、claiming 窗口并发认领拒绝；P4-B 报告中的其余风险保留在 `phase4-backend.md`。

## 4. 固定提示词唯一位置
`backend/src/agent/prompts.py`：`INTERACTIVE_POLICY`、`INTERACTIVE_RETRY_POLICY`、`TOOL_DESCRIPTIONS` 的 board_inspect / board_create_card / board_update_card / board_add_link / board_group_cards。其他模块只引用。全文归档见 `docs/interactive-v2/prompts-frozen.md`。

## 5. 视觉对照
- `D:\qio-dev\.cv-shots\design-review\i2-side-by-side.png`（对话/互动并排）
- `i2-final4-1440.png`、`i2-conv2-1440.png`、`i2-dark-1440.png`
- `i2-final2-bookmarks.png`、`i2-phase3-chat.png`、`i2-480x640-chat.png`、`i2-320.png`、`i2-tauri-interactive.png`

## 6. 审计报告
- `docs/interactive-v2/phase1-data-audit.md`
- `docs/interactive-v2/phase1-chat-approval-audit.md`
- `docs/interactive-v2/phase4-visual.md`
- `docs/interactive-v2/phase4-backend.md`
- `docs/interactive-v2/phase4-compat.md`
- `docs/interactive-v2/phase4-conversation.md`

## 7. 交付状态
- 最终集成分支 HEAD：`git rev-parse HEAD`（见最终回复）。
- 未运行完整 CI、未 push 主分支、未合并、未打包。
- 完整后端全量/完整 CI 是交付前必须补的两项；本文件不把定向测试冒充完整验收。