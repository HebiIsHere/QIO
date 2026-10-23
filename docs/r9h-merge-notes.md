# R9-H 合并与验收材料（审批与授权第三轮修复）

> 基线：r9g/main = 60208e325679303a545f1cac47bef2f0edb56b21（开工 fetch 核对：远端一致、工作区干净）
> 修复分支：r9h/main（worktree D:/qio-dev/qio-r9h-main）；契约 docs/r9h-contracts.md；状态 docs/status.md P76
> 本轮不合并 main、不发布安装包；不新增运行期沙箱/文件范围隔离/网络隔离/子进程隔离。

## 1. 基线与集成

| 项 | 值 |
| --- | --- |
| 基线 | 60208e325679303a545f1cac47bef2f0edb56b21 |
| 修复分支 | r9h/main（origin 已推送） |
| 契约/引擎预接线 | 39c0f48e（契约 + 引擎内容字段/名称清理/结构化限制读取预接线） |
| 子分支 | A r9h/a-binding = f954c6b0；B r9h/b-content = 384511c3 + dc287d87；C r9h/c-limits = e889dd8c；D r9h/d-verify = cd9a6a7e |
| Lead 集成 | 2d0b4693（引擎测试适配）→ c363fa33（合入 D）→ 97a3918e（合入 A/C）→ 7f75348b（合入 B 核心）→ ccbd0ab9（合入 B 修复）→ 最终提交见 CI 记录 |
| 最终代码 SHA | bb260107f61f47a2874296de84f4a29f140f7959（完整 CI 验证；其后仅 docs 更新提交 [skip ci]） |

## 2. 四个问题：根因、修改、真实流程验证

### 2.1 要求解析成功后主流程拿不到绑定（A）
- 根因：app._execute_turn 读取 applied.requirement_id / applied.id（不存在）；AppliedResolution.requirement 才是真实要求；成功路径不触发回退登记，导致 requirement_binding=None。
- 修改：app._execute_turn 改读 AppliedResolution.requirement；new/amend 通过 requirements.binding_for 取有效版本并核对版本；withdraw 不绑定已撤回要求；绑定建立失败记录日志/轮 notices/requirement_resolutions 审计并标 unavailable，不回退登记、不用最近一条 active；apply_resolution 传真实原话 message=instruction.text、message_id/turn_id/topic_id；resolution.py 增加 removed_limits 与 extract_limits（历史 legacy 限制重新理解），模型 corrections 不替代原话。
- 验证：tests/test_r9h_binding_flow.py 22 项，全部通过真实 _execute_turn、真实台账/版本绑定（fake resolver/可观察编排器）；覆盖 new/amend/withdraw、原话逐字、corrections 改写、来源标识、绑定失败、解析失败、取消不触发后续操作。

### 2.2 内容变化仍可能复用旧授权（B + Lead 引擎）
- 根因：ReviewOperation/operation_fingerprint 已有 content_digest/precondition_digest/spec_digest/delete_mode/conditions，但 engine._review_operation 未从真实 effect 传入；工具也未生产这些事实。
- 修改：engine._review_operation 读取 effect.detail（content_digest/content_sha256、precondition_digest/base_content_sha256、spec_digest/execution.spec_digest、script_sha256、delete_mode、conditions）；B 在 fs_write/fs_patch/fs_delete、附件写回/回收、cmd 永久删除、审批执行器生产/校验这些事实与 operation_binding；附件写回修正 content_sha256 语义（新内容），旧形状兼容；审批恢复执行前校验绑定内容，漂移即 invalidated。
- 验证：tests/test_r9h_content_binding.py 16 项（真实工具 + 真实引擎 _review_operation）：content A 允许后 B 不复用、同内容同前置状态复用、前置状态变化不复用、同路径脚本内容变化、内联代码/argv/cwd 变化、fs_delete/附件回收 delete_mode、命令永久删除、批量子项独立绑定、审批内容漂移不执行；test_approved_operations 23 项；tests/test_r9h_engine_wiring.py 4 项验证引擎字段映射/指纹。

### 2.3 名称/后缀仍直接决定权限（C）
- 根因：security/internal_data.py 的 SENSITIVE_DIR_NAMES/SENSITIVE_FILE_NAMES/SENSITIVE_SUFFIXES/is_sensitive_credential_path；services/computer.py 的 REDLINE_NAMES/REDLINE_SUFFIXES/_is_redline；引擎据此拒绝读取/要求确认。
- 修改：删除上述名称词表/函数及调用；内部运行数据只按 AppContext 真实注册路径 + 路径规范化（符号链接/相对路径）识别；computer check_path 只按根内 auto/根外 approve；普通 .key/.pem/.crt、credentials/.config 同名材料按普通规则；改名/别名不绕过真实注册范围；不新增名称词表/正则/全盘扫描/普通读取模型审查。
- 验证：tests/test_r9h_internal_data.py、test_internal_data_guard.py、test_computer_sandbox.py、test_sr_a_computer.py 及 test_authorization_engine 适配断言通过；覆盖普通同名材料不被名称拒绝、真实注册内部数据仍保护、符号链接/相对路径规范化。

