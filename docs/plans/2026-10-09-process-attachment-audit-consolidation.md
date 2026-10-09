# 计划：对话过程区 / 流式 / 附件审计问题集中修复（fix/process-attachment-audit-consolidation）

- 基线：e3d1205eafef28f37db33c0c43a48a32b102de1b（分支 fix/attachment-send-cancel-integrity，本地与 origin 一致，worktree 干净）。
- 该开发线（P16—P22 及此前里程碑）目前**未**并入 main（origin/main = 6e073e9，落后 240 个提交），因此集成与子分支一律从审计 SHA 起建，不从 main 重建。
- 本轮只处理：统一过程区、流式回答、耗时、附件及直接相关的历史恢复与 Markdown 渲染。不合并互动板（interactive）开发线，不合入 main，不发布安装包或版本；本地提交，未经授权不推送。
- 范围：F01—F14 为本轮明确待修复问题；F15—F24 为更早审计登记的对照问题，必须逐项先核实「是否已有修复 + 反例通过」，已有修复者列提交与验证即可，不再重复实现。

## 一、F01—F24 状态表（初始判定，负责人随验证补记）

初始状态一律记「尚未验证」；每个负责人必须先在**基线**上实际运行反例（能失败），再判定「仍成立 / 已有修复且反例通过 / 本轮修复」。

| 项 | 初始状态 | 负责人 | 反例/证据登记处 |
| --- | --- | --- | --- |
| F01 附件解压/解析资源无界 | 尚未验证 | A | 本目录 verification-acc-phase1.md §A |
| F02 大文本按行迭代整行分配 | 尚未验证 | A | 同上 |
| F03 noopener 误判/URL 过早撤销 | 尚未验证 | D | 同上 §D |
| F04 历史重新上传结果不可见 | 尚未验证 | D | 同上 |
| F05 排队改变活动 turn 归属 | 尚未验证 | C | 同上 §C |
| F06 SSE 无结束标记仍算完成 | 尚未验证 | B | 同上 §B |
| F07 流式事件绕过脱敏 | 尚未验证 | B | 同上 |
| F08 异步结果跨话题写入 | 尚未验证 | D | 同上 §D |
| F09 重传失败仍删旧附件 | 尚未验证 | D | 同上 |
| F10 暂时恢复失败清空持久化 | 尚未验证 | D | 同上 |
| F11 附注释导致整段回答重复 | 尚未验证 | C（后端出口协同 B） | 同上 §C |
| F12 排队轮取消无结束事实 | 尚未验证 | C | 同上 |
| F13 列表内代码块/表格/嵌套压平 | 尚未验证 | C | 同上 |
| F14 折叠与明细耗时口径冲突 | 尚未验证 | C | 同上 |
| F15 绑定跨 await 使用失效归属 | 尚未验证 | E | 同上 §E |
| F16 多重试前序克隆缺回滚 | 尚未验证 | E | 同上 |
| F17 副本不可读仍放行 | 尚未验证 | E | 同上 |
| F18 引用消失仍可重试 | 尚未验证 | E | 同上 |
| F19 未声明前缀阶段中断清空文字 | 尚未验证 | B | 同上 §B |
| F20 重定位旧后台结果覆盖新内容 | 尚未验证 | E | 同上 §E |
| F21 附件同步读取阻塞事件循环 | 尚未验证 | A | 同上 §A |
| F22 嗅探截断 UTF-8 误判 GBK | 尚未验证 | A | 同上 |
| F23 分页元数据与实际返回不符 | 尚未验证 | A | 同上 |
| F24 missing 重试响应致前端放弃 | 尚未验证 | E(后端)+D(前端) | 同上 §E/§D |

## 二、冻结契约（并行修改前生效；变动须经 Lead 记录裁定）

