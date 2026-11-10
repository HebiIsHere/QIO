# 话题申请流程 + 全量测试提速 · 本轮共享契约（2026-11-10）

> 主智能体维护。所有子智能体先读本文件再动手；**不得单方面修改本文件**，有冲突先发消息给 lead。
>
> **修订（2026-11-10 第二轮 · 五项 P1 修复）**：本文件是**上一轮**的契约与事实记录。以下口径
> 已被同日第二轮明确取代，实现以 `docs/architecture.md` §21 与
> `docs/topic-application-p1r-contract-20261110.md` 为准：
> 1. §1.3「用户本轮已明确要求时不再弹普通申请」的**判定方式**：不再由程序关键词/正则
>    （`detect_explicit_navigation` / `detect_explicit_topic_creation`）识别，改由本轮主模型在
>    工具参数 `intent` 中声明（`user_request` / `suggestion`，缺省 suggestion）；轮前直切已删除。
> 2. §1.4「正式回答不泄露」的**实现方式**：不再用「先发布再改判 delta」；改为后端话题阶段门
>    （`TopicPhase`，只有 `answerable` 才发布正式正文），决策/等待期间整段缓冲。
> 3. §2.1 payload 在原有字段外**新增**：`operation_id`、`param_version`、`base_nav_version`、
>    `user_message_id`、`from_fragment_id`；确认前与写入前按导航版本复核。
> 4. 新增：稳定 `operation_id` 衔接改名/查重/重试；`TopicStages` 分步事实与
>    `topic_context_prepare_failed` 恢复入口；`TOPIC_PHASE` 事件。
> 5. 本轮新增一张**追加迁移**的单行表 `navigation_version`（单调导航版本）；申请记录仍沿用
>    既有审批表，未改历史迁移。
> 基线：`origin/main` @ `a20f5e1f`（v0.3.0 实际实现）→ 分支 `fix/conv-page-030-20261109` @ `354eef36`（对话页 0.3.0 收尾，已推送、工作区干净）。
> 本轮集成分支：`feat/topic-application-20261110`（从 354eef36 切出，本地开发，最后才推送一次触发唯一 CI）。
> 参考快照 13f611a1 只用于定位；仓库当前事实优先。未取得 `QIO_Topic_Mechanisms_20261110.md`，以用户本轮提示词为准。

## 0. 共同禁区（所有人）

- **不要执行任何 git 写操作**（commit / checkout / switch / stash / push / rebase）。所有提交由 lead 统一做。
- **不要跑全量 pytest / 全量 vitest / npm test / 生产构建 / CI**。只跑自己负责文件的定向测试（`-q`、文件级或 nodeid 级）。
- 不要在测试里用真实 API Key / 联网 / 真实用户数据；模型一律 fake/mock provider。
- 不要改历史迁移、不要新增真实 schema 迁移（本轮不需要 schema 变化；申请走既有 `pending_approvals`）。
- 新增日志/事件/错误文本必须过既有脱敏（`agent/trace/redact.py`），不得出现密钥原文。
- 不要新增 Python/npm 生产依赖；唯一例外：E 可在 `backend/pyproject.toml` dev extras 增加 `pytest-xdist`（并同步 uv.lock + 第三方声明）。
- 资源：同一时刻最多 2 个 pytest 进程；不要与 E 的基准测量并发跑重测试；不要占用固定端口（8199/5199 等）做长时间服务。
- 不要用浏览器 alert/confirm/prompt；颜色只能 `var(--*)`；新控件圆角 14px；字体三声部沿用。

## 1. 已确认的产品行为（验收口径）

