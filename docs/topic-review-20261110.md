# 话题重构审查十四项修复 · 交付记录（2026-11-10 · 第三轮）

> 本文件记录本轮**实际做了什么、证据在哪、边界是什么**。唯一共享契约是
> `docs/topic-review-20261110-contract.md`；结构语义见 `docs/architecture.md` §21。
> 历史行号只作定位线索；以下描述以实际实现为准。本轮不合并 main、不打标签、不发布。

## 1. 基线与分支

- 仓库：`HebiIsHere/QIO`。
- 审查基线：`feat/topic-application-20261110` @ `ab140e75549dea4d6593bd3593706c7e6b0cc4e4`
  （已 fetch 核实 `origin/feat/topic-application-20261110` 同 SHA；其 merge-base 是
  `origin/main` @ `a20f5e1f`，分支比 main 多 8 个提交）。基线 CI run 42798647336 = success。
- 修复分支：`fix/topic-review-20261110`（从上述 SHA 切出）；开发在隔离 worktree 中完成，
  原始工作树中他人的未提交改动未被触碰。
- 上一轮（P1-01…P1-05）成果全部保留并复用；本轮不重复实现、不倒退旧功能。

## 2. 十四项与实现落点

| 编号 | 级别 | 实现落点（主要文件） | 行为变化 |
| --- | --- | --- | --- |
| F01 | P1 | `core/loop.py`、`services/turn_orchestrator.py` | 普通搜索/读取工具与过程说明不再单独结束话题决策；只有准备发布正式答案且发布前提满足才进入可回答阶段；含话题工具调用的旧缓冲正文整段丢弃；决策期零正式正文。 |
| F02 | P1 | `services/topic_operation.py`、`services/topic_application.py`、`tools/topic_tools.py` | 决定层与执行层分开记录；主动要求/冲突待处理/可重试失败/真实成功/拒绝/失效各自成事实；同操作同精确参数版本的合法重试可再次进入原工具与具体冲突确认；成功后重放只返回既有结果。 |
| F03 | P1 | `services/topic_recovery.py`、`api/recovery_routes.py`、`storage/migration_r14_b_recovery.py` | 恢复记录持久化、原子领取/租约、并发去重、唯一逻辑答案、答案与成功状态同事务、生成成功保存失败只补存。 |
| F04 | P1 | 后端同上；前端 `stores/session.ts`、`stores/events.ts`、`stores/unifiedApprovals.ts`、`services/approvalLifecycle.ts`、`services/recoveryApi.ts`、`components/RecoveryInbox.vue`、`components/conversation/UnifiedApprovalLayer.vue`、`components/approval/TopicStageNotice.vue` | 待恢复事项独立于活动轮次：TURN_END/关闭/断连/刷新/重启不丢；成功由权威结果关闭记录、移除旧失败提示；恢复旧轮次不激活它、不影响当前轮/导航/草稿。 |
| F05 | P1 | `core/turn.py`、`core/loop.py`、`services/turn_orchestrator.py`（快照）；`services/topic_application.py`、`services/topic_operation.py`（执行侧） | 在建立本轮判断上下文时冻结原问题/来源话题与片段/待接续选择/导航版本；操作继承快照，不在工具返回时补当前版本；同轮自己成功导航有明确因果更新；申请/确认/写入前都用同一快照复核；失效≠拒绝。 |
| F06 | P1 | `core/loop.py`、`services/turn_orchestrator.py`、`services/topic_application.py` | 工具/导航成功但归属迁移失败或话题后置处理异常 → 分步事实记 `failed_stage=ownership`、可恢复失败、持久恢复记录、可观察 ERROR、零正式答案；恢复先修归属再装配上下文再回答，不重跑已成功操作。 |
| F07 | P1 | `services/topic_operation.py`、`services/topic_application.py` | 执行参数（create 仅外层 strip 的真实名称）决定 `param_version` 与批准身份；宽松归一化只作查重/相似证据；内部空格/全角空格/引号/冒号等实际变化使旧批准与旧冲突决定失效。 |
| F08 | P2 | `services/navigation.py`、`services/binding.py` | 接续登记在同一事务里得出新建/替换/幂等复用事实；只有真实选择改变才推进导航版本；同 request_id/重复点击返回相同选择与版本；旧请求重放不重新激活。 |
| F09 | P2 | `services/topic_decision.py`、`services/turn_orchestrator.py`（证据注入）；`services/topic_application.py`（记录/接口） | 拒绝证据沿既有 `same_stage/source_fragment_id` 来源链收集（有界、防环、去重）；容量分段继承原讨论范围；不同讨论不盲目继承；跨轮作为证据交给主模型判断。 |
| F10 | P2 | `services/topic_application.py`、`tools/topic_tools.py`、`services/topic_operation.py` | 查重冲突确认被拒绝进入统一终态（`refused + conflict_declined`），清待处理冲突、进入原话题回答；同版本不重复弹；拒绝范围只覆盖具体对象；超时≠拒绝，失效不写拒绝证据。 |
| F11 | P2 | `tools/topic_lookup.py`（新）、`prompts.py`、`services/app.py`（注册）、`services/tool_router.py`、`tools/display.py` | 新增只读工具 `lookup_topic`：按名称/ID 解析真实话题，覆盖空话题、重名、排名第 6 及更后、已删除/已结束；零导航副作用；查询不关闭后续话题决策。 |
| F12 | P1 | 后端 `services/app.py`（ANCHOR 关联字段）；前端 `stores/session.ts`、`stores/events.ts`、`types/api` | 删除 `targetTopicId=null` 通配；跟随身份绑定 operation/参数版本/轮次/原位置/草稿版本；只有本次操作权威成功事件迁移一次；无关/重复/乱序事件不消费；失效按失效收口；目标已有草稿保护。 |
| F13 | P1 | `services/topic_recovery.py`、`storage/migration_r14_b_recovery.py`、消息读写/检索/导出适配 | 封存片段后恢复答案仍可保存：迟到答案写在原话题/原片段归属的 messages 行（`answer_key` 唯一、`late_answer_of_message_id` 指向原问题），不重开片段、不写当前片段、不搬原问题；读取/检索/导出统一。 |
| F14 | P3 | `services/navigation.py`、既有测试 | 已核实 `continue_from_history` 无生产调用，删除该组合便捷入口；既有测试改为 `register_continuation` + `apply_continuation` 两阶段契约。 |

