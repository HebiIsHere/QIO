# 计划：七项残留边界问题收尾（R1—R7，fix/process-attachment-audit-final-boundaries）

- 审计基线（前一参考）：e3d1205eafef28f37db33c0c43a48a32b102de1b
- 本轮复核提交：**43a9fcb1d2ea22e3dfdeaae07a958c1b50599fa9**（fix/process-attachment-audit-consolidation，远端核实一致，其后无新提交）
- main 不含本开发线（origin/main 已到 da0436b 的另一条线，21 个提交与本线无共同工作内容）→ 本轮**从集中修复分支最新提交起建**，不从 main 重建
- 本轮只收尾 R1—R7；F01—F24、F25、兼容路径、脱敏、bounded 读取、分页、incomplete、Markdown 等既有修复必须保持有效
- 合规：不合并 main、不发版本、不混入互动板等其它线；**未经单独授权不推送**

## 一、范围（R1—R7）

| 项 | 摘要 | 负责人 |
| --- | --- | --- |
| R1 | 旧重新定位任务改写最新 ready 副本（共用同名 `.part` + 检查与提交之间的窗口） | A |
| R2 | 前项在后项克隆等待期间被删除，集合仍返回成功（缺整组最终复核/一致性边界） | A |
| R3 | 已移除附件被旧轮询或恢复结果重新加入（异步结果缺操作身份） | B |
| R4 | 文件选择器等待期间丢失发起话题（返回后才读 currentTopicId） | B |
| R5 | 系统核对说明刷新/历史恢复后丢失（历史转换只读 raw.verified） | C（后端字段如需调整由 D 落实） |
| R6 | 最终正文校正仍按文字相似度判断，生成两份回答 | C（最小回答身份协议：生产端 B/D、消费端 C） |
| R7 | 活动任务停止后「重新发送」返回 409（cancelled 却给 resend） | D（后端）+ C（前端接线） |

## 二、冻结契约（并行修改前生效；调整须经 Lead 记录并同步生产端/消费端/测试/文档）

### K1 附件操作：topic、附件 ID、操作版本、失效与恢复写回合并

1. 每个异步附件操作（上传/路径准备/轮询/校验/重新定位/历史重传/恢复）在**发起时刻**捕获 `{topicId, attachmentId(s), opToken, kind}`；后续辅助函数**不得**再读 `currentTopicId` 决定归属。
2. `opToken` 由 Composer 维护：每个待发送附件有自己的 `opToken`（注册/替换时递增）；topic 级有 `topicEpoch`（发送被接受、话题被清空、组件重挂载后恢复基线时递增）。
3. **写入权限判定**：只有 `opToken` 仍是该附件当前 token、该附件仍在当前列表、且不在 `removed/tombstone` 集合、且 `topicEpoch` 未越过捕获值时，才允许更新该话题的 UI 与该话题的持久化；不满足时：
   - 若结果属于**其它话题**、且该话题数据仍存在 → 落到该话题的持久化并刷新该话题（不写当前 UI）；
   - 若该附件已被移除/已随发送确认失效 → 静默丢弃，**不得** upsert 回来。
4. `removeOne(id)`：递增该附件 token、从列表与持久化移除、写入 **removed tombstone**（随待发送持久化一起存，键含 topicId+id；发送成功、显式重新添加或话题清空时清理）。tombstone 必须在组件卸载/重挂载后仍有效。
5. 发送被接受：`topicEpoch` 递增，被发送的附件 id 进入 `sent` 失效集（在飞操作晚到不得再入待发送列表）。
6. `restorePendingAttachments` **不再自行写持久化**：签名改为按 topic 返回补丁（例：`restorePendingAttachments(topicId, { candidateIds, revision })` → `{ restored, missing, revision }`），由 Composer 在修订号一致时合并；合并规则：只**新增**不在 removed/sent 且不在当前列表的 id；**绝不**复活 removed、绝不覆盖更晚的列表状态、绝不整表回写旧快照。暂时失败保留身份（既有修复）不变。
7. 删除失败只把**属于该失败操作**的附件放回；不得恢复整份旧列表。

### K2 回答与系统注记：稳定回答身份、校准目标、annotation 形状与兼容