1. 相似度只作**证据**：预测器/分类器不再单独驱动「切换建议」前端动作，也不再发布可执行的 `TOPIC_SWITCH_SUGGESTED`。唯一判断入口是本轮主模型（受控工具调用），四结果：延续 / 建议切换已有话题 / 建议创建新话题 / 暂时无法判断（无法判断=留在当前话题正常回答，不强制询问）。
2. 模型需要换题时，调用**原有** `switch_topic` / `create_topic`。程序在执行前拦截：建立申请（复用 `ApprovalService.request` 的身份/有效期/单次消费/持久化）→ 本轮暂停 → 用户处理 → 批准后执行**原工具**、把本轮归属与上下文切到目标话题、继续生成正式回答；拒绝/取消则在原话题继续回答原问题。不新增第二套切换/创建执行器，前端不直接写话题。
3. 用户本轮已明确要求具体切换/新建时不再弹普通申请；但 create_topic 的真实查重与相似冲突审批保留。
   **（第二轮修订：这里的「明确要求」改为主模型在工具参数 `intent="user_request"` 里声明，不再由程序的
   `detect_explicit_navigation` / `detect_explicit_topic_creation` 识别；见 `docs/architecture.md` §21.1。）**
4. 未确认前：话题工具不执行、正式回答不泄露（流式正文要么仍在缓冲/过程区，要么被扣住；**不能靠提示词保证**）。
5. 拒绝或「在当前话题回答」后：本轮在原话题形成正式回答，不把整轮标成用户停止；同一段讨论不再自动重复提示（程序不得再用分类器自动建议；同轮内重复申请被程序拦截；跨轮由证据文案 + 模型判断，测试用 fake 模型断言证据与行为）。
6. 关闭弹窗=仅收起（不调用 decide、不消费申请、不判拒绝）；保留已编辑新名称；现有待处理入口可重开同一申请；不自动反复弹出已收起项。
7. 等待期间：只能写草稿，该对话的发送按钮与快捷发送禁用（其他对话/页面不受影响）；处理后恢复既有发送/排队规则；不自动发送等待期间草稿。
8. 确认切换/新建成功后：等待期间草稿仍留输入框，之后主动发送到**确认后的话题**；拒绝后留原话题；目标话题已有草稿要保留可恢复能力，不能被静默覆盖；切换失败草稿不能丢或提前改归属。
9. switch/create 真实成功前界面不得显示「已切换/已创建」；重复点击、刷新、重试、过期、后端重启均不得重复执行或丢消息。
10. 执行失败与回答失败分开记录；已成功创建的话题不因回答失败回滚。保留默认话题、引导、手动进入、只读浏览/检索、历史接续语义；`continue_from_fragment` 不是创建话题的替代工具。

## 2. 共享接口契约（前后端必须严格对齐）

### 2.1 审批 kind 与 payload（后端 → 前端）

新增两个 ApprovalService legacy kind：`topic_switch`、`topic_create`（走既有 `ApprovalService.request()`，因此自动获得 approval_id / 600s 有效期 / 单次消费 / `pending_approvals` 持久化 / `APPROVAL_REQUIRED` 事件 / `/api/approvals/pending` 恢复）。payload：

```json
{
  "action": "switch" | "create",
  "turn_id": "turn-...",
  "from_topic_id": "t1",
  "from_topic_name": "当前话题名",
  "target_topic_id": "t2",            // switch 必填
  "target_topic_name": "目标话题名",   // switch 必填
  "new_name": "新话题名",              // create 必填（可被用户编辑）
  "reason": "简短申请原因（≤80字，模型给）",
  "source": "model"                   // 固定；用户明确要求时不会建申请
}
```

- 前端**不得展示原问题/原问题摘要/预览**（payload 里也不放）。只展示：当前话题、目标话题或可编辑新名称、简短原因。
- 次要动作「在当前话题回答」= decide `reject`（带 `scope.reason="stay_current"` 可选）。主按钮：已有话题「转到并回答」= `approve`；新话题「新建并回答」= `approve` + edits。
- 取消话题申请 = 上述 reject；「停止整轮」仍是既有 turn cancel，两者不共用按钮/接口。

### 2.2 决定接口（前端 → 后端）

