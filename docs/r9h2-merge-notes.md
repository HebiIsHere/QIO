# R9-H2 合并与验收材料（第三轮后续补修）

> 基线：r9h/main = 6ee961d293ec9d06390ca36c00fa491b895b4647（代码 bb260107f61f47a2874296de84f4a29f140f7959；完整 CI 39930482370 success）
> 补修分支：r9h2/main（worktree D:/qio-dev/qio-r9h2-main）；契约 docs/r9h2-contracts.md；状态 docs/status.md P77
> 本轮不合并 main、不发布安装包；沿用 R9-H 累计 CI/全量预算。

## 1. 基线与提交

| 项 | 值 |
| --- | --- |
| 审查基线 | 6ee961d2（docs/evidence）；代码 bb260107 |
| 开工核对 | fetch 后 origin/r9h/main 仍为 6ee961d2，无后续提交；工作区干净 |
| 修复分支 | r9h2/main |
| 子分支 | A r9h2/a-chain = e818a8b5；B r9h2/b-state = ed8f8a1f；C r9h2/c-identity = ea255897 |
| 最终代码 SHA | 见最终报告（本文件随代码提交；CI 以最终代码提交为准） |
| 集成 | 契约/迁移 a6732f4c → 迁移纠正测试 fad44f08 → C 合并 96292b38 → A 合并 5867e584 → B 合并 c56d58d8 → 真实流程集成 8884a8d1 → 平台测试修复 ea60595b |

## 2. 三个缺口

### 2.1 历史要求补充后恢复限制只读补充句（A/B）
- 根因：_ensure_legacy_structured_limits 只把当前子要求 text 交给 extract_limits；子要求 structured/[] 后 unrestricted 丢失原限制。
- 修复：A 的 recovery_context 按真实 parent_id 组装根->当前完整链；extract_limits_from_context 让模型基于完整链输出目标版本有效限制；B 的 app 自动恢复、返回后重读核对、expected_* 条件写回、失败保留 known limits/source。
- 验证：A 17 项（真实 _execute_turn/台账/apply_resolution，fake resolver；覆盖 legacy+标题补充、多次补充、解除一个/全部、不相关要求、缺父/环/版本不连续、等待期间 amend/withdraw、失败保留）；Lead tests/test_r9h2_real_flow.py 验证真实 app 自动恢复：完整链进入模型、子要求 structured、B DENY/A ALLOW、source 含 chain/version。

### 2.2 解析失败落成“已确认无限制”（B/Lead 迁移）
- 根因：fallback record_user_message 默认 structured/[]，临时 status unavailable 与持久化状态不一致。
- 修复：fallback 显式 unresolved/source；不再立即重解释；引擎 unresolved 禁复用旧 allow/delete_permanent；unrestricted 相关操作走既有审查的针对性语义判断（targeted_limits_review），审查不可用退回确认；迁移 61 只纠正有失败审计证据的旧记录，补偿 62 只重放 DDL。
- 验证：B 16 项（四类失败/成功空限制/失败后恢复/重启/审批恢复/旧 allow 复用/无关要求）；Lead 真实流程测试验证 targeted review 下 B DENY、无关 A ALLOW、读 ALLOW；迁移纠正测试验证只纠正有证据记录。

### 2.3 内部数据只按路径、硬链接/移动失去保护（C/B/Lead）
- 根因：internal_data 只按规范化路径；无对象身份登记；旧测试把移出注册范围当普通文件。
- 修复：迁移 61 internal_data_identities；InternalDataIdentityStore 以 volume:file_id 登记真实注册文件/目录（有界枚举），原子替换旧身份 retired、新身份登记，路径缺失/身份读失败保留历史登记；internal_data 支持路径 + identity + known_paths；engine/app 启动同步与注入；security_gate 提供身份 helper；普通同名/同内容不同身份不保护。
- 验证：C 测试（真实 sqlite + 临时目录，Windows 硬链接实际执行；相对/规范化/符号链接/硬链接/改名移动/普通同名/同内容不同身份/原子替换/身份读失败/重启/三模式）；Lead 真实 app 测试验证硬链接与移动后 read_only DENY、write 三次确认；Linux 定向 CI 通过。

## 3. 定向验证

- A：17 passed；B：16 passed；C：身份/内部数据套件通过；Lead 真实流程 3 passed；迁移纠正 1 passed。
- 宽回归：382 passed（R9-H2 新增 + 引擎/审批/工具/固定查询/内部数据/迁移相关）。
- 受影响套件：345 passed（迁移/应用/turn/审批/附件/fs/删除/固定查询/安全模式/提示词）。
- Linux 定向 CI：run 39958384570（backend-py311，SHA ea60595b）success，覆盖新测试与相关内部数据/绑定/限制测试。
- 完整 CI：见最终运行记录（此前 R9-H 已用 1 次；本轮最多再用 1 次；不以定向 CI 冒充完整 CI）。

## 4. 兼容与边界

- 迁移只追加（61），补偿 62 只重放 DDL；旧 structured/[] 只在同 turn 有 unavailable/unknown 审计时纠正；无证据保持；历史要求/审计/授权不删除。
- unresolved/legacy 不当无限制；unrestricted 相关操作针对性审查，不新增覆盖所有普通操作的审查；structured 明确限制/无限制行为保持。
- 内部身份：同内容复制件不保护；身份读失败时历史登记路径保守命中，新路径不猜；跨卷移动按新对象；不做全盘扫描。
- 未验证：安装版、真实模型链恢复质量、POSIX 符号链接单独真机、批量附件部分子项失效 E2E；conditions 无真实工具产生；无 move/copy/外发工具。

## 5. 合并准备

- r9h2/main 基于 r9h/main；未合并 main、未发布；最终完整 CI 成功后仅 docs 更新（[skip ci]）不改变代码。
