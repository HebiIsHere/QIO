# 话题系统复核补修 R1—R4 · 交付记录（2026-11-11 · 第四轮）

> 本文件记录**本轮实际做了什么、证据在哪、边界是什么**。唯一共享契约是
> `docs/topic-review-r1r4-contract-20261111.md`；结构语义见 `docs/architecture.md` §21。
> 历史行号只作定位线索，以下描述以实际实现为准。本轮不合并 main、不打标签、不发布。

## 1. 基线与隔离

- 仓库：`HebiIsHere/QIO`。
- 受检分支：`fix/topic-review-20261110`；受检提交（本轮起点）：
  `44d92fb38338bb81a315d528abbdcad5eef79f69`。
- 开工时已 `git fetch origin --prune` 核实 `origin/fix/topic-review-20261110` 同 SHA
  （ahead/behind = 0/0）；工作树干净；未发现后续修复，故本轮不重做、不覆盖既有十四项与五项 P1。
- 开发工作树：`D:\qio-dev\qio-r14`；只读基线副本：`D:\qio-dev\r15-baseline`
  （detached 于 44d92fb3，仅用于「基线红」证据，未改基线实现）。原始证据在
  `D:\qio-dev\r15-evidence\<role>\`，不进工作树。
- 基线 CI 事实（E 只读取证）：分支上最后一次 CI run `42825357813` 绑定
  `1abe82d777fb1721d03cdfbc347f55d8a257a5b9`（conclusion=failure，唯一失败用例是
  `tests/test_stage_protocol.py::test_streamed_interim_text_shares_the_stage_of_its_narrative`
  的 F01 旧断言，三个 backend job 同因；docs/frontend/rust/frozen worker/install e2e 全绿）。
  `44d92fb3` 使用 `[skip ci]` 提交、**没有 CI run / check-run**——本轮不能宣称它已被 CI 验证，
  也不能把旧 run 记作本轮 R1—R4 的证据。详见 `D:\qio-dev\r15-evidence\e\ci-baseline.md`。

## 2. 本轮四项与行为变化（R1—R4）

### 2.1 R1 / P1：位置快照早于异步上下文装配（A）

- 问题：`_snapshot_decision_context` 原先在 `await build_context(...)` **之后**执行。用户在
  装配期间于同一话题改选历史片段会推进导航版本；快照随后取到**新版本**、但本轮绑定/上下文
  仍是旧片段，模型基于旧依据返回的工具调用 `base_nav_version` 恰好等于当前版本，写入前 CAS
  误通过并覆盖用户的新选择。
- 修复：
  1. 快照移到 `begin()` 成功返回后、`build_context` **之前**，一轮只写一次；新增
     `decision_turn_id / decision_user_message_id / decision_intent_id / decision_intent_version`
     字段（原用户消息在装配期才落库，如实先记 None，之后只补身份事实，位置字段绝不回填）；
  2. 装配结束、首次模型调用前新增 `_verify_decision_basis`：只读当前权威导航版本与 Anchor，
     写 `fresh / superseded / unknown` 与 trace，**不**把快照更新为新版本；
  3. superseded 后操作仍只继承旧 `base_nav_version`，由既有写入前 CAS 判 stale；
     `suggestion` 在 `_route_application` 入口直接按既有 stale 结果返回（不弹旧依据申请、
     不写拒绝证据）；整轮不停止、已绑定回答仍按原归属完成；
  4. 同轮工具合法导航只通过 `note_decision_navigation` 的权威结果做因果更新
     （`decision_basis_state=causal`），不在 await 后重读当前位置；采用新依据必须显式重建
     上下文与快照（本轮不新增自动重取路径）；
  5. 顺带修复既有 bug：`advance_anchor` 的 `trace.write(..., pending_intent=...)` 关键字参数
     会 TypeError 让整轮 failed（用户装配期间改选即触发），改为字符串参数。
- 行为证据：新用例先红后绿（7 failed → 7 passed）；覆盖装配中同话题改选、切走再返回、
  待接续位置改变、正常无导航变化对照、快照顺序与字段、superseded suggestion 不弹申请、
  三态只读复核。回答始终写入原绑定话题/片段。

### 2.2 R2 / P1：旧位置事件整条拒绝，位置事实与草稿跟随分离（C）

- 问题：`events.ts` 在 `applyAuthoritativeTopicAnchor`（只决定草稿迁移）后仍**无条件**
  `setAnchor`；`navVersion` 没有完整位置顺序保护，`seq` 依附一次性草稿跟随对象。
  已复现：手动导航 v9/seq20 后再收旧操作成功事件 v8/seq19，草稿未搬错但界面跳回旧目标。
- 修复：
  1. 后端 `_publish_anchor_event` 增加 `instance_id`（进程实例身份，跨重启/重连时 `seq` 会归零）；
  2. `/api/runtime/state` 增加权威 `anchor` 快照（与 ANCHOR 事件同源：AnchorService +
     navigation_version + peek_intent + 片段标题；无 Anchor 时如实 null，不伪造）；
  3. 前端 session 新增 `_positionFacts` 与 `_invalidTopicOperationIds`（有界）以及
     `applyAuthoritativePositionSnapshot` / `acceptTopicPositionEvent`；
  4. ANCHOR 分支先判定是否接受：操作/轮次关联拒绝（invalid 集合、旧轮）→ 位置顺序
     （nav_version 主序；同版本只允许合法细化；读不到版本时 instance+seq 兜底）→ 接受后
     统一更新位置 / 待接续 / 草稿跟随；rejected 时三者都不动、不迁移草稿、不广播消费；
  5. 位置接受**不依赖**草稿跟随是否存在/已消费/草稿是否非空；既有 `applyAuthoritativeTopicAnchor`
     只保留草稿迁移职责；目标已有草稿保护、不自动发送、不清空草稿规避问题。
- 行为证据：新用例先红后绿（模拟 baseline「无条件 setAnchor」时 10/13 失败 → 13/13 通过），
  覆盖新手动导航后旧事件、跟随已消费后重放、无草稿重放、同话题乱序、失效操作迟到事件、
  重连/快照/实例变化与 seq 归零、同版本细化/冲突、目标草稿保护；每个场景同时断言
  当前位置 / 待接续 / 草稿。
- 实施期收窄（F 交叉评审）：`recoverable_failure` 不是「拒绝/失效/被新导航替代」的终态证据，
  不再登记 invalid；其迟到事件交给 nav_version 顺序规则，避免将来恢复成功后的权威事件被误拒。

### 2.3 R3 / P1：历史旧请求不能绕过服务层失效结果（B）

- 问题：`POST /api/anchor` 的 continue 分支忽略 `register_continuation` 返回的
  `inactive / state=replaced / intent_id=null`，仍用旧 `fragment_id` 调用 `enter_topic`
  且不推进版本。真实 HTTP 复现 A→B→重放 A：三次 200，最后 Anchor=A、pending 来源=B、版本保持 2。
- 修复：
  1. 新增 `TopicNavigationService.register_and_position_continuation`，把登记、有效性判断、
     需要的 Anchor 落位与版本推进放进**同一个 SQLite 事务**（嵌套复用；失败整体回滚，
     不留下「显示 A、实际接续 B」的半截状态）；点击路径仍不创建片段，
     `apply_continuation` 仍是唯一落实位置；
  2. 路由消费结果并区分：幂等复用（200 replay，零位置写入/零版本推进/零 ANCHOR 广播）、
     失效旧请求（409 `continuation_request_inactive` + state，零写入）、真实新选择
     （200 changed，落位且只推进一次版本、广播一次 ANCHOR）、同 request_id 参数不一致
     （409 `continuation_request_conflict`，零写入）；
  3. 保留既有响应字段并追加 changed/replay/inactive/nav_version；非 continue 分支不变。
- 行为证据：真实 FastAPI + 临时 SQLite + TestClient，修复前 7 failed → 9 passed；覆盖
  A→重复 A、A→B→重放 A、取消后重放、消费后重放、重新主动选择、request_id 参数冲突、
  点当前开放片段、runtime anchor；每场景断言 Anchor 行/pending 来源/意图状态/导航版本/
  ANCHOR 广播次数/片段计数，不只断状态码。

### 2.4 R4 / P2：逻辑答案执行权、租约续期与写入权收口（D）

- 问题：租约按 recovery record 领取；唯一答案约束防重复落库但不防重复生成——
  同 answer_key 的两条记录并发各调一次模型；活任务 600s 租约到期会被接管再次调用模型。
- 修复：
  1. 追加迁移 81 + 补偿 82（只追加、不改历史迁移）：新表 `topic_answer_runs`（answer_key 主键、
     status、generation、owner、owner_instance、claim_until、record_id、answer_id）+ 恢复记录
     追加 `execution_generation / execution_owner / last_heartbeat_at`；
     `migrate.REQUIRED_OBJECTS` 同步登记新表/列/索引；
  2. 按 `answer_key` 协调执行权：同一逻辑答案第二个请求收到 202 `recovery_in_progress`，
     不调用模型；执行权 =（answer_key, generation, owner）令牌，每次领取/接管 generation+1；
  3. 模型调用/装配期间心跳按租约间隔条件续租（短事务，不持 SQLite 写事务、不全局锁、
     不同 answer_key 可并行）；活任务不因固定时间到期被重开；
  4. 接管复用 `InstanceRegistry.owner_alive`：True 不接管、False 可接管、None 不接管；
     真正失效的进程可以被接管；
  5. 答案暂存、保存、成功收口、失败更新、租约释放全部校验当前执行权；失去执行权的旧任务
     返回 `{ok:false, code:"recovery_execution_superseded", superseded:true}` 且零写入；
     保存事务内再校验执行权 → 写唯一答案行 → 所有同 answer_key 的 pending 记录置 answered →
     run 置 answered，原子提交；
  6. 保留唯一答案、只补存、并行、封存后原轮次归属、恢复不覆盖导航。
- 行为证据：先红（两场景各失败于模型调用=2）→ 新用例 12 passed；覆盖同 answer_key 双记录
  并发、同 record 并发、活任务超过原租约（受控时钟+心跳）、失效实例接管、旧执行者迟到成功、
  迟到失败、只补存、接管与保存竞争、owner_alive True/None 不接管、迁移/补偿对象。
  测试缝：模块级 `_now_utc()`、`retry_topic_answer(..., heartbeat_interval, heartbeat_wait)`；
  不用 sleep 造时序。
- 不声称外部模型调用严格 exactly-once：接管场景模型调用可能为旧+新两次，本轮的保证是
  「同一逻辑答案唯一持久化 + 执行权收口 + 旧执行者零写入」。

## 3. 多智能体分工与独立评审

- Lead：共享契约、文件所有权、集成（server/app/schema 接缝协调）、文档、git/CI、最终验证调度。
- A：R1 绑定快照/上下文装配/主循环接线；B：R3 服务与 HTTP 路由；C：R2 前端位置事件/草稿关联；
  D：R4 逻辑答案执行权/租约/持久化；E：跨模块集成测试 + 基线 CI 失败证据（只读 gh，不触发 CI）；
  F：独立复现 + 交叉评审（只写自己的新测试文件）。
- 独立性证据（F）：
  - 后端独立用例 22 项：基线副本 `17 failed / 5 passed`（exit 1）→ 主工作树 22 passed / exit 0；
  - 前端独立用例 12 项：基线副本 `9 failed / 3 passed`（exit 1）→ 主工作树 12 passed / exit 0；
  - R1/R2/R3/R4 每项都有基线红到修复绿的逐项断言，见
    `D:\qio-dev\r15-evidence\f\baseline-red.md` 与 `green-matrix.md`。
- 交叉评审结论（F，只读）：R1 快照时机/复核只读/不回填、R2 接受规则/无第二权威、
  R3 同事务/零写入、R4 执行权 CAS/心跳/迁移只追加、无关键词识别、无实现 sleep 均符合契约；
  推动的真实修正：E 集成断言收窄（R1 裁决）、E 前端类型错误修复、C invalid 集合收窄
  （recoverable_failure 不算失效终态）。评审清单见 `D:\qio-dev\r15-evidence\f\cross-review.md`。
- E 的集成结果（真实主循环/HTTP/事务/前端事件边界）：后端集成文件 10 passed；受影响后端子集
  全绿；前端集成文件 7 passed；受影响前端子集全绿。E 亦核对了基线 CI 失败证据与
  `44d92fb3` 无 CI 的事实。

## 4. 定向验证台账（全部为本轮实际命令结果，非全量）

| 范围 | 结果 | 证据 |
| --- | --- | --- |
| R1 新用例（A） | 先红 7 failed → 7 passed | `r15-evidence\a\` |
| R1 契约 6 文件（A） | 70 passed，exit 0 | `r15-evidence\a\final-required-6files-with-counts.log` |
| R2 新用例（C） | 模拟 baseline 10/13 失败 → 13/13 passed | `r15-evidence\c\` |
| R2 必跑前端集（C） | 5 files / 66 passed，exit 0 | `r15-evidence\c\FINAL-vitest-required.log` |
| R3 新用例（B） | 先红 7 failed → 9 passed | `r15-evidence\b\` |
| R3 既有目标套件（B） | 100 passed，exit 0 | `r15-evidence\b\final-targeted-suite.log` |
| R4 新用例（D） | 先红 2 failed → 12 passed | `r15-evidence\d\` |
| R4 既有恢复相关（D） | 93 passed，exit 0 | `r15-evidence\d\final-existing-recovery.txt` |
| R4 迁移对象/补偿（D） | 76 passed，exit 0 | `r15-evidence\d\final-migration-objects.txt` |
| E 集成（后端/前端） | 10 passed / 7 passed，exit 0 | `r15-evidence\e\` |
| F 独立（后端/前端） | 基线红 → 22 passed / 12 passed | `r15-evidence\f\green-matrix.md` |
| Lead 最终后端定向批（18 文件） | 196 passed，exit 0 | `r15-evidence\lead\final-backend-targeted.log` |
| Lead 最终前端定向批（14 文件） | 173 passed，exit 0 | `r15-evidence\lead\final-frontend-targeted.log` |
| Lead 全仓 `npx vue-tsc --noEmit` | exit 0，无输出 | 同上目录 |
| `python scripts/check_docs.py` | 见最终交付报告（提交前运行） | 同上目录 |

未运行全量 pytest / 全量 vitest / 生产构建 / 离线评测（按本轮纪律），因此不声称全量通过。

## 5. CI 状态（如实记录，不含未发生的结果）

- 本轮按纪律**不默认启动完整 CI 矩阵**；也避免 push、PR、manual 重复触发。
- 推送策略：交付提交使用 `[skip ci]` 推送以避免自动触发完整矩阵；随后用
  `targeted-verify.yml`（dispatch-only，按完整 SHA 锁定）定向验证 docs / backend-py311 /
  frontend 的筛选范围（新用例 + 受影响文件）。
- 本文件随交付提交冻结时，**定向 CI 尚未派发**；其 run 链接、受测 SHA、筛选范围与真实状态
  （通过/失败/运行中/排队/未启动/受阻）以最终交付报告为准——报告前会查询一次真实状态。
- 未覆盖：完整 CI 矩阵、Windows runner 复验、安装包 E2E、真实模型链路；`44d92fb3` 本身
  没有 CI run，不能把旧 run 42825357813 记作本轮验证。

## 6. 已知边界与限制

- 真实模型自然语言语义判断准确率**未**验证：fake/scripted 模型只证明程序接线、快照时机、
  事件顺序、事务与执行权收口；不得把预设输出包装成真实模型准确率。
- 外部模型调用**不保证** exactly-once：崩溃发生在模型返回与结果持久化之间仍可能再次调用；
  本轮保证的是同一逻辑答案唯一保存与执行权/世代收口。
- R2 的「重连 / 实例变化 / seq 归零」用事件与快照字段模拟（未起真实 SSE/uvicorn 端到端）；
  R3 覆盖真实 HTTP 的**顺序**重放，未做真实并发 HTTP 重放；R4 用单进程多任务 +
  InstanceRegistry 模拟实例失效（契约未要求真实多进程）；前端为 store/事件层验证，
  未做浏览器 E2E 视觉核对。
- R4 旧版本残留边界：执行权以 `topic_answer_runs` 为权威；仅有旧 record.claim_owner 而无 run 行
  时按「无执行/可接管」处理（单一进程升级假设），不引入第二套存活判定。
- R1 边界：`decision_user_message_id` 在装配期补记身份事实；位置字段永不回填。导航版本
  读不到时快照如实 None、复核状态 unknown，不把未知伪装成 fresh/superseded。
- R2 边界：`recoverable_failure` 不登记 invalid（恢复重试可能成功）；失效来源仍以
  APPROVAL_RESULT 非 executed 终态为准。
- 基线 CI run 42825357813 属于 1abe82d7；本轮起点 44d92fb3 的 CI 状态为「无运行 / 未验证」。

## 7. 文件清单（本轮变更）

实现：
- `backend/src/agent/core/turn.py`
- `backend/src/agent/services/turn_orchestrator.py`
- `backend/src/agent/services/topic_application.py`
- `backend/src/agent/services/app.py`（ANCHOR instance_id）
- `backend/src/agent/services/navigation.py`（register_and_position_continuation）
- `backend/src/agent/api/server.py`（anchor 路由 + runtime anchor 快照）
- `backend/src/agent/services/topic_recovery.py`（逻辑答案执行权/心跳/写入权）
- `backend/src/agent/storage/migration_r15_r4_recovery_lease.py`（迁移 81）
- `backend/src/agent/storage/schema.py`（迁移 81 + 补偿 82）
- `backend/src/agent/storage/migrate.py`（REQUIRED_OBJECTS）
- `frontend/src/stores/session.ts`、`events.ts`、`restore.ts`、`services/api.ts`

测试：
- 新建 `backend/tests/test_r15_a_r1_snapshot.py`、`test_r15_b_r3_anchor_replay.py`、
  `test_r15_d_r4_recovery_lease.py`、`test_r15_e_integration.py`、`test_r15_f_independent.py`
- 新建 `frontend/src/stores/__tests__/r15-c-position-events.test.ts`、
  `frontend/src/__tests__/r15-e-integration.test.ts`、`r15-f-independent.test.ts`
- 既有测试按新契约更新：`backend/tests/test_r14_b_migration.py`（版本断言跟随 SCHEMA_VERSION，
  未弱化）；`frontend/src/stores/__tests__/r14-d-f12.test.ts`（2 处 fixture 版本推进 +
  2 处失效/拒绝迟到事件改为整条拒绝语义）；`frontend/src/__tests__/r14-f-draft-follow.test.ts`
  （fixture 增加 navVersion，断言未改）。

文档：
- `docs/topic-review-r1r4-contract-20261111.md`（本轮共享契约 + 实施期修订）
- `docs/topic-review-r1r4-20261111.md`（本文件）
- `docs/architecture.md` §21（R1—R4 语义与边界同步）
- `docs/status.md`（P104 与最后核对）

## 8. 明确声明

**未合并 main、未打标签、未发布、未改变生产更新源。**
