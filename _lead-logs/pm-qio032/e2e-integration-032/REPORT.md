# QIO 0.3.2 · 真实界面验收报告（ui-verify）

> 生成者：accept-inventory（验收入口清单员）。驱动脚本在仓库外：`D:\qio-dev\_lead-logs\integration-032\ui-verify\`。
> 本报告汇总两次运行：**主验收**（`summary.json`，runId 20261113172945）与 **#47 宽窗定向复跑**（`summary-layout.json`，runId 20261113173858）。

## 1. 身份与环境

| 项 | 值 |
|---|---|
| 集成 HEAD | `acf323a3669010b7ae99f5d9c1989bf4f58340fd`（两次运行一致；`git rev-parse HEAD` 实测） |
| 分支 | `fix/pm-qio032-integration` |
| 工作树状态 | 干净（仅 2 个未跟踪的 Lead 证据文件：`_lead-logs/pm-qio032/integration-032/verify-final.ps1`、`_lead-logs/pm-qio032/reports/lead-integration-032.md`；无代码改动） |
| 数据域（主验收） | `D:\qio-dev\_lead-logs\integration-032\ui-data-final2`（**fresh**，冷库迁移后首次使用） |
| 数据域（#47 定向） | 同上 `ui-data-final2`（复用，已含主验收产生的消息） |
| 服务 | backend 8734 / vite 5199 / fake provider 8807 / CDP 9333（系统 Edge headless） |
| 模型 | 本机 fake provider（`scripts/verify-pm-qio032/fake_provider.py`），**无真实网络、无真实模型** |
| 浏览器层 | 系统 Edge + CDP（`scripts/verify-pm-qio032/cdp.mjs`），非 Playwright |

## 2. 总览

| 运行 | PASS | FAIL | UNVERIFIED | TOTAL | 文件 |
|---|---|---|---|---|---|
| 主验收 | 22 | 1 | 5 | 28 | `summary.json` |
| #47 宽窗定向复跑 | 14 | 0 | 0 | 14 | `summary-layout.json` |

**合并口径：#47 宽窗四步（L2–L10）全部 PASS**，主验收里 s1 的 FAIL 由该定向复跑覆盖并定位为**窄口径产品问题**（见 §4）。

**一句话结论**：本轮界面验收共 42 条断言（主验收 28 + #47 定向 14），**PASS 36 / FAIL 1 / UNVERIFIED 5**；唯一 FAIL 是产品问题 P1（未持久化消息的代码预览停在读取态），5 条 UNVERIFIED 中 4 条已被 #47 定向复跑覆盖为 PASS、1 条是「真实入口无失败浮层形态」。

## 3. 逐条结论（主验收 `summary.json`）

| # | 结论 | 条目 | 一句事实 | 证据 |
|---|---|---|---|---|
| 1 | **PASS** | 环境-0 服务与集成 HEAD 记录 | HEAD=acf323a3669010b7ae99f5d9c1989bf4f58340fd | `summary.json` |
| 2 | **PASS** | 环境-2 后端会话可用（GET /api/settings/ui，只读核对服务端真值） | {"typewriter_cps":50,"hasConversationLayout":true,"error":null} | `summary.json` |
| 3 | **PASS** | 环境-1 关键选择器预检（对话/互动/设置） | {"checks":[{"group":"conversation","missing":[]},{"group":"interactive-menu","missing":[]},{"group":"interactive","missing":[]},{"group":"settings","missing":[]},{"group":"settings-about","missing":[] | `summary.json` |
| 4 | **FAIL** | s1 场景未跑完 | Error: 预览搜索框未出现（python 标签）：{"tabId":"replycode:local_1794591004179_2:0","readerKind":"code","tools":false,"search":false,"scroll":false,"problem":null,"stateText":"正在读取这个文件…"} | `failure-s1-20261113173211-provider-log.json`<br>`failure-s1-20261113173211-backend-tail.log`<br>`failure-s1-20261113173211-page.json` |
| 5 | **UNVERIFIED** | #47-9 窄窗（800×600+预览占位）：自动收起/全窗口未被写成用户偏好 | s1 未产出两个预览标签（s1 前置不成立/失败），无法在“预览占位”条件下验证窄窗自动布局；按 UNVERIFIED 记录，不判产品缺陷 | `summary.json` |
| 6 | **UNVERIFIED** | #47-10 窄窗下点击还原默认布局 | s1 未产出两个预览标签（s1 前置不成立/失败），无法在“预览占位”条件下验证窄窗自动布局；按 UNVERIFIED 记录，不判产品缺陷 | `summary.json` |
| 7 | **UNVERIFIED** | #47-11 窄窗重开预览：自动全窗口 | s1 未产出两个预览标签（s1 前置不成立/失败），无法在“预览占位”条件下验证窄窗自动布局；按 UNVERIFIED 记录，不判产品缺陷 | `summary.json` |
| 8 | **UNVERIFIED** | #47-12 拉宽回分栏 | s1 未产出两个预览标签（s1 前置不成立/失败），无法在“预览占位”条件下验证窄窗自动布局；按 UNVERIFIED 记录，不判产品缺陷 | `summary.json` |
| 9 | **PASS** | #46-1 上传在途时切到设置页（两张截图） | {"shots":["46-uploading-interactive","46-uploading-settings"],"simulated":true} | `shots/03-46-uploading-interactive.png`<br>`shots/04-46-uploading-settings.png` |
| 10 | **PASS** | #46-2 返回后只多一张卡且数量稳定（无重复卡） | {"before":0,"after":1,"stable":1} | `summary.json` |
| 11 | **PASS** | #46-3 文件卡已关联：工具栏变「重新关联附件」且无「未关联」提示 | {"card":{"index":0,"count":1,"text":"文本\ncrosspage-20261113172945.txt\n显示并参与\n参与\n\n文本\ncrosspage-20261113172945.txt\n449 B\n\n材料详情（路径 / 来源 / 引用）\n11-14 01:32","unlinked":false,"retry":false,"statusTe | `summary.json` |
| 12 | **PASS** | #46-4 界面没有要求重新选文件/重新导入；成功状态可读 | 提示：已关联附件「crosspage-20261113172945.txt」：已保存为工作区材料源文件（来源：浏览器上传）；板面关联是本地改动，提交后 QIO 才可见 | `summary.json` |
| 13 | **PASS** | #46-5 补充：单文件拖放批次结算后显示「已接收 1 份」（非跨页） | 批次结算后：本批已接收 1 份 / 未接收 0 份（单批上限 5 份） | `summary.json` |
| 14 | **PASS** | #47-13 设置搜索「单段长度」：自动展开「记忆·高级选项」并定位到可聚焦输入框 | {"foldExpanded":"true","targetExists":true,"targetVisible":true,"highlighted":false,"inputExists":true,"inputLabel":"单段长度目标","inputFocused":true,"category":"memory","rect":{"x":281,"y":480.65625,"widt | `summary.json` |
| 15 | **PASS** | #47-14 设置搜索「还原默认布局」：命中并定位到真实按钮 | {"inputValue":"还原默认布局","hitKeys":["regular.window.reset-layout"],"groups":["常规"],"highlightObserved":true,"hit":true,"targetExists":true,"targetVisible":true,"highlighted":true,"buttonText":"还原默认布局"," | `summary.json` |
| 16 | **PASS** | #47-15 [模拟] 布局保存失败（HTTP 500）：明确失败提示 + 重试入口 | 提示：还原默认布局失败 | `summary.json` |
| 17 | **PASS** | #47-16 [模拟] 重试成功：显示「已还原默认布局」 | 提示：已还原默认布局（预览标签与阅读位置保留） | `summary.json` |
| 18 | **PASS** | 视觉-light-1440x900 控件可达 / 无横向溢出 / 无重复提示 | {"residualBoardCards":0,"convBase":{"pass":true,"controlsOk":true,"failingControls":[],"overflowOk":true,"overflowing":[],"docOverflow":false,"duplicatesOk":true,"duplicateNotices":[]},"convPreview":{ | `summary.json` |
| 19 | **PASS** | 视觉-light-1440x900 关键截图（对话底部/预览标题/互动底部/设置搜索） | {"shots":["D:\\qio-dev\\_lead-logs\\integration-032\\ui-verify\\shots\\11-matrix-light-1440x900-conversation.png","D:\\qio-dev\\_lead-logs\\integration-032\\ui-verify\\shots\\12-matrix-light-1440x900- | `shots/11-matrix-light-1440x900-conversation.png`<br>`shots/12-matrix-light-1440x900-preview-head.png`<br>`shots/13-matrix-light-1440x900-interactive.png`<br>`shots/14-matrix-light-1440x900-interactive-bottom.png`<br>`shots/15-matrix-light-1440x900-settings-search.png`<br>`shots/16-matrix-light-1440x900-settings.png` |
| 20 | **PASS** | 视觉-light-800x600 控件可达 / 无横向溢出 / 无重复提示 | {"residualBoardCards":0,"convBase":{"pass":true,"controlsOk":true,"failingControls":[],"overflowOk":true,"overflowing":[],"docOverflow":false,"duplicatesOk":true,"duplicateNotices":[]},"convPreview":{ | `summary.json` |
| 21 | **PASS** | 视觉-light-800x600 关键截图（对话底部/预览标题/互动底部/设置搜索） | {"shots":["D:\\qio-dev\\_lead-logs\\integration-032\\ui-verify\\shots\\17-matrix-light-800x600-conversation.png","D:\\qio-dev\\_lead-logs\\integration-032\\ui-verify\\shots\\18-matrix-light-800x600-pr | `shots/17-matrix-light-800x600-conversation.png`<br>`shots/18-matrix-light-800x600-preview-head.png`<br>`shots/19-matrix-light-800x600-interactive.png`<br>`shots/20-matrix-light-800x600-interactive-bottom.png`<br>`shots/21-matrix-light-800x600-settings-search.png`<br>`shots/22-matrix-light-800x600-settings.png` |
| 22 | **PASS** | 视觉-dark-1440x900 控件可达 / 无横向溢出 / 无重复提示 | {"residualBoardCards":0,"convBase":{"pass":true,"controlsOk":true,"failingControls":[],"overflowOk":true,"overflowing":[],"docOverflow":false,"duplicatesOk":true,"duplicateNotices":[]},"convPreview":{ | `summary.json` |
| 23 | **PASS** | 视觉-dark-1440x900 关键截图（对话底部/预览标题/互动底部/设置搜索） | {"shots":["D:\\qio-dev\\_lead-logs\\integration-032\\ui-verify\\shots\\23-matrix-dark-1440x900-conversation.png","D:\\qio-dev\\_lead-logs\\integration-032\\ui-verify\\shots\\24-matrix-dark-1440x900-pr | `shots/23-matrix-dark-1440x900-conversation.png`<br>`shots/24-matrix-dark-1440x900-preview-head.png`<br>`shots/25-matrix-dark-1440x900-interactive.png`<br>`shots/26-matrix-dark-1440x900-interactive-bottom.png`<br>`shots/27-matrix-dark-1440x900-settings-search.png`<br>`shots/28-matrix-dark-1440x900-settings.png` |
| 24 | **PASS** | 视觉-dark-800x600 控件可达 / 无横向溢出 / 无重复提示 | {"residualBoardCards":0,"convBase":{"pass":true,"controlsOk":true,"failingControls":[],"overflowOk":true,"overflowing":[],"docOverflow":false,"duplicatesOk":true,"duplicateNotices":[]},"convPreview":{ | `summary.json` |
| 25 | **PASS** | 视觉-dark-800x600 关键截图（对话底部/预览标题/互动底部/设置搜索） | {"shots":["D:\\qio-dev\\_lead-logs\\integration-032\\ui-verify\\shots\\29-matrix-dark-800x600-conversation.png","D:\\qio-dev\\_lead-logs\\integration-032\\ui-verify\\shots\\30-matrix-dark-800x600-prev | `shots/29-matrix-dark-800x600-conversation.png`<br>`shots/30-matrix-dark-800x600-preview-head.png`<br>`shots/31-matrix-dark-800x600-interactive.png`<br>`shots/32-matrix-dark-800x600-interactive-bottom.png`<br>`shots/33-matrix-dark-800x600-settings-search.png`<br>`shots/34-matrix-dark-800x600-settings.png` |
| 26 | **PASS** | 视觉-失败态（真实入口：设置→关于与更新→检查更新） | 真实入口的失败提示是内联 .line.err，不是浮层；已保留截图 | `summary.json` |
| 27 | **UNVERIFIED** | 视觉-失败浮层（真实入口） | 真实入口只出现内联失败提示（.line.err），没有浮层形态；失败态本身已取证，见截图 matrix-failure-inline | `summary.json` |
| 28 | **PASS** | 视觉-矩阵总览（2 主题 × 2 尺寸） | {"combos":[{"label":"light-1440x900","pass":true,"shots":6},{"label":"light-800x600","pass":true,"shots":6},{"label":"dark-1440x900","pass":true,"shots":6},{"label":"dark-800x600","pass":true,"shots": | `summary.json` |

## 3.1 关于主验收里 s1 的 FAIL（重要）

主验收的 s1 是**流程脚本**：它要求「刚流式产出的消息」上的代码预览立刻可搜索。该要求在真实产品里**不成立**（见 §4 P1），因此 s1 判 FAIL 反映的是产品行为，不是脚本等待不足。
同一批 #47 断言在**已持久化消息**条件下由 `summary-layout.json` 完整复跑：**14/14 PASS**，覆盖 (a) 两标签各自搜索词/滚动写入服务端 `conversation_layout`、(b) 设置页还原后栏宽回 292/420 + 左栏展开 + 预览收起、(c) 重开预览标签/当前标签/搜索词/滚动位置都在、(d) 整页刷新后仍成立、(e) 窄窗 800×600 自动收起/全窗口不写偏好 + 拉宽回分栏。

## 4. 产品问题（唯一一条，已定位）

### P1 · 从「刚流式产出、尚未持久化」的消息打开代码预览，阅读器停在「正在读取这个文件…」

- **结论**：**窄口径产品问题**（不是 fixture、不是脚本等待问题）。
- **现象**：主验收 s1 中，助手刚流式产出两段代码块后立刻点「在右侧查看这段代码」，预览标签 id 为 `replycode:local_1794591004179_2:0`（`local_` 前缀 = 本地临时消息 id），阅读器 `readerKind=code`、`tools=false`、`search=false`、`scroll=false`、`stateText="正在读取这个文件…"`，直到 120s 超时都没有进入可搜索/可滚动状态。
- **对照证据（同一台机、同一数据域）**：`diag-preview.json` 与 `summary-layout.json` 的 `#47-L2` 显示，**已持久化**的消息（id 形如 `replycode:msg_f89b84e33d4b:0`）打开同一段代码预览时 `search=true`、`scroll=true`、`tools=true`，内容正常渲染（`Python · 共 70 行`）。
- **复现步骤**：① 对话页发一条会让助手回复两段代码块的消息；② 回复刚出现（消息仍在流式/尚未落库）时立刻点代码块下方的「在右侧查看」；③ 右侧预览停在「正在读取这个文件…」，无搜索框、无滚动区。整页刷新后再点同一按钮则正常。
- **影响面**：只影响「刚产出、尚未持久化」这一瞬间的预览入口；刷新后或对历史消息无影响。
- **证据文件**：`failure-s1-20261113173211-page.json`、`failure-s1-20261113173211-provider-log.json`、`failure-s1-20261113173211-backend-tail.log`、`shots/01-s1-failed.png`；对照：`diag-preview.json`、`summary-layout.json`（#47-L2）、`shots-layout/01-layout-reply-codeblocks.png`。

