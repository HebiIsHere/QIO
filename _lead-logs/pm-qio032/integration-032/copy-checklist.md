# QIO 0.3.2 集成轮 · 文案核对与删减（第五节）最终核对表

> 角色：文案核对与删减负责人（第五节）。基线：`7445170d`；集成 worktree：`D:\qio-dev\pm-qio032-integration`（分支 `fix/pm-qio032-integration`；工作期间 HEAD 由其他成员从 `e324e392` 推进到 `66bd9636`，最终验证在 `66bd9636` 上跑）。
> 取证时间：2026-11-13 23:57 – 2026-11-14 00:45（Asia/Shanghai）。**未切分支、未 commit、未 push、未 stash、未 reset**，改动全部留在工作树。
> 口径：以用户定稿（本任务原文）为准；定稿原文缺失的设置项（T01–T20 只有名目），按「用户能否知道当前状态 / 实际影响 / 下一步动作」判断。
> 本文件：页面与触发条件 | 改动前 | 改动后 | 删减或保留的理由。

## 0. 结论摘要

- 实际改动 **14 个前端源文件 / 22 处文案**（其中 1 处是读屏名称 aria-label，其余为可见文字）+ **6 个既有测试文件**的断言同步更新；另 **11 项**（6 待判 + 5 部分）判定为已达标不改；**13 项**需要转交其它 owner（AddMenu / InteractiveView / stores/interactive / 设置搜索 / BoardCanvas 常量建议）。
- **静态边界守卫已复核**：后端 `tests/test_p3_d_static_boundaries.py`、`tests/test_p2_d_static_boundaries.py` 全绿（23 passed，EXITCODE=0）。删文案时保留了守卫要的 token：IntentLayer.vue 里仍有一句短版「确认后由服务端续作。」（删掉的是「（本轮不代表已完成）」这类判据解释，不是把事实句删没）。
- 3 个点名发现已处理：① CardInlinePreview.vue 的重复「只读」；② 6 待判 + 5 部分逐条裁决；③ 表 B 中**用户可见且属判据解释/同义复述**的句子。
- 未处理：任务要求的「约 60 处」中缺逐条原文的设置 20 项无法逐字追证（沿用 catalog 结论）；界面截图 / 真机视觉复核未做（本节点无浏览器运行条件，仅静态核对 + 定向单测）；未跑全量 `npm test`（按纪律）。

## 1. 表 1 · 本节点实际改动（逐条）

