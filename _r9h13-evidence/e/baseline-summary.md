# R9-H13 E 基线红证据（F1–F6）

- worktree: D:\qio-dev\qio-r9h13-e（分支 r9h13/e）
- 基线: 49ba218a（r9h12/main e68fb9b7 + Lead 49ba218a，未含 E 修复）
- 命令: cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_r9h13_e_subagent.py -q -p no:cacheprovider
- 结果: 9 failed, 1 passed（F6 对象/动作边界相邻用例在基线也拒绝，属相邻正常拒绝用例）
- 真实/fake 分界: 真实 AuthorizationEngine / RequirementLedger / AuthorizationLedger /
  InstructionRegistry / ApprovalService / OperationRegistry / SettingsStore / SubagentTool /
  TaskManager / 临时文件；模型 fake（FakeReviewer 与脚本化 adapter）。
- 逐问题基线事实（详见 baseline-F1-F6-red.txt）:
  * F1 write-only grant 的子范围 : READ → ALLOW（应 DENY）。
  * F2 父 grant 撤销后 : READ → ALLOW（应 DENY）；模型审查记录撤销后已形成子范围 : READ → 仍 DENY
    是因为基线根本没有形成子范围（F6），撤销状态未进入读取判定。
  * F3 unrestricted + 内部日志 : 子范围 READ → ALLOW（应 DENY，内部数据/模式不能绕过子范围）。
  * F4 execute 程序 + delete 指定文件同时授权 : 复合 EXECUTE effect → DENY（应 ALLOW）。
  * F5 仅 execute 路径授权 : 同一复合 effect（含 delete 影响）→ ALLOW（应 DENY）。
  * F6 模型审查 ALLOW : 子 scope 无 verified grant → 正常子任务读写被拒；结果无 delegation /
    scope_requests 结构化字段；request_agent_scope 未注册（loop 日志可见）。
