# 互动页 v2 · Phase 1-D 对话/审批复用与路由门控审计

> 审计基线：worktree D:/qio-dev/qio-i2-p1-d，分支 qio-i2/p1-d，commit 13ec0935（feat(conversation): 顶栏收窄、轨道品牌标志、移除代码预览成功提示）。
> 审计性质：只读审计。本文件是唯一写入；没有修改任何生产代码、测试或配置。
> 引用格式：文件路径:行号（均为上述基线的行号；后续改动会让行号漂移，结论以代码事实为准）。
> 相关契约：docs/interactive-mode-contract.md、docs/conversation-page-contract-20261022.md、docs/conversation-page-v2-merge-notes.md。

---

## 0. 一句话结论

对话页 v2 已经把「聊天、草稿、失败恢复、排队、历史继续、话题建议、停止、统一审批」做成了完整的一套；互动页的 ChatDock 只复用了其中的会话底层（session.draft、session.send、失败记录、消息渲染），**没有复用输入区的五入口、停止、排队、历史继续、话题建议、附件持久化和媒体提示**，审批则完全停留在旧 ApprovalEntry / ApprovalModal。

因此 Phase 1-D 的主线不是「再写一套互动聊天」，而是：

1. 先把**路由归属门控**做对：互动路由也必须由统一审批层接管，旧 ApprovalEntry / ApprovalModal 在互动页让位；互动页恰好挂载一套统一审批层，并且要给它一个可达入口（输入区上方第⑤项）。
2. 再把 ChatDock 替换为**对话页既有组件的复用组合**（MessageStream + Composer + ComposerEntries + ContinueBar），只保留互动专有的 chatOpen / chatFrame 作为「本机查看偏好」并在新外壳里决定去留。
3. 模式切换组件 ConversationModeSwitch 目前只有「对话 → 互动」单向且只挂在对话页输入区；双向化属于 P1-A 的写作用域，P1-D 只给接口与验收要求。

最需要先改的门控是 **frontend/src/App.vue 的 isConversation（第 33 行）以及第 112-118 行的旧审批挂载条件**；但它必须与「互动页挂载唯一 UnifiedApprovalLayer」在同一个提交里原子完成，否则要么互动页暂时没有任何审批入口，要么旧弹窗与统一弹窗同时出现。

---

## 1. ChatDock 与对话页 Composer 的复用/分叉点

### 1.1 结论总表

| 能力 | 对话页 v2（现状） | 互动 ChatDock（现状） | 判定 |
| --- | --- | --- | --- |
| 消息流 / 流式 | MessageStream + MessageItem + 轮次分组 + 虚拟滚动（MessageStream.vue:1-19、55；656） | MessageItem 平铺，只渲染最近 60 条（ChatDock.vue:95、151-154、1211） | 分叉。应迁移到 MessageStream（同时得到 TurnProcess / ContinueBar） |
| 工具过程 / 轮次状态 | TurnProcess（MessageStream.vue:12、592） | 无（ChatDock 的「过程」只表达板面理解，见 ChatDock.vue:1221-1321） | 必须迁移。否则互动聊天里工具记录、内联审批、继续条都不可见 |
| 继续条（预算 / 无进展） | ContinueBar（MessageStream.vue:13、656） | 无 | 必须迁移。否则互动聊天触发预算暂停后没有可操作的 UI |
| 附件 | 完整：选择 / 拖放 / 路径 / 重新上传 / 重新定位 / 准备态 / 中止 / 发送被拒 / 按话题持久化（Composer.vue:294-723、1024-1087、1118-1155） | 只在本组件内存里维护 pendingAttachments（ChatDock.vue:836-845、1401-1513）；路由切换/组件卸载即丢 | 必须迁移。复用 Composer 或 attachments 持久化通道 |
| 媒体 | 媒体能力提示（Composer.vue:4、1458-1460）；消息侧媒体事实行共用 MessageItem | 消息侧共用 MessageItem；没有媒体能力提示（ChatDock 头部注释 ChatDock.vue:10 声称放进了「帮助」，但模板 ChatDock.vue:1169-1189 只渲染标题/状态/尺寸/收起，没有帮助入口） | 低优先级迁移：复用 Composer 的提示即可 |
| 草稿 | session.draft 单一写者（stores/session.ts:6115-6127 明确写 Composer 与 ChatDock 共用；Composer.vue:53-58） | 同一份 session.draft（ChatDock.vue:134-140）；另有保存状态与被让位草稿提示（ChatDock.vue:516-525、1525-1537） | 已复用会话层；差异在承载位置，应统一到输入区入口① |
| 失败恢复 | 入口① SendIssuesPanel（覆盖未决发送、失败原文、中断轮、被让位草稿、存储读写失败） | 共用 FailedSendNotice（ChatDock.vue:31、1354），覆盖未决发送四个动作与失败原文找回/互换/放弃；不含中断轮 | 主要能力已复用；需与 v2 对话页「只在入口①」的口径统一，避免同一条记录两处按钮 |
| 排队 | 入口④ QueuePanel（会话层 turnQueue + 本机乐观消息；QueuePanel.vue:38-51、entries.ts:206-273） | 只有状态文案（ChatDock.vue:145-150），发送按钮标题写「发送（会排队）」（1559），没有检查/取消入口 | 必须迁移。否则用户看不到排队项，也不能取消 |
| 历史继续 | 入口②（ComposerEntries.vue:36-58、174-195；会话层 pendingContinuation / anchorHistoric：stores/session.ts:1593-1598） | 无入口；发送时却会带上接续身份（session 在 send 内捕获，stores/session.ts:5148、5181-5187） | 必须迁移。至少要有「当前从哪继续」与取消入口 |
| 话题切换建议 | 入口③（ComposerEntries.vue:197-224；session.pendingSwitch：stores/session.ts:1809-1811、2681-2724） | 无 | 必须迁移 |
| 停止 | Composer 与发送共用四态按钮（Composer.vue:76-139、1545-1560），stopActiveTurn 精确取消（Composer.vue:1338-1355；stores/session.ts:3658-3668） | 没有停止按钮，只有发送（ChatDock.vue:1542-1564） | P0 必须迁移。互动页现在可以开始一轮却无法在互动页停止 |
| 发送/停止按钮 | 四态：有内容→发送（回复中排队）/ 回复中无内容→停止 / 空闲无内容→禁用 / 有内容但未满足条件→保留发送态并说明原因（Composer.vue:87-139） | 只有两态：可发 / 不可发（ChatDock.vue:141-144、1554-1563） | 随输入区复用一起迁移 |
| 意图审批（互动专有） | 不显示 | ChatDock 内联批准/拒绝（ChatDock.vue:646-665、1284-1319），走 interactive store（stores/interactive.ts:3170-3208） | 保留独立，不并入统一审批（见 §2.6） |
| 意图修改绑定 | 无 | 无入口；needs_update 的意图不能批准（frontend/src/interactive/approval.ts:320-325、1067-1070）；updatePreview API 存在但全仓无消费方（stores/interactive.ts:3231-3235；services/interactive.ts:330-344） | Phase 1 建议明确「不做修改意图」，或只补「需要更新 / 重新理解」的可读入口，不伪造能力 |
| 载体 / 尺寸 / 收起 | 由布局 flex 自然决定 | 悬浮框 + 拖动/八向调整 + 本机偏好 chatFrame（frontend/src/interactive/chatFrame.ts:1-10、35、49-52）；store.chatOpen（stores/interactive.ts:373） | 若新外壳取代悬浮框，需要 Lead 裁决 chatFrame 去留（见风险 R-13） |

### 1.2 逐项证据与分叉原因

#### 1.2.1 消息流与过程：ChatDock 没有复用 MessageStream，因而丢掉了 TurnProcess / ContinueBar

- 对话页：MessageStream 负责轮次分组、虚拟滚动、消息项、过程区、继续条、回到最新（MessageStream.vue:1-19、55、656）。
- 互动：ChatDock 直接渲染 MessageItem 列表，并自己实现跟随滚动、回到最新、尾部截断（ChatDock.vue:95、151-154、1211、1366-1380）。它没有 import TurnProcess、ContinueBar、MessageStream（见 ChatDock.vue:25-90 的导入区）。
- 后果：
  - 互动聊天里工具调用过程不可见；
  - 过程区里的**内联审批**永远不出现（TurnProcess.vue:418-431 只在 current turn 显示内联卡）；
  - 预算耗尽 / 无进展暂停时 ContinueBar 不渲染 —— session.pendingContinue 会被设置（stores/events.ts:762-776），但互动页没有响应入口，只能切回对话页；
  - 消息侧附件/媒体事实行仍可用，因为 MessageItem 是共用的。

