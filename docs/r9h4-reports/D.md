# R9-H4 D 交付报告：程序/脚本启动、审查后内容核对、进程操作

- 分支：r9h4/d（worktree D:/qio-dev/qio-r9h4-d，基线 67fa88c6）
- 独占文件：backend/src/agent/tools/cmd_tools.py、backend/src/agent/security/launch.py、
  backend/tests/test_r9h4_cmd_program.py（新增）、docs/r9h4-reports/D.md
- 纪律：未改 engine/app/approved_operations/security_gate 等共享文件；未 push/merge；
  **未触发完整 CI / 全量矩阵**（额度已用尽），只跑定向文件；模型零调用、未操作真实用户进程。

## 1. 实现（契约 §2/§3/§6）

### 1.1 命令/脚本 effect 的真实 limit_impacts（§2/§3-D）

新增 security/launch.py 单一构造器（工具/测试/审计复用，不再各写一套）：

- build_limit_impacts(facts, delete_intent=..., shell=...)：返回契约形状的
  [{operation, objects:[{kind,value}]}]
  * execute 影响 → objects=[{kind:"program", value: resolve 后的真实程序路径}]
    （shell 命令取 resolve 后的 COMSPEC/sh，不是 "shell" 占位）；
  * delete 影响 → objects=[{kind:"path", value: 已解析删除对象路径}]
    （入口是 execute / run_program / run_shell 时也保留删除影响，不因 effect.kind 丢失）。
- build_launch_facts(spec)：审查/审批/执行三方共用的可比较事实（kind/program_path/
  program_sha256/argv/cwd/shell/shell_cmd/script_sha256/argument_files/spec_digest）。

cmd_tools.py 接线：

- _CmdTool._limit_impacts / _launch_facts：与 launch 同一形状（只做字段投影）。
- _execute_effect：effect.detail 增加 limit_impacts 与 launch_facts；base effect 的
  program target 改为 resolve 后程序路径（shell 也取真实 shell 程序）；detail.cwd 用
  resolve 后 cwd。删除对象仍同时保留为 effect path target（既有规则不变）。
- RunProgramTool / RunCmdTool：payload 增加 limit_impacts（审批展示/绑定）；
  _pending_facts（审批指纹）增加 limit_impacts 与 launch_facts，使审批/授权记录绑定
  "实际影响 + 真实启动事实"，而不是只看 effect.kind。
- proc_kill：保持既有规则（managed pid 判定 + 模式/授权），effect 形状未改。

### 1.2 审查后内容核对（§6）

- 审批/授权时 fingerprint.launch_facts 与 spec_digest 覆盖：程序路径 + 程序内容 sha256 +
  argv + cwd + shell 命令 + 脚本内容摘要（argument_files）。
- recheck_approved_spec 增强：指纹里存有 launch_facts 时，执行前逐项比较
  kind/program_path/program_sha256/argv/cwd/shell/shell_cmd/script_sha256，不一致给出
  精确原因（字段名）并 invalidated，不执行；不再只报笼统"摘要不一致"。
- 真实链路验证（测试）：脚本或程序文件在**审批等待期间**被替换 → 执行器 invalidated、
  没有任何标记文件产生（见 §2 测试 9/10）。

### 1.3 固定查询 / 普通读取

固定查询不受影响：只读下 python --version 仍直接允许，limit_impacts 只含 execute；
普通读取未改。

## 2. 测试与结果（真实 cmd/launch/engine，全部定向）

新增 backend/tests/test_r9h4_cmd_program.py（13 tests：11 通过 + 2 待 B 合入 xfail）：

| # | 用例 | 断言要点 | 结果 |
| --- | --- | --- | --- |
| 1 | test_run_program_effect_has_resolved_program_impact | limit_impacts.execute 的 program 值 = execution.program_path = effect program target | 通过 |
| 2 | test_run_program_delete_command_keeps_delete_impact | run_program 删除命令（POSIX rm / Windows cmd del）保留 delete 影响与 path target | 通过 |
| 3 | test_run_shell_delete_command_has_delete_and_execute_impacts | shell 入口同时有 execute（真实 shell 程序）与 delete（victim path）影响；payload 绑定 | 通过 |
| 4 | test_effect_launch_facts_match_real_spec | detail.launch_facts 与 build_launch_facts(spec) 逐字段一致（含 spec_digest 64 位） | 通过 |
| 5 | test_approval_fingerprint_binds_impacts_and_launch_facts | 审批指纹含 limit_impacts/launch_facts，spec_digest 一致 | 通过 |
| 6 | test_fixed_query_still_allowed_with_impacts | 只读固定查询仍允许；影响只有 execute | 通过 |
| 7 | test_recheck_rejects_script_content_change | 脚本内容变化 → recheck_approved_spec False | 通过 |
| 8 | test_recheck_rejects_program_content_change | 程序文件内容变化（复制 cmd/sh 后追加字节）→ False | 通过 |
| 9 | test_approval_wait_script_change_never_executes | 等待期换脚本 → execute_proc_run_program invalidated，标记文件不存在 | 通过 |
| 10 | test_approval_wait_program_change_never_executes | 等待期换程序内容 → invalidated，标记文件不存在 | 通过 |
| 11 | test_proc_kill_read_only_denied_without_real_process_operation | 只读结束进程被拒绝，未执行真实 kill | 通过 |
| 12 | test_user_limit_on_program_blocks_real_execution | 用户限制 program（真实 resolve 路径）→ 引擎 DENY 且工具不启动 | xfail（待 B） |
| 13 | test_user_limit_on_delete_object_blocks_delete_command | 用户限制 delete 对象 → 引擎 DENY、文件保留 | xfail（待 B） |

