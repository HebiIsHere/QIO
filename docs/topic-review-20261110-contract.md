# 话题重构审查 · 十四项修复共享契约（2026-11-10 · 第三轮）

> 本文件由 Lead 冻结，是本轮**唯一共享契约**。所有角色先读完再动手；不得单方面修改本文件，
> 需要变更接口先发消息给 Lead。基线、分支、工作树、文件所有权、验证纪律都在这里。
>
> **修订（实施期，Lead）**：§3.6 ANCHOR 事件的操作来源字段名统一为 `origin`
> （初稿写作 `reason`；前端 events.ts 与后端 app.py 已按 origin 实现，不再改名）。

## 0. 基线与隔离

- 仓库 `HebiIsHere/QIO`。审查基线 = 分支 `feat/topic-application-20261110` 尖端
  `ab140e75549dea4d6593bd3593706c7e6b0cc4e4`（其 merge-base = `origin/main` @ `a20f5e1f`，
  分支比 main 多 8 个提交；已 fetch 核实 `origin/feat/topic-application-20261110` 同 SHA）。
  本 SHA 是**本轮起点**，不是「远端最新 main」。
- 本轮修复分支：`fix/topic-review-20261110`；隔离工作树：`D:\qio-dev\qio-r14`
  （`git worktree add` 自 ab140e75 切出；`backend/.venv` 与 `frontend/node_modules` 为
  junction 链接，只读复用，不要删除/重装）。
- 原始工作树 `D:\qio-dev\qio` 有其他未提交改动，**本轮任何人不得到那里读写/重置**。
- 所有代码/测试改动只在 `D:\qio-dev\qio-r14`。**只有 Lead 执行 git 写操作**
  （add/commit/branch/push/rebase/checkout）；子智能体只读 git（status/diff/log/show）。
