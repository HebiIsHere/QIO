# 话题重构审查五项 P1 修复 · 本轮共享契约（2026-11-10 · 第二轮）

> 本文件是**本轮唯一的共享契约**，由主智能体冻结。任何角色不得单方面修改本文件；
> 需要变更接口时先发给主智能体（Lead）。
> 代码级契约：`backend/src/agent/services/topic_operation.py`（Lead 已写，只读复用）。

## 0. 基线

- 仓库 `HebiIsHere/QIO`，分支 `feat/topic-application-20261110`。
- 正式基线提交 `03b3eab708655976d5afb86b7a8ed58b32ffc3f5`（= 远端同名分支 head，
  fetch refspec `+refs/heads/*:refs/remotes/origin/*` 已核对，不是只拉标签）。
- 上一轮已实现并**保留**：统一话题判断（相似度只作证据）、话题申请桥、
  测试提速（DB 模板克隆 / 并行+串行分组 / CI 去重）。
- 上一轮完整批次在基线提交上的已知失败（本轮要先核实再处理，不得当成已解决）：
  `tests/test_r9g_acceptance_e2e.py::test_duplicate_decide_does_not_execute_twice`、
  `tests/test_rm_c_r05_derived_claims.py::test_claim_mirrors_record_owners_and_clears_it`。
- 工作方式：**同一工作树 + 严格文件所有权 + 仅 Lead 执行 git 写操作**（相当于隔离 worktree
  的同等可靠方式：所有角色只写自己名下的文件，测试互相隔离在 pytest tmp_path）。
  子智能体**不得** git commit / push / 切分支 / 跑全量 / 触发 CI。

## 1. 五项 P1 与所有权

| P1 | 内容 | 所有者 | 独占文件 |
| --- | --- | --- | --- |
| P1-01 | 删除程序授权识别与轮前直切，语义判断交给主循环 | A | `services/turn_orchestrator.py`、`prompts.py`、`services/topic_decision.py` |
| P1-02 | 后端正式回答发布边界（阶段门） | B | `core/loop.py`、`core/turn.py`、（必要时）`core/message_assembly.py` |
| P1-03 | 上下文准备失败可恢复，不回落到旧上下文 | B（loop）+ A（orchestrator 重建闭包）+ C（记录/校验） | 见上 |
| P1-04 | 申请绑定真实导航位置与版本 | C | `services/topic_application.py`、`services/navigation_version.py`（新建）、`storage/migration_p1_nav.py`（新建） |
| P1-05 | 稳定 operation_id 衔接改名/查重/重试 | C | `tools/topic_tools.py`、`services/topic_application.py` |
| Lead | 导航服务入口（删识别器、版本推进、CAS）、文档、集成、全量/CI | Lead | `services/navigation.py`、`docs/*`、`scripts/*` |
| D | 前端弹窗/入口组件状态 | D | `components/approval/UnifiedApprovalModal.vue`、`components/approval/TopicApprovalContent.vue`、`components/conversation/UnifiedApprovalLayer.vue`、`components/conversation/ComposerEntries.vue` |
| E | 前端 store/事件/草稿/发送限制 | E | `stores/unifiedApprovals.ts`、`stores/approvals.ts`、`stores/session.ts`、`stores/events.ts`、`services/approvalLifecycle.ts`、`api/*.ts` |
| F | 定向验证、回归、契约/事件顺序/接口评审、已知 CI 失败定位 | F | 新建测试文件（`backend/tests/test_p1r_*.py`、`frontend/src/**/__tests__/p1r-*.test.ts`）、`_lead-logs/p1r-*/**` |

同一文件只允许其所有者编辑；其他人通过消息提接口需求。D/E 不得同时改同一个 store。

## 2. 唯一语义判断：主循环模型 + 工具参数（P1-01）

1. **删除**任何「进入主循环前用关键词/正则/包含匹配判断能否执行」的路径：
   - `navigation.detect_explicit_navigation` / `detect_explicit_topic_creation` 及所有调用点；
   - `turn_orchestrator.begin()` 里的轮前 `enter_topic` 直切与 `ctx.explicit_target` /
     `ctx.explicit_create_name` 生产依赖；
   - `topic_application.TopicApplicationBridge.explicit_requirement` 及
     `_decision_state` 中的 explicit hint。
2. 语义判断唯一位置：本轮主循环模型。它调用**原工具**并在参数里声明来源：
   - `switch_topic(topic_id, reason?, intent="user_request"|"suggestion")`
   - `create_topic(name, reason?, intent="user_request"|"suggestion")`
   - 缺省 / 未知值 = `suggestion`（安全侧）。这是**受控通道**：只有本轮真实
     `tool_call` 携带的值才被消费，不接受客户端字段、历史布尔值、跨轮/跨会话复用。