| # | 页面与触发条件 | 改动前（原文） | 改动后 | 删减 / 保留的理由 | 位置 |
| --- | --- | --- | --- | --- | --- |
| 1 | 互动页 · 文件卡内预览（Markdown） | `Markdown · 共 N 行 · 编码 UTF-8 · 已解析为正文 · 只读` | `Markdown · 共 N 行 · 编码 UTF-8 · 已解析为正文` | 跨组遗漏：与 shell 组文案 1 同款模板；定稿「预览只读只在必要位置显示一次」。对话侧「一次」是标题徽标（PreviewPanel.vue:233），卡片内预览由「展开预览」进入且没有编辑入口，窄卡片不宜再加徽标，故删正文附注 | CardInlinePreview.vue:759 |
| 2 | 互动页 · 文件卡内预览（代码 / 文本） | `Python · 共 N 行 · 编码 UTF-8 · 只读` | `Python · 共 N 行 · 编码 UTF-8` | 同上（同一模板另一分支，避免两个渲染器口径不一致） | CardInlinePreview.vue:767 |
| 3 | 互动页 · 卡片编辑态上传附件（浏览器字节上传） | 「正在保存附件「X」…（首次落盘为工作区材料源文件）」 | 「正在保存附件「X」…」 | 常态进度不带来源附注；来源在成功行由 attachmentSourceNote 说一次（「已保存为工作区材料源文件（来源：浏览器上传）」），且板面关联说明保留 | BoardCard.vue:1070-1072 |
| 4 | 互动页 · 虚拟意图卡片（常态） | 「确认前不执行；确认后由服务端续作（本轮不代表已完成）。」 | 「确认后由服务端续作。」 | 删推测性的「（本轮不代表已完成）」与层图例（:527-529）已说过的「确认前不执行真实板面改动」；保留的「确认后由服务端续作」是「确认后会发生什么」的事实，也是后端静态边界守卫（test_p3_d_static_boundaries）要求的 .vue token，**没有恢复长解释** | IntentLayer.vue:595-596 |
| 5 | 互动页 · 意图详情（可确认时） | 「确认只提交这一条（显式 id），确认后以服务端返回的执行轮次与状态为准；已确认不等于已完成。」 | 删除（保留进度行 intent-detail-progress） | 复述按钮「确认这一条」；「显式 id」是实现细节；「以服务端…为准」是判据解释；实际进度由 intentProgressText 承担 | IntentLayer.vue（原 :648-650） |
| 6 | 互动页 · 确认结果面板（部分完成 / 确认未完成） | 同一句 `summary.notice` 同时出现在层底 role=status 行与结果面板内 | 只在层底 role=status 行显示一次；面板只留标题 / 成功项 / 逐项行 / 失败项 / 重试入口 | 同屏同句两处（底左 role=status、底右结果面板），删重复；读屏播报保留（role=status 仍在） | IntentLayer.vue:549 / :696-704 |
| 7 | 互动页 · 确认结果说明（部分失败） | 「部分失败：<ids> 没有确认成功，可逐项重试或查看原因。」 | 「部分失败：<ids> 没有确认成功。」 | 重试按钮就在同一面板（「重试失败项（N）」），「点哪里」属闲置引导 | intentDisplay.ts:600 |
| 8 | 互动页 · 确认结果说明（已确认未完成） | 「已确认不等于已完成：执行结果以服务端状态、账本与结果卡为准。」 | 删除（逐项状态 + 卡片实际进度已区分「已确认 / 已完成 / 未开始」） | 解释软件如何判断、以及「不等于」式防御句；状态本身没有合并，仍分开显示 | intentDisplay.ts（原 :620） |
| 9 | 互动页 · 提交详情 · 上次结果读取失败 | 「上次结果读取失败：<原因>（这不代表没有上次结果）」 | 「上次结果读取失败：<原因>」+「重试读取」 | 三态已分开（error-first / error-stale / ok-empty），失败行 + 重试按钮足够；括注是「失败≠空」防御句 | SubmitCluster.vue:1307 |
| 10 | 互动页 · 提交详情 · 本次改动读取失败 | 「本次改动读取失败：<原因>（这不代表本次没有改动）」 | 「本次改动读取失败：<原因>」+「重试读取」 | 同上（有效空态另走 ok-empty 分支） | SubmitCluster.vue:1330 |
| 11 | 对话页 · 历史读取失败的空状态 | 「这不代表没有消息；可在上方状态条里重试」 | 「可在上方状态条里重试」 | 标题「历史没有读取出来」已说明失败；空对话引导只在成功且真的为空时出现；保留下一步动作 | MessageStream.vue:1294 |
| 12 | 对话页 · 发送异常面板 · 失败原文读取失败 | 「读取失败：<原因>（这不代表没有失败原文）」 | 「读取失败：<原因>」 | 同款防御句；重试按钮已在 | SendIssuesPanel.vue:550 |
| 13 | 互动页 · 任务面板 · 取消任务按钮的读屏名称 | `aria-label="取消这条互动任务（与停止当前聊天不是同一件事）"` | `aria-label="取消这条互动任务"` | 图标/文字按钮的读屏名称要短而准；「任务 ≠ 聊天停止」由分区与按钮位置区分 | taskCenter.ts:288 |
| 14 | 对话页 · 未完成事项 · 禁用动作的悬停 | `v-qio-tip="action.reason"`（每个禁用按钮各自重复同一条原因） | 删除悬停（保留按钮禁用、aria-label 与下方按原因分组的 .blocked 行） | 文案 7 定稿：多个动作共用的禁用原因只显示一次；悬停把同一句再抄一遍属同义复述（分组行可见、可读屏） | RecoveryInbox.vue:463-464 |
| 15 | 互动页 · 提交查证（确认已可靠受理） | 「…已按**同一个请求身份**查证，服务端确认…」 | 「…已按同一个请求身份查证，服务端确认…」 | 该串在 SubmitCluster 里按纯文本渲染（`{{ note.text }}`），`**` 会原样显示；不是 markdown | submission.ts:787 |
| 16 | 互动页 · 提交查证（查证无记录） | 「…结果仍然未知：这次提交可能已经受理，也不排除没有到达服务端；改动与勾选都保留，不会自动重复提交。」 | 「…结果仍然未知：这次提交可能已经受理；改动与勾选都保留，不会自动重复提交。」 | 用户点名要去掉的推测句（「也不排除没有到达服务端」）；真实状态与下一步（保留、不自动重复提交）保留 | submission.ts:797 |
| 17 | 互动页 · 执行记录 · 操作读取失败 / 账本不可用 | 「读取失败不等于没有操作记录，请重试。」「账本不可用不等于没有操作记录。」 | 删除该两句（保留错误原因 +「重试读取操作记录」） | 失败 / 不可用 / 空态（「没有已记录的操作。」）三分支各自显示，判据解释多余 | ExecutionRecordPanel.vue:476-485 |
| 18 | 互动页 · 执行记录 · 结果读取失败 / 结果不可用 | 「读取失败不等于没有成果，不能当作「没有结果」。」「结果不可用不等于没有成果。」 | 删除该两句（保留原因 +「重试读取结果」） | 同上（空态「没有已保存的结果卡。」另分支） | ExecutionRecordPanel.vue:496-506 |
| 19 | 互动页 · 执行记录 · 操作行 | `修改卡片 · 已生效（key_xxx）` | `修改卡片 · 已生效` | 用户点名「移除执行记录主界面上的调试字段」；operation_key 是内部键，原始条目仍在「诊断（原始标识与结构数据）」里 | ExecutionRecordPanel.vue:487-490 |
| 20 | 对话页 · 话题栏历史读取失败状态条 | 「历史记录暂时无法读取（已经加载的内容仍保留，不代表没有历史）」 | 「历史记录暂时无法读取（已经加载的内容仍保留）」+「重试」 | 状态 + 已保留内容 + 动作已是全部决定信息；「不代表没有」是判据解释 | ChatColumn.vue:186 |
| 21 | 互动页 · 恢复面板 · 恢复能力不可用 | 「服务端的意图恢复能力当前不可用：<原因>（不代表没有需要恢复的事项）」 | 「服务端的意图恢复能力当前不可用：<原因>」 | 空态「暂无可恢复的任务」是另一分支；不可用行自身就是事实 | RecoveryPanel.vue:241 |
| 22 | 星球 / 对话页 · 记忆问题读取失败 | 「维护事项存储不可用：读不到记忆问题，不代表没有问题。」「没能读到记忆整理问题：<原因>（这不代表没有问题）」 | 「维护事项存储不可用：读不到记忆问题。」「没能读到记忆整理问题：<原因>」 | error 状态本身说明读不到；成功且为空是另一条路径 | memoryQuestions.ts:133 / :144 |

