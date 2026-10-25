# W4 审计：审批/授权判定入口与最终执行点（R9-G）

- 分支：`r9g/w4-verify`，基线 `97777b55`（= r9f/main + R9-G 契约冻结；W1/W2/W3 分支开工时仍在同一起点）
- 方法：静态搜索定位候选 + **逐个读实际调用链**（文件:行号为基线快照实测）；不修改产品代码。
- 结论口径：`现状` = 基线代码事实；`R9-G 要求` = docs/r9g-contracts.md 对应条款；`缺口` = 需要在集成后由对应分支修复的点。

## 1. 判定入口（谁在决定"能不能做"）

| # | 入口（真实调用链） | 现状判定依据 | 现状绑定字段 | R9-G 要求 | 缺口 |
| --- | --- | --- | --- | --- | --- |
| E1 | `tools/fs_tools.py:169`（fs_read/write/patch/list/find/info 共用 `_gateway_permitted`）→ `security_gate.authorize`（`tools/security_gate.py:292`）→ `AuthorizationEngine.authorize`（`security/engine.py:176`） | 固定规则（只读/内部数据/凭据红线/子智能体范围）+ 关键词禁止（`engine.py:793 _user_prohibition`）+ 受限模式模型审查（`engine.py:440 _apply_review`） | Effect(kind/targets/identity/content_sha256/detail.origin)；**无 requirement 版本绑定** | §1/§3：判定依据 = 具体用户要求（版本化绑定）+ 固定规则；不得用文字词/句式 | `_user_prohibition` 关键词必须删；`AuthContext` 无 `requirement` 绑定；engine 用 `requirements.current()`（`engine.py:364`）取"最近一条 active" → 串用风险 |
| E2 | `tools/cmd_tools.py:521/628`（run_program）、`:730`（run_shell）→ `security_gate.authorize` | 同上 + `_readonly_program_proof`（`:442`）只读白名单 + `_permanent_delete_program_reason`（`:77`）/`_permanent_delete_shell_reason`（`:89`）程序名/文本判断 | argv/cwd/risk/shell；审批单 payload program/args/cwd | §2/§4/§5：删除与只读许可都不得用文本判断；固定查询用唯一分类器（真实程序身份+argv） | `_permanent_delete_*` 文本/程序名判断要删；无 `fixed_queries.classify_fixed_query`；引擎未按 effect.detail 重新分类固定查询 |
| E3 | `tools/registry.py:459` → `authorize_tool_invocation`（`engine.py:261`） | 有声明 effect → `authorize_many`；未声明 → 受限 APPROVE / 只读 DENY / 无限制 ALLOW | tool 名 + arguments（仅作 detail） | §6：批量/工具调用逐项判定，未声明不得猜 | 未声明效果的保守 APPROVE 合理；批量逐项（`BatchItem`）未实现 |
| E4 | `tools/subagent_tool.py:429` → `authorize`（SUBAGENT/EXECUTE） | 子智能体范围子集（`engine.py:708 _scope_limited`）+ 模式规则 | AgentScope.grants/depth | §3/§7：子智能体继承父 requirement 绑定，不得改绑 | `child_scope`（`engine.py:131`）只继承 grants，不继承 requirement 绑定 |
| E5 | `tools/tool_envs.py:1117/1661` → `authorize`（INSTALL_DEPS） | 专用环境（`env_scope=tool_env`）ALLOW，其余 APPROVE | target path/packages | §8：应用受控运行与专用环境安装按既有规则 | 无 requirement 绑定 |
| E6 | `services/attachment_access.py:266/309/313/320`（read/write/delete effect）→ `security_gate.authorize` | 同 E1 | attachment id/path/identity/content_sha256 | §7：写回/回收绑定对象身份与内容摘要 | 无 requirement 版本；重核对由 W3 的 fs/附件部分补 |
| E7 | `security/review.py:955 ModelAuthorizationReviewer.review` → `parse_review_outcome` → `validate_outcome`（`:730`）→ `engine._apply_review`（`engine.py:440`） | 模型输出结构校验 + **关键词确定性守卫**（`review.py:493 deterministic_allow_guard`：否定/疑问/条件/引号/材料） | ReviewRequest（requirement 原话/修正/limits/operation 对象/程序/argv/cwd/脚本摘要/接收方/材料）+ request digest | §1/§2：删守卫；ALLOW 只由后端校验结构、来源、对象/操作一致性、范围可落实 | `deterministic_allow_guard` 及 `_ACTION_VERBS/_REFUSAL_PREFIXES/_CONDITIONAL_RE/_QUESTION_TAIL_RE/_QUOTED_RE` 等要删（W1）；审查记录无 `requirement_version` |
| E8 | `engine._apply_review` 复用分支（`engine.py:487` `authorizations.reuse`） | 按 (requirement_id, operation fingerprint) 复用 AuthorizationRecord | fingerprint（含对象/程序/argv/cwd/脚本摘要/接收方/模式） | §7：复用必须绑定 requirement 版本与对象/内容，变化后不得复用 | `authorization_records` 无 `requirement_version`（迁移 57 已加列，代码未写）；复用不校验版本/内容摘要 |
| E9 | `engine._verdict` 固定规则（`engine.py:710`） | 内部数据三次确认（`:715`）、凭据红线（`:736`）、子智能体范围（`:748`）、关键词禁止（`:754`）、三模式（`:758`） | protected paths / effect targets | §8：固定规则保留；关键词删除；应用受控运行用可信类型/路径区分 | `app_managed` 只有 `engine.py:641` 读取、**全仓无生产者**（grep 实测）：任何调用方只要在 detail 里自报 `app_managed=True` 即可在只读模式绕过拒绝 → §8 "不允许任意工具自报标记豁免"未落实 |