3. 行为：
   - `user_request` → 校验参数（目标存在 / 名称非空 / 查重冲突）后**直接执行原工具**，
     不弹普通话题确认；
   - `suggestion` → 建立话题申请（复用 `pending_approvals`），用户决定前不执行；
   - 直接执行不跳过名称/目标校验、相似冲突二次确认、其它非话题安全策略。
4. `intent` 只允许出现在两个话题工具的 schema 里；不要扩展到其它工具。
   帮助模型判断的规则集中写在 `prompts.py`（含 7 条反例，见 §7）。
5. 手动点击话题 / 历史选择 / 默认话题 / 引导 / 只读检索仍走各自既有入口，不经过模型。

## 3. 输出边界：阶段门（P1-02）

- 枚举与判定唯一来源：`topic_operation.TopicPhase` / `publishes_formal_answer()`。
- 轮内阶段：`DECISION_OPEN → (AWAITING_CONFIRMATION ⇒ EXECUTING) → CONTEXT_PREPARING → ANSWERABLE`；
  失败：`RECOVERABLE_FAILURE`（不得发布正式回答）。
- **DECISION_OPEN 期间不得发布任何正式正文**（`interim=false` 的 delta、一次性回答、
  正式消息持久化都不行）。实现方式：后端缓冲该次模型响应正文，直到确认集合：
  - 响应含话题工具调用 → 正文**不发布**（既不能当回答，也不改名成过程说明发出去）；
  - 响应不含话题工具调用 → 交付缓冲正文（一次性 `streaming=false`），随后本轮进入
    `ANSWERABLE`，之后的回答调用恢复流式。
- 已批准 → 工具成功 → 上下文就绪后才 `ANSWERABLE`；拒绝 → 立即 `ANSWERABLE`，
  在原话题回答原问题（整轮不结束于「已取消」）。
- `ANSWERABLE` 后本轮归属固定：迟到的新的导航请求不得改变归属（按既有 `_warn_late_tool_calls`
  口径变为可观察警告 + 不执行），已执行操作的重复结果只做幂等处理。
- 适配器差异：原生工具模式与文本兼容模式都必须覆盖；若某适配器只能在完整响应后知道工具集合，
  就整段缓冲并如实记录降级（记录时延取舍）。**不得**靠 `[[QIO]]` 标记假装保证。

## 4. 分步事实与恢复（P1-03）

- `TopicStages` 分开记录：`tool_success / navigation_applied / turn_ownership / context_ready /
  answer_complete`，失败记 `failed_stage / failure`。任何一步失败不得被别的成功覆盖。
- 上下文装配：异常、超时、空结果、不完整结果 → **不得**继续用旧 system 上下文回答；
  **不得**清空待重建记录；进入 `RECOVERABLE_FAILURE`。
- 只有准备成功才消费待重建记录，并把本轮 system / 历史 / 摘要 / 工具状态整体切到真实目标
  （不复制原话题全部历史；保留原问题、操作ID、实际目标、失败记录）。
- 恢复入口：复用 RecoveryInbox / 现有错误恢复通道（`RECOVERY_KIND = "topic_answer_recovery"`）。
  重试只重做未完成步骤：
  - `context_prepare` 失败 → 只重做上下文装配 + 回答生成，**不重新执行** create/switch，
    不新建重复用户消息；
  - `answer` 失败 → 只重做回答；
  - 归属失败 → 显式恢复（重新绑定 + 如实说明），**不**删除已创建话题、不谎称导航没发生。
- 恢复前复核最新位置与操作状态；不得覆盖用户后来的手动选择（版本校验同 P1-04）。
- 最终消息/轮次只有一份正确归属。

## 5. 申请起点快照与导航版本（P1-04）

- 新增单调导航版本：`services/navigation_version.py` + 追加迁移（新表，禁止改历史迁移）。
  - `current(conn) -> int`；`advance(conn, *, source, topic_id, fragment_id, turn_id) -> int`；
  - **原子读改写**在 SQLite 事务内完成；`compare_and_advance(conn, expected) -> bool`（CAS）。
- 推进来源（只有真实位置变化）：用户手动进入/切回话题、选历史片段、改接续位置、
  工具真实导航成功。**不推进**：渲染、重复事件、草稿编辑、仅收起/打开弹窗、无关后台状态。
- 申请 payload 至少保存：`operation_id`、`turn_id`、`user_message_id`、`from_topic_id`、
  `from_fragment_id`、`base_nav_version`、`param_version`、`requires`（editable name 等）。
- 校验三处：确认前（respond 处理）、实际写入前（工具内 CAS）、异步恢复时。
  校验与执行不得留竞态窗口：导航服务在事务内用 `compare_and_advance` 做条件更新，
  条件不满足 → 工具真实失败（`nav_version_conflict`），申请按失效处理。