测试同步（文案改动带来的断言更新，均先看到红再改绿）：`interactive/__tests__/CardInlinePreview.test.ts`、`interactive/__tests__/d-execution-record-switch.test.ts`、`interactive/v2/__tests__/p3a-intent-layer.test.ts`、`interactive/v2/__tests__/fu2c-confirm-failure-reason.test.ts`、`conversation/__tests__/ChatColumn.test.ts`、`interactive/__tests__/p3c-retry-recovery.test.ts`。

## 2. 表 2 · catalog 的 6 个「待判」+ 5 个「部分」裁决

| 编号 | 现状取证（集成快照） | 裁决 | 理由 / 是否改动 |
| --- | --- | --- | --- |
| I05 任务入口悬停 | InteractiveView.vue:134 `tasksEntryTip` = 「互动任务」/「互动任务（读取失败）」；:486-487 同值 title + v-qio-tip | **已达标，不改** | 定稿「任务入口悬停只写「互动任务」」已逐字落地；失败态补「（读取失败）」是真实状态，不是同义复述。该文件属他人范围，未动 |
| I10 部分完成 | partialConfirmView（intentDisplay.ts:410-432）给标题「部分完成 / 确认未完成」；IntentLayer.vue:696-742 渲染标题 + 成功项 + 逐项行 + 失败项 +「重试失败项（N）」 | **已达标，只做去重** | 四段（标题/成功/失败/重试）齐备；本次只删面板里与 role=status 重复的那句 notice、以及「可逐项重试或查看原因」这类指路句（表 1 #6 #7） |
| I14 已确认意图进度 | intentProgressText（intentDisplay.ts:379-396）输出「进度：已完成 X / 共 N 项 …」；卡片 intent-progress（:604）与详情 intent-detail-progress（:646）都渲染；拦截原因只在真的被拦（confirmBlockReason / cardBlockText :398-408）时出现 | **已达标；删三处判据句** | 「显示实际进度」与「只有真被拦截才提示原因」都已成立；本次删掉「已确认不等于已完成」的两处正文字（#8、#5）与卡片附注（#4）。保留 recoveryResultText（intentDisplay.ts:733）里的「（不代表已完成）」——恢复受理没有结构化状态块承接，属「可区分状态仍要区分」 |
| T01 嵌入模型行 | ToolsSection.vue:219/222-225（标签「嵌入模型」+「使用当前配置」）；UsageAssignmentSection.vue:321/331/449-451（「嵌入模型使用 QIO 当前内置配置…」，`showRowNote` :335-340 不给嵌入行重复 note） | **已达标（非逐字），不改** | 定稿是「嵌入模型 · 使用当前配置」+「限制说明只在用途分配里写一次」；现实现语义等价（标签与状态两个内联 span），不为凑字面重写。遗留 searchIndex.ts:53 与现行为矛盾 → 转交 |
| T02 第三方声明入口 | AboutSection.vue:50-60：标题「第三方开源组件」+ 入口「查看第三方开源声明」；旧「（生成物，请勿手工编辑）」已不在；说明写清分发内容与可离线查阅 | **已达标，不改** | 定稿只给名目「开源声明入口」；现入口短、无编辑指引；分发范围/离线可查属合规事实，保留 |
| T05 连接概览实际地址 | ConnectionCard.vue:212-217「实际连接目标」= 真实 endpoint（mono）+「（OpenAI 兼容接口 · 服务商）」+ 一句「只有你保存并确认后，QIO 才会用这把 Key 向这个地址获取模型或做验证；不会自动试别的厂商」 | **已达标，不改** | 定稿名目是「连接概览实际地址」：现在真的显示实际地址，并说明向该地址发送的时机（决定相关：Key 去哪）；「不会自动试别的厂商」是用户把 Key 交给 QIO 前的信任事实，保留 |
| T08 博查常态与留空说明 | SearchConfigGroup.vue:506 状态「已配置 / 未配置」；:500-502 输入框旁「留空保存不会改动已保存的 Key；要移除请点「清除」。」 | **已达标，不改** | 旧句「已配置（留空保存不会改动它；要移除必须点「清除」）」被拆成状态 + 决定位置的一句说明，符合「在作决定的位置讲一次」 |
| T09 生效模型列表共同条件 | UsageAssignmentSection.vue:445 分组 summary「不单独生效的用途与历史配置（按需展开）」；:446-448 共同条件写一次；:449-451 嵌入说明；`sharedNotes`（:299-311）只在同一句出现 ≥2 次时上提 | **已达标，不改** | 定稿要点是「共同条件集中一次」；实现已按组级说明 + 行级只留差异，且不再给嵌入行重复 note |
| T11 整理间隔 | MemorySection.vue:358-374：title「整理间隔」+ desc「两次后台整理之间的间隔」+ QNumber `label="整理间隔（小时）"` `unit="小时"` `min=1 max=720` | **已达标，不改** | 定稿名目为「整理间隔」；字段含义 + 单位在场，范围由控件 clamp 收口（1–720），没有写错范围。遗留 searchIndex.ts:270 旧句 → 转交 |
| T16 记忆分段保存条 | MemorySection.vue:434-444 GroupSaveBar 只在 `dirty || saving || error` 时出现；:85-88 保存成功一句「记忆分段已保存：3 轮 · 单段 8000」；:91-94 取消一句 | **已达标，不改** | 定稿「无改动时收起多余保存提示，编辑时显示未保存和操作」已落地；常驻「已保存」条不再存在 |
| S07 禁用原因 | RecoveryInbox.vue:250-265 `blockedGroups` 按原因分组；:495-501 每个原因一行「动作1、动作2：原因」 | **已达标；补删一处悬停** | 同因只显示一次已实现；唯一残留是禁用按钮上重复同一原因的 v-qio-tip（表 1 #14），已删 |