**迁移建议**：新互动页的对话区直接复用 MessageStream（放进 P1-B 外壳的 chat 槽），不再维护 ChatDock 的平铺流。前提是核对其底部让位/输入框锚定逻辑在「对话区与输入区分离」的新布局里成立（MessageStream.vue:261-276 的 clearance 逻辑读取 .composer 元素）。

#### 1.2.2 附件：ChatDock 只做了「这一屏」的附件，没有会话级持久化

- 对话页 Composer 的附件链路按话题持久化：挂载时 seed + restore（Composer.vue:930-1008、1061-1087）、切话题先落旧话题再恢复新话题（1022-1040）、发送后失效/tombstone 清理（269-276）、发送被拒的逐条原因与「移除后发送」（141-175）。
- ChatDock 的 pendingAttachments 是组件本地 ref（ChatDock.vue:836-845），只有「文件/路径」两种入口（1401-1449），没有按话题持久化；组件卸载（路由切换会卸载组件）后这批未发送附件丢失。卸载钩子只 flushDraft、不保存附件（ChatDock.vue:801-813）。
- 复用方式有两种：
  1. 让互动输入区直接用 Composer（推荐，见 §4.2、§5），附件能力零成本对齐；
  2. 若短期保留 ChatDock，则必须把 pendingAttachments 接到 services/attachments 的 savePendingAttachments / loadPendingAttachments 通道（Composer.vue:14、26 已 import），并按话题恢复。

#### 1.2.3 媒体：消息侧共用，输入侧缺失

- MessageItem 已负责多媒体读取事实行（MessageItem.vue:339-363、544）；ChatDock 与对话页共用它，所以「已交付/抽样/转写/不支持」这些事实在两边一致。
- 输入侧的能力提示只在 Composer（MEDIA_CAPABILITY_HINT：Composer.vue:4、1458-1460），ChatDock 没有。当前 ChatDock 顶部注释（ChatDock.vue:10）说媒体读取方式放进可打开的帮助，但模板里没有帮助面板（1169-1189），属于注释与实现不一致。
- 结论：媒体不是必须重写的模块；随输入区复用 Composer 即自然获得提示，删除 ChatDock 时不要保留这份过时注释。

#### 1.2.4 草稿：会话层已单写者共享，差异只在提示承载

- stores/session.ts:6115-6127 的注释是硬事实：草稿的读写入口就是 session.draft，Composer.vue 与 ChatDock.vue 都用 v-model 直接读写；不能改成 getter/方法，也不能再建第二份草稿状态。
- ChatDock 额外显示草稿保存状态与被让位旧草稿（ChatDock.vue:516-525、1525-1537）；对话页 v2 把这些收敛到入口①（ComposerEntries.vue:36-58；SendIssuesPanel.vue:510-523、461-507）。
- 结论：草稿本身无需迁移；**承载要统一**。若互动新页同时保留 ChatDock 的草稿提示与入口①，用户会看到同一份失败/被让位信息两处可操作（风险 R-05）。

#### 1.2.5 失败恢复：主要能力已复用，但入口口径不一致

- ChatDock 已经使用共享 FailedSendNotice（ChatDock.vue:31、1354-1358），它包含：
  - 未决发送（受理未知）的查证 / 同身份重试 / 查看原文 / 明确放弃（FailedSendNotice.vue:257-345）；
  - 失败原文的找回 / 互换 / 不再保留（347-420）；
  - 本机存储读写失败（435-489）。
- 对话页 v2 不再在消息流里放 FailedSendNotice（测试注释明确：final-b3-notice-visibility.test.ts:112-113「FailedSendNotice 在 v2 只由悬浮聊天使用」），失败恢复只在入口①。
- ChatDock 不覆盖「上次退出时没有执行的消息」（interruptedTurns / interruptedNotice），这部分只在入口①与 RecoveryInbox。
- 建议：互动新页统一走入口①（ComposerEntries），不再在聊天流里重复渲染 FailedSendNotice；迁移期间若两者并存，必须按记录身份去重，且同一记录只保留一处动作按钮。

#### 1.2.6 排队：会话层已经排队，UI 没有入口

- session.send 在 turnRunning 时会给乐观消息打 queued 标记（stores/session.ts:5142-5157），服务端快照落在 turnQueue（1834）。
- 对话页入口④用 QueuePanel 展示 running / queued / cancelled / local 四种行，并区分「服务端已接受」与「仅本机已提交」（entries.ts:206-273；QueuePanel.vue:53-58、84-105）。
- ChatDock 只把 queuedMessageIds.length 拼进状态文案（ChatDock.vue:145-150），没有列表、没有取消。用户在互动页排队后无法查看/取消，只能切页。
- 结论：必须迁移入口④（QueuePanel + queueRows 纯函数），不要重写一套。

#### 1.2.7 历史继续：入口缺失但发送会生效

- session.captureStartIdentity / pendingContinuation 会在发送时固化「从哪继续」（stores/session.ts:5148、5181-5187；pendingContinuation 状态见 1598）。
- 对话页入口②既解释当前位置（anchorHistoric）又允许取消已登记的接续（ComposerEntries.vue:174-195；cancelPendingContinuation：stores/session.ts:2643-2655）。
- 互动页没有入口：用户可能在对话页设了接续，切到互动后看不见；在互动页也无法取消。**这是「无 UI 但行为已生效」的典型不一致**，必须迁移入口②或至少显示只读接续条 + 取消。

#### 1.2.8 话题切换建议：同样的「无 UI」风险

- pendingSwitch 由事件写入（stores/session.ts:1809-1811、2614-2628），对话页入口③允许「保留当前 / 转到这里」并显示真实结果（ComposerEntries.vue:111-135、197-224）。
- 互动页不显示；如果模型在互动聊天中触发话题切换建议，用户看不到，后续行为却可能受影响。必须迁移入口③或至少在互动聊天流里显示同样的低干扰条（TopicSwitchPrompt.vue:11-23 已是现成的无遮罩组件，但对话页 v2 用的是入口③内嵌版本）。

#### 1.2.9 停止：互动页目前可发不可停（最高优先级）

- 对话页发送/停止共用按钮的四态与 stopActiveTurn 的语义在 Composer.vue:76-139、1338-1355、1545-1560。
- ChatDock 的按钮只有发送（ChatDock.vue:1542-1564），没有停止；session.canStopTurn（stores/session.ts:1919）已经可用，但没有被 ChatDock 使用。
- 后果：在互动页发起一轮后，若模型长时间运行或卡住，用户只能切到对话页停止。这是功能缺口，不是审美问题。
- 迁移建议：不要再给 ChatDock 单独补一个停止按钮；直接复用 Composer 的四态按钮（或抽出共享组合式函数），保证两页语义完全一致，测试复用 ComposerPrimaryButton.test.ts 的断言口径。

#### 1.2.10 意图审批与「意图修改绑定」

这一块和统一审批不是同一件事，必须分开说清：