## 3. 共享契约要点（细节见契约文件与 architecture §21）

- 决策快照在建立判断上下文时形成；真实位置变化才推进导航版本；同轮自己的成功导航有明确因果更新。
- 精确执行参数（带版本）与查重归一化（证据）是两个用途，不共用同一口径。
- 正式答案的必要前提：可回答阶段 + 无未完成话题操作 + 归属/上下文分步事实齐备。
- 恢复持久化/租约/唯一答案/封存后迟到答案的语义与边界。
- 操作关联 ANCHOR 事件（origin/operation_id/param_version/turn_id/nav_version/seq）与草稿保护。
- 拒绝范围继承与具体冲突拒绝终态。

## 4. 验证状态（冻结前，如实记录）

### 4.1 定向检查（按角色，均为真实临时 SQLite + fake 模型 / 真实 Pinia store + 可控事件）

- A（F01/F05/F06/F09）：定向总批 144 passed / exit 0（含 flow、phase gate、topic_decision 集成与回归）；
  同一批次 A 复跑 C 的 R14 套件 41 passed、B 的 32 passed、F/E/Lead 的 43 passed。
- B（F03/F04-backend/F13）：合并定向 103 passed / exit 0（重启持久化、并发直调 + 两个并发 HTTP 请求、
  租约、唯一约束、同事务回滚、暂存后只补存、封存前/生成中封存、容量分段、检索 + 冷归档导出、
  不覆盖后来导航、ownership 先修归属）；旧恢复清单/路由 61 passed。