## 2. 人工批准 / 恢复入口

| # | 入口（真实调用链） | 现状判定依据 | 现状绑定字段 | R9-G 要求 | 缺口 |
| --- | --- | --- | --- | --- | --- |
| A1 | `api/server.py:3776 decide_approval` → `tools/approval.py:521 decide` → `storage/approvals.py:348 decide`（CAS） | 用户决定 + 状态机（waiting→approved/rejected/...） | approval_id/state/revisions/mode_at_request | §9：10 分钟原始期限、过期/拒绝/撤回不自动启动、执行前重核对 | **实测缺口**：`decide` 的 approve 分支（`storage/approvals.py:391-414`）不检查 `expires_at`；过期只在 `expire_waiting`（`:578`，recover/sweep 调用）与 `list_pending`（`:281`）收口 → 未被收口的过期 waiting 行仍可批准并执行（用例 `test_expired_approval_does_not_execute` 实测红）；无 requirement 版本/对象内容绑定 |
| A2 | `api/server.py:3792 confirm_approval` → `tools/approval.py:544 confirm` → `storage/approvals.py:416 confirm`（CAS 连续 stage） | confirmed_stage 0→1→2→3；stage 必须连续 | stage/required/approval_id | §9：三次确认按序且绑定相同对象/动作/内容 | 确认不校验对象/内容摘要（只校验阶段序号）；"普通一次确认不能替代三次"已有，但绑定不足 |
| A3 | `tools/approval.py:633 _execute_approved`（批准后后台执行） | `ExecutionContext.authorize_started`（`security/operations.py:138`）→ 只检查"当前是否只读"；随后 CAS `claim` + `operations.execute`（`operations.py:178`） | mode_at_request/mode_at_approval/attempt | §7：重新走 engine（模式+要求+授权/审批有效期+对象身份+内容摘要+前置状态+确认次数+委派范围）后才执行 | **authorize_started 只覆盖 read_only 切换**；对象/内容/要求/过期/确认次数均未重核对；没有 `RecheckService` |
| A4 | 重启恢复 `tools/approval.py:602-612`（report：waiting/taken_over/approved/interrupted） | waiting 恢复展示；approved 重新 `_schedule_execute`；executing → `interrupted_unverified`（`approval.py:256`） | 持久化行 + 原到期时间 | §9：重启不续期、不自动重放、恢复时重核对 | 恢复只按行状态调度；无 requirement/对象重核对；approved 行恢复即执行（缺 §7 重核对） |
| A5 | 审批执行器注册 `services/app.py:365-370`：`tool.invoke` + `register_approved_operations`（`approved_operations.py:1169`） | `_ApprovedReplayGateway.authorize`（`approved_operations.py:614`）：只读 DENY；其余按 effect 快照/payload 形状核对 argv/command/cwd/paths | PendingOperation.effect/payload/fingerprint | §5/§7：执行器使用已绑定内容，不重新生成；对象/内容/程序/脚本变化不得沿用 | 形状核对不含：脚本内容摘要（launch spec 有 `recheck_approved_spec`，fs/附件路径没有统一 recheck）、requirement 版本、前置状态、确认次数 |