### C1 turn / message / 回答标识与历史兼容
- 统一身份：turn 有服务端 turn_id；同一 turn 恰好一次 TURN_START、一次 TURN_END（终态 completed / failed / cancelled / unavailable）。
- 过程区、工具卡、阶段、说明、回答、结束事实一律按 turn_id 归属；活动 turn 同一时刻只有一个。
- 排队中的 turn（已受理未启动）**不改变**任何事件归属；旧历史无 turn_id 的记录按时间顺序整体分组，不与实时事件混合。
- TURN_END 可携带 final_content；前端 applyFinalAnswer 以 **turn 身份 + 最终校准**为准，禁止再用「全文是否相等」判断同一次回答。

### C2 有效流式结束 / 不完整结束
- 结束语义由 provider 协议判定（OpenAI 兼容：finish_reason；Anthropic：message_stop）；EOF 无终止标记 = **不完整结束**，不是完成。
- 不完整结束时：保留已确认正文，过程区明确显示未完成与可恢复操作，历史如实区分；不输出「正常完成」状态。
- 取消 / 传输错误 / 合法长度截断 / 工具调用未结束各自有明确 reason_code；不为不同协议虚构同名标记。

### C3 脱敏跨分块策略
- 一切可观测输出（增量、累计快照、一次性正文、最终校准、注释、事件、Trace、历史、错误）统一走 agent/trace/redact.py::redact_text。
- **先脱敏再发布**：任何含正文的对外载荷在事件出口处对当前累计文本脱敏；已登记敏感值跨分块切开的部分必须可识别——采用有界未定稿尾部缓冲：对已登记敏感值的最大长度范围，尾部不发布直到确认无完整对齐再放行。
- 实现语义：缓冲是有界的、随流推进释放；不退化为「整段生成后显示」。文件所有权：core/loop.py 流式出口归 B；C 只消费展示，不改 B 的文件。

### C4 附件异步操作的 topic 与版本归属
- 恢复 / 上传 / 选择 / 路径准备 / 重试 / 重新上传 / 重定位 / 轮询 / 删除，在发起时记录 (topicId, operationSeq, attachmentId)；返回时校验当前 store 的归属版本，不匹配则把结果落到发起话题的持久化数据并刷新该话题，**不写当前 UI**。
- 后端落库 topic 与前端写入 topic 必须一致；旧操作不得覆盖较新的操作或用户编辑；晚到结果按 topic 可归属时保留，制造孤儿时如实报告。

### C5 重传替换的提交条件
- 替换 = 明确的 (replyToAttachmentId, newAttachmentId, topicId) 三元组；仅当**指定的新附件**达到 ready 且成功加入发起话题的待发送列表，才提交替换。
- 提交动作：新附件入列表成功 → 移除旧条目；移除失败如实报告（保留可恢复状态），不得宣称无条件成功。新准备失败 / 取消 / 无选择 / 多文件歧义 → 保留旧条目。

### C6 绑定 / 克隆的提交与回滚
- 绑定遵循既有契约：显式 attachment_ids 存在即语义；集合级提交——任一成员失败整轮拒绝，无半绑定。
- 重试克隆：本轮的中间克隆（新行 / 新副本 / preparing 状态）在后续项失败或整轮取消时**完整补偿回滚**；取消后仍在执行的线程不得写回已撤销结果（落库前校验操作版本）。
- 重定位：每次定位分配版本标识；旧任务结果既不更新数据库也不覆盖最终内容；临时文件安全清理。

### C7 读取预算与分页游标
- read_attachment 有界读取：单次返回字符上限、单成员/累计展开上限、单行处理走有界分块+增量解码，不用无界 readline 或整文件解码。
- 分页事实以实际交付为准：截断时 next_offset 指向真实继续位置；超长行引入行内片段游标，旧游标兼容；元数据必须反映实际交付内容。
- 超资源 → 明确、可理解的限制原因，不伪装成完整读取成功。

