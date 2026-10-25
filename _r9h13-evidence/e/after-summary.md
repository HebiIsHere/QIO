# R9-H13 E 修复后证据（F1–F6 转绿 + 邻近回归）

- 分支 `r9h13/e`；最终实现提交见 `git log --oneline r9h13/e`
  （基线 `e68fb9b7`；引擎修复由 Lead 在 `r9h13/main` 提供并在本分支合并：
  `f9143105` 逐动作逐对象 scope 核对/可委派范围、`f641d86f`+`dce12204` 有效 allow 记录接口）。
- 定向命令与结果：
  * `pytest tests/test_r9h13_e_subagent.py -p no:cacheprovider` → **13 passed**
    （`after-F1-F6-green.txt`）。
  * 邻近回归（subagent/scope/审批授权/e2e）：
    `test_subagent_scope.py test_subagent.py test_subagent_integration.py
    test_p4_d_subtask_control.py test_p4_d_subtask_lifecycle.py
    test_p7_subagent_race.py test_rm_d_m09_budget.py test_authorization_engine.py
    test_r9h4_engine_binding.py test_approved_operations.py test_r9f_approved_replay.py
    test_r9h13_lead.py test_r9g_acceptance_e2e.py test_r9h11_cross_operation.py
    test_r9h12_cross_operation.py` → **228 passed, 2 warnings**
    （`after-regression.txt`；warning 是既有测试 fake reviewer 返回协程的历史告警，
    不是本轮失败）。

## 逐问题根因 → 修改位置 → 状态（真实行为验收）

| 问题 | 基线事实（红） | 修改位置 | 修复后事实（绿） |
| --- | --- | --- | --- |
| F1 只有写权限仍能读 | `_scope_limited` READ 分支只比对象路径，不查 grant.operations | `security/engine.py::_scope_limited`（Lead `f9143105`：READ 逐 (operation=read, path) 用 `grant.allows("read", ...)`） | `test_f1_write_only_grant_does_not_authorize_read`：write-only 子范围读 → DENY；显式 read 授权对象读 → ALLOW |
| F2 撤销后仍能读 | READ 分支不走 `grant.allows`（撤销标记不生效）；授权记录撤销后已形成子范围仍可读 | 同上（`Grant.is_active` 含 revoked/active_check）+ `security/instructions.py::Grant.allows/is_active`、`security/engine.py` 内部数据顺序 | `test_f2_revoked_parent_grant_stops_child_read`（撤销前 ALLOW → 撤销后 DENY）；`test_f2_authorization_record_revocation_stops_child_read`（授权记录 revoke 后已形成子范围的读取立即 DENY） |
| F3 空文件子范围在 unrestricted 读内部日志 | `_verdict` 内部数据分支在 scope 检查之前，unrestricted 直接 ALLOW | `security/engine.py::_verdict`（Lead：scope 检查移到内部数据分支前；`_is_app_managed` 豁免保留） | `test_f3_child_scope_bounds_internal_data_read_in_unrestricted`：主智能体无 scope 读内部日志 ALLOW（规则不变）；子范围读内部日志 DENY；空文件授权不覆盖普通报告 |
| F4 双动作已授权仍被拒 | 把 `targets`+`limit_impacts` 拍平后用 effect 级 `("execute",)` 核对所有路径 | `security/engine.py::_scope_impacts/_grant_covers_impact/_scope_coverage`（Lead） | `test_f4_compound_delete_allowed_when_each_action_and_object_granted`：execute 程序 + delete 文件 → ALLOW，且不重复审查；单独运行/单独删除分别 ALLOW；换成未授权对象 → DENY |
| F5 仅 execute 却能删除 | 同上：删除影响被按 execute 授权核对 | 同上（逐 impact.operation 核对） | `test_f5_execute_only_does_not_authorize_delete_impact`：execute-only → 含 delete 影响的复合 effect DENY；无删除影响的 execute 仍 ALLOW |
| F6 模型审查授权不形成可委派范围 | ALLOW 只写 AuthorizationLedger，不进 instruction grants → 子 scope 为空；结果只有一句错误文字 | `security/engine.py::child_scope/_delegable_grants`（Lead，当前版本/未撤销/未过期 allow 记录 → verified grant + 同对象只读子 grant）；`security/instructions.py`（Grant 边界字段/`bind_verified_grants`/`describe_scope`/`scope_covers`/`ScopeRequestLedger`）；`security/tools/subagent_tool.py`（子范围形成与结构化委派事实、`request_agent_scope` 工具、撤销实时核对）；`security/review.py`（`record_is_active`/`delegable_record_facts` 边界映射） | `test_f6_verified_review_authorization_forms_delegable_child_scope`：子 scope 含 verified write/read grant，对象/要求版本/用途/指纹/限制齐全；子任务读写已核实对象 ALLOW（不重复审查），其他对象/动作 DENY；结果带 `agent_scope.delegation`。`test_f6_boundary_other_object_and_action_denied`、`test_f6_authorization_stays_bound_to_its_requirement`（换新要求不继承旧授权）、`test_f6_revoked_or_stale_authorization_not_delegated`（revoke → revoked；要求改版 → requested/superseded 且不委派旧记录）、`test_f6_child_request_grant_retry_revoke_structured`（真实 AgentLoop + 真实 `request_agent_scope`：requested → granted（重试）→ revoked）、`test_f6_read_only_escalates_to_user_not_silent_grant`（主智能体也无权限 → escalation=user，不静默放宽）、`test_f6_empty_scope_research_subtask_still_runs`（研究型子任务不被无故拒绝） |

