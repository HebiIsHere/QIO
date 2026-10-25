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
- 组缩放后会对组本身做确定性最小平移避让；卡片拖动仍保留既有成组语义，极端重叠只如实提示。
- 自动重试有 Turn 收尾调度；启动恢复会释放上一进程遗留的 claiming/active，并重新受理 pending 自动重试（每次启动最多 10 个系列）。重启恢复已补，但真实进程崩溃/多实例边界仍未做端到端压测。
- 自动重试调度已限定本轮 intent_ids；Turn 正常结束但意图未 completed 时，failed/partial 记失败、paused/cancelled 停止系列、不留下 running。启动恢复已补。
- 旧 IntentBatchTray/IntentStatusPopover 在新链 IntentView 存在时让位给 IntentLayer，仅保留旧链兼容展示；旧 IntentWorkbench 仍可打开用于兼容/账本查看。暂停/继续/取消的新链完整迁移仍建议后续单独收口。
- DemoIntentEntry 未挂载（按任务书演示状态不属于生产功能）。
- 320px 输入胶囊偏窄；页面无结构横向滚动，板面内容按画布裁切/平移。
- 待审批/失败/重试/部分完成的真实 UI 链路主要靠定向测试，无完整真实数据截图。
- Tauri 已用关闭引导后的 1200×800 实机截图验证共用栏、窗口按钮、板面/工具栏/模式切换（见 `i2-tauri-final.png`）；真实模型界面级执行仍未跑。

## 3. 验证
- 前端全量：`npx vitest run` → **436 files / 4072 tests passed**（最终 UI 轮）。
- 前端 `npx vue-tsc --noEmit`：通过；`npm run build`：通过；文档一致性：通过。
- 后端定向：P3-D 九文件重跑 **105 项全部通过**（含冻结面与 claiming 并发修复）；P4-C 后端 200+39 项通过。
- 后端完整全量：在最终代码上 **pytest 全量通过（exit 0）**；完整 CI 仍待运行。
- 关键失败已修：提示词冻结面同步、claiming 窗口并发认领拒绝、启动恢复 stale/pending 重试、展示/重试 HTTP 接口测试；P4-B 报告中的其余风险保留在 `phase4-backend.md`。

## 4. 固定提示词唯一位置
`backend/src/agent/prompts.py`：`INTERACTIVE_POLICY`、`INTERACTIVE_RETRY_POLICY`、`TOOL_DESCRIPTIONS` 的 board_inspect / board_create_card / board_update_card / board_add_link / board_group_cards。其他模块只引用。全文归档见 `docs/interactive-v2/prompts-frozen.md`。

## 5. 视觉对照
- `D:\qio-dev\.cv-shots\design-review\i2-side-by-side.png`（对话/互动并排）
- `i2-final4-1440.png`、`i2-conv2-1440.png`、`i2-dark-1440.png`
- `i2-final2-bookmarks.png`、`i2-phase3-chat.png`、`i2-480x640-chat.png`、`i2-320.png`
- `i2-tauri-final.png`（Tauri 1200×800：共用全局栏/窗口按钮/板面/工具栏/模式切换）

## 6. 审计报告
- `docs/interactive-v2/phase1-data-audit.md`
- `docs/interactive-v2/phase1-chat-approval-audit.md`
- `docs/interactive-v2/phase4-visual.md`
- `docs/interactive-v2/phase4-backend.md`
- `docs/interactive-v2/phase4-compat.md`
- `docs/interactive-v2/phase4-conversation.md`

## 7. 交付状态
- 最终集成分支 HEAD：`git rev-parse HEAD`（见最终回复）。
- 未合并、未打包、未发布。
- 完整后端全量已通过（最新 code SHA `d4109e71`，exit 0）。
- 最终完整 CI：**run 40041195250 全部 job 成功**（SHA `627d0670`）——docs / install e2e / frontend / backend py3.11 / py3.12 / windows / rust Ubuntu+Windows / frozen worker / dedup 全部 success。
- 对比：上一轮 CI run 40033918863 也成功（SHA `64ef4e50`），最终代码在其上补了组缩放避让、旧托盘让位、重试收尾。

## 8. 第二迭代（2026-10-24，用户主导设计）

### 已落地
- 悬浮对话：固定在底部居中，板面保持完整高度；消息向上展开，工具栏和输入区不动。
- 悬浮工具栏：添加 / 撤销 / 重做 / 标注 / 书签 / 提交；底部重复搜索已删除；提交状态改为图标按钮，点击打开提交详情。
- 卡片自动详细：取消普通卡片“展开预览”按钮，按真实尺寸 420×280 进入、360×240 退出；图片采用方向/面积规则，极端比例才出现右上角小眼睛；全部阅读状态保留。
- 板面背景：白色底 + 主题色 24px/1px 点阵；暗色深底同主题色点阵，随板面平移缩放。
- 执行记录：原意图详情新增“执行记录”，展示确认材料/版本、授权范围、实际操作、结果、失败与重试。
- 旧入口：互动页移除 IntentBatchTray / IntentStatusPopover / IntentWorkbench / ImpactConfirmDialog 的挂载，删除未挂载的 DemoIntentEntry；统一使用 IntentLayer + 统一审批 + 执行记录。