1. 互动意图审批：ChatDock 里对 understandingIntents 的 pending / needs_update / waiting_dependency / waiting_confirm 显示批准/拒绝（ChatDock.vue:598-602、1284-1319），调用 interactive store 的 approve / reject（stores/interactive.ts:3170-3208），走互动自己的接口。批准前会做 ensureFreshIntents 核验（stores/interactive.ts:1774-1786、3179-3182），不会用旧快照放行。
2. needs_update（需要更新）：状态说明是「不能批准，需要提交并由 QIO 更新预览」（frontend/src/interactive/approval.ts:320-325）；批量默认选择也排除 needs_update（1067-1070）。但 ChatDock 对 needs_update 只显示禁用的批准按钮（ChatDock.vue:1296-1317），**没有「修改意图 / 重新理解 / 告诉用户下一步」的入口**。
3. 意图修改的 API 已经存在但无人使用：services/interactive.ts:330-344 的 updateIntentPreview、stores/interactive.ts:3231-3235 的 updatePreview，全仓没有组件调用（grep updatePreview 只命中 store/service 与测试 mock）。这意味着「修改意图」目前只有后端能力，没有产品入口。
4. 真正接通的「修改后生效」是知识审批：统一弹窗的「修改」→ 编辑 →「按修改后的内容保存」（UnifiedApprovalModal.vue:340-386），通过知识原子接口带 expected_version/content/updated_at（stores/unifiedApprovals.ts:741-826）。它是审批内的内容修改，不是互动意图修改。
5. 结论与建议：
   - Phase 1 不要把互动意图审批并入统一审批队列（身份、接口、状态机都不同；见 §2.6）。
   - 若产品要求「意图修改绑定」，最小诚实实现是：needs_update 时显示「重新理解 / 重新提交后更新预览」的可读说明与入口（重提走 board submission，不伪造 approve），或明确在文案里写「需要重新提交」并给提交区定位；不要复活无消费方的 updatePreview 当作已实现。
   - 无论选哪种，都必须保留「实质范围变化使旧授权失效」的后端核验路径（interactive store 的 ensureFreshIntents + 服务端版本校验），不要在前端缓存里直接改意图后再批准。

#### 1.2.11 互动专有状态：chatFrame / chatOpen 不属于对话能力

- chatFrame 是「本机查看偏好」，按板面身份存 localStorage（chatFrame.ts:1-10、49-52），不写板面状态、不进提交载荷。
- 收起状态 store.chatOpen 在 interactive store（stores/interactive.ts:373），ChatDock 使用它（ChatDock.vue:1150、1152、1574-1585）。
- 若新外壳把「聊天区从底部展开」作为布局（P1-B 的 InteractiveWorkspace 提供 chatOpen prop 与 chat 槽，见 §6.1 的并行观察），则**拖动/八向调整/悬浮尺寸这套交互大概率退役**；但收起/展开状态仍应沿用 store.chatOpen，chatFrame 是否保留需要 Lead 明确（保留会造成「两套尺寸来源」）。

---

## 2. 路由门控与模式切换双向缺口（审批唯一挂载 + 旧入口让位）

### 2.1 现状证据链（审批）

1. 事件 → 审批队列：events.handleApprovalRequired 统一决定「continue 交给 ContinueBar（通过 session.pendingContinue），其余进 approvals.queue」（stores/events.ts:754-787）。
2. 知识候选 → 会话缓冲 → TURN_END 才显示 → 统一层 ingest：events.ts:589-598、stores/session.ts:4598-4611、UnifiedApprovalLayer.vue:53-62。
3. 统一审批层只在对话页挂载：ConversationView.vue:42（import）、536（挂载），其职责包括：
   - 挂载期间把 approvals.inlineSuppressed 置 true（UnifiedApprovalLayer.vue:85-102；卸载置 false：104-111）；
   - 周期刷新知识待确认（30s，34-35、95-97）；
   - 把 approvals.showFull 的显式请求桥接到统一弹窗（131-140）；
   - 渲染 UnifiedApprovalModal 与 MemoryQuestionReminder（150-157）。
4. 旧全局审批在非对话路由继续渲染：App.vue:33（isConversation = route name conversation）、112-115（top-notes 内 ApprovalEntry + DevTaskEntry）、118（ApprovalModal，按 !isConversation && !approvals.inlineClaimed）。
5. 互动页：InteractiveView 的 import（13-25）与模板（120-249）都没有任何审批层、知识候选卡、记忆提醒；因此当前 /interactive 上的审批完全由第 4 步的旧入口/旧弹窗承担。
6. 入口位置：统一层的模板只有弹窗 + 记忆提醒 + 提示（UnifiedApprovalLayer.vue:150-157），**没有入口按钮**；统一入口是 ComposerEntries 的第⑤项（ComposerEntries.vue:26-31、69-78、236-276），通过 unified.open() 打开。
7. 旧入口的计数只覆盖工具审批（ApprovalEntry.vue:34-44 用 approvals.pendingCount），不含知识；统一入口的计数覆盖工具 + 知识（unifiedApprovals.count：stores/unifiedApprovals.ts:459-467；ComposerEntries.vue:57 用 unified.count）。

### 2.2 直接缺口（不改会出什么）

**G1：旧弹窗与统一弹窗并存（P0）。**
若只在互动页新增 UnifiedApprovalLayer、不同时改 App.vue 门控：
- 工具审批到达时，旧 ApprovalModal 因 approvals.visible 为真会显示（ApprovalModal.vue:26-29、172-183）；统一层也可能自动打开同一条（unifiedApprovals.maybeAutoOpen：584-613）。
- 两个遮罩都是 fixed、z-index 200（ApprovalModal.vue:231；UnifiedApprovalModal.vue:395-405），会出现两个 role=dialog aria-modal=true，两套批准/拒绝按钮，同一 approval_id 可被提交两次（第二次通常 404/stale，但用户已经看到「批了又报错」）。
- 反方向也成立：isBlockingModalOpen 把**任何**别的 aria-modal 弹窗视为阻挡（stores/unifiedApprovals.ts:207-217），旧弹窗开着时统一层的自动弹出会被压住（autoPending），于是「统一层明明在，却永远不自动弹」。

**G2：互动页没有知识审批与记忆问题提醒（P0/P1）。**
- 知识候选只在 ChatColumn 渲染（ChatColumn.vue:427-439），互动页没有这份 UI；统一层也没有挂载，所以互动页既没有知识候选卡，也没有统一入口第⑤项。互动聊天产生的知识候选在互动页**完全不可见**。
- 记忆问题提醒由统一层内的 MemoryQuestionReminder 渲染（UnifiedApprovalLayer.vue:155），互动页没有；即使把层挂上，点击提醒后的 open-memory 目标也需要消费方（见 G4）。
- 这条缺口不是「样式差异」，是「有真实待办却没有入口」。

**G3：统一层没有自己的入口（P0，集成顺序陷阱）。**
- 统一层的模板不含入口（见 2.1 第 6 条）；入口在 ComposerEntries 第⑤项。
- 如果 Lead 只给互动页挂 UnifiedApprovalLayer 而互动页还没有 ComposerEntries，则：
  - 工具审批只能靠自动弹出；用户一旦点「稍后处理」或关闭，unified.deferredKeys 会阻止再次自动弹（stores/unifiedApprovals.ts:584-613），而互动页没有任何入口可以手动打开 —— 审批从此不可达。
  - 这不是理论问题：UnifiedApprovalModal 的「稍后处理」就是设计成保留申请并靠入口找回的（UnifiedApprovalModal.vue:326-328、356-358；unifiedApprovals.deferActive:650-673）。
- 因此「唯一挂载」必须和「唯一入口」一起交付。可选：
  1. 互动新输入区同时复用 ComposerEntries（推荐，一次拿到五入口）；
  2. 过渡期复用已经存在但全仓无引用的 UnifiedApprovalEntry.vue（顶部居中入口，见 §2.5），先保证可达，再在 ComposerEntries 落地后删除；
  3. 不要复用旧 ApprovalEntry（它用旧 store 计数，知识不计入，且与「旧入口不并存」目标冲突）。

**G4：记忆提醒的跳转在互动页没有落点（P1）。**
- ConversationView 把 open-memory 接到 PlanetView 的 initialTab/focusPendingId（ConversationView.vue:314-318、536-544）。
- 互动页没有 PlanetView（全仓 PlanetView 只在 ConversationView 挂载；可用 grep PlanetView 验证）。MemoryQuestionReminder 虽然有 window 事件兜底（MemoryQuestionReminder.vue:45-56），但该事件没有任何监听方（全仓 grep qio:open-memory-pending 只有派发处）。
- 直接后果：把统一层原样挂到互动页，用户点「有 N 个问题待回答」不会有任何反应。
- 三种处理（需 Lead 裁决）：
  a. 互动页也挂 PlanetView（与对话页同一套 props）；
  b. 点击时先 router.push("/") 并带一个「打开星球待确认」的跨页请求（需要新通道，当前不存在）；
  c. 互动页暂不渲染记忆提醒（给 UnifiedApprovalLayer 加 prop），并在契约里写明这是已知限制。**不能默认静默无效。**