## 结构化状态（F6）

`agent_scope.delegation` 与 `scope_requests`（子任务结果 data）包含：
state（granted/requested/revoked/superseded/denied）、requirement_id/version、
mode、purpose、escalation（main_agent/user）、attempts、operations、objects、
每个 grant 的 source/operations/objects/purpose/fingerprint/limits/source_record_id、
revocable。子智能体权限不足时调用 `request_agent_scope`（真实工具，登记到
`ScopeRequestLedger`），主智能体先核对当前要求/模式/已核实授权；主智能体也无权限
时升级为 user（read_only）。

## 旧期望修订

1. `F6` 初版测试曾期望"授权记录撤销后仍创建子任务并由子任务自己失败"；按契约
   "子权限不足先向主智能体申请、结构化状态"，改为：撤销后仍创建（研究型子任务
   不得因无范围被拒），但委派状态为 `revoked`、子任务真实动作被拒、申请状态
   revoked；旧记录的授权不进入新范围。
2. `F6` 初版测试曾断言"要求修正后子 scope state 必须 superseded"；实际修正会
   产生**新要求 id**，新要求没有任何已核实授权，因此按事实为 `requested`
   （旧授权不继承、不伪造 superseded）；断言改为 requested + grants 为空 +
   旧版本动作 DENY。
3. 其他用例期望未放宽：F1/F2/F3 的拒绝、F4 的允许、F5 的拒绝均为真实行为断言。

## 已知限制

- `ScopeRequestLedger` 是进程内结构化状态（不新增迁移、不落库），重启后申请
  状态不保留；跨重启的持久化边界见契约（本轮不要求）。
- 审批 payload 重建子 scope 时保留 grant 的操作/对象/要求版本/指纹等事实，但
  实时撤销核对闭包（active_check）不可序列化；重建后依赖提交时的 revoked 快照
  （审批执行器另有模式/对象/限制重核对）。
- unrestricted 模式下没有父级显式 grant/已核实授权时，子范围仍为空（F3 要求
  不能绕过子范围），结构化申请 escalation=user；不会自动放宽成"全盘通配"。
- 不新增沙箱/网络/后台监听；子智能体只获得当前要求版本下的明确子集。