## 3. 表 3 · 表 B 可疑措辞逐条判定（仅「用户可见」）

判定列：**删 / 缩 = 本节点已改**；**留 = 有决定价值保留**；**不动 = 注释/日志/诊断/内部代码**；**转交 = 他人文件**。

| 位置（集成快照） | 原文（节选） | 判定 | 依据 |
| --- | --- | --- | --- |
| interactive/CardInlinePreview.vue:759 / :767 | …已解析为正文 · 只读 | **删（缩）** | 见表 1 #1 #2 |
| interactive/v2/IntentLayer.vue:595 | 确认前不执行；确认后由服务端续作（本轮不代表已完成） | **缩（保留短版事实句）** | 删推测性的「（本轮不代表已完成）」与判据解释，保留「确认后由服务端续作。」：静态边界守卫要求 .vue 里有「续作」token（#4） |
| interactive/v2/IntentLayer.vue:649 | 确认只提交这一条（显式 id）…已确认不等于已完成 | **删** | #5 |
| interactive/v2/intentDisplay.ts:620 | 已确认不等于已完成：执行结果以服务端状态、账本与结果卡为准 | **删** | #8（判据解释） |
| interactive/v2/intentDisplay.ts:599 | 部分失败：ids 没有确认成功，可逐项重试或查看原因 | **缩** | #7（重试按钮在旁） |
| interactive/SubmitCluster.vue:1307 / :1330 | …（这不代表没有上次结果）/（这不代表本次没有改动） | **删（括注）** | #9 #10 |
| interactive/SubmitCluster.vue:1108 | 提交结果待确认，改动已保留 | 留 | 定稿原文本身；主区只此一处状态 |
| interactive/v2/intentDisplay.ts:733 | 恢复已受理：当前状态「X」；不会自动执行操作，后续由服务端续作（不代表已完成） | 留 | 恢复受理 ≠ 已完成，且此处没有结构化状态块承接（状态区分） |
| interactive/v2/taskCenter.ts:287 | 取消这条互动任务（与停止当前聊天不是同一件事） | **删（缩）** | #13 |
| interactive/ExecutionRecordPanel.vue:478/483/500/505 | …不等于没有操作记录 / 不等于没有成果 | **删** | #17 #18 |
| interactive/ExecutionRecordPanel.vue:489 | （operation_key） | **删** | #19（调试字段移入现有诊断） |
| interactive/ExecutionRecordPanel.vue:477/483/499/505 错误原因 | 读取失败：<真实原因> / 账本暂时不可用：<原因> | 留 | 真实状态 + 重试入口 |
| interactive/BoardCard.vue:1070 | …（首次落盘为工作区材料源文件） | **删** | #3 |
| interactive/BoardCanvas.vue:1654/2534/2566；BoardGroupFrame.vue:95/438；BoardPreviewLayer.vue:148；SelectionMenu.vue:303/406 | 普通组：摆放顺序不代表先后 | 留（建议收常量） | 组顺序会改变用户对执行次序的理解（决定相关）；跨 7 处重复属维护成本，文案本身不删。文件属他人 → 转交建议 |
| interactive/BoardCanvas.vue:3022；v2/InteractiveConversation.vue:498；SubmitCluster.vue:7 | 打开编辑器不等于修改 / 收起不等于清空 / 「已保存」不等于「QIO 已收到」 | 不动 | 源码注释（不渲染） |
| interactive/CardInlinePreview.vue:111 | 预览不等于 QIO 已读到图片内容 | 留 | 区分「能看图」与「模型读到图」，会改变用户行为（决定相关） |
| interactive/approval.ts:460；stores/interactive.ts:4192 | 受理不等于理解完成，也不等于批准执行 | 留 | 三种状态必须区分（受理/理解/批准），不是判据解释 |
| interactive/approval.ts:594 | 拿到文件名或地址不等于读到正文 | 留 | 真实能力边界，影响用户判断「材料是否可用」 |
| interactive/PendingItems.vue:708 | 服务端一次只返回一部分，列表可能不是全部 | 留 | 截断是真实限制，用户据此知道「列表不全」；句子短、无判据解释 |
| interactive/RecoveryPanel.vue:241 | （不代表没有需要恢复的事项） | **删** | #21 |
| views/InteractiveView.vue:581 | 恢复事项读取失败：…（这不代表没有事项） | **转交** | 文件属他人（见 §4） |
| views/InteractiveView.vue:560 | 首次读取失败：这不代表板面是空的。失败期间不会对旧板面发起编辑或提交 | **转交** | 前半句删、后半句（真实影响）留 |
| components/MessageStream.vue:1294 | 这不代表没有消息；可在上方状态条里重试 | **删（缩）** | #11 |
| components/conversation/SendIssuesPanel.vue:550 | （这不代表没有失败原文） | **删（括注）** | #12 |
| components/conversation/ChatColumn.vue:186 | …不代表没有历史 | **删（缩）** | #20 |
| components/conversation/ChatColumn.vue:195 | 断线后的状态同步没有完成：当前显示可能不是最新 | 留 | 真实状态（数据新鲜度），决定用户是否要刷新 |
| stores/memoryQuestions.ts:133 / :144 | …不代表没有问题 /（这不代表没有问题） | **删（缩）** | #22 |
| components/approval/TopicApprovalContent.vue:362 | 选择「在当前话题回答」不会执行这次申请，也不代表你拒绝过 | 留 | 决定相关：这一选择既不是执行也不是拒绝，会改变用户对结果的预期 |
| components/approval/ApprovalResultsPanel.vue:46/59 | …这不是执行成功。（不等于已完成） | 留 | 已批准 ≠ 已完成，属状态区分；行内已有真实状态与历史入口 |
| components/planet/MaintenanceRestoreDialog.vue:235 | 你后来做过的修改不会被覆盖——受影响的那几项会被拒绝并列出原因 | 留 | 不可撤销 / 数据安全事实 |
| components/planet/EntityCandidates.vue:114-115 | 你手动写过这个字段，自动结果不会覆盖它 | 留 | 解释候选为何没被应用（决定用户是否手改） |
| components/models/ConnectionCard.vue:286 | 停用后不会自动选中这条连接；记录与账本保留 | 留 | 数据保留 + 自动选择行为（决定相关） |
| components/UpdateCard.vue:206 | 签名校验不通过就不会安装 | 留 | 安装时序 / 安全事实 |
| components/onboarding/OnboardingWizard.vue:505 | 这件事已经结束了（结束不等于删掉，只是不再当作当前状态） | 留 | 数据保留语义（用户会据此判断记忆是否还在） |
| views/settings/sections/SearchConfigGroup.vue:448 | 「已配置」不代表「检查通过」：检查只在你主动点击下面的按钮时进行… | **转交** | 两条测试断言该句（settings-content.test.ts:364、SettingsView.test.ts:155），其中一条在设置组 __tests__（他人范围） |
| views/settings/sections/SearchConfigGroup.vue:193 | 连接检查请求失败（不代表通道不可用） | **转交** | 测试 settings-content.test.ts:500 断言该括注 |
| views/InteractiveView.vue:590 | 上次没有结束的任务已经暂停（N 项）：重新打开不会自动继续，需要你确认后才会开始 | 留 | 真实状态 + 影响 + 下一步 |
| services/security.ts:270；104-126 | 系统权限边界…不是安全沙箱…不等同于隔离环境 | 留 | AGENTS.md 硬性诚实表述；PermissionModeSettings 只渲染一次（三模式三行 facts + 一条边界说明） |
| services/security.ts:247/250/256/259 | 等待中的审批不会自动批准 / 不会复活已拒绝或已过期的审批 | 留 | 切模式时会决定用户是否立刻切（真实影响），且每句只在切换说明里出现一次 |
| stores/interactive.ts:2705 | 服务端的意图视图能力当前不可用（不代表没有意图） | **转交** | 文件属他人（见 §4） |
| interactive/intentWorkbench.ts:337 | 已确认不等于已完成，执行结果以账本与结果卡为准 | 不动 | `confirmOutcomeText` 已无生产调用（仅测试引用），用户不可见；按纪律不改死代码 |
| interactive/approval.ts:460 / stores/interactive.ts:4192 | 受理不等于理解完成 | 留 | 见上（状态区分，且只出现一次） |
| 全部 CSS 注释 / .test.ts / 后端 prompts | … | 不动 | 用户不可见（口径同 catalog 表 B） |