为什么 12/13 是 xfail（如实）：它们断言的是**引擎侧**消费 limit_impacts 的程序/删除对象匹配
（契约 §3-B）。当前基线 engine._limit_conflict 只按 effect.kind 映射操作名 + path targets 匹配
（engine.py:1096-1146），还没有读 limit_impacts / program 对象；该改动属 B/Lead 独占
（engine.py）。D 侧的事实（limit_impacts/launch_facts）已经就绪，B 合入后这两条应转绿
（届时把 strict 打开即可）。

回归（受影响既有套件，定向）：

~~~powershell
cd D:/qio-dev/qio-r9h4-d/backend
uv run --frozen pytest -q tests/test_r9h4_cmd_program.py tests/test_r9g_cmd_binding.py   tests/test_r9g_deletion.py tests/test_r9g_engine_core.py   tests/test_r9f_approved_replay.py tests/test_r9f_exec_boundary.py -p no:warnings
# → 97 passed, 2 xfailed（上面的待 B 用例）, 0 failed；证据 _r9h4-evidence/d-regression.log

uv run --frozen pytest -q tests/test_r9h4_cmd_program.py -p no:warnings -rxX
# → 11 passed, 2 xfailed（待 B）, 0 failed；证据 _r9h4-evidence/d-targeted-run.log
~~~

注：本机为 Windows，脚本替换/程序替换用例在 Windows 真实执行；Linux CI 由同一测试文件覆盖
（sh + 脚本分支）。

## 3. 接线需求（Lead / 共享文件）

1. **B/Lead engine.py**：_limit_conflict 需消费 effect.detail.limit_impacts：
   base operation（effect.kind 映射）+ limit_impacts 全部 operation 都要检查，并把 kind=program
   的对象与 resolve 后程序路径匹配（含 shell 的真实 shell 程序）；delete 影响的对象按 path 匹配。
   D 已提供稳定形状的事实与测试 12/13 作为验收。
2. **Lead effects.py / security_gate.py**：若 Effect/OperationBinding 有独立字段承载影响，请把
   detail.limit_impacts 映射进去（当前已放进 detail + 审批指纹 + payload；执行前重核对可用）。
   注意不要重复解释程序名文本——只做结构事实透传。
3. **F/Lead approved_operations.py**：proc.run_program/proc.shell 重放已能消费
   fingerprint.launch_facts（本报告 §1.2）；**proc.kill** 仍按 pid 直接执行（契约 §10.2 要求
   传递原审批快照/操作依据），D 侧 effect 只有 pid/managed，未新增通道——需 F/Lead 在执行器侧补。
4. 不需要 D 修改 engine/app；若上述接线引入新字段名，请通知 D 更新测试断言。

## 4. 契约偏差 / 未完成 / 未验证

- 无契约接口变更：LaunchSpec/FixedQueryFacts/Effect 结构未变；只在 effect.detail 增加
  limit_impacts/launch_facts（契约新增字段），新增 launch 辅助函数（下划线语义之外的两个公有
  build_limit_impacts/build_launch_facts 供工具/测试复用）。
- 未验证：Linux 真实文件系统上的程序替换分支（本机 Windows 通过；CI 由同一用例覆盖）；
  proc_kill 对非 managed 的真实进程终结（安全起见未执行，只验证只读拒绝）。
- 未触发完整 CI/全量 pytest（额度纪律）；需要时由 Lead 派发定向 CI，建议 nodeids：
  tests/test_r9h4_cmd_program.py tests/test_r9g_cmd_binding.py tests/test_r9f_approved_replay.py。
- 残余边界：检查与提交不是同一原子操作（契约 §6 允许的 OS 并发边界）；launch 事后
  postflight 仍会标记执行期间被替换（既有能力），执行前核对靠审批指纹 launch_facts/spec_digest。