## 5. #47 宽窗定向复跑（`summary-layout.json`，14/14 PASS）

关键方法：发消息拿到两个代码块后**先整页 reload**（让消息持久化、id 变成服务端 id），再点最后两个代码块预览按钮。

| # | 结论 | 条目 | 一句事实 |
|---|---|---|---|
| 0 | **PASS** | 环境-0 HEAD 与工作树记录 | HEAD=acf323a3669010b7ae99f5d9c1989bf4f58340fd |
| 1 | **PASS** | #47-L0 助手回复两段代码块（本轮唯一标记） | 代码块按钮：4 |
| 2 | **PASS** | #47-L1 刷新后消息已持久化（代码块按钮仍在） | 代码块按钮：["在右侧查看这段python代码（只读）","在右侧查看这段javascript代码（只读）","在右侧查看这段python代码（只读）","在右侧查看这段javascript代码（只读）"] |
| 3 | **PASS** | #47-L2 持久化消息的代码预览进入可搜索/可滚动状态 | {"tabId":"replycode:msg_f89b84e33d4b:0","readerKind":"code","tools":true,"search":true,"scroll":true,"stateText":null} |
| 4 | **PASS** | #47-L3 打开两个预览标签 | {"tabs":[{"id":"replycode:msg_f89b84e33d4b:0","active":false,"title":"python\n代码\n×"},{"id":"replycode:msg_f89b84e33d4b:1","active":true,"title":"javascript\n代码\n×"}]} |
| 5 | **PASS** | #47-L4 两个标签各自有搜索词与滚动位置（DOM） | DOM 实测：replycode:msg_f89b84e33d4b:0 search=alpha-20261113173858 scrollTop=420 ; replycode:msg_f89b84e33d4b:1 search=beta-20261113173858 scrollTop=260 |
| 6 | **PASS** | #47-L5 搜索词与阅读位置进入 conversation_layout（服务端真值） | 标签/搜索/滚动：replycode:msg_f89b84e33d4b:0 search=alpha-20261113173858 scrollTop=420 ; replycode:msg_f89b84e33d4b:1 search=beta-20261113173858 scrollTop=260 |
| 7 | **PASS** | #47-L6 拖动左右分隔线 + 收起左栏（服务端已记录） | 服务端 prefs：{"leftWidth":420,"leftCollapsed":true,"rightWidth":372,"rightOpen":true} |
| 8 | **PASS** | #47-L7 还原默认布局：成功反馈 + 服务端偏好回默认 + 标签/搜索保留 | 标签/搜索/滚动：replycode:msg_f89b84e33d4b:0 search=alpha-20261113173858 scrollTop=420 ; replycode:msg_f89b84e33d4b:1 search=beta-20261113173858 scrollTop=260 |
| 9 | **PASS** | #47-L8 返回对话：左栏 292 展开、预览收起、预览入口显示 2 标签 | {"leftPane":true,"leftWidth":292,"rightPane":false,"rightWidth":null,"leftAuto":"false","previewFull":"false","overlay":false,"reopen":true,"reopenLabel":"打开文件预览，2 个标签","centerWidth":"1080","viewport":{"w":1440,"h":900}} |
| 10 | **PASS** | #47-L9 重新打开预览：两标签/当前标签/搜索词/滚动位置都在 | {"activeBefore":"replycode:msg_f89b84e33d4b:1","activeAfter":"replycode:msg_f89b84e33d4b:1","after":[{"id":"replycode:msg_f89b84e33d4b:0","title":"python\n代码\n×","search":"alpha-20261113173858","scrollTop":420},{"id":"re |
| 11 | **PASS** | #47-L10 整页刷新：默认布局字段仍默认 + 标签/当前标签/搜索词/位置都在 | 服务端 prefs：{"leftWidth":292,"rightWidth":420,"leftCollapsed":false,"previewFull":false,"rightOpen":true} |
| 12 | **PASS** | #47-L11 窄窗（800×600+预览占位）：自动收起左栏且预览临时全窗口，未写成用户偏好 | 服务端 prefs：{"leftCollapsed":false,"previewFull":false} |
| 13 | **PASS** | #47-L12 拉宽到 ≥1280：回到分栏，偏好仍是默认 | 服务端 prefs：{"leftWidth":292,"previewFull":false} |