## 4. 表 4 · 需要转交的改动清单（精确到文件 + 行 + 建议文案）

### 4.1 AddMenu.vue（互动上传 / 替换 / 批次）
1. **:178** 当前 `progress(token, "正在保存附件「" + fileName + "」…（首次落盘为工作区材料源文件）");`
   → `progress(token, "正在保存附件「" + fileName + "」…");`
2. **:210** 当前 `progress(token, fallbackNote + "正在保存附件「" + fileName + "」…（首次落盘为工作区材料源文件）");`
   → 去掉「（首次落盘为工作区材料源文件）」。
3. **:276** 当前 `? "正在保存附件「" + job.fileName + "」…（首次落盘为工作区材料源文件）"`
   → `? "正在保存附件「" + job.fileName + "」…"`
4. **失败时说明「哪一份」**（单卡关联路径目前只有错误原因，没有文件名）：
   - **:220** → `fail(token, "附件保存失败「" + fileName + "」：" + messageOf(error), retryOf(kind, cardId));`
   - **:303** → `"附件保存失败「" + job.fileName + "」：" + (job.error || "原因未知")`
   - **:347** → `"附件保存失败「" + (event.fileName || "这份附件") + "」：" + (result.error || "原因未知")`
5. 批次区（:602-637）已经逐份列 name + 状态 + 错误 + 重试，符合「哪一份失败 + 可做的操作」，**不需要改**。