### 已知边界
- 旧组件文件仍留在仓库并继续被旧测试引用，尚未物理删除；页面入口已移除，后续可连同旧测试一起清理。
- 执行记录目前只从原意图打开；成果卡“更多”入口尚未接线（成果卡没有可用的 intent 关联字段）。
- 320px 下模式切换移到对话卡上方，页面无横向滚动；输入区仍偏窄。
- 本轮前端全量 443 files / 4095 tests 通过，`vue-tsc --noEmit` 与 `npm run build` 通过；未重跑真实模型/完整 CI，提交前仍需在最终 SHA 上跑完整 CI。

### 第二迭代交互收口（2026-10-24 续）
- 缩放：移除四边/四角尺寸热区；只在右下角保留 40px 命中区，视觉由卡片边框自身从 1px 短距离加粗到约 3px，圆角连续，不新增把手控件。
- 连线：四边端口改为贴边短条；悬停短条平滑变为圆点；拖到目标卡片建立关系，线吸附卡片边缘。
- 板面：点阵背景移到整个视野层；视口内任意位置都有点阵，并可继续平移/操作；画布表面不再决定点阵覆盖范围。
- 验证：前端全量 443 files / 4088 tests passed；vue-tsc 与 npm run build 通过。
- 未重跑完整 CI；旧组件文件仍未物理删除。

## 9. 第三轮：互动页 v2 前端操作逻辑修复（F01–F10，2026-10-25）

> 基线 9b23634892fa7fac0a8ccd471626d07d2bdefd0e（origin/feat/interactive-page-v2，fetch 后确认未前进）。
> 修复分支 fix/interactive-v2-frontend-flow；未合并 main、未打包、未发布。
> 逐项根因 / 改动 / 验证证据见 docs/interactive-v2/frontend-flow-fixes-20261025.md。

### 9.1 内容

- F01 材料影响确认回到页面：唯一确认面，真实受影响任务 / 材料 / 后果、「等待确认，尚未保存」、确认 / 取消、确认过期与保存失败处理；与工具 / 权限审批「同一时刻只有一个阻塞面」。
- F02 意图状态唯一维护位置（store）：延迟理解、刷新、重试、确认 / 恢复、任务控制后统一刷新；旧链重复预览让位；旧失败 / 旧回执不覆盖新状态；回前台与断线只读复核；离开页面清理计时器。
- F03 「任务」与「批量审批」入口接到真实任务面板：暂停 / 继续 / 取消、执行记录、逐条去向；聊天停止与互动任务取消明确分离；新链有视图时旧提交区不再重复批准 / 拒绝。
- F04 从已有关系两端重复拖线只打开原关系，不覆盖 id / 含义 / 方向 / 曲线；关闭编辑器不产生修改；显式保存才更新；删除后可重建。
- F05 恢复事项摘要进 store：稳定去重、覆盖保存 / 草稿 / 提交刷新 / 结果未知 / 冲突 / 可恢复任务 / 重试用尽 / 影响确认等待；读取失败不伪装为空；处理后立即减少；对话收起仍可打开面板。
- F06 意图读取失败保留旧内容 + 区分「更新失败」/「从未读到」+ 可重试；板面读取失败的重试同时恢复意图读取。
- F07 菜单移出组：先收拢剩余成员，再为移出卡片找组外最近合理空位；成员 + 位置一次提交；密集布局如实反馈。
- F08 定位收起组内卡片：先展开组、等布局可用，再按扣除浮动对话等遮挡后的实际可用视野定位并高亮。
- F09 关系胶囊拖动：按画布 scale 换算屏幕位移；只写临时预览，正常松手一次提交；Esc / pointercancel / 失焦 / 卸载取消并清理监听；pointerId 约束；拖动结束不误触编辑 / 删除。
- F10 意图位置：区分临时 / 等待保存 / 已确认；失败回到本次操作前位置；按对象与操作版本归属；服务器位置优先于本机缓存；pointercancel 取消回手势开始位置。

### 9.2 验证与边界

- 真实页面（隔离数据域 + 受控板面 + Playwright）：F01–F10 的主路径在 1440×900、700×800、480×760 下逐条操作验证；受控注入 500 / 延迟覆盖读取失败、保存失败、位置保存失败；没有使用真实 API Key 或真实模型。
- 最终全量前端测试在 `58c86718` 上运行（466 files / 4267 tests）：1 项 A 组 F05 测试与集成收口冲突而失败；保留两条提示各自重试入口、只做纵向堆叠后，重跑 6 个直接相关文件 26 tests 全绿。随后完整 CI 第一次（`eb524cae`）在 frontend typecheck 暴露 D 组验收测试 4 处类型错误，已仅修测试文件类型并重跑 vue-tsc / 受影响验收测试（通过）；修复提交后重新运行完整 CI，最终结果以新 run 为准（run ID / URL / SHA 见交付回复）。
- 已知边界：组收起仍是会话内显示状态（不读 board.groups[].collapsed，刷新不保持）；新建关系仍先落空含义再打开编辑器（F04 只修重复拖线覆盖）；F10 取消手势走 pointercancel，未新增 Esc 取消意图拖动；受控种子任务没有真实模型执行，真实模型成果卡链路未验证；延迟理解用受控测试覆盖时序。