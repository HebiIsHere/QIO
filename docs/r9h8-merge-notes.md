# R9-H8 合并与验收材料

> 基线 r9h7/main 89397527（代码 ca47d07d）；修复分支 r9h8/main；根因 docs/r9h8-root-causes.md；状态 docs/status.md P83。
> 完整 CI 本轮不新增（历史 2/2 已用尽）；最终代码同一 SHA 做 Linux py311/py312/Windows 定向 CI。

## 1. 基线与提交

| 项 | 值 |
| --- | --- |
| 基线 HEAD | 89397527（代码 ca47d07d） |
| 修复分支 | r9h8/main |
| 最终代码 SHA | 967401c218ad25285e429134deeac1f514e4da3c |
| 最终 HEAD | 见最终报告（其后仅 docs/evidence） |

## 2. 根因与修复

- 共同根因：H7 在共享限制匹配函数里把“objects 为空=限制全部对应操作”改成 False；把明确禁止但对象未绑定的 A 状态降为可普通批准的 APPROVE；内部维护用取消内容摘要换确认前不读；删除读取检查整体 return；script_sha256 同一字段承载两种事实。
- 问题一：恢复空 objects 全局语义；混合未解决对象不丢失、不误扩。
- 问题二：program_name 按平台程序解析/真实对象绑定；同对象换入口拒绝；未绑定明确禁止直接拒绝，普通批准不能覆盖；不同版本不合并。
- 问题三：内部维护确认前只绑身份/size/mtime；确认后元数据核对；等待变化不覆盖。
- 问题四：批量禁读形成子项 denied 并继续；审批只含有效项。
- 问题五：内部目录确认前 metadata-only，执行前 metadata-only 比较；不读正文。
- 问题六：script_sha256 只表示单一脚本来源；参数文件集合由 argument_files/spec_digest 表达；未变化脚本批准后执行成功。

## 3. 证据

- 基线 89397527 跑 tests/test_r9h8_regressions.py：12 项中 11 失败。
- 修复后同批：12 passed。
- 宽回归：732 passed。
- 真实工具/引擎/台账/审批/后台恢复；模型 fake/mock；批量回收 fake 移动，非真实系统回收站。

## 4. CI

- 完整 CI 未新增（历史 2/2：39930482370、39958699070 仅基线证据）。
- 最终代码定向 CI：40090355400（backend-py311）success、40090608125（backend-py312）success、40090842980（backend-windows）success。

## 5. 边界

- 内部维护元数据无法检测同 size/mtime 伪造；执行前核对到系统调用仍有竞态。
- 真实系统回收站、安装版、真实模型、完整矩阵未验证。
- program_name 未绑定对象时直接拒绝，需恢复/澄清，不能由普通批准解除。

## 6. 合并准备

- 未合并 main、未发布安装包；最终定向 CI 成功后仅 docs/evidence 更新（[skip ci]）。
