# R9-H3 合并与验收材料（第三轮后续补修）

> 基线：r9h2/main = 7b66092e01f57121067c48588e8c3a30237367d0（代码 8cb92eab6d9724c910ed8b7b8642c149be0c5bde；完整 CI 39958699070 success）
> 修复分支：r9h3/main（worktree D:/qio-dev/qio-r9h3-main）；契约 docs/r9h3-contracts.md；状态 docs/status.md P78
> **累计完整 CI 2/2 已用尽：本轮未触发第三次完整 CI，也没有换工作流绕过预算；最终代码只做定向 CI。**

## 1. 基线与提交

| 项 | 值 |
| --- | --- |
| 审查基线 | 7b66092e（docs/evidence）；代码 8cb92eab |
| 开工核对 | fetch 后 origin/r9h2/main 仍为 7b66092e，无后续提交；工作区干净 |
| 修复分支 | r9h3/main |
| 子分支 | A r9h3/a-limits = c204195d；B r9h3/b-unrestricted = b75673ed；C r9h3/c-identity = d5e5fbab |
| 最终代码 SHA | 7f877bf3547b85a38c6209acb91971c453d3596d（定向 CI 验证；其后仅 docs/evidence 更新 [skip ci]） |
| 集成 | 契约/迁移 8d97904f → A/B/C 合并 beef5163/26f44445/b596e1d6 → 审查清理 f714fe79 |

## 2. 三处问题

### 2.1 已知限制被后续审查覆盖（A）
- 根因：_verdict 先命中 unresolved 具体判断分支，之后才做 _limit_conflict；模型审查/旧 allow 后已知限制未执行。
- 修复：限制冲突提前到 internal_data/scope/永久删除/固定查询/模式/unresolved guard 之前；recheck_approved 同步；INTERNAL_DATA 映射 write；旧 allow/人工批准/内部数据确认/模式不能覆盖；补充解除按原语义/版本机制。
- 证据：tests/test_r9h3_limits_priority.py 11 passed（真实台账/引擎/RecheckService）：unresolved+known 写 B DENY、reviewer 0；范围外 A 不 DENY；always-ALLOW 不能覆盖；旧 allow/批准/内部数据/永久删除不能覆盖；真实解除后恢复；批量仅 B DENY；执行前新增限制 recheck DENY。

### 2.2 完全无限制模式重新进入独立审查（B/A）
- 根因：targeted_limits_review 让 unrestricted 调用普通 ModelAuthorizationReviewer，提示词仍判断操作授权，标记未进模型内容，重复核对重复调用。
- 修复：删除字段/参数/校验例外/engine 路径；review.py 仅允许 restricted；engine unrestricted 不再调用审查；unresolved 返回具体确认；迁移 63 撤销可识别的 targeted 授权记录（保留审计），补偿 64 只重放 DDL，版本 64。
- 证据：tests/test_r9h3_unrestricted_no_review.py 14 passed（真实引擎/台账/审批）：unrestricted unresolved/structured/批量/重复 authorize+recheck 审查器调用 0；known limits DENY；恢复成功继续；受限仍审查；read_only 不新增；内部数据/永久删除规则不变；旧 targeted 记录撤销后不可复用。

### 2.3 正常内部文件替换后身份不更新（C）
- 根因：identity store 只在启动/显式刷新更新；真实提交后的新对象未登记，硬链接在刷新前按普通文件。
- 修复：调查实际生命周期；AppContext 包装真实审批执行器 operations.execute，fs.write/fs.patch 提交成功后按明确 payload 目标 note_committed_path；SettingsService 提交成功后通知 DB 路径；note_committed_path 读真实身份、旧身份 retired、新身份 active；失败记录错误/保留旧登记；普通工具不猜；有界更新。
- 证据：tests/test_r9h3_identity_refresh.py 10 passed（真实 AppContext/真实审批执行器/真实设置服务）：原子替换后不刷新/不重启新对象硬链接立即受保护（读 DENY、写三次确认）；新建/重建登记；相对/规范化/硬链接/符号链接或平台别名/改名移动一致；设置保存成功触发、失败不触发；身份更新失败不静默降级；普通复制件/同名不误伤。

## 3. 定向验证与 CI

- A 11、B 14、C 10、迁移 63/64 测试通过；宽回归 431 passed；受影响套件 330 passed；审查清理回归 124 passed。
- 最终代码定向 CI（SHA 7f877bf3547b85a38c6209acb91971c453d3596d）：
  - run 39979313725（backend-py311）success；
  - run 39979659563（backend-py312）success；
  - run 39979919730（backend-windows）success。
  - 首次定向 run 39978484989 暴露 C 测试在 POSIX 上 logs/sub/.. 未建父目录的平台假设错误，已修测试并重跑成功，未放宽产品断言。
- **完整 CI 未新增**：累计 2/2（39930482370、39958699070）均 Success，仅作基线证据；本轮最终代码没有完整矩阵结果，需额外额度才能声称完整 CI 成功。
- 不删除检查、不降低阈值、不放宽断言；未通过 skip/xfail/continue-on-error 隐藏失败。

## 4. 兼容与边界

- 迁移 63 只撤销可明确识别（mode=unrestricted + source=model_review）的 targeted 例外记录，保留行/审计；补偿 64 只重放 DDL；版本 64。
- 已知限制优先于内部数据三次确认、旧 allow、人工批准、权限模式；只覆盖明确对象/操作，不扩大成全部拒绝。
- 完全无限制模式恢复既定行为；限制恢复专用流程不承担操作授权职责、不生成普通授权记录；恢复失败保持 unknown 并落实已知限制。
- 内部身份：凭据无独立文件未单独接线；进程外日志轮转不可感知；原子替换退休旧身份但不全盘扫描旧对象引用；普通复制件/同名不误登记。
- 未验证：安装版、真实模型、完整 CI、POSIX 符号链接专项、跨卷移动身份变化；Windows 本机硬链接已验证。

## 5. 合并准备

- r9h3/main 基于 r9h2/main；未合并 main、未发布；最终定向 CI 成功后仅 docs 更新（[skip ci]）不改变代码。