沿用统一审批弹窗现有工具审批路径（`POST /api/approvals/{id}/respond`；**C 必须确认现有 UnifiedApprovalModal 工具 kind 实际走哪条路，并只走这一条**，不得新增平行提交入口）。请求体扩展一个可选字段：

```json
{
  "decision": "approved" | "rejected",
  "scope": {"reason": "stay_current"},
  "edits": {"name": "用户改过的新话题名"}      // 仅 topic_create，可选
}
```

- 空名称：后端 400 `{"detail":{"code":"topic_name_required","message":"话题名不能为空"}}`，申请保持 pending，不消费；前端就地显示错误，不关闭弹窗。
- 目标话题不存在/已删除（switch）：400 `{"detail":{"code":"topic_target_missing","message":"目标话题已不存在，请选择在当前话题回答"}}`，申请保持 pending；前端就地显示并提供「在当前话题回答」。
- 批准后执行前重校验：`from_topic_id` 是否仍是本轮绑定话题/active anchor；若用户在等待期间手动导航，申请判 stale：不执行，按拒绝处理（在原话题回答），返回结构允许 200 + 后续事件说明。
- 后端响应/事件沿用 `APPROVAL_RESULT`（既有字段 + `phase`）。不新增事件类型。

### 2.3 后端模块边界

- `agent/services/topic_decision.py`（A 新建）：纯数据 + 提示词渲染，不读业务状态机。
  - `@dataclass TopicDiscussionState`: `topic_id/name`, `scope_summary`, `current_goal`, `confirmed_facts: list[str]`, `open_items: list[str]`, `refused_attempts: list[dict]`（只来自真实消息/操作结果）。
  - `@dataclass TopicCandidate`: `topic_id/name/score/summary`（相似度只是证据）。
  - `build_topic_decision_state(app, ctx, topic_id, prediction, refuse_scope) -> TopicDiscussionState`
  - `render_topic_decision_note(state, candidates, *, explicit_hint) -> str`（注入 system 上下文的固定小节；四结果语义、何时可申请、拒绝范围、不要只凭相似度换题、无法判断就正常回答）。
  - `build_candidates(prediction, topics) -> list[TopicCandidate]`（按分数排序，最多 5）。
  - 拒绝范围证据来自 `ctx.topic_refusal_scope`（B 维护）与 `app.topic_application_state`（B 的 `TopicDiscussionGuard`），A 只读不写。
- `agent/services/topic_application.py`（B 新建）：
  - `class TopicApplicationBridge`：`async before_tool(call) -> ToolResult | None`（None=照常执行；非 None=已处理，作为该 call 的结果）、`async rebuild_system(topic_id) -> str | None`（由 Lead 的 orchestrator 闭包提供）、`payload(kind, arguments) -> dict`、`execute_approved(...)`、拒绝记录 `record_refusal(...)`。
  - `class TopicDiscussionGuard`：`app.topic_application_state`；同轮/同一段讨论的重复申请拦截、拒绝范围记录；仅内存，不新增表。
- `agent/core/loop.py`（B 改）：新增构造参数 `topic_application=None`；`_guarded_execute` 在 `registry.execute` 前调用 `await self.topic_application.before_tool(call)`（拦截 `switch_topic`/`create_topic`）；批准并成功换题后调用 `rebuild_system(new_topic_id)` 并替换 `messages[0]`（system）后继续循环；申请等待期间不得把待确认的正式回答发到回答区（AnswerBuffer/流式路径要验证并有测试）。
- `agent/services/turn_orchestrator.py`（Lead 改）：持有 `TopicDiscussionGuard` 的接线、注入 A 的 `render_topic_decision_note`、删除旧 `TOPIC_SWITCH_SUGGESTED` 发布与 `new_topic_candidate` 驱动的可执行建议、提供 `_rebuild_system_after_topic_application(ctx, adapter, plan, topic_id) -> str` 闭包（重新装配目标话题 blocks 并渲染 system），提供 `_rebind_turn_to_topic(ctx, topic_id)`（移动本轮用户消息、binding、fragment，供 bridge 调用），收尾归属与 `post_turn(final_topic)` 保持一致。
- `agent/services/navigation.py`（Lead 改）：新增窄检测 `detect_explicit_topic_creation(message) -> str | None`（「新建/创建 话题 + 引号或明确名称」；不依赖语义分类）；去掉程序先切一次与模型工具重复执行的风险。
- `agent/tools/topic_tools.py`（B 改）：保持实际执行语义；支持被 bridge 以已批准参数调用；create 查重/冲突审批不变；工具描述与 prompts 一致（A 写文案，B 落描述引用）。
- `agent/tools/approval.py` + `agent/api/server.py`（B 改）：legacy `request` 结果可携带 `edits`（`ApprovalResult.edits: dict | None = None`）；`/respond` 透传 `edits` 并在 topic kind 上校验空名称/目标存在；无 waiter 的 topic 申请返回结构化 409 `topic_application_stale`（前端可处理，不假成功）。其它审批行为不变（安全口径不动）。