### 2.4 结构化限制不能可靠保存/恢复（C + Lead recheck/engine）
- 根因：user_requirements.limits 仍是展平文字；引擎忽略纯文字；执行前重核对重建 AuthContext 丢限制；重启/后台/审批恢复可能放行受限操作。
- 修改：迁移 59 增加 structured_limits TEXT（NULL=历史未知，[]=明确无限制）、structured_limits_state（legacy/structured/unresolved）、structured_limits_source；补偿 60 = 补偿 58 全部语句 + 59（去重）；SCHEMA_VERSION=COMPENSATION_VERSION=60；REQUIRED_OBJECTS 增加三列；security/requirement_limits.py 规范化/序列化/合并；requirements 增加字段与 set_structured_limits/structured_limits_for/legacy_requirements；new/amend/withdraw 持久化、继承、修改、解除、失效；legacy/NULL 不当明确无限制，unrestricted 相关操作不自动放行；RecheckService 重建 AuthContext 时从台账加载限制；A 的 extract_limits 对 legacy 原话重新理解并持久化（失败标 unresolved）。
- 验证：tests/test_r9h_limits.py（真实 sqlite 文件 + 真实迁移 59/60、关闭连接重开恢复、amend 继承/解除、父 legacy/unresolved 不声称完整、withdraw 失效、无关要求不串入、引擎按持久化限制拒绝）；tests/test_r9h_recheck_limits.py 3 项（重核对重载限制、范围外允许、legacy 不当无限制）；迁移套件（main_closeout/p1/p2/p3/irm/append_only/atomicity 等）通过。

## 3. 固定查询兼容性（D）

- 失败复现：tests/test_r9g_fixed_queries.py::test_node_npm_version_forms 在 POSIX 系统安装路径（/usr/local/bin/npm 符号链接、/opt/<...>/bin）返回 None。
- 根因：(a) 系统 bin 目录白名单缺 /opt/<...>/bin；(b) 真实工具传 resolve 后脚本入口（npm-cli.js），入口名丢失。
- 修复：fixed_queries.py 系统 bin 目录 = 精确名单 + /opt/<≥1>/bin 结构；真实目标必须在系统安装根（/usr、/opt、/snap 等）内；目标是同族脚本（npm-cli.js→npm、pnpm.cjs→pnpm）；resolve 后目标仅在系统根内时按同族脚本推导程序族。工作区/cwd/临时目录同名替身、任意脚本、/opt 任意程序、shell/解释器脚本仍拒绝。
- 验证：本机 Windows 平台 11 项 + 原固定查询 32 项通过；**Linux 定向 CI**：backend-py311 run 39925645309 success、backend-py312 run 39925793535 success（SHA c363fa33，包含 test_r9h_fixed_queries_platform.py 与固定查询/删除/命令测试）。

## 4. 测试与 CI

- 定向回归（本轮，全部真实运行；模型 fake/mock）：
  - 413 项宽集成（R9-H 新增 + 安全/审批/工具/固定查询/名称清理/迁移相关）通过；
  - 271 项受影响套件（迁移/应用/turn/审批/附件/fs/删除/固定查询/安全模式/提示词）通过；
  - 90 项 CI 相关补充（dev workflow/tool restore/legacy cleanup/R9 验收/健康探测）通过（6 skipped 为既有环境跳过）；
  - A 22 项、B 16 项、C 新测试/迁移套件、D 平台测试均在本分支定向通过。
- 全量 pytest：本轮未在本地完成全量（按预算以完整 CI 矩阵为主；不把定向当全量；完整 CI 覆盖三平台全量）。
- 完整 CI（本轮唯一完整矩阵运行）：run 39930482370，SHA bb260107f61f47a2874296de84f4a29f140f7959，结论 success，10/10 jobs 绿
  （docs consistency / frontend / rust×2 / frozen worker / install e2e / backend py3.11 / py3.12 / windows）。
  首次运行失败项均为既有 flake：py3.12 健康探测 746ms>300ms（同 run py3.11 通过）、windows timing 排队断言 864<647（docs/verification-acc-phase2.md 记录）；
  按纪律仅重跑失败 job：py3.12 通过；windows 首次重跑出现已知 sqlite 跨线程 InterfaceError（测试线程直接读共享连接），再次重跑 windows 后通过。
  未新增完整 CI 运行、未删检查/降阈值/放宽断言；不以定向 CI 成功冒充完整 CI 成功。

## 5. 历史数据兼容与边界

- 迁移只追加（59/60），不改历史迁移；旧 user_requirements 行 structured_limits=NULL/state=legacy 不当作“明确无限制”；legacy 可由 extract_limits 按真实原话重新理解并记录 source/state；失败标 unresolved，只对相关操作要求确认，不清空全部授权。
- 旧审批/授权缺 requirement 绑定仍按 invalidated/needs_confirmation 处理，不默认补宽泛授权。
- 已知边界：受限子进程不是安全沙箱；Low IL 只阻止写、不阻止读/网络；解释器内联代码/xargs/sudo/嵌套 shell 等无法结构性约束；conditions 字段已贯通但当前无产生结构化条件的真实工具；move/copy/外发工具不存在，未伪造来源/目的地；批量附件部分子项失效 E2E 未覆盖；安装版未复验；真实模型重新理解依赖模型可用。

## 6. 合并准备

- 分支可合入 r9g/main 之上；未合并 main；未发布。
- 完整 CI 结果与最终代码 SHA 见最终报告/运行记录；若预算用尽仍未取得完整 CI 成功，会如实标未完成。
