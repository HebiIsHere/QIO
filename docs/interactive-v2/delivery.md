# 互动页 v2 交付说明（草稿，等待最终验证数字）

> 基线：`origin/feat/conversation-page-v2` = `13ec09357cd1477348635096a3999eb2e4ef8298`
> 集成分支：`feat/interactive-page-v2`（工作区 `D:\\qio-dev\\qio-interactive-v2`）
> 未合并 main、未打包、未发布；各阶段子分支见下表。

## 1. 阶段与提交

| 阶段 | 分支 | commit | 内容 |
| --- | --- | --- | --- |
| 基线契约 | feat/interactive-page-v2 | a7bee58b / b482d83d / 93f6ad27 | 冻结 Phase 1–3 字段、文件归属与验收边界 |
| P1-A | qio-i2/p1-a | 9ad23356 | 对话/互动共用全局栏、互动挂载点、双向模式切换 |
| P1-B | qio-i2/p1-b | 82b0e7d9 | InteractiveWorkspace / ChromeTools / 布局纯函数 |
| P1-C | qio-i2/p1-c | a79701ea | 旧能力与接口审计 |
| P1-D | qio-i2/p1-d | f8e2ea11 | 对话/审批复用与路由门控审计 |
| P2-A | qio-i2/p2-a | 5950e652 | 统一几何、确定性避让、组缩放纯逻辑 |
| P2-B | qio-i2/p2-b | d2c4fea7 | 组端点连线、收起概览、拖整组/组缩放组件 |
| P2-C | qio-i2/p2-c | fed3bbfd | 图片卡原比例、详情态、附件异步保护 |
| P2-D | qio-i2/p2-d | c716782e | 后端 participation/组端点/图片比例/权威版本 |
| P2-INT-CANVAS | qio-i2/p2-int-canvas | ab74232c | BoardCanvas 接线图片比例/组/组端点目标/参与 |
| P2-INT-STORE | qio-i2/p2-int-store | e8374f8c | store/board 层参与、组端点、collapsed、版本 |
| P3-A | qio-i2/p3-a | 65288219 | 虚拟意图层、display 位置、显式 ids 确认 |
| P3-B | qio-i2/p3-b | 6cd48bb6 | 底部互动对话复用 MessageStream/Composer/五入口 |
| P3-C | qio-i2/p3-c | 80dc09e8 | 唯一审批入口、恢复/重试事项行、保存失败 |
| P3-D | qio-i2/p3-d | d345bc4f | 确认/恢复后真实 Turn、持久化重试、固定提示词 |
| Lead 集成 | feat/interactive-page-v2 | 5c87ec8c…7ee921ff | v2 外壳、审批门控、意图/底聊/书签/工具栏/显示位置接口 |

## 2. 功能覆盖

### 已实现
- 对话与互动共用 52px 圆角全局栏、70px 标题占位、轨道品牌、设置与窗口控制；浏览器不画不可用窗口按钮。
- 互动页顶栏搜索 + 唯一保存状态；搜索浮层 Teleport 到 body，不被栏裁切。
- 双向模式切换、唯一审批入口（App 门控旧 ApprovalEntry/Modal 在聊天路由让位）、统一审批层唯一挂载。
- InteractiveWorkspace：板面、底部工具栏、输入区、向上展开对话区、桌面右侧/窄屏下侧书签栏。
- 板内搜索、书签栏（真实 bookmarked 卡片定位高亮）与顶部保存状态。
- 底部对话复用 MessageStream / Composer / ComposerEntries（附件、失败恢复、排队、历史继续、话题建议、审批）。
- 输入区上方事项行支持恢复/重试额外入口；保存失败常驻可重试。
- 卡片统一文字/文件体系；图片仍是 file 附件；原图比例、详情态、异步附件身份/版本保护。
- 卡片参与讨论控件（included/excluded），组级批量；选中/多选/框选不改参与；提交后保留。
- 组固定槽位、收起概览、拖整组、组缩放、组端点连线、成员跨组、解散组清组端点线。
- 意图虚拟层：虚线、按 intent_id 的 display、单条/批量显式 ids 确认、部分失败逐项、只认真实 turnId。
- 确认/恢复后服务端创建/恢复互动 Turn，程序绑定 board/submission/intent/authorization/attempt/topic；同一意图单有效 Turn。
- 持久化重试系列：首次失败不算、最多 3 次自动、手动一次一次、重启不重置、耗尽只说明一次。
- 固定提示词集中到 backend/src/agent/prompts.py 的指定键（见 §4）。

### 明确保留占位 / 未完成
- 标注菜单中的画笔/橡皮：点击仅显示“正在开发中，敬请期待”，不创建笔迹、不切鼠标模式。
- 旧 IntentBatchTray / IntentStatusPopover 仍保留在旧链；新链暂停/继续/取消未全部迁移。
- 自动重试 pending 扫描只在服务端互动 Turn 收尾时调度；进程启动不主动扫描。
- 崩溃后 board_intent_turns 的 stale active 认领没有实例归属回收。
- 组缩放后的重叠只如实提示，没有对所有路径做自动避让；卡片拖动仍遵循既有“拖到卡片上成组”语义。
- 新链意图的 preview/impact 数据仍主要来自旧工作台只读展示；不伪造。
- 桌面 Tauri 原生拖拽/窗口按钮需在最终阶段另做一次实机检查。

## 3. 已执行验证
- 前端全量：`npx vitest run` → 436 files / 4072 tests passed（本集成分支，2026-10-24）。
- 前端类型：`npx vue-tsc --noEmit` 通过。
- 后端定向：P3-D 15 项、Phase2 定向 528+ 项；全量结果待补。
- 文档一致性：待最终 `python scripts/check_docs.py`。

## 4. 固定提示词唯一位置
`backend/src/agent/prompts.py`（本轮逐字写入，不在其他模块复制）：
- `PROMPTS["INTERACTIVE_POLICY"]`
- `PROMPTS["INTERACTIVE_RETRY_POLICY"]`
- `TOOL_DESCRIPTIONS["board_inspect"]`
- `TOOL_DESCRIPTIONS["board_create_card"]`
- `TOOL_DESCRIPTIONS["board_update_card"]`
- `TOOL_DESCRIPTIONS["board_add_link"]`
- `TOOL_DESCRIPTIONS["board_group_cards"]`

## 5. 视觉对照
- 互动页 1440/1024/800/480：`.cv-shots/design-review/i2-final2-*.png`
- 互动页 + 书签栏：`i2-final2-bookmarks.png`
- 底聊展开：`i2-phase3-chat.png`
- 对话页同轮对照：待补 `i2-conversation-1440.png`

## 6. 已知边界与未验证
- 未执行完整 CI；最终报告需记录实际运行号/SHA。
- 真实模型主循环端到端（确认后实际工具执行）用 fake turn 测试，未联网调用真实模型。
- 图片卡视觉只用测试与静态检查，未用真实横/竖/长图实机复核。
- 互动页 320px 结构溢出、暗色、减少动画仍需最终截图验收。

## 7. 合并准备
- 不合并 main；本分支可单独评审。
- 共享文件均由 Lead 最终集成：App.vue、InteractiveView.vue、store、board.ts、types、services、WindowChrome、BoardCanvas、BoardToolbar。
- 子分支保留独立 worktree，未 push、未建 PR。