1. 回答身份 = 流式 `delta_id`（一次模型调用的累计正文段）。生产端在 TURN_END 增加最小字段 **`answer_id`**：指向本次最终校准的目标回答的 `delta_id`；没有可校准回答时缺省/为 null（旧生产端没有该字段）。
2. 消费端规则（冻结，禁止文字相似度）：
   - 有 `answer_id`：只更新该 turn 内 `deltaId === answer_id` 的回答消息正文；**不新建**第二条正式回答；找不到该身份时按缺失处理（不猜）。
   - 无 `answer_id`（旧事件）：校准该 turn 内**最后一条**「已作为正式回答发布」的消息（`interim === false`）；该 turn 没有任何正式回答且 `final_content` 非空时才新建。不得比较全文/前缀。
   - `final_content` **缺省**（undefined/null）= 不校准；**显式空串** = 清空目标回答正文（并保留消息与注记）。禁止用 truthy 判断吞掉空值。
   - 同 turn 多个不同回答身份：各自保留，只改目标那一条；重复 delta / 重复 TURN_END 幂等；旧 turn 或错误身份晚到不得污染当前回答。
   - 正常校准不重启动画、不把正式回答移进过程区。
3. annotation 形状：
   - 实时：TURN_END `annotation`（兼容别名 `final_annotation`）。
   - 历史：`raw.annotation`（字符串）；legacy 旧记录可能把注记内联在正文末尾（含固定表头）。
   - 去重规则：字段存在 → 用字段，正文按原样（不再从正文里二次抽取）；字段缺失且正文含内联表头 → 按既有 `splitSystemAnnotation` 拆分；两者都在且内容等价 → **只渲染一次**（优先字段）；异常 raw/缺字段 → 正常恢复，不制造失败/未验证提醒。
4. 前端渲染：系统事实区域独立、与正文分离，注记 DOM 恰好一次、正文恰好一次、归属原 turn。

### K3 终态动作：可用动作、API 与新旧历史显示一致

1. 后端动作表：`user_stopped`（活动取消、排队取消）→ `("retry",)`；`interrupted` → `("resend",)`；其余 reason_code 不变。
2. `retry` 语义：前端用**既有发送接口**创建**新 turn**（同话题、`retry_of_turn_id`=原轮、附件按既有克隆规则复用历史副本，不依赖原始文件存在）；**只有用户点击才执行**，绝不自动启动；重复点击由 `turnActionBusy` + 发送入口保证只产生一个新 turn。
3. `resend` 语义保持：仅 `interrupted` 且一次性 claim（/api/turns/{id}/resend 准入条件不动）。
4. 历史兼容：读取历史 turn facts 时，`status === cancelled` 且 actions 含 `resend` 的旧记录，在**读路径**归一成 `retry`（不批量改写 journal 执行事实）；前端对 cancelled+resend 亦做同样的防御性归一。`interrupted` 历史保留 resend。
5. 旧 cancelled 行保持 cancelled 事实，不得伪装 interrupted；真正 interrupted 的恢复、一次性领取、queued cancelled 行为不退化。

## 三、文件所有权（同一文件同时只有一个实现者）

| 所有者 | 文件 |
| --- | --- |
| A | `backend/src/agent/services/attachments.py` + 新增 `backend/tests/test_fb_a_*.py` |
| B | `frontend/src/components/Composer.vue`、`frontend/src/services/attachments.ts`、新增共享守卫模块 `frontend/src/composables/attachmentOps.ts`（供 C 在 MessageItem 使用）+ 测试 |
| C | `frontend/src/stores/session.ts`、`frontend/src/components/MessageItem.vue`、`frontend/src/components/TurnProcess.vue` + 测试 |
| D | `backend/src/agent/core/turn.py`、`backend/src/agent/storage/turn_journal.py`、`backend/src/agent/api/server.py`、`backend/src/agent/services/turn_orchestrator.py`（R5 字段如需）+ 测试 |
| E | 只新增 `backend/tests/test_fb_e_*.py`、`frontend/src/**/fb_e_*.test.ts`、`scripts/verify-fb-*`、验证报告；**不改产品实现** |
| Lead | 契约、集成、`docs/**`、最终复跑 |