### C8 总耗时口径
- 用户可见「总耗时」= **排队 + 执行**；可分列执行与排队。
- 执行 duration_ms 与排队 queue_ms 由 TURN_END 权威字段提供（core/turn.py）；前端 buildTurnTiming 的 total 与折叠显示使用同一数字与标签；明细缺失时只显示可证明的时间并明确标签。

## 三、文件所有权（同一文件同一时刻一个维护者；需要越界时先向 Lead 登记，由 Lead 串行裁决）

| 维护者 | 主权文件 |
| --- | --- |
| A | backend/src/agent/tools/attachment_tools.py（读取/解析/嗅探/分页/取消调度） |
| B | backend/src/agent/adapters/*（native/anthropic）、agent/core/loop.py（流式出口/未声明前缀缓冲注释拼接）、agent/core/turn.py（timing 字段出口） |
| C | frontend/src/stores/events.ts、stores/session.ts、services/trace.ts、TurnProcess*/TurnTimingPanel 组件（耗时）、components/MarkdownContent.vue |
| D | frontend/src/services/attachments.ts、components/Composer.vue、components/MessageItem.vue（reuploadOne 与附件卡片）、utils/externalLink.ts |
| E | backend/src/agent/services/attachments.py（绑定/克隆/重定位/重试与 missing 语义） |
| F | 只新增 backend/tests/*_acc_verify.py、frontend/src/**/*_verify 复验文件、scripts/verify-acc-*、验证报告；**不改产品实现** |
| B/C 界面 | F11 后端注释拼接归 B；前端最终校准消费（applyFinalAnswer / 事件 stores）归 C |
| E/D 界面 | F24 后端 missing→preparing 语义归 E；前端 track 的等待与放弃规则归 D |

## 四、环境与验证命令（各 worktree 独立，依赖已装好）

- backend 测试：cd backend 后 uv run --frozen pytest -q（先跑相关文件，交付前全量）。
- frontend：npx vue-tsc --noEmit；npm test -- 相关文件；交付前 npm test 全量。
- eval（涉及 runtime / 工具策略改动时）：uv run --frozen python -m agent.eval.run 对比基线。
- 文档一致性：python scripts/check_docs.py（三处里程碑状态一致）。
- 假 provider -> adapter -> loop -> 事件链 用于流式 / 结束 / 注释 / 脱敏验证；浏览器层用无头浏览器装实际页面。
- 模型测试只用 fake/mock provider；测试装置自身要有资源保护（子进程有上限、超时）。

## 五、并行组织

| 成员 | 分支 / worktree | 范围 |
| --- | --- | --- |
| Lead | fix/process-attachment-audit-consolidation @ D:/qio-dev/qio-acc | 契约冻结、集成、冲突裁定、最终报告与文档 |
| A | wt/acc-a @ D:/qio-dev/qio-acc-a | F01 F02 F21 F22 F23 |
| B | wt/acc-b @ D:/qio-dev/qio-acc-b | F06 F07 F19 + F11 后端 |
| C | wt/acc-c @ D:/qio-dev/qio-acc-c | F05 F11 F12 F13 F14 |
| D | wt/acc-d @ D:/qio-dev/qio-acc-d | F03 F04 F08 F09 F10 F24 前端 |
| E | wt/acc-e @ D:/qio-dev/qio-acc-e | F15 F16 F17 F18 F20 F24 后端 |
| F | wt/acc-f @ D:/qio-dev/qio-acc-f | 独立验证：先在基线建反例，集成后复跑；不改产品代码 |

## 六、验收与交付

1. 每项：基线反例（能失败）→ 修复 → 反例转绿 + 回归绿灯，逐项记录触发条件、修复前后结果与验证层级。
2. 跨层组合：发送/排队/取消/不完整结束/注释/脱敏；附件全生命周期（恢复/重传/替换/绑定/克隆/重定位/missing 重试）。
3. 前端实际起应用视觉检查（执行与排队共存、失败与取消、附件恢复、Markdown 列表代码 / 表格、窄窗口）。
4. 文档同步：docs/status.md、docs/architecture.md 相关契约小节，与本计划一致；check_docs 通过。
5. 完成本地提交；未经授权不推送、不合 main。