**G5：关闭统一弹窗没有同步旧 store 的 deferred，跨页可能复现旧弹窗（P1）。**
- UnifiedApprovalModal 的 Esc / 关闭调用 unified.close()（UnifiedApprovalModal.vue:120-125、209-217），close 只写 unified.deferredKeys 并 visible=false（stores/unifiedApprovals.ts:638-648）。
- 旧 ApprovalModal 的显示条件是 approvals.visible = queue.length>0 && !approvals.deferred（stores/approvals.ts:330-333）；unified.close 没有设置 approvals.deferred。
- 因此：用户在对话页关闭（不是点「稍后处理」）一条工具审批 → 去设置页 → App.vue 会挂旧 ApprovalModal（!isConversation）且 approvals.visible 仍为真 → 旧弹窗再次出现。这与「关闭/稍后只隐藏、不因重渲染/轮询自动弹」的契约不符。
- 建议：unified.close 对工具项同步 approvals.defer()（与 deferActive 的 658-661 一致），或统一层接管期间彻底不渲染旧 ApprovalModal（见 §2.3），并把这条写进测试。

**G6：inlineSuppressed 是全局布尔，不是引用计数（P1，低概率）。**
- UnifiedApprovalLayer onMounted 置 true、onBeforeUnmount 置 false（85-111），TurnProcess 直接读它（TurnProcess.vue:420-431）。
- 如果出现两个层实例（例如路由过渡、或未来 App 级 + 页面级同时挂载），任意一个卸载都会把 false 写回，另一个仍在挂载却不生效；内联按钮和统一弹窗会短暂并存。
- 与 G1 的修复绑定：**全应用只允许一个层实例**，最好用路由谓词 + 单一挂载点保证；若做不到，至少把布尔改成计数或在层内校验唯一性。

### 2.3 唯一挂载与旧入口让位方案

**路由谓词（单一事实来源）**
- 在 frontend/src/router.ts 增加并导出：
  - CHAT_ROUTE_NAMES = ["conversation", "interactive"]
  - isChatRoute(name: unknown): boolean
- App.vue、WindowChrome.vue 都改用它；不要再各自写 route.name === conversation（现状：App.vue:33、WindowChrome.vue:46 各写一份）。
- WindowChrome 的同一谓词还决定：52px 全局栏 / 70px titlebar 高度（WindowChrome.vue:33-35、86-107、366-376）与设置入口（WindowChrome.vue:75-78、180-198）。互动页要获得同一套顶栏与设置入口，必须与审批门控用同一个 isChatRoute；这一改动属于 P1-A 的 task-6，P1-D 只要求门控口径一致，不重复实现。
- 保留 App.vue:19-28 的「读不到路由时按非对话页处理」语义：isChatRoute(undefined) === false，这样 approvalInlineGating.test.ts:28-71 的「无 router 挂载 App 仍渲染旧弹窗」不会被破坏。

**App.vue 门控（Lead 所有，必须先改）**
- 第 112-115 行拆成两个条件：
  - ApprovalEntry：只在非聊天路由显示（!isChatRoute）；
  - DevTaskEntry：建议保持所有路由显示（它不是聊天/审批入口；互动页现在也能看到，直接隐藏会让「没做完的开发任务」在互动页消失，属于行为回退）。若产品决定把它移入顶部栏，再另行迁移。
- 第 118 行 ApprovalModal 改为 v-if="!isChatRoute && !approvals.inlineClaimed"。
- 注释要同步改写：现行注释（App.vue:29-32、116-117）只提到「对话页 v2」，必须改成「聊天路由（对话 / 互动）」。
- 绝对不要按 approvals.inlineClaimed 之外的旁路保留旧弹窗；否则 G1 复活。

**统一层挂载（推荐页面级，每个聊天页恰好一个）**
- 方案 A（推荐）：保持 ConversationView.vue:536 不变；在 InteractiveView（由 Lead 集成 P1-B 外壳后）挂载同一个 UnifiedApprovalLayer，并把 open-memory 接到互动页自己的星球落点（或显式禁用提醒）。
  - 优点：open-memory 需要页面本地 PlanetView，页面级绑定最直接；对话页已有模式不推翻。
  - 约束：两个页面都必须有入口（G3）；两个页面都不能再挂第二个层；用一个渲染断言测试锁定。
- 方案 B（备选）：把 UnifiedApprovalLayer 上移到 App.vue，按 isChatRoute 挂载，ConversationView 删除挂载。
  - 优点：唯一挂载点最强，G6 自然消失。
  - 代价：open-memory 需要全局通道（当前不存在），MemoryQuestionReminder 需要 prop 控制；ConversationView 的 PlanetView 绑定要改成事件/短路；改动面更大。
- 无论 A/B，都必须满足：
  1. 每个聊天路由 DOM 上至多一个 UnifiedApprovalModal（可用 .ua-mask 或 role=dialog[aria-modal=true] 计数断言）；
  2. 旧 ApprovalEntry / ApprovalModal 在聊天路由不存在；
  3. 非聊天路由（设置/调试）仍保留旧入口/旧弹窗，因为后台审批仍可能到达（events.handleApprovalRequired 是全局的）；
  4. 入口只有一个：ComposerEntries 第⑤项（首选）或过渡期 UnifiedApprovalEntry（次选），不得与旧 ApprovalEntry 同时出现。

**入口方案选择**
- 首选：互动新输入区复用 ComposerEntries（五入口一次到位，见 §1、§4）。
- 过渡期（如果 ComposerEntries 不能在第一步落地）：把 UnifiedApprovalEntry.vue 挂到互动页顶部，并确保它只依赖 unified store（它已经是这样：UnifiedApprovalEntry.vue:8-31）。它现在是死代码（全仓无 import），正好可以在过渡期利用；ComposerEntries 落地后删除，避免两处入口。
- 严禁：用旧 ApprovalEntry 过渡（计数口径不同、且与目标冲突）。

### 2.4 验收断言（可直接写成测试）

1. 在 /conversation 挂载：存在 UnifiedApprovalLayer；不存在旧 ApprovalEntry；旧 ApprovalModal 不渲染；打开一条工具审批时 DOM 里 role=dialog aria-modal 的弹窗恰好 1 个。
2. 在 /interactive 挂载：同样满足第 1 条；且第⑤入口（或过渡期 UnifiedApprovalEntry）可见并能打开统一弹窗。
3. 在 /settings 挂载：旧 ApprovalEntry / ApprovalModal 仍按现有规则可用；不存在 UnifiedApprovalLayer。
4. 无 router 挂载 App：旧行为不变（approvalInlineGating.test.ts 的两个用例继续通过）。
5. 审批暂缓：在互动页点「稍后处理」后，入口仍可见且能再次打开；不因页面重渲染/轮询自动弹。
6. 知识审批：在互动页产生知识候选事件后，unified.count 增加；入口可见；打开后只有统一弹窗一套按钮。
7. 记忆提醒：互动页点击后要么真的打开星球并定位，要么按显式配置不显示；不允许「显示但点了没反应」。

### 2.5 旧组件处置表

| 组件/文件 | 现状 | 建议 |
| --- | --- | --- |
| components/ApprovalEntry.vue | 非对话路由顶部入口；只计工具审批（34-44） | 保留给非聊天路由；聊天路由让位 |
| components/ApprovalModal.vue | 非对话路由全局弹窗；与统一弹窗同 z-index（231） | 保留给非聊天路由；聊天路由让位 |
| components/approval/UnifiedApprovalEntry.vue | 顶部统一入口；全仓无 import（死代码） | 二选一：过渡期用作互动入口；或删除并等 ComposerEntries 第⑤项 |
| components/conversation/UnifiedApprovalLayer.vue | 统一层；只在 ConversationView 挂载 | 保持唯一；互动页复用同一组件 |
| components/conversation/ComposerEntries.vue | 五入口；第⑤项打开统一弹窗 | 互动新输入区复用（入口唯一来源） |
| components/DevTaskEntry.vue | 顶部开发任务入口；被 App.vue top-notes 一起控制（112-115） | 与审批门控解耦，保持所有路由可见（除非产品另行迁移） |

### 2.6 边界：互动意图审批不并入统一审批

