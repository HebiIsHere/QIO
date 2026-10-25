# R9-H11 程序对象状态判定与跨操作一致性：合并与验收材料

> 基线 r9h10/main 80b0630874b6749df5ddf90bafc12e2d31f5eced（代码 0a0df3a1f84eec1f0182885efa2af563382a07f4）。
> 修复分支 r9h11/main；共同对象判定契约 docs/r9h11-contract.md；根因 docs/r9h11-root-causes.md；状态 docs/status.md P86。
> 完整 CI 本轮不新增（历史累计 2/2 已用尽）；最终代码同一 SHA 做 Linux py311/py312 与 Windows 定向 CI。

## 1. 基线与提交

| 项 | 值 |
| --- | --- |
| 基线 HEAD | 80b0630874b6749df5ddf90bafc12e2d31f5eced（代码 0a0df3a1f84eec1f0182885efa2af563382a07f4） |
| 修复分支 | r9h11/main |
| 协作分支 | r9h11/a（共同对象状态）、r9h11/b（绑定恢复）、r9h11/c（适配入口/审批恢复）、r9h11/d（独立验收） |
| 工作区 | 独立 worktree：qio-r9h11-main/-a/-b/-c/-d；共享 venv（junction） |
| 最终代码 SHA | d9d806b43b34b35f0b638dfdb6931ab878118594（定向 CI 三目标同一 SHA；其后仅 docs/evidence） |
| 最终 HEAD | 见最终报告（其后仅 docs/evidence 提交） |

## 2. 四项修复

### 2.1 共同对象判定（不再用布尔衔接）

- 新增 `agent/security/object_state.py`：五态 ObjectState（MATCH/UNRELATED/UNBOUND/INVALID/UNKNOWN）、
  结构化 ObjectDecision（state + basis + operation + 限制对象 + facts）、LimitAssessment、
  唯一身份/生命周期比较 `compare_bound_identity`、只读澄清 `resolve_program_name`。
- `AuthorizationEngine.assess_limit_conflict(effect, requirement, *, extra_limits=())` 是唯一聚合入口：
  操作范围先过滤；空 objects=限制全部；逐限制×实际影响 `decide_object`；
  优先级 MATCH > INVALID/UNBOUND > UNKNOWN > UNRELATED；只有**可靠无关**才继续按模式判定。
- `_limit_conflict` 收敛为薄包装；`_program_name_unresolved`/`_program_binding_path_replaced`/
  `_limit_program_matches`/`_limit_path_matches`/`_limit_matches_impact` 的独立布尔判断被删除。
- 拒绝携带 decision_code（program_name_requires_object_binding / program_binding_invalidated /
  object_state_unconfirmed）与 AuthDecision.object_decisions 结构化依据（原因文字只说明）。

### 2.2 问题一：历史无绑定记录仍可通过绝对路径执行或文件读取绕过

- 无绑定 program_name 进入 **UNBOUND**（相关但对象未确定），不是 false：
  名称入口相同、当前 PATH 澄清到同一对象 → UNBOUND；当前无法澄清且候选存在 → UNKNOWN；
  只有候选不存在/不同裸入口名/澄清到别的对象等**可靠依据**才 UNRELATED。
- 专用恢复（B）：structured-but-unbound 走服务层 `_recover_structured_binding_limits`，
  只用后端只读候选事实补绑定；写回走 `apply_binding_recovery` → set_structured_limits 条件写入
  （id/version/status/chain_digest），恢复期间要求修改/撤回/链变化 → 零写入；失败保留原限制/known/来源。
- 保存时解析失败显式记录 `binding_state=unresolved` + `binding_failure` + 有界候选事实（纯数据；
  读取/规范化/反序列化不查找不补建）；历史原始 JSON 在环境变化后字节不变。
- 读取限制在正文/摘要读取前落实：fs.read 正文、write/patch 覆盖前摘要、delete 内容摘要/目录子项摘要
  都先消费同一 assessment，拒绝时 0 次读取；普通批准不能解除 UNBOUND/INVALID/UNKNOWN。

### 2.3 问题二：原路径替换后旧对象硬链接失去读取保护

- 已绑定对象使用**保存身份**（binding.identity/birth_key）与候选对象核对，不再把 binding.path 交给
  路径比较并重新取该路径当前对象身份。