- C（F02/F05-exec/F07/F08/F09-record/F10/F14）：C 定向 103 passed（新增 F02/F05/F07/F08/F09/F10/F14
  与既有 C 测试）；integration 组 47 passed。
- D（F04-frontend/F12）：新增 r14-d-f12 16 passed、r14-d-f04 16 passed；既有 topic/草稿/审批/恢复
  回归 53 + 126 + 98 + 78 passed；`npx vue-tsc --noEmit` exit 0。
- E（F11）：F11 定向 5 文件 41 passed；提示词/工具注册契约 65 passed；统一弹窗前端既有 123 passed；
  真实 AppContext + 真实 bridge + fake adapter 验证「先 lookup 再 switch 同轮执行」「只查询不触发切换/申请」。
- Lead（共享接线/F12 后端）：ANCHOR 操作关联字段测试 + 既有 anchor 事件/取消/接续/导航回归通过；
  F08 路由层修复后 F 独立路由回归（真实 FastAPI create_app）通过。
- F（独立验证，23 后端 + 14 前端用例）：后端独立回归 23 passed / exit 0，前端独立回归 14 passed /
  exit 0，`vue-tsc` exit 0；**基线快照同一批用例 20 failed / 2 passed**（旧问题可复现）。

### 4.2 交叉评审

- F 逐项矩阵：F01/F02/F03/F04(前后端)/F05/F06/F07/F08/F10/F11/F12/F13 均有「基线红 → 修复后绿」
  独立用例；F14 静态核对（生产源码无 `continue_from_history`、路由为两阶段）；F09 以 A 的来源链
  有界/防环/同阶段继承测试 + 真实轮证据注入核对。
- 交叉评审推动修复的真实接缝：
  1) F07/F10：v1 查重冲突（keep_decision_open）遗留标志会永久拦住改名 v2 被拒绝后的正式回答；
     A 改为按桥当前终态 `has_incomplete_operation()` 判断 + 消费 blocked facts + ANSWERABLE 清标志。
  2) F08：`POST /api/anchor` 的 continue 分支 register 与 enter_topic 各推进一次导航版本（一次点选 +2、
     重放仍 +1）；Lead 修 server.py（历史改选只由 register 推进；回到当前开放片段仍推进一次）。
  3) F05：跨响应连续导航与 P1-02「answerable 后冻结」冲突 → 裁决保持 P1-02，因果更新用同一模型批次
     多次调用 + 迟到 WARNING 验证。
  4) F12：改名后确认时 approval payload 的旧 param_version 与 ANCHOR 执行后版本不同 → 后端 ANCHOR 增
     `approval_id` 权威关联，前端同一确认改名仍跟随一次；不同操作/不同审批仍不迁移。
- E 交叉评审：ANCHOR 事件字段初稿 `reason` → 实施统一 `origin`（契约已修订并在交付记录写明）；
  工具级查重冲突 payload 缺相似话题名（既有缺口）→ C 补 `similar_topic_name`（纯展示，不参与身份）。

### 4.3 界面核对

- 前端改动集中在 store/事件/恢复入口与统一弹窗队列消费：组件级 DOM + 真实 store 行为验证覆盖
  等待确认、收起保留、从入口重开、失效、恢复重试、成功后清除失败态、封存后答案显示与草稿保护；
  未新增页面/面板/浏览器原生确认框，颜色/圆角沿用既有变量。
- 真实应用截图未作为本轮冻结条件；如需视觉复核可在后续只读检查（本轮不声称已做像素核对）。

### 4.4 最终批次与 CI（如实记录，含失败与修复）