### 2.4 前端接口

- `stores/unifiedApprovals.ts`（C 改）：
  - `UnifiedKind = "tool" | "knowledge" | "topic"`；topic item 由 approvals 队列中 kind ∈ {topic_switch, topic_create} 映射，payload 按 §2.1。
  - 新增：`topicItems`（当前 pending 话题申请）、`collapsedTopicKeys: Set<string>`、`openTopicApproval(key)`、`decideTopic(item, {decision, edits?})`（走 §2.2 同一路径；执行中 `submittingKeys` 防重复提交）、`topicPendingForTurn(turnId)`。
  - 收起：关闭动作只把 key 加入 collapsedTopicKeys、**不调用 decide**；同一申请不自动重开（除非用户从入口点开或审批队列刷新出现新申请）。
- `components/approval/UnifiedApprovalModal.vue` + 新 `TopicApprovalContent.vue`（C 改/新建）：复用中央统一弹窗队列，不抢占正在处理的弹窗（既有互斥规则）；内容=当前话题、目标/可编辑新名称、原因；主按钮「转到并回答」/「新建并回答」，次要「在当前话题回答」；右上角关闭=收起；空名称、目标消失、过期、执行失败就地显示可处理状态；执行中按钮禁用；成功前不显示已切换/已创建。
- `stores/session.ts` / `stores/events.ts` / `components/conversation/ComposerEntries.vue`（D 改）：
  - 删除旧 `TOPIC_SWITCH_SUGGESTED` → `pendingSwitch` 可执行确认路径（入口改为打开同一话题申请的统一弹窗；不再有 `confirmPendingSwitch`/`rejectPendingSwitch` 独立提交）。
  - 等待状态：有本对话 pending 话题申请时，过程区显示「正在等待你选择话题」（不是「生成中」）；`topicApprovalPending` 派生自 unifiedApprovals（只读引用）。
  - 发送限制：仅该对话的发送按钮/快捷发送禁用；草稿可写；处理后恢复既有发送/排队规则。
  - 草稿跟随：确认切换/新建成功后，等待期间草稿仍留输入框并归属确认后的目标话题（之后主动发送）；拒绝保持原话题；目标已有草稿保留恢复；失败不丢/不改归属。普通导航草稿规则不变。
  - 事件接入：不新增重依赖；只处理既有 APPROVAL_REQUIRED/APPROVAL_RESULT/ANCHOR/TURN_END 的 UI 派生。
- 样式：颜色 var(--*)；标题/话题衬线、正文无衬线、数据等宽；新控件 14px 圆角；优先图标承载辅助操作，保留必要文字。

## 3. 文件所有权（互斥；只改自己名下文件）