- 原路径替换/删除/移动后，旧对象别名（硬链接/符号链接目标）身份一致 → MATCH，继续受限制；
  原路径上的新对象 identity 不同且路径==binding.path → INVALID（拒绝/恢复，不自动继承或清除禁止）；
  不同对象其它路径 → UNRELATED；身份不可读 → UNKNOWN；候选不存在且非绑定路径 → UNRELATED。
- read/write/delete/execute 与审批恢复共用同一判定；路径型限制保留既定语义（精确路径/前缀/目录、
  两侧当前身份核对硬链接，不做全盘别名扫描，不合并同内容复制件）。

### 2.4 问题三：编号复用/生命周期变化时新程序被放行

- 编号与生命周期按同一身份契约核对：`compare_bound_identity` 同时返回
  same/different_object/different_lifecycle/lifecycle_unverifiable/identity_unavailable；
  匹配与失效检查消费同一结果（不再分别漏查字段）。
- identity 相同、birth 不同 → INVALID（另一个生命周期对象；原路径发生即绑定失效/恢复，
  不视为原对象、也不直接当无关）；保存有 birth、当前取不到 → UNKNOWN（不能默认一致）；
  birth 依据只来自文件身份，禁止内容摘要/mtime/名称相似/调用方自报。
- 身份不可读、编号复用、生命周期缺失、旧对象仍被硬链接引用均有覆盖；拒绝发生在程序启动前。

## 3. 真实流程证据

- 基线（80b06308）独立验收 18 项：3 passed / 15 failed（D，真实工具/台账/引擎/审批/临时文件）；
  其中名称入口/绝对路径/读取正文/摘要/审批恢复/硬链接替换/生命周期差异均复现。
- Lead 复现探针（临时文件，不入库）在基线上 3/3 失败、修复后 3/3 通过：
  无绑定 execute 绝对路径真实执行（marker 写出）→ 停止且 marker 不存在；
  无绑定 read 返回正文字节 → 拒绝且读取次数 0；
  绑定读取限制 + 硬链接 + 原路径替换 → 旧别名读取被拒且读取次数 0。
- 修复后 H11 定向：object_state 91、binding_recovery 15、cross_operation 22、
  D 独立验收 18，合计 146 项（含真实硬链接、真实 PATH 澄清、真实 SQLite/审批、关闭数据库重开恢复）。
- 邻近回归：H4-H10 核心 225；C 上述 173/210/59；B 连带 245；Lead 合并后关键 261 全绿。
- 宽回归：见 §4 运行记录。
- 模拟/真实分界：真实 file_id 复用本机无法稳定复现（删除重建 40/40 交集 0，单文件删除重建 identity 变化），
  「同编号不同生命周期 / 生命周期缺失 / 身份不可读」在 `agent.services.file_identity.identity_of`
  身份提供器边界注入，用例 docstring 标「模拟」；真实硬链接/替换/删除/移动/绝对路径/PATH 变化为真实验证。

## 4. 测试命令与结果

离线、无外部连接；模型一律 fake/mock；venv 为共享环境的 worktree junction。

```
cd <worktree>/backend
# H11 新增（A/B/C/D）
& .\.venv\Scripts\python.exe -m pytest tests/test_r9h11_object_state.py tests/test_r9h11_binding_recovery.py tests/test_r9h11_cross_operation.py tests/test_r9h11_regressions.py -q -p no:cacheprovider
# 邻近核心（H4-H10）
& .\.venv\Scripts\python.exe -m pytest tests/test_r9h4_engine_binding.py tests/test_r9h4_limits_semantics.py tests/test_r9h5_regressions2.py tests/test_r9h6_regressions.py tests/test_r9h7_regressions.py tests/test_r9h8_regressions.py tests/test_r9h9_regressions.py tests/test_r9h10_regressions.py -q -p no:cacheprovider
# 宽回归（71 个受影响/邻近文件，含 r9 全系、authorization/approval/tools/fs/app/turn 装配）
& .\.venv\Scripts\python.exe -m pytest @files -q -p no:cacheprovider --junitxml=_lead-wide-junit.xml
# 文档一致性（stdlib）
& .\backend\.venv\Scripts\python.exe scripts\check_docs.py; & .\backend\.venv\Scripts\python.exe scripts\check_docs.py --selftest
# 运行时评估（与基线对比）
& .\.venv\Scripts\python.exe -m agent.eval.run
```