- 统一审批 store 的队列只有 tool（approval_id）与 knowledge（knowledge id）两类（stores/unifiedApprovals.ts:27-29、357-393），提交分派按结构化身份（741-826）。
- 互动意图的身份是 intent id，接口是 interactive 自己的 approve/reject（services/interactive.ts:319-328），状态机包含 needs_update / waiting_dependency / waiting_confirm（interactive/approval.ts 的状态文案）。
- 把意图塞进统一审批会破坏「按结构化字段分派、不猜标题」的契约（conversation-page-contract-20261022.md §4.4，第 167-176 行），也会让 ChatDock/IntentBatchTray 的批量与依赖语义失效。
- 结论：Phase 1 明确保留两套：统一审批只处理工具/权限/凭据与知识保存；互动意图审批留在 ChatDock / IntentBatchTray / IntentWorkbench。二者可以同时出现（工具审批弹窗盖在板面上），但**同一申请只能有一套提交按钮**；在契约里写清这条边界，避免后续「统一」时误迁。

### 2.7 模式切换双向缺口（摘要；完整证据与接口建议见 §4.3）

- 组件只做单向导航：只有 goInteractive（ConversationModeSwitch.vue:26-32），没有 goConversation；菜单永远把「对话模式」标成当前（100-103），aria-label 固定为对话（87）。
- 组件只挂在对话页输入区（ComposerDock.vue:14-35）；互动页只有普通链接「回到对话」（InteractiveView.vue:221），因此互动页既不能表达当前是互动模式，也不能通过同一组件切回对话。
- 写作用域：该文件由 P1-A 的 task-6 负责；P1-D 只要求双向化后的接口与验收（§4.3），Lead 负责在互动新输入区渲染同一个组件。
- 路由门控的总要求不变：切换只导航，不清任务/草稿/附件/审批；「当前模式」由路由决定，不复制到 store。

---

## 3. 保存状态、草稿、冲突、不明受理、记忆问题的现有归属与迁移建议

### 3.1 状态所有权总表

| 状态类别 | 唯一事实来源（store / 字段 / 行号） | 当前 UI 承载 | 迁移建议 |
| --- | --- | --- | --- |
| 板面保存 | interactive store：saveStatus / dirty / saveError / lastSavedAt（stores/interactive.ts:189-193） | InteractiveView 顶部唯一保存状态（InteractiveView.vue:40-60、128-138） | 保留；P1-B 顶栏/Teleport 接管位置，语义不变 |
| 板面提交 / 理解 | interactive store：submitStatus / submitError / understanding / understandingStatus / submitResultUnknown / acceptanceLookupNote（stores/interactive.ts:210-239、259-360） | SubmitCluster + InteractiveView 顶部（InteractiveView.vue:139-178） | 保留；不要和聊天发送状态混用 |
| 卡片草稿 | interactive store：drafts / draftStates / draftLocalStates（stores/interactive.ts:1814-1833） | BoardCard / CardDraftHint | 保留 |
| 卡片草稿冲突 | interactive store：draftConflicts / conflictVersions / conflictFingerprints（stores/interactive.ts:166-187） | BoardCard 冲突入口（CardDraftHint） | 保留；与聊天草稿无关 |
| 布局偏好（对话页） | conversationShell：prefs / loaded / saveError（stores/conversationShell.ts:38-41、90-137） | ChatColumn 全局状态区（ChatColumn.vue:262-269） | 仅对话页；互动不要复用 conversation_layout 存互动面板尺寸（见 chatFrame 先例） |
| 聊天草稿 | session.draft + draftSaveStatus / draftSaveError（stores/session.ts:1609-1611；单写者说明 6115-6127） | ChatDock 状态行（516-525）或入口①（SendIssuesPanel 510-523） | 保留 session 唯一来源；UI 统一到入口①，不要在聊天流里重复 |
| 被让位草稿 | session.displacedDraftsForTopic（getter 2012） | ChatDock 本地块（1525-1537）或入口①（461-507） | 统一到入口①，删除 ChatDock 的独立块（避免两处动作） |
| 发送失败原文 | session.failedSendsForTopic / failedSendErrors（getter 1999） | 入口① + ChatDock 的 FailedSendNotice | 统一到入口①（对话页 v2 已如此；见 final-b3 测试 112-113） |
| 未决发送（受理未知） | session.pendingSendConfirm（getter 1934）、unresolvedSendsForTopic（1954） | 入口① + ChatDock 的 FailedSendNotice | 保留两个动作面的唯一一套；推荐入口① |
| 中断轮（上次没执行） | session.interruptedTurns / interruptedNotice（1553-1571、3195-3209） | 入口① + RecoveryInbox（左栏） | ChatDock 目前没有；互动页若没有左栏，必须把入口①的五类事项完整带上 |
| 排队 | session.turnQueue / queuedMessageIds（1834、1779） | 入口④ QueuePanel | 迁移入口④ |
| 历史继续 | session.pendingContinuation / anchorHistoric（1593-1598） | ChatColumn 话题栏 + 入口② | 迁移入口② |
| 话题建议 | session.pendingSwitch（1809-1811） | 入口③ | 迁移入口③ |
| 审批（工具/知识） | approvals.queue + unifiedApprovals.knowledge / deferredKeys（stores/approvals.ts:301-340；stores/unifiedApprovals.ts:421-445） | 统一弹窗 + 入口⑤（对话页）；旧入口/弹窗（其他页） | 见 §2；唯一层、唯一入口 |
| 记忆问题 | stores/memoryQuestions.ts（83-113）；不是审批 | MemoryQuestionReminder（统一层内） | 见 §2 G4；必须接真实落点或显式关闭 |
| 互动意图 | interactive store：intents / understandingIntents / approve / reject（stores/interactive.ts:243-304、3170-3208） | ChatDock / IntentBatchTray / IntentWorkbench | 保持独立；不并入统一审批 |

### 3.2 冲突不是一种「保存状态」，要分三层说

1. **板面卡片草稿冲突**：版本无法判定新旧时保留两份，由用户明确选择（interactive store 166-187；契约 §12.1）。入口在卡片上，不在聊天输入区。
2. **知识审批的版本/内容冲突**：提交知识批准时服务端用 expected_version / expected_content / expected_updated_at 校验（stores/unifiedApprovals.ts:334-355、788-793），冲突时「重新加载，请再看一遍后确认」（classifyKnowledgeSubmitError：229-294）。它不是草稿冲突。
3. **聊天草稿没有「冲突选择」**，只有：
   - 保存失败（draftSaveStatus=error，用户可重试）；
   - 被让位旧草稿（两份都保留，取回 = 互换，不覆盖）；
   - 未决发送（不能把原文自动放回输入框，避免用新身份绕过幂等）。
   不要把这三件事混成「草稿冲突」，否则会出现不存在的「选服务器 / 选本机」按钮。

### 3.3 不明受理（投递未知）要分聊天与板面两条线

- 聊天发送：session.pendingSendConfirm / unresolvedSendsForTopic；语义是「可能已到后端，也可能没有」，处理动作是查证 / 同身份重试 / 查看原文 / 明确放弃（FailedSendNotice.vue:257-345；SendIssuesPanel.vue:288-365）。**不能**把它显示成失败并自动放回草稿（Composer.vue:233-262 有明确注释）。
- 板面提交：interactive store.submitResultUnknown / submitAcceptedByLookup / acceptanceLookupNote（stores/interactive.ts:212-239、2927-2951）；语义是「POST 没有回执」，处理入口在提交区，绝不自动重复提交。
- 这两条都必须在互动页有真实入口：聊天走入口①，板面走提交区。不要合成一个「网络错误」横幅。

### 3.4 记忆问题不是审批

- stores/memoryQuestions.ts:1-12 明确「不是审批」；它只显示真实数量、加载失败 ≠ 0。
- 统一审批层内的 MemoryQuestionReminder 是提醒，不是队列项（UnifiedApprovalLayer.vue:155）。
- 迁移时必须保持：不放进统一审批队列、不算 pendingCount、不自动弹；加载失败要显示「无法读取」并可重试（MemoryQuestionReminder.vue:18-20、45-49）。

### 3.5 迁移建议（归属不变，只收敛 UI）