| 角色 | 独占文件 |
| --- | --- |
| Lead | `backend/src/agent/services/turn_orchestrator.py`、`agent/services/context.py`、`agent/services/navigation.py`、`agent/services/binding.py`、`docs/topic-application-contract-20261110.md`、`docs/topic-application-20261110.md`、`docs/status.md`、`docs/architecture.md`、`docs/SETUP.md`（E 提供测试章节文本）、最终验证脚本/证据 |
| A | `agent/services/topic_decision.py`(新)、`agent/prompts.py`、`agent/services/params.py`、`backend/tests/test_topic_decision_evidence.py`(新)、`backend/tests/test_topic_decision_integration.py`(新，Lead 集成后写) |
| B | `agent/services/topic_application.py`(新)、`agent/core/loop.py`、`agent/tools/topic_tools.py`、`agent/tools/approval.py`、`agent/api/server.py`、`backend/tests/test_topic_application_flow.py`(新)、`backend/tests/test_topic_application_approval.py`(新) |
| C | `frontend/src/components/approval/UnifiedApprovalModal.vue`、`frontend/src/components/approval/TopicApprovalContent.vue`(新)、`frontend/src/stores/unifiedApprovals.ts`、`frontend/src/stores/approvals.ts`、`frontend/src/services/approvalLifecycle.ts`、`frontend/src/services/api.ts`(仅新增话题决定函数/类型)、`frontend/src/components/approval/__tests__/` 下新增用例 |
| D | `frontend/src/stores/session.ts`、`frontend/src/stores/events.ts`、`frontend/src/components/conversation/ComposerEntries.vue`、`frontend/src/components/conversation/MessageStream.vue`、`frontend/src/components/conversation/entries.ts`、`frontend/src/components/Composer.vue`（仅发送按钮/Enter gate 读 session.topicApprovalPending）、`frontend/src/components/TurnProcess.vue`（仅等待状态词）、`frontend/src/views/__tests__/ConversationView.test.ts`（旧 pendingSwitch 用例退役）、`frontend/src/stores/__tests__/topicSwitch.test.ts`、`frontend/src/components/conversation/__tests__/ComposerEntries.test.ts` |
| E | `backend/tests/conftest.py`、`backend/pyproject.toml`、`backend/uv.lock`、`scripts/run_backend_suite.py`(新)、`scripts/test_partition_check.py`(新)、`backend/tests/test_test_infra_*.py`(新)、`THIRD-PARTY-NOTICES.md`（依赖声明生成物）、`docs/SETUP.md` 测试章节文本（交 Lead 落盘） |
| F | `.github/workflows/ci.yml`、`.github/workflows/targeted-verify.yml`、`scripts/targeted-verify/verify_workflow_contract.py`、`scripts/targeted-verify/` 内相关脚本、`docs/test-duplication-audit-20261110.md`(新) |

冲突处理：需要动别人文件 → 发消息给 lead，由 lead 裁决/转交。**C 与 D 都不得改对方 store**；A 与 B 不碰 orchestrator；Lead 是 orchestrator/context/navigation 唯一作者。

## 4. 定向验证清单（各角色至少覆盖自己范围内的项）

话题（A/B/Lead/C/D 分工，最终由 Lead 汇总）：
1. 单话题时仍可 create_topic 申请新话题（B fake model）。
2. 延续/纠正/追问不误触发；无法判断正常回答（A 证据 + B fake model）。
3. 旧预测器/分类器不再发布可执行建议（Lead 集成测试 + A）。
4. 未确认：工具未执行、正式回答未泄露（B）。
5. 确认已有话题：本轮后续模型请求含目标上下文，原问题与回答归目标话题、不在两处重复（B + Lead）。
6. 新建用修改后名称；空名称/相似冲突正确处理（B + C）。
7. 拒绝/取消：原话题正式回答，不标用户停止，同讨论不重复提示（B）。
8. 关闭只收起、名称保留、可恢复、等待可写草稿但按钮/快捷键不能发（C + D）。
9. 确认后草稿跟随且不自动发送；目标已有草稿恢复（D）。
10. 重复点击/刷新/重试/失效/重启恢复/中途导航不重复创建、不丢消息、不覆盖新选择（B + C + D）。
11. 不移动其它历史/排队消息；历史接续与只读检索不退化（Lead 回归）。
12. 模型失败/工具失败/回答失败状态真实；权限与脱敏不退化（B）。