截图：`shots-layout/01-layout-reply-codeblocks.png` … `09-layout-wide-restored.png`（9 张）。

## 6. [模拟] 项（明确标注，不是真实故障）

- ③-1 上传延迟 4.5s（CDP Fetch delayRequests，等价 route 延迟；真实端点 POST /api/attachments/upload）
- ③-2 [模拟] PUT /api/settings/ui（conversation_layout）返回 500：CDP Fetch fulfillRequest，只影响还原布局这一次写入

## 7. UNVERIFIED（不是 PASS，也不是产品缺陷）

- **#47-9 窄窗（800×600+预览占位）：自动收起/全窗口未被写成用户偏好**：s1 未产出两个预览标签（s1 前置不成立/失败），无法在“预览占位”条件下验证窄窗自动布局；按 UNVERIFIED 记录，不判产品缺陷
- **#47-10 窄窗下点击还原默认布局**：s1 未产出两个预览标签（s1 前置不成立/失败），无法在“预览占位”条件下验证窄窗自动布局；按 UNVERIFIED 记录，不判产品缺陷
- **#47-11 窄窗重开预览：自动全窗口**：s1 未产出两个预览标签（s1 前置不成立/失败），无法在“预览占位”条件下验证窄窗自动布局；按 UNVERIFIED 记录，不判产品缺陷
- **#47-12 拉宽回分栏**：s1 未产出两个预览标签（s1 前置不成立/失败），无法在“预览占位”条件下验证窄窗自动布局；按 UNVERIFIED 记录，不判产品缺陷
- **视觉-失败浮层（真实入口）**：真实入口只出现内联失败提示（.line.err），没有浮层形态；失败态本身已取证，见截图 matrix-failure-inline
- 说明：#47-9..12 的 UNVERIFIED 是**主验收**里 s1 前置不成立导致的；同一批断言在 `summary-layout.json` 的 #47-L11/L12 已 **PASS**（预览占位 + 800×600 → `data-left-auto=true`、`data-preview-full=true`，服务端 prefs 仍默认；拉宽 1440 回分栏）。