- 冻结 SHA（第一次）：`1abe82d777fb1721d03cdfbc347f55d8a257a5b9`（分支 `fix/topic-review-20261110`）。
- 本地唯一完整验收批次在该 SHA 上运行一次（后端并行+串行、前端类型检查+全量、离线评测、
  文档/合规/workflow/分区检查）：**2 个步骤非零**：
  1) `backend_suite`：并行组 1 项失败 `tests/test_stage_protocol.py::test_streamed_interim_text_shares_the_stage_of_its_narrative`
     —— 该用例断言「工具轮后首答先 streaming=true 再 streaming=false 两条事件」，正是 F01 要修掉的
     旧行为（普通工具轮提前 ANSWERABLE 才可能实时流式）。F01 后工具轮首答在决策门内缓冲、
     一次性交付，断言已按新语义更新（同一正文只出现一次、streaming=false），并记录时延取舍。
     串行组 101 passed、并行组其余全过；总计一次性失败为 1。
  2) `frontend_tests`：`src/__tests__/router-aliases.test.ts` 2 项在并行负载下 5015ms 超时
     （历史同款负载抖动，P102 亦记录）；同机空闲单独复跑 **3/3 passed / exit 0**，未改断言。
  其余步骤（前端 tsc、离线评测、docs、third-party、workflow 契约、测试分区）全部 exit 0。
- 修复提交（第二次冻结）：仅更新上述受 F01 语义影响的测试断言 + architecture/delivery 文档；
  定向复跑该用例与相关阶段门用例通过；未再触发第二轮完整批次/CI（按本轮纪律）。
- 唯一完整 CI run：`42825357813`（event=push，SHA `1abe82d7`，repo HebiIsHere/QIO，
  https://github.com/HebiIsHere/QIO/actions/runs/42825357813）；修复提交推送使用 `[skip ci]`
  以避免自动触发第二份完整矩阵，最终状态在最终交付报告如实列出。
- 本文件随冻结提交记录事实，不把「运行中」写成「通过」；未覆盖差异与新 SHA 关系见最终报告。

## 5. 已知边界与显式偏差

- 真实模型自然语言语义判断准确率未验证：fake/mock 只证明程序接线、状态传递、证据来源与分支衔接；
  不得把预设输出包装成真实模型准确率。F09（同一讨论拒绝证据继承）以证据传递与分支接线为验证边界。
- 外部模型调用不保证 exactly-once：崩溃发生在「模型已返回」与「正文已暂存」之间时可能再次调用模型；
  已保证同一逻辑答案唯一持久化、已暂存正文只补存、最终只有一份正式答案。
- 恢复租约默认 600s 且无心跳续租：生成中崩溃时新进程在租约到期前重试会得到 202
  `recovery_in_progress`（可见性已恢复、暂不可重领）；按实例归属提前收回属后续增强。
- 迟到答案排序：用真实 `created_at` 追加在原片段末尾；封存场景中原问题就是片段最后一条消息，
  因此天然在原问题之后；同片段若在封存后仍有更晚消息则排末尾而非紧贴原问题（如实时序，不伪造插队）。
- 旧内存恢复记录（无 conn 的测试替身 scope）仅兼容读取；生产路径一律 SQLite。
- 新增可观察停止原因 `stop_reason_code=topic_operation_incomplete`：模型已准备发布正式答案但话题
  操作/归属/上下文前提未满足时的诚实收口（不发布正文）；契约未列该 code，运行时为新增事实。
- ANCHOR 事件来源字段实施为 `origin`（契约初稿写作 `reason`；前后端已统一，属命名修订，不改行为）。
- 新增 `GET /api/recovery/records/{id}` 详情入口（契约只要求查询/详情/重试接入，未冻结具体形状）。
- 同一轮话题操作成功后可能连续出现两条 ANCHOR：工具成功的 `origin=topic_operation` 与内部 rebind
  位置广播的 `origin=manual`（目标一致）；前端只消费前者，草稿跟随正确；「每次操作只一条 ANCHOR」
  属后续设计细化。
- 未合并 main、未打标签、未发布、未改变生产更新源；未验证 Windows runner 行为与真实模型链路。