## 3. 最终执行点（真正产生副作用的地方）

| 执行点 | 代码位置 | 执行前核对 | 缺口 |
| --- | --- | --- | --- |
| fs.write / fs.patch | `approved_operations.py:153/212`（atomic_write_text，写前 `_check_identity`） | identity + 内容摘要 | 无 requirement 版本/前置状态；无统一 RecheckService |
| fs.delete（回收站） | `approved_operations.py:375`；永久删除拦截在 cmd 路径 `cmd_tools.py:600/708` | 回收站移动/失败保留 | 永久删除无结构化 `deletion.detect_delete_intent`；restricted 下"模型 ALLOW 不可替代人工确认"未实现 |
| proc.run_program / proc.shell / proc.kill | `approved_operations.py:488/514/538` → cmd_tools（`_ApprovedReplayGateway`） | argv/command/cwd 形状 + 只读模式 | 无 fixed query 分类；无 requirement 版本；shell 管道/重定向/嵌套未结构化解析 |
| 自建工具 / 脚本 | `security/launch.py`（`recheck_approved_spec:481`）+ `tools/sandbox.py` | spec_digest（程序身份/argv/cwd/脚本内容） | 绑定不含 requirement 版本/前置状态；engine 固定查询分类未接 |
| 子智能体创建 | `approved_operations.py:820 execute_subagent_create` | 父 scope 子集重建 | 不继承 requirement 绑定 |
| 附件读/写回/回收 | `approved_operations.py:583/679/755` | identity/hash（写回） | 无 requirement 版本 |
| tool.invoke（批准后重跑工具） | `services/app.py:768 _execute_approved_tool` → registry | 无（重新走工具管线，工具内部再判） | 可能重复弹审批或绕过绑定（需 W3/Lead 核对） |
| 交互板批量确认 | `api/interactive_intents.py:622 confirm_intents_batch` | 板面/意图版本（interactive 自有） | 与安全批量（`BatchItem`）不是同一路径；§6 批量未实现 |

## 4. 关键词逻辑现状（§2 清除范围）

| 位置 | 现状 | 处置 |
| --- | --- | --- |
| `security/review.py:493 deterministic_allow_guard`（+ `_ACTION_VERBS:344`、`_has_explicit_refusal:451`、`_has_conditional_or_question:475`、`_has_quote_marks:485`、`_text_mentions_operation:434`） | 仍在，`_validate_allow:770` 调用 | W1 删除（契约 §2） |
| `security/engine.py:769 _MUTATION_PROHIBITIONS` / `:782 _DELETE_PROHIBITIONS` / `:783 _EGRESS_PROHIBITIONS` + `:793 _user_prohibition` | 仍在，`_verdict:754` 调用 | 删除；用户限制改由结构化 limits（要求解析结果）承载 |
| `tools/cmd_tools.py:77 _permanent_delete_program_reason` / `:89 _permanent_delete_shell_reason` | 仍在，程序名/文本搜索 | W2 改结构化 `deletion.detect_delete_intent` |
| `security/intent.py`（`extract_grants` 关键词授权） | 仍在；`instructions.begin(extract=True)`（`instructions.py:220`）调用 | 契约 §2：关键词 grant 能力整体删除；调用点同步清理 |
| `services/computer.py` 命令分类 | 结构化分类保留 | 不得用于全文搜索用户文字（现状未见全文搜索，需 W2 复核） |

## 5. 固定查询与永久删除（§4/§5）现状