## 8. 分类汇总

| 类别 | 条目 |
|---|---|
| **产品问题** | P1（未持久化消息的代码预览停在读取态） |
| **fixture / 脚本问题（已修，非产品缺陷）** | ① 要求解析调用吃掉 fake provider 脚本首步（改为 JSON+代码块顺序脚本）；② 复用数据域累积历史消息导致「第几个按钮」漂移（改为唯一标记 + 最后两个按钮）；③ 搜索框未清空导致 s5 查询拼接（改为先清空再输入）；④ #46-5 读得太早（改为轮询到批次结算）；⑤ 视觉矩阵探针把「预览搜索框」当必选（改为可选观测）；⑥ 窄窗断言期望值错误（无预览时允许左栏并排，改为先开预览再缩窗） |
| **未验证** | 视觉-失败浮层（真实入口只有内联 `.line.err`，无浮层形态）；② 板面读工具链路（沿用上一轮 E2E 的 UNVERIFIED，本轮未覆盖） |

## 9. 截图清单

**主验收（`shots/`，62 张）**：
- 矩阵：`11..34-matrix-{light,dark}-{1440x900,800x600}-{conversation,preview-head,interactive,interactive-bottom,settings-search,settings}.png`
- #46：`03/04-46-uploading-interactive.png`、`04/05-46-uploading-settings.png`、`05/06-46-returned-board.png`、`06/07-46-drop-batch-success.png`
- 设置搜索：`07/08-settings-search-fold.png`、`08-settings-search-reset-layout.png`
- [模拟] 保存失败：`09/10-47-save-failure-500.png`、`10/11-47-save-retry-ok.png`
- 失败态：`32/35-matrix-failure-inline.png`
- s1 失败现场：`01-s1-failed.png`、`02-s1-failed.png`