### 4.2 InteractiveView.vue
1. **:581** `恢复事项读取失败：{{ recoveryReadError }}（这不代表没有事项）` → 去掉「（这不代表没有事项）」（重试按钮保留）。
2. **:560** `首次读取失败：这不代表板面是空的。失败期间不会对旧板面发起编辑或提交。` → `首次读取失败：失败期间不会对旧板面发起编辑或提交。`（前半句是判据解释，后半句是真实影响，保留）。
3. I05 已达标（:134 / :486-487），**无需改动**；:590 恢复提示已含状态 + 影响，**保留**。

### 4.3 stores/interactive.ts
1. **importBatchSummaryText（:338-354）** 现输出「本批已接收 X 份 / 未接收 M 份 / 进行中 K 份（单批上限 N 份）」，其中 M = failed + unreceived 合并。建议：
   - 失败与未接收分开（可区分状态仍要区分）：`失败 F 份`、`未接收 U 份`；
   - 「（单批上限 N 份）」只在 **U > 0**（真的被上限拦下）时出现，别放在常态进度里。
2. **:2705** `…当前不可用（不代表没有意图）` → 去掉括注（同 InteractiveView:581 口径）。
3. 判定**保留**：:4094 / :4251「结果未知、改动保留、不要直接重复提交」、:4192「受理不等于理解完成，也不等于批准执行」——真实状态与下一步，且各只出现一次。

### 4.4 设置搜索（searchIndex.ts，owner：设置搜索 #47）
1. **:53** embedding 的 note「更换外部嵌入模型会影响既有索引维度」与现行为矛盾（嵌入模型使用 QIO 内置配置、不能在设置里换成外部嵌入模型）→ 建议 `"嵌入模型使用 QIO 当前内置配置"`。
2. **:181** `"区分「已配置」「未检查」「检查通过 / 检查失败」，不把已配置说成可用"` → 短版 `"配置状态与检查状态分开显示"`。
3. **:203** `"真实打到端点的低影响检查；检查通过才显示通过"` → `"主动发起一次真实的连接检查"`（前半是实现细节，后半是同义反复）。
4. **:270** `"每次后台整理的间隔（1-720 小时）"` → 与字段一致 `"两次后台整理之间的间隔"`（T11 已按此口径落地）。
5. **:196** `"配置 / 替换 / 清除；留空保存不会清除，清除必须显式操作"` → 建议 `"配置 / 替换 / 清除博查 Key"`（留空语义已在输入框旁说一次）。
6. 与 4.5 联动：SearchConfigGroup.vue:448 的三句判据解释若要删，必须同批改 `views/settings/__tests__/settings-content.test.ts:364` 与 `views/__tests__/SettingsView.test.ts:155`；:193 的括注若删，需改 `settings-content.test.ts:500`。**这两处测试在设置组范围内，故本节点未动代码**。

### 4.5 BoardCanvas.vue / BoardGroupFrame.vue / BoardPreviewLayer.vue / SelectionMenu.vue（建议，非必须）
- 「普通组：摆放顺序不代表先后」在 7 处重复（BoardCanvas:1654/2534/2566、BoardGroupFrame:95/438、BoardPreviewLayer:148、SelectionMenu:303/406）。**判定保留文案**（组顺序语义决定相关），仅建议收成一处常量/一处文案来源，属维护项而非文案删减。
- I06 手势指引（BoardCanvas:3641）已逐字定稿，无需改动。

## 5. 表 5 · 按定稿保留的提示 / 确认 / 布局（写明它保护什么）

| 保留项 | 位置 | 保护的用户选择 / 数据 |
| --- | --- | --- |
| 权限三模式 facts（可读 / 可改 / 何时审批）+ 一条系统权限边界说明 | security.ts:104-126、269-271；PermissionModeSettings.vue:129-151 | 用户在「切模式」前知道实际可读、可改与审批范围；诚实表述（受限子进程不是安全沙箱）是 AGENTS.md 硬性要求 |
| 切换模式说明（收紧/放宽 + 等待中审批不自动批准） | security.ts:243-260 | 防止用户以为放宽会替自己批准等待中的申请 |
| 审批结果「已批准，等待后端执行（不等于已完成）」 | ApprovalResultsPanel.vue:59 | 区分「已批准」与「已完成」，避免用户以为操作已经生效 |
| 「在当前话题回答」既不执行也不等于拒绝 | TopicApprovalContent.vue:362 | 影响用户对该动作结果的预期 |
| 撤回确认「你后来做过的修改不会被覆盖——受影响的那几项会被拒绝并列出原因」 | MaintenanceRestoreDialog.vue:235 | 不可撤销动作前的影响说明 |
| 关闭引导确认「已填写的内容不会保存，下次打开是空白」 | OnboardingWizard.vue:449 | 未保存数据，且在作决定的位置只说一次 |
| 「最小化到托盘」只是隐藏窗口 / 退出后待审批事项去向 | RegularSection.vue:258（shell 已改） | 关窗不等于停止服务；避免误判后台已停 |
| 密钥撤销范围（确认框里说一次） | ConnectionCard / ModelServicesSection（settings/shell 已改） | 撤销范围决定用户是否要先去厂商后台吊销 |
| 队列常态「待发送消息」、取消回执「正在取消…」、取消排队 | entries.ts / QueuePanel / SendIssuesPanel（shell 已改） | 区分「待发送 / 正在取消 / 已发出」，避免重复发送 |
| 未完成事项「未完成事项 · N」+ 按原因分组的禁用说明 | RecoveryInbox.vue:88、495-501 | 一个标题一处计数；同一原因只讲一次，不同原因分别保留 |
| 执行记录「诊断（原始标识与结构数据）」折叠区 | ExecutionRecordPanel.vue:524-537 | 原始请求 / 编号 / 结构数据留在诊断内，主界面不再铺调试字段 |
| 星球空态三态用词（暂无 / 暂时读不到 / 没有可撤回的内容） | MaintenanceRecords.vue:255/341/347/508；recoverySummary.ts:84 | 区分「读取成功且为空」与「读取失败」，避免误判数据不存在 |
| 「暂无话题说明」「暂无审批结果」「暂无可恢复的任务」 | ChatColumn:353；ApprovalResultsPanel:231；RecoveryPanel:410 | 空态短句，不解释内部判断 |

