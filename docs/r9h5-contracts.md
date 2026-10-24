# R9-H5 契约：六项真实流程补修

> 基线：r9h4/main HEAD = d2403425490fdb82555cf66bac48d708d29e8f6c（上轮定向 CI 代码 76e6248b；已 fetch 核对无后续改动）
> 修复分支 r9h5/main；完整 CI 本轮不新增（历史 2/2 已用尽），只做同一 SHA 的 Linux py311/py312/Windows 定向 CI。
> 只修审批/授权/限制执行/操作一致性；不新增网络、沙箱、文件代理、全盘扫描、后台监听、UI 或语言风格。

## 1. 原则

1. 已知限制三模式优先；模型允许、历史授权、人工批准、内部数据三次确认不能覆盖。
2. unrestricted 不调用普通独立授权审查；专用限制恢复不得变相判断操作授权。
3. restricted 保留独立审查；read_only 保留只读规则。
4. 普通读取/固定查询/永久删除/内部数据三次确认/正常可信内部写入保持现有契约。
5. 单次人工批准只完成绑定具体操作，不清未知限制、不扩大范围、不形成可随意复用授权。
6. 授权决策不得依赖自然语言/界面文案/中文原因/文件名/后缀关键词。
7. 批量逐项核对；单项失效不自动使其他独立有效项失效。

## 2. 六项问题与接口

### 问题一：模型审查等待期间文件变化，fs_write/fs_patch 仍覆盖
- 根因：RecheckService 路径提前返回，跳过真实磁盘身份/内容核对；fs_write 未把 base 内容作为 expected；新文件 absent 未核对。
- 修复（A：fs_tools.py）：recheck_before_execute 无论走 RecheckService 还是回退，都用当前磁盘核对 expected identity/base content/absent；fs_write 传 base_content_sha/absent；fs_patch 传 base content；服务 ok 但本地前置不匹配明确 invalidated；人工审批/模型等待/直接执行同规则。
- 测试真实临时文件：write/patch 等待模型/人工期间修改；新增/删除/替换；未变化正常执行。

### 问题二：模型审查期间脚本替换，直接执行新脚本
- 根因：直接模型允许后按当前脚本重新采集事实并执行，旧审查事实被替换。
- 修复（B：cmd_tools.py、launch.py）：审查前采集 launch facts/spec/script digest 作为绑定；执行前按当前真实事实比较；变化 invalidated，不继承旧批准；直接允许/人工恢复/重复重核一致；不新增沙箱。
- 测试真实替换临时脚本（解释器脚本/直接脚本入口/未变化/人工恢复）。

### 问题三：禁止读取时 fs_patch 先读正文再只按 write 授权
- 根因：fs_patch 先读文件再构造 write effect；读取限制未执行。
- 修复（A：fs_tools.py）：读取前构造真实 read effect 并授权；被拒则不读、不算补丁、不修改、不返回正文；读写分别核对；允许写不等于允许读；同根因有限检查。
- 测试：禁读允许写、允许读禁写、读写均允许、三模式；证明读取未发生（注入读取缝/记录读取次数），不能只看最终文件。

### 问题四：批量审批一个子项失效导致整批不执行
- 根因：approval 在逐项执行前整体 authorize_started，B 新增限制使整批 invalidated；执行器内部又按整批 effect 重核。
- 修复（C：tools/approval.py、storage/approvals.py；Lead/D 提供 operations/执行器接口）：区分整批共同条件（要求撤回、审批过期、模式、确认次数）与仅影响子项条件（单项限制/内容变化）；共同失败终止整批，子项条件只失效子项；每项独立结果，总体部分成功；不加入审批外对象；不绕过依赖/确认/过期/单次执行。
- 测试真实工具创建批量审批→真实 decide→后台执行恢复：单项禁止、单项内容变化、全部有效、全部失效、共同要求撤回；临时回收实现，明确非真实系统回收站验收。

### 问题五：单次审批是否完成由中文 reason 关键词决定
- 根因：operations 的 _approve_completes_unresolved 检查 reason 固定中文。
- 修复（Lead：effects.py；D：engine.py、operations.py）：AuthDecision 增加结构化 decision_code（unresolved_limits_requires_confirmation），引擎按结构事实设置；operations 按 code/结构化状态判断，不解析 reason；旧记录无 code 不得按中文推断权限，失效/重新确认；即使有 code 仍核对限制/要求/版本/对象/内容/期限/确认；批准只完成本次，保留 unresolved/known limits。
- 测试：换中文/空 reason 行为一致；相同中文不能使其他 APPROVE 可执行；已知禁止拒绝；重启/恢复保留依据。

### 问题六：禁止明确路径程序扩大为禁止同名程序
- 根因：_limit_program_matches 完整路径不等后回退 basename/stem/小写。
- 修复（D：engine.py；A：requirement_limits 如支持名称类型）：路径限制只按平台规范化/对象身份，不回退同名；若支持名称限制，解析成明确结构化 kind（program_name），执行只按该 kind；不新增名称词表/后缀词表；历史含义不明按 unresolved。
- 测试真实两个不同内容临时程序：同名不同目录、不同后缀同名主体、平台大小写、规范化同一目标、链接别名（平台支持）、明确路径与明确名称区别。

## 3. 文件归属

| 分支 | 负责 | 独占文件 |
| --- | --- | --- |
| r9h5/main | Lead：共享接口/集成/effects/security_gate/app/approved_operations/迁移/docs | services/app.py、tools/approved_operations.py、security/effects.py、tools/security_gate.py、storage/migration_r9h5_*.py、storage/schema.py、storage/migrate.py、docs/status、docs/r9h5-*、集成测试 |
| r9h5/a | A：文件写入前置/patch 读取 | tools/fs_tools.py、tests/test_r9h5_fs_write_read.py、docs/r9h5-reports/A.md |
| r9h5/b | B：程序/脚本审查事实 | tools/cmd_tools.py、security/launch.py、tests/test_r9h5_script_binding.py、docs/r9h5-reports/B.md |
| r9h5/c | C：批量审批恢复逐项 | tools/approval.py、storage/approvals.py、tests/test_r9h5_batch_approval.py、docs/r9h5-reports/C.md |
| r9h5/d | D：结构化决策/程序匹配 | security/engine.py、security/operations.py、tests/test_r9h5_decision_program.py、docs/r9h5-reports/D.md |

- 共享文件只 Lead 改；各组提交接线需求/测试；不能只交无人调用 helper。
- 不删除/skip/xfail/放宽既有测试；不改历史迁移；模型 fake/mock；不触发完整 CI。
- 先复现（测试基线红）再修复；报告列修复前失败命令/修复后通过。

## 4. 交付

- 更新 docs/status.md（P80）、docs/r9h5-merge-notes.md，区分已修复/已验证/未验证/边界。
- 最终中文报告：基线/分支/代码 SHA/HEAD；六项根因/修改/前后行为；真实流程证据/命令结果；共享规则复用；迁移/历史兼容；定向 CI 运行号/链接/SHA/结果/重跑；未验证平台/边界；仅文档证据提交说明。