- **不要新建 store**：以上所有状态都已有唯一来源，Phase 1-D 只做 UI 复用与门控，不复制状态。
- 聊天相关：统一由 Composer + ComposerEntries 承载（对话/互动同一组件），即「一个输入区、五入口、一套草稿/失败/排队/接续」。
- 板面相关：保存/提交/理解留在 InteractiveView 顶部与 SubmitCluster；卡片草稿与冲突留在卡片。
- 审批：唯一统一层 + 唯一入口⑤。
- 记忆问题：统一层提醒 + 真实星球落点（或显式关闭）。
- 互动意图：保持 ChatDock/IntentBatchTray 现有入口（若 ChatDock 退役，需把这些入口迁到新页面的「意图与结果」入口，不能丢）。

---

## 4. 具体文件、组件、props/事件、store 状态、测试与风险

### 4.1 复用组件接口表（现状，供集成直接对照）

| 组件 | props | emits | 读取的 store | 建议挂载位置 | 注意 |
| --- | --- | --- | --- | --- | --- |
| MessageStream.vue | 无 | 无 | session / approvals | 新互动页 chat 槽 | 虚拟滚动与底部让位按 .composer 测量（261-276），新布局需实测 |
| Composer.vue | 无（需新增互动绑定 prop，见 4.2） | 无 | session / attachments | 新互动页 composer 槽 | 旧契约写明「仅对话页使用」（conversation-page-contract-20261022.md:59），复用需更新契约 |
| ComposerEntries.vue | 无 | 无 | session / approvals / unified | composer 上方 | 第⑤项直接 unified.open（69-78），是统一审批唯一入口 |
| QueuePanel.vue | active? | openSendIssues | session | ComposerEntries 内 | 运行中停止 / 排队取消的文案以「请求已提交」为准（38-51） |
| SendIssuesPanel.vue | 无 | 无 | session | ComposerEntries 内 | 入口①五类事项完整覆盖 |
| FailedSendNotice.vue | scope / notice / reason | 无 | session | 若走入口①则不重复挂 | scope chat/conversation 决定 data-im 前缀（27-37） |
| ContinueBar.vue | 无 | 无 | session | 由 MessageStream 自动渲染（656） | 预算/无进展的继续/停止 |
| ConversationModeSwitch.vue | 无 | 无 | router | 两页的 composer 旁 | 需双向化，见 §2、§4.4 |
| UnifiedApprovalLayer.vue | 无 | open-memory | approvals / session / unified / memoryQuestions | 每个聊天页恰好一个 | 设 inlineSuppressed；不渲染入口 |
| UnifiedApprovalModal.vue | 无 | 无 | unified / approvals | 由 Layer 挂载 | 单实例、按身份分派 |
| KnowledgeCandidateCard.vue | candidate | 无 | session / unified | 对话 ChatColumn 底部；互动可选 | 只打开统一弹窗，不自带提交（KnowledgeCandidateCard.vue:5-7、23-31） |
| MemoryQuestionReminder.vue | 无 | open(target) | memoryQuestions | 由 Layer 挂载 | 互动页必须有消费方或显式关闭 |

### 4.2 Composer 复用需要的接口改动（建议）

现状：Composer 的发送调用是 sendWithAttachments(value, ids, attachments, prep?...)（Composer.vue:224-229），没有 interactive 选项；ChatDock 则显式传 interactive: store.chatBinding（ChatDock.vue:1097-1108；session.send 的 options 定义在 stores/session.ts:5099-5116；chatBinding 在 stores/interactive.ts:204-208）。

如果互动页直接复用 Composer，必须补一个不含业务判断的接口，例如：

    defineProps<{
      chatBinding?: { mode: "interactive" | "normal"; boardId?: string; submissionId?: string };
    }>();

并在 send 调用里把它作为 options.interactive 传下去；默认 undefined → 对话页行为完全不变。禁止在 Composer 内部直接 useInteractiveStore（否则对话页也带板面身份，违反互动契约 §8.3「文字发送只发文字、不调用板面提交接口」）。

同时必须保持：
- 不追加板面、未提交改动、注释或选择范围（interactive-mode-contract.md §8.3，419-425）；
- 附件/文件路径仍只随本次消息发送；
- 发送绑定在点击那一刻固化（session.send 已做，stores/session.ts:5129-5148）。

### 4.3 ConversationModeSwitch 双向缺口（P1-A 写作用域，P1-D 只给要求）

**现状证据**
- current 由 router 计算，能识别 interactive（ConversationModeSwitch.vue:17-20）；
- 但只有 goInteractive，没有 goConversation（26-32）；
- aria-label 固定「当前为对话模式」（87），按钮文字按 current 显示（92），但菜单永远把「对话模式」标成当前（100-103），只提供「互动模式」可点（105-108）；
- 只挂载在 ComposerDock（14-35），互动页没有；互动页只有一句普通链接「回到对话」（InteractiveView.vue:221）。
- 基线没有 ConversationModeSwitch 的测试文件（现有测试清单里没有；P1-A 应补）。

**结果**
- 互动页不能通过同一个组件切回对话，也不能表达「当前是互动模式」；
- 即便把组件硬挂到互动页，菜单会显示错误的当前项，且没有返回动作。

**建议接口（不改业务状态）**
- 增加 goConversation()：router.push({ name: "conversation" })；保留 goInteractive()。
- 菜单两项都按 current 渲染：当前项用 aria-current 标记且不可点；另一项可点。
- aria-label 动态化：切换页面模式（当前为互动模式/对话模式）。
- 可选 prop：compact?: boolean（窄输入区只留图标/短文字），不要引入 v-model 或事件，避免把导航状态复制到 store。
- 关键约束保持不变：切换只导航，不取消任务、不清草稿、不丢审批、不丢附件、不重复提交（组件现有注释 5-9；契约 §6）。

**测试要求（新增）**
- 在 conversation 路由：点击菜单互动项 → push /interactive；当前项标记为对话。
- 在 interactive 路由：点击菜单对话项 → push /；当前项标记为互动；aria-label 正确。
- 切换不调用任何 store 的清理/发送方法（用 spy 断言）。
- Esc / 外部点击收起，不导航。

### 4.4 页面切换时必须保持的状态（两方向都要验）

| 状态 | 事实来源 | 切换后应保持 | 现有风险 |
| --- | --- | --- | --- |
| 聊天草稿 | session.draft（单写者 6115-6127） | 保持，并在离开前 flushDraft（ChatDock.vue:676-684、801-803；Composer 由草稿 keeper 防抖 + 卸载写入） | 若新输入区替换 ChatDock，必须保留离页 flush |
| 待发附件 | Composer 按话题持久化（1024-1087） | 保持 | ChatDock 的本地附件会丢（1.2.2） |
| 聊天展开状态 | store.chatOpen（stores/interactive.ts:373） | 保持 | 新壳用 prop 控制时要接同一个 store 字段 |
| 悬浮框尺寸 | chatFrame（localStorage） | 若保留则保持；若退役则明确不恢复 | 两套尺寸来源会打架（R-13） |
| 进行中/排队轮次 | session.turnRunning / turnQueue | 保持；切换不得取消 | 组件 onMounted 不得重置 |
| 统一审批状态 | unified.visible / deferredKeys | 保持 | 页面卸载不影响 pinia；但 close 的 deferred 语义有 G5 缺陷 |
| inlineSuppressed | approvals.inlineSuppressed | 离开聊天页应为 false，回到聊天页应为 true | 全局布尔，双实例会错（G6） |
| 行星 / onboarding / 恢复层 | 各自 store | 按现有行为 | 互动页没有 PlanetView，open-memory 另计 |

### 4.5 测试清单

**现有定向测试（改动前后都应保持全绿；命令在 frontend 下）**
- 审批门控与统一审批：
  - src/__tests__/approvalInlineGating.test.ts（App 层旧弹窗门控）
  - src/components/approval/__tests__/UnifiedApprovalModal.test.ts
  - src/stores/__tests__/unifiedApprovals.test.ts
  - src/stores/__tests__/approvalRouting.test.ts、approvalInline.test.ts、approvals.test.ts、approvalInvalidation(.verify).test.ts
  - src/components/__tests__/ApprovalModal.test.ts、ApprovalDefer.test.ts、ApprovalFacts(.audit.verify).test.ts