- `security/fixed_queries.py` **不存在**；唯一近似是 `cmd_tools.py:442 _readonly_program_proof`（只读白名单，工具自报）。
- `security/deletion.py` **不存在**；永久删除只有 cmd 路径的程序名/文本拦截，文件工具默认走回收站。
- 因此：只读模式下"固定查询"与"任意执行"的区分、restricted 下固定查询免审查、永久删除的
  "restricted 必须人工确认（模型 ALLOW 不可替代）/ unrestricted ALLOW / read_only DENY" 均**未实现**（W2 + Lead 接线）。

## 6. 批量、要求绑定、重核对（§3/§6/§7）现状

- `security/requirements.py` 已有 `UserRequirement`/`RequirementLedger`/`AuthorizationLedger`，
  但**没有** `version`/`binding_for`/`candidates`/`apply_resolution`（grep 实测 0 命中）；
  `security/resolution.py`、`security/recheck.py`、`BatchItem` 均不存在。
- engine 仍以 `requirements.current()`（"最近一条 active"）作为审查/禁止判断依据（`engine.py:364`），
  与 §3"操作绑定正确用户要求、不得串用"冲突。
- `AuthorizationRecord` 无 `requirement_version`；迁移 57 已加列（`storage/migration_r9g_requirement_binding.py`），
  代码侧待 W1/Lead 接线。

## 7. 前端审批文案（只读检查，详见 W4.md §4）

- 组件：`frontend/src/components/ApprovalModal.vue`、`ApprovalFacts.vue`（R9-F 复用）等；
  本次只读核对"只展示具体对象/操作/原因/影响/状态、不暴露实现术语"，结论与建议写在 W4.md，不改前端代码。

## 8. 动态复核发现（用例实测，基线 97777b55）

| # | 发现 | 证据（用例） | 影响条款 | 期望修复 |
| --- | --- | --- | --- | --- |
| G1 | **过期审批仍可批准并执行**：`decide(approve)` 不核对 `expires_at`，`_execute_approved` 的 `authorize_started` 也只查只读模式 | `test_expired_approval_does_not_execute`（xfail，实测 state=completed） | §9 期限/不自动启动、§7 执行前重核对 | Lead `recheck.py` + decide/执行前核对有效期 |
| G2 | **自报 `app_managed` 可绕过只读**：`engine._is_app_managed`（`engine.py:639-641`）只读 `effect.detail`，全仓无生产者/可信校验 | `test_app_managed_marker_cannot_bypass_read_only`（xfail） | §8 应用自身运行必须用可信类型/调用路径区分 | Lead：受控操作类型/路径白名单，不接受 detail 自报 |
| G3 | **审批记录被改动后重放仍执行**：`_ApprovedReplayGateway` 的"预期形状"从同一条 `operation_detail` 推导，记录被改时预期与实际一起变 | `test_argv_change_in_approval_record_is_not_executed`（xfail） | §7 执行器使用已绑定内容、变化不得沿用 | Lead：独立于 payload 的 spec_digest/绑定摘要 |
| G4 | **三次确认不绑定对象/内容**：`confirm`（`storage/approvals.py:416+`）只校验阶段序号，不校验对象/内容摘要；内容变化不会重置 stage | `test_confirmations_bind_same_object_and_content`（xfail） | §9 确认按序且绑定相同对象/动作/内容 | Lead/W3：确认记录绑定内容摘要，变化即重置 |

已通过的真实接线（20 项，见 W4.md §2）：三模式矩阵、内部数据三次确认（三模式 + 齐备后执行）、
同名普通文件不误判、无限制不审查、CONFIRM→审批 / DENY→拒绝 / 审查不可用不默认允许、
ALLOW 结构校验（limits/purpose/对象子集/真实要求来源）、不同对象不复用、同指纹复用、
撤回要求不授权、讨论文本不改变 WRITE 判定、脚本内容变化旧许可失效、模式降级拒绝、
重复回调不重复执行、确认阶段必须连续、内部数据执行等。

## 9. 审计方法边界

- 行号为基线 `97777b55` 快照；W1/W2/W3 合入后行号会移动，但入口/执行点结构不变。
- 本审计为静态阅读 + 调用链确认，未运行动态探针；动态行为证据见 `test_r9g_acceptance_e2e.py` 与 W4.md。