**#47 定向（`shots-layout/`，9 张）**：`01-layout-reply-codeblocks`、`02-layout-two-tabs`、`03-layout-custom`、`04-layout-reset-notice`、`05-layout-after-reset`、`06-layout-reopened`、`07-layout-after-reload`、`08-layout-narrow`、`09-layout-wide-restored`。

## 10. 边界（结论能读到哪）

- fake provider 是本机扮演的假厂商（真 SSE、有请求台账），只证明「QIO 自己的链路对」，不证明真实厂商兼容性。
- 浏览器层是系统 Edge + CDP，不是 Playwright；没有自动等待/严格模式语义，所有等待都是显式轮询 + 超时。
- 允许且只允许的模拟：CDP Fetch 延迟/中断、`Fetch.fulfillRequest` 返回 500；均在 `summary.json` 的 `simulated` 列明。
- 未覆盖：真实 Tauri 原生窗口拖放、真实厂商网络行为、② 板面读工具链路（沿用上一轮 UNVERIFIED）。

---

产物目录：`D:\qio-dev\_lead-logs\integration-032\ui-verify\`（`summary.json`、`summary-layout.json`、`REPORT.md`、`shots/`、`shots-layout/`、`failure-s1-*`、`diag-preview.json`、`run.log`、`run-layout.log`）。