- 原始证据（日志/JUnit/截图）写到 `D:\qio-dev\r14-evidence\<role>\`，**不要**写进工作树
  （保持 `git status` 干净，冻结 SHA 与受测代码一致）。提交进仓库的只有契约/状态/架构文档。

### 禁止事项（所有人）

- 不跑全量 pytest / 全量 vitest / npm test / 生产构建；不触发 CI；不 push。
- 不用真实 API Key / 联网 / 真实用户数据 / 付费模型；模型一律 fake/mock provider。
- 不改历史迁移；不新增生产依赖；新增日志/事件/错误文本必须过 `agent/trace/redact.py`。
- 不新增关键词/正则/包含匹配/字数/相似度阈值式的自然语言意图识别；不新增授权分类器或第二模型层。
- 不为每轮固定增加一次判题 LLM 请求。

## 1. 不可变产品决定（验收底线）

1. 所有用户消息完整进入主循环；是否「主动要求操作」由主模型在**现有话题工具**参数
   `intent=user_request|suggestion`（缺省 suggestion）声明；程序只消费这个受控通道。
2. 主动要求直接进入现有工具（不弹普通申请）；QIO 自己建议才走申请；两者都遵守目标有效性、
   参数、位置版本、查重冲突等既有执行约束。不新增第二套前端直建/直切执行器。
3. QIO 申请发生在正式回答之前。拒绝/取消话题申请后仍须回答原问题；「停止整轮」是另一动作。
4. 原问题与答案只有一份真实归属；工具成功 / 导航成功 / 归属迁移 / 上下文 / 答案分步记录，
   任何一步成功不能代表整轮完成。
5. 查重归一化只作证据；精确执行参数才决定批准身份。失效 ≠ 拒绝。
6. 保留现有中央统一弹窗、事件体系、草稿保护、恢复收件箱与手动 UI 导航入口；不重做布局。

## 2. 十四项与所有者

| 编号 | 内容 | 所有者 | 独占文件（后端相对 `backend/src/agent/`，前端相对 `frontend/src/`） |
| --- | --- | --- | --- |
| F01 | 普通工具调用不得过早打开可回答阶段 | A | `core/loop.py`、`core/turn.py`（仅 ctx 字段）、`services/turn_orchestrator.py` |
| F05 | 决策依据形成时冻结真实位置；旧模型结果不覆盖用户后来选择 | A（写入执行侧由 C） | 同上 + `core/turn.py` |
| F06 | 工具/导航成功但归属失败必须阻止正式回答，进入可恢复失败 | A | `core/loop.py`、`services/turn_orchestrator.py` |
| F09 | 拒绝证据跟随同一讨论的容量分段（模型输入接线） | A（记录由 C） | `services/topic_decision.py`、`services/turn_orchestrator.py` |
| F02 | 直接要求 ≠ 执行成功；冲突/可重试失败后可合法重试 | C | `services/topic_application.py`、`tools/topic_tools.py` |
| F05-exec | 主动要求/申请/确认后执行/写入前都复核同一快照 | C | `services/topic_application.py`、`services/navigation_version.py` |
| F07 | 精确执行参数决定批准版本（查重归一化 ≠ 批准身份） | C | `services/topic_operation.py` |
| F08 | 重复接续登记不推进导航版本 | C | `services/navigation.py`、`services/binding.py` |
| F09-record | 拒绝/冲突拒绝记录进统一状态与证据接口 | C | `services/topic_application.py` |
| F10 | 查重冲突确认的拒绝进入统一终态 | C | `services/topic_application.py`、`tools/topic_tools.py`、`services/topic_operation.py` |
| F14 | 删除无用的 `continue_from_history` 组合入口（已核实无生产依赖）或真正原子化 | C | `services/navigation.py`、对应既有测试 |
| F03 | 恢复执行与答案保存后端幂等（持久化、租约、唯一答案、事务收口） | B | `services/topic_recovery.py`、`api/recovery_routes.py`、`storage/schema.py`+新迁移、消息读写/检索/导出适配 |
| F04-backend | 恢复事项独立于活动轮次、启动后可见、成功权威收口 | B | 同上 |
| F13 | 原片段封存后恢复答案仍可保存并出现在原轮次/检索/导出 | B | 同上（迟到答案存储与读取） |
| F04-frontend | 前端恢复事项与活动轮次分离、成功关闭失败态 | D | `stores/session.ts`、`stores/events.ts`、`stores/unifiedApprovals.ts`、`services/approvalLifecycle.ts`、`services/api.ts` |
| F12 | 草稿跟随只消费本次操作的权威成功结果 | D | 同上 + `stores/events.ts` 事件关联；后端 ANCHOR 字段由 C 提供、Lead 集成 |
| F11 | 只读真实话题对象解析（名称/ID → 真实话题） | E | 新建 `tools/topic_lookup.py`、`prompts.py`、统一弹窗/入口组件（`components/approval/*.vue`） |
| 共享接线 | 工具注册/API 装配/文档/集成 | Lead | `services/app.py`、`api/server.py`、`docs/*`、`scripts/*` |
| 独立验证 | 独立复现、定向集成、交叉评审、台账 | F | 新建 `backend/tests/test_r14_f_*.py`、`frontend/src/__tests__/r14-f-*.test.ts` |

同一文件只能有一个编辑者。需要别人文件 → 发消息给 Lead（Lead 裁决/转交）。
**既有测试文件**：`backend/tests/test_topic_application_flow.py`、`test_p1r_a_mainloop_semantics.py`、
`test_topic_decision_*.py` 归 A；`test_topic_application_approval.py`、`test_p1r_c_topic_identity.py`、
`test_topic_tools.py`、`test_topic_navigation.py`、`test_continue_intent.py`、`test_topic_switch_policy.py`
归 C；`test_p1r_b_topic_recovery.py` 及恢复/消息存储相关既有测试归 B；前端 `p1r-e-topic-phase.test.ts`、
`topicApprovalState.test.ts`、`topicSwitch.test.ts`、`final-b2-chatTopicDrafts.test.ts` 归 D；
`components/approval/__tests__/*` 归 E。别人需要改这些文件 → 发需求给所有者。

### 2.1 本轮编号与历史编号不混用

本文 F01—F14 是**本轮**编号；不要与上一轮 P1-01…P1-05 混写。提交信息、测试名、台账都用 F 编号。

## 3. 冻结接口

### 3.1 决策位置快照（F05，A 写入 / C 读取）

**形成时机**：本轮实际建立判断上下文时（`turn_orchestrator.begin()` 完成本轮用户消息绑定、
before 任何模型调用之前）一次性快照：`turn_nav_version`（真实导航版本）、`bound_topic`、
`bound_fragment_id`、`user_message_id`。排队消息使用**提交时**已有的 `turn_bindings` 绑定，
不得在工具返回时随当前 Anchor 重绑。

- `TurnContext` 新增字段（`core/turn.py`，A）：`decision_nav_version: int | None`、
  `decision_topic_id: str | None`、`decision_fragment_id: str | None`、
  `decision_queued: bool`（是否排队消息）。begin 时写入；已有测试替身缺字段时用 `getattr(ctx, ..., None)` 兼容。
- `TopicOperation` 的 `base_nav_version / from_topic / from_fragment` 必须继承该快照
  （A 的 ctx → C 的 `_operation_for_call` 读取；**禁止**在工具返回时用 `_nav_version()` 补快照）。
- **因果更新**：同轮某话题操作真实导航成功后，由 A 在 orchestrator/loop 中调用 C 提供的
  明确方法（`ctx` 快照更新为操作结果里的真实 `nav_version/topic_id/fragment_id`）——这是
  「本轮自己的合法收尾」，不算外来手动导航，后续同轮操作以新快照为起点。
- 失效判定统一用 `operation.base_nav_version` 与当前真实版本比较（C 的 `_stale_reason`、
  工具写入 CAS、恢复 `require_current`）；不相等 → stale：不执行、不弹申请、不跳回旧位置、
  不写拒绝证据。需要新起点必须显式重新建立（新一轮模型调用/新操作版本）。
- 仅编辑草稿、收起/重开弹窗、渲染/事件、无关后台状态**不**推进导航版本。

### 3.2 操作身份与精确参数版本（F02/F07/F10，C）

- **执行参数**（决定 `param_version` 与批准身份）：
  - `topic_switch`：`{"topic_id": <strip 后>}`；
  - `topic_create`：`{"name": <仅外层 strip 后的真实名称>}`。
    内部空格、全角空格、引号、冒号等**任何实际存储会变化的字符都算参数变化**。
- **查重归一化**（`normalize_topic_name` 宽松形）只用于相似/查重证据，**不得**参与
  `param_version` 或批准匹配。两者在 `topic_operation.py` 里显式分开命名（如
  `execution_params` / `dedup_key`），禁止同一函数两用。
- 用户弹窗编辑并确认的最终名称 → 同一操作的新 `param_version`（用户刚确认的最终名称不再追问）；
  模型后来自行改成不同实际名称 → 旧批准失效，需要新版本/新申请。
- `TopicOperation` 状态拆两层（C 扩展枚举/字段，保持 `TopicStages` 不变）：
  - 决定层：`decided=none|pending|approved|refused|stale`；
  - 执行层：`attempt=none|executing|retryable_failure|conflict_pending|succeeded|terminal_failure`。
- **重试**：同 `operation_id` + 同 `param_version`，若上次执行是
  `retryable_failure / conflict_pending`，允许再次进入**原工具**及既有具体冲突确认；
  不得被 direct 缓存/「本轮已处理过」拦截。成功后同键重放只返回既有目标与结果。
  `stale/refused/terminal_failure` 不重试。稳定操作 ID 不延长有效期。
- `base_nav_version` 在申请建立、确认前、写入前都校验（C）；A 的 loop 在发布正式答案前
  也要求不存在未完成话题操作前提。

### 3.3 阶段前提与归属失败（F01/F06，A + C 提供事实）

- `TopicStages` 五步事实不变：`tool_success / navigation_applied / turn_ownership /
  context_ready / answer_complete` + `failed_stage/failure`。发布正式答案的前提：
  - 处于 `ANSWERABLE`（唯一判定 `publishes_formal_answer`）；
  - 无「待处理/执行中」话题操作（`conflict_pending` 或等待用户决定都不算完成）；
  - 若发生过真实话题操作：`navigation_applied && turn_ownership && context_ready`；
  - 无未消费的待重建/归属事实。
- `_settle_gated_call` 修正：响应只有普通工具调用或过程说明时**不**打开 ANSWERABLE，
  保持 DECISION_OPEN；过程正文只按既有过程区口径处理（含话题工具调用的正文整段丢弃）。
  只有在「准备发布正式答案」时（无工具调用/已声明回答）先做上述前提检查，再 `_open_answerable`
  并一次性交付正文；之后恢复既有流式。
- 归属失败（工具/导航已成功但 rebind 失败或话题后置处理异常）→ 必须：
  `stages.failed_stage="ownership"`、阶段 `RECOVERABLE_FAILURE`、写恢复记录
  （`failed_stage="ownership"`，复用 RecoveryInbox 通道与 `RECOVERY_KIND`）、发可观察 ERROR
  （`code=topic_ownership_failed`，带 operation_id/topic_id/failed_stage/recovery）、**零正式答案**。
  非话题工具的普通后置异常沿用现有容错，不误标。
- 恢复顺序：先修归属（`rebind_turn_to_topic` 重试，不重建已成功话题、不重跑工具、不复制用户消息）
  → 再装配目标上下文 → 再生成答案。已成功导航不重跑。

### 3.4 恢复持久化、租约、唯一答案（F03/F04-backend，B）

- 恢复记录**持久化**（SQLite 新迁移，追加在 77/78 之后，从 79 起；不改历史迁移）：
  记录 `record_id, kind=topic_answer_recovery, status, turn_id, topic_id, from_topic_id,
  user_message_id, operation_id, param_version, base_nav_version, recovery_nav_version,
  failed_stage, message, stages_json, answer_id, answer_text(暂存), last_error,
  claim_owner, claim_until, attempt_count, created_at, updated_at`。
  旧内存记录仅作迁移期兼容读取。
- **执行占用/租约**：`retry_topic_answer` 先原子领取（`UPDATE ... SET claim_owner=?, claim_until=?
  WHERE record_id=? AND status='pending' AND (claim_until IS NULL OR claim_until < now)`），
  领取成功才生成；模型调用期间**不持有 SQLite 写事务**。并发请求：已有答案 → 返回同一
  `answer_id`；正在执行 → 结构化「处理中」（如 409/202 + `code=recovery_in_progress`，带 record_id），
  不重复生成。租约过期可被重新领取。不同恢复事项互不全局串行。
- **唯一逻辑答案**：`answer_key = sha256(turn_id + ":" + user_message_id)`（同原问题/轮次唯一；
  无 user_message_id 时退化为 turn_id+operation_id）。答案表以 `answer_key` 唯一约束，
  多个恢复记录/并发请求最终只产生一份正式答案。
- **事务收口**：答案写入与恢复记录置 `answered`（含 answer_id）在**同一事务**提交；
  已成功重复请求返回同一 answer_id。生成成功但保存失败：把生成正文暂存到记录的
  `answer_text`（与记录更新同事务或可重放），后续重试只补存、不重复生成。
  崩溃发生在外部模型调用与暂存之间时，**如实说明可能再次调用模型**，不声称外部调用 exactly-once。
- 日志/事件/错误不暴露完整敏感上下文。

### 3.5 封存后的迟到答案（F13，B）

- 专用迟到答案写入能力：关联原轮次、原用户消息、固定话题/片段归属；**不重开已封存片段**
  （`closed_at` 不变）、不放宽对所有历史片段写入、不写当前新片段、不搬动原用户问题、
  不新建重复轮次。
- 同一逻辑答案只有**一个权威保存位置**（优先：普通消息表 + 专用迟到标记/专用写入路径；
  或独立答案表 + 读取时合并——选定后在文档记录）。普通答案与迟到答案读取、搜索/记忆检索、
  导出、删除/清理路径都统一考虑，唯一稳定 answer id；同一答案绝不长期保留两份正文。
- 历史加载/当前对话：迟到答案显示在原问题之后（同一话题/片段归属），封存摘要可保留旧版本，
  但检索/导出不能漏。
- 与 F03 共用 `answer_key` 与事务收口；模型已生成仅保存失败时保留结果补存、不重复生成。
- 容量封存不得伪装成用户新导航；恢复不覆盖用户后来的导航（`recovery_nav_version` 复核保留）。

### 3.6 导航版本、权威事件与草稿跟随（F08/F12，C 后端 / D 前端）

- `register_continuation` 返回是否真实改变（如 `changed: bool`）：同 `request_id` 重放、
  重复点同一来源 → 返回既有选择与**相同 nav_version**，不推进；真正改选/离开后返回 → 推进。
  重放已消费/被替换的旧请求不能重新激活旧选择或覆盖当前选择。
- `continue_from_history`：已核实仅测试调用、无生产依赖 → **删除**，既有测试改用
  `register_continuation` + `apply_continuation` 两阶段契约（F14）。
- ANCHOR 事件（及对应后端结果）增加权威关联字段：`operation_id`（手动导航为 null）、
  `param_version`、`turn_id`、`nav_version`、`seq`（单调事件序号）、`origin`
  （manual / topic_operation；初稿写作 reason，实施前后端统一为 origin，见修订说明）。
  普通手动 ANCHOR 不伪造 operation_id。
- 前端草稿跟随：`armTopicDraftFollow` 至少绑定 `operation_id + param_version + turn_id +
  原话题/片段 + 原草稿版本`；目标未知时只保留待关联状态，**删除 targetTopicId=null 匹配下一次
  任意导航的通配语义**；仅当权威成功结果（operation 匹配、成功、目标真实）到达才迁移一次；
  迁移前复核原草稿仍待迁移且版本未被用户新编辑覆盖；无关手动导航、旧/重复/乱序 ANCHOR
  （`seq` 更小或 operation 不匹配）不迁移、不提前消费。操作失效 → 按失效收口，不被迟到成功
  事件再带走草稿。目标已有草稿沿用现有保护/恢复机制，不静默覆盖；草稿始终用户主动发送。

### 3.7 拒绝范围与具体冲突拒绝（F09/F10，A + C）

- 复用既有 `same_stage / source_fragment_id` 连续关系（fragments 已有字段）沿来源链收集拒绝
  证据：**有界**（上限如 64 个片段）、防环、去重；容量分段继承原拒绝范围；`same_stage` 明确
  不同的来源链不盲目继承。不得用「忽略 N 轮」替代语义。
- 拒绝记录保留真实对象（topic_id/name 或新名称）、动作、原因、范围（fragment/turn/user_message）。
  跨轮把证据注入 `topic_decision` 的固定小节；是否重新提出由主模型判断。
- 查重冲突确认的拒绝：消费工具结构化结果 `topic_create_conflict_declined`，记
  `OperationStatus.REFUSED + ConflictStatus.CONFLICT_DECLINED`，清除待处理冲突，进入原话题回答；
  同版本不再重复弹冲突；拒绝范围只覆盖该具体对象，不扩大为「永远拒绝任何新话题」。
  普通拒绝 / 具体冲突拒绝 / 超时 / 失效 / 技术错误分别表示；超时不得写成主动拒绝，
  失效不写用户拒绝证据。

### 3.8 只读话题解析（F11，E；注册由 Lead）

- 新工具名 `lookup_topic`（只读）：入参 `name?`、`topic_id?`（至少一个）、`include_ended?=false`；
  返回真实话题节点列表：`topic_id/name/status/type/score?` 与必要区分信息；
  同名/相似名返回全部真实候选，不擅自挑选；不存在就明确 not_found，**不编 ID、不自动新建**。
- 覆盖空话题（无消息/无片段）与已结束话题（明确 status；是否可切换按现有产品规则如实返回）。
- 零副作用：不改 Anchor、不建片段、不登记接续、不推进导航版本、不自动授权执行。
- 工具说明、路由可见性、集中提示词同步；主模型可先 lookup 再调用现有 `switch_topic`。
  相似度前五继续作为自动建议证据，不无限扩大初始提示词。
- 查询后仍保持 F01：先查询不关闭后续话题决策。

## 4. 验证纪律

- 每个实现者先核实现状、建立最小失败场景（能红），再改，再跑定向测试（含行为边界）。
- 环境：`cd D:\qio-dev\qio-r14\backend`，`$env:PYTHONPATH='D:\qio-dev\qio-r14\backend\src'`，
  `.\\.venv\\Scripts\\python.exe -m pytest tests/<file> -q -p no:cacheprovider`。
  前端：`cd D:\qio-dev\qio-r14\frontend; npx vitest run <spec>`（node_modules 为 junction）。
- 并发：同一时刻最多 3 个 pytest 进程；单文件定向测试优先；不跑全量（Lead 只跑一次完整批次）。
- 事件/时序用事件或屏障控制，不用任意 sleep 制造通过。真实临时 SQLite + fake 模型/审批替身。
- 每个实现者完成后发消息给 Lead：改动文件、每项行为变化、跑过的命令/结果、未完成与风险。
  然后转只读交叉评审，随时响应 Lead/F 的复现请求。

## 5. 汇报格式（子智能体 → Lead）

1. 改动/新增文件精确清单；2. 逐项（F0x）行为变化与最小复现；3. 定向命令 + 通过/失败数 + exit code
+ 日志路径；4. 未完成/已知限制/需要 Lead 集成的接缝；5. 与本文契约的偏差（必须显式说明）。

## 6. 集成与最终验证（Lead）

1. 全部实现 + 定向检查 + F 独立评审完成 → 冻结最终受测 SHA（工作树干净、无未提交改动）。
2. 只跑**一次**本地完整批次（`python scripts/run_backend_suite.py` + 前端全量 + 必要检查）
   与**一次**完整 CI（push 修复分支一次触发；不再 dispatch/PR 二次触发）。
3. 长验证在固定 SHA 独立目录后台运行；记录命令、SHA、北京时间、PID、日志、JUnit、退出码。
4. 如实报告：未合并 main、未打标签、未发布、未验证真实模型语义准确率；失败/运行中分开写。
