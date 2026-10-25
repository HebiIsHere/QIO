# R9-H4 合并与验收材料（审批与授权大修）

> 基线：r9h3/main = 68101d6261253bb742d01af73a458bab2e0a1c5a（代码 7f877bf3；定向 CI 仅基线证据）
> 完整 CI 累计 2/2 已用尽；本轮未新增完整 CI，仅定向 CI。修复分支 r9h4/main；契约 docs/r9h4-contracts.md；状态 docs/status.md P79。

## 1. 基线与提交

| 项 | 值 |
| --- | --- |
| 审查基线 | 68101d62（代码 7f877bf3） |
| 开工核对 | fetch 后无后续提交，工作区干净 |
| 修复分支 | r9h4/main |
| 子分支 | A 0fd12257、B bf818047、C 3d35e8e6、D cb0fed0f、E 666b3437、F ccacd114 |
| 最终代码 SHA | 76e6248bfc6694367c43ef7fab4f7f9a9907fe11（定向 CI 验证；其后仅 docs/evidence） |

## 2. 修复与证据

- 实际影响/限制匹配：A 22、B 27、C 10、D 13；D 的 program/delete 真实执行测试已无 xfail；Lead 真实读取限制经保存/重启仍拒绝。
- 限制继承与精确解除：A 22 项，覆盖父 known 继承、部分对象/操作解除、无法精确保守。
- 撤回/内容一致性：B 27、D 13、F 19；Lead 真实文件等待变化不采用旧许可。
- 目录/批量：C 10、Lead 真实目录审批三次确认；目录新增/移除/替换失效；平台去重。
- 内部身份/确认：E 24、F 19；Linux statx btime 定向通过；Windows 硬链接已验证。
- 子智能体/审批并发：B scope 测试、F 19、proc.kill 重放测试。
- 核心/宽/受影响：118/150、553、336 通过；迁移 65/66 与补偿通过。

## 3. 定向 CI

- Linux 40008364084（backend-py311）success。
- Linux 40008722828（backend-py312）success。
- Windows 40009077589（backend-windows）success（首次 test_r9g_acceptance_e2e 的 sqlite 跨线程 InterfaceError 既有 flake，按纪律只重跑失败 job 后通过）。
- 完整 CI 未新增：累计 2/2（39930482370、39958699070）仅基线；最终代码无完整矩阵结果，需额外额度。

## 4. 边界

- Linux 仅 statx 支持 btime 的文件系统可区分编号复用；不支持时生命周期未知（路径/历史保护，不凭裸编号声称同一文件）；POSIX ctime 不作生命周期。
- 跨卷移动按新对象；目录影响有界上限超限明确不确定；Windows 符号链接权限不足如实记录。
- 安装版/真实模型/完整 CI 未验证；额外完整 CI 需新增额度。

## 5. 合并准备

- r9h4/main 基于 r9h3/main；未合并 main、未发布；最终定向 CI 成功后仅 docs/evidence 更新（[skip ci]）不改变代码。