- 对话输入区与五入口：
  - src/components/conversation/__tests__/ChatColumn.test.ts、ComposerEntries.test.ts、ComposerPrimaryButton.test.ts、entries.test.ts
  - src/components/__tests__/Composer.test.ts、ComposerAttachments.test.ts、ComposerAttachmentRejection.test.ts、ComposerContinuationHint.test.ts、ComposerMediaCapabilityHint.test.ts、SendWhileAttachmentPreparing.test.ts、QueueChip.test.ts
- 失败恢复与草稿：
  - src/components/__tests__/FailedSendNotice.test.ts、d6FailedSendNoticeDisplay.test.ts、final-b3-notice-visibility.test.ts
  - src/stores/__tests__/draftPersistence.test.ts、final-b2-chatTopicDrafts.test.ts、p1-draft-promotion.verify.test.ts
- 记忆问题：
  - src/stores/__tests__/memoryQuestions.test.ts、src/components/conversation/__tests__/MemoryQuestionReminder.test.ts
- 视图：
  - src/views/__tests__/ConversationView.test.ts、ConversationViewShell.test.ts、ConversationViewNotices.test.ts、src/__tests__/appBoot.test.ts
- 互动现存聊天与审批可访问性：
  - src/components/interactive/__tests__/irl-a-chatDock.test.ts、irl-d-approval-access.test.ts、chatDockAttachments.test.ts
  - src/components/__tests__/b-chatDockPendingSend.test.ts
  - src/interactive/__tests__/chat.test.ts、approval.test.ts

**必须新增（建议文件）**
1. src/components/conversation/__tests__/ConversationModeSwitch.test.ts：双向导航、当前项、aria、仅导航。
2. src/__tests__/appApprovalRouteGating.test.ts（或扩展现有 approvalInlineGating.test.ts）：conversation / interactive / settings / 无 router 四种路由下旧入口、旧弹窗、统一层的存在性；互动路由恰好一个统一层。
3. src/views/__tests__/InteractiveViewApproval.test.ts：InteractiveView（或 Lead 集成后的新页）挂载后存在 UnifiedApprovalLayer、存在入口⑤/UnifiedApprovalEntry、无旧 ApprovalEntry/ApprovalModal；打开审批时 role=dialog[aria-modal] 计数为 1。
4. src/components/interactive/__tests__/interactiveComposerReuse.test.ts：互动输入区发送时带 interactive 绑定；对话页发送不带；草稿/附件共用 session。
5. src/stores/__tests__/unifiedApprovalsCrossPage.test.ts：close/defer 与 approvals.deferred 的关系（G5）；deferred 后旧弹窗不复现。
6. 可选：src/components/interactive/__tests__/v2-chat-parity.test.ts：停止、排队、历史继续、话题建议四类入口在互动页可见（用新页或组件级挂载）。

**手工验收（真实界面，不改现有截图门槛）**
- 1440×900 / 1024×768 / 800×600 / 480×600；亮/暗/减少动画。
- 对话⇄互动多次往返：草稿、待发附件、排队、审批暂缓、聊天展开状态保持；无重复订阅、无重复消息、无重复审批按钮。
- 互动页：发起长轮 → 停止；回复中发送 → 排队可见/可取消；设置接续 → 入口②可见/可取消；触发话题建议 → 入口③可见；产生知识候选 → 入口⑤计数增加；预算暂停 → ContinueBar 可见。
- 审批：工具审批自动弹、知识审批不自动弹但入口计数、稍后处理后可再打开、切到设置再回来不复现旧弹窗、记忆提醒点击有真实落点。
- 窄窗口：新对话区/输入区不重叠，滚动只有一层纵向视口（契约 §16.2，1182-1189）。

### 4.6 风险登记

| 编号 | 风险 | 触发条件 | 等级 | 缓解 |
| --- | --- | --- | --- | --- |
| R-01 | 旧 ApprovalModal 与统一弹窗并存，双弹窗/双提交 | 只挂新层不改 App.vue 门控 | P0 | §2.3 原子改门控；加 DOM 计数测试 |
| R-02 | 统一层有弹窗无入口，稍后处理后审批不可达 | 只挂层不挂 ComposerEntries/UnifiedApprovalEntry | P0 | 同一步交付入口；测试暂缓后再打开 |
| R-03 | 互动页知识审批不可见 | 不挂层、不迁移知识候选/入口 | P0/P1 | 挂层 + 入口⑤；候选事件 ingest 测试 |
| R-04 | 迁移期间两个输入区同时可发（ChatDock + 新 Composer） | 新页先加 composer、后删 ChatDock | P0 | 同一提交移除一个；或用 store 级开关保证只有一个发送面 |
| R-05 | 同一条失败/被让位草稿在两处可操作 | ChatDock 的 FailedSendNotice/草稿块与入口①并存 | P1 | 统一到入口①；迁移期按记录 id 去重 |
| R-06 | 关闭统一弹窗后切页复现旧弹窗 | unified.close 未同步 approvals.deferred | P1 | close 对工具项 defer，或删旧弹窗在聊天页的挂载；测试覆盖 |
| R-07 | inlineSuppressed 全局布尔被错误清掉 | 双实例或路由过渡 | P1 | 唯一挂载；必要时计数化 |
| R-08 | 记忆提醒显示但点击无反应 | 挂层但互动页无 open-memory 消费方 | P1 | 接 PlanetView / 跨页通道 / 显式关闭三选一 |
| R-09 | 模式切换丢状态或重复提交 | 切换时组件卸载/重建、重复 onMounted | P1 | 仅导航；store 层保持；往返测试 |
| R-10 | ChatDock 退役后 chatFrame 偏好成为孤儿 | 新壳不支持悬浮尺寸但保留 localStorage | P2 | Lead 明确去留；清理或保留兼容读 |
| R-11 | 复用 Composer 丢掉 interactive 绑定 | 只换组件不传 options.interactive | P0/P1 | §4.2 prop；请求体断言测试 |
| R-12 | MessageStream 的底部让位/锚定在新槽位失效 | 输入区与聊天区分离、.composer 位置变化 | P2 | 实测 + 调整 composerMetrics/clearance |
| R-13 | 两套布局来源（chatFrame vs 新壳 chatOpen 高度） | 新壳尺寸 + ChatDock 仍在 | P2 | 单一所有权；嵌入模式禁用 frame |
| R-14 | 把互动意图审批并入统一审批导致语义丢失 | 误「统一」 | P1 | 契约写明边界；不迁 |
| R-15 | DevTaskEntry 被审批门控连带隐藏 | 用同一个 isChatRoute 控制整个 top-notes | P2 | 拆分两个条件 |

---

## 5. 给 Lead 的集成顺序

> 原则：先门控、后能力；同一可见行为必须原子提交；每步都有可回滚点与定向测试。

**Step 0（决策，0.5 天）**
- 冻结三个决策并写入契约（Lead 所有 docs/interactive-v2 或 docs/interactive-mode-contract.md 补节）：
  1. 统一审批层采用页面级（推荐）还是 App 级；
  2. 互动页记忆提醒的落点（PlanetView / 跨页 / 显式关闭）；
  3. 新聊天区复用 MessageStream + Composer（推荐）还是保留 ChatDock（若保留，需要补停止/五入口/附件持久化，工作量更大且会与 P1-B 槽位重复）。
- 与 P1-A 确认 ConversationModeSwitch 双向化由其提交（写作用域 task-6），Lead 只负责在新输入区渲染。

**Step 1（P0，原子提交）路由门控 + 唯一审批层 + 入口**
- 在 router.ts 增加 isChatRoute。
- App.vue：拆分 top-notes；旧 ApprovalEntry/ApprovalModal 只在 !isChatRoute；DevTaskEntry 保持可见。
- InteractiveView（或集成后的新页）：挂载恰好一个 UnifiedApprovalLayer；接 open-memory（或显式关闭提醒）。
- 入口：同一提交内提供互动页入口（优先 ComposerEntries 第⑤项；若输入区尚未迁移，先用 UnifiedApprovalEntry 过渡）。
- 测试：新增 appApprovalRouteGating + InteractiveViewApproval，跑 approvalInlineGating 回归。
- 回滚点：若新页入口未就绪，不要先删旧入口；把「挂层 + 门控 + 入口」作为一个整体回滚单元。