## 6. 跑过的命令与结果

1. `cd D:\qio-dev\pm-qio032-integration\frontend`，`npx vitest run src/components/interactive/__tests__/CardInlinePreview.test.ts` → **24 passed / 1 file**.
2. `npx vitest run`（**最终一轮**，集成 HEAD 已前进到 `66bd9636`：17 个文件 — interactive/v2 p3a、fu2c-confirm-failure-reason；interactive ExecutionRecordPanel、d-execution-record-switch、a-unknown-consistency、irl-d-submit-status、submitClusterPanels、CardInlinePreview、p3c-retry-recovery；interactive a-submission-identity；components recovery-inbox-focus-escape、recovery-inbox-retry-action、fu5-stream-empty-layout；conversation ChatColumn、SendIssuesPanel、send-issues-cancel-swap-copy；views i2v2-a-task-entries；stores memoryQuestions）→ **162 passed / 17 files**。早前一轮（16 个文件：interactive/v2 p3a、fu2c-confirm-failure-reason；interactive ExecutionRecordPanel、d-execution-record-switch、a-unknown-consistency、irl-d-submit-status、submitClusterPanels、fix025-card-attach-notice、CardInlinePreview；interactive a-submission-identity；interactive p3c-retry-recovery；components recovery-inbox-focus-escape、recovery-inbox-retry-action、fu5-stream-empty-layout；conversation ChatColumn、SendIssuesPanel、send-issues-cancel-swap-copy；views i2v2-a-task-entries）→ **143 passed / 16 files**（首轮 4 failed 均为执行记录测试断言 operation_key 出现在主界面，已按新口径改读「诊断」区后转绿）。
3. `npx vitest run`（最后一轮 5 个文件：ChatColumn、p3c-retry-recovery、memoryQuestions、p3a-intent-layer、fu2c-confirm-failure-reason）→ **63 passed / 5 files**.
4. `npx vue-tsc --noEmit` → **通过（无输出、EXITCODE=0）**；集成 HEAD 前进到 `66bd9636` 后复跑一次，仍 **EXITCODE=0**。
5. 后端静态边界守卫（只读前端源码，不需要 DB；按 Lead 给的命令跑）：
   `cd D:\qio-dev\pm-qio032-integration\backend; $env:QIO_DATA_DIR='D:\qio-dev\pm-qio032-integration\.tmp-test-data'; $env:QIO_DISABLE_DB_CHECK='1'; & .\.venv\Scripts\python.exe -m pytest tests\test_p3_d_static_boundaries.py tests\test_p2_d_static_boundaries.py -q`
   → **23 passed，EXITCODE=0**（第一次跑之前已按守卫口径把 IntentLayer 的「续作」短版事实句放回，见 §8）。
6. 未跑全量 `npm test`（按任务纪律）；其余后端测试未跑（本节点未改后端逻辑）。

## 6.5 表 · 既有测试断言的改动逐条说明（删减标准条款 + 用户可见 / 读屏）

> 说明：所有断言改动都发生在**文案被主动改写**的场景，改的是「断言读哪一句 / 期望删掉哪句」，没有弱化原有守卫（每条都改成 `not.toContain(...)` 形式的反断言或改为读新的真实位置）。

