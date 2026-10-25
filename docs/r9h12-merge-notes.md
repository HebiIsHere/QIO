# R9-H12 审批与授权补修：UNRELATED 证据标准与未绑定对象恢复依据（合并与验收材料）

> 基线 r9h11/main 503df11a（代码 d9d806b4）。修复分支 r9h12/main；契约 docs/r9h12-contract.md；根因 docs/r9h12-root-causes.md；状态 docs/status.md P87。
> 完整 CI 本轮不新增（历史累计 2/2 已用尽）；最终代码同一 SHA 做 Linux py311/py312 与 Windows 定向 CI。

## 1. 基线与提交

| 项 | 值 |
| --- | --- |
| 基线 HEAD | 503df11a91a905dff6c1d774bcab08aec226ff09（代码 d9d806b4） |
| 修复分支 | r9h12/main（已推送 origin/r9h12/main） |
| 协作分支 | r9h12/a（UNRELATED 证据契约）、r9h12/b（保存候选恢复）、r9h12/c（适配入口）、r9h12/d（独立验收） |
| 工作区 | 独立 worktree：qio-r9h12-main/-a/-b/-c/-d；共享 venv（junction）；模型 fake/mock |
| 最终代码 SHA | 4594358f7b3602acbc28ce4f443e9c3fec691e4a（生产代码 e4e478b4；其后仅测试/证据/文档） |
| 最终 HEAD | 见最终报告（仅 docs/evidence 提交） |

## 2. 共同契约修正（先冻结，再修生产）

* 五态保留：MATCH / UNRELATED / UNBOUND / INVALID / UNKNOWN；UNRELATED 只能来自白名单依据。
* 白名单（UNRELATED_BASES 注册表 + 判定工厂强制校验；未注册/事实不足直接 ValueError）：
  operation_out_of_scope、object_kind_out_of_scope、candidate_not_exists、path_prefix_outside、service_outside、
  different_object_identity、path_different_object、path_different_lifecycle、entry_resolves_to_other_object。
* 明确禁止以这些事实单独生成 UNRELATED（改 UNBOUND/INVALID/UNKNOWN）：入口名不同、原名称查找失败、
  当前 PATH 解析到另一对象、限制路径身份不可读、保存时没有完整绑定。
* 聚合优先级：MATCH > INVALID/UNBOUND > UNKNOWN > UNRELATED；事实不足永远不能当无关放行。

## 3. 三项原始复现的修复

1. **未绑定限制经硬链接 alternate 绕过**（P1）：删除“入口名不同→UNRELATED”提前返回；保存候选
   (binding_candidates) 参与判定（同路径/同身份→UNBOUND；有候选不匹配→UNKNOWN；无候选时当前解析结果
   只作候选事实，不能证明无关）。修复后：绝对路径与 alternate 名称入口都在启动前拒绝，0 次启动、无 marker。
2. **恢复把历史限制转移到当前 PATH 对象**（P2）：新保存与历史恢复分成两个入口；recover_program_bindings
   只采纳保存候选（路径现在存在、身份可读、路径未变、候选自带 identity/birth 一致），写回前复核；
   无保存候选/候选不可验证/多冲突 → 零写入保持未解决；当前 PATH 只记审计事实，不参与绑定。
   修复后：未恢复读/执行 A 都拒绝（0 读取/0 启动）；恢复把绑定写成 A（source=saved_candidate_verified），
   绝不绑 B；恢复后 A 仍拒绝；写回条件 id/version/status/chain_digest + 采纳候选复核。
3. **显式路径身份不可读误放行**（P3）：_path_core 不再把 target_observed=None 归 UNRELATED——
   目标不存在→UNKNOWN(path_limit_target_unreadable)、存在但身份不可读→UNKNOWN(path_limit_identity_unavailable)；
   候选不存在仍可靠 UNRELATED；显式 program 路径共用。修复后：读硬链接在正文/摘要读取前拒绝（0 读取）。

## 4. 错误契约如何进入测试（修订，不删用例）

| 文件 | 旧期望 | 新期望（依据） |
| --- | --- | --- |
| test_r9h11_object_state.py | path_limit_identity_unavailable=UNRELATED | UNKNOWN（契约 §3.1/§2.4） |
| test_r9h11_object_state.py | 未绑定+当前解析到别处=UNRELATED | UNKNOWN（§3.2/§2.3） |
| test_r9h11_object_state.py | 未绑定+入口名不同=UNRELATED | UNKNOWN（§3.2/§2.1） |
| test_r9h11_object_state.py | 已绑定+入口名不同且解析失败=UNRELATED | UNKNOWN（§3.2/§2.2） |
| test_r9h11_regressions.py | 不同入口名/同名另一目录放行 | 失败关闭（0 启动），另补可靠无关守卫（D） |
| test_r9h5_regressions2.py | 未绑定+不同入口名执行 ALLOW | DENY/UNKNOWN；补操作范围外/候选不存在 ALLOW（Lead） |
| test_r9h7_regressions.py | 不同版本入口 ALLOW（仅名称差异） | 真实对象身份不同才 ALLOW（Lead） |
| test_r9h8_regressions.py | 不同版本入口 ALLOW（仅名称差异） | 真实对象身份不同才 ALLOW（Lead） |
| test_r9h11_binding_recovery.py | 无保存候选时恢复从当前 PATH 绑定 | 零写入保持未解决；有保存候选才采纳（B） |

所有修订保留原始输入与复现步骤；未删除用例、未 skip/xfail、未放宽断言、未改原因文字换绿。