**Step 2（P0）互动输入区复用 Composer（含停止/附件/媒体/排队/接续/话题/审批⑤）**
- Composer 增加互动绑定 prop（§4.2）；互动 composer 槽放 ComposerEntries + Composer + ConversationModeSwitch。
- 保证只有一个输入区（移除该页面的 ChatDock 或隐藏其 composer）。
- 测试：interactiveComposerReuse（绑定）、ComposerPrimaryButton、ComposerAttachments、ComposerEntries 回归。
- 回滚点：绑定默认 undefined，不影响对话页；若互动发送异常，可临时退回 ChatDock 但必须隐藏新 composer，绝不允许两个同时可发。

**Step 3（P1）聊天区复用 MessageStream（得到过程/工具/ContinueBar）**
- chat 槽放 MessageStream；确认底部让位/回到底部/阅读位置在新布局成立。
- 移除 ChatDock 时同步移除其 toggle/尺寸逻辑；chatOpen 接到新壳展开状态。
- 测试：MessageStream 既有用例 + 互动页手工验收；出现两套消息渲染前不要发布。

**Step 4（P1）对话⇄互动双向切换**
- P1-A 提交双向化后，在两页 composer 旁渲染同一组件；验证状态保持清单（§4.4）。
- 测试：ConversationModeSwitch.test.ts + 往返手工验收。

**Step 5（P1）记忆问题与旧入口清理**
- 接真实落点或显式关闭提醒；删除 UnifiedApprovalEntry（若已由 ComposerEntries 取代）；更新 App.vue 注释与契约。
- 删除 dead code 前先 grep 确认无 import（现状：UnifiedApprovalEntry 无 import）。

**Step 6（收尾）契约与文档**
- 更新 docs/interactive-mode-contract.md（新增本轮门控/复用/边界条目）、docs/conversation-page-contract-20261022.md 的 Composer 使用范围（若复用）或新契约；
- 更新 docs/status.md 的里程碑与已知限制（互动聊天仍未做的：意图修改、媒体提示若未迁、chatFrame 取舍）；
- 运行 python scripts/check_docs.py（若改 status/架构文档）。

**验证命令（每个 Step 的定向）**
- 前端：cd frontend; npx vitest run <相关 spec>；npx vue-tsc --noEmit。
- 后端：本审计不涉及后端改动，无需 pytest；若 Lead 改了契约涉及后端，再按 AGENTS.md 跑定向。
- 完整全量（npm test / 完整 CI）只在 Lead 集成完成后跑一次（AGENTS.md 验证纪律）。

---

## 6. 附录：引用索引（便于复核）

| 结论 | 证据位置 |
| --- | --- |
| 草稿唯一写者、Composer 与 ChatDock 共用 session.draft | stores/session.ts:6115-6127；Composer.vue:53-58；ChatDock.vue:134-140 |
| ChatDock 只保留 60 条消息、平铺 MessageItem | ChatDock.vue:95、151-154、1211 |
| 对话页有 TurnProcess / ContinueBar / 虚拟滚动 | MessageStream.vue:1-19、12-13、656 |
| ChatDock 没有停止按钮 | ChatDock.vue:1542-1564；session.canStopTurn：stores/session.ts:1919 |
| 对话页发送/停止四态 | Composer.vue:76-139、1545-1560；stopActiveTurn：1338-1355 |
| ChatDock 附件是组件本地状态、卸载不保存 | ChatDock.vue:836-845、801-813 |
| 对话页附件按话题持久化/恢复 | Composer.vue:930-1008、1022-1040、1061-1087 |
| 对话页五入口 | ComposerEntries.vue:26-31、36-58、69-78、174-224、236-276 |
| 排队纯逻辑 | entries.ts:184-273；QueuePanel.vue:38-51 |
| 历史继续/话题建议状态 | stores/session.ts:1593-1598、1809-1811、2643-2730 |
| 失败恢复共享组件 | FailedSendNotice.vue:257-345、347-420；ChatDock.vue:31、1354 |
| 对话页 v2 不再在流内渲染 FailedSendNotice | components/__tests__/final-b3-notice-visibility.test.ts:112-113 |
| 旧审批只在非对话路由 | App.vue:33、112-118；ApprovalEntry.vue:34-44；ApprovalModal.vue:26-29 |
| 统一层挂载与职责 | ConversationView.vue:42、536；UnifiedApprovalLayer.vue:85-111、131-157 |
| 统一层没有入口、入口在 ComposerEntries ⑤ | UnifiedApprovalLayer.vue:150-157；ComposerEntries.vue:69-78、236-276 |
| 统一 store 队列/自动打开/暂缓 | stores/unifiedApprovals.ts:357-393、421-481、584-613、638-673 |
| 旧弹窗与统一弹窗同层 | ApprovalModal.vue:231；UnifiedApprovalModal.vue:395-405 |
| isBlockingModalOpen 把任何 aria-modal 视为阻挡 | stores/unifiedApprovals.ts:207-217 |
| inlineSuppressed | stores/approvals.ts:324-339；TurnProcess.vue:418-431；UnifiedApprovalLayer.vue:85-111 |
| 互动页没有审批层/知识卡/记忆提醒 | InteractiveView.vue:13-25、120-249；ChatColumn.vue:427-439 |
| 知识候选只打开统一弹窗 | KnowledgeCandidateCard.vue:5-7、23-31 |
| 记忆提醒需要页面消费 open | UnifiedApprovalLayer.vue:155；MemoryQuestionReminder.vue:13-16、45-56；ConversationView.vue:314-318、536-544 |
| 互动页没有 PlanetView | ConversationView.vue:43、54-58、537-544（全仓唯一挂载点） |
| 模式切换单向、只挂对话页 | ConversationModeSwitch.vue:17-20、26-32、87、100-108；ComposerDock.vue:14-35；InteractiveView.vue:221 |
| 互动意图审批独立 | ChatDock.vue:598-602、646-665、1284-1319；stores/interactive.ts:3170-3208 |
| needs_update 不可批准 | interactive/approval.ts:320-325、1067-1070 |
| 意图修改 API 无消费方 | services/interactive.ts:330-344；stores/interactive.ts:3231-3235 |
| 知识修改后保存 | UnifiedApprovalModal.vue:340-386；stores/unifiedApprovals.ts:741-826 |
| 互动发送带绑定 | ChatDock.vue:1097-1108；stores/session.ts:5099-5116；stores/interactive.ts:204-208 |
| 旧契约写 Composer 仅对话页使用 | docs/conversation-page-contract-20261022.md:57-61 |
| chatFrame 是本机偏好 | interactive/chatFrame.ts:1-10、35、49-52 |
| 互动意图/板面提交状态 | stores/interactive.ts:189-239、2814-2951 |

### 6.1 并行任务接口假设（非本分支事实，集成前需与 P1-B 复核）

team task-7 要求 P1-B 只新增 frontend/src/components/interactive/v2/**，可用真实 store/现有 ChatDock/BoardToolbar 组合，不得改 InteractiveView。审计时其工作树中的 InteractiveWorkspace.vue 头部声明了 props bookmarksOpen / chatOpen 与 slots board / bookmarks / chat / toolbar / composer（观察自 D:/qio-dev/qio-i2-p1-b/frontend/src/components/interactive/v2/InteractiveWorkspace.vue:13-18，该分支仍在进行、接口可能变化）。

本审计据此给出两个集成映射，Lead 以 P1-B 最终提交为准：
- 推荐：chat 槽 = MessageStream，composer 槽 = ComposerEntries + Composer + ConversationModeSwitch，toolbar 槽 = BoardToolbar，chatOpen 接 store.chatOpen；
- 备选：chat 槽 = 现有 ChatDock（需先剥离其 composer/尺寸交互，避免与新 composer 双输入区），但功能缺口（§1）仍需补齐，工作量更大。

### 6.2 本次审计未覆盖 / 需另行确认

- 未运行完整测试与真实界面截图：本任务只读、只写文档；所有结论来自代码与既有测试证据。
- 未检查 P1-B 的最终样式与槽位行为；未检查 P1-A 的双向切换实现（其分支仍在进行）。
- 未评估 Tauri 窗口层与新的互动顶栏 Teleport 的点击/拖拽命中；属于 P1-A / Lead 集成时的真实界面验证项。
- 未把「意图修改」定为实现范围；该项需要产品决策，本审计只给出边界与现状证据。
