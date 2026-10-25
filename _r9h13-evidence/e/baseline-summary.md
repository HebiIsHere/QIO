# R9-H13 E 基线红证据（F1–F6）

- worktree: `D:\qio-dev\qio-r9h13-e`（分支 `r9h13/e`）
- 基线: `e68fb9b7`（r9h12/main，生产代码 e4e478b4；未含 Lead 引擎修复与 E 修复）
- 命令: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_r9h13_e_subagent.py -q -p no:cacheprovider`
- 最终基线结果（13 条）：**12 failed, 1 passed**；见 `baseline-final-red.txt`
  （最初的 F1–F6 基线快照见 `baseline-F1-F6-red.txt`：10 条中 9 failed 1 passed，
  当时尚未补 3 条 F6 相邻/边界用例）
- 唯一通过的用例是 F6 相邻拒绝用例 `test_f6_boundary_other_object_and_action_denied`
  （范围外对象/动作在基线也拒绝）；F6 的"允许"侧与结构化事实全部为红。
- 真实/fake 分界：真实 `AuthorizationEngine` / `RequirementLedger` /
  `AuthorizationLedger` / `InstructionRegistry` / `ApprovalService` /
  `OperationRegistry` / `SettingsStore` / `SubagentTool` / `TaskManager` /
  临时文件与 SQLite；模型 fake（`FakeReviewer` 与脚本化 adapter），不联网。
- 基线复现的逐问题事实：
  * F1 只有 write grant 的子范围：READ → ALLOW（应 DENY）。
  * F2 父 grant 撤销后：READ → ALLOW（应 DENY）；模型审查记录撤销后已形成子范围
    的读取仍 ALLOW（撤销状态未进入读取判定）。
  * F3 unrestricted + 内部日志：子范围 READ → ALLOW（应 DENY；内部数据/模式
    不能绕过子范围）。
  * F4 "运行删除程序 + 删除指定文件"同时授权：复合 EXECUTE effect → DENY
    （应 ALLOW，不得无故缩小）。
  * F5 仅 execute 路径授权：同一复合 effect（含 delete 影响）→ ALLOW（应 DENY，
    不能用 execute 覆盖 delete）。
  * F6 模型审查 ALLOW：子 scope 无 verified grant → 正常子任务读写被拒；结果无
    `agent_scope.delegation` / `scope_requests` 结构化字段；`request_agent_scope`
    未注册（AgentLoop 日志："未注册的工具 request_agent_scope"）；要求换绑/撤销/
    只读升级等状态不可观测。