| 测试文件:行 | 原断言 | 改后断言 | 删减标准依据 | 对应改动性质 |
| --- | --- | --- | --- | --- |
| interactive/__tests__/CardInlinePreview.test.ts:341-350 | code 事实行 `toContain("只读")` | `not.toContain("只读")`（用例名改为「行号 + 等宽 + 语言（正文附注不再重复只读）」） | 「删去标题、按钮、正文和悬停之间的同义复述」「预览只读只在必要位置显示一次」 | 用户可见文案（卡片内预览事实行） |
| interactive/__tests__/d-execution-record-switch.test.ts:347/389/423/519 | 用 `[data-im=execution-record-operations]` 文本里出现 `key_*` 来核对读侧身份 | 改读 `[data-im=execution-record-diagnostics]`（同一份数据仍在诊断原始条目里） | 「原始请求地址、内部编号…放进现有诊断」；测试目的（A/B 侧身份与迟到丢弃）不变 | 用户可见文案（主界面调试字段移除；断言改读诊断，不丢失守卫） |
| interactive/v2/__tests__/p3a-intent-layer.test.ts:216-218 | `summary.notice toContain("已确认不等于已完成")` | `not.toContain("已确认不等于已完成")`，保留「部分失败」断言 | 「删去解释软件如何判断或保证自己不会误判的句子」 | 用户可见文案（确认结果说明） |
| interactive/v2/__tests__/p3a-intent-layer.test.ts:358-363 | 读结果面板内 `intent-confirm-notice` 文本 | 断言面板里**不再有**该节点，改读层底 `intent-layer-notice`（role=status，同一句只出现一次） | 「删去…同义复述」（同屏同句两处 → 一处） | 用户可见文案（确认结果面板） |
| interactive/v2/__tests__/fu2c-confirm-failure-reason.test.ts:114-119 | 成功后 notice `toContain("已确认不等于已完成")` | `not.toContain(...)`，并保留「已确认」断言（状态仍如实） | 同上（判据解释） | 用户可见文案（确认结果说明） |
| conversation/__tests__/ChatColumn.test.ts:6 + :252 | 断言状态条文案 `toContain("不代表没有历史")` | 断言 `toContain("暂时无法读取")` + `toContain("已经加载的内容仍保留")` + `not.toContain("不代表没有历史")` | 「删去解释软件如何判断…的句子」，保留状态与已保留内容（下一步「重试」在按钮上） | 用户可见文案（话题栏状态条） |
| interactive/__tests__/p3c-retry-recovery.test.ts:281-283 | 恢复不可用行 `toContain("不代表没有需要恢复的事项")` | 保留「不可用」断言，改为 `not.toContain("不代表没有需要恢复的事项")` | 同上（空态是另一分支） | 用户可见文案（恢复面板错误行） |

**用户可见 vs 读屏名称**：22 处改动中 **21 处为可见文字**（模板文本 / 状态行 / 提示 / 事实行）；**1 处为读屏名称**——taskCenter.ts:288 的 `aria-label`「取消这条互动任务（与停止当前聊天不是同一件事）」→「取消这条互动任务」（依据：「图标按钮保留简短准确的读屏名称」）。其余 aria-label（RecoveryInbox 动作名 + 记录原文）未动。

## 7. 未处理 / 需要 Lead 决定

1. **「约 60 处」无法逐条追证**：settings 20 项只有名目（T05 在基线也无唯一命中），本表按「用户能否知道状态/影响/下一步」裁决，未编造定稿。
2. **卡片内预览是否需要一个「只读」徽标**：本次按「必要位置只显示一次」删掉正文附注、不新增徽标（卡片头已有文件名 + 关联状态 + 收起按钮，窄卡片加徽标会挤文件名）。若产品口径要求卡片侧也保留一处「只读」，建议放进卡片预览头（`cip-head`）而不是事实行。
3. **他人文件中的同类句子**：见 §4（AddMenu / InteractiveView / stores-interactive / 设置搜索与其测试）。
4. **界面 / 读屏实机复核未做**：本节点无浏览器可运行环境；结论基于源码 + 定向单测。建议 Lead 在验收时按 §4 转交项一起过一遍截图（卡片内预览、执行记录、确认结果面板、搜索状态行）。
5. **未改测试里保留的旧文案断言**（先红后绿纪律）：除本次主动改写的 6 个测试文件外，其余测试未动。

## 8. 静态边界守卫与「能删到哪」的边界

删文案时**不动**这些被守卫测试当作证据的 token / 结构（本次已复核）：

| 守卫（后端只读前端源码） | 要求 | 本节点处理 |
| --- | --- | --- |
| `tests/test_p3_d_static_boundaries.py::test_frontend_has_waiting_and_resume_entries` | `frontend/src/**/*.vue` 内同时出现「等待」「续作」「部分完成」 | 「等待」115 处、「部分完成」15 处本就在；「续作」原先只在被我删掉的长句里 → 已按口径放回**短版事实句**「确认后由服务端续作。」（IntentLayer.vue:595-596），删的是「（本轮不代表已完成）」与「确认前不执行」这类判据解释 |
| `tests/test_p2_d_static_boundaries.py` | PlanetView.vue + ManagementArea.vue 保留 PendingItems / MaintenanceRecords 组件名与「待处理」/「整理记录」标签；PendingItems.vue 保留 knowledge_proposal / maintenance_question / entity_candidate / kind / actions；MaintenanceRecords.vue 保留 listMaintenanceBatches / previewMaintenanceRestore / restoreMaintenance；KnowledgePanel.vue 保留「后台整理」 | 本节点**未改**这些 planet 文件（P01–P06 已由 planet 组处理），守卫跑通 |
| `tests/test_event_protocol.py` | 扫描前端事件名 / 字符串 | 本节点未改任何事件名或事件负载字符串 |

**经验**：以后删含「续作 / 等待 / 部分完成」或组件名/函数名的句子前，先 `rg` 一遍 backend/tests 的静态守卫，删完立刻跑上面那条 pytest。