跨范围需求：C 需要 B 的守卫 → B 提供 `attachmentOps.ts` 并由 Lead 冻结接口；D 需要 C 的前端动作 → 以 K3 为准；任何一方需要改对方文件时先报 Lead。

实际成员分配（会话的团队名额上限 8 已被上一轮成员占满，本轮**复用**上一轮成员承担五个工作流，均使用各自独立分支/worktree；并发能力未受限，五条流同时运行）：

| 本轮角色 | 成员 | 分支 / worktree |
| --- | --- | --- |
| A（R1/R2） | fb-a | `wt/fb-a` @ `qio-acc-a` |
| B（R3/R4） | fb-b | `wt/fb-b` @ `qio-acc-d` |
| C（R5/R6/R7 前端） | fb-c（成员 acc-c2） | `wt/fb-c` @ `qio-acc-c` |
| D（R7 后端 + R6 生产端） | fb-d（成员 acc-b2） | `wt/fb-d` @ `qio-acc-b` |
| E（独立验证） | fb-e（成员 acc-f2） | `wt/fb-e` @ `qio-acc-f` |
| Lead | lead | `fix/process-attachment-audit-final-boundaries` @ `qio-acc` |


## 执行记录（Lead，2026-10-10）

- 集成分支 `fix/process-attachment-audit-final-boundaries` 起点 `9e53736`（= 复核提交 43a9fcb + 本计划）。
- 分支与提交：fb-d（R7 后端 + R6 answer_id 生产端）：`57c458a` + `25a8ba6`（Lead 接手完成并修其测试装置）；fb-c（R5/R6/R7 前端）：`a983f30`（成员 acc-c2）；fb-b（R3/R4）：`c030c8b`（fb-b 产出，Lead 验证后代为提交）；fb-a（R1/R2）：`1171920`（fb-a）；fb-e（独立验证）：`0471d87`（阶段一）、阶段二待并。
- Lead 在集成树上亲自复跑（全部退出码 0）：后端全量 `uv run --frozen pytest`（无 FAILED）、前端 `npx vue-tsc --noEmit` + `npx vitest run`（163 文件 / 1360 用例）、`python scripts/check_docs.py`（36 个里程碑条目）。
- 契约对齐：被测方与验证者共同确认的旧断言更新（`test_turn_timing_facts`、`test_acc_b2_queued_cancel_end`、`test_audit_turn_end_facts_verify`、`test_turn_journal_facts`、前端 `acc_c_final_answer` / `TurnEndFacts.audit.verify` / `answerStreaming` / `AnswerContainerStreaming` / `AnswerContainer.r4.verify` / `acc_c2_incomplete_turn`）均在提交信息与报告里注明了依据（K2/K3），不是为绿灯放宽。

## 四、验收（每项：旧基线先红 → 修复后绿 + 回归）

- R1/R2：真实文件、真实附件服务、确定性闸门（只控制 I/O 时序，不 sleep 碰运气）；断言最终字节、身份、集合回执、数据库绑定、临时文件与句柄清理；/api/turns 入口级证据（模型调用数 0）。
- R3/R4：真实组件/store/localStorage/恢复服务，只控制网络响应时序；覆盖卸载重挂载、逆序、发送确认、删除失败与新操作交错。
- R5/R6：假 provider → 真实后端事件/历史 API → 真实前端 store 与渲染；覆盖实时→刷新、分页、旧内联记录、异常 raw、非前缀改写、缩短、显式清空、重放与乱序。
- R7：真实 FastAPI + TurnManager + 闸门 runner；前端真实按钮 → 实际 API；重复点击、真正 interrupted、queued cancelled、新旧历史。
- 全量：`cd backend; uv run --frozen pytest`；`npx vue-tsc --noEmit`；`npm test`；`python scripts/check_docs.py`；涉及 runtime/工具策略时 `uv run --frozen python -m agent.eval.run` 对比基线；实机截图（取消后按钮、附件移除/切话题、注记历史恢复、唯一正文、窄窗口）。
- 回归红线：F01—F25 相关用例、兼容路径整轮拒绝、脱敏、Markdown、incomplete、排队取消事实不得退化。