| 运行 | 结果 |
| --- | --- |
| H11 新增（object_state 91 / binding_recovery 15 / cross_operation 22 / D 验收 18） | 146 passed |
| 邻近核心 H4-H10（8 个回归文件） | 112 passed |
| 合并后关键（H11 146 + H4-H10 112，最终证据见 _r9h11-evidence/lead/*.xml） | 258 passed |
| 宽回归 71 文件 | 1015 项：1014 passed / 1 failed（`test_r9_content_binding::test_script_content_change_prevents_reuse` 在基线 80b06308 与修复后都以同一 Windows 环境原因失败，非本轮引入）/ 0 skipped |
| Lead 原始复现探针（临时文件，不入库） | 基线 3/3 失败；修复后 3/3 通过 |
| docs 一致性 | 通过（95 个里程碑条目）；--selftest 6 场景符合预期 |
| agent.eval.run | 与基线 80b06308 输出逐字节相同（无回归） |

首轮 Linux py311 定向 CI 在 A 的单元测试 `test_resolve_path_absolute_and_relative` 上暴露平台假设错误
（Windows 反斜杠被当成 POSIX 分隔符的断言）；修正为按平台语义断言（POSIX 反斜杠是文件名字符）后重跑全绿。
生产代码未因此改动。

## 5. 平台真实验证与模拟

| 能力 | 本机（Windows） | 说明 |
| --- | --- | --- |
| 真实硬链接 | 支持并验证 | 旧对象别名读取/执行保护、替换后仍保护 |
| 原路径替换/删除/移动 | 真实 | os.replace/unlink 后旧别名与路径新对象分别核对 |
| 名称入口 vs 绝对路径 | 真实 | 临时程序 + 真实 PATH；无绑定记录两种入口都被停住 |
| 编号复用（同 identity 不同 birth） | 模拟 | 平台不稳定复现；身份提供器边界注入并在测试标注 |
| 生命周期缺失/身份不可读 | 模拟 | 同上；映射到 UNKNOWN（失败关闭） |
| 审批恢复 | 真实 | 真实 SQLite/审批状态机；恢复前重新核对同一 assessment |

## 6. 定向 CI

最终代码 SHA **d9d806b4**（= `d9d806b43b34b35f0b638dfdb6931ab878118594`；其后仅证据/文档提交）。三个目标使用同一 SHA、
同一 pytest 筛选集（H11 四套 + H4-H10 + r9h2 链恢复 + r9h limits/binding flow + fs/cmd/approved_operations）：

| 目标 | run | 结论 |
| --- | --- | --- |
| backend-py311 | 40114369702 | success |
| backend-py312 | 40114727713 | success |
| windows | 40114932045 | success |

同一 SHA 前一次 py311（3215f2ca，run 40114085555）因上述平台假设测试失败，修正测试后按新 SHA 重跑成功；
同一 SHA 的 windows（3215f2ca，run 40114096592）亦为 success（代码与最终 SHA 相同，仅测试断言不同）。
每轮只重跑了必要项；未新增完整 CI（历史累计 2/2 已用尽）。

## 7. 边界与已知限制

- 绑定创建依赖保存时平台程序查找与文件身份读取能力；保存失败显式 unresolved，恢复依赖当前环境候选事实。
- 当前环境无法澄清名称且候选对象存在时按 UNKNOWN 失败关闭（恢复后可继续）；不同入口名（裸名）是可靠无关依据。
- 生命周期依据来自 OS；文件系统不提供 birth 时只能按弱依据匹配并标注。
- 执行前核对到系统调用仍有竞态；真实回收站/安装版/真实模型/完整矩阵未验证。
- 审批预检查行只持久化 reason（执行器阶段 detail 带结构化码）；读取/执行边界命中即拒绝时不生成 AuthDecision
  审计行（ToolResult.data 携带 object_decisions），普通批准不能解除。
- 完整 CI 未新增（历史 2/2 已用尽）；不合并 main、不发布安装包。