## 5. 真实流程证据

* 基线 503df11a 独立验收：H12 8 例中 6 红（P1 alternate 真实启动、P2 未恢复读 A 返回正文、P3 读硬链接返回正文、
  规则反例 ALLOW、审批恢复真实启动、H11 旧守卫放行）→ 修复后 27 passed（H12 8 + H11 19），0 skip。
* Lead 复现探针（临时文件，不入库）：基线 3/3 失败（alternate 真实启动/读 A 返回正文/读硬链接返回正文）→
  修复后 3/3 通过（0 启动、0 读取、恢复采纳 A 不采纳 B）。
* H12 新增套件 97 项全绿（object_state 55 / binding_recovery 18 / cross_operation 16 / regressions 8）。
* 邻近 H11 + H4-H10 核心 260 项全绿。
* 宽回归 75 个受影响/邻近文件 1113 项：1112 passed / 1 failed（test_r9_content_binding::test_script_content_change_prevents_reuse
  在基线 503df11a 与修复后都以同一 Windows 环境原因失败，非本轮引入）/ 0 skipped。
* docs 一致性通过（95 里程碑）；agent.eval.run 与基线 503df11a 输出逐字节相同。

## 6. 测试命令（离线；模型 fake/mock）

```
cd <worktree>/backend
& ./.venv/Scripts/python.exe -m pytest tests/test_r9h12_object_state.py tests/test_r9h12_binding_recovery.py tests/test_r9h12_cross_operation.py tests/test_r9h12_regressions.py -q -p no:cacheprovider
& ./.venv/Scripts/python.exe -m pytest tests/test_r9h11_object_state.py tests/test_r9h11_binding_recovery.py tests/test_r9h11_cross_operation.py tests/test_r9h11_regressions.py tests/test_r9h10_regressions.py tests/test_r9h9_regressions.py tests/test_r9h8_regressions.py tests/test_r9h7_regressions.py tests/test_r9h6_regressions.py tests/test_r9h5_regressions2.py tests/test_r9h4_engine_binding.py tests/test_r9h4_limits_semantics.py -q -p no:cacheprovider
& ./.venv/Scripts/python.exe -m pytest @wide_files -q -p no:cacheprovider --junitxml=_lead-wide-h12-final.xml
& ./.venv/Scripts/python.exe -m agent.eval.run
```

## 7. 平台真实验证 vs 模拟

| 能力 | 本机（Windows 10.0.26xxx，py3.11.15） | 说明 |
| --- | --- | --- |
| 真实程序启动/未启动 | 真实 | cmd.exe 副本与真实硬链接入口；marker + 启动计数；另有“本可启动”控制组 |
| 真实硬链接 | 支持并验证 | P1 alternate↔runner、P3 硬链接；无 skip |
| 真实 SQLite/台账/审批 | 真实 | 含真实 approve 后恢复执行器判定 |
| 保存时/限制路径身份读取失败 | 模拟（边界注入） | agent.services.file_identity.identity_of 返回 None，用例标注模拟 |
| 编号复用/生命周期差异 | 模拟（沿用 H11 注入边界） | 平台无法稳定制造 OS file_id 复用 |
| P2 恢复期当前候选事实 | fake（审计事实，不参与采纳） | 真实 PATH 亦指向 B；恢复采纳保存候选 A |

## 8. 定向 CI

最终 SHA 4594358f（生产代码 e4e478b4；同一 SHA 三目标）：

| 目标 | run | 结论 |
| --- | --- | --- |
| backend-py311 | 40122111129 | success |
| backend-py312 | 40122291315 | success |
| windows | 40122468968 | success |

同一 SHA 的首次 py311（4594358f，run 40121305822）因一条恢复核验断言写死 identity_mismatch 而红：
Linux tmpfs 会复用 file_id，删除重建后给出 lifecycle_mismatch；两者都是“原对象已替换”的结构化失败依据，
已把断言收紧为“必须失败且原因属于这两类”，生产代码未改。首次 py312（0a7a430b，run 40121881337）
暴露修订后的版本入口测试在 POSIX 未设可执行位（which 解析落到系统解释器/查不到，绑定未形成），
已按平台设置可执行位后重跑；两次都只重跑了必要项。

## 9. 边界与已知限制

* execute 入口在 assessment 前采集 launch facts（含程序内容摘要，H5/H7 既有设计）；本轮 execute 验收口径=
  “0 次启动 + 0 次正文读取”，程序摘要不属于正文；若要连程序摘要后置需重构 launch facts（未做）。
* 审批重放网关（_ApprovedReplayGateway）未透传 assess_limit_conflict；权威判定在审批执行器入口
  authorize_started（先于 claim/execute）。属既有架构的窄 TOCTOU 窗口，未加第二次引擎调用。
* 无保存候选的历史未解决绑定本轮不做模型侧语义恢复：保持未解决、零写入，如实要求澄清/后续恢复；
  候选只有 path（保存时 identity 不可读）时，若路径被换成另一对象且候选无 identity，无法区分（不猜内容/mtime/名称）。
* 真实 file_id 复用/身份读取失败用提供器边界注入（模拟，已标注）；真实回收站/安装版/真实模型/完整矩阵未验证。
* 两个测试在 CI 首次暴露的平台假设已修正（tmpfs file_id 复用 → 接受 identity/lifecycle 两类替换依据；
  POSIX which 需要可执行位 → 测试程序按平台 chmod），生产代码未因此改动。
* 完整 CI 未新增（历史 2/2 已用尽）；不合并 main、不发布安装包。