- 失效 ≠ 拒绝：不写拒绝范围、不自动重弹、不自动跳回旧位置；禁用原确认，保留原问题/草稿/恢复入口。
- 前端按 `approval_id + operation_id + base_nav_version` 消费事件；过期事件不改当前位置、
  不误解锁另一条申请、不覆盖草稿。

## 6. 操作身份：改名 / 查重 / 重试（P1-05）

- 身份 = `operation_id`（稳定）；名称/目标是**带版本的参数**（`param_version`）。
- 弹窗确认改名：把规范化后的真实名称写入同一操作并推进参数版本；执行用该版本。
- 状态：`pending / awaiting_confirmation / approved / executing / executed / context_ready /
  answered / refused / stale / failed`，查重冲突独立记 `conflict_pending / conflict_confirmed /
  conflict_declined`。冲突 ≠ 创建成功 ≠ 整轮结束。
- 同轮防重从「按名称/随便什么」改为：同 `operation_id` + 同 `param_version` 的合法重试
  允许再次进入既有冲突确认流程（`create_topic` 工具内部二次审批），不被「本轮已处理过」拦截；
  不同参数版本 → 需要新的申请。
- 幂等：双击 / 重复确认 / 网络重放 / 超时重试 / 刷新恢复 → 至多消费一次；已成功返回既有结果。
  不得通过移除整体同轮防重或无限放行实现。
- 操作ID稳定**不延长有效期**；收起/重试/刷新不续期。

## 7. 主模型提示词必须包含的规则与反例

集中在 `prompts.py` 一个区块（唯一维护点），至少包含：

| 用户原话 | 正确行为 |
| --- | --- |
| 不要切换到原神话题，继续回答这里的问题 | 不调用话题工具，直接回答 |
| 请不要新建名为「原神」的话题 | 不调用话题工具 |
| 请解释「新建话题」是什么意思，不要执行操作 | 解释，不执行 |
| 把「切换到原神话题」翻译成英文 | 翻译，不执行 |
| 等我确认以后再切过去 | 现在不执行（可以说明会等确认） |
| 切到原神。不，还是留在这里 | 不执行（改口后的最终意图是留下） |
| 我想玩会原神 | 只表明讨论内容，不当然等于导航要求 |
| 有明确上下文的「同意，切过去」 | `intent="user_request"`，直接执行 |
| 指向不清楚的「那个」 | 不直接执行：澄清或按 `suggestion` 走申请 |
| QIO 自己认为该拆题/换题 | `intent="suggestion"`，先申请 |

其它规则：引用/否定/条件/改口/指代不算确认；解释文字不等于用户确认；
不确定目标或时机时不得直接执行；仅讨论主题不一定值得拆题。

## 8. 前端（D/C/E 配合，不重做布局）

必须保留/实现：自动展开 + 中央弹窗互斥排队；内容只显示当前话题、目标话题或可编辑新名称、
简短原因（不显示原问题/摘要/预览）；主操作「转到并回答」/「新建并回答」；次操作
「在当前话题回答」；关闭=仅收起并保留名称；收起后从既有入口重开同一申请（不反复自动弹）；
等待期间可写草稿、该对话发送按钮与快捷发送不可用、不锁死整个应用；确认后草稿跟随真实目标
但不自动发送；拒绝后草稿保留在原话题；目标话题已有草稿保留不覆盖、禁止通配跟随；
只有后端进入可回答阶段才展示正文；上下文准备失败显示真实错误 + 重试入口（保留原问题/草稿/
已成功话题操作）；申请失效 → 原确认不可执行、明确说明、保留恢复入口、不自动覆盖新选择；
改名贯穿关闭/恢复/失败/查重。

## 9. 验证计划

- 每个角色只跑自己范围的定向测试；F 负责独立回归与交叉评审。
- 定向测试矩阵：见各任务 description 与原始提示词第 6 节。
- 语义分支用 fake/mock 模型做确定性测试（证明接线与分支衔接），**不**声称真实模型准确率。
- 前端：定向交互测试 + `npx vue-tsc --noEmit` + 必要构建 + 实际界面检查
  （等待 / 收起恢复 / 重建失败 / 申请失效 / 改名后冲突 五种状态，截图存 `_lead-logs/p1r-ui/`）。
- 真实模型语义能力未验证时如实注明。

## 10. 集成与最终验证

- Lead 集成后运行定向回归（受影响文件集合），冻结最终受测 SHA，然后**一次**本地完整批次
  （`python scripts/run_backend_suite.py` + 前端全量 + 必要评测）和**一次** CI。
- 子智能体不得触发 CI / 全量 / 推送中间分支。
- 台账：`_lead-logs/p1r-20261110/`（命令、开始时间、日志、JUnit、退出码、CI run 链接）。