## 七、本轮追加裁定（Lead，2026-10-09 21:10；针对集成分支复跑发现的 3 项红灯）

集成分支复跑独立验证者的阶段一反例后，F01—F05、F07—F10、F13—F24 转绿；仍红三项，裁定如下。

### C2 追加：终止状态集合
- `TURN_END.status` 取值集合扩为：`completed / failed / cancelled / unavailable / incomplete`（终态台账 `turn_journal` 同步接受 `incomplete`）。
- `incomplete` **只**用于「不完整 EOF」：native 无 `finish_reason`、anthropic 无 `message_stop`、仅 usage/空分块、未结束的工具调用（即 `reason_code == "incomplete_stream"`）。
- 厂商合法终止保持诚实区分而不升级为失败：`length_limit`、`content_filter` 的 status 仍是 `completed`，只用 `reason_code` 区分。
- `incomplete` 时：已确认正文保留在 `final_content`，`reason_code=incomplete_stream`、`stopped_by=system`、带人话 reason、`actions` 含 `retry`；结束语义必须贯穿 adapter → loop → TURN_END → 前端 → **历史台账**（服务层 `turn_journal.record_facts` 落 reason_code/reason/stopped_by/actions），刷新后仍是「未完成 + 原因 + retry」。
- 因此 F06 验收断言为：`status == "incomplete"`、`reason_code == "incomplete_stream"`、`actions` 含 retry、已确认正文保留、未确认后缀不得出现。

### C1/C5 追加：排队轮的结束事实（F12）
- 一个 accepted turn 恰好一次 TURN_END，**包括排队期（accepted 未开始）被取消的 turn**；该 END：`status=cancelled`、`reason_code=user_stopped`、`stopped_by=user`、`actions` 含 `retry`，并且立刻发出，不等 active turn 跑完。
- 操作动词裁定：**排队取消路径 `actions=("retry",)`**；active 取消路径保持既有 `("resend",)`。理由：`/api/turns/{id}/resend` 只接受台账里 `interrupted` 的行（`recoverable`/`claim`），排队取消落台账是 `cancelled`，`resend` 必然 409 —— 列出它是死按钮；`retry` 走前端「重发该轮用户消息」，真实可用。验证者的 F12 反例相应从 `resend` 对齐为 `retry`（其余断言不放）。
- 取消排队轮必须：先可靠落地结束事实（台账终态 + record_facts），再清理队列标记；重复/迟到/竞争取消幂等；**绝不**触碰 active turn 的归属、事件与状态。
- 前端 `events.ts` 的 TURN_END「非 active 但已知 turn」分支按此消费：事实落到该 turn，A 不受影响。

### C1 追加：系统核对注释的交付形态（F11）
- 裁定：注释**不再拼进 `final_content`**。后端以独立字段 `annotation`（别名 `final_annotation`）随 TURN_END 交付，`final_content` 保持纯正文；前端在独立「系统事实」区域渲染该注释，正文只出现一次、不重启打字动画。
- 因此验证者原反例中「注释必须出现在 final_content」的断言属于**基线行为**，改为契约对齐断言：`final_content` 以正文开头且不含注释头、`annotation` 字段完整（含头与结论句）、ASSISTANT 事件里正文恰好一次且不含注释头。正文唯一性与注释完整性两类断言都不许删除。

### 未决到本轮结束的项
- F11 后端已实现独立字段；F06/F12 由 acc-b2 修复、`incomplete` 的前端消费由 acc-c2 完成；阶段二复跑与实机取证由 acc-f2 完成；F01—F24 最终判定以 acc-f2 的 `docs/verification-acc-phase2.md` + Lead 复跑为准。