测试基础设施（E/F）：模板数据库隔离且来自当前迁移；迁移测试走真实迁移；并行/串行清单并集完整交集为空；清理正确；删除/合并有映射；CI 无重复执行。

## 5. 测试提速约定（E 主导）

- 现有事实：无 pytest-xdist；`db_conn` 每用例重放完整迁移；CI Linux backend job 先单独跑 `tests/test_sandbox_worker.py tests/test_tool_worker.py` 再跑全量（重复）；windows job 用 `-m "not requires_docker"`；CI run 约 41–45 分钟。
- 方案：session 级（每 xdist worker 一次）用当前代码真实迁移建空白模板 → 每用例用 sqlite3 backup API 复制到独立 tmp 数据库（连接设置外键/WAL/busy_timeout 保持）；迁移/升级/旧版本/损坏自愈/初始化/持久性测试继续走真实迁移（加 `QIO_TEST_REAL_MIGRATIONS` 或显式 fixture，不得静默绕过）；`pytest-xdist` 受控 worker（先 4，再按机器调整，禁止 auto）；ONNX/数值线程上限 env（只影响测试进程）；串行组用显式 marker `serial`（真实进程清理/共享系统资源/严格时序），入口脚本先并行组后串行组，两组并集=全部、交集=空，由 `scripts/test_partition_check.py` 机器校验；失败退出码保留。
- 入口：`python scripts/run_backend_suite.py`（本地与 CI 同一入口）；CI Linux/Windows backend job 改用它；去掉 Linux 的独立重复测试步骤（JUnit 分阶段命名、都上传）。
- 证据：固定小样本（普通 DB 业务 / 真实迁移 / 应用接口 / 异步等待 / 子进程边界）同环境同并发前后对比；无同机完整前后数据时只报告局部测量，不推算全量倍数。


## 7. 本轮并行角色与入口约定（lead 维护）

- A = `agent-a-judgement`（话题判断/证据/提示词）；B = `agent-b-runtime`（工具拦截/申请执行/上下文衔接）；
  C = `agent-c-modal`（统一弹窗/话题申请 UI）；D = `agent-d-state`（事件、等待、草稿与发送限制）；
  E = `agent-e-testinfra`（数据库模板/并行/串行分组/耗时）；F = `agent-f-ci`（重复覆盖审计/CI 去重/工作流契约）。
- 所有子智能体在共享工作区 `D:\qio-dev\qio` 直接改文件，**不执行 git 写操作**；提交由 lead 统一完成。
- 后端定向测试用 `D:\qio-dev\qio\backend\.venv\Scripts\python.exe -m pytest <文件/nodeid> -q`（工作目录 backend）；
  前端用 `npx vitest run <spec>`（工作目录 frontend）。不要用 `uv run` 跑测试（E 改造期间锁文件会变动）。
- 全量后端唯一入口（E 交付，CI 与本地共用）：
  ```
  python scripts/run_backend_suite.py [--workers N] [--junit-dir DIR] [--marker "not requires_docker"]
  ```
  行为：先跑 `-m "not serial"` 并行组（xdist，JUnit `DIR/pytest-parallel.xml`），再跑 `-m serial` 串行组（JUnit `DIR/pytest-serial.xml`）；
  两组并集覆盖全部测试、交集为空；任一阶段失败 → 退出码非 0；失败诊断分别保留。CI Linux 用 `--workers 4`，Windows 用 `--workers 4 --marker "not requires_docker"`。

## 6. 汇报格式（每个子智能体 → lead，发送消息即可）

1. 我改了/新增的文件精确清单；2. 每个改动的简要说明（行为，不复述代码）；3. 跑过的定向命令与结果（通过/失败数、exit code、未跑的说明）；4. 未完成/已知限制/需要 Lead 在集成时处理的接缝；5. 如果改了共享契约相关实现，写明与本文档的偏